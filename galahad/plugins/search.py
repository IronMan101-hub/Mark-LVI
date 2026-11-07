"""Unified search across files, apps, tabs, conversations, tasks, reminders, articles, memory, research.
Files are searched only in the folders the Files plugin is allowed to scan."""
from __future__ import annotations

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

SCOPES = ["files", "apps", "tabs", "conversations", "tasks", "reminders", "articles", "memory", "research", "events"]


class Search(Plugin):
    manifest = Manifest(
        id="search", name="Unified Search", category="Utilities", version="1.0.0",
        description="One search box across local files, folders, apps, browser tabs, conversations, tasks, reminders, "
                    "saved news, memory, research and calendar.",
        permissions=["fs.scan", "plugins.call", "memory.read"])

    def health(self):
        return Health("ok")

    @command(description="Search everywhere (or limit with scopes).", readonly=True, timeout=60,
             params={"query": {"type": "string", "required": True}, "scopes": {"type": "array", "description": f"any of {SCOPES}"},
                     "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 50}})
    def search(self, query, scopes=None, limit=8):
        scopes = [s for s in (scopes or SCOPES) if s in SCOPES]
        q, like, db, res = query.strip(), f"%{query.strip()}%", self.ctx.db, []

        def add(kind, title, snippet="", action=None, section=None, **extra):
            res.append({"type": kind, "title": str(title)[:140], "snippet": str(snippet)[:200], "action": action, "section": section, **extra})

        if "tasks" in scopes:
            for r in db.query("SELECT * FROM tasks WHERE title LIKE ? OR notes LIKE ? ORDER BY status DESC, id DESC LIMIT ?", (like, like, limit)):
                add("task", r["title"], f"{r['status']}", section="productivity", id=r["id"])
        if "reminders" in scopes:
            for r in db.query("SELECT * FROM reminders WHERE text LIKE ? LIMIT ?", (like, limit)):
                add("reminder", r["text"], r["status"], section="productivity", id=r["id"])
        if "events" in scopes:
            for r in db.query("SELECT * FROM events_local WHERE title LIKE ? OR location LIKE ? LIMIT ?", (like, like, limit)):
                add("event", r["title"], r["location"], section="productivity", id=r["id"])
        if "conversations" in scopes:
            for r in db.query("SELECT * FROM conversations WHERE content LIKE ? ORDER BY id DESC LIMIT ?", (like, limit)):
                add("conversation", r["content"][:100], r["role"], section="assistant", id=r["id"])
        if "articles" in scopes:
            for r in db.query("SELECT * FROM articles WHERE (saved=1 OR bookmarked=1 OR read_at IS NOT NULL OR title LIKE ?) AND (title LIKE ? OR summary LIKE ?) "
                              "ORDER BY saved DESC, fetched DESC LIMIT ?", (like, like, like, limit)):
                add("article", r["title"], r["publisher"], action={"plugin": "browser", "command": "open_url", "args": {"url": r["url"]}},
                    section="intelligence", id=r["id"])
        if "research" in scopes:
            for r in db.query("SELECT id,question,created FROM research WHERE question LIKE ? OR report LIKE ? LIMIT ?", (like, like, limit)):
                add("research", r["question"], "report", section="intelligence", id=r["id"])
        if "memory" in scopes and self.ctx.has("memory.read"):
            for r in self.ctx._e.memory.recall(q, limit):
                add("memory", f"{r['key']}: {r['value']}", r["category"], section="assistant")
        if "files" in scopes and self.ctx.has("fs.scan"):
            r = self.ctx.call("files", "search", {"query": q, "limit": limit})
            for f in (r.data or {}).get("results", []) if r.ok else []:
                add("folder" if f["is_dir"] else "file", f["name"], f["path"],
                    action={"plugin": "files", "command": "open", "args": {"path": f["path"]}}, section="documents", path=f["path"])
        if "apps" in scopes:
            r = self.ctx.call("computer", "find_app", {"query": q})
            for a in (r.data or {}).get("apps", [])[:limit] if r.ok else []:
                add("app", a, "application", action={"plugin": "computer", "command": "open_app", "args": {"name": a}}, section="home")
        if "tabs" in scopes:
            ent = self.ctx._e.registry.entries.get("browser")
            if ent and ent.state == "active" and ent.plugin._tabs_online():
                r = self.ctx.call("browser", "tabs", {})
                for t in (r.data or {}).get("tabs", []) if r.ok else []:
                    if q.lower() in (t["title"] + t["url"]).lower():
                        add("tab", t["title"], t["url"], action={"plugin": "browser", "command": "switch_tab", "args": {"index": t["index"]}}, section="home")
        return {"message": f"{len(res)} result(s) for '{q}' across {len(scopes)} area(s)." + (" Top: " + "; ".join(f"{r['type']}: {r['title']}" for r in res[:4]) if res else ""),
                "data": {"results": res, "scopes": scopes}}

    @command(description="Open a search result by running its attached action.", permissions=("plugins.call",),
             params={"plugin": {"type": "string", "required": True}, "command": {"type": "string", "required": True},
                     "args": {"type": "object", "default": {}}})
    def open_result(self, plugin, command, args=None):
        if (plugin, command) not in {("files", "open"), ("browser", "open_url"), ("computer", "open_app"), ("browser", "switch_tab")}:
            raise PluginError("That kind of result can't be opened from search.")
        r = self.ctx.call(plugin, command, args or {})
        return {"ok": r.ok, "message": r.message, "data": r.data}


PLUGINS = [Search]
