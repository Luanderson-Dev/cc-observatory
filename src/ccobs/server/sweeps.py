"""The schedulers: the stale sweep and retention (ADRs 0024 and 0026), and the alert
rules (ADR 0041).

They write nothing themselves. Every ``tick_interval_s`` the sweeper submits a tick to
the writer (``Writer.submit_tick``), which runs ``mark_stale`` with the writer's
threshold over every session in memory; every ``retention_interval_s`` it submits
retention (``Writer.submit_retention``) with the cutoff ``now - retention``. Every
``interval_s`` (5 s) the alert ticker submits an alert tick
(``Writer.submit_alert_tick``), on which the writer runs the alert rules. Every write
stays on the writer thread (ADR 0026, one writer).

Each runs on a daemon thread of its own, like the writer and the metrics listener,
rather than as an asyncio task: submitting is a put on the writer's thread-safe control
queue, so a scheduler needs nothing from the event loop, can never block it, and its
loop is plain code that tests drive synchronously. It waits on a stop ``Event``, so
``stop`` interrupts the wait at once instead of sitting out a 60 s sleep.

Retention runs right after the first tick, so an app that was down for a long time
catches up a minute after it starts, then once every ``retention_interval_s`` worth of
ticks. An error never stops a loop: it is logged by its type only (a message may quote
a session ID or SQL) and the next round runs as usual.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Final, Protocol

from ccobs.server.ingest import Clock, wall_clock_ms

__all__ = [
    "ALERT_TICK_INTERVAL_S",
    "RETENTION_INTERVAL_S",
    "TICK_INTERVAL_S",
    "AlertTarget",
    "AlertTicker",
    "SweepTarget",
    "Sweeper",
]

_log = logging.getLogger(__name__)

# Staleness is measured in hours (ADR 0024, 12 h by default): a minute of granularity is
# plenty, and a tick a minute costs one walk over the sessions in memory.
TICK_INTERVAL_S: Final = 60.0
# Retention is measured in days (ADR 0026, at least 1d): once an hour is plenty.
RETENTION_INTERVAL_S: Final = 3600.0
# The alert rules' thresholds are tens of seconds or more (ADR 0041): an evaluation
# every 5 s fires at most 5 s late, for one walk over the sessions in memory.
ALERT_TICK_INTERVAL_S: Final = 5.0
_MS_PER_S: Final = 1000


class SweepTarget(Protocol):
    """What the sweeper needs of the writer."""

    def submit_tick(self, now_ms: int) -> None: ...

    def submit_retention(self, cutoff_ms: int) -> None: ...


class AlertTarget(Protocol):
    """What the alert ticker needs of the writer."""

    def submit_alert_tick(self, now_ms: int) -> None: ...


def _check_positive(name: str, value: float) -> None:
    if not value > 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _submit(what: str, submit: Callable[[int], None], value: int) -> None:
    try:
        submit(value)
    except Exception as error:
        _log.error("%s not submitted (%s)", what, type(error).__name__)


class _Scheduler:
    """Runs ``loop`` on a daemon thread of its own, named ``ccobs-<name>``.
    ``wait(timeout_s)`` waits between rounds and returns True to stop; by default it
    waits on the stop event, which ``stop`` sets."""

    def __init__(
        self, name: str, loop: Callable[[], None], wait: Callable[[float], bool] | None
    ) -> None:
        self._name = name
        self._loop = loop
        self._stopping = threading.Event()
        self._wait: Callable[[float], bool] = wait or self._stopping.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run the loop on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError(f"the {self._name} is already started")
        thread_name = f"ccobs-{self._name.replace(' ', '-')}"
        self._thread = threading.Thread(target=self._loop, name=thread_name, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        """Tell the loop to end, interrupting its wait; ``stop`` then joins it."""
        self._stopping.set()

    def stop(self, timeout_s: float) -> None:
        """Stop the loop, interrupting its wait, and join the thread for ``timeout_s``."""
        self._stopping.set()
        if self._thread is None:
            return
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            _log.warning("%s did not stop within %.1f s", self._name, timeout_s)


class Sweeper(_Scheduler):
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
        _check_positive("tick_interval_s", tick_interval_s)
        _check_positive("retention_interval_s", retention_interval_s)
        super().__init__("sweeper", self.run, wait)
        self.retention_ms: Final = int(retention.total_seconds() * _MS_PER_S)
        self.tick_interval_s: Final = tick_interval_s
        self.retention_interval_s: Final = retention_interval_s
        # Retention runs on every n-th tick, never more often than its interval asks.
        self._ticks_per_retention: Final = max(1, math.ceil(retention_interval_s / tick_interval_s))
        self._writer = writer
        self._clock = clock

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
            _submit("tick", self._writer.submit_tick, now_ms)
            if retention_due:
                _submit("retention", self._writer.submit_retention, now_ms - self.retention_ms)
                retention_due = False


class AlertTicker(_Scheduler):
    """Submits an alert tick to the writer every ``interval_s``, with the time ``clock``
    gives (the hook route's). Like the sweeper's, its ``run`` is the loop and ``start``
    runs it on a daemon thread; ``wait`` is injectable for tests."""

    def __init__(
        self,
        writer: AlertTarget,
        *,
        interval_s: float = ALERT_TICK_INTERVAL_S,
        clock: Clock = wall_clock_ms,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        _check_positive("interval_s", interval_s)
        super().__init__("alert ticker", self.run, wait)
        self.interval_s: Final = interval_s
        self._writer = writer
        self._clock = clock

    def run(self) -> None:
        """Wait an interval, submit, repeat, until ``wait`` says stop."""
        while not self._wait(self.interval_s):
            try:
                now_ms = self._clock()
            except Exception as error:
                _log.error("alert tick skipped: the clock failed (%s)", type(error).__name__)
                continue
            _submit("alert tick", self._writer.submit_alert_tick, now_ms)
