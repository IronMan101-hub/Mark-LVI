"""Plugin registry: discovery, validation, dependency order, lifecycle,
permission-checked & timeout-bounded execution, health, and configuration."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import pkgutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import VERSION, logs, paths
from .plugin_api import (CATEGORIES, CommandResult, CommandSpec, Health, Manifest, Plugin,
                         PluginContext, PluginError, PARAM_TYPES)

log = logs.get("registry")


@dataclass
class Entry:
    plugin_id: str
    cls: Optional[type] = None
    plugin: Optional[Plugin] = None
    manifest: Optional[Manifest] = None
    ctx: Optional[PluginContext] = None
    state: str = "discovered"
    error: str = ""
    source: str = "builtin"             # builtin | user
    file: str = ""
    missing: list[str] = field(default_factory=list)
    commands: dict[str, CommandSpec] = field(default_factory=dict)
    consecutive_errors: int = 0
    last_exec: float = 0.0
    last_error: str = ""
    unsubscribers: list = field(default_factory=list)


# ─────────────────────────── argument validation ───────────────────────────
def validate_args(params: dict, args: dict) -> dict:
    """Coerce + validate against a command's param schema. Unknown keys are dropped."""
    args = dict(args or {})
    out: dict = {}
    for name, spec in params.items():
        if name not in args or args[name] in (None, ""):
            if spec.get("required"):
                raise PluginError(f"Missing required parameter '{name}'.")
            if "default" in spec:
                out[name] = spec["default"]
            continue
        out[name] = _coerce(name, spec, args[name])
    return out


def _coerce(name: str, spec: dict, v: Any) -> Any:
    t = spec.get("type", "string")
    try:
        if t == "string":
            v = v if isinstance(v, str) else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
            if len(v) > spec.get("max_length", 20000):
                raise PluginError(f"Parameter '{name}' is too long.")
        elif t == "integer":
            if isinstance(v, bool):
                raise ValueError
            v = int(float(v))
        elif t == "number":
            v = float(v)
        elif t == "boolean":
            if isinstance(v, str):
                if v.strip().lower() not in ("true", "false", "yes", "no", "1", "0"):
                    raise ValueError
                v = v.strip().lower() in ("true", "yes", "1")
            else:
                v = bool(v)
        elif t == "array":
            if isinstance(v, str):
                s = v.strip()
                v = json.loads(s) if s.startswith("[") else [x.strip() for x in s.split(",") if x.strip()]
            if not isinstance(v, list):
                raise ValueError
        elif t == "object":
            if isinstance(v, str):
                v = json.loads(v)
            if not isinstance(v, dict):
                raise ValueError
    except PluginError:
        raise
    except Exception:
        raise PluginError(f"Parameter '{name}' must be a {t}.")
    if "enum" in spec and v not in spec["enum"]:
        raise PluginError(f"Parameter '{name}' must be one of: {', '.join(map(str, spec['enum']))}.")
    if t in ("integer", "number"):
        if "minimum" in spec and v < spec["minimum"]:
            raise PluginError(f"Parameter '{name}' must be >= {spec['minimum']}.")
        if "maximum" in spec and v > spec["maximum"]:
            raise PluginError(f"Parameter '{name}' must be <= {spec['maximum']}.")
    return v


def validate_manifest(m: Any) -> list[str]:
    errs = []
    if not isinstance(m, Manifest):
        return ["manifest must be a Manifest"]
    import re
    if not re.match(r"^[a-z][a-z0-9_]{1,40}$", m.id or ""):
        errs.append("id must be lowercase letters/digits/underscore")
    if not m.name or not m.description:
        errs.append("name and description are required")
    if m.category not in CATEGORIES:
        errs.append(f"category must be one of {', '.join(CATEGORIES)}")
    from .permissions import CATALOG
    for p in m.permissions:
        if p not in CATALOG:
            errs.append(f"unknown permission '{p}'")
    for f in m.config_schema:
        if "key" not in f:
            errs.append("config_schema fields need a 'key'")
    return errs


