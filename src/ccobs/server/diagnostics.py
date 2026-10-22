"""The diagnostics route: ``GET /api/v1/diagnostics``, for ``ccobs doctor`` and ``ccobs
status`` (ADR 0038).

The CLI runs on the host, and Loki and Prometheus publish no port (ADR 0018), so the app
answers for them on the port it already publishes. One read-only JSON answer:

- ``version``, ``host`` (the host label) and ``started_at_ms``, when the app started (by
  the wall clock), since the counters below count from then;
- ``sessions``: how many sessions each status has, every status of the state machine
  (ADR 0024) listed, and the open blocks, read from SQLite through the read gate like
  every API read (ADR 0033), so a saturated gate answers 503 like the other routes;
- ``hooks``: the in-process counters that feed the app's hook events received and
  dropped metrics (ADR 0028), every event label and every drop reason listed.
  ``ccobs doctor``'s own probe of the hook route (an empty JSON object) is dropped by
  the writer as ``invalid``, so each doctor run adds one ``invalid`` drop;
- ``hook_failures``: the hook failure counts of the last hour from Loki
  (``ccobs.domain.diagnostics``), or ``available: false`` with a short ``reason`` when
  Loki gave no usable answer. The route answers 200 either way;
- ``alerts`` (ADR 0046): the rules on (names only), the alerts still on, those fired in
  the last 24 hours by the app's clock, and Discord's delivery: whether a webhook is
  configured, the status that suspended sending (``DiscordSender.suspended_status``),
  the deliveries pending, and those of the last 24 hours that failed or expired. Read
  in the same slot of the read gate as the sessions, so on the same budget; when that
  read fails, ``alerts`` is ``null`` and the rest of the answer stands. A CLI older than
  v0.6.0 ignores the field; a CLI from v0.6.0 on reads an app without it as one that
  predates alerts.

Never a session ID, a path, content or a URL (the webhook's least of all): the answer
holds counts and closed-list names only.

``HookFailuresReader`` asks Loki like the usage reader asks Prometheus
(``ccobs.server.usage``, ADR 0035): one instant ``GET /loki/api/v1/query`` on an
``httpx.AsyncClient`` of its own, on the event loop, bounded by ``timeout_s`` per phase
and for the whole query, read as a stream up to ``max_answer_bytes``, never following a
redirect. It is not behind the read gate, which guards SQLite's connections only, and it
runs after the database read, so a 503 sends no query. Each query is counted on
the app's Loki query counter (``ok`` or ``error``). Logs hold exception type names and
status codes only: never the URL, the query or a body.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Literal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

import ccobs
from ccobs.domain.alerts import ALERT_RULES, AlertConfig, AlertRule
from ccobs.domain.app_metrics import EVENT_LABELS, LokiQueryResult
from ccobs.domain.diagnostics import (
    HOOK_FAILURES_WINDOW_S,
    HookFailures,
    UnavailableReason,
    hook_failures_query,
    parse_hook_failures,
)
from ccobs.server.api import SESSION_STATUSES
from ccobs.server.db.repository import AlertCounts, Repository, SessionCounts
from ccobs.server.discord import DiscordSender
from ccobs.server.ingest import DROP_REASONS, HookQueue
from ccobs.server.reads import ReadGate
from ccobs.server.writer import WriterCounts

__all__ = [
    "ALERTS_WINDOW_MS",
    "HTTP_TIMEOUT_S",
    "MAX_ANSWER_BYTES",
    "QUERY_PATH",
    "AlertsOut",
    "DiagnosticsOut",
    "DiscordOut",
    "HookFailuresReader",
    "HookFailuresUnavailable",
    "UnavailableReason",
    "router",
]

_log = logging.getLogger(__name__)

# Per phase and for the whole query: a Loki on the same machine answers in milliseconds,
# and doctor must not wait long for a part it can do without.
HTTP_TIMEOUT_S: Final = 2.0
# The answer has three series: a few hundred bytes.
MAX_ANSWER_BYTES: Final = 64 * 1024
QUERY_PATH: Final = "/loki/api/v1/query"
# How far back the alerts part counts the alerts fired and the deliveries that failed or
# expired (ADR 0046): the last 24 hours, by the app's clock.
ALERTS_WINDOW_MS: Final = 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class HookFailuresUnavailable:
    """Loki gave no usable answer, for ``reason`` (the set of
    ``ccobs.domain.diagnostics.UnavailableReason``, which the CLI reads back): no
    connection (or a broken exchange), no answer within the timeout, a non-2xx status (a
    redirect included), or an answer that is too large, not JSON or not the vector of
    counts."""

    reason: UnavailableReason


class _QueryError(Exception):
    """A query that got no usable answer; its message is safe to log."""

    def __init__(self, reason: UnavailableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: Final = reason


class HookFailuresReader:
    """Reads the hook failure counts of the last ``window_s`` seconds from Loki.

    ``loki_url`` is ``CCOBS_LOKI_URL`` (no trailing slash) and ``host`` the host label of
    the stream to read. ``transport`` replaces httpx's (tests pass an
    ``httpx.MockTransport``) and ``on_query`` receives the result of every query sent
    (``AppMetrics.count_loki_query``). Build it on the event loop that uses it.
    """

    def __init__(
        self,
        *,
        loki_url: str,
        host: str,
        window_s: int = HOOK_FAILURES_WINDOW_S,
        timeout_s: float = HTTP_TIMEOUT_S,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        on_query: Callable[[LokiQueryResult], None] | None = None,
    ) -> None:
        for name, value in (("timeout_s", timeout_s), ("max_answer_bytes", max_answer_bytes)):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        self.window_s: Final = window_s
        self._query: Final = hook_failures_query(host, window_s)
        self._url: Final = f"{loki_url}{QUERY_PATH}"
        self._timeout_s: Final = timeout_s
        self._max_answer_bytes: Final = max_answer_bytes
        self._on_query = on_query
        # No proxy from the environment: Loki is on the compose network.
        self._client: Final = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, trust_env=False, follow_redirects=False
        )

    @property
    def closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def read(self) -> HookFailures | HookFailuresUnavailable:
        """The counts, or why there are none; the query is counted either way."""
        try:
            async with asyncio.timeout(self._timeout_s):
                document = await self._get()
            counts = parse_hook_failures(document)
            if counts is None:
                raise _QueryError("shape", "unexpected answer")
        except _QueryError as error:
            return self._failed(error.reason, str(error))
        except (TimeoutError, httpx.TimeoutException) as error:
            return self._failed("timeout", type(error).__name__)
        except Exception as error:
            # httpx's other errors: no connection, a broken exchange.
            return self._failed("unreachable", type(error).__name__)
        self._count("ok")
        return counts

    async def _get(self) -> object:
        """The decoded answer, read up to ``max_answer_bytes``."""
        async with self._client.stream("GET", self._url, params={"query": self._query}) as response:
            if not response.is_success:
                raise _QueryError("status", f"status {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_answer_bytes:
                    raise _QueryError("shape", "answer too large")
        try:
            document: object = json.loads(body)
        except (ValueError, RecursionError) as error:
            # Not JSON, not UTF, or nested too deep to decode.
            raise _QueryError("shape", type(error).__name__) from None
        return document

    def _failed(self, reason: UnavailableReason, detail: str) -> HookFailuresUnavailable:
        self._count("error")
        _log.warning("loki diagnostics query failed (%s)", detail)
        return HookFailuresUnavailable(reason=reason)

    def _count(self, result: LokiQueryResult) -> None:
        if self._on_query is None:
            return
        try:
            self._on_query(result)
        except Exception as error:
            _log.error("loki query not counted (%s)", type(error).__name__)


class _Out(BaseModel):
    # Only the declared fields; a validation error never quotes the value.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SessionsOut(_Out):
    """Sessions by status, every status listed, and the open blocks."""

    by_state: dict[str, int]
    open_blocks: int


class HooksOut(_Out):
    """Hooks parsed by event label and hooks dropped by reason, since the app started."""

    received: dict[str, int]
    dropped: dict[str, int]


class HookFailuresOut(_Out):
    """Over the last ``window_s`` seconds, from Loki."""

    available: Literal[True]
    window_s: int
    executions: int
    non_blocking_error: int
    cancelled: int


class HookFailuresUnavailableOut(_Out):
    """Loki gave no usable answer, for ``reason``."""

    available: Literal[False]
    reason: UnavailableReason
    window_s: int


class DiscordOut(_Out):
    """Discord delivery (ADR 0046): whether a webhook is configured (never its URL), the
    status that suspended sending, the deliveries pending, and those of the last 24
    hours that failed or expired."""

    configured: bool
    suspended_status: int | None
    pending: int
    failed_24h: int
    expired_24h: int


class AlertsOut(_Out):
    """The rules on (names only), the alerts still on, those fired in the last 24
    hours, and Discord's delivery."""

    rules: list[AlertRule]
    open: int
    fired_24h: int
    discord: DiscordOut


