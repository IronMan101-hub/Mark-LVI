"""One background thread drives every periodic job (plugin ticks, workflow
triggers, housekeeping). Jobs run on their own short-lived threads, never
overlap with themselves, and a crashing job is logged and retried next period."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import logs

log = logs.get("scheduler")


@dataclass
class Job:
    owner: str
    name: str
    interval: float
    fn: Callable[[], None]
    next_due: float = 0.0
    running: bool = False
    failures: int = 0
    last_error: str = ""
    last_run: float = 0.0


class Scheduler:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, owner: str, name: str, interval: float, fn: Callable[[], None], run_soon: bool = False) -> None:
        with self._lock:
            self.jobs[f"{owner}:{name}"] = Job(owner, name, interval, fn,
                                               next_due=time.time() + (1 if run_soon else interval))

    def remove_owner(self, owner: str) -> None:
        with self._lock:
            for k in [k for k, j in self.jobs.items() if j.owner == owner]:
                del self.jobs[k]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="gal-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_due(self) -> int:
        """One scheduling pass (also called directly by tests)."""
        now, started = time.time(), 0
        with self._lock:
            due = [j for j in self.jobs.values() if j.next_due <= now and not j.running]
        for j in due:
            j.running = True
            j.next_due = now + j.interval
            started += 1
            threading.Thread(target=self._exec, args=(j,), daemon=True, name=f"gal-job-{j.name}").start()
        return started

    def _exec(self, j: Job) -> None:
        try:
            j.fn()
            j.failures, j.last_error = 0, ""
        except Exception as e:  # noqa: BLE001
            j.failures += 1
            j.last_error = str(e)
            log.warning("job %s:%s failed (%d): %s", j.owner, j.name, j.failures, e)
        finally:
            j.last_run = time.time()
            j.running = False

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.run_due()
            except Exception as e:  # noqa: BLE001
                log.error("scheduler loop: %s", e)

    def status(self) -> list[dict]:
        return [{"owner": j.owner, "name": j.name, "interval": j.interval, "failures": j.failures,
                 "last_error": j.last_error, "last_run": j.last_run} for j in self.jobs.values()]
