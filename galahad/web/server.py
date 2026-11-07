"""Local dashboard server: REST + Server-Sent Events + the single-page UI.

Security model
  * Binds to 127.0.0.1 only. Nothing is reachable from the network.
  * A random token (stored in the SecretStore) is required for every /api call, delivered as an
    HttpOnly SameSite=Strict cookie after the app opens http://127.0.0.1:PORT/?token=… once.
  * The Host header must be a loopback name (blocks DNS-rebinding) and state-changing requests
    must carry `X-Galahad: 1` (blocks cross-site form posts).
  * Every action goes through the same registry pipeline as voice: permissions, confirmations,
    activity log. The dashboard cannot approve anything the voice assistant couldn't be asked to do.
"""

import asyncio
import json
import queue
import secrets as pysecrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .. import VERSION, logs
from ..engine import Engine
from ..plugin_api import PluginError
from ..settings import DEFAULTS

log = logs.get("web")
STATIC = Path(__file__).with_name("static")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def create_app(eng: Engine, token: str):
    from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

    app = FastAPI(title="GALAHAD Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    cache: dict[str, tuple[float, Any]] = {}

    def cached(key: str, ttl: float, fn):
        hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        cache[key] = (time.time(), val)
        return val

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0] if not (request.headers.get("host") or "").startswith("[") \
            else (request.headers.get("host") or "").split("]")[0] + "]"
        if host not in LOOPBACK_HOSTS:
            return JSONResponse({"error": "bad host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            ok = pysecrets.compare_digest(request.cookies.get("galahad_token", ""), token) or \
                pysecrets.compare_digest((request.headers.get("authorization") or "").removeprefix("Bearer ").strip(), token)
            if not ok:
                return JSONResponse({"error": "unauthorised"}, status_code=401)
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-galahad") != "1":
                return JSONResponse({"error": "missing X-Galahad header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
                                                    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    def run_sync(fn, *a):
        return asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a))

    def ex(plugin, command, args=None, origin="dashboard"):
        return eng.registry.execute(plugin, command, args or {}, origin=origin)

    # ───────────────────────────── pages ─────────────────────────────
    @app.get("/")
    async def index(request: Request):
        q = request.query_params.get("token")
        if q and pysecrets.compare_digest(q, token):
            r = Response(status_code=303, headers={"Location": "/"})
            r.set_cookie("galahad_token", token, httponly=True, samesite="strict", max_age=86400 * 30)
            return r
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def static(name: str):
        f = (STATIC / name).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    # ───────────────────────────── status ────────────────────────────
    @app.get("/api/status")
    async def status():
        st = eng.status()
        prov = eng.settings.get("ai.provider")
        st["ai"] = {"provider": prov, "model": eng.ai.provider_config(prov)["model"] if prov in __import__("galahad.ai", fromlist=["PROVIDERS"]).PROVIDERS else "",
                    "configured": eng.ai.available()}
        st["voice"] = voice_state()
        st["active_runs"] = [r for r in eng.workflows.runs(10) if r["status"] in ("running", "awaiting_confirmation")]
        st["settings"] = {k: eng.settings.get(k) for k in ("appearance.theme", "appearance.accent", "appearance.background", "appearance.transparency",
                                                           "appearance.animations", "general.app_name", "appearance.layout", "general.date_format", "general.time_format")}
        return st

    def voice_state() -> dict:
        p = eng.player
        out = {"available": p is not None, "mode": eng.settings.get("voice.listening_mode"), "wake_word": eng.settings.get("voice.wake_word"),
               "enabled": eng.settings.get("startup.voice_service"), "muted": None, "awake": None, "speaking": None}
        if p is not None:
            try:
                out["muted"] = bool(p.muted)
            except Exception:
                pass
            try:
                ws = p.wake_get_state()
                out["awake"], out["wake_enabled"] = ws.get("awake"), ws.get("enabled")
            except Exception:
                pass
        return out

    # ───────────────────────────── home ──────────────────────────────
    @app.get("/api/home")
    async def home():
        def build():
            now = time.time()
            sys_r = ex("sysmon", "snapshot", origin="dashboard:home")
            tasks = eng.db.query("SELECT * FROM tasks WHERE status='pending' ORDER BY COALESCE(due,9e18), priority DESC LIMIT 6")
            rems = eng.db.query("SELECT * FROM reminders WHERE status='active' ORDER BY due LIMIT 5")
            events = eng.db.query("SELECT * FROM events_local WHERE start>? ORDER BY start LIMIT 4", (now - 1800,))
            heads = eng.db.query("SELECT id,title,publisher,category,published,url FROM articles ORDER BY COALESCE(published,fetched) DESC LIMIT 6")
            return {"system": sys_r.data if sys_r.ok else None, "tasks": tasks, "reminders": rems, "events": events, "headlines": heads,
                    "notifications": eng.notifications.list(unread_only=True, limit=5), "unread": eng.notifications.unread_count()}
        return await run_sync(build)

    @app.get("/api/weather")
    async def weather(city: str = ""):
        def go():
            r = ex("utilities", "weather", {"city": city}, "dashboard:home")
            return {"ok": r.ok, "message": r.message, "data": r.data}
        return await run_sync(lambda: cached(f"weather:{city}:{eng.settings.get('location.city')}", 600, go))

    # ───────────────────────────── commands ──────────────────────────
    @app.post("/api/command")
    async def command(body: dict = Body(...)):
        r = await run_sync(lambda: ex(str(body.get("plugin", "")), str(body.get("command", "")), body.get("args") or {}))
        return r.to_dict()

    @app.post("/api/chat")
    async def chat(body: dict = Body(...)):
        msg = str(body.get("message", "")).strip()
        if not msg:
            raise HTTPException(400, "empty message")
        out = await run_sync(lambda: eng.orchestrator.handle(msg, origin="dashboard", wait=20))
        return out

    @app.get("/api/chat/history")
    async def chat_history(limit: int = 60):
        return {"messages": eng.memory.recent_messages(limit), "session": eng.memory.session}

    @app.get("/api/search")
    async def search(q: str, scopes: str = ""):
        r = await run_sync(lambda: ex("search", "search", {"query": q, "scopes": [s for s in scopes.split(",") if s] or None}, "dashboard:search"))
        return r.to_dict()

    # ───────────────────────────── plugins ───────────────────────────
    @app.get("/api/plugins")
    async def plugins():
        return {"plugins": eng.registry.list(), "categories": __import__("galahad.plugin_api", fromlist=["CATEGORIES"]).CATEGORIES,
                "permission_catalog": {k: {"description": v[0], "sensitive": v[1]} for k, v in __import__("galahad.permissions", fromlist=["CATALOG"]).CATALOG.items()}}

    @app.post("/api/plugins/{pid}/{action}")
    async def plugin_action(pid: str, action: str, body: dict = Body(default={})):
        reg = eng.registry
        def go():
            try:
                if action == "enable": ok = reg.enable(pid); return {"ok": ok, "message": "Enabled." if ok else reg.entries[pid].error or "Could not activate."}
                if action == "disable": reg.disable(pid); return {"ok": True, "message": "Disabled."}
                if action == "install": ok = reg.install(pid); return {"ok": ok, "message": "Installed." if ok else reg.entries[pid].error}
                if action == "uninstall": reg.uninstall(pid); return {"ok": True, "message": "Uninstalled (your data is kept)."}
                if action == "update": return {"ok": True, "message": reg.update(pid)}
                if action == "suspend": reg.suspend(pid); return {"ok": True, "message": "Suspended."}
                if action == "resume": reg.resume(pid); return {"ok": True, "message": "Resumed."}
                if action == "test":
                    ent = reg._need(pid)
                    if ent.state != "active":
                        return {"ok": False, "message": f"Plugin is {ent.state}. {ent.error}"}
                    ok, detail = ent.plugin.self_test()
                    return {"ok": ok, "message": detail or ("Self-test passed." if ok else "Self-test failed.")}
                if action == "config": return {"ok": True, "config": reg.set_config(pid, body.get("values") or {})}
                if action == "permission":
                    eng.permissions.set(pid, str(body.get("permission")), bool(body.get("granted")))
                    return {"ok": True, "permissions": eng.permissions.for_plugin(pid)}
                if action == "trust": reg.trust_user_plugin(pid); return {"ok": True, "message": "Trusted and loaded."}
            except PluginError as e:
                return {"ok": False, "message": str(e)}
            except KeyError as e:
                return {"ok": False, "message": f"Unknown: {e}"}
            raise HTTPException(404, "unknown action")
        out = await run_sync(go)
        out["plugin"] = reg.info(pid) if pid in reg.entries and reg.entries[pid].manifest else None
        return out

    # ───────────────────── confirmations / activity ──────────────────
    @app.get("/api/confirmations")
    async def confirmations():
        return {"pending": eng.confirmations.pending()}

    @app.post("/api/confirmations/{pid}/{decision}")
    async def decide(pid: str, decision: str):
        if decision not in ("approve", "deny"):
            raise HTTPException(404)
        r = await run_sync(lambda: eng.registry.approve(pid) if decision == "approve" else eng.registry.deny(pid))
        return r.to_dict()

    @app.get("/api/activity")
    async def activity(limit: int = 100, plugin: str = "", status: str = ""):
        return {"activity": eng.activity.recent(min(limit, 500), plugin, status), "active": eng.activity.active()}

    @app.post("/api/activity/{aid}/cancel")
    async def cancel(aid: int):
        return {"ok": eng.registry.cancel(aid)}

    # ───────────────────────── notifications ─────────────────────────
    @app.get("/api/notifications")
    async def notifications(category: str = "", unread: bool = False, limit: int = 100, dismissed: bool = False):
        return {"notifications": eng.notifications.list(category, unread, dismissed, limit), "unread": eng.notifications.unread_count()}

    @app.post("/api/notifications/{action}")
    async def notif_action(action: str, body: dict = Body(default={})):
        nid = body.get("id")
        if action == "read":
            n = eng.notifications.mark_read(nid)
        elif action == "dismiss":
            n = eng.notifications.dismiss(nid)
        else:
            raise HTTPException(404)
        return {"changed": n, "unread": eng.notifications.unread_count()}

    # ───────────────────────── workflows ─────────────────────────────
    @app.get("/api/workflows")
    async def workflows():
        return {"workflows": eng.workflows.list(), "runs": eng.workflows.runs(30)}

    @app.get("/api/runs/{rid}")
    async def run_detail(rid: int):
        try:
            return eng.workflows.get_run(rid)
        except PluginError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/runs/{rid}/cancel")
    async def run_cancel(rid: int):
        return {"ok": eng.workflows.cancel(rid)}

    # ───────────────────────── settings / memory ─────────────────────
    @app.get("/api/settings")
    async def get_settings():
        from .. import autostart
        return {"settings": eng.settings.all(), "secret_backend": eng.secrets.backend_name(), "autostart": autostart.is_enabled(),
                "ai": eng.ai.status(), "paths": {"data": str(eng.db.path.parent), "database": str(eng.db.path)}}

    @app.post("/api/settings")
    async def set_setting(body: dict = Body(...)):
        try:
            v = eng.settings.set(str(body.get("key")), body.get("value"))
        except (KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
        msg = ""
        if body.get("key") == "startup.start_with_windows":
            from .. import autostart
            msg = autostart.set_enabled(bool(v), bool(eng.settings.get("startup.start_minimised")))
        if body.get("key") == "startup.start_minimised" and eng.settings.get("startup.start_with_windows"):
            from .. import autostart
            msg = autostart.set_enabled(True, bool(v))
        return {"ok": True, "value": v, "message": msg}

    @app.get("/api/memory")
    async def memory():
        return eng.memory.inspect()

    @app.post("/api/memory/remember")
    async def remember(body: dict = Body(...)):
        try:
            return {"ok": True, "memory": eng.memory.remember(str(body.get("key", "")), str(body.get("value", "")), str(body.get("category", "notes")))}
        except ValueError as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

    @app.delete("/api/memory/{key}")
    async def forget(key: str):
        return {"ok": eng.memory.forget(key)}

    @app.post("/api/memory/clear")
    async def clear_memory(body: dict = Body(...)):
        which = body.get("which", "")
        if which not in ("short_term", "long_term", "operational", "all"):
            raise HTTPException(400, "which must be short_term, long_term, operational or all")
        return {"ok": True, "deleted": eng.memory.delete_all(which)}

    @app.get("/api/memory/export")
    async def export_memory():
        return Response(eng.memory.export(), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=galahad-memory.json"})

    # ───────────────────────── media / system ────────────────────────
    @app.get("/api/media")
    async def media():
        def go():
            out = {}
            for pid in ("spotify", "youtube"):
                ent = eng.registry.entries.get(pid)
                if not ent or ent.state != "active":
                    out[pid] = {"ok": False, "message": "plugin inactive"}
                    continue
                r = ex(pid, "now_playing", {}, "dashboard:media")
                out[pid] = {"ok": r.ok, "message": r.message, "data": r.data, "health": eng.registry.health(pid).__dict__}
            return out
        return await run_sync(lambda: cached("media", 3, go))

    @app.get("/api/system")
    async def system(points: int = 60):
        def go():
            snap = ex("sysmon", "snapshot", origin="dashboard:system")
            hist = ex("sysmon", "history_points", {"points": points}, "dashboard:system")
            return {"snapshot": snap.data if snap.ok else None, "history": (hist.data or {}).get("samples", []), "message": snap.message}
        return await run_sync(go)

    @app.get("/api/finance")
    async def finance():
        def go():
            if not eng.permissions.is_granted("finance", "finance.data"):
                return {"ok": False, "needs_permission": True, "message": "Grant the 'finance.data' permission in Plugins → Finance to view your records."}
            r = ex("finance", "dashboard", {}, "dashboard:finance")
            return {"ok": r.ok, "message": r.message, "data": r.data}
        return await run_sync(go)

    # ───────────────────────── events (SSE) ──────────────────────────
    @app.get("/api/stream")
    async def stream(request: Request):
        q: "queue.Queue[dict]" = queue.Queue(maxsize=200)

        def push(topic, payload):
            if topic.startswith(("activity.", "notify.", "confirm.", "workflow.", "plugin.", "settings.", "command.")):
                try:
                    q.put_nowait({"topic": topic, "payload": payload})
                except queue.Full:
                    pass
        unsub = eng.bus.subscribe("*", push, owner="sse")

        async def gen():
            try:
                yield "retry: 3000\n\n"
                last = time.time()
                while not await request.is_disconnected():
                    try:
                        ev = q.get_nowait()
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                        last = time.time()
                    except queue.Empty:
                        await asyncio.sleep(0.4)
                        if time.time() - last > 20:
                            yield ": keepalive\n\n"
                            last = time.time()
            finally:
                unsub()
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


class DashboardServer:
    def __init__(self, eng: Engine, port: Optional[int] = None):
        self.eng = eng
        self.port = port or int(eng.settings.get("dashboard.port", 8765))
        tok = eng.secrets.get("dashboard", "token")
        if not tok:
            tok = pysecrets.token_urlsafe(32)
            eng.secrets.set("dashboard", "token", tok)
        self.token = tok
        self._server = None
        self._thread: Optional[threading.Thread] = None

    def _free_port(self) -> int:
        for p in [self.port] + list(range(self.port + 1, self.port + 20)):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                    return p
                except OSError:
                    continue
        raise RuntimeError("no free port for the dashboard")

    def start(self) -> None:
        import uvicorn
        self.port = self._free_port()
        cfg = uvicorn.Config(create_app(self.eng, self.token), host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="gal-dashboard")
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("dashboard did not start")

    def url(self, with_token: bool = False) -> str:
        return f"http://127.0.0.1:{self.port}/" + (f"?token={self.token}" if with_token else "")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=3)