class DiagnosticsOut(_Out):
    version: str
    host: str
    started_at_ms: int
    sessions: SessionsOut
    hooks: HooksOut
    hook_failures: HookFailuresOut | HookFailuresUnavailableOut
    # None when the alerts could not be read; the rest of the answer stands.
    alerts: AlertsOut | None


# Async, like the API's dependencies: FastAPI runs a sync dependency on its threadpool.
async def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.read_repository
    return repository


async def _reads(request: Request) -> ReadGate:
    reads: ReadGate = request.app.state.reads
    return reads


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]

router = APIRouter(prefix="/api/v1")


def _sessions(counts: SessionCounts) -> SessionsOut:
    return SessionsOut(
        by_state={status: counts.by_state.get(status, 0) for status in SESSION_STATUSES},
        open_blocks=counts.open_blocks,
    )


def _hooks(writer: WriterCounts, queue: HookQueue) -> HooksOut:
    # Each ccobs doctor run probes the hook URL with POST {} (ADR 0011), which the writer
    # drops as invalid: one more "invalid" drop per doctor run.
    drops = queue.drops
    return HooksOut(
        received={event: writer.events_received.get(event, 0) for event in EVENT_LABELS},
        dropped={reason: drops[reason] for reason in DROP_REASONS},
    )


