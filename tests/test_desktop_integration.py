"""Qt tray / minimise-to-tray / second-launch behaviour, run headless (offscreen platform)."""
import os

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def qt(full_engine, monkeypatch):
    from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon
    app = QApplication.instance() or QApplication([])
    # The offscreen platform has no tray host, so pretend one exists; the integration logic is what is under test.
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    win = QMainWindow(); win.show()
    return app, win


def test_close_minimises_to_tray_until_quit(qt, full_engine):
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QCloseEvent
    from galahad import qt_integration
    app, win = qt
    tray = qt_integration.install(app, win)
    assert tray is not None and tray.isVisible()
    ev = QCloseEvent()
    app.sendEvent(win, ev)
    assert not ev.isAccepted() and not win.isVisible()                    # hidden, not closed
    full_engine.settings.set("startup.minimise_to_tray", False)
    ev2 = QCloseEvent(); app.sendEvent(win, ev2)
    assert ev2.isAccepted()                                               # setting honoured


def test_second_launch_message_shows_window(qt, full_engine):
    from galahad import instance, qt_integration
    app, win = qt
    instance.release()
    assert instance.acquire()                      # main() acquires before the window is built
    qt_integration.install(app, win)
    win.hide()
    assert not _second_acquire()                   # a second process cannot take the lock
    instance.poke_existing("show")
    import time
    for _ in range(40):
        app.processEvents(); time.sleep(0.05)
        if win.isVisible():
            break
    assert win.isVisible()
    instance.release()


def _second_acquire():
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 47653)); return True
    except OSError:
        return False
    finally:
        s.close()
