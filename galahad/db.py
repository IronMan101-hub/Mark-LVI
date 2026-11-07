"""SQLite access with versioned migrations.

One connection per call (SQLite is cheap to open) with WAL, so the scheduler,
the web server and tool threads never share a connection object.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths

# Each entry is applied exactly once, in order. NEVER edit a shipped migration;
# append a new one.
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE plugin_state (
        plugin_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
        config TEXT NOT NULL DEFAULT '{}', installed INTEGER NOT NULL DEFAULT 1, updated REAL);
    CREATE TABLE permissions (
        plugin_id TEXT NOT NULL, permission TEXT NOT NULL, granted INTEGER NOT NULL,
        updated REAL NOT NULL, PRIMARY KEY (plugin_id, permission));
    CREATE TABLE kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL,
        PRIMARY KEY (ns, key));
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, plugin TEXT, origin TEXT,
        args TEXT, status TEXT NOT NULL, result TEXT, error TEXT, confirmation TEXT DEFAULT 'not_required',
        run_id INTEGER, started REAL NOT NULL, finished REAL);
    CREATE INDEX idx_activity_started ON activity(started);
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT, category TEXT NOT NULL DEFAULT 'system',
        priority INTEGER NOT NULL DEFAULT 1, source TEXT, dedupe_key TEXT, created REAL NOT NULL,
        read INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX idx_notif_created ON notifications(created);
    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL, created REAL NOT NULL);
    CREATE INDEX idx_conv_session ON conversations(session, id);
    CREATE TABLE memory_long (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'notes', created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT DEFAULT '',
        due REAL, priority INTEGER NOT NULL DEFAULT 2, category TEXT DEFAULT 'general',
        status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, completed REAL);
    CREATE TABLE reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL,
        recurrence TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active', announce INTEGER NOT NULL DEFAULT 1,
        created REAL NOT NULL, last_fired REAL, task_id INTEGER);
    CREATE INDEX idx_rem_due ON reminders(status, due);
    CREATE TABLE events_local (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, start REAL NOT NULL, end REAL,
        location TEXT DEFAULT '', notes TEXT DEFAULT '', remind_minutes INTEGER DEFAULT 15,
        reminded INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
    CREATE TABLE workflows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT '',
        trigger TEXT NOT NULL DEFAULT '{"type":"manual"}', steps TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        created REAL NOT NULL, updated REAL NOT NULL, last_run REAL, next_run REAL);
    CREATE TABLE workflow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER, name TEXT, request TEXT, plan TEXT,
        status TEXT NOT NULL, progress TEXT DEFAULT '[]', error TEXT, started REAL NOT NULL, finished REAL);
    CREATE TABLE articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, title TEXT, publisher TEXT,
        category TEXT, published REAL, summary TEXT, fetched REAL NOT NULL,
        saved INTEGER NOT NULL DEFAULT 0, bookmarked INTEGER NOT NULL DEFAULT 0, read_at REAL);
    CREATE INDEX idx_art_cat ON articles(category, published);
    CREATE TABLE research (
        id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, fmt TEXT, report TEXT,
        sources TEXT, created REAL NOT NULL);
    CREATE TABLE finance_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, amount REAL NOT NULL, category TEXT DEFAULT '',
        note TEXT DEFAULT '', day TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', created REAL NOT NULL);
    CREATE INDEX idx_fin_day ON finance_entries(day);
    CREATE TABLE finance_budgets (category TEXT PRIMARY KEY, monthly_limit REAL NOT NULL);
    CREATE TABLE finance_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, target REAL NOT NULL,
        saved REAL NOT NULL DEFAULT 0, deadline TEXT, created REAL NOT NULL);
    CREATE TABLE finance_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, amount REAL NOT NULL,
        rate REAL, start TEXT, maturity TEXT, monthly REAL, note TEXT DEFAULT '', updated REAL NOT NULL);
    CREATE TABLE ai_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, model TEXT, purpose TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, ok INTEGER, created REAL NOT NULL);
    """),
]


class Database:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else paths.db_path()
        self._lock = threading.Lock()
        self.migrate()

    # -- connections ---------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextmanager
    def tx(self):
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write; returns lastrowid."""
        con = self._connect()
        try:
            cur = con.execute(sql, tuple(params))
            return cur.lastrowid or 0
        finally:
            con.close()

    def rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        con = self._connect()
        try:
            return con.execute(sql, tuple(params)).rowcount
        finally:
            con.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        con = self._connect()
        try:
            return [dict(r) for r in con.execute(sql, tuple(params)).fetchall()]
        finally:
            con.close()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- migrations ----------------------------------------------------
    def schema_version(self) -> int:
        con = self._connect()
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()

    def migrate(self) -> int:
        with self._lock:
            con = self._connect()
            try:
                current = con.execute("PRAGMA user_version").fetchone()[0]
                for version, sql in MIGRATIONS:
                    if version <= current:
                        continue
                    con.execute("BEGIN IMMEDIATE")
                    try:
                        for stmt in _split(sql):
                            con.execute(stmt)
                        con.execute(f"PRAGMA user_version={int(version)}")
                        con.execute("COMMIT")
                    except Exception:
                        con.execute("ROLLBACK")
                        raise
                    current = version
                return current
            finally:
                con.close()

    # -- plugin-scoped key/value store --------------------------------
    def kv_get(self, ns: str, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_set(self, ns: str, key: str, value) -> None:
        self.execute(
            "INSERT INTO kv(ns,key,value,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(ns,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (ns, key, json.dumps(value), time.time()))

    def kv_delete(self, ns: str, key: str) -> None:
        self.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))

    def kv_all(self, ns: str) -> dict:
        out = {}
        for r in self.query("SELECT key,value FROM kv WHERE ns=?", (ns,)):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
