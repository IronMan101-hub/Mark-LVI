"""Start-with-Windows via the per-user Run key (no admin rights, no service, easily removed)."""
from __future__ import annotations

import sys
from pathlib import Path

from . import paths

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "GALAHAD"


def command_line(minimised: bool = True) -> str:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").exists() else py
    main = paths.project_root() / "main.py"
    target = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{pyw}" "{main}"'
    return target + (" --minimized" if minimised else "")


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            winreg.QueryValueEx(k, NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool, minimised: bool = True) -> str:
    if sys.platform != "win32":
        return "Start-with-Windows only applies on Windows; nothing was changed."
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command_line(minimised))
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
    ok = is_enabled() == enabled
    return ("GALAHAD will start with Windows." if enabled else "GALAHAD will no longer start with Windows.") if ok else "The Windows registry did not accept the change."
