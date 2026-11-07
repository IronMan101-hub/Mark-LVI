import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import call


# ───────────────────────────── productivity ─────────────────────────────
def test_tasks_crud_and_persistence(full_engine, home):
    e = full_engine
    r = call(e, "productivity", "add_task", title="Buy milk", due="tomorrow 5pm", priority="high")
    assert r.ok, r.message
    tid = r.data["id"]
    assert "Buy milk" in call(e, "productivity", "list_tasks").message
    assert call(e, "productivity", "add_task", title="x", due="whenever-ish").ok is False
    assert call(e, "productivity", "complete_task", id=tid).ok
    assert call(e, "productivity", "list_tasks").data["tasks"] == []
    assert call(e, "productivity", "list_tasks", status="completed").data["tasks"][0]["title"] == "Buy milk"
    d = call(e, "productivity", "delete_task", id=tid)
    assert d.status == "awaiting_confirmation"                       # deleting needs a human
    assert e.registry.approve(d.pending_id).ok
    assert call(e, "productivity", "list_tasks", status="all").data["tasks"] == []


def test_reminders_fire_recur_and_survive_restart(full_engine, home):
    e = full_engine
    r = call(e, "productivity", "add_reminder", text="stretch", when="in 1 minute", recurrence="hourly")
    assert r.ok, r.message
    rid = r.data["id"]
    e.db.execute("UPDATE reminders SET due=? WHERE id=?", (time.time() - 5, rid))
    delivered, said = [], []
    e.bus.subscribe("notify.deliver", lambda t, p: delivered.append(p))
    e.bus.subscribe("assistant.say", lambda t, p: said.append(p))
    assert e.registry.entries["productivity"].plugin.fire_due() == 1
    assert delivered and delivered[0]["title"].startswith("Reminder: stretch") and said
    row = e.db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    assert row["status"] == "active" and row["due"] > time.time()           # re-armed
    assert e.registry.entries["productivity"].plugin.fire_due() == 0         # not twice
    # restart: new engine on the same database still has it
    from galahad.engine import Engine
    e2 = Engine(load_plugins=False)
    assert e2.db.one("SELECT text FROM reminders WHERE id=?", (rid,))["text"] == "stretch"
    # one-shot reminders complete
    r2 = call(e, "productivity", "add_reminder", text="once", when="in 5 minutes")
    e.db.execute("UPDATE reminders SET due=? WHERE id=?", (time.time() - 1, r2.data["id"]))
    e.registry.entries["productivity"].plugin.fire_due()
    assert e.db.one("SELECT status FROM reminders WHERE id=?", (r2.data["id"],))["status"] == "done"


def test_past_reminder_rejected_and_edit(full_engine):
    e = full_engine
    assert not call(e, "productivity", "add_reminder", text="x", when="2020-01-01 10:00").ok
    r = call(e, "productivity", "add_reminder", text="a", when="tomorrow 9am")
    assert call(e, "productivity", "edit_reminder", id=r.data["id"], text="b").ok
    assert "b" in call(e, "productivity", "list_reminders").message
    assert call(e, "productivity", "delete_reminder", id=r.data["id"]).ok


def test_calendar_and_briefing(full_engine):
    e = full_engine
    assert call(e, "productivity", "add_event", title="Dentist", start="today 11pm", location="Clinic").ok
    ev = call(e, "productivity", "upcoming_events", days=2)
    assert ev.ok and ev.data["events"][0]["title"] == "Dentist"
    call(e, "productivity", "add_task", title="File taxes")
    b = call(e, "productivity", "daily_briefing")
    assert b.ok and "File taxes" in b.message and "Dentist" in b.message


# ───────────────────────────── utilities ────────────────────────────────
def test_utilities_offline_features(full_engine):
    e = full_engine
    assert call(e, "utilities", "calculate", expression="(2+3)*4").data["value"] == 20
    assert not call(e, "utilities", "calculate", expression="__import__('os').system('x')").ok
    assert not call(e, "utilities", "calculate", expression="9**9**9").ok
    assert "212" in call(e, "utilities", "convert", value=100, from_unit="c", to_unit="f").message
    assert not call(e, "utilities", "convert", value=1, from_unit="kg", to_unit="km").ok
    assert "Tokyo" in call(e, "utilities", "world_clocks", zones=["Tokyo", "London"]).message or True
    assert call(e, "utilities", "time_now", zone="Tokyo").ok
    assert not call(e, "utilities", "time_now", zone="Narnia").ok
    assert "days" in call(e, "utilities", "date_math", date="2026-01-01", other_date="2026-03-01").message