def _failures(
    answer: HookFailures | HookFailuresUnavailable, window_s: int
) -> HookFailuresOut | HookFailuresUnavailableOut:
    if isinstance(answer, HookFailuresUnavailable):
        return HookFailuresUnavailableOut(available=False, reason=answer.reason, window_s=window_s)
    return HookFailuresOut(
        available=True,
        window_s=window_s,
        executions=answer.executions,
        non_blocking_error=answer.non_blocking_error,
        cancelled=answer.cancelled,
    )


def _counts(
    repository: Repository, host: str, since_ms: int
) -> tuple[SessionCounts, AlertCounts | None]:
    """Both reads in one slot of the read gate, on one budget. The alert counts are an
    extra: when they fail, the route still answers, with ``alerts: null``."""
    sessions = repository.session_counts()
    try:
        alerts = repository.alert_counts(host, since_ms=since_ms)
    except Exception as error:
        # The type only: a database error's text can quote the statement.
        _log.warning("alerts diagnostics read failed (%s)", type(error).__name__)
        alerts = None
    return sessions, alerts


def _rules_on(config: AlertConfig) -> list[AlertRule]:
    enabled: Mapping[AlertRule, bool] = {
        "waiting_user": config.waiting_user.enabled,
        "repeated_failures": config.repeated_failures.enabled,
        "session_cost": config.session_cost.enabled,
        "daily_cost": config.daily_cost.enabled,
    }
    return [rule for rule in ALERT_RULES if enabled[rule]]


def _alerts(
    counts: AlertCounts | None, config: AlertConfig, sender: DiscordSender | None
) -> AlertsOut | None:
    if counts is None:
        return None
    return AlertsOut(
        rules=_rules_on(config),
        open=counts.open,
        fired_24h=counts.fired,
        discord=DiscordOut(
            configured=sender is not None,
            suspended_status=sender.suspended_status if sender is not None else None,
            pending=counts.pending,
            failed_24h=counts.failed,
            expired_24h=counts.expired,
        ),
    )


@router.get("/diagnostics")
async def get_diagnostics(request: Request, repository: _Repo, reads: _Reads) -> DiagnosticsOut:
    """Session counts, hook counters, the last hour's hook failures from Loki, and the
    alerts with their Discord delivery."""
    state = request.app.state
    host: str = state.host_label
    now_ms: int = state.clock()
    # First: a busy database answers 503 before any query goes to Loki.
    counts, alert_counts = await reads.run(_counts, repository, host, now_ms - ALERTS_WINDOW_MS)
    hooks = _hooks(state.writer.counts(), state.queue)
    alerts = _alerts(alert_counts, state.writer.alert_config(), state.discord)
    reader: HookFailuresReader = state.hook_failures
    failures = await reader.read()
    return DiagnosticsOut(
        version=ccobs.__version__,
        host=host,
        started_at_ms=state.started_at_ms,
        sessions=_sessions(counts),
        hooks=hooks,
        hook_failures=_failures(failures, reader.window_s),
        alerts=alerts,
    )
