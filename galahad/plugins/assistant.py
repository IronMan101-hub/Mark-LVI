"""JARVIS Assistant: conversation, memory controls, activity & notification access."""
from __future__ import annotations

from ..plugin_api import Health, Manifest, Plugin, PluginError, command


class Assistant(Plugin):
    manifest = Manifest(
        id="assistant", name="JARVIS Assistant", category="AI", version="1.0.0",
        description="The conversational layer: understands requests, plans multi-step actions, keeps transparent memory, "
                    "and reports what it has done.",
        permissions=["ai.use", "memory.read", "memory.write", "plugins.call", "notify"],
        subscriptions=["assistant.say"])

    def health(self):
        ai = self.ctx._e.ai.available()
        return Health("ok" if ai else "degraded",
                      "AI planning online" if ai else "No AI provider configured — using the built-in offline command router.")

    def on_event(self, topic, payload):
        pl = self.ctx.player
        if pl and hasattr(pl, "write_log"):
            try:
                pl.write_log(f"JARVIS: {payload.get('text', '')}")
            except Exception:
                pass

    @command(description="Handle a free-form request: answer it, or plan and run the needed tool steps.",
             params={"message": {"type": "string", "required": True, "max_length": 4000}}, timeout=120)
    def chat(self, message):
        out = self.ctx._e.orchestrator.handle(message, origin="assistant")
        return {"ok": out.get("ok", True), "message": out["reply"], "data": out}

    @command(description="Save something about the user for later (preferences, facts they asked you to remember).",
             params={"key": {"type": "string", "required": True}, "value": {"type": "string", "required": True},
                     "category": {"type": "string", "default": "notes"}}, permissions=("memory.write",))
    def remember(self, key, value, category="notes"):
        r = self.ctx.remember(key, value, category)
        return f"I'll remember: {r['key']} = {r['value']}"

    @command(description="Look up saved memories.", params={"query": {"type": "string"}}, permissions=("memory.read",), readonly=True)
    def recall(self, query=""):
        rows = self.ctx.recall(query)
        if not rows:
            return {"message": "I don't have anything saved" + (f" about '{query}'." if query else "."), "data": {"memories": []}}
        return {"message": "; ".join(f"{r['key']}: {r['value']}" for r in rows[:8]), "data": {"memories": rows}}

    @command(description="Delete one saved memory by key.", params={"key": {"type": "string", "required": True}},
             permissions=("memory.write",), confirm="Forget '{key}'?")
    def forget(self, key):
        if not self.ctx._e.memory.forget(key):
            raise PluginError(f"I have no memory called '{key}'.")
        return f"Forgot '{key}'."

    @command(description="What GALAHAD has recently done (from the activity log).", readonly=True,
             params={"limit": {"type": "integer", "default": 10, "maximum": 50}})
    def recent_activity(self, limit=10):
        rows = self.ctx._e.activity.recent(limit)
        if not rows:
            return {"message": "Nothing yet.", "data": {"activity": []}}
        return {"message": "; ".join(f"{r['action']} → {r['status']}" for r in rows[:8]), "data": {"activity": rows}}

    @command(description="Show notifications.", readonly=True,
             params={"unread_only": {"type": "boolean", "default": True}, "category": {"type": "string"}})
    def notifications(self, unread_only=True, category=""):
        rows = self.ctx._e.notifications.list(category, unread_only, limit=20)
        return {"message": f"{len(rows)} notification(s): " + "; ".join(r["title"] for r in rows[:5]) if rows else "No notifications.",
                "data": {"notifications": rows}}

    @command(description="Mark all notifications read.")
    def mark_notifications_read(self):
        return f"Marked {self.ctx._e.notifications.mark_read()} notification(s) read."

    @command(description="What can GALAHAD do? Lists active plugins and their commands.", readonly=True)
    def capabilities(self):
        cat = self.ctx._e.registry.catalog()
        by = {}
        for c in cat:
            by.setdefault(c["plugin"], []).append(c["command"])
        return {"message": "; ".join(f"{p}: {', '.join(v)}" for p, v in by.items()), "data": {"plugins": by}}


PLUGINS = [Assistant]
