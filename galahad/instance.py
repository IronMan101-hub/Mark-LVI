"""Single-instance guard: one GALAHAD per user. A second launch pokes the first and exits,
so there are never duplicate background processes fighting over the microphone or database."""
from __future__ import annotations

import socket
import threading
from typing import Callable, Optional

PORT = 47653
_sock: Optional[socket.socket] = None


def acquire() -> bool:
    """True if we are now the only instance."""
    global _sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None
        s.bind(("127.0.0.1", PORT))
        s.listen(4)
    except OSError:
        s.close()
        return False
    _sock = s
    return True


def poke_existing(message: str = "show") -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as c:
            c.sendall(message.encode()[:32])
        return True
    except OSError:
        return False


def listen(on_message: Callable[[str], None]) -> None:
    """Handle pokes from later launches (runs on a daemon thread)."""
    def loop():
        while _sock:
            try:
                conn, _ = _sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(1)
                msg = conn.recv(32).decode(errors="ignore").strip()
                if msg:
                    on_message(msg)
            except Exception:
                pass
            finally:
                conn.close()
    threading.Thread(target=loop, daemon=True, name="gal-instance").start()


def release() -> None:
    global _sock
    if _sock:
        try:
            _sock.close()
        finally:
            _sock = None
