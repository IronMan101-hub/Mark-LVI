"""Phase-14 style checks: contracts across ALL plugins, persistence across restarts,
background operation, security properties and resource use."""
import json
import os
import sys
import time
import types
from pathlib import Path

import psutil
import pytest

from tests.conftest import call


# ───────────────────────────── contracts ───────────────────────────────
def test_every_plugin_loads_and_honours_the_contract(full_engine):
    from galahad.plugin_api import CATEGORIES, PARAM_TYPES
    from galahad.permissions import CATALOG
    reg = full_engine.registry
    infos = reg.list()
    assert len(infos) == 22 and not [i for i in infos if i["state"] == "error"], [(i["id"], i["error"]) for i in infos if i["state"] == "error"]
    assert {i["category"] for i in infos} == set(CATEGORIES)                      # all 12 requested categories are populated
    for ent in reg.entries.values():
        m = ent.manifest
        assert m.version and m.author and m.description and m.name
        assert set(m.permissions) <= set(CATALOG)
        assert ent.commands, m.id
        for n, c in ent.commands.items():
            assert c.description, f"{m.id}.{n} lacks a description"
            assert set(c.permissions) <= set(m.permissions), f"{m.id}.{n}"
            for pn, ps in c.params.items():
                assert ps.get("type", "string") in PARAM_TYPES
            if c.destructive:
                assert c.confirm, f"{m.id}.{n} is destructive but has no confirmation"
        h = reg.health(m.id)
        assert h.status in ("ok", "unconfigured", "degraded", "unsupported", "error")


def test_dangerous_commands_are_all_gated(full_engine):
    """The safeguards the brief calls out must exist on the right commands."""
    c = lambda p, n: full_engine.registry.entries[p].commands[n]            # noqa: E731
    for p, n in (("files", "delete"), ("computer", "power"), ("gmail", "send"), ("gmail", "reply"), ("whatsapp", "send"),
                 ("sysmon", "end_process"), ("productivity", "delete_task"), ("automation", "delete"), ("finance", "delete_entry"),
                 ("word", "create"), ("excel", "create"), ("powerpoint", "create"), ("word", "save_as"), ("files", "organise"),
                 ("vision", "describe_screen"), ("vision", "describe_image"), ("assistant", "forget")):
        assert c(p, n).confirm, f"{p}.{n} must ask first"
    # reading mail / screen / finance are sensitive, default-deny permissions
    from galahad.permissions import CATALOG
    assert all(CATALOG[x][1] for x in ("mail.read", "screen.capture", "finance.data"))
    assert not full_engine.permissions.is_granted("gmail", "mail.read")
    assert not full_engine.permissions.is_granted("vision", "screen.capture")


def test_voice_catalog_matches_the_live_registry(full_engine):
    from galahad.bridge import CATALOG, build_catalog, GROUPS
    on_disk = json.loads(CATALOG.read_text(encoding="utf-8"))
    live = build_catalog(full_engine)
    assert on_disk == live, "galahad/catalog.json is stale — run: python -m galahad catalog"
    grouped = {p for _d, ps in GROUPS.values() for p in ps}
    assert grouped == set(live), "every plugin must be reachable by voice through exactly one bridge tool"
    for name in GROUPS:
        assert Path(__file__).resolve().parent.parent.joinpath("plugins", name + ".py").exists()


def test_bridge_runs_through_the_legacy_plugin_loader(full_engine, monkeypatch):
    import galahad.engine as ge
    monkeypatch.setattr(ge, "_engine", full_engine)                       # the bridge talks to the process-wide engine
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.plugin_loader import discover_plugins
    reg = discover_plugins(plugins_dir=Path(__file__).resolve().parent.parent / "plugins", core_tool_names=set(), logger=lambda m: None)
    names = {d["name"] for d in reg.get_tool_declarations()}
    assert {"galahad_productivity", "galahad_system", "galahad_comms", "galahad_office", "galahad_media", "galahad_intel",
            "galahad_finance", "galahad_browser", "galahad_core"} <= names
    out = reg.run("galahad_productivity", {"plugin": "productivity", "command": "add_task", "arguments": '{"title": "from voice"}'})
    assert "status=done" in out and "from voice" in out
    assert any(t["title"] == "from voice" for t in call(full_engine, "productivity", "list_tasks").data["tasks"])
    gm = reg.run("galahad_comms", {"plugin": "gmail", "command": "send", "arguments": '{"to":"a@b.co","subject":"s","body":"b"}'})
    assert "status=ask" in gm and "NOTHING was done" in gm                                      # voice can request, never confirm
    assert "status=failed" in reg.run("galahad_core", {"plugin": "bogus", "command": "x"})
    assert "status=failed" in reg.run("galahad_core", {"plugin": "assistant", "command": "recall", "arguments": "not json"})


