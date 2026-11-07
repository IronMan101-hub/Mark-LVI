"""Central notification engine: store, de-duplicate, filter, deliver."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from .db import Database
from .events import EventBus
from .settings import Settings

CATEGORIES = ("reminder", "task", "workflow", "email", "system", "plugin", "assistant", "briefing")
# priority: 0 low (stored only) · 1 normal · 2 high · 3 urgent (bypasses quiet hours)


def in_quiet_hours(spec: str, now: Optional[datetime] = None) -> bool:
    try:
        a, b = spec.split("-")
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        s = int(a[:2]) * 60 + int(a[3:5]); e = int(b[:2]) * 60 + int(b[3:5])
        return (s <= cur < e) if s <= e else (cur >= s or cur < e)
    except Exception:
        return False


class Notifications:
    def __init__(self, db: Database, bus: EventBus, settings: Settings):
        self.db, self.bus, self.settings = db, bus, settings

    def add(self, title: str, body: str = "", category: str = "system", priority: int = 1,
            source: str = "", dedupe_key: str = "", dedupe_window: float = 300.0) -> Optional[int]:
        """Returns the id, or None if suppressed as a duplicate / disabled."""
        if category not in CATEGORIES:
            category = "system"
        if not self.settings.get("startup.notifications", True):
            return None
        if dedupe_key:
            dup = self.db.one("SELECT id FROM notifications WHERE dedupe_key=? AND created>?",
                              (dedupe_key, time.time() - dedupe_window))
            if dup:
                return None
        nid = self.db.execute(
            "INSERT INTO notifications(title,body,category,priority,source,dedupe_key,created) VALUES(?,?,?,?,?,?,?)",
            (title[:200], body[:2000], category, int(priority), source, dedupe_key, time.time()))
        deliver = priority >= int(self.settings.get("notifications.min_priority", 0))
        quiet = self.settings.get("notifications.quiet_hours", "")
        if deliver and quiet and priority < 3 and in_quiet_hours(quiet):
            deliver = False
        if deliver:
            self.db.execute("UPDATE notifications SET delivered=1 WHERE id=?", (nid,))
            self.bus.publish("notify.deliver", {"id": nid, "title": title, "body": body, "category": category,
                                                "priority": priority,
                                                "speak": bool(self.settings.get("notifications.voice", True))
                                                and category in ("reminder", "task", "system") and priority >= 2})
        self.bus.publish("notify.added", {"id": nid})
        return nid

    def list(self, category: str = "", unread_only: bool = False, include_dismissed: bool = False,
             limit: int = 100) -> list[dict]:
        sql, p = "SELECT * FROM notifications WHERE 1=1", []
        if category:
            sql += " AND category=?"; p.append(category)
        if unread_only:
            sql += " AND read=0"
        if not include_dismissed:
            sql += " AND dismissed=0"
        sql += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        return self.db.query(sql, p)

    def unread_count(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM notifications WHERE read=0 AND dismissed=0")["n"]

    def mark_read(self, nid: Optional[int] = None) -> int:
        if nid is None:
            return self.db.rowcount("UPDATE notifications SET read=1 WHERE read=0")
        return self.db.rowcount("UPDATE notifications SET read=1 WHERE id=?", (nid,))

    def dismiss(self, nid: Optional[int] = None) -> int:
        if nid is None:
            return self.db.rowcount("UPDATE notifications SET dismissed=1, read=1")
        return self.db.rowcount("UPDATE notifications SET dismissed=1, read=1 WHERE id=?", (nid,))