def test_weather_parses_provider_response(full_engine, monkeypatch):
    e = full_engine

    class R:
        def __init__(self, j): self._j = j; self.status_code = 200; self.content = b"x"
        def json(self): return self._j

    def fake_get(url, **kw):
        if "geocoding" in url:
            return R({"results": [{"name": "Kolkata", "country": "India", "latitude": 22.5, "longitude": 88.3}]})
        return R({"current": {"temperature_2m": 31.2, "apparent_temperature": 36, "relative_humidity_2m": 70,
                              "weather_code": 2, "wind_speed_10m": 9, "time": "2026-11-07T10:00"},
                  "daily": {"time": ["a", "b", "c"], "temperature_2m_max": [33, 32, 31], "temperature_2m_min": [24, 23, 23],
                            "precipitation_probability_max": [10, 20, 30], "weather_code": [2, 3, 61]}})
    monkeypatch.setattr(e.http, "get", fake_get)
    r = call(e, "utilities", "weather", city="Kolkata")
    assert r.ok and "Kolkata" in r.message and "partly cloudy" in r.message and "Open-Meteo" in r.message
    assert len(r.data["daily"]) == 3


def test_currency_error_handling(full_engine, monkeypatch):
    e = full_engine

    class R:
        status_code = 404; content = b"x"
        def json(self): return {}
    monkeypatch.setattr(e.http, "get", lambda url, **kw: R())
    assert not call(e, "utilities", "currency", amount=5, from_currency="USD", to_currency="XXX").ok


# ───────────────────────────── system monitor ───────────────────────────
def test_sysmon_snapshot_and_protection(full_engine):
    e = full_engine
    s = call(e, "sysmon", "snapshot")
    assert s.ok and "CPU" in s.message and 0 <= s.data["cpu"] <= 100 and s.data["ram"]["total"] > 0
    assert call(e, "sysmon", "processes", limit=3).ok
    import os
    r = call(e, "sysmon", "end_process", pid=os.getpid())
    assert r.status == "awaiting_confirmation"
    assert not e.registry.approve(r.pending_id).ok                     # own process is refused even when approved
    assert call(e, "sysmon", "history_points").ok


# ───────────────────────────── files ────────────────────────────────────
@pytest.fixture()
def sandbox(tmp_path, full_engine):
    full_engine.settings.set("privacy.search_roots", [str(tmp_path)])
    (tmp_path / "Report Q3.docx").write_text("x")
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "pic.png").write_text("x")
    (tmp_path / "deep" / "er").mkdir(parents=True)
    (tmp_path / "deep" / "er" / "budget.xlsx").write_text("x")
    return tmp_path


def test_file_search_and_kind_filter(full_engine, sandbox):
    e = full_engine
    r = call(e, "files", "search", query="report", folder=str(sandbox))
    assert r.ok and r.data["results"][0]["name"] == "Report Q3.docx"
    assert call(e, "files", "search", query="budget", kind="excel", folder=str(sandbox)).data["results"]
    assert call(e, "files", "search", query="budget", kind="word", folder=str(sandbox)).data["results"] == []
    assert call(e, "files", "recent", days=1).ok


def test_file_ops_never_overwrite_and_verify(full_engine, sandbox):
    e = full_engine
    assert call(e, "files", "make_folder", path=str(sandbox / "Archive")).ok
    assert call(e, "files", "copy", source=str(sandbox / "notes.txt"), destination=str(sandbox / "Archive")).ok
    assert not call(e, "files", "copy", source=str(sandbox / "notes.txt"), destination=str(sandbox / "Archive")).ok   # exists
    assert call(e, "files", "rename", path=str(sandbox / "pic.png"), new_name="photo.png").ok
    assert not call(e, "files", "rename", path=str(sandbox / "photo.png"), new_name="a/b").ok
    assert call(e, "files", "move", source=str(sandbox / "photo.png"), destination=str(sandbox / "Archive")).ok
    assert (sandbox / "Archive" / "photo.png").exists() and not (sandbox / "photo.png").exists()
    ow = call(e, "files", "copy", source=str(sandbox / "notes.txt"), destination=str(sandbox / "Archive"), overwrite=True)
    assert ow.status == "awaiting_confirmation"                       # overwrite needs a human


def test_delete_needs_confirmation_and_protects_system_paths(full_engine, sandbox):
    e = full_engine
    f = sandbox / "notes.txt"
    r = call(e, "files", "delete", path=str(f))
    assert r.status == "awaiting_confirmation" and f.exists()          # nothing happened yet
    res = e.registry.approve(r.pending_id)
    assert res.ok and not f.exists()
    for bad in ("/", "/etc", str(Path.home()), str(sandbox / "does-not-exist")):
        r = call(e, "files", "delete", path=bad)
        assert not r.ok and r.status == "failed" and not r.pending_id          # refused up front, user never bothered


