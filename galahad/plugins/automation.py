"""Workflow automation: sequential steps with data flow, conditions, retries, schedules and
event triggers. Every step runs through the same permission / confirmation pipeline as a
direct request, so a workflow can never do something you couldn't — and can never skip an
approval prompt."""
from __future__ import annotations

import json

from ..ai import parse_json
from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from ..workflows import validate_steps

STEP_DOC = ('steps: [{"id":"s1","plugin":"news","command":"headlines","args":{"category":"medicine"}}, '
            '{"id":"s2","plugin":"productivity","command":"add_task","args":{"title":"{{s1.message}}"},'
            '"condition":{"step":"s1","op":"ok"},"retries":1,"on_error":"stop|continue"}]')


class Automation(Plugin):
    manifest = Manifest(
        id="automation", name="Automation", category="Automation", version="1.0.0",
        description="Create, edit, schedule, run, cancel and review multi-step workflows across plugins.",
        permissions=["plugins.call", "ai.use", "schedule"])

    def health(self):
        w = self.ctx._e.workflows.list()
        return Health("ok", f"{len(w)} workflow(s), {sum(1 for x in w if x['enabled'])} enabled")

    @property
    def wf(self):
        return self.ctx._e.workflows

    @command(description="Turn a plain-English request into a step plan WITHOUT running it (preview). Needs an AI provider for free-form requests.",
             params={"request": {"type": "string", "required": True}}, timeout=60, readonly=True)
    def plan(self, request):
        o = self.ctx._e.orchestrator
        plan = None
        if self.ctx._e.ai.available():
            try:
                plan = o._plan_with_ai(request)
            except Exception as e:  # noqa: BLE001
                raise PluginError(f"Planning failed: {e}")
        else:
            plan = o._plan_locally(request)
        steps = [s for s in plan.get("steps", []) if isinstance(s, dict)]
        if not steps:
            return {"ok": False, "message": plan.get("reply") or "I couldn't turn that into steps.", "data": {"plan": plan}}
        try:
            clean = validate_steps(steps, self.ctx._e.registry)
        except PluginError as e:
            return {"ok": False, "message": f"The plan was invalid: {e}", "data": {"plan": plan}}
        return {"message": f"{len(clean)} step(s): " + " → ".join(f"{s['plugin']}.{s['command']}" for s in clean), "data": {"steps": clean}}

    @command(description="Save a workflow. " + STEP_DOC + ' trigger: {"type":"manual"} | {"type":"schedule","at":"08:00","days":[0,1,2,3,4]} | '
                                                          '{"type":"interval","minutes":30} | {"type":"event","topic":"reminder.fired"}.',
             params={"name": {"type": "string", "required": True}, "steps": {"type": "array", "required": True}, "description": {"type": "string"},
                     "trigger": {"type": "object"}, "enabled": {"type": "boolean", "default": True}, "id": {"type": "integer"}})
    def save(self, name, steps, description="", trigger=None, enabled=True, id=None):
        w = self.wf.save(name, steps, description, trigger, enabled, id)
        return {"message": f"Workflow '{w['name']}' saved ({len(w['steps'])} steps, trigger {w['trigger']['type']})."
                           + (f" Next run {__import__('datetime').datetime.fromtimestamp(w['next_run']):%a %d %b %H:%M}." if w.get("next_run") else ""), "data": {"workflow": w}}

    @command(description="Create a workflow from a plain-English description (AI plans the steps; you can inspect them afterwards).",
             params={"name": {"type": "string", "required": True}, "request": {"type": "string", "required": True}, "trigger": {"type": "object"}}, timeout=90)
    def create_from_text(self, name, request, trigger=None):
        p = self.plan(request)
        steps = (p.get("data") or {}).get("steps")
        if p.get("ok") is False or not steps:
            return {"ok": False, "message": p.get("message", "I couldn't build steps from that.")}
        return self.save(name, steps, request, trigger)

    @command(description="List workflows.", readonly=True)
    def list(self):
        ws = self.wf.list()
        return {"message": f"{len(ws)} workflow(s): " + "; ".join(f"{w['name']} [{w['trigger']['type']}{'' if w['enabled'] else ', disabled'}]" for w in ws) if ws else "No workflows yet.",
                "data": {"workflows": ws}}

    @command(description="Show one workflow with its steps.", readonly=True, params={"workflow": {"type": "string", "required": True}})
    def show(self, workflow):
        w = self.wf.find(workflow)
        return {"message": f"{w['name']}: " + " → ".join(f"{s['plugin']}.{s['command']}" for s in w["steps"]), "data": {"workflow": w}}

    @command(description="Run a workflow now (by name or id). Steps that need confirmation wait for you.", params={"workflow": {"type": "string", "required": True}})
    def run(self, workflow):
        w = self.wf.find(workflow)
        rid = self.wf.run_async(w["id"], origin="user")
        return {"message": f"Started '{w['name']}' (run #{rid}). Follow it in AI Execution.", "data": {"run_id": rid}}

    @command(description="Cancel a running workflow.", params={"run_id": {"type": "integer", "required": True}})
    def cancel(self, run_id):
        if not self.wf.cancel(run_id):
            raise PluginError(f"Run #{run_id} isn't running.")
        return f"Cancelling run #{run_id}…"

    @command(description="Status and per-step progress of a run.", readonly=True, params={"run_id": {"type": "integer", "required": True}})
    def status(self, run_id):
        r = self.wf.get_run(run_id)
        return {"message": f"Run #{run_id} '{r['name']}': {r['status']}" + (f" — {r['error']}" if r["error"] else ""), "data": {"run": r}}

    @command(description="Recent workflow runs.", readonly=True, params={"limit": {"type": "integer", "default": 15}, "workflow": {"type": "string"}})
    def history(self, limit=15, workflow=""):
        wid = self.wf.find(workflow)["id"] if workflow else None
        runs = self.wf.runs(limit, wid)
        return {"message": f"{len(runs)} run(s): " + "; ".join(f"#{r['id']} {r['name']} {r['status']}" for r in runs[:6]), "data": {"runs": runs}}

    @command(description="Enable or disable a workflow's trigger.", params={"workflow": {"type": "string", "required": True}, "enabled": {"type": "boolean", "required": True}})
    def set_enabled(self, workflow, enabled):
        w = self.wf.set_enabled(self.wf.find(workflow)["id"], enabled)
        return f"'{w['name']}' {'enabled' if enabled else 'disabled'}."

    @command(description="Delete a workflow.", params={"workflow": {"type": "string", "required": True}}, confirm="Delete workflow {workflow}?", destructive=True)
    def delete(self, workflow):
        w = self.wf.find(workflow)
        self.wf.delete(w["id"])
        return f"Deleted '{w['name']}'."


PLUGINS = [Automation]
