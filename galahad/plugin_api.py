"""The contract every GALAHAD plugin is written against.

A plugin is a class deriving from `Plugin`, with a `manifest` and methods
decorated with `@command`. It never receives the engine — only a `PluginContext`,
whose every capability is gated by a permission the user controls.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

CATEGORIES = ("System", "Browser", "Media", "Communication", "Office", "Productivity",
              "Intelligence", "Finance", "AI", "Vision", "Automation", "Utilities")

LIFECYCLE = ("discovered", "validated", "registered", "initialised", "active",
             "suspended", "disabled", "unloaded", "error")

PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")


class PluginError(Exception):
    """Raise from a command to report a clean, user-facing failure."""


class NeedsConfig(PluginError):
    """The command can't run until the user supplies credentials/settings."""


@dataclass
class Manifest:
    id: str
    name: str
    description: str
    category: str
    version: str = "1.0.0"
    author: str = "GALAHAD"
    permissions: list[str] = field(default_factory=list)
    requires_python: list[str] = field(default_factory=list)     # import names that MUST exist
    optional_python: list[str] = field(default_factory=list)     # import names that unlock extras
    requires_plugins: list[str] = field(default_factory=list)
    config_schema: list[dict] = field(default_factory=list)      # {key,label,type,secret,required,default,help}
    subscriptions: list[str] = field(default_factory=list)       # event patterns
    platforms: Optional[list[str]] = None                        # e.g. ["win32"]; None = any
    background: bool = False                                     # has a service worth toggling
    setup_help: str = ""                                         # shown when unconfigured


@dataclass
class Health:
    status: str = "ok"      # ok | unconfigured | degraded | unsupported | error
    detail: str = ""


@dataclass
class CommandSpec:
    name: str
    description: str
    params: dict
    permissions: list[str]
    confirm: Any            # None | str template | callable(args) -> Optional[str]
    destructive: bool
    timeout: float
    readonly: bool
    func_name: str


def command(name: Optional[str] = None, description: str = "", params: Optional[dict] = None,
            permissions: tuple = (), confirm: Any = None, destructive: bool = False,
            timeout: float = 30.0, readonly: bool = False):
    """Mark a method as an invocable command.

    params: {"path": {"type": "string", "description": "...", "required": True, "enum": [...]}}
    confirm: a message template ("Send email to {to}?") or a callable(args) that
             returns a message when confirmation is needed, else None.
    """
    def deco(fn: Callable):
        fn._command = {"name": name or fn.__name__, "description": description or (fn.__doc__ or "").strip(),
                       "params": params or {}, "permissions": list(permissions), "confirm": confirm,
                       "destructive": destructive, "timeout": timeout, "readonly": readonly}
        return fn
    return deco


@dataclass
class CommandResult:
    ok: bool
    message: str = ""
    data: Any = None
    status: str = "completed"   # completed | failed | cancelled | awaiting_confirmation
    pending_id: Optional[str] = None
    activity_id: Optional[int] = None
    plugin: str = ""
    command: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "data": self.data, "status": self.status,
                "pending_id": self.pending_id, "activity_id": self.activity_id, "plugin": self.plugin,
                "command": self.command, "duration": round(self.duration, 3)}


class Plugin:
    manifest: Manifest

    def __init__(self):
        self.ctx: Optional["PluginContext"] = None

    # ── lifecycle (all optional to override) ──────────────────────────
    def initialize(self, ctx: "PluginContext") -> None:
        self.ctx = ctx

    def activate(self) -> None: ...
    def suspend(self) -> None: ...
    def resume(self) -> None: ...
    def deactivate(self) -> None: ...
    def unload(self) -> None: ...
    def migrate(self, from_version: str) -> None: ...

    # ── introspection ─────────────────────────────────────────────────
    def health(self) -> Health:
        return Health("ok")

    def self_test(self) -> tuple[bool, str]:
        h = self.health()
        return h.status in ("ok", "degraded"), h.detail or h.status

    def on_event(self, topic: str, payload: dict) -> None: ...

    def ticks(self) -> list[tuple[str, float, Callable[[], None]]]:
        """Periodic jobs driven by the core scheduler: (name, seconds, fn)."""
        return []

    def commands(self) -> dict[str, CommandSpec]:
        out: dict[str, CommandSpec] = {}
        for attr in dir(type(self)):
            fn = getattr(type(self), attr, None)
            meta = getattr(fn, "_command", None)
            if meta:
                out[meta["name"]] = CommandSpec(func_name=attr, **meta)
        return out