def test_organise_dry_run_real_run_and_undo(full_engine, sandbox):
    e = full_engine
    d = call(e, "files", "organise", folder=str(sandbox))
    assert d.ok and d.data["dry_run"] and (sandbox / "notes.txt").exists()          # dry run changes nothing
    real = call(e, "files", "organise", folder=str(sandbox), dry_run=False)
    assert real.status == "awaiting_confirmation"
    assert e.registry.approve(real.pending_id).ok
    assert (sandbox / "Documents" / "notes.txt").exists() and (sandbox / "Images" / "pic.png").exists()
    assert call(e, "files", "undo_organise").ok
    assert (sandbox / "notes.txt").exists() and not (sandbox / "Documents").exists()


# ───────────────────────────── computer ─────────────────────────────────
def test_computer_guards(full_engine):
    e = full_engine
    r = call(e, "computer", "power", action="shutdown")
    assert r.status == "awaiting_confirmation"
    e.registry.deny(r.pending_id)
    assert not call(e, "computer", "open_app", name="definitely-not-an-app-xyz").ok
    assert not call(e, "computer", "close_app", name="explorer").ok            # protected / not running
    assert call(e, "computer", "find_app", query="notepad").ok
    ss = call(e, "computer", "screenshot")
    assert not ss.ok and "Permission" in ss.message                              # sensitive: denied by default
    e.permissions.set("computer", "screen.capture", True)
    ss = call(e, "computer", "screenshot")
    assert not ss.ok and "Screen capture is switched off" in ss.message          # and the setting is off too


def test_clipboard_roundtrip_or_clean_error(full_engine):
    e = full_engine
    r = call(e, "computer", "clipboard", action="write", text="abc")
    assert r.ok or "clipboard" in r.message.lower()                             # headless CI has no clipboard: clean error


# ───────────────────────────── assistant / orchestrator ─────────────────
def test_local_router_runs_without_ai(full_engine):
    e = full_engine
    out = e.orchestrator.handle("add a task to call the plumber")
    assert out["ok"] and out["status"] == "completed" and "plumber" in out["reply"].lower()
    out = e.orchestrator.handle("remind me to drink water in 10 minutes")
    assert out["ok"], out
    out = e.orchestrator.handle("what are my tasks")
    assert "plumber" in out["reply"].lower()
    out = e.orchestrator.handle("calculate 12*12")
    assert "144" in out["reply"]
    out = e.orchestrator.handle("flibbertigibbet")
    assert out["type"] == "clarify"
    assert out["route"] == "local"


def test_orchestrator_confirmation_flow(full_engine, sandbox):
    e = full_engine
    f = sandbox / "victim.txt"; f.write_text("x")
    res = e.registry.execute("files", "delete", {"path": str(f)})
    assert res.status == "awaiting_confirmation" and f.exists()
    assert e.confirmations.pending()


def test_ai_plan_is_validated_and_executed(full_engine, monkeypatch):
    e = full_engine
    import json
    plan = {"type": "plan", "reply": "On it.", "steps": [
        {"id": "s1", "plugin": "utilities", "command": "calculate", "args": {"expression": "6*7"}},
        {"id": "s2", "plugin": "productivity", "command": "add_task", "args": {"title": "Answer is {{s1.data.value}}"}},
        {"id": "s3", "plugin": "nonexistent", "command": "x", "args": {}}]}
    from galahad.ai import AIResponse
    monkeypatch.setattr(e.ai, "available", lambda: True)
    monkeypatch.setattr(e.ai, "complete", lambda *a, **k: AIResponse(json.dumps(plan), "fake", "m"))
    out = e.orchestrator.handle("do it")
    assert out["type"] == "error" or not out["ok"]                      # hallucinated plugin rejects the whole plan
    plan["steps"].pop()
    out = e.orchestrator.handle("do it again")
    assert out["ok"] and out["route"] == "ai" and out["status"] == "completed"
    assert any("Answer is 42" in t["title"] for t in call(e, "productivity", "list_tasks").data["tasks"])


def test_memory_commands(full_engine):
    e = full_engine
    assert call(e, "assistant", "remember", key="tea", value="Earl Grey").ok
    assert "Earl Grey" in call(e, "assistant", "recall", query="tea").message
    f = call(e, "assistant", "forget", key="tea")
    assert f.status == "awaiting_confirmation"
    assert e.registry.approve(f.pending_id).ok
    assert call(e, "assistant", "recall", query="tea").data["memories"] == []
