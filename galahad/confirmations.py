"""Human-in-the-loop gate.

A confirmation can only be resolved by the interface (dashboard button or the
desktop HUD banner) — never by the model and never by a tool argument. Pending
requests expire so a forgotten prompt can't be approved hours later by accident.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .events import EventBus

TTL = 600.0


@dataclass
class Pending:
    id: str
    plugin: str
    command: str
    args: dict
    summary: str
    activity_id: int
    origin: str
    run_id: Optional[int]
    created: float = field(default_factory=time.time)
    state: str = "pending"          # pending | approved | denied | expired
    result: Any = None
    done: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {"id": self.id, "plugin": self.plugin, "command": self.command, "args": self.args,
                "summary": self.summary, "activity_id": self.activity_id, "origin": self.origin,
                "created": self.created, "state": self.state,
                "expires_in": max(0, int(TTL - (time.time() - self.created)))}


class ConfirmationBroker:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._items: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def create(self, **kw) -> Pending:
        p = Pending(id=uuid.uuid4().hex[:10], **kw)
        with self._lock:
            self._items[p.id] = p
        self.bus.publish("confirm.requested", p.to_dict())
        return p

    def get(self, pid: str) -> Optional[Pending]:
        self.expire()
        return self._items.get(pid)

    def pending(self) -> list[dict]:
        self.expire()
        with self._lock:
            return [p.to_dict() for p in self._items.values() if p.state == "pending"]

    def expire(self) -> list[Pending]:
        now, out = time.time(), []
        with self._lock:
            for p in self._items.values():
                if p.state == "pending" and now - p.created > TTL:
                    p.state = "expired"
                    out.append(p)
            # forget old resolved items
            for pid in [k for k, v in self._items.items() if v.state != "pending" and now - v.created > 3600]:
                del self._items[pid]
        for p in out:
            p.done.set()
            self.bus.publish("confirm.resolved", {"id": p.id, "state": "expired", "activity_id": p.activity_id})
        return out

    def take(self, pid: str, state: str) -> Optional[Pending]:
        """Atomically move pending -> state. Returns None if not pending (double-click safe)."""
        self.expire()
        with self._lock:
            p = self._items.get(pid)
            if not p or p.state != "pending":
                return None
            p.state = state
            return p

    def finish(self, p: Pending, result: Any) -> None:
        p.result = result
        p.done.set()
        self.bus.publish("confirm.resolved", {"id": p.id, "state": p.state, "activity_id": p.activity_id})
