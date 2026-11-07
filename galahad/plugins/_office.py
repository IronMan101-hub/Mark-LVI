"""Shared safeguards for the Word / Excel / PowerPoint plugins."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..plugin_api import PluginError
from .files import protected_path


def default_dir() -> Path:
    d = Path.home() / "Documents" / "GALAHAD"
    return d


def resolve(path: str, ext: str, must_exist: bool = False, search: Optional[list[Path]] = None) -> Path:
    raw = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
    if not raw:
        raise PluginError("Which file?")
    p = Path(raw)
    if p.suffix.lower() != ext:
        p = p.with_suffix(ext) if not p.suffix else p
    if p.suffix.lower() != ext:
        raise PluginError(f"That isn't a {ext} file.")
    if not p.is_absolute():
        for base in (search or []) + [default_dir(), Path.home() / "Documents", Path.home() / "Desktop", Path.home() / "Downloads"]:
            if (base / p).exists():
                return base / p
        p = default_dir() / p
    if reason := protected_path(p):
        raise PluginError(f"I won't use that location: {reason}.")
    if must_exist and not p.exists():
        raise PluginError(f"I can't find {p.name}. Say the full path, or look for it with the Files plugin.")
    return p


def backup(p: Path) -> Optional[Path]:
    """Copy the original aside before any in-place edit. Returns the backup path."""
    if not p.exists():
        return None
    d = p.parent / ".galahad-backup"
    d.mkdir(exist_ok=True)
    b = d / f"{p.stem}.{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}"
    shutil.copy2(p, b)
    return b


def overwrite_confirm(path_key: str = "path", ext: str = ""):
    """confirm= callable: ask only when a create/save_as would replace an existing file."""
    def fn(a: dict):
        try:
            p = resolve(a.get(path_key, ""), ext) if ext else Path(a.get(path_key, ""))
        except PluginError:
            return None
        return f"Overwrite the existing file {p}? A copy of the original will be kept in .galahad-backup." if p.exists() else None
    return fn


def open_in_app(p: Path) -> str:
    if sys.platform == "win32":
        os.startfile(str(p))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Asked the system to open {p.name}. (I can't confirm the window appeared.)"


def verify_saved(p: Path, min_bytes: int = 200) -> None:
    if not p.exists() or p.stat().st_size < min_bytes:
        raise PluginError(f"The file {p} was not saved correctly.")
