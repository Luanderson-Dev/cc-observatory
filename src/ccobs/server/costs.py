"""The cost reader: the cost rules' totals, from Prometheus (ADRs 0041 and 0043).

The cost rules (``session_cost``, ``daily_cost``) run on the writer thread over the last
``CostSample`` the writer was given (ADR 0041). This reader gives it one: every
``poll_interval_s`` (60 s, Claude Code's metrics export interval, and never less, since a
faster poll sees no new sample), while at least one cost rule is enabled in the writer's
effective rules (``Writer.alert_config``, read at every round), it sends the instant
queries of ``ccobs.domain.alerts`` as ``GET /api/v1/query`` with the round's evaluation
``time``, and submits their parsed answers (``parse_cost_sample``) to the writer
(``Writer.submit_cost_sample``), which stores the sample for its next alert tick.

- ``session_cost_query`` only while ``session_cost`` is on and the writer holds a
  session that can alert: ``Writer.session_cost_since``, read under the writer's lock,
  gives the oldest open session's start and so the query's range, which grows with that
  session's age (ADR 0041).
- ``daily_cost_query`` only while ``daily_cost`` is on.
- Neither query carries a ``session_id`` matcher: the session query groups by it, so no
  session ID reaches Prometheus, and one query covers every session.
- No ``lookback_delta``: ADR 0043 records why.

A round submits a sample only when every query it sent was answered with a valid
vector: never part of a total. A failure (no connection, a timeout, a non-2xx status, a
redirect, which is never followed, an answer too large, not JSON or not a valid vector)
submits nothing and sends no further query that round, so the writer's last sample ages
and, past ``COST_STALE_MS``, the cost rules neither fire nor clear (they hold). The next
wait then doubles, from ``backoff_initial_s`` (at least the poll interval) up to
``backoff_max_s``, until a round succeeds or has nothing to ask.

Like the reconciler (``ccobs.server.reconcile``), it runs on a daemon thread with a sync
``httpx.Client`` of its own and waits on a stop ``Event``, so ``stop`` interrupts the wait
at once; a query in flight is not cut short. Each query is bounded like the session
page's (``ccobs.server.usage``): httpx's timeout per phase and ``timeout_s`` for the
whole answer, checked as it streams in, and ``max_answer_bytes``. Each query counts its
result (``on_query``, ``AppMetrics.count_prometheus_query``): ``ok``, ``error``,
``timeout`` or ``invalid``. Logs hold exception type names, status codes and reasons
only: never the URL (with its query), a session ID or a body.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Final, Literal, Protocol

import httpx

from ccobs.domain.alerts import (
    AlertConfig,
    CostSample,
    daily_cost_query,
    parse_cost_sample,
    session_cost_query,
)
from ccobs.domain.app_metrics import PrometheusQueryResult
from ccobs.server.ingest import Clock, wall_clock_ms

__all__ = [
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "HTTP_TIMEOUT_S",
    "MAX_ANSWER_BYTES",
    "MIN_POLL_INTERVAL_S",
    "POLL_INTERVAL_S",
    "QUERY_PATH",
    "CostReader",
    "CostTarget",
    "PollResult",
]

_log = logging.getLogger(__name__)

# Claude Code exports its metrics every 60 s by default (OTEL_METRIC_EXPORT_INTERVAL,
# which ccobs setup leaves alone): a faster poll would read the same samples again, over
# a range that can span days (ADR 0041).
POLL_INTERVAL_S: Final = 60.0
MIN_POLL_INTERVAL_S: Final = 60.0
# Per phase, and for the whole answer, as the session page's queries (ADR 0035).
HTTP_TIMEOUT_S: Final = 3.0
# The session query has one series per session in its range, a few hundred bytes each:
# 8 MiB holds tens of thousands of sessions.
MAX_ANSWER_BYTES: Final = 8 * 1024 * 1024
# After a failure: 2, 4 and 8 minutes, then every 10 minutes until a round succeeds.
BACKOFF_INITIAL_S: Final = 60.0
BACKOFF_MAX_S: Final = 600.0
QUERY_PATH: Final = "/api/v1/query"
_MS_PER_S: Final = 1000
# What the session query answers over no session: the sample's sessions when it is not
# sent.
_NO_SESSIONS: Final = {"status": "success", "data": {"resultType": "vector", "result": []}}

# idle: nothing to ask, no query sent; ok and error: queries were sent.
type PollResult = Literal["idle", "ok", "error"]
type _Failure = Literal["error", "timeout", "invalid"]


class CostTarget(Protocol):
    """What the cost reader needs of the writer."""

    def alert_config(self) -> AlertConfig: ...

    def session_cost_since(self) -> int | None: ...

    def submit_cost_sample(self, sample: CostSample) -> None: ...


class _QueryError(Exception):
    """A query that got no usable answer; its message is safe to log."""

    def __init__(self, reason: _Failure, detail: str) -> None:
        super().__init__(detail)
        self.reason: Final = reason


def _time_param(now_ms: int) -> str:
    """``now_ms`` as the ``time`` parameter of Prometheus's query API: seconds."""
    seconds, millis = divmod(now_ms, _MS_PER_S)
    return f"{seconds}.{millis:03d}"


