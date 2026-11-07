"""Desktop integration for the PyQt6 window: system-tray icon + menu, minimise-to-tray,
start-minimised, and "show me" requests from a second launch. Runs on the Qt main thread
(called from JarvisUI.__init__) and touches only the widgets it creates itself."""
from __future__ import annotations

import sys
from typing import Optional

from . import instance, logs, paths, runtime
from .engine import get_engine

log = logs.get("qt")


def install(app, window, player=None) -> Optional[object]:
    """Returns the tray icon (kept alive by the caller) or None when no tray exists."""
    from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

    eng = get_engine()
    quitting = {"flag": False}

    class Bridge(QObject):
        show_sig = pyqtSignal()
        dash_sig = pyqtSignal()

    br = Bridge()

    def show_window():
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def open_dash():
        import threading
        threading.Thread(target=runtime.open_dashboard, daemon=True, name="gal-open-dashboard").start()

    br.show_sig.connect(show_window)
    br.dash_sig.connect(open_dash)

    def on_instance_message(msg: str):
        if msg == "show":
            br.show_sig.emit()
        elif msg == "dashboard":
            br.dash_sig.emit()
    instance.listen(on_instance_message)

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        ico = paths.project_root() / "config" / "jarvis.ico"
        tray = QSystemTrayIcon(QIcon(str(ico)) if ico.exists() else window.windowIcon(), parent=window)
        tray.setToolTip(f"{eng.settings.get('general.app_name', 'GALAHAD')} — running")
        menu = QMenu()
        a_show = QAction("Show JARVIS", menu); a_show.triggered.connect(show_window)
        a_dash = QAction("Open GALAHAD dashboard", menu); a_dash.triggered.connect(open_dash)
        a_notif = QAction("Notifications", menu); a_notif.setCheckable(True)
        a_notif.setChecked(bool(eng.settings.get("startup.notifications", True)))
        a_notif.toggled.connect(lambda on: eng.settings.set("startup.notifications", bool(on)))
        a_quit = QAction("Quit GALAHAD", menu)

        def quit_all():
            quitting["flag"] = True
            tray.hide()
            try:
                runtime.shutdown()
            except Exception:
                pass
            instance.release()
            app.quit()
        a_quit.triggered.connect(quit_all)
        for a in (a_show, a_dash, a_notif):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: show_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        tray.show()
        window._galahad_tray_menu = menu          # keep references alive
        window._galahad_tray = tray

        # Show in-app notifications as tray balloons too (works even when the window is hidden).
        def balloon(_t, p):
            if p.get("priority", 1) >= 2 and not window.isActiveWindow():
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, lambda: tray.showMessage(p["title"], p.get("body", ""), QSystemTrayIcon.MessageIcon.Information, 6000))
        eng.bus.subscribe("notify.deliver", balloon, owner="tray")

    class Filter(QObject):
        shown_hint = False

        def eventFilter(self, obj, ev):  # noqa: N802
            if obj is window and ev.type() == QEvent.Type.Close and not quitting["flag"] \
                    and tray is not None and tray.isVisible() and eng.settings.get("startup.minimise_to_tray", True):
                ev.ignore()
                window.hide()
                if not Filter.shown_hint:
                    Filter.shown_hint = True
                    tray.showMessage("Still running", "GALAHAD keeps working in the background. Right-click the tray icon to quit.",
                                     QSystemTrayIcon.MessageIcon.Information, 4000)
                return True
            return False

    flt = Filter(window)
    window.installEventFilter(flt)
    window._galahad_filter = flt
    if tray is not None:
        app.setQuitOnLastWindowClosed(False)

    if tray is not None and (eng.settings.get("startup.start_minimised", False) or "--minimized" in sys.argv):
        window.hide()
    log.info("desktop integration installed (tray=%s)", bool(tray))
    return tray