# ───────────────────────────── persistence ─────────────────────────────
def test_everything_survives_an_application_restart(home):
    from galahad.engine import Engine
    e = Engine().start()
    e.permissions.set("finance", "finance.data", True)
    call(e, "productivity", "add_task", title="persisted task")
    call(e, "productivity", "add_reminder", text="persisted reminder", when="tomorrow 9am")
    call(e, "finance", "add_entry", kind="expense", amount=42, category="tea")
    call(e, "assistant", "remember", key="colour", value="teal")
    e.settings.set("appearance.accent", "#ff00aa")
    e.registry.set_config("browser", {"debug_port": 9333})
    e.secrets.set("ai", "openai", "sk-persist-test-0123456789")
    e.workflows.save("persisted-wf", [{"plugin": "utilities", "command": "calculate", "args": {"expression": "1+1"}}])
    e.notifications.add("persisted note", "", "system", 2)
    e.registry.disable("word")
    e.memory.add_message("user", "persisted chat")
    e.stop()

    e2 = Engine().start()
    try:
        assert "persisted task" in call(e2, "productivity", "list_tasks").message
        assert "persisted reminder" in call(e2, "productivity", "list_reminders").message
        assert call(e2, "finance", "summary").data["expenses"] == 42
        assert "teal" in call(e2, "assistant", "recall", query="colour").message
        assert e2.settings.get("appearance.accent") == "#ff00aa"
        assert e2.registry.get_config("browser")["debug_port"] == 9333
        assert e2.secrets.get("ai", "openai") == "sk-persist-test-0123456789"
        assert [w["name"] for w in e2.workflows.list()] == ["persisted-wf"]
        assert e2.notifications.list()[0]["title"] == "persisted note"
        assert e2.registry.entries["word"].state == "disabled"                                 # plugin state persists
        assert e2.permissions.is_granted("finance", "finance.data")                            # so do permissions
        assert any(m["content"] == "persisted chat" for m in e2.memory.history())
        assert e2.db.schema_version() >= 1
    finally:
        e2.stop()


def test_two_engines_share_one_database_safely(home):
    """The dashboard thread, scheduler and tool threads all write at once."""
    import threading
    from galahad.engine import Engine
    e = Engine().start()
    errs = []
    def worker(i):
        try:
            for j in range(15):
                r = call(e, "productivity", "add_task", title=f"t{i}-{j}")
                assert r.ok, r.message
                e.notifications.add(f"n{i}-{j}", "", "system", 1)
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    e.stop()
    assert not errs
    e2 = Engine()
    assert e2.db.one("SELECT COUNT(*) n FROM tasks")["n"] == 90


def test_voice_settings_are_views_of_mark_lvi_config(home, tmp_path, monkeypatch):
    from memory import config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "cfg"); monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "cfg" / "api_keys.json")
    from galahad.engine import Engine
    e = Engine(load_plugins=False)
    calls = []
    e.settings.player = types.SimpleNamespace(on_voice_change=lambda: calls.append("voice"), on_wake_toggle=lambda on: calls.append(("wake", on)),
                                              on_push_to_talk=lambda on: calls.append(("ptt", on)))
    e.settings.set("voice.listening_mode", "push_to_talk")
    assert cm.get_push_to_talk_enabled() and not cm.get_wake_word_enabled()                       # written to MARK's own config
    assert e.settings.get("voice.listening_mode") == "push_to_talk" and ("ptt", True) in calls    # and the live session was told
    e.settings.set("voice.listening_mode", "wake_word")
    assert cm.get_wake_word_enabled() and e.settings.get("voice.listening_mode") == "wake_word"
    e.settings.set("voice.voice", cm.AVAILABLE_VOICES[1])
    assert cm.get_voice() == cm.AVAILABLE_VOICES[1] and "voice" in calls
    with pytest.raises(ValueError):
        e.settings.set("voice.listening_mode", "telepathy")


# ───────────────────────────── background operation ────────────────────
def test_scheduler_thread_fires_reminders_without_any_ui(home):
    """No dashboard, no window: the engine alone must deliver a due reminder."""
    from galahad.engine import Engine
    e = Engine().start()
    try:
        r = call(e, "productivity", "add_reminder", text="background reminder", when="in 5 minutes")
        e.db.execute("UPDATE reminders SET due=? WHERE id=?", (time.time() - 1, r.data["id"]))
        got = []
        e.bus.subscribe("notify.deliver", lambda t, p: got.append(p["title"]))
        job = e.scheduler.jobs["plugin:productivity:due-reminders"]
        job.next_due = 0                                                    # the 15 s timer, brought forward
        end = time.time() + 6
        while not got and time.time() < end:
            time.sleep(0.1)
        assert got and "background reminder" in got[0]
    finally:
        e.stop()


def test_plugins_can_be_removed_from_background_service(home):
    from galahad.engine import Engine
    e = Engine().start()
    try:
        assert "plugin:sysmon:sample" in e.scheduler.jobs
        e.settings.set("startup.background_plugins", ["productivity"])         # only these services run in the background
        e._wire_ticks()
        assert "plugin:sysmon:sample" not in e.scheduler.jobs and "plugin:productivity:due-reminders" in e.scheduler.jobs
        e.registry.disable("productivity"); e._wire_ticks()
        assert "plugin:productivity:due-reminders" not in e.scheduler.jobs     # disabling a plugin stops its service
    finally:
        e.stop()