class CostReader:
    """Reads the cost rules' totals from Prometheus and submits them to the writer.

    ``prometheus_url`` is ``CCOBS_PROMETHEUS_URL`` (no trailing slash) and ``host`` the
    host label the queries filter on. ``clock`` gives the wall-clock time in ms, the
    hook route's, which times the queries and the sample; ``monotonic`` bounds each
    whole answer. ``wait(timeout_s)`` waits between rounds and returns True to stop; by
    default it waits on the stop event. ``transport`` replaces httpx's (tests pass an
    ``httpx.MockTransport``), and ``on_query`` receives the result of every query sent.
    ``run`` is the loop and ``poll_once`` one round; ``start`` runs the loop on a daemon
    thread, and ``stop`` ends it and closes the HTTP client.
    """

    def __init__(
        self,
        writer: CostTarget,
        *,
        prometheus_url: str,
        host: str,
        poll_interval_s: float = POLL_INTERVAL_S,
        timeout_s: float = HTTP_TIMEOUT_S,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        clock: Clock = wall_clock_ms,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        transport: httpx.BaseTransport | None = None,
        on_query: Callable[[PrometheusQueryResult], None] | None = None,
    ) -> None:
        for name, value in (("timeout_s", timeout_s), ("max_answer_bytes", max_answer_bytes)):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        for name, value, floor, what in (
            ("poll_interval_s", poll_interval_s, MIN_POLL_INTERVAL_S, "the export interval"),
            ("backoff_initial_s", backoff_initial_s, poll_interval_s, "poll_interval_s"),
            ("backoff_max_s", backoff_max_s, backoff_initial_s, "backoff_initial_s"),
        ):
            if not value >= floor:
                raise ValueError(f"{name} must be at least {what} ({floor:g}), got {value}")
        self.prometheus_url: Final = prometheus_url
        self.host: Final = host
        self.poll_interval_s: Final = poll_interval_s
        self._url: Final = f"{prometheus_url}{QUERY_PATH}"
        self._timeout_s: Final = timeout_s
        self._max_answer_bytes: Final = max_answer_bytes
        self._backoff_initial_s: Final = backoff_initial_s
        self._backoff_max_s: Final = backoff_max_s
        self._writer = writer
        self._clock = clock
        self._monotonic = monotonic
        self._on_query = on_query
        # No proxy from the environment: Prometheus is on the compose network.
        self._client: Final = httpx.Client(
            timeout=timeout_s, transport=transport, trust_env=False, follow_redirects=False
        )
        self._stopping = threading.Event()
        self._wait: Callable[[float], bool] = wait or self._stopping.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run the loop on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError("the cost reader is already started")
        self._thread = threading.Thread(target=self.run, name="ccobs-cost-reader", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        """Tell the loop to end, interrupting its wait; ``stop`` then joins it."""
        self._stopping.set()

    def stop(self, timeout_s: float) -> None:
        """Stop the loop, interrupting its wait, join the thread for ``timeout_s`` and
        close the HTTP client."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                _log.warning("cost reader did not stop within %.1f s", timeout_s)
        self._client.close()

    def run(self) -> None:
        """Wait, poll, repeat, until ``wait`` says stop; errors stretch the wait."""
        delay = self.poll_interval_s
        backoff = self._backoff_initial_s
        while not self._wait(delay):
            try:
                result = self.poll_once()
            except Exception as error:
                # The clock or the writer: not Prometheus's doing, so no back-off.
                _log.error("cost round failed (%s)", type(error).__name__)
                result = "idle"
            if result == "error":
                delay = backoff
                backoff = min(backoff * 2, self._backoff_max_s)
            else:
                delay = self.poll_interval_s
                backoff = self._backoff_initial_s

    def poll_once(self) -> PollResult:
        """One round: query what the enabled cost rules need, then submit the sample."""
        config = self._writer.alert_config()
        session_on, daily_on = config.session_cost.enabled, config.daily_cost.enabled
        if not (session_on or daily_on):
            return "idle"
        since_ms = self._writer.session_cost_since() if session_on else None
        if since_ms is None and not daily_on:
            return "idle"
        now_ms = self._clock()
        sessions = CostSample(observed_at_ms=now_ms, sessions={})
        daily_usd: float | None = None
        try:
            if since_ms is not None:
                query = session_cost_query(self.host, since_ms=since_ms, now_ms=now_ms)
                sessions = self._query(query, now_ms, daily=False)
            if daily_on:
                daily_usd = self._query(daily_cost_query(self.host), now_ms, daily=True).daily_usd
        except _QueryError as error:
            self._count(error.reason)
            _log.warning("prometheus cost query failed (%s)", error)
            return "error"
        self._writer.submit_cost_sample(
            CostSample(observed_at_ms=now_ms, sessions=sessions.sessions, daily_usd=daily_usd)
        )
        return "ok"

    def _query(self, query: str, now_ms: int, *, daily: bool) -> CostSample:
        """One instant query's answer as a sample: the sessions' totals, or, ``daily``,
        the day's. Counted ``ok`` here; a failure raises, and the caller counts it."""
        try:
            document = self._get(query, now_ms)
        except _QueryError:
            raise
        except httpx.TimeoutException as error:
            raise _QueryError("timeout", type(error).__name__) from None
        except Exception as error:
            # httpx's other errors: no connection, a broken exchange.
            raise _QueryError("error", type(error).__name__) from None
        if daily:
            parsed = parse_cost_sample(observed_at_ms=now_ms, sessions=_NO_SESSIONS, daily=document)
        else:
            parsed = parse_cost_sample(observed_at_ms=now_ms, sessions=document)
        if parsed is None:
            raise _QueryError("invalid", "unexpected answer")
        self._count("ok")
        return parsed

    def _get(self, query: str, now_ms: int) -> object:
        """The decoded answer, read up to ``max_answer_bytes`` within ``timeout_s``."""
        deadline = self._monotonic() + self._timeout_s
        params = {"query": query, "time": _time_param(now_ms)}
        with self._client.stream("GET", self._url, params=params) as response:
            if not response.is_success:
                raise _QueryError("error", f"status {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > self._max_answer_bytes:
                    raise _QueryError("invalid", "answer too large")
                if self._monotonic() > deadline:
                    raise _QueryError("timeout", "answer too slow")
        try:
            document: object = json.loads(body)
        except (ValueError, RecursionError) as error:
            # Not JSON: a JSONDecodeError, a UnicodeDecodeError (bytes that are no UTF
            # encoding json.loads detects), or nesting too deep to decode.
            raise _QueryError("invalid", type(error).__name__) from None
        return document

    def _count(self, result: PrometheusQueryResult) -> None:
        if self._on_query is None:
            return
        try:
            self._on_query(result)
        except Exception as error:
            _log.error("prometheus query not counted (%s)", type(error).__name__)
