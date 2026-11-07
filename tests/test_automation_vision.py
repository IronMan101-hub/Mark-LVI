import json
import time

import pytest

from tests.conftest import call


def wait_run(e, rid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        r = e.workflows.get_run(rid)
        if r["status"] not in ("running", "awaiting_confirmation"):
            return r
        time.sleep(0.05)
    return e.workflows.get_run(rid)


STEPS = [{"id": "s1", "plugin": "utilities", "command": "calculate", "args": {"expression": "20+22"}},
         {"id": "s2", "plugin": "productivity", "command": "add_task", "args": {"title": "Result {{s1.data.value}}"}, "condition": {"step": "s1", "op": "ok"}},
         {"id": "s3", "plugin": "productivity", "command": "add_task", "args": {"title": "never"}, "condition": {"step": "s1", "field": "data.value", "op": "gt", "value": 100}}]


def test_workflow_data_flow_conditions_and_history(full_engine):
    e = full_engine
    r = call(e, "automation", "save", name="calc-and-note", steps=STEPS, description="d")
    assert r.ok, r.message
    run = call(e, "automation", "run", workflow="calc-and-note")
    rr = wait_run(e, run.data["run_id"])
    assert rr["status"] == "completed", rr
    st = {p["id"]: p["status"] for p in rr["progress"]}
    assert st == {"s1": "completed", "s2": "completed", "s3": "skipped"}
    titles = [t["title"] for t in call(e, "productivity", "list_tasks").data["tasks"]]
    assert "Result 42" in titles and "never" not in titles
    h = call(e, "automation", "history")
    assert h.data["runs"][0]["status"] == "completed"
    assert any(a["run_id"] == run.data["run_id"] for a in e.activity.recent(20))


def test_failure_stops_or_continues_and_notifies(full_engine):
    e = full_engine
    steps = [{"id": "a", "plugin": "utilities", "command": "calculate", "args": {"expression": "1/0"}},
             {"id": "b", "plugin": "productivity", "command": "add_task", "args": {"title": "after failure"}}]
    w = e.workflows.save("stops", steps)
    r = wait_run(e, e.workflows.run_async(w["id"]))
    assert r["status"] == "failed" and "step a failed" in r["error"]
    assert not any(t["title"] == "after failure" for t in call(e, "productivity", "list_tasks").data["tasks"])
    assert any("Workflow failed" in n["title"] for n in e.notifications.list())
    steps[0]["on_error"] = "continue"
    w2 = e.workflows.save("continues", steps)
    r2 = wait_run(e, e.workflows.run_async(w2["id"]))
    assert r2["status"] == "completed" and r2["progress"][0]["status"] == "failed"


def test_retries(full_engine, monkeypatch):
    e = full_engine
    n = {"c": 0}
    real = e.registry.execute
    def flaky(plugin, cmd, args=None, origin="user", run_id=None):
        if plugin == "utilities" and cmd == "calculate":
            n["c"] += 1
            if n["c"] < 3:
                from galahad.plugin_api import CommandResult
                return CommandResult(False, "temporary glitch", status="failed")
        return real(plugin, cmd, args, origin, run_id)
    monkeypatch.setattr(e.registry, "execute", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None) if False else None
    w = e.workflows.save("retry", [{"id": "s1", "plugin": "utilities", "command": "calculate", "args": {"expression": "2+2"}, "retries": 3}])
    assert wait_run(e, e.workflows.run_async(w["id"]))["status"] == "completed" and n["c"] == 3


def test_workflow_pauses_for_confirmation_and_never_bypasses_it(full_engine, tmp_path):
    e = full_engine
    f = tmp_path / "doomed.txt"; f.write_text("x")
    w = e.workflows.save("cleanup", [{"id": "d", "plugin": "files", "command": "delete", "args": {"path": str(f)}}])
    rid = e.workflows.run_async(w["id"])
    time.sleep(0.8)
    assert e.workflows.get_run(rid)["status"] == "awaiting_confirmation" and f.exists()
    pend = e.confirmations.pending()
    assert pend and pend[0]["origin"] == "user"
    e.registry.approve(pend[0]["id"])
    assert wait_run(e, rid)["status"] == "completed" and not f.exists()
    # denial cancels the step and the run fails without deleting
    g = tmp_path / "kept.txt"; g.write_text("x")
    w2 = e.workflows.save("cleanup2", [{"id": "d", "plugin": "files", "command": "delete", "args": {"path": str(g)}}])
    rid2 = e.workflows.run_async(w2["id"])
    time.sleep(0.8)
    e.registry.deny(e.confirmations.pending()[0]["id"])
    assert wait_run(e, rid2)["status"] in ("failed", "cancelled") and g.exists()


def test_cancel_running_workflow(full_engine):
    e = full_engine
    steps = [{"id": f"s{i}", "plugin": "utilities", "command": "connectivity", "args": {}} for i in range(1, 4)]
    steps = [{"id": "w", "plugin": "files", "command": "search", "args": {"query": "zzzz-nothing", "folder": "/usr/lib"}}] + steps
    w = e.workflows.save("slow", steps)
    rid = e.workflows.run_async(w["id"])
    e.workflows.cancel(rid)
    r = wait_run(e, rid, 20)
    assert r["status"] in ("cancelled", "completed", "failed")


def test_validation_rejects_bad_plans(full_engine):
    e = full_engine
    assert not call(e, "automation", "save", name="x", steps=[{"plugin": "nope", "command": "x"}]).ok
    assert not call(e, "automation", "save", name="x", steps=[{"plugin": "utilities", "command": "nope"}]).ok
    assert not call(e, "automation", "save", name="x", steps=[]).ok
    ok = {"plugin": "utilities", "command": "calculate", "args": {"expression": "1"}}
    assert not call(e, "automation", "save", name="x", steps=[ok], trigger={"type": "schedule", "at": "25:99"}).ok
    assert not call(e, "automation", "save", name="x", steps=[ok], trigger={"type": "interval", "minutes": 0}).ok
    assert not call(e, "automation", "save", name="x", steps=[ok], trigger={"type": "teleport"}).ok
    assert not call(e, "automation", "save", name="x", steps=[{**ok, "id": "a"}, {**ok, "id": "a"}]).ok


def test_schedule_interval_and_event_triggers(full_engine):
    e = full_engine
    step = [{"id": "s", "plugin": "productivity", "command": "add_task", "args": {"title": "ticked"}}]
    w = e.workflows.save("every", step, trigger={"type": "interval", "minutes": 5})
    assert e.workflows.get(w["id"])["next_run"] > time.time()
    e.db.execute("UPDATE workflows SET next_run=? WHERE id=?", (time.time() - 1, w["id"]))
    assert e.workflows.tick() == 1
    time.sleep(1.0)
    assert any(t["title"] == "ticked" for t in call(e, "productivity", "list_tasks").data["tasks"])
    assert e.workflows.get(w["id"])["next_run"] > time.time()
    e.workflows.set_enabled(w["id"], False)
    e.db.execute("UPDATE workflows SET next_run=? WHERE id=?", (time.time() - 1, w["id"]))
    assert e.workflows.tick() == 0                                   # disabled never fires
    ev = e.workflows.save("on-event", [{"id": "s", "plugin": "productivity", "command": "add_task", "args": {"title": "heard {{event.topic}}"}}],
                          trigger={"type": "event", "topic": "custom.ping"})
    e.bus.publish("custom.ping", {"x": 1})
    time.sleep(1.0)
    assert any(t["title"] == "heard custom.ping" for t in call(e, "productivity", "list_tasks").data["tasks"])
    from galahad.workflows import WorkflowEngine
    nxt = WorkflowEngine._next_run({"type": "schedule", "at": "08:00", "days": [0]}, __import__("datetime").datetime(2026, 11, 7, 9, 0))
    assert __import__("datetime").datetime.fromtimestamp(nxt).weekday() == 0


def test_workflows_survive_restart_and_interrupted_runs_are_recovered(full_engine, home):
    e = full_engine
    e.workflows.save("keep", [{"id": "s", "plugin": "utilities", "command": "calculate", "args": {"expression": "1+1"}}])
    e.db.execute("INSERT INTO workflow_runs(name,request,plan,status,progress,started) VALUES('stuck','r','[]','running','[]',?)", (time.time(),))
    from galahad.engine import Engine
    e2 = Engine(load_plugins=False)
    e2.workflows.recover_interrupted()
    assert [w["name"] for w in e2.workflows.list()] == ["keep"]
    assert e2.workflows.runs(5)[0]["status"] == "cancelled"


def test_plan_preview_and_create_from_text_local(full_engine):
    e = full_engine
    p = call(e, "automation", "plan", request="add a task to water plants")
    assert p.ok and p.data["steps"][0]["command"] == "add_task"
    assert not call(e, "automation", "plan", request="zxcvbnm").ok
    c = call(e, "automation", "create_from_text", name="plants", request="add a task to water plants", trigger={"type": "schedule", "at": "08:00"})
    assert c.ok and c.data["workflow"]["trigger"]["at"] == "08:00"


# ───────────────────────────── vision ───────────────────────────────────
@pytest.fixture()
def img(tmp_path):
    from PIL import Image, ImageDraw
    p = tmp_path / "shot.png"
    im = Image.new("RGB", (200, 80), "white"); ImageDraw.Draw(im).text((10, 30), "HELLO", fill="black"); im.save(p)
    return p


def test_screen_capture_is_off_and_gated(full_engine):
    e = full_engine
    assert "OFF" in call(e, "vision", "status").message
    d = call(e, "vision", "describe_screen")
    assert d.status == "failed" and "Permission" in d.message                 # sensitive permission denied by default
    e.permissions.set("vision", "screen.capture", True)
    d = call(e, "vision", "describe_screen")
    assert d.status == "awaiting_confirmation"
    res = e.registry.approve(d.pending_id)
    assert not res.ok and "switched off" in res.message


def test_describe_image_asks_consent_then_sends_bytes_and_audits(full_engine, img, monkeypatch):
    e = full_engine
    sent = {}
    monkeypatch.setattr(e.ai, "order", lambda purpose: ["openai"])
    def fake_call(pid, cfg, prompt, system, history, json_mode, max_tokens, temperature, images=(), timeout=60):
        sent["images"], sent["prompt"] = images, prompt
        return "A white box that says HELLO", 10, 5
    monkeypatch.setattr(e.ai, "_call", fake_call)
    r = call(e, "vision", "describe_image", path=str(img))
    assert r.status == "awaiting_confirmation" and not sent                    # nothing is sent before the user agrees
    assert "AI provider" in e.confirmations.pending()[0]["summary"]
    res = e.registry.approve(r.pending_id)
    assert res.ok and "HELLO" in res.message
    assert sent["images"][0][0] == "image/png" and sent["images"][0][1][:4] == b"\x89PNG" and "untrusted" in sent["prompt"]
    assert any(a["action"] == "ai.private_send" for a in e.activity.recent(10))


def test_vision_rejects_non_images_and_huge_files(full_engine, tmp_path):
    e = full_engine
    t = tmp_path / "a.txt"; t.write_text("x")
    r = call(e, "vision", "describe_image", path=str(t))
    assert not e.registry.approve(r.pending_id).ok
    big = tmp_path / "big.png"; big.write_bytes(b"0" * 7_000_000)
    r = call(e, "vision", "describe_image", path=str(big))
    assert not e.registry.approve(r.pending_id).ok


def test_ai_adapters_attach_images(full_engine, monkeypatch):
    e = full_engine
    seen = []
    class R:
        status_code = 200; content = b"x"
        def __init__(self, j): self._j = j
        def json(self): return self._j
    def post(url, **kw):
        seen.append((url, kw["json"]))
        if "anthropic" in url: return R({"content": [{"type": "text", "text": "ok"}], "usage": {}})
        if "generativelanguage" in url: return R({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        return R({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(e.http, "post", post)
    for pid in ("openai", "anthropic", "gemini"):
        e.ai.configure(pid, api_key="k")
        assert e.ai.complete("what is this", provider=pid, images=[("image/png", b"PNGDATA")]).text == "ok"
    bodies = json.dumps([b for _u, b in seen])
    assert "image_url" in bodies and '"type": "image"' in bodies and "inline_data" in bodies