def test_graceful_shutdown_stops_threads_and_marks_nothing_running(home):
    from galahad.engine import Engine
    before = {t.name for t in __import__("threading").enumerate()}
    e = Engine().start()
    call(e, "productivity", "add_task", title="x")
    e.stop()
    time.sleep(0.3)
    left = {t.name for t in __import__("threading").enumerate()} - before
    assert not [n for n in left if n.startswith(("gal-scheduler", "gal-job"))], left
    assert not e.activity.active()


def test_idle_resource_use_is_small(home):
    from galahad.engine import Engine
    proc = psutil.Process()
    e = Engine().start()
    try:
        time.sleep(2.0)                                       # let the first 'run_soon' ticks finish
        rss0 = proc.memory_info().rss
        proc.cpu_percent(None)
        t0 = time.time(); time.sleep(6.0)
        cpu = proc.cpu_percent(None)
        assert cpu < 5.0, f"idle engine used {cpu}% CPU over {time.time() - t0:.0f}s"
        assert proc.memory_info().rss - rss0 < 40 * 1024 * 1024
        assert proc.num_threads() < 60
    finally:
        e.stop()


# ───────────────────────────── security ────────────────────────────────
def test_secrets_never_reach_logs_activity_or_the_database_in_clear(full_engine, home):
    e = full_engine
    secret = "sk-TOPSECRETKEY-1234567890abcdef"
    r = call(e, "ai_providers", "configure", provider="openai", api_key=secret, model="gpt-4o-mini")
    assert r.ok
    import logging
    logging.getLogger("galahad.test").warning("leaky line api_key=%s and %s", secret, secret)
    for h in logging.getLogger("galahad").handlers:
        h.flush()
    log_text = "\n".join(p.read_text(errors="ignore") for p in (home / "logs").glob("*.log*"))
    assert secret not in log_text
    rows = json.dumps(e.activity.recent(50), default=str)
    assert secret not in rows and "***" in rows
    assert secret.encode() not in Path(e.db.path).read_bytes()
    assert secret.encode() not in b"".join(p.read_bytes() for p in Path(e.db.path).parent.glob("galahad.db-*"))
    assert e.ai.api_key("openai") == secret
    assert secret not in json.dumps(e.registry.list(), default=str) and secret not in json.dumps(e.ai.status())


def test_plugins_cannot_exceed_their_declared_permissions(full_engine):
    from galahad.plugin_api import PluginError
    ctx = full_engine.registry.entries["utilities"].ctx                      # declares only 'network'
    for fn in (lambda: ctx.llm("x"), lambda: ctx.remember("k", "v"), lambda: ctx.notify("t"), lambda: ctx.call("files", "search", {"query": "x"})):
        with pytest.raises(PluginError):
            fn()
    full_engine.permissions.set("utilities", "network", False)
    with pytest.raises(PluginError):
        ctx.http_get("https://example.com")


def test_user_plugins_load_only_after_explicit_trust(home):
    from galahad import paths
    from galahad.engine import Engine
    code = ('from galahad.plugin_api import Plugin, Manifest, command\n'
            'class Mine(Plugin):\n'
            '    manifest = Manifest(id="mine", name="Mine", description="d", category="Utilities", permissions=[])\n'
            '    @command(description="hi")\n'
            '    def hi(self): return "hello from mine"\n'
            'PLUGINS = [Mine]\n')
    f = paths.user_plugins_dir() / "mine.py"
    f.write_text(code)
    e = Engine().start()
    try:
        ent = e.registry.entries["user:mine"]
        assert ent.state == "discovered" and "Not trusted" in ent.error and "mine" not in [k for k, v in e.registry.entries.items() if v.manifest]
        e.registry.trust_user_plugin("mine.py"); e.registry.load_all()
        assert call(e, "mine", "hi").message == "hello from mine"
        f.write_text(code + "# tampered\n")                                     # a changed file is no longer trusted
        e2 = Engine().start()
        assert e2.registry.entries["user:mine"].state == "discovered"
        e2.stop()
    finally:
        e.stop()


def test_autostart_writes_and_removes_the_run_key(monkeypatch):
    """The Windows registry code path, exercised against a fake winreg."""
    from galahad import autostart
    store = {}
    class Key:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *a, **k: Key(), SetValueEx=lambda k, n, r, t, v: store.__setitem__(n, v),
        QueryValueEx=lambda k, n: (store[n], 1) if n in store else (_ for _ in ()).throw(OSError()),
        DeleteValue=lambda k, n: store.pop(n))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    assert "will start with Windows" in autostart.set_enabled(True)
    assert autostart.is_enabled() and "main.py" in store["GALAHAD"] and "--minimized" in store["GALAHAD"]
    assert "no longer" in autostart.set_enabled(False) and not autostart.is_enabled()
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    assert "only applies on Windows" in autostart.set_enabled(True)


def test_cli_entry_points(home, capsys):
    from galahad.__main__ import main
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "productivity" in out and "spotify" in out
    assert main(["run", "calculate", "7*6"]) == 0
    assert "42" in capsys.readouterr().out
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["plugins"]["total"] == 22
