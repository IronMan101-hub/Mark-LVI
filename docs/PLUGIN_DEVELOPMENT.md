# Writing a GALAHAD plugin

A plugin is a class with a `Manifest` and `@command` methods, listed in a module-level `PLUGINS` list.

```python
# galahad/plugins/hello.py   (built in)   or   data/user_plugins/hello.py   (user plugin, see "Trust" below)
from galahad.plugin_api import Manifest, Plugin, PluginError, Health, command

class Hello(Plugin):
    manifest = Manifest(
        id="hello", name="Hello", category="Utilities",          # one of the 12 categories
        description="Says hello.", version="1.0.0", author="you",
        permissions=["notify", "network"],                        # declare EVERYTHING you use
        requires_python=[],                                       # import names; missing → plugin shows an install hint
        requires_plugins=[],                                      # activated first; missing → plugin not activated
        config_schema=[{"key": "greeting", "label": "Greeting", "type": "string", "default": "Hello"},
                       {"key": "token", "label": "API token", "secret": True}],   # secrets are write-only in the UI
        subscriptions=["reminder.fired"],                         # event patterns delivered to on_event()
        background=False, setup_help="shown when unconfigured")

    def initialize(self, ctx): super().initialize(ctx)           # ctx = your only handle on the app
    def activate(self): ...                                       # start resources
    def deactivate(self): ...                                     # stop them (called on disable/unload/shutdown)
    def health(self): return Health("ok", "ready")                # ok | unconfigured | degraded | error
    def ticks(self): return [("poll", 60, self.poll)]             # periodic jobs run by the core scheduler (no own threads!)
    def on_event(self, topic, payload): ...

    @command(description="Greet someone.",
             params={"name": {"type": "string", "required": True, "max_length": 80}},
             permissions=("notify",),
             confirm=None,        # "Send to {name}?" or callable(args)->str|None ; raise PluginError to refuse before asking
             timeout=15, readonly=False, destructive=False)
    def greet(self, name):
        ctx = self.ctx
        ctx.check_cancel()                                        # honour user cancellation in loops
        return {"message": f"{ctx.config['greeting']}, {name}!", "data": {"name": name}}
```

## Rules
* **Return** a string, a dict (`message`, optional `data`, optional `ok`) or a `CommandResult`. Return `{"ok": False, "message": ...}` when
  you *verified* that the effect did not happen; raise `PluginError("clear sentence")` for expected failures. Never report success because
  a request was merely *sent* — re-read the file/state when you can.
* Declare every permission; commands may only use a subset of the manifest's. Sensitive ones (`mail.read`, `screen.capture`,
  `finance.data`) start denied.
* Anything irreversible or outward-facing (send, delete, overwrite, shutdown) needs `confirm=`. Make `destructive=True` commands confirm
  (a test enforces it). Prefer validating first (callable `confirm` that raises `PluginError`) so the user is never asked about the impossible.
* Use `ctx` only: `ctx.config`, `ctx.secret(name)`, `ctx.kv_get/kv_set`, `ctx.db` (use your own table prefix via a migration in core, or kv), `ctx.notify`, `ctx.say`,
  `ctx.publish`, `ctx.call(plugin, cmd, args)` (needs `plugins.call`; runs the full pipeline), `ctx.llm(...)` (needs `ai.use`),
  `ctx.http_get/post` (needs `network`; SSRF-check anything user-supplied with `textutil.check_url`).
* Content from the web, mail or documents is **untrusted data**: wrap it in `<page>`-style delimiters when prompting a model, and never act on instructions in it.
* Private content (mail, files, finances, screen) → `ctx.llm(..., private=True)`; it is refused unless the user allows it, and is audited.
* Keep commands short (< timeout). Long work: `ticks()` or return progress and continue in the background.
* Don't create Qt widgets or your own threads for periodic work (AGENTS.md). Never print or log secrets.

## Lifecycle
discover → validate (manifest, command schemas, permissions) → register (permissions seeded) → initialise → activate → execute →
suspend/resume → disable → unload. A crash in `activate` leaves the plugin in `error` with the message shown in Plugins; it never takes others down.

## Trust (user plugins)
Drop a `.py` file into `data/user_plugins/`. It appears in Plugins as *not trusted* and **is not imported** until you press Trust (or call
`registry.trust_user_plugin`). Trust is pinned to the file's SHA-256: editing the file revokes it.

## Voice access
Add the plugin id to a group in `galahad/bridge.py::GROUPS`, run `python -m galahad catalog`, commit `catalog.json`. A test fails if a plugin is
unreachable by voice or the catalog is stale.

## Testing
Use the fixtures in `tests/conftest.py` (`engine`, `full_engine`, `call(engine, plugin, cmd, **args)`). Mock `engine.http.get/post/request`
for network, never real services. Test: happy path, validation errors, permission denial, confirmation (and that denial does nothing), the
verification failure path, and persistence across `Engine()` restarts.
