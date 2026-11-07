"""Permission model.

Every plugin declares the permissions it needs. A permission is either
*normal* (granted automatically the first time a plugin is registered, because
the user installed it) or *sensitive* (denied until the user grants it in the
dashboard). The user can revoke any permission at any time; the registry
checks on every command, not just at load.
"""
from __future__ import annotations

import time

from .db import Database
from .events import EventBus

# name -> (description, sensitive)
CATALOG: dict[str, tuple[str, bool]] = {
    "fs.read": ("Read files and folders you point it at", False),
    "fs.write": ("Create, move, copy and rename files", False),
    "fs.delete": ("Delete files (always asks first, always recoverable)", False),
    "fs.scan": ("Scan your Documents/Desktop/Downloads folders for search", False),
    "network": ("Make internet requests", False),
    "system.control": ("Open/close apps, windows, volume, clipboard, keystrokes", False),
    "system.power": ("Lock, sleep, restart, shut down (always asks first)", False),
    "system.monitor": ("Read CPU, memory, disk, battery and process information", False),
    "browser.control": ("Open pages and control your browser", False),
    "media.control": ("Control Spotify / YouTube playback", False),
    "messages.send": ("Draft and (after confirmation) send messages and email", False),
    "mail.read": ("Read your email", True),
    "screen.capture": ("Capture and interpret your screen", True),
    "office.documents": ("Create and edit Word, Excel and PowerPoint files", False),
    "ai.use": ("Send prompts to your configured AI provider", False),
    "memory.read": ("Read assistant memory", False),
    "memory.write": ("Write assistant memory", False),
    "plugins.call": ("Call commands of other plugins", False),
    "notify": ("Show notifications", False),
    "schedule": ("Create reminders, tasks and scheduled work", False),
    "finance.data": ("Read and write your financial records", True),
}


class PermissionManager:
    def __init__(self, db: Database, bus: EventBus):
        self.db, self.bus = db, bus

    @staticmethod
    def is_sensitive(perm: str) -> bool:
        return CATALOG.get(perm, ("", True))[1]

    def register_plugin(self, plugin_id: str, required: list[str]) -> None:
        """Seed grants for permissions never seen before; never override a decision."""
        known = {r["permission"] for r in
                 self.db.query("SELECT permission FROM permissions WHERE plugin_id=?", (plugin_id,))}
        for perm in required:
            if perm in known:
                continue
            self.db.execute(
                "INSERT INTO permissions(plugin_id,permission,granted,updated) VALUES(?,?,?,?)",
                (plugin_id, perm, 0 if self.is_sensitive(perm) else 1, time.time()))

    def is_granted(self, plugin_id: str, perm: str) -> bool:
        row = self.db.one("SELECT granted FROM permissions WHERE plugin_id=? AND permission=?", (plugin_id, perm))
        return bool(row and row["granted"])

    def set(self, plugin_id: str, perm: str, granted: bool) -> None:
        if perm not in CATALOG:
            raise KeyError(f"unknown permission '{perm}'")
        self.db.execute(
            "INSERT INTO permissions(plugin_id,permission,granted,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(plugin_id,permission) DO UPDATE SET granted=excluded.granted, updated=excluded.updated",
            (plugin_id, perm, int(granted), time.time()))
        self.bus.publish("permissions.changed", {"plugin": plugin_id, "permission": perm, "granted": granted})

    def for_plugin(self, plugin_id: str) -> list[dict]:
        rows = self.db.query("SELECT permission, granted FROM permissions WHERE plugin_id=? ORDER BY permission", (plugin_id,))
        return [{"permission": r["permission"], "granted": bool(r["granted"]),
                 "sensitive": self.is_sensitive(r["permission"]),
                 "description": CATALOG.get(r["permission"], ("", True))[0]} for r in rows]

    def missing(self, plugin_id: str, needed: list[str]) -> list[str]:
        return [p for p in needed if not self.is_granted(plugin_id, p)]