class PluginContext:
    """The only handle a plugin has on the rest of the application."""

    def __init__(self, engine, plugin_id: str, manifest: Manifest):
        self._e = engine
        self.plugin_id = plugin_id
        self.manifest = manifest
        self.log = __import__("galahad.logs", fromlist=["get"]).get(f"plugin.{plugin_id}")
        self.cancel_event = threading.Event()   # set when the user cancels a running command
        from .secrets import ScopedSecrets
        self.secrets = ScopedSecrets(engine.secrets, plugin_id)
        self.player = None                      # the Qt UI, when running inside the desktop app

    # ── permissions ───────────────────────────────────────────────────
    def has(self, perm: str) -> bool:
        return perm in self.manifest.permissions and self._e.permissions.is_granted(self.plugin_id, perm)

    def require(self, perm: str) -> None:
        if perm not in self.manifest.permissions:
            raise PluginError(f"{self.plugin_id} did not declare permission '{perm}'")
        if not self._e.permissions.is_granted(self.plugin_id, perm):
            raise PluginError(f"Permission '{perm}' is not granted to {self.manifest.name}. "
                              f"Grant it in Plugins → {self.manifest.name} → Permissions.")

    # ── config / storage ──────────────────────────────────────────────
    @property
    def config(self) -> dict:
        cfg = {f["key"]: f.get("default") for f in self.manifest.config_schema if not f.get("secret")}
        cfg.update(self._e.registry.get_config(self.plugin_id))
        return cfg

    def secret(self, name: str) -> Optional[str]:
        return self.secrets.get(name)

    @property
    def db(self):
        return self._e.db

    def kv_get(self, key: str, default=None):
        return self._e.db.kv_get(f"plugin:{self.plugin_id}", key, default)

    def kv_set(self, key: str, value) -> None:
        self._e.db.kv_set(f"plugin:{self.plugin_id}", key, value)

    def setting(self, key: str, default=None):
        return self._e.settings.get(key, default)

    # ── services ──────────────────────────────────────────────────────
    def publish(self, topic: str, payload: Optional[dict] = None) -> None:
        self._e.bus.publish(f"{self.plugin_id}.{topic}" if "." not in topic else topic, payload or {})

    def notify(self, title: str, body: str = "", category: str = "plugin", priority: int = 1,
               dedupe_key: str = "") -> Optional[int]:
        self.require("notify")
        return self._e.notifications.add(title, body, category, priority, self.plugin_id, dedupe_key)

    def say(self, text: str) -> None:
        """Ask the voice layer to speak (no-op when there is none)."""
        self._e.bus.publish("assistant.say", {"text": text, "source": self.plugin_id})

    def call(self, plugin_id: str, command_name: str, args: Optional[dict] = None) -> CommandResult:
        """Plugin-to-plugin call. Goes through the full pipeline (permissions,
        confirmation, activity) exactly like a user request."""
        self.require("plugins.call")
        return self._e.registry.execute(plugin_id, command_name, args or {}, origin=f"plugin:{self.plugin_id}")

    def llm(self, prompt: str, *, system: str = "", purpose: str = "general", json_mode: bool = False,
            private: bool = False, max_tokens: Optional[int] = None):
        """Ask the configured AI provider. `private=True` marks content from the
        user's mail/files/finances/screen, which is refused unless the user has
        enabled Privacy → share private data with AI (every such send is logged)."""
        self.require("ai.use")
        if private and not self._e.settings.get("privacy.share_private_with_ai", False):
            raise PluginError("This would send private content to an AI provider. Enable "
                              "Settings → Privacy → 'share private data with AI' to allow it.")
        return self._e.ai.complete(prompt, system=system, purpose=purpose, json_mode=json_mode,
                                   max_tokens=max_tokens, private=private, origin=self.plugin_id)

    def http_get(self, url: str, **kw):
        self.require("network")
        return self._e.http.get(url, **kw)

    def http_post(self, url: str, **kw):
        self.require("network")
        return self._e.http.post(url, **kw)

    def remember(self, key: str, value: str, category: str = "notes"):
        self.require("memory.write")
        return self._e.memory.remember(key, value, category)

    def recall(self, query: str = ""):
        self.require("memory.read")
        return self._e.memory.recall(query)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set() or self._e.registry.cancel_requested():
            raise PluginError("Cancelled by user.")
