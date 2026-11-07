"""Three kinds of memory, all inspectable, editable, exportable and erasable.

short-term   the current conversation + working context (what is being done now)
long-term    preferences and facts the USER approved (explicit, keyed, editable)
operational  what GALAHAD recently did — derived from the activity log and
             workflow runs, so it can't drift from what really happened
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from .db import Database
from .settings import Settings


class Memory:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.session = uuid.uuid4().hex[:12]
        self.context: dict = {}      # short-term working context (RAM only)

    # ── short-term ────────────────────────────────────────────────────
    def add_message(self, role: str, content: str, session: Optional[str] = None) -> int:
        if not self.settings.get("memory.enabled", True) and role != "system":
            return 0
        return self.db.execute("INSERT INTO conversations(session,role,content,created) VALUES(?,?,?,?)",
                               (session or self.session, role, content, time.time()))

    def recent_messages(self, limit: Optional[int] = None, session: Optional[str] = None) -> list[dict]:
        limit = limit or int(self.settings.get("ai.context_messages", 12))
        rows = self.db.query("SELECT role,content,created FROM conversations WHERE session=? ORDER BY id DESC LIMIT ?",
                             (session or self.session, limit))
        return list(reversed(rows))

    def history(self, limit: int = 200, query: str = "") -> list[dict]:
        if query:
            return self.db.query("SELECT * FROM conversations WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                                 (f"%{query}%", limit))
        return self.db.query("SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,))

    def set_context(self, key: str, value) -> None:
        self.context[key] = value

    def clear_short_term(self) -> None:
        self.context.clear()
        self.db.execute("DELETE FROM conversations WHERE session=?", (self.session,))

    # ── long-term ─────────────────────────────────────────────────────
    def remember(self, key: str, value: str, category: str = "notes") -> dict:
        key, value = key.strip().lower()[:80], value.strip()[:1000]
        if not key or not value:
            raise ValueError("both key and value are required")
        now = time.time()
        self.db.execute(
            "INSERT INTO memory_long(key,value,category,created,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, category=excluded.category, updated=excluded.updated",
            (key, value, category, now, now))
        return {"key": key, "value": value, "category": category}

    def recall(self, query: str = "", limit: int = 20) -> list[dict]:
        if not query:
            return self.db.query("SELECT * FROM memory_long ORDER BY updated DESC LIMIT ?", (limit,))
        words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
        rows = self.db.query("SELECT * FROM memory_long")
        scored = []
        for r in rows:
            hay = f"{r['key']} {r['value']} {r['category']}".lower()
            s = sum(w in hay for w in words)
            if s:
                scored.append((s, r))
        return [r for _s, r in sorted(scored, key=lambda x: -x[0])[:limit]]

    def forget(self, key: str) -> bool:
        return self.db.rowcount("DELETE FROM memory_long WHERE key=?", (key.strip().lower(),)) > 0

    def prompt_block(self) -> str:
        rows = self.recall(limit=25)
        return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)

    # ── operational ───────────────────────────────────────────────────
    def operational(self, limit: int = 30) -> list[dict]:
        acts = self.db.query(
            "SELECT id,action,plugin,status,result,started FROM activity ORDER BY id DESC LIMIT ?", (limit,))
        runs = self.db.query(
            "SELECT id,name,request,status,started FROM workflow_runs ORDER BY id DESC LIMIT ?", (10,))
        return [{"type": "action", **a} for a in acts] + [{"type": "workflow_run", **r} for r in runs]

    # ── transparency controls ─────────────────────────────────────────
    def inspect(self) -> dict:
        return {"short_term": {"session": self.session, "messages": self.recent_messages(50),
                               "context": {k: str(v)[:200] for k, v in self.context.items()}},
                "long_term": self.recall(limit=500),
                "operational": self.operational(50)}

    def export(self) -> str:
        return json.dumps({"exported": time.time(), "long_term": self.recall(limit=10000),
                           "conversations": self.history(5000), "operational": self.operational(200)},
                          indent=2, default=str)

    def delete_all(self, which: str = "all") -> dict:
        out = {}
        if which in ("all", "short_term"):
            self.context.clear()
            out["conversations"] = self.db.rowcount("DELETE FROM conversations")
        if which in ("all", "long_term"):
            out["long_term"] = self.db.rowcount("DELETE FROM memory_long")
        if which in ("all", "operational"):
            out["activity"] = self.db.rowcount("DELETE FROM activity")
            out["workflow_runs"] = self.db.rowcount("DELETE FROM workflow_runs")
        return out

    def prune(self) -> int:
        days = int(self.settings.get("memory.history_days", 90))
        return self.db.rowcount("DELETE FROM conversations WHERE created < ?", (time.time() - days * 86400,))
