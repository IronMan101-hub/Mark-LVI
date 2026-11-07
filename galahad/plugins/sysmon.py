"""System monitoring: CPU, RAM, disk, GPU, battery, network, processes, uptime, temperatures."""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

PROTECTED = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
             "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "init", "systemd",
             "launchd", "kernel_task", "sshd", "python", "python.exe", "pythonw.exe", "galahad", "jarvis"}


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


class SysMon(Plugin):
    manifest = Manifest(
        id="sysmon", name="System Monitor", category="System", version="1.0.0",
        description="CPU, RAM, disk, GPU, battery, network, running processes, uptime and temperatures.",
        permissions=["system.monitor", "system.control", "notify"], requires_python=["psutil"], optional_python=["pynvml"],
        background=True)

    def initialize(self, ctx):
        super().initialize(ctx)
        self.history: deque = deque(maxlen=180)       # ~30 min at 10 s
        self._net_last: Optional[tuple] = None

    def health(self):
        return Health("ok", "psutil available")

    def ticks(self):
        return [("sample", 10, self.sample)]

    # ── sampling ──────────────────────────────────────────────────────
    def sample(self) -> dict:
        import psutil
        now = time.time()
        net = psutil.net_io_counters()
        up = down = 0.0
        if self._net_last:
            dt = max(now - self._net_last[0], 0.001)
            up, down = (net.bytes_sent - self._net_last[1]) / dt, (net.bytes_recv - self._net_last[2]) / dt
        self._net_last = (now, net.bytes_sent, net.bytes_recv)
        s = {"t": now, "cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
             "net_up": max(up, 0), "net_down": max(down, 0)}
        self.history.append(s)
        recent = list(self.history)[-3:]
        for label, key in (("CPU", "cpu"), ("memory", "ram")):
            if len(recent) == 3 and all(h[key] > 90 for h in recent) and self.ctx.has("notify"):
                self.ctx.notify(f"High {label} usage", f"{label} has been above 90% for 30 seconds ({s[key]:.0f}%).",
                                "system", 2, dedupe_key=f"high-{label}", )
        return s

    def _gpu(self) -> Optional[dict]:
        try:
            import pynvml
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            name = pynvml.nvmlDeviceGetName(h)
            return {"name": name.decode() if isinstance(name, bytes) else name, "util": util.gpu,
                    "mem_percent": round(mem.used / mem.total * 100, 1)}
        except Exception:
            return None

    @command(description="Full snapshot: CPU, RAM, disks, GPU, battery, network, uptime, temperatures.", readonly=True,
             permissions=("system.monitor",))
    def snapshot(self):
        import psutil
        cpu = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        disks = []
        for p in psutil.disk_partitions(all=False):
            if sys.platform != "win32" and (p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay") or p.mountpoint.startswith(("/snap", "/boot"))):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({"mount": p.mountpoint, "total": u.total, "used": u.used, "percent": u.percent})
            except (PermissionError, OSError):
                continue
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        battery = {"percent": round(bat.percent), "charging": bool(bat.power_plugged),
                   "minutes_left": None if bat.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else int(bat.secsleft / 60)} if bat else None
        temps = {}
        try:
            for name, lst in (psutil.sensors_temperatures() or {}).items():
                if lst:
                    temps[name] = round(lst[0].current, 1)
        except Exception:
            pass
        stats = psutil.net_if_stats()
        net = {"interfaces_up": [n for n, s in stats.items() if s.isup and n.lower() not in ("lo", "loopback pseudo-interface 1")],
               "connected": any(s.isup for n, s in stats.items() if n.lower() not in ("lo",))}
        last = self.history[-1] if self.history else {}
        net.update({"up_bps": last.get("net_up", 0), "down_bps": last.get("net_down", 0)})
        data = {"cpu": cpu, "cpu_cores": psutil.cpu_count(), "ram": {"percent": vm.percent, "used": vm.used, "total": vm.total},
                "disks": disks, "gpu": self._gpu(), "battery": battery, "network": net, "temperatures": temps,
                "uptime_seconds": int(time.time() - psutil.boot_time()), "processes": len(psutil.pids()),
                "platform": sys.platform}
        bits = [f"CPU {cpu:.0f}%", f"RAM {vm.percent:.0f}% ({human_bytes(vm.used)} of {human_bytes(vm.total)})"]
        if disks:
            d = max(disks, key=lambda x: x["percent"])
            bits.append(f"disk {d['mount']} {d['percent']:.0f}% full")
        if battery:
            bits.append(f"battery {battery['percent']}% ({'charging' if battery['charging'] else 'on battery'})")
        if data["gpu"]:
            bits.append(f"GPU {data['gpu']['util']}%")
        bits.append("network " + ("connected" if net["connected"] else "down"))
        bits.append(f"up {human_duration(data['uptime_seconds'])}")
        return {"message": ", ".join(bits) + ".", "data": data}

    @command(description="Recent CPU/RAM/network samples for graphs (about 10 s apart).", readonly=True,
             permissions=("system.monitor",), params={"points": {"type": "integer", "default": 60, "maximum": 180, "minimum": 1}})
    def history_points(self, points=60):
        if not self.history:
            self.sample()
        pts = list(self.history)[-points:]
        return {"message": f"{len(pts)} samples.", "data": {"samples": pts}}

    @command(description="Top running processes by CPU or memory, optionally filtered by name.", readonly=True,
             permissions=("system.monitor",),
             params={"sort": {"type": "string", "enum": ["cpu", "memory"], "default": "memory"},
                     "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100}, "name": {"type": "string"}})
    def processes(self, sort="memory", limit=10, name=""):
        import psutil
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "username"]):
            try:
                nm = p.info["name"] or ""
                if name and name.lower() not in nm.lower():
                    continue
                rows.append({"pid": p.info["pid"], "name": nm, "cpu": p.info["cpu_percent"] or 0.0,
                             "memory": p.info["memory_info"].rss if p.info["memory_info"] else 0})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: r["cpu" if sort == "cpu" else "memory"], reverse=True)
        rows = rows[:limit]
        return {"message": f"Top {len(rows)} by {sort}: " + ", ".join(f"{r['name']} ({human_bytes(r['memory'])})" for r in rows[:5]),
                "data": {"processes": rows}}

    @command(description="End a process by PID. Refuses critical system processes. Asks for confirmation.",
             permissions=("system.control",), destructive=True,
             params={"pid": {"type": "integer", "required": True}},
             confirm=lambda a: f"End process {a['pid']}?")
    def end_process(self, pid):
        import psutil
        if pid in (os.getpid(), 0, 1, 4):
            raise PluginError("I won't end that process — it's critical.")
        try:
            p = psutil.Process(pid)
            name = p.name()
            if name.lower() in PROTECTED or p.username() == "root" and sys.platform != "win32":
                raise PluginError(f"'{name}' is a protected system process; I won't end it.")
            p.terminate()
            try:
                p.wait(5)
            except psutil.TimeoutExpired:
                return {"ok": False, "message": f"{name} (pid {pid}) did not exit after a polite request; not forced."}
        except psutil.NoSuchProcess:
            raise PluginError(f"No process with PID {pid}.")
        except psutil.AccessDenied:
            raise PluginError("Windows denied access to that process.")
        return f"Ended {name} (pid {pid})."


PLUGINS = [SysMon]
