"""python -m galahad <command> — headless service and maintenance tools."""
from __future__ import annotations

import argparse
import json
import sys
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="galahad", description="GALAHAD command centre")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the engine + dashboard without the desktop window")
    sub.add_parser("status", help="print engine, plugin and AI status")
    sub.add_parser("plugins", help="list plugins and their health")
    sub.add_parser("catalog", help="regenerate galahad/catalog.json (tool descriptions for the voice bridge)")
    sub.add_parser("doctor", help="check dependencies and configuration")
    r = sub.add_parser("run", help="send a request to the assistant"); r.add_argument("text", nargs="+")
    a = sub.add_parser("autostart", help="start with Windows"); a.add_argument("state", choices=["on", "off", "status"])
    sub.add_parser("dashboard", help="open the dashboard in your browser (starts it if needed)")
    args = ap.parse_args(argv)

    if args.cmd == "catalog":
        from .engine import Engine
        from .bridge import CATALOG, build_catalog
        e = Engine().start()
        try:
            cat = build_catalog(e)
            CATALOG.write_text(json.dumps(cat, indent=1, sort_keys=True), encoding="utf-8")
            print(f"wrote {CATALOG} ({sum(len(v) for v in cat.values())} commands in {len(cat)} plugins)")
        finally:
            e.stop()
        return 0
    if args.cmd == "autostart":
        from . import autostart
        print(autostart.is_enabled() if args.state == "status" else autostart.set_enabled(args.state == "on"))
        return 0
    if args.cmd == "serve":
        from . import instance, runtime
        if not instance.acquire():
            instance.poke_existing("show")
            print("GALAHAD is already running; asked it to show the dashboard.")
            return 0
        eng = runtime.attach()
        instance.listen(lambda m: runtime.open_dashboard() if m == "show" else None)
        print(f"GALAHAD running. Dashboard: {runtime.dashboard_url(False)}   (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            runtime.shutdown()
            instance.release()
        return 0
    if args.cmd == "dashboard":
        from . import runtime
        runtime.attach()
        print(runtime.open_dashboard())
        input("Press Enter to stop…")
        runtime.shutdown()
        return 0

    from .engine import Engine
    e = Engine().start()
    try:
        if args.cmd == "status" or args.cmd is None:
            print(json.dumps(e.status(), indent=2))
        elif args.cmd == "plugins":
            for p in e.registry.list():
                print(f"{p['id']:14} {p['category']:13} {p['state']:10} {p['health']['status']:12} {p['health']['detail'][:70]}")
        elif args.cmd == "run":
            out = e.orchestrator.handle(" ".join(args.text))
            print(out["reply"])
        elif args.cmd == "doctor":
            return doctor(e)
    finally:
        e.stop()
    return 0


def doctor(e) -> int:
    import importlib.util
    bad = 0
    print(f"GALAHAD doctor — python {sys.version.split()[0]} on {sys.platform}")
    print(f"  secret store : {e.secrets.backend_name()}")
    print(f"  database     : {e.db.path} (schema v{e.db.schema_version()})")
    print(f"  AI provider  : {'configured' if e.ai.available() else 'NOT configured (assistant uses offline command router)'}")
    for mod, why in (("psutil", "system monitor"), ("docx", "Word"), ("openpyxl", "Excel"), ("pptx", "PowerPoint"), ("bs4", "web reading"),
                     ("fastapi", "dashboard"), ("uvicorn", "dashboard"), ("cryptography", "secret store"), ("playwright", "browser tab control"),
                     ("mss", "screenshots"), ("pyautogui", "keyboard/mouse"), ("pyperclip", "clipboard"), ("PIL", "images")):
        ok = importlib.util.find_spec(mod) is not None
        bad += (not ok)
        print(f"  {'ok ' if ok else 'MISSING'} {mod:12} ({why})")
    for p in e.registry.list():
        if p["state"] == "error":
            bad += 1
            print(f"  PLUGIN ERROR {p['id']}: {p['error']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
