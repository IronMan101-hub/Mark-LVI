"""Workflow engine: ordered steps, `{{sN.field}}` data flow, conditions, retries,
cancellation, scheduling and event triggers. Every step goes through the
registry, so permissions, confirmations and the activity log all apply."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from . import logs
from .plugin_api import CommandResult, PluginError

log = logs.get("workflows")
_REF = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)((?:\.[A-Za-z0-9_\[\]]+)*)\s*\}\}")
CONFIRM_WAIT = 600


def _dig(obj: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit():
            i = int(part)
            obj = obj[i] if -len(obj) <= i < len(obj) else None
        else:
            return None
    return obj


def resolve(value: Any, results: dict) -> Any:
    """Substitute {{s1.message}} / {{s1.data.items.0.title}} in strings, recursively."""
    if isinstance(value, str):
        full = _REF.fullmatch(value.strip())
        if full:
            r = results.get(full.group(1), {})
            return _dig(r, full.group(2).lstrip("."))
        return _REF.sub(lambda m: str(_dig(results.get(m.group(1), {}), m.group(2).lstrip(".")) or ""), value)
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, results) for k, v in value.items()}
    return value


def eval_condition(cond: Any, results: dict) -> bool:
    """cond: {"step":"s1","field":"data.count","op":"gt","value":0} · op in eq ne gt lt contains exists ok"""
    if not cond:
        return True
    r = results.get(cond.get("step", ""), {})
    op = cond.get("op", "ok")
    if op == "ok":
        return bool(r.get("ok"))
    if op == "failed":
        return not r.get("ok", False)
    left = _dig(r, cond.get("field", "message"))
    right = cond.get("value")
    try:
        if op == "exists":
            return left not in (None, "", [], {})
        if op == "eq":
            return str(left) == str(right)
        if op == "ne":
            return str(left) != str(right)
        if op == "gt":
            return float(left) > float(right)
        if op == "lt":
            return float(left) < float(right)
        if op == "contains":
            return str(right).lower() in str(left).lower()
    except (TypeError, ValueError):
        return False
    return False


def validate_steps(steps: Any, registry=None) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise PluginError("A workflow needs at least one step.")
    seen, out = set(), []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or not s.get("plugin") or not s.get("command"):
            raise PluginError(f"Step {i} needs 'plugin' and 'command'.")
        sid = str(s.get("id") or f"s{i}")
        if sid in seen:
            raise PluginError(f"Duplicate step id '{sid}'.")
        seen.add(sid)
        if registry is not None:
            ent = registry.entries.get(s["plugin"])
            if not ent or not ent.manifest:
                raise PluginError(f"Step {sid}: unknown plugin '{s['plugin']}'.")
            if s["command"] not in ent.commands:
                raise PluginError(f"Step {sid}: {s['plugin']} has no command '{s['command']}'.")
        out.append({"id": sid, "plugin": s["plugin"], "command": s["command"], "args": s.get("args") or {},
                    "condition": s.get("condition"), "retries": max(0, min(5, int(s.get("retries", 0)))),
                    "on_error": s.get("on_error", "stop") if s.get("on_error") in ("stop", "continue") else "stop",
                    "label": s.get("label", "")})
    return out


class WorkflowEngine:
    def __init__(self, engine):
        self.e = engine
        self._cancels: dict[int, threading.Event] = {}
        self._threads: dict[int, threading.Thread] = {}

    # ── CRUD ──────────────────────────────────────────────────────────
    def save(self, name: str, steps: list, description: str = "", trigger: Optional[dict] = None,
             enabled: bool = True, wid: Optional[int] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise PluginError("A workflow needs a name.")
        steps = validate_steps(steps, self.e.registry)
        trigger = self._validate_trigger(trigger or {"type": "manual"})
        now = time.time()
        nxt = self._next_run(trigger, datetime.now()) if enabled else None
        if wid:
            self.e.db.execute("UPDATE workflows SET name=?,description=?,trigger=?,steps=?,enabled=?,updated=?,next_run=? WHERE id=?",
                              (name, description, json.dumps(trigger), json.dumps(steps), int(enabled), now, nxt, wid))
        else:
            wid = self.e.db.execute(
                "INSERT INTO workflows(name,description,trigger,steps,enabled,created,updated,next_run) VALUES(?,?,?,?,?,?,?,?)",
                (name, description, json.dumps(trigger), json.dumps(steps), int(enabled), now, now, nxt))
        return self.get(wid)

    @staticmethod
    def _validate_trigger(t: dict) -> dict:
        kind = t.get("type", "manual")
        if kind == "manual":
            return {"type": "manual"}
        if kind == "schedule":
            if not re.fullmatch(r"\d{2}:\d{2}", str(t.get("at", ""))):
                raise PluginError("A scheduled trigger needs 'at' as HH:MM.")
            h, m = map(int, t["at"].split(":"))
            if h > 23 or m > 59:
                raise PluginError("'at' must be a valid time.")
            days = t.get("days") or []
            if any(d not in range(7) for d in days):
                raise PluginError("'days' are numbers 0 (Mon) to 6 (Sun).")
            return {"type": "schedule", "at": t["at"], "days": days}
        if kind == "interval":
            mins = int(t.get("minutes", 0))
            if mins < 1:
                raise PluginError("An interval trigger needs minutes >= 1.")
            return {"type": "interval", "minutes": mins}
        if kind == "event":
            if not t.get("topic"):
                raise PluginError("An event trigger needs a 'topic' pattern.")
            return {"type": "event", "topic": str(t["topic"])}
        raise PluginError(f"Unknown trigger type '{kind}'.")

    def _row(self, r: dict) -> dict:
        r = dict(r)
        r["trigger"] = json.loads(r["trigger"])
        r["steps"] = json.loads(r["steps"])
        r["enabled"] = bool(r["enabled"])
        return r

    def get(self, wid: int) -> dict:
        r = self.e.db.one("SELECT * FROM workflows WHERE id=?", (wid,))
        if not r:
            raise PluginError(f"No workflow #{wid}.")
        return self._row(r)

    def find(self, ref) -> dict:
        r = self.e.db.one("SELECT * FROM workflows WHERE id=? OR name=? COLLATE NOCASE", (ref if str(ref).isdigit() else -1, str(ref)))
        if not r:
            raise PluginError(f"No workflow '{ref}'.")
        return self._row(r)

    def list(self) -> list[dict]:
        return [self._row(r) for r in self.e.db.query("SELECT * FROM workflows ORDER BY name")]

    def delete(self, wid: int) -> bool:
        return self.e.db.rowcount("DELETE FROM workflows WHERE id=?", (wid,)) > 0

    def set_enabled(self, wid: int, enabled: bool) -> dict:
        w = self.get(wid)
        nxt = self._next_run(w["trigger"], datetime.now()) if enabled else None
        self.e.db.execute("UPDATE workflows SET enabled=?, next_run=? WHERE id=?", (int(enabled), nxt, wid))
        return self.get(wid)

    def runs(self, limit: int = 50, workflow_id: Optional[int] = None) -> list[dict]:
        sql, p = "SELECT * FROM workflow_runs", []
        if workflow_id:
            sql += " WHERE workflow_id=?"; p.append(workflow_id)
        sql += " ORDER BY id DESC LIMIT ?"; p.append(limit)
        out = []
        for r in self.e.db.query(sql, p):
            r["progress"] = json.loads(r["progress"] or "[]")
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else None
            out.append(r)
        return out

    def get_run(self, rid: int) -> dict:
        for r in self.runs(500):
            if r["id"] == rid:
                return r
        raise PluginError(f"No run #{rid}.")

    def recover_interrupted(self) -> None:
        self.e.db.execute("UPDATE workflow_runs SET status='cancelled', error='interrupted by restart', finished=? "
                          "WHERE status IN ('running','awaiting_confirmation','pending')", (time.time(),))

    # ── triggers ──────────────────────────────────────────────────────
    @staticmethod
    def _next_run(trigger: dict, now: datetime) -> Optional[float]:
        if trigger["type"] == "interval":
            return (now + timedelta(minutes=trigger["minutes"])).timestamp()
        if trigger["type"] == "schedule":
            h, m = map(int, trigger["at"].split(":"))
            for add in range(0, 8):
                cand = (now + timedelta(days=add)).replace(hour=h, minute=m, second=0, microsecond=0)
                if cand > now and (not trigger["days"] or cand.weekday() in trigger["days"]):
                    return cand.timestamp()
        return None

    def tick(self) -> int:
        """Fire due scheduled/interval workflows."""
        fired, now = 0, time.time()
        for w in self.list():
            if not w["enabled"] or w["trigger"]["type"] not in ("schedule", "interval"):
                continue
            if w["next_run"] is None:
                self.e.db.execute("UPDATE workflows SET next_run=? WHERE id=?",
                                  (self._next_run(w["trigger"], datetime.now()), w["id"]))
            elif w["next_run"] <= now:
                nxt = self._next_run(w["trigger"], datetime.now())
                self.e.db.execute("UPDATE workflows SET next_run=? WHERE id=?", (nxt, w["id"]))
                self.run_async(w["id"], origin="schedule")
                fired += 1
        return fired

    def on_event(self, topic: str, payload: dict) -> None:
        import fnmatch
        if topic.startswith(("workflow.", "activity.", "notify.", "command.", "confirm.")):
            return                          # never let workflows trigger on their own bookkeeping
        for w in self.list():
            t = w["trigger"]
            if w["enabled"] and t["type"] == "event" and fnmatch.fnmatch(topic, t["topic"]):
                self.run_async(w["id"], origin=f"event:{topic}", seed={"event": {"topic": topic, "payload": payload}})

    # ── execution ─────────────────────────────────────────────────────
    def run_async(self, wid: int, origin: str = "user", seed: Optional[dict] = None) -> int:
        w = self.get(wid)
        rid = self._new_run(w["name"], wid, f"workflow:{w['name']}", w["steps"])
        t = threading.Thread(target=self._execute, args=(rid, w["steps"], origin, seed or {}, wid), daemon=True,
                             name=f"gal-wf-{rid}")
        self._threads[rid] = t
        t.start()
        return rid

    def run_plan(self, name: str, request: str, steps: list, origin: str = "assistant", wait: bool = False,
                 timeout: float = 120) -> int:
        steps = validate_steps(steps, self.e.registry)
        rid = self._new_run(name, None, request, steps)
        t = threading.Thread(target=self._execute, args=(rid, steps, origin, {}, None), daemon=True, name=f"gal-wf-{rid}")
        self._threads[rid] = t
        t.start()
        if wait:
            t.join(timeout)
        return rid

    def run_sync(self, wid: int, origin: str = "user", timeout: float = 300) -> dict:
        rid = self.run_async(wid, origin)
        self._threads[rid].join(timeout)
        return self.get_run(rid)

    def _new_run(self, name, wid, request, steps) -> int:
        prog = [{"id": s["id"], "plugin": s["plugin"], "command": s["command"], "status": "pending", "message": ""} for s in steps]
        rid = self.e.db.execute(
            "INSERT INTO workflow_runs(workflow_id,name,request,plan,status,progress,started) VALUES(?,?,?,?,?,?,?)",
            (wid, name, request, json.dumps(steps), "running", json.dumps(prog), time.time()))
        self._cancels[rid] = threading.Event()
        self.e.bus.publish("workflow.started", {"run": rid, "name": name})
        return rid

    def cancel(self, rid: int) -> bool:
        ev = self._cancels.get(rid)
        if ev:
            ev.set()
            return True
        return False

    def _save_progress(self, rid, prog, status=None, error="") -> None:
        if status:
            self.e.db.execute("UPDATE workflow_runs SET progress=?, status=?, error=?, finished=? WHERE id=?",
                              (json.dumps(prog, default=str), status, error,
                               time.time() if status in ("completed", "failed", "cancelled") else None, rid))
        else:
            self.e.db.execute("UPDATE workflow_runs SET progress=? WHERE id=?", (json.dumps(prog, default=str), rid))

    def _execute(self, rid: int, steps: list, origin: str, seed: dict, wid: Optional[int]) -> None:
        results: dict = dict(seed)
        cancel = self._cancels[rid]
        prog = self.get_run(rid)["progress"]
        by_id = {p["id"]: p for p in prog}
        final, error = "completed", ""
        try:
            for step in steps:
                p = by_id[step["id"]]
                if cancel.is_set():
                    p["status"] = "cancelled"
                    final, error = "cancelled", "cancelled by user"
                    break
                if not eval_condition(step.get("condition"), results):
                    p["status"], p["message"] = "skipped", "condition not met"
                    results[step["id"]] = {"ok": False, "skipped": True, "message": "skipped"}
                    self._save_progress(rid, prog)
                    continue
                p["status"] = "running"
                self._save_progress(rid, prog)
                res = self._run_step(step, results, origin, rid, p, cancel, prog)
                results[step["id"]] = {"ok": res.ok, "message": res.message, "data": res.data, "status": res.status}
                p["status"] = "completed" if res.ok else ("cancelled" if res.status == "cancelled" else "failed")
                p["message"] = (res.message or "")[:300]
                self._save_progress(rid, prog)
                if not res.ok:
                    if res.status == "cancelled" and cancel.is_set():
                        final, error = "cancelled", "cancelled by user"
                        break
                    if step.get("on_error") != "continue":
                        final, error = "failed", f"step {step['id']} failed: {res.message}"
                        break
        except Exception as e:  # noqa: BLE001
            final, error = "failed", f"{type(e).__name__}: {e}"
            log.error("workflow run %s crashed: %s", rid, e, exc_info=True)
        self._save_progress(rid, prog, final, error)
        if wid:
            self.e.db.execute("UPDATE workflows SET last_run=? WHERE id=?", (time.time(), wid))
        name = self.e.db.one("SELECT name FROM workflow_runs WHERE id=?", (rid,))["name"]
        self.e.bus.publish("workflow.finished", {"run": rid, "name": name, "status": final, "error": error})
        if final == "failed":
            self.e.notifications.add(f"Workflow failed: {name}", error, "workflow", 2, "automation")
        elif final == "completed" and origin != "user":
            self.e.notifications.add(f"Workflow finished: {name}", "", "workflow", 1, "automation")
        self._cancels.pop(rid, None)

    def _run_step(self, step, results, origin, rid, p, cancel, prog) -> CommandResult:
        args = resolve(step.get("args") or {}, results)
        attempts = step.get("retries", 0) + 1
        res = CommandResult(False, "not run", status="failed")
        for attempt in range(attempts):
            res = self.e.registry.execute(step["plugin"], step["command"], args, origin=origin, run_id=rid)
            if res.status == "awaiting_confirmation":
                p["status"], p["message"] = "awaiting_confirmation", res.message
                p["pending_id"] = res.pending_id
                self._save_progress(rid, prog)
                self.e.db.execute("UPDATE workflow_runs SET status='awaiting_confirmation' WHERE id=?", (rid,))
                pend = self.e.confirmations.get(res.pending_id)
                waited = 0.0
                while pend and not pend.done.wait(0.5):
                    waited += 0.5
                    if cancel.is_set():
                        self.e.registry.deny(res.pending_id)
                        break
                    if waited > CONFIRM_WAIT:
                        break
                self.e.db.execute("UPDATE workflow_runs SET status='running' WHERE id=?", (rid,))
                p.pop("pending_id", None)
                if pend and pend.state == "approved" and isinstance(pend.result, CommandResult):
                    res = pend.result
                else:
                    state = pend.state if pend else "expired"
                    return CommandResult(False, f"Confirmation {state}; step not performed.", status="cancelled")
            if res.ok or res.status == "cancelled" or cancel.is_set():
                return res
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 5) if not self.e.db.path.name.startswith("test") else 0.01)
        return res
