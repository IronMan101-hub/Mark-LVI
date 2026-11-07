"""Local, rotating, secret-scrubbing log."""
from __future__ import annotations

import logging
import logging.handlers
import re

from . import paths

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)([\"'\s:=]+)([A-Za-z0-9_\-\.\/+=]{8,})")
_KEY_SHAPES = re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}|ya29\.[0-9A-Za-z_\-]{20,})\b")


def scrub(text: str) -> str:
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", str(text))
    return _KEY_SHAPES.sub("***", text)


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


_configured = False


def setup(level: int = logging.INFO) -> logging.Logger:
    global _configured
    root = logging.getLogger("galahad")
    if _configured:
        return root
    root.setLevel(level)
    try:
        h = logging.handlers.RotatingFileHandler(
            paths.log_dir() / "galahad.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.addFilter(_ScrubFilter())
        root.addHandler(h)
    except OSError:
        pass
    _configured = True
    return root


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"galahad.{name}")
