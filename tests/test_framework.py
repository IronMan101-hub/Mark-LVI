import threading
import time

import pytest

from galahad.plugin_api import Manifest, Plugin, PluginError, command, Health
from tests.conftest import call


class Demo(Plugin):
    manifest = Manifest(id="demo", name="Demo", description="demo plugin", category="Utilities",
                        permissions=["fs.read", "screen.capture", "notify"], requires_python=[])

    def __init__(self):
        super().__init__()
        self.events = []
        self.activated = False

    def activate(self):
        self.activated = True

    def deactivate(self):
        self.activated = False

    @command(description="add numbers", params={"a": {"type": "integer", "required": True},
                                               "b": {"type": "integer", "default": 1, "maximum": 100}}, permissions=("fs.read",))
    def add(self, a, b):
        return {"message": f"sum {a + b}", "data": {"sum": a + b}}

    @command(description="needs sensitive", permissions=("screen.capture",))
    def peek(self):
        return "peeked"

    @command(description="dangerous", confirm="Delete everything named {name}?", params={"name": {"type": "string", "required": True}},
             permissions=("fs.read",))
    def wipe(self, name):
        return f"wiped {name}"

    @command(description="slow", timeout=0.3)
    def slow(self):
        time.sleep(2)
        return "late"

    @command(description="fails")
    def boom(self):
        raise RuntimeError("kaboom")

    @command(description="reports failure")
    def verify_fail(self):
        return {"ok": False, "message": "file did not appear"}

    def on_event(self, topic, payload):
        self.events.append(topic)


@pytest.fixture()
def reg(engine):
    engine.registry._register_class(Demo, "builtin", "x")
    engine.registry.load_all()
    return engine.registry


def test_lifecycle_states(engine, reg):
    ent = reg.entries["demo"]
    assert ent.state == "active" and ent.plugin.activated
    reg.suspend("demo"); assert ent.state == "suspended"
    assert call(engine, "demo", "add", a=1).status == "failed"
    reg.resume("demo"); assert ent.state == "active"
    reg.disable("demo"); assert ent.state == "disabled" and not ent.plugin.activated
    assert "disabled" in call(engine, "demo", "add", a=1).message
    reg.enable("demo"); assert ent.state == "active"
    reg.uninstall("demo"); assert ent.state == "unloaded"
    reg.install("demo"); assert ent.state == "active"


def test_validation_and_defaults(engine, reg):
    r = call(engine, "demo", "add", a="5")
    assert r.ok and r.data["sum"] == 6
    assert not call(engine, "demo", "add").ok                    # missing required
    assert "maximum" not in call(engine, "demo", "add", a=1, b=5).message
    assert "<=" in call(engine, "demo", "add", a=1, b=500).message
    assert "integer" in call(engine, "demo", "add", a="zebra").message


def test_sensitive_permission_denied_until_granted(engine, reg):
    r = call(engine, "demo", "peek")
    assert not r.ok and "Permission" in r.message
    engine.permissions.set("demo", "screen.capture", True)
    assert call(engine, "demo", "peek").ok
    engine.permissions.set("demo", "screen.capture", False)
    assert not call(engine, "demo", "peek").ok


def test_confirmation_cannot_be_bypassed_by_argument(engine, reg):
    r = call(engine, "demo", "wipe", name="x", confirmed="yes")   # forged arg is dropped
    assert r.status == "awaiting_confirmation" and r.pending_id
    assert engine.activity.get(r.activity_id)["status"] == "awaiting_confirmation"
    res = reg.approve(r.pending_id)
    assert res.ok and res.message == "wiped x"
    assert engine.activity.get(r.activity_id)["confirmation"] == "approved"
    assert not reg.approve(r.pending_id).ok                      # cannot approve twice


def test_deny_cancels(engine, reg):
    r = call(engine, "demo", "wipe", name="y")
    d = reg.deny(r.pending_id)
    assert d.status == "cancelled"
    assert engine.activity.get(r.activity_id)["status"] == "cancelled"


def test_timeout_and_crash_are_isolated(engine, reg):
    t = call(engine, "demo", "slow")
    assert not t.ok and "Timed out" in t.message
    b = call(engine, "demo", "boom")
    assert not b.ok and "kaboom" in b.message
    assert call(engine, "demo", "add", a=1).ok                  # still healthy afterwards


def test_failure_reported_by_plugin_is_not_success(engine, reg):
    r = call(engine, "demo", "verify_fail")
    assert not r.ok and r.status == "failed"
    assert engine.activity.recent(1)[0]["status"] == "failed"


def test_health_degrades_after_repeated_failures(engine, reg):
    for _ in range(5):
        call(engine, "demo", "boom")
    assert reg.health("demo").status == "degraded"
    assert any("trouble" in n["title"] for n in engine.notifications.list())
    call(engine, "demo", "add", a=1)
    assert reg.health("demo").status == "ok"


