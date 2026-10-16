"""The reconciler: closes permission blocks denied with Esc, from Loki (ADR 0029).

A dialog denied with Esc fires no closing hook (ADR 0024), so its block would stay open,
and the session ``waiting_user``, until the next progress event. Claude Code's telemetry
records the denial as a ``tool_decision`` reject, which reaches Loki through the
collector (ADR 0015). While permission blocks are open, the reconciler reads those
rejections back and submits a close for each match to the writer
(``Writer.submit_telemetry_close``), which applies ``close_block_denied`` on its own
thread (ADR 0026, one writer).

Every ``poll_interval_s`` it asks the writer for its open permission blocks
(``Writer.open_permission_blocks``, read under the writer's lock) and keeps those open
for at least ``grace_s``, which gives the closing hook its chance first, and at most
``horizon_s``, which bounds the query's window; older blocks are left to the hooks and
the stale sweep. With none, it sends nothing. Otherwise it sends one
``GET /loki/api/v1/query_range`` for the newest ``tool_decision`` rejects of the host's
stream (``ccobs.domain.denials``), bounded from the oldest due block's opening to now,
both widened by ``margin_s`` for clock differences, and closes each due block whose
``session_id`` and ``tool_use_id`` a rejection names. The query carries no session ID,
so Loki's info-level query log never records one. The answer is read as a stream and
given up past ``max_answer_bytes``.

Like the sweeper (``ccobs.server.sweeps``), it runs on a daemon thread and waits on a
stop ``Event``, so ``stop`` interrupts the wait at once. A query in flight is not cut
short: the client's timeout applies to each phase (connecting, and each read or write
of the exchange), not to the whole request. When Loki cannot be reached or answers
anything but a streams result, the round counts an ``error`` and the next wait doubles, from
``backoff_initial_s`` up to ``backoff_max_s``, until a round succeeds or has nothing to
ask. Nothing stops the loop. Logs hold exception type names, status codes and counts
only: never the URL (with its query), a session or call ID, or a response body. httpx
logs each request with its URL at INFO, below the WARNING that the root logger keeps for
libraries (``ccobs.server.__main__``).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Final, Literal, Protocol

import httpx

from ccobs.domain.app_metrics import LokiQueryResult
from ccobs.domain.denials import (
    CATEGORIZE_LABELS,
    ENCODING_FLAGS_HEADER,
    OpenBlock,
    due_blocks,
    match_denials,
    parse_rejections,
    query_window_ns,
    rejections_query,
)
from ccobs.server.ingest import Clock, wall_clock_ms

__all__ = [
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "GRACE_S",
    "HORIZON_S",
    "HTTP_TIMEOUT_S",
    "MAX_ANSWER_BYTES",
    "POLL_INTERVAL_S",
    "QUERY_LIMIT",
    "QUERY_RANGE_PATH",
    "WINDOW_MARGIN_S",
    "PollResult",
    "ReconcileTarget",
    "Reconciler",
]

_log = logging.getLogger(__name__)

# How often the open blocks are checked: a denied dialog shows as closed within about
# this, plus the grace period and Claude Code's 1 to 5 s export interval.
POLL_INTERVAL_S: Final = 2.0
# How long a block stays open before Loki is asked: a dialog answered at the keyboard
# closes by its hook within this, and costs no query.
GRACE_S: Final = 2.0
# How long a block stays due: a reject exported later than this is ignored, and the
# block waits for its next hook or the stale sweep. Keeps the window at most about
# HORIZON_S + 2 * WINDOW_MARGIN_S (35 min) however long a block stays open.
HORIZON_S: Final = 900.0
# Widens the window on both sides for a clock difference between the app's container
# and the machine that stamps the events (Docker Desktop's VM clock can drift).
WINDOW_MARGIN_S: Final = 600.0
# Rejections in one answer: far more than one machine produces in a window. The query
# reads newest first, so a hit limit drops the oldest rejects.
QUERY_LIMIT: Final = 1000
# Per phase (connect, each read, each write, a pool wait), not for the whole request.
HTTP_TIMEOUT_S: Final = 5.0
# The largest answer read: 1000 entries fit well within, since the largest fixture
# record has about 1.3 KB of attributes (ADR 0015).
MAX_ANSWER_BYTES: Final = 8 * 1024 * 1024
BACKOFF_INITIAL_S: Final = 2.0
BACKOFF_MAX_S: Final = 60.0
QUERY_RANGE_PATH: Final = "/loki/api/v1/query_range"
_MS_PER_S: Final = 1000

# idle: nothing to ask, no query sent; ok and error: a query was sent.
type PollResult = Literal["idle", "ok", "error"]


class ReconcileTarget(Protocol):
    """What the reconciler needs of the writer."""

    def open_permission_blocks(self) -> list[OpenBlock]: ...

    def submit_telemetry_close(self, session_id: str, tool_use_id: str, at_ms: int) -> None: ...


class _QueryError(Exception):
    """A query that got no streams result; its message is safe to log."""


class Reconciler:
    """Closes Esc-denied permission blocks from Loki's ``tool_decision`` rejects.

    ``loki_url`` is ``CCOBS_LOKI_URL`` (no trailing slash) and ``host`` the host label of
    the stream to read. ``clock`` gives the wall-clock time in ms, the same clock as the
    hook route, so that block openings and now compare. ``wait(timeout_s)`` waits between
    rounds and returns True to stop; by default it waits on the stop event.
    ``transport`` replaces httpx's (tests pass an ``httpx.MockTransport``), and
    ``on_query`` receives the result of every query sent (``AppMetrics.count_loki_query``).
    ``run`` is the loop and ``poll_once`` one round; ``start`` runs the loop on a daemon
    thread, and ``stop`` ends it and closes the HTTP client.
    """

    def __init__(
        self,
        writer: ReconcileTarget,
        *,
        loki_url: str,
        host: str,
        poll_interval_s: float = POLL_INTERVAL_S,
        grace_s: float = GRACE_S,
        horizon_s: float = HORIZON_S,
        margin_s: float = WINDOW_MARGIN_S,
        limit: int = QUERY_LIMIT,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        timeout_s: float = HTTP_TIMEOUT_S,
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        clock: Clock = wall_clock_ms,
        wait: Callable[[float], bool] | None = None,
        transport: httpx.BaseTransport | None = None,
        on_query: Callable[[LokiQueryResult], None] | None = None,
    ) -> None:
        for name, value in (
            ("poll_interval_s", poll_interval_s),
            ("grace_s", grace_s),
            ("horizon_s", horizon_s),
            ("margin_s", margin_s),
            ("timeout_s", timeout_s),
            ("backoff_initial_s", backoff_initial_s),
            ("backoff_max_s", backoff_max_s),
            ("limit", limit),
            ("max_answer_bytes", max_answer_bytes),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if not horizon_s > grace_s:
            raise ValueError(f"horizon_s ({horizon_s}) must exceed grace_s ({grace_s})")
        self.loki_url: Final = loki_url
        self.query: Final = rejections_query(host)
        self.poll_interval_s: Final = poll_interval_s
        self._grace_ms: Final = int(grace_s * _MS_PER_S)
        self._horizon_ms: Final = int(horizon_s * _MS_PER_S)
        self._max_answer_bytes: Final = max_answer_bytes
        # Whether the last answer filled the limit: warned once on the way in.
        self._limited = False
        self._margin_ms: Final = int(margin_s * _MS_PER_S)
        self._limit: Final = limit
        self._backoff_initial_s: Final = backoff_initial_s
        self._backoff_max_s: Final = backoff_max_s
        self._writer = writer
        self._clock = clock
        self._on_query = on_query
        self._url: Final = f"{loki_url}{QUERY_RANGE_PATH}"
        # No proxy from the environment: Loki is on the compose network.
        self._client: Final = httpx.Client(
            timeout=timeout_s, transport=transport, trust_env=False, follow_redirects=False
        )
        self._stopping = threading.Event()
        self._wait: Callable[[float], bool] = wait or self._stopping.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run the loop on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError("the reconciler is already started")
        self._thread = threading.Thread(target=self.run, name="ccobs-reconciler", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float) -> None:
        """Stop the loop, interrupting its wait, join the thread for ``timeout_s`` and
        close the HTTP client."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                _log.warning("reconciler did not stop within %.1f s", timeout_s)
        self._client.close()

    def run(self) -> None:
        """Wait, poll, repeat, until ``wait`` says stop; errors stretch the wait."""
        delay = self.poll_interval_s
        backoff = self._backoff_initial_s
        while not self._wait(delay):
            try:
                result = self.poll_once()
            except Exception as error:
                # The clock or the writer: not Loki's doing, so no back-off.
                _log.error("reconcile round failed (%s)", type(error).__name__)
                result = "idle"
            if result == "error":
                delay = backoff
                backoff = min(backoff * 2, self._backoff_max_s)
            else:
                delay = self.poll_interval_s
                backoff = self._backoff_initial_s

    def poll_once(self) -> PollResult:
        """One round: query Loki if a permission block is due, then submit the matches."""
        now_ms = self._clock()
        due = due_blocks(
            self._writer.open_permission_blocks(),
            now_ms=now_ms,
            grace_ms=self._grace_ms,
            horizon_ms=self._horizon_ms,
        )
        if not due:
            return "idle"
        start_ns, end_ns = query_window_ns(due, now_ms=now_ms, margin_ms=self._margin_ms)
        try:
            document = self._get(start_ns, end_ns)
            parsed = parse_rejections(document)
            if not parsed.valid:
                raise _QueryError("unexpected answer")
        except _QueryError as error:
            return self._failed(str(error))
        except Exception as error:
            # httpx's errors (connection, timeout, ...) and a body that is not JSON.
            return self._failed(type(error).__name__)
        self._count("ok")
        if parsed.skipped:
            _log.warning("loki answer: %d malformed entries skipped", parsed.skipped)
        limited = parsed.entries >= self._limit
        if limited and not self._limited:
            _log.warning(
                "loki answer hit its limit of %d entries; the oldest rejects are left out",
                self._limit,
            )
        self._limited = limited
        for close in match_denials(parsed.rejections, due, now_ms=now_ms):
            self._writer.submit_telemetry_close(close.session_id, close.tool_use_id, close.at_ms)
        return "ok"

    def _get(self, start_ns: int, end_ns: int) -> object:
        """The decoded answer, read up to ``max_answer_bytes``."""
        with self._client.stream(
            "GET",
            self._url,
            params={
                "query": self.query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(self._limit),
                # Newest first: a hit limit drops the oldest rejects.
                "direction": "backward",
            },
            headers={ENCODING_FLAGS_HEADER: CATEGORIZE_LABELS},
        ) as response:
            if not response.is_success:
                raise _QueryError(f"status {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > self._max_answer_bytes:
                    raise _QueryError("answer too large")
        document: object = json.loads(body)
        return document

    def _failed(self, reason: str) -> PollResult:
        self._count("error")
        _log.warning("loki query failed (%s)", reason)
        return "error"

    def _count(self, result: LokiQueryResult) -> None:
        if self._on_query is None:
            return
        try:
            self._on_query(result)
        except Exception as error:
            _log.error("loki query not counted (%s)", type(error).__name__)
