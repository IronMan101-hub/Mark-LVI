"""Tasks, reminders, calendar and the daily briefing — all persisted in SQLite."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from ..google_auth import GoogleAuth, GoogleAuthError
from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from ..timeparse import next_occurrence, parse_recurrence, parse_when

PRIORITY = {"low": 1, "normal": 2, "medium": 2, "high": 3, "urgent": 4}
PRIO_NAME = {1: "low", 2: "normal", 3: "high", 4: "urgent"}
CAL_SCOPE = "https://www.googleapis.com/auth/calendar"


def _fmt(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else ""


class Productivity(Plugin):
    manifest = Manifest(
        id="productivity", name="Productivity", category="Productivity", version="1.0.0",
        description="Tasks, reminders (one-off and recurring), calendar and the daily briefing.",
        permissions=["schedule", "notify", "plugins.call", "memory.read", "network"], background=True,
        config_schema=[
            {"key": "client_id", "label": "Google OAuth client ID (optional, for Google Calendar)", "secret": True},
            {"key": "client_secret", "label": "Google OAuth client secret", "secret": True},
            {"key": "calendar_id", "label": "Google calendar ID", "type": "string", "default": "primary"},
            {"key": "briefing_sections", "label": "Briefing sections", "type": "string",
             "default": "tasks,reminders,calendar,notifications,news,system"}],
        setup_help="Tasks, reminders and the local calendar work with no setup. Google Calendar sync needs your own "
                   "Google Cloud OAuth client (Desktop app); enter it above then use 'google_connect'.")

    def initialize(self, ctx):
        super().initialize(ctx)
        self.google = GoogleAuth(ctx, [CAL_SCOPE])

    def health(self):
        return Health("ok", "local tasks/reminders ready" + ("; Google Calendar connected" if self.google.connected() else ""))

    # ── background ticks ──────────────────────────────────────────────
    def ticks(self):
        return [("due-reminders", 15, self.fire_due), ("deadlines", 300, self.check_deadlines)]

    def fire_due(self) -> int:
        db, now, fired = self.ctx.db, time.time(), 0
        for r in db.query("SELECT * FROM reminders WHERE status='active' AND due<=? ORDER BY due", (now,)):
            late = now - r["due"]
            prefix = "" if late < 120 else f"(missed at {_fmt(r['due'])}) "
            self.ctx.notify(f"Reminder: {r['text']}", prefix + r["text"], "reminder", 3,
                            dedupe_key=f"rem-{r['id']}-{int(r['due'])}")
            if r["announce"] and late < 3600:
                self.ctx.say(f"Reminder: {r['text']}")
            self.ctx.publish("reminder.fired", {"id": r["id"], "text": r["text"]})
            nxt = next_occurrence(datetime.fromtimestamp(r["due"]), r["recurrence"]) if r["recurrence"] else None
            if nxt:
                db.execute("UPDATE reminders SET due=?, last_fired=? WHERE id=?", (nxt.timestamp(), now, r["id"]))
            else:
                db.execute("UPDATE reminders SET status='done', last_fired=? WHERE id=?", (now, r["id"]))
            fired += 1
        for ev in db.query("SELECT * FROM events_local WHERE reminded=0 AND start>?", (now - 600,)):
            if ev["start"] - ev["remind_minutes"] * 60 <= now:
                self.ctx.notify(f"Upcoming: {ev['title']}", f"Starts {_fmt(ev['start'])}" + (f" at {ev['location']}" if ev["location"] else ""),
                                "reminder", 2, dedupe_key=f"ev-{ev['id']}")
                db.execute("UPDATE events_local SET reminded=1 WHERE id=?", (ev["id"],))
        return fired

    def check_deadlines(self) -> None:
        now = time.time()
        for t in self.ctx.db.query("SELECT * FROM tasks WHERE status='pending' AND due IS NOT NULL AND due<=?", (now + 3600,)):
            overdue = t["due"] < now
            self.ctx.notify(("Overdue: " if overdue else "Due soon: ") + t["title"], f"Deadline {_fmt(t['due'])}", "task",
                            3 if overdue else 2, dedupe_key=f"task-{t['id']}-{'o' if overdue else 's'}", )

    # ── tasks ─────────────────────────────────────────────────────────
    @command(description="Create a task. 'due' accepts natural language (e.g. 'friday 5pm').",
             params={"title": {"type": "string", "required": True}, "due": {"type": "string"},
                     "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                     "category": {"type": "string", "default": "general"}, "notes": {"type": "string"}},
             permissions=("schedule",))
    def add_task(self, title, due="", priority="normal", category="general", notes=""):
        due_ts = None
        if due:
            d = parse_when(due)
            if not d:
                raise PluginError(f"I couldn't understand the deadline '{due}'. Try 'tomorrow 5pm' or '2026-12-01 14:00'.")
            due_ts = d.timestamp()
        tid = self.ctx.db.execute("INSERT INTO tasks(title,notes,due,priority,category,created) VALUES(?,?,?,?,?,?)",
                                  (title.strip(), notes, due_ts, PRIORITY[priority], category, time.time()))
        row = self.ctx.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not row:
            return {"ok": False, "message": "The task was not saved."}
        return {"message": f"Task #{tid} added: {title}" + (f", due {_fmt(due_ts)}" if due_ts else ""), "data": {"id": tid}}

    @command(description="List tasks. status: pending | completed | all.",
             params={"status": {"type": "string", "enum": ["pending", "completed", "all"], "default": "pending"},
                     "category": {"type": "string"}, "query": {"type": "string"}}, readonly=True)
    def list_tasks(self, status="pending", category="", query=""):
        sql, p = "SELECT * FROM tasks WHERE 1=1", []
        if status != "all":
            sql += " AND status=?"; p.append(status)
        if category:
            sql += " AND category=?"; p.append(category)
        if query:
            sql += " AND (title LIKE ? OR notes LIKE ?)"; p += [f"%{query}%"] * 2
        sql += " ORDER BY status DESC, priority DESC, COALESCE(due, 9e18), id"
        rows = self.ctx.db.query(sql, p)
        for r in rows:
            r["priority_name"] = PRIO_NAME.get(r["priority"], "normal")
        if not rows:
            return {"message": f"No {status if status != 'all' else ''} tasks.".replace("  ", " "), "data": {"tasks": []}}
        lines = [f"#{r['id']} {r['title']}" + (f" (due {_fmt(r['due'])})" if r["due"] else "") + f" [{r['priority_name']}]" for r in rows[:15]]
        return {"message": f"{len(rows)} task(s): " + "; ".join(lines), "data": {"tasks": rows}}

    @command(description="Edit a task's title, deadline, priority, category or notes.",
             params={"id": {"type": "integer", "required": True}, "title": {"type": "string"}, "due": {"type": "string"},
                     "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                     "category": {"type": "string"}, "notes": {"type": "string"}}, permissions=("schedule",))
    def edit_task(self, id, title="", due="", priority="", category="", notes=""):
        if not self.ctx.db.one("SELECT id FROM tasks WHERE id=?", (id,)):
            raise PluginError(f"No task #{id}.")
        sets, vals = [], []
        for col, v in (("title", title), ("category", category), ("notes", notes)):
            if v:
                sets.append(f"{col}=?"); vals.append(v)
        if priority:
            sets.append("priority=?"); vals.append(PRIORITY[priority])
        if due:
            d = parse_when(due)
            if not d:
                raise PluginError(f"I couldn't understand the deadline '{due}'.")
            sets.append("due=?"); vals.append(d.timestamp())
        if not sets:
            raise PluginError("Nothing to change.")
        self.ctx.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*vals, id))
        return f"Task #{id} updated."

    @command(description="Mark a task completed (by id).", params={"id": {"type": "integer", "required": True}},
             permissions=("schedule",))
    def complete_task(self, id):
        n = self.ctx.db.rowcount("UPDATE tasks SET status='completed', completed=? WHERE id=? AND status='pending'", (time.time(), id))
        if not n:
            raise PluginError(f"No pending task #{id}.")
        return f"Task #{id} marked completed."

    @command(description="Delete a task (by id).", params={"id": {"type": "integer", "required": True}},
             permissions=("schedule",), confirm="Delete task #{id}?", destructive=True)
    def delete_task(self, id):
        if not self.ctx.db.rowcount("DELETE FROM tasks WHERE id=?", (id,)):
            raise PluginError(f"No task #{id}.")
        return f"Task #{id} deleted."

    # ── reminders ─────────────────────────────────────────────────────
    @command(description="Create a reminder. 'when' is natural language ('in 20 minutes', 'tomorrow 8am'); "
                         "'every ...' phrases make it recurring.",
             params={"text": {"type": "string", "required": True}, "when": {"type": "string", "required": True},
                     "recurrence": {"type": "string", "description": "daily | weekdays | weekly | monthly | hourly | every:N:minutes|hours|days"},
                     "announce": {"type": "boolean", "default": True}}, permissions=("schedule",))
    def add_reminder(self, text, when, recurrence="", announce=True):
        rec = recurrence or parse_recurrence(when)
        d = parse_when(when)
        if not d:
            raise PluginError(f"I couldn't work out when '{when}' is. Try 'in 20 minutes' or 'tomorrow at 8am'.")
        if d < datetime.now() - timedelta(minutes=1):
            raise PluginError("That time is in the past.")
        text = text.strip()
        rid = self.ctx.db.execute("INSERT INTO reminders(text,due,recurrence,announce,created) VALUES(?,?,?,?,?)",
                                  (text, d.timestamp(), rec, int(announce), time.time()))
        if not self.ctx.db.one("SELECT id FROM reminders WHERE id=?", (rid,)):
            return {"ok": False, "message": "The reminder was not saved."}
        return {"message": f"Reminder #{rid} set for {_fmt(d.timestamp())}" + (f", repeating {rec}" if rec else "") + f": {text}",
                "data": {"id": rid, "due": d.timestamp()}}

    @command(description="List reminders. status: active | done | all.", readonly=True,
             params={"status": {"type": "string", "enum": ["active", "done", "all"], "default": "active"}})
    def list_reminders(self, status="active"):
        rows = self.ctx.db.query("SELECT * FROM reminders" + ("" if status == "all" else " WHERE status=?") + " ORDER BY due",
                                 [] if status == "all" else [status])
        if not rows:
            return {"message": "No reminders.", "data": {"reminders": []}}
        return {"message": f"{len(rows)} reminder(s): " + "; ".join(
            f"#{r['id']} {r['text']} at {_fmt(r['due'])}" + (f" ({r['recurrence']})" if r["recurrence"] else "") for r in rows[:15]),
            "data": {"reminders": rows}}

    @command(description="Edit a reminder's text, time or recurrence.",
             params={"id": {"type": "integer", "required": True}, "text": {"type": "string"}, "when": {"type": "string"},
                     "recurrence": {"type": "string"}}, permissions=("schedule",))
    def edit_reminder(self, id, text="", when="", recurrence=""):
        if not self.ctx.db.one("SELECT id FROM reminders WHERE id=?", (id,)):
            raise PluginError(f"No reminder #{id}.")
        sets, vals = [], []
        if text:
            sets.append("text=?"); vals.append(text)
        if when:
            d = parse_when(when)
            if not d:
                raise PluginError(f"I couldn't understand '{when}'.")
            sets += ["due=?", "status='active'"]; vals.append(d.timestamp())
        if recurrence:
            sets.append("recurrence=?"); vals.append("" if recurrence == "none" else recurrence)
        if not sets:
            raise PluginError("Nothing to change.")
        self.ctx.db.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=?", (*vals, id))
        return f"Reminder #{id} updated."

    @command(description="Delete a reminder (by id).", params={"id": {"type": "integer", "required": True}},
             permissions=("schedule",))
    def delete_reminder(self, id):
        if not self.ctx.db.rowcount("DELETE FROM reminders WHERE id=?", (id,)):
            raise PluginError(f"No reminder #{id}.")
        return f"Reminder #{id} deleted."

    # ── calendar ──────────────────────────────────────────────────────
    @command(description="Add a calendar event (local calendar, mirrored to Google Calendar when connected).",
             params={"title": {"type": "string", "required": True}, "start": {"type": "string", "required": True},
                     "duration_minutes": {"type": "integer", "default": 60, "minimum": 1}, "location": {"type": "string"},
                     "notes": {"type": "string"}, "remind_minutes": {"type": "integer", "default": 15}},
             permissions=("schedule", "network"))
    def add_event(self, title, start, duration_minutes=60, location="", notes="", remind_minutes=15):
        d = parse_when(start)
        if not d:
            raise PluginError(f"I couldn't understand the start time '{start}'.")
        end = d + timedelta(minutes=duration_minutes)
        eid = self.ctx.db.execute(
            "INSERT INTO events_local(title,start,end,location,notes,remind_minutes,created) VALUES(?,?,?,?,?,?,?)",
            (title, d.timestamp(), end.timestamp(), location, notes, remind_minutes, time.time()))
        msg = f"Event #{eid} '{title}' added for {_fmt(d.timestamp())}."
        if self.google.connected():
            try:
                self.google.post(f"https://www.googleapis.com/calendar/v3/calendars/{self.ctx.config.get('calendar_id') or 'primary'}/events",
                                 {"summary": title, "location": location, "description": notes,
                                  "start": {"dateTime": d.astimezone().isoformat()}, "end": {"dateTime": end.astimezone().isoformat()}})
                msg += " Also added to Google Calendar."
            except GoogleAuthError as e:
                msg += f" (Google Calendar sync failed: {e})"
        return {"message": msg, "data": {"id": eid}}

    @command(description="Show events in the next N days (local + Google Calendar if connected).", readonly=True,
             params={"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 365}, "query": {"type": "string"}},
             permissions=("schedule", "network"))
    def upcoming_events(self, days=7, query=""):
        now = time.time()
        rows = self.ctx.db.query("SELECT id,title,start,end,location FROM events_local WHERE start>=? AND start<=? ORDER BY start",
                                 (now - 3600, now + days * 86400))
        for r in rows:
            r["source"] = "local"
        note = ""
        if self.google.connected():
            try:
                iso = lambda ts: datetime.fromtimestamp(ts).astimezone().isoformat()  # noqa: E731
                j = self.google.get(f"https://www.googleapis.com/calendar/v3/calendars/{self.ctx.config.get('calendar_id') or 'primary'}/events",
                                    timeMin=iso(now), timeMax=iso(now + days * 86400), singleEvents="true", orderBy="startTime", maxResults=50)
                for it in j.get("items", []):
                    s = it.get("start", {}).get("dateTime") or it.get("start", {}).get("date")
                    try:
                        ts = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    rows.append({"id": it.get("id"), "title": it.get("summary", "(no title)"), "start": ts,
                                 "end": None, "location": it.get("location", ""), "source": "google"})
            except GoogleAuthError as e:
                note = f" (Google Calendar unavailable: {e})"
        if query:
            rows = [r for r in rows if query.lower() in (r["title"] or "").lower()]
        rows.sort(key=lambda r: r["start"])
        if not rows:
            return {"message": f"No events in the next {days} days." + note, "data": {"events": []}}
        return {"message": f"{len(rows)} event(s): " + "; ".join(f"{r['title']} {_fmt(r['start'])}" for r in rows[:10]) + note,
                "data": {"events": rows}}

    @command(description="Edit a local calendar event.", permissions=("schedule",),
             params={"id": {"type": "integer", "required": True}, "title": {"type": "string"}, "start": {"type": "string"},
                     "location": {"type": "string"}})
    def edit_event(self, id, title="", start="", location=""):
        if not self.ctx.db.one("SELECT id FROM events_local WHERE id=?", (id,)):
            raise PluginError(f"No local event #{id}.")
        sets, vals = [], []
        if title: sets.append("title=?"); vals.append(title)
        if location: sets.append("location=?"); vals.append(location)
        if start:
            d = parse_when(start)
            if not d:
                raise PluginError(f"I couldn't understand '{start}'.")
            sets += ["start=?", "reminded=0"]; vals.append(d.timestamp())
        if not sets:
            raise PluginError("Nothing to change.")
        self.ctx.db.execute(f"UPDATE events_local SET {', '.join(sets)} WHERE id=?", (*vals, id))
        return f"Event #{id} updated."

    @command(description="Delete a local calendar event.", permissions=("schedule",), confirm="Delete calendar event #{id}?",
             params={"id": {"type": "integer", "required": True}}, destructive=True)
    def delete_event(self, id):
        if not self.ctx.db.rowcount("DELETE FROM events_local WHERE id=?", (id,)):
            raise PluginError(f"No local event #{id}.")
        return f"Event #{id} deleted."

    @command(description="Begin Google Calendar sign-in; returns a URL to open in the browser.", permissions=("network",))
    def google_connect(self):
        try:
            url = self.google.begin()
        except GoogleAuthError as e:
            raise PluginError(str(e))
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        return {"message": "Complete the Google sign-in in your browser.", "data": {"url": url}}

    # ── briefing ──────────────────────────────────────────────────────
    @command(description="Daily briefing: today's tasks, reminders, events, notifications, news and system status.",
             readonly=True, permissions=("schedule", "plugins.call"), timeout=60)
    def daily_briefing(self):
        sections = {s.strip() for s in str(self.ctx.config.get("briefing_sections") or "").split(",")}
        now = datetime.now()
        end_of_day = now.replace(hour=23, minute=59, second=59).timestamp()
        parts, data = [f"{'Good morning' if now.hour < 12 else 'Good afternoon' if now.hour < 18 else 'Good evening'}. "
                       f"It's {now.strftime('%A %d %B, %H:%M')}."], {}
        db = self.ctx.db
        if "tasks" in sections:
            tasks = db.query("SELECT * FROM tasks WHERE status='pending' AND (due IS NULL OR due<=?) ORDER BY priority DESC, due", (end_of_day,))
            overdue = [t for t in tasks if t["due"] and t["due"] < time.time()]
            data["tasks"] = tasks
            parts.append(f"Tasks: {len(tasks)} to do today" + (f", {len(overdue)} overdue" if overdue else "") +
                         (": " + ", ".join(t["title"] for t in tasks[:5]) if tasks else "") + ".")
        if "reminders" in sections:
            rem = db.query("SELECT * FROM reminders WHERE status='active' AND due<=? ORDER BY due", (time.time() + 86400,))
            data["reminders"] = rem
            parts.append("Reminders next 24h: " + ("; ".join(f"{r['text']} at {_fmt(r['due'])}" for r in rem[:5]) if rem else "none") + ".")
        if "calendar" in sections:
            ev = db.query("SELECT * FROM events_local WHERE start>=? AND start<=? ORDER BY start", (time.time() - 1800, end_of_day))
            data["events"] = ev
            parts.append("Calendar today: " + ("; ".join(f"{e['title']} {_fmt(e['start'])}" for e in ev[:5]) if ev else "nothing scheduled") + ".")
        if "notifications" in sections:
            n = self.ctx._e.notifications.list(unread_only=True, limit=5)
            data["notifications"] = n
            if n:
                parts.append(f"{len(n)} unread notification(s): " + "; ".join(x["title"] for x in n[:3]) + ".")
        for sec, pid, cmd in (("news", "news", "headlines"), ("system", "sysmon", "snapshot")):
            if sec in sections and self.ctx.has("plugins.call"):
                r = self.ctx.call(pid, cmd, {"limit": 3} if sec == "news" else {})
                if r.ok:
                    data[sec] = r.data
                    parts.append(("Headlines: " if sec == "news" else "System: ") + r.message[:300])
        return {"message": " ".join(parts), "data": data}


PLUGINS = [Productivity]