def _validate_commands(cmds: dict[str, CommandSpec], manifest: Manifest) -> list[str]:
    errs = []
    for n, c in cmds.items():
        for pn, ps in c.params.items():
            if ps.get("type", "string") not in PARAM_TYPES:
                errs.append(f"{n}.{pn}: bad type {ps.get('type')}")
        for p in c.permissions:
            if p not in manifest.permissions:
                errs.append(f"{n}: uses permission '{p}' the manifest does not declare")
    return errs


class Registry:
    def __init__(self, engine):
        self.e = engine
        self.entries: dict[str, Entry] = {}
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._running: dict[int, threading.Event] = {}

    # ───────────────────────────── discovery ─────────────────────────────
    def discover(self) -> None:
        import galahad.plugins as pkg
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            try:
                m = importlib.import_module(f"galahad.plugins.{mod.name}")
                for cls in getattr(m, "PLUGINS", []):
                    self._register_class(cls, "builtin", getattr(m, "__file__", ""))
            except Exception as e:  # noqa: BLE001
                log.error("plugin module %s failed to import: %s", mod.name, e)
                self.entries[f"mod:{mod.name}"] = Entry(f"mod:{mod.name}", state="error",
                                                      error=f"import failed: {e}", file=mod.name)
        self._discover_user_plugins()

    def _trusted(self) -> dict:
        return self.e.db.kv_get("core", "trusted_user_plugins", {})

    def _discover_user_plugins(self) -> None:
        trusted = self._trusted()
        for f in sorted(paths.user_plugins_dir().glob("*.py")):
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            pid = f"user:{f.stem}"
            if trusted.get(f.name) != digest:
                self.entries[pid] = Entry(pid, state="discovered", source="user", file=str(f),
                                          error="Not trusted. Review the file, then trust it in Plugins to load it.")
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"galahad_user_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for cls in getattr(mod, "PLUGINS", []):
                    self._register_class(cls, "user", str(f))
            except Exception as e:  # noqa: BLE001
                self.entries[pid] = Entry(pid, state="error", source="user", file=str(f), error=str(e))

    def trust_user_plugin(self, filename: str) -> None:
        f = paths.user_plugins_dir() / Path(filename).name
        if not f.exists():
            raise PluginError("No such file in the user plugins folder.")
        trusted = self._trusted()
        trusted[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
        self.e.db.kv_set("core", "trusted_user_plugins", trusted)
        self.entries.pop(f"user:{f.stem}", None)
        self._discover_user_plugins()

    def _register_class(self, cls: type, source: str, file: str) -> None:
        try:
            inst = cls()
            m = inst.manifest
        except Exception as e:  # noqa: BLE001
            self.entries[f"cls:{cls.__name__}"] = Entry(f"cls:{cls.__name__}", state="error", error=str(e), file=file)
            return
        errs = validate_manifest(m)
        if m.id in self.entries and self.entries[m.id].state != "error":
            log.error("duplicate plugin id %s ignored", m.id)
            return
        ent = Entry(m.id, cls=cls, plugin=inst, manifest=m, source=source, file=file, state="discovered")
        ent.commands = inst.commands()
        errs += _validate_commands(ent.commands, m)
        if errs:
            ent.state, ent.error = "error", "; ".join(errs)
        else:
            ent.state = "validated"
        self.entries[m.id] = ent

    # ──────────────────────── persisted plugin state ─────────────────────
    def _row(self, pid: str) -> dict:
        row = self.e.db.one("SELECT * FROM plugin_state WHERE plugin_id=?", (pid,))
        if row is None:
            self.e.db.execute("INSERT INTO plugin_state(plugin_id,enabled,config,installed,updated) VALUES(?,?,?,?,?)",
                              (pid, 1, "{}", 1, time.time()))
            row = self.e.db.one("SELECT * FROM plugin_state WHERE plugin_id=?", (pid,))
        return row

    def is_enabled(self, pid: str) -> bool:
        return bool(self._row(pid)["enabled"])

    def is_installed(self, pid: str) -> bool:
        return bool(self._row(pid)["installed"])

    def get_config(self, pid: str) -> dict:
        try:
            return json.loads(self._row(pid)["config"])
        except ValueError:
            return {}

    def set_config(self, pid: str, values: dict) -> dict:
        ent = self._need(pid)
        schema = {f["key"]: f for f in ent.manifest.config_schema}
        cfg = self.get_config(pid)
        for k, v in values.items():
            f = schema.get(k)
            if not f:
                raise PluginError(f"Unknown setting '{k}'.")
            if f.get("secret"):
                if v not in (None, ""):          # blank = leave unchanged
                    self.e.secrets.set(pid, k, str(v))
                continue
            cfg[k] = _coerce(k, {"type": {"bool": "boolean", "int": "integer", "number": "number",
                                           "text": "string"}.get(f.get("type", "string"), f.get("type", "string"))}, v) \
                if v not in (None, "") else f.get("default")
        self.e.db.execute("UPDATE plugin_state SET config=?, updated=? WHERE plugin_id=?",
                          (json.dumps(cfg), time.time(), pid))
        self.e.bus.publish("plugin.config_changed", {"plugin": pid})
        return self.config_view(pid)

    def config_view(self, pid: str) -> list[dict]:
        ent = self._need(pid)
        cfg = self.get_config(pid)
        out = []
        for f in ent.manifest.config_schema:
            item = dict(f)
            if f.get("secret"):
                item["value"] = ""
                item["is_set"] = self.e.secrets.has(pid, f["key"])
            else:
                item["value"] = cfg.get(f["key"], f.get("default"))
            out.append(item)
        return out

    def _need(self, pid: str) -> Entry:
        ent = self.entries.get(pid)
        if not ent or not ent.manifest:
            raise PluginError(f"No such plugin '{pid}'.")
        return ent

    # ─────────────────────────────── lifecycle ───────────────────────────
    def _check_deps(self, ent: Entry) -> None:
        m = ent.manifest
        ent.missing = [mod for mod in m.requires_python if importlib.util.find_spec(mod.split(".")[0]) is None]
        if m.platforms and sys.platform not in m.platforms:
            ent.state = "error"
            ent.error = f"Not supported on this platform ({sys.platform}); needs {', '.join(m.platforms)}."

    def load_all(self) -> None:
        """validate → register → initialise → activate, in dependency order."""
        order = self._dependency_order()
        for pid in order:
            ent = self.entries[pid]
            if ent.state in ("error",) or not ent.manifest:
                continue
            self.e.permissions.register_plugin(pid, ent.manifest.permissions)
            ent.state = "registered"
            if not self.is_installed(pid):
                ent.state = "unloaded"
                continue
            if not self.is_enabled(pid):
                ent.state = "disabled"
                continue
            self.activate(pid)

    def _dependency_order(self) -> list[str]:
        ids = [p for p, e in self.entries.items() if e.manifest]
        done: list[str] = []
        visiting: set[str] = set()

        def visit(pid: str):
            if pid in done:
                return
            if pid in visiting:
                self.entries[pid].state, self.entries[pid].error = "error", "circular plugin dependency"
                return
            visiting.add(pid)
            for dep in self.entries[pid].manifest.requires_plugins:
                if dep in self.entries and self.entries[dep].manifest:
                    visit(dep)
            visiting.discard(pid)
            done.append(pid)
        for p in ids:
            visit(p)
        return done

    def activate(self, pid: str) -> bool:
        ent = self._need(pid)
        with self._lock:
            self._check_deps(ent)
            if ent.state == "error":
                return False
            if ent.missing:
                ent.state = "error"
                ent.error = "Missing Python package(s): " + ", ".join(ent.missing) + \
                            ". Install with: pip install " + " ".join(ent.missing)
                return False
            for dep in ent.manifest.requires_plugins:
                d = self.entries.get(dep)
                if not d or d.state != "active":
                    ent.state, ent.error = "error", f"requires plugin '{dep}' which is not active"
                    return False
            try:
                if ent.ctx is None:
                    ent.ctx = PluginContext(self.e, pid, ent.manifest)
                    ent.ctx.player = self.e.player
                    ent.plugin.initialize(ent.ctx)
                    ent.state = "initialised"
                    self._migrate_if_needed(ent)
                ent.plugin.activate()
                ent.state, ent.error = "active", ""
                self._subscribe(ent)
                self.e.bus.publish("plugin.activated", {"plugin": pid})
                log.info("plugin %s active", pid)
                return True
            except Exception as e:  # noqa: BLE001
                ent.state, ent.error = "error", f"activation failed: {e}"
                log.error("activate %s failed: %s\n%s", pid, e, traceback.format_exc())
                return False

    def _migrate_if_needed(self, ent: Entry) -> None:
        old = self.e.db.kv_get("core", f"plugin_version:{ent.plugin_id}")
        if old and old != ent.manifest.version:
            ent.plugin.migrate(old)
        self.e.db.kv_set("core", f"plugin_version:{ent.plugin_id}", ent.manifest.version)

    def _subscribe(self, ent: Entry) -> None:
        self._unsubscribe(ent)
        for pat in ent.manifest.subscriptions:
            ent.unsubscribers.append(self.e.bus.subscribe(pat, self._safe_event(ent), owner=ent.plugin_id))

    def _unsubscribe(self, ent: Entry) -> None:
        for u in ent.unsubscribers:
            u()
        ent.unsubscribers.clear()

    @staticmethod
    def _safe_event(ent: Entry):
        def h(topic, payload):
            if ent.state == "active":
                ent.plugin.on_event(topic, payload)
        return h

    def suspend(self, pid: str) -> None:
        ent = self._need(pid)
        if ent.state == "active":
            try:
                ent.plugin.suspend()
            finally:
                ent.state = "suspended"

    def resume(self, pid: str) -> None:
        ent = self._need(pid)
        if ent.state == "suspended":
            ent.plugin.resume()
            ent.state = "active"

    def _stop(self, ent: Entry, final: str) -> None:
        self._unsubscribe(ent)
        if ent.state in ("active", "suspended"):
            try:
                ent.plugin.deactivate()
            except Exception as e:  # noqa: BLE001
                log.warning("deactivate %s: %s", ent.plugin_id, e)
        if final == "unloaded" and ent.ctx:
            try:
                ent.plugin.unload()
            except Exception as e:  # noqa: BLE001
                log.warning("unload %s: %s", ent.plugin_id, e)
            ent.ctx = None
        ent.state = final

    def enable(self, pid: str) -> bool:
        self._need(pid)
        self.e.db.execute("UPDATE plugin_state SET enabled=1 WHERE plugin_id=?", (self._row(pid)["plugin_id"],))
        self.e.permissions.register_plugin(pid, self.entries[pid].manifest.permissions)
        return self.activate(pid) if self.is_installed(pid) else False

    def disable(self, pid: str) -> None:
        ent = self._need(pid)
        self._row(pid)
        self.e.db.execute("UPDATE plugin_state SET enabled=0 WHERE plugin_id=?", (pid,))
        self._stop(ent, "disabled")

    def uninstall(self, pid: str) -> None:
        ent = self._need(pid)
        if ent.manifest.category == "AI" and pid == "ai_providers":
            raise PluginError("The AI provider plugin is required by the assistant and cannot be uninstalled.")
        self._row(pid)
        self.e.db.execute("UPDATE plugin_state SET installed=0 WHERE plugin_id=?", (pid,))
        self._stop(ent, "unloaded")

    def install(self, pid: str) -> bool:
        self._need(pid)
        self._row(pid)
        self.e.db.execute("UPDATE plugin_state SET installed=1, enabled=1 WHERE plugin_id=?", (pid,))
        self.e.permissions.register_plugin(pid, self.entries[pid].manifest.permissions)
        return self.activate(pid)

    def update(self, pid: str) -> str:
        """Re-initialise and run pending data migrations. (Plugins ship with the app; there is
        no remote plugin store, so 'update' means applying the installed version cleanly.)"""
        ent = self._need(pid)
        was_active = ent.state == "active"
        self._stop(ent, "unloaded")
        ok = self.activate(pid) if (was_active or self.is_enabled(pid)) else True
        return f"{ent.manifest.name} v{ent.manifest.version} re-initialised" + ("" if ok else f" — but failed: {ent.error}")

    def shutdown(self) -> None:
        for ent in self.entries.values():
            if ent.state in ("active", "suspended"):
                self._stop(ent, "unloaded")

    # ───────────────────────────────── health ────────────────────────────
    def health(self, pid: str) -> Health:
        ent = self._need(pid)
        if ent.state == "error":
            return Health("error", ent.error)
        if ent.state in ("disabled", "unloaded"):
            return Health("ok", ent.state)
        try:
            h = ent.plugin.health()
        except Exception as e:  # noqa: BLE001
            return Health("error", f"health check crashed: {e}")
        if h.status == "ok" and ent.consecutive_errors >= 5:
            return Health("degraded", f"{ent.consecutive_errors} consecutive command failures: {ent.last_error}")
        return h

    # ───────────────────────────── introspection ─────────────────────────
    def info(self, pid: str) -> dict:
        ent = self.entries[pid]
        m = ent.manifest
        if not m:
            return {"id": pid, "name": pid, "state": ent.state, "error": ent.error, "installed": False,
                    "enabled": False, "category": "", "description": ent.error, "source": ent.source,
                    "file": ent.file, "health": {"status": "error", "detail": ent.error}}
        last = self.e.db.one("SELECT action,status,started,finished,result,error FROM activity WHERE plugin=? "
                             "ORDER BY id DESC LIMIT 1", (pid,))
        h = self.health(pid)
        return {
            "id": pid, "name": m.name, "description": m.description, "category": m.category,
            "version": m.version, "author": m.author, "source": ent.source, "state": ent.state,
            "installed": self.is_installed(pid), "enabled": self.is_enabled(pid),
            "health": {"status": h.status, "detail": h.detail}, "error": ent.error,
            "permissions": self.e.permissions.for_plugin(pid),
            "requires_python": m.requires_python, "optional_python": m.optional_python,
            "requires_plugins": m.requires_plugins, "missing": ent.missing,
            "subscriptions": m.subscriptions, "background": m.background, "setup_help": m.setup_help,
            "commands": [{"name": n, "description": c.description, "params": c.params,
                          "confirm": bool(c.confirm), "destructive": c.destructive, "readonly": c.readonly}
                         for n, c in ent.commands.items()],
            "config": self.config_view(pid), "last_execution": last, "last_error": ent.last_error,
        }

    def list(self) -> list[dict]:
        return [self.info(p) for p in sorted(self.entries, key=lambda x: (self.entries[x].manifest.category
                if self.entries[x].manifest else "~", x))]

    def catalog(self, plugin_ids: Optional[list[str]] = None) -> list[dict]:
        """Compact tool catalog of everything callable right now (for the orchestrator)."""
        out = []
        for pid, ent in self.entries.items():
            if ent.state != "active" or (plugin_ids and pid not in plugin_ids):
                continue
            for n, c in ent.commands.items():
                out.append({"plugin": pid, "command": n, "description": c.description,
                            "params": {k: {kk: vv for kk, vv in v.items() if kk in ("type", "description", "required", "enum")}
                                       for k, v in c.params.items()},
                            "confirm": bool(c.confirm), "readonly": c.readonly})
        return out

    def ticks(self) -> list[tuple[str, str, float, Any]]:
        bg = self.e.settings.get("startup.background_plugins", [])
        out = []
        for pid, ent in self.entries.items():
            if ent.state != "active":
                continue
            if ent.manifest.background and bg and pid not in bg:
                continue
            for name, secs, fn in ent.plugin.ticks():
                out.append((pid, name, secs, fn))
        return out

    # ───────────────────────────────── execution ─────────────────────────
    def cancel_requested(self) -> bool:
        ev = getattr(self._tls, "cancel", None)
        return bool(ev and ev.is_set())

    def execute(self, plugin_id: str, command_name: str, args: Optional[dict] = None, origin: str = "user",
                run_id: Optional[int] = None) -> CommandResult:
        t0 = time.time()
        label = f"{plugin_id}.{command_name}"
        ent = self.entries.get(plugin_id)
        if not ent or not ent.manifest:
            return self._refuse(label, plugin_id, command_name, f"No such plugin '{plugin_id}'.", origin, args, run_id)
        if ent.state != "active":
            why = {"disabled": "is disabled", "unloaded": "is not installed", "suspended": "is suspended",
                   "error": f"is unavailable ({ent.error})"}.get(ent.state, f"is not active ({ent.state})")
            return self._refuse(label, plugin_id, command_name, f"The {ent.manifest.name} plugin {why}.", origin, args, run_id)
        spec = ent.commands.get(command_name)
        if not spec:
            return self._refuse(label, plugin_id, command_name,
                                f"{ent.manifest.name} has no command '{command_name}'. "
                                f"Available: {', '.join(ent.commands)}.", origin, args, run_id)
        try:
            clean = validate_args(spec.params, args or {})
        except PluginError as e:
            return self._refuse(label, plugin_id, command_name, str(e), origin, args, run_id)
        missing = self.e.permissions.missing(plugin_id, spec.permissions)
        if missing:
            return self._refuse(label, plugin_id, command_name,
                                f"Permission not granted: {', '.join(missing)}. Grant it in Plugins → "
                                f"{ent.manifest.name} → Permissions.", origin, args, run_id)
        # confirmation gate
        summary = None
        if spec.confirm:
            try:
                summary = spec.confirm(clean) if callable(spec.confirm) else spec.confirm.format(**clean)
            except Exception:
                summary = f"{ent.manifest.name}: {command_name}"
        if summary:
            aid = self.e.activity.start(label, plugin_id, origin, clean, status="awaiting_confirmation", run_id=run_id)
            pend = self.e.confirmations.create(plugin=plugin_id, command=command_name, args=clean, summary=summary,
                                               activity_id=aid, origin=origin, run_id=run_id)
            self.e.activity.update(aid, "awaiting_confirmation", confirmation="pending")
            return CommandResult(False, f"Waiting for your confirmation: {summary}", status="awaiting_confirmation",
                                 pending_id=pend.id, activity_id=aid, plugin=plugin_id, command=command_name)
        aid = self.e.activity.start(label, plugin_id, origin, clean, run_id=run_id)
        return self._run(ent, spec, clean, aid, t0)

    def _refuse(self, label, plugin_id, cmd, msg, origin, args, run_id) -> CommandResult:
        aid = self.e.activity.start(label, plugin_id, origin, args, status="running", run_id=run_id)
        self.e.activity.update(aid, "failed", error=msg)
        return CommandResult(False, msg, status="failed", activity_id=aid, plugin=plugin_id, command=cmd)

    def approve(self, pending_id: str) -> CommandResult:
        p = self.e.confirmations.take(pending_id, "approved")
        if not p:
            return CommandResult(False, "That confirmation is no longer pending.", status="failed")
        ent = self.entries.get(p.plugin)
        if not ent or ent.state != "active":
            self.e.activity.update(p.activity_id, "failed", error="plugin no longer active", confirmation="approved")
            res = CommandResult(False, "The plugin is no longer active.", status="failed", activity_id=p.activity_id)
            self.e.confirmations.finish(p, res)
            return res
        self.e.activity.update(p.activity_id, "running", confirmation="approved")
        res = self._run(ent, ent.commands[p.command], p.args, p.activity_id, time.time())
        self.e.confirmations.finish(p, res)
        return res

    def deny(self, pending_id: str) -> CommandResult:
        p = self.e.confirmations.take(pending_id, "denied")
        if not p:
            return CommandResult(False, "That confirmation is no longer pending.", status="failed")
        self.e.activity.update(p.activity_id, "cancelled", error="denied by user", confirmation="denied")
        res = CommandResult(False, "Cancelled — nothing was done.", status="cancelled", activity_id=p.activity_id)
        self.e.confirmations.finish(p, res)
        return res

    def cancel(self, activity_id: int) -> bool:
        ev = self._running.get(activity_id)
        if ev:
            ev.set()
            return True
        row = self.e.activity.get(activity_id)
        if row and row["status"] in ("pending", "awaiting_confirmation"):
            for p in self.e.confirmations.pending():
                if p["activity_id"] == activity_id:
                    self.deny(p["id"])
                    return True
        return False

    def _run(self, ent: Entry, spec: CommandSpec, args: dict, aid: int, t0: float) -> CommandResult:
        cancel = threading.Event()
        self._running[aid] = cancel
        box: dict = {}
        plugin_id = ent.plugin_id

        def target():
            self._tls.cancel = cancel
            try:
                box["raw"] = getattr(ent.plugin, spec.func_name)(**args)
            except PluginError as e:
                box["err"] = str(e)
            except Exception as e:  # noqa: BLE001
                box["err"] = f"{type(e).__name__}: {e}"
                box["tb"] = traceback.format_exc()

        th = threading.Thread(target=target, daemon=True, name=f"gal-{plugin_id}.{spec.name}")
        th.start()
        th.join(spec.timeout)
        self._running.pop(aid, None)
        dur = time.time() - t0
        ent.last_exec = time.time()

        if th.is_alive():
            cancel.set()
            return self._finish(ent, spec, aid, dur, False, f"Timed out after {int(spec.timeout)}s.", None, "failed")
        if cancel.is_set() and "err" in box:
            return self._finish(ent, spec, aid, dur, False, "Cancelled.", None, "cancelled")
        if "err" in box:
            if "tb" in box:
                log.error("%s.%s crashed: %s\n%s", plugin_id, spec.name, box["err"], box["tb"])
            return self._finish(ent, spec, aid, dur, False, box["err"], None, "failed")
        ok, msg, data = self._normalise(box.get("raw"))
        return self._finish(ent, spec, aid, dur, ok, msg, data, "completed" if ok else "failed")

    @staticmethod
    def _normalise(raw: Any) -> tuple[bool, str, Any]:
        if isinstance(raw, CommandResult):
            return raw.ok, raw.message, raw.data
        if raw is None:
            return True, "Done.", None
        if isinstance(raw, str):
            return True, raw, None
        if isinstance(raw, dict):
            ok = raw.get("ok", True) is not False
            msg = raw.get("message") or ("Done." if ok else "Failed.")
            data = raw.get("data", {k: v for k, v in raw.items() if k not in ("ok", "message")} or None)
            return ok, str(msg), data
        return True, "Done.", raw

    def _finish(self, ent, spec, aid, dur, ok, msg, data, status) -> CommandResult:
        if ok:
            ent.consecutive_errors = 0
            self.e.activity.update(aid, "completed", result=msg)
        else:
            ent.consecutive_errors += 1 if status == "failed" else 0
            ent.last_error = msg
            self.e.activity.update(aid, status, result=None, error=msg)
            if status == "failed" and ent.consecutive_errors == 5:
                self.e.notifications.add(f"{ent.manifest.name} is having trouble", msg, "plugin", 2,
                                         ent.plugin_id, dedupe_key=f"plugin-health-{ent.plugin_id}", dedupe_window=3600)
        self.e.bus.publish("command.finished", {"plugin": ent.plugin_id, "command": spec.name, "ok": ok,
                                                "status": status})
        return CommandResult(ok, msg, data, status=status, activity_id=aid, plugin=ent.plugin_id,
                             command=spec.name, duration=dur)
