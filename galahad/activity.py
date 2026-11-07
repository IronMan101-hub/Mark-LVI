"""Structured activity records — what GALAHAD did, to what, and how it ended."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .db import Database
from .events import EventBus

STATES = ("pending", "running", "awaiting_confirmation", "completed", "failed", "cancelled")
ACTIVE = ("pending", "running", "awaiting_confirmation")


def _short(value: Any, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class ActivityLog:
    def __init__(self, db: Database, bus: EventBus):
        self.db, self.bus = db, bus

    def start(self, action: str, plugin: str = "", origin: str = "user", args: Any = None,
              status: str = "running", run_id: Optional[int] = None) -> int:
        aid = self.db.execute(
            "INSERT INTO activity(action,plugin,origin,args,status,run_id,started) VALUES(?,?,?,?,?,?,?)",
            (action, plugin, origin, _redact(args), status, run_id, time.time()))
        self.bus.publish("activity.started", {"id": aid, "action": action, "plugin": plugin, "status": status})
        return aid

    def update(self, aid: int, status: str, result: Any = None, error: str = "",
               confirmation: Optional[str] = None) -> None:
        assert status in STATES, status
        finished = time.time() if status not in ACTIVE else None
        sets, vals = ["status=?"], [status]
        if result is not None:
            sets.append("result=?"); vals.append(_short(result))
        if error:
            sets.append("error=?"); vals.append(_short(error, 800))
        if confirmation:
            sets.append("confirmation=?"); vals.append(confirmation)
        if finished:
            sets.append("finished=?"); vals.append(finished)
        vals.append(aid)
        self.db.execute(f"UPDATE activity SET {', '.join(sets)} WHERE id=?", vals)
        self.bus.publish("activity.updated", {"id": aid, "status": status})

    def get(self, aid: int) -> Optional[dict]:
        return self.db.one("SELECT * FROM activity WHERE id=?", (aid,))

    def recent(self, limit: int = 100, plugin: str = "", status: str = "") -> list[dict]:
        sql, p = "SELECT * FROM activity WHERE 1=1", []
        if plugin:
            sql += " AND plugin=?"; p.append(plugin)
        if status:
            sql += " AND status=?"; p.append(status)
        sql += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        return self.db.query(sql, p)

    def active(self) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE))
        return self.db.query(f"SELECT * FROM activity WHERE status IN ({marks}) ORDER BY id DESC", ACTIVE)

    def recover_interrupted(self) -> int:
        """After a crash/restart nothing is really still running."""
        marks = ",".join("?" * len(ACTIVE))
        return self.db.rowcount(
            f"UPDATE activity SET status='cancelled', error='interrupted by restart', finished=? "
            f"WHERE status IN ({marks})", (time.time(), *ACTIVE))

    def prune(self, days: int) -> int:
        return self.db.rowcount("DELETE FROM activity WHERE started < ?", (time.time() - days * 86400,))


_SENSITIVE_KEYS = ("password", "token", "secret", "api_key", "apikey", "key")


def _redact(args: Any) -> str:
    def walk(v):
        if isinstance(v, dict):
            return {k: ("***" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return _short(walk(args) if args is not None else {})
