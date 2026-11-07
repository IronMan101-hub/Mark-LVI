"""Tiny in-process pub/sub. Handlers run on the publisher's thread inside a
try/except so one broken subscriber can never break an emitter."""
from __future__ import annotations

import fnmatch
import threading
from collections import deque
from typing import Callable

from . import logs

log = logs.get("events")


class EventBus:
    def __init__(self):
        self._subs: list[tuple[str, Callable, str]] = []
        self._lock = threading.Lock()
        self.recent: deque = deque(maxlen=300)

    def subscribe(self, pattern: str, handler: Callable, owner: str = "core") -> Callable[[], None]:
        entry = (pattern, handler, owner)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe():
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)
        return unsubscribe

    def unsubscribe_owner(self, owner: str) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[2] != owner]

    def publish(self, topic: str, payload: dict | None = None) -> int:
        payload = payload or {}
        self.recent.append({"topic": topic, "payload": payload})
        with self._lock:
            targets = [s for s in self._subs if fnmatch.fnmatch(topic, s[0])]
        for _pat, handler, owner in targets:
            try:
                handler(topic, payload)
            except Exception as e:  # noqa: BLE001 — isolation is the point
                log.warning("subscriber %s failed on %s: %s", owner, topic, e)
        return len(targets)
