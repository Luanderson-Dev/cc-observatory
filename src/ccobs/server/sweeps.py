"""The sweeper: the stale sweep and retention, on a schedule (ADRs 0024 and 0026).

The sweeper writes nothing itself. Every ``tick_interval_s`` it submits a tick to the
writer (``Writer.submit_tick``), which runs ``mark_stale`` with the writer's threshold
over every session in memory; every ``retention_interval_s`` it submits retention
(``Writer.submit_retention``) with the cutoff ``now - retention``. Every write stays on
the writer thread (ADR 0026, one writer).

It runs on a daemon thread of its own, like the writer and the metrics listener, rather
than as an asyncio task: submitting is a put on the writer's thread-safe control queue,
so the sweeper needs nothing from the event loop, can never block it, and its loop is
plain code that tests drive synchronously. It waits on a stop ``Event``, so ``stop``
interrupts the wait at once instead of sitting out a 60 s sleep.

Retention runs right after the first tick, so an app that was down for a long time
catches up a minute after it starts, then once every ``retention_interval_s`` worth of
ticks. An error never stops the loop: it is logged by its type only (a message may
quote a session ID or SQL) and the next round runs as usual.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Final, Protocol

from ccobs.server.ingest import Clock, wall_clock_ms

__all__ = ["RETENTION_INTERVAL_S", "TICK_INTERVAL_S", "SweepTarget", "Sweeper"]

_log = logging.getLogger(__name__)

# Staleness is measured in hours (ADR 0024, 12 h by default): a minute of granularity is
# plenty, and a tick a minute costs one walk over the sessions in memory.
TICK_INTERVAL_S: Final = 60.0
# Retention is measured in days (ADR 0026, at least 1d): once an hour is plenty.
RETENTION_INTERVAL_S: Final = 3600.0
_MS_PER_S: Final = 1000


class SweepTarget(Protocol):
    """What the sweeper needs of the writer."""

    def submit_tick(self, now_ms: int) -> None: ...

    def submit_retention(self, cutoff_ms: int) -> None: ...


class Sweeper:
    """Submits ticks and retention to the writer on a schedule.

    ``retention`` is ``CCOBS_APP_RETENTION``; the stale threshold is the writer's own.
    ``clock`` gives the wall-clock time in milliseconds that each tick and cutoff use.
    ``wait(timeout_s)`` waits between rounds and returns True to stop; by default it
    waits on the stop event. ``run`` is the loop; ``start`` runs it on a daemon thread.
    """

    def __init__(
        self,
        writer: SweepTarget,
        *,
        retention: timedelta,
        tick_interval_s: float = TICK_INTERVAL_S,
        retention_interval_s: float = RETENTION_INTERVAL_S,
        clock: Clock = wall_clock_ms,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        for name, value in (
            ("tick_interval_s", tick_interval_s),
            ("retention_interval_s", retention_interval_s),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        self.retention_ms: Final = int(retention.total_seconds() * _MS_PER_S)
        self.tick_interval_s: Final = tick_interval_s
        self.retention_interval_s: Final = retention_interval_s
        # Retention runs on every n-th tick, never more often than its interval asks.
        self._ticks_per_retention: Final = max(1, math.ceil(retention_interval_s / tick_interval_s))
        self._writer = writer
        self._clock = clock
        self._stopping = threading.Event()
        self._wait: Callable[[float], bool] = wait or self._stopping.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run the loop on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError("the sweeper is already started")
        self._thread = threading.Thread(target=self.run, name="ccobs-sweeper", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float) -> None:
        """Stop the loop, interrupting its wait, and join the thread for ``timeout_s``."""
        self._stopping.set()
        if self._thread is None:
            return
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            _log.warning("sweeper did not stop within %.1f s", timeout_s)

    def run(self) -> None:
        """Wait a tick interval, submit, repeat, until ``wait`` says stop."""
        rounds = 0
        # Set on a scheduled round, cleared once retention is submitted: a round the
        # clock fails on moves its retention to the next tick instead of dropping it.
        retention_due = False
        while not self._wait(self.tick_interval_s):
            if rounds % self._ticks_per_retention == 0:
                retention_due = True
            rounds += 1
            try:
                now_ms = self._clock()
            except Exception as error:
                _log.error("sweep skipped: the clock failed (%s)", type(error).__name__)
                continue
            self._submit("tick", self._writer.submit_tick, now_ms)
            if retention_due:
                self._submit("retention", self._writer.submit_retention, now_ms - self.retention_ms)
                retention_due = False

    @staticmethod
    def _submit(what: str, submit: Callable[[int], None], value: int) -> None:
        try:
            submit(value)
        except Exception as error:
            _log.error("%s not submitted (%s)", what, type(error).__name__)
