"""Glue between the GALAHAD engine and a host process (the MARK LVI desktop app, or the headless service)."""
from __future__ import annotations

import threading
import webbrowser
from typing import Callable, Optional

from . import logs
from .engine import Engine, get_engine

log = logs.get("runtime")
_server = None
_server_lock = threading.Lock()


def attach(player=None, speak: Optional[Callable[[str], None]] = None, start_dashboard: bool = True) -> Engine:
    """Start the engine in this process and connect it to the host's UI/voice."""
    eng = get_engine()
    eng.player = player
    for ent in eng.registry.entries.values():
        if ent.ctx is not None:
            ent.ctx.player = player
    if not getattr(eng, "_runtime_wired", False):
        eng._runtime_wired = True

        def on_deliver(_t, p):
            if player is not None:
                try:
                    player.write_log(f"SYS: {p['title']}" + (f" — {p['body']}" if p.get("body") else ""))
                except Exception:
                    pass
            if speak and p.get("speak"):
                try:
                    speak(f"Tell the user briefly, in their language: {p['title']}. {p.get('body', '')}")
                except Exception:
                    pass
            _toast(p["title"], p.get("body", ""))

        def on_say(_t, p):
            if speak:
                try:
                    speak(f"Say to the user, briefly and naturally: {p.get('text', '')}")
                except Exception:
                    pass

        def on_confirm(_t, p):
            if player is not None:
                try:
                    player.write_log(f"SYS: Confirmation needed — {p['summary'].splitlines()[0]} (Dashboard → AI Execution)")
                except Exception:
                    pass

        eng.bus.subscribe("notify.deliver", on_deliver, owner="runtime")
        eng.bus.subscribe("assistant.say", on_say, owner="runtime")
        eng.bus.subscribe("confirm.requested", on_confirm, owner="runtime")
    if start_dashboard and eng.settings.get("dashboard.enabled", True):
        start_server(eng)
    return eng


def _toast(title: str, body: str) -> None:
    """Best-effort desktop toast; failure is silent (the in-app log already has it)."""
    try:
        import sys
        if sys.platform == "win32":
            from win10toast import ToastNotifier
            threading.Thread(target=lambda: ToastNotifier().show_toast(title[:60], body[:200], duration=6, threaded=False),
                             daemon=True).start()
    except Exception:
        pass


def start_server(eng: Optional[Engine] = None):
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        eng = eng or get_engine()
        try:
            from .web.server import DashboardServer
            _server = DashboardServer(eng)
            _server.start()
            log.info("dashboard at %s", _server.url())
        except Exception as e:  # noqa: BLE001
            log.error("dashboard failed to start: %s", e)
            _server = None
        return _server


def dashboard_url(with_token: bool = True) -> Optional[str]:
    return _server.url(with_token) if _server else None


def open_dashboard() -> str:
    srv = _server or start_server()
    if not srv:
        return "The dashboard couldn't start (see logs)."
    webbrowser.open(srv.url(True))
    return srv.url(False)


def shutdown() -> None:
    global _server
    from .engine import reset_engine
    if _server:
        _server.stop()
        _server = None
    reset_engine()
