"""Computer control: apps, windows, volume, clipboard, keyboard/mouse, power, screenshots.

Where MARK LVI already has proven Windows code (volume/brightness via pycaw and
WMI) it is reused rather than rewritten. Anything that cannot be verified on
this platform says so instead of claiming success.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from .sysmon import PROTECTED

WIN = sys.platform == "win32"
APP_ALIASES = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "cmd": "cmd.exe", "command prompt": "cmd.exe",
    "powershell": "powershell.exe", "task manager": "taskmgr.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "firefox": "firefox.exe", "word": "winword.exe",
    "excel": "excel.exe", "powerpoint": "powerpnt.exe", "outlook": "outlook.exe", "spotify": "spotify.exe",
    "settings": "ms-settings:", "snipping tool": "snippingtool.exe", "control panel": "control.exe",
}
POWER_CMDS = {
    "win32": {"lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
              "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
              "restart": ["shutdown", "/r", "/t", "15"], "shutdown": ["shutdown", "/s", "/t", "15"],
              "cancel": ["shutdown", "/a"]},
    "darwin": {"lock": ["pmset", "displaysleepnow"], "sleep": ["pmset", "sleepnow"],
               "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
               "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down']},
    "linux": {"lock": ["loginctl", "lock-session"], "sleep": ["systemctl", "suspend"],
              "restart": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]},
}


def _start_menu_apps() -> dict[str, str]:
    """name(lower) -> shortcut path, from the Windows Start Menu."""
    out: dict[str, str] = {}
    if not WIN:
        return out
    roots = [Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
    for r in roots:
        if r.is_dir():
            for lnk in r.rglob("*.lnk"):
                out.setdefault(lnk.stem.lower(), str(lnk))
    return out


def _user32():
    return ctypes.windll.user32


class Computer(Plugin):
    manifest = Manifest(
        id="computer", name="Computer Control", category="System", version="1.0.0",
        description="Open/close/switch apps, manage windows, volume, brightness, clipboard, keystrokes, mouse, "
                    "lock/sleep/restart/shutdown (confirmed) and screenshots.",
        permissions=["system.control", "system.power", "system.monitor", "screen.capture", "fs.write"],
        requires_python=["psutil"], optional_python=["pyautogui", "pyperclip", "mss", "pycaw", "pygetwindow"],
        setup_help="Screenshots stay disabled until you grant 'screen.capture' and turn on Settings → Privacy → Screen capture.")

    def health(self):
        if WIN:
            return Health("ok", "Windows control available")
        return Health("degraded", f"Running on {sys.platform}: process, clipboard, power and screenshot features work where the "
                                   "OS tools exist; Windows window management is unavailable.")

    # ── applications ──────────────────────────────────────────────────
    def _resolve_app(self, name: str) -> tuple[str, list]:
        key = name.strip().lower()
        if key in APP_ALIASES:
            t = APP_ALIASES[key]
            return ("uri", [t]) if t.endswith(":") else ("exe", [t])
        menu = _start_menu_apps()
        if key in menu:
            return "lnk", [menu[key]]
        close = [n for n in menu if key in n]
        if close:
            best = min(close, key=len)
            return "lnk", [menu[best]]
        w = shutil.which(name) or shutil.which(name + (".exe" if WIN else ""))
        if w:
            return "exe", [w]
        raise PluginError(f"I couldn't find an app called '{name}'. Try 'find_app' to search what's installed.")

    def _procs_matching(self, name: str):
        import psutil
        key = name.strip().lower().removesuffix(".exe")
        exe = APP_ALIASES.get(name.strip().lower(), "").lower().removesuffix(".exe")
        out = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                nm = (p.info["name"] or "").lower().removesuffix(".exe")
                if nm in PROTECTED or not nm:
                    continue
                if nm == key or (exe and nm == exe) or (len(key) > 3 and key in nm):
                    out.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    @command(description="List installed apps matching a name (Start Menu on Windows).", readonly=True,
             permissions=("system.control",), params={"query": {"type": "string"}})
    def find_app(self, query=""):
        names = sorted(set(list(_start_menu_apps()) + list(APP_ALIASES)))
        if query:
            names = [n for n in names if query.lower() in n]
        return {"message": f"{len(names)} app(s)" + (": " + ", ".join(names[:15]) if names else "."), "data": {"apps": names[:200]}}

    @command(description="Launch an application by name, then verify it is running.",
             permissions=("system.control",), params={"name": {"type": "string", "required": True}}, timeout=25)
    def open_app(self, name):
        before = {p.pid for p in self._procs_matching(name)}
        kind, target = self._resolve_app(name)
        try:
            if WIN:
                os.startfile(target[0])  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", name] if kind != "exe" else [target[0]])
            else:
                subprocess.Popen(target, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise PluginError(f"Couldn't start {name}: {e}")
        if kind == "uri":
            return {"message": f"Asked Windows to open {name}. (Can't verify system pages.)", "data": {}}
        for _ in range(16):
            self.ctx.check_cancel()
            time.sleep(0.5)
            now = {p.pid for p in self._procs_matching(name)}
            if now - before or (now and not before):
                return {"message": f"{name} is running.", "data": {"pids": sorted(now)}}
        return {"ok": False, "message": f"I launched {name} but couldn't see it running after 8 seconds."}

    @staticmethod
    def _force_confirm(a):
        return f"Force-quit {a['name']}? Unsaved work will be lost." if a.get("force") else None

    @command(description="Close an application politely (it may ask to save). force=true kills it and asks you first.",
             permissions=("system.control",), confirm=_force_confirm.__func__, timeout=30,
             params={"name": {"type": "string", "required": True}, "force": {"type": "boolean", "default": False}})
    def close_app(self, name, force=False):
        import psutil
        procs = self._procs_matching(name)
        if not procs:
            raise PluginError(f"'{name}' doesn't seem to be running (or it's a protected process).")
        for p in procs:
            try:
                p.kill() if force else p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        gone, alive = psutil.wait_procs(procs, timeout=8)
        if alive:
            return {"ok": False, "message": f"{name} is still running — it may be asking to save. Use force to kill it."}
        return {"message": f"Closed {name} ({len(gone)} process{'es' if len(gone) != 1 else ''})."}

    # ── windows (Windows only) ────────────────────────────────────────
    def _windows(self) -> list[dict]:
        if not WIN:
            raise PluginError("Window management needs Windows.")
        u = _user32()
        out = []
        cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    out.append({"hwnd": hwnd, "title": buf.value})
            return True
        u.EnumWindows(cb_t(cb), 0)
        return [w for w in out if w["title"] not in ("Program Manager", "Windows Input Experience")]

    @command(description="List open windows.", readonly=True, permissions=("system.control",))
    def list_windows(self):
        w = self._windows()
        return {"message": f"{len(w)} window(s): " + "; ".join(x["title"][:40] for x in w[:8]),
                "data": {"windows": [{"title": x["title"]} for x in w]}}

    @command(description="Act on a window found by (part of) its title: focus, minimize, maximize, restore or close.",
             permissions=("system.control",),
             params={"title": {"type": "string", "required": True},
                     "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "close"], "default": "focus"}})
    def window(self, title, action="focus"):
        match = [w for w in self._windows() if title.lower() in w["title"].lower()]
        if not match:
            raise PluginError(f"No open window with '{title}' in its title.")
        u, hwnd = _user32(), match[0]["hwnd"]
        codes = {"minimize": 6, "maximize": 3, "restore": 9}
        if action == "focus":
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
            ok = u.GetForegroundWindow() == hwnd
        elif action == "close":
            u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE — the app may prompt to save
            time.sleep(0.6)
            ok = not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd)
        else:
            u.ShowWindow(hwnd, codes[action])
            time.sleep(0.2)
            ok = {"minimize": bool(u.IsIconic(hwnd)), "maximize": bool(u.IsZoomed(hwnd)),
                  "restore": not u.IsIconic(hwnd)}[action]
        return {"ok": ok, "message": f"{action.title()}d '{match[0]['title'][:50]}'." if ok else
                f"I sent '{action}' to '{match[0]['title'][:50]}' but it didn't take effect (Windows may block focus changes)."}

    # ── volume / brightness ───────────────────────────────────────────
    def _cs(self):
        try:
            import actions.computer_settings as cs
            return cs
        except Exception:
            return None

    def _get_volume(self) -> Optional[int]:
        cs = self._cs()
        if WIN and cs:
            try:
                return cs.volume_get()
            except Exception:
                return None
        if shutil.which("pactl"):
            try:
                out = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=3).stdout
                return int(out.split("/")[1].strip().rstrip("%"))
            except Exception:
                return None
        return None

    @command(description="Volume control: get | up | down | set | mute | unmute.", permissions=("system.control",),
             params={"action": {"type": "string", "enum": ["get", "up", "down", "set", "mute", "unmute"], "required": True},
                     "level": {"type": "integer", "minimum": 0, "maximum": 100}})
    def volume(self, action, level=None):
        cs = self._cs()
        if action == "get":
            v = self._get_volume()
            if v is None:
                raise PluginError("I can't read the volume on this system.")
            return {"message": f"Volume is {v}%.", "data": {"level": v}}
        before = self._get_volume()
        if WIN and cs:
            if action == "set":
                if level is None:
                    raise PluginError("Say what level (0–100).")
                cs.volume_set(level)
            else:
                {"up": cs.volume_up, "down": cs.volume_down, "mute": cs.volume_mute, "unmute": cs.volume_mute}[action]()
        elif shutil.which("pactl"):
            sink = "@DEFAULT_SINK@"
            args = {"up": ["set-sink-volume", sink, "+10%"], "down": ["set-sink-volume", sink, "-10%"],
                    "set": ["set-sink-volume", sink, f"{level}%"], "mute": ["set-sink-mute", sink, "1"],
                    "unmute": ["set-sink-mute", sink, "0"]}[action]
            if action == "set" and level is None:
                raise PluginError("Say what level (0–100).")
            subprocess.run(["pactl", *args], check=False, timeout=5)
        else:
            raise PluginError("No supported audio control found on this system.")
        time.sleep(0.2)
        after = self._get_volume()
        if action in ("mute", "unmute"):
            return f"Sent {action}. (Mute state can't be read back reliably.)"
        if after is None:
            return {"message": f"Sent '{action}', but I can't read the volume back to confirm.", "data": {}}
        changed = after != before if action in ("up", "down") else abs(after - (level or 0)) <= 2
        return {"ok": changed or (after in (0, 100)), "message": f"Volume is now {after}%." if changed else
                f"Volume is {after}% — it didn't change as expected.", "data": {"level": after}}

    @command(description="Get or set screen brightness (laptop/internal displays).", permissions=("system.control",),
             params={"action": {"type": "string", "enum": ["get", "set", "up", "down"], "required": True},
                     "level": {"type": "integer", "minimum": 0, "maximum": 100}})
    def brightness(self, action, level=None):
        cs = self._cs()
        if not (WIN and cs):
            raise PluginError("Brightness control is only available on Windows.")
        cur = cs.brightness_get()
        if action == "get":
            if cur is None:
                raise PluginError("This display doesn't report brightness (external monitors often can't).")
            return {"message": f"Brightness is {cur}%.", "data": {"level": cur}}
        target = level if action == "set" else (min(100, (cur or 50) + 10) if action == "up" else max(0, (cur or 50) - 10))
        if target is None:
            raise PluginError("Say what level (0–100).")
        cs.brightness_set(int(target))
        time.sleep(0.4)
        new = cs.brightness_get()
        ok = new is not None and abs(new - target) <= 5
        return {"ok": ok, "message": f"Brightness is now {new}%." if ok else "I asked for the change but the display didn't confirm it."}

    # ── clipboard / keyboard / mouse ──────────────────────────────────
    def _clip(self):
        try:
            import pyperclip
            return pyperclip
        except ImportError:
            raise PluginError("Clipboard access needs the 'pyperclip' package.")

    @command(description="Clipboard: read | write | clear.", permissions=("system.control",),
             params={"action": {"type": "string", "enum": ["read", "write", "clear"], "required": True},
                     "text": {"type": "string"}})
    def clipboard(self, action, text=""):
        c = self._clip()
        try:
            if action == "read":
                v = c.paste()
                return {"message": f"Clipboard: {v[:300]}" if v else "The clipboard is empty.", "data": {"text": v}}
            c.copy("" if action == "clear" else text)
            ok = c.paste() == ("" if action == "clear" else text)
            return {"ok": ok, "message": "Clipboard updated." if ok else "I wrote to the clipboard but could not read it back."}
        except c.PyperclipException as e:
            raise PluginError(f"The system clipboard isn't available: {e}")

    def _gui(self):
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            return pyautogui
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"Keyboard/mouse control isn't available here ({type(e).__name__}). It needs a desktop session and pyautogui.")

    @command(description="Press a keyboard shortcut, e.g. keys='ctrl+shift+t'.", permissions=("system.control",),
             params={"keys": {"type": "string", "required": True}})
    def hotkey(self, keys):
        parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
        if not parts or len(parts) > 4:
            raise PluginError("Give 1–4 keys joined with +.")
        if set(parts) >= {"ctrl", "alt", "delete"}:
            raise PluginError("I won't send Ctrl+Alt+Delete.")
        self._gui().hotkey(*parts)
        return f"Pressed {'+'.join(parts)}. (Can't verify what the active app did with it.)"

    @command(description="Type text into the active window.", permissions=("system.control",),
             params={"text": {"type": "string", "required": True, "max_length": 2000}})
    def type_text(self, text):
        self._gui().write(text, interval=0.01)
        return f"Typed {len(text)} characters into the active window."

    @command(description="Move the mouse and optionally click at screen coordinates.", permissions=("system.control",),
             params={"x": {"type": "integer", "required": True, "minimum": 0}, "y": {"type": "integer", "required": True, "minimum": 0},
                     "click": {"type": "string", "enum": ["none", "left", "right", "double"], "default": "none"}})
    def mouse(self, x, y, click="none"):
        g = self._gui()
        w, h = g.size()
        if x >= w or y >= h:
            raise PluginError(f"That's off screen (screen is {w}×{h}).")
        g.moveTo(x, y, duration=0.2)
        if click == "double":
            g.doubleClick()
        elif click != "none":
            g.click(button=click)
        pos = g.position()
        return {"ok": abs(pos.x - x) < 3 and abs(pos.y - y) < 3, "message": f"Mouse at ({pos.x}, {pos.y})" + (f", {click} click." if click != "none" else ".")}

    # ── power ─────────────────────────────────────────────────────────
    @command(description="Power: lock | sleep | restart | shutdown | cancel. Everything except lock asks for confirmation first.",
             permissions=("system.power",), destructive=True,
             confirm=lambda a: None if a["action"] in ("lock", "cancel") else f"{a['action'].title()} the computer?",
             params={"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown", "cancel"], "required": True}})
    def power(self, action):
        cmds = POWER_CMDS.get("win32" if WIN else "darwin" if sys.platform == "darwin" else "linux", {})
        cmd = cmds.get(action)
        if not cmd:
            raise PluginError(f"'{action}' isn't supported on this system.")
        if not shutil.which(cmd[0]):
            raise PluginError(f"This system lacks '{cmd[0]}', so I can't {action}.")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "message": f"The system refused to {action}: {(r.stderr or r.stdout).strip()[:150]}"}
        extra = " Use 'cancel' within 15 seconds to stop it." if action in ("restart", "shutdown") and WIN else ""
        return f"{action.title()} command accepted by the system.{extra}"

    # ── screenshot ────────────────────────────────────────────────────
    def screenshot_to(self, path: Path) -> Path:
        if not self.ctx.setting("privacy.screen_capture", False):
            raise PluginError("Screen capture is switched off. Enable Settings → Privacy → Screen capture.")
        try:
            import mss
            import mss.tools
        except ImportError:
            raise PluginError("Screenshots need the 'mss' package.")
        try:
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[0])
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"The screen couldn't be captured ({type(e).__name__}: {e}). A desktop session is required.")
        if not path.exists() or path.stat().st_size < 100:
            raise PluginError("The screenshot file wasn't created.")
        return path

    @command(description="Capture the screen to a PNG file (requires screen-capture permission and setting).",
             permissions=("screen.capture", "fs.write"), params={"folder": {"type": "string"}})
    def screenshot(self, folder=""):
        from .. import paths
        d = Path(folder).expanduser() if folder else paths.home() / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        p = self.screenshot_to(d / f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
        return {"message": f"Screenshot saved to {p} ({p.stat().st_size // 1024} KB).", "data": {"path": str(p)}}


PLUGINS = [Computer]
