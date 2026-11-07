"""The Engine wires every service together and owns the application lifecycle."""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import VERSION, logs, paths
from .activity import ActivityLog
from .ai import AIManager
from .confirmations import ConfirmationBroker
from .db import Database
from .events import EventBus
from .http import Http
from .memory import Memory
from .notifications import Notifications
from .permissions import PermissionManager
from .registry import Registry
from .scheduler import Scheduler
from .secrets import SecretStore
from .settings import Settings

log = logs.get("engine")
_engine: Optional["Engine"] = None
_engine_lock = threading.Lock()


class Engine:
    def __init__(self, db_path=None, load_plugins: bool = True):
        self.started = time.time()
        self.running = False
        self.player = None                      # Qt JarvisUI when embedded in the desktop app
        self.db = Database(db_path)
        self.bus = EventBus()
        self.settings = Settings(self.db, self.bus)
        logs.setup(getattr(__import__("logging"), str(self.settings.get("privacy.log_level", "INFO")).upper(), 20))
        self.secrets = SecretStore(self.db)
        self.permissions = PermissionManager(self.db, self.bus)
        self.http = Http()
        self.ai = AIManager(self.db, self.settings, self.secrets, self.http)
        self.activity = ActivityLog(self.db, self.bus)
        self.notifications = Notifications(self.db, self.bus, self.settings)
        self.confirmations = ConfirmationBroker(self.bus)
        self.memory = Memory(self.db, self.settings)
        self.scheduler = Scheduler()
        self.registry = Registry(self)
        from .orchestrator import Orchestrator
        from .workflows import WorkflowEngine
        self.workflows = WorkflowEngine(self)
        self.orchestrator = Orchestrator(self)
        self._load_plugins = load_plugins
        self._plugin_job_owners: set[str] = set()

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self) -> "Engine":
        if self.running:
            return self
        n = self.activity.recover_interrupted()
        if n:
            log.info("marked %d interrupted activities as cancelled", n)
        self.workflows.recover_interrupted()
        if self._load_plugins:
            self.registry.discover()
            self.registry.load_all()
        self.bus.subscribe("plugin.activated", lambda t, p: self._wire_ticks())
        self.bus.subscribe("*", self.workflows.on_event, owner="workflows")
        self._wire_ticks()
        self.scheduler.add("core", "housekeeping", 6 * 3600, self.housekeeping)
        self.scheduler.add("core", "workflow-triggers", 20, self.workflows.tick, run_soon=True)
        self.scheduler.add("core", "confirm-expiry", 30, self.confirmations.expire)
        self.scheduler.start()
        self.running = True
        self.bus.publish("engine.started", {"version": VERSION})
        log.info("GALAHAD %s started: %d plugins", VERSION, sum(1 for e in self.registry.entries.values() if e.state == "active"))
        return self

    def _wire_ticks(self) -> None:
        for owner in list(self._plugin_job_owners):
            self.scheduler.remove_owner(owner)
        self._plugin_job_owners.clear()
        for pid, name, secs, fn in self.registry.ticks():
            owner = f"plugin:{pid}"
            self._plugin_job_owners.add(owner)
            self.scheduler.add(owner, name, secs, fn, run_soon=True)

    def stop(self) -> None:
        if not self.running:
            return
        self.bus.publish("engine.stopping", {})
        self.scheduler.stop()
        self.registry.shutdown()
        self.running = False
        log.info("GALAHAD stopped")

    def housekeeping(self) -> None:
        self.activity.prune(int(self.settings.get("privacy.activity_retention_days", 60)))
        self.memory.prune()

    def status(self) -> dict:
        ents = self.registry.entries.values()
        return {"version": VERSION, "uptime": int(time.time() - self.started), "running": self.running,
                "plugins": {"total": len(self.registry.entries),
                            "active": sum(1 for e in ents if e.state == "active"),
                            "errors": sum(1 for e in ents if e.state == "error")},
                "ai_available": self.ai.available(), "db_version": self.db.schema_version(),
                "secret_backend": self.secrets.backend_name(), "unread_notifications": self.notifications.unread_count(),
                "pending_confirmations": len(self.confirmations.pending()),
                "schema_version": self.db.schema_version()}


def get_engine(start: bool = True) -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        if start and not _engine.running:
            _engine.start()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        if _engine:
            _engine.stop()
        _engine = None