def test_events_reach_subscribers_and_errors_are_isolated(engine, reg):
    reg.entries["demo"].manifest.subscriptions.append("thing.*")
    reg._subscribe(reg.entries["demo"])
    engine.bus.subscribe("thing.*", lambda t, p: 1 / 0, owner="bad")
    engine.bus.publish("thing.happened", {})
    assert "thing.happened" in reg.entries["demo"].plugin.events


def test_duplicate_ids_rejected_and_bad_manifest_flagged(engine):
    class Bad(Plugin):
        manifest = Manifest(id="Bad Id!", name="x", description="y", category="Nope")
    engine.registry._register_class(Bad, "builtin", "x")
    ent = engine.registry.entries["Bad Id!"]
    assert ent.state == "error" and "category" in ent.error
    engine.registry._register_class(Demo, "builtin", "x")
    engine.registry._register_class(Demo, "builtin", "x")
    assert sum(1 for k in engine.registry.entries if k == "demo") == 1


def test_missing_dependency_blocks_activation(engine):
    class NeedsPkg(Plugin):
        manifest = Manifest(id="needs_pkg", name="N", description="d", category="Utilities",
                            requires_python=["definitely_not_installed_pkg"])
    engine.registry._register_class(NeedsPkg, "builtin", "x")
    engine.registry.load_all()
    ent = engine.registry.entries["needs_pkg"]
    assert ent.state == "error" and "pip install definitely_not_installed_pkg" in ent.error


def test_activity_records_and_restart_recovery(engine, reg):
    r = call(engine, "demo", "wipe", name="z")
    assert engine.activity.active()
    assert engine.activity.recover_interrupted() >= 1
    assert engine.activity.get(r.activity_id)["status"] == "cancelled"


def test_settings_persist_and_validate(home):
    from galahad.engine import Engine
    e = Engine(load_plugins=False)
    e.settings.set("appearance.theme", "light")
    e.settings.set("voice.speed", "1.25")
    with pytest.raises(ValueError):
        e.settings.set("appearance.theme", "neon")
    with pytest.raises(KeyError):
        e.settings.set("nope.nothing", 1)
    e2 = Engine(load_plugins=False)
    assert e2.settings.get("appearance.theme") == "light" and e2.settings.get("voice.speed") == 1.25


def test_secrets_roundtrip_and_never_in_activity(engine, reg):
    engine.secrets.set("demo", "token", "sk-supersecretvalue1234567890")
    assert engine.secrets.get("demo", "token") == "sk-supersecretvalue1234567890"
    raw = engine.db.kv_all("secrets")
    assert all("supersecret" not in v for v in raw.values())
    from galahad.logs import scrub
    assert "supersecret" not in scrub("key sk-supersecretvalue1234567890 and api_key=abcdefghijklmn")


def test_notifications_dedupe_filter_and_quiet_hours(engine):
    n = engine.notifications
    a = n.add("Hi", "b", "reminder", 2, dedupe_key="k")
    assert a and n.add("Hi", "b", "reminder", 2, dedupe_key="k") is None
    engine.settings.set("notifications.min_priority", 2)
    delivered = []
    engine.bus.subscribe("notify.deliver", lambda t, p: delivered.append(p))
    n.add("low", "", "system", 1)
    n.add("high", "", "system", 3)
    assert [d["title"] for d in delivered] == ["high"]
    assert n.unread_count() == 3
    n.mark_read(a); assert n.unread_count() == 2
    n.dismiss(); assert n.list() == []
    from galahad.notifications import in_quiet_hours
    from datetime import datetime
    assert in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 23, 30)) and not in_quiet_hours("22:00-07:00", datetime(2026, 1, 1, 12, 0))


def test_memory_three_kinds(engine):
    m = engine.memory
    m.add_message("user", "hello"); m.remember("favourite colour", "teal")
    assert m.recent_messages()[0]["content"] == "hello"
    assert m.recall("colour")[0]["value"] == "teal"
    engine.activity.start("x.y", "x", "user", {})
    snap = m.inspect()
    assert snap["long_term"] and snap["operational"]
    assert "teal" in m.export()
    assert m.forget("favourite colour") and not m.recall("colour")
    m.delete_all("short_term"); assert not m.recent_messages()


def test_db_migrations_idempotent(home):
    from galahad.db import Database
    d = Database(); v = d.schema_version(); assert v >= 1
    assert Database().schema_version() == v


def test_scheduler_runs_jobs_without_overlap(engine):
    n = []
    engine.scheduler.add("t", "j", 0.0, lambda: (n.append(1), time.sleep(0.2)))
    engine.scheduler.jobs["t:j"].next_due = 0
    assert engine.scheduler.run_due() == 1
    engine.scheduler.jobs["t:j"].next_due = 0
    assert engine.scheduler.run_due() == 0          # still running
    time.sleep(0.35)
    assert len(n) == 1
