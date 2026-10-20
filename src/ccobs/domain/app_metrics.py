"""The names of the app's own metrics, served on its ``/metrics`` listener (ADR 0028).

Prometheus scrapes these series from the app, so, unlike Claude Code's metrics (ADR
0014), they are stored under the names below without translation. The app's metrics
module, the Grafana dashboards (milestone 6) and ``ccobs doctor`` (milestone 4) take
names, labels and help texts from here; no metric name is spelled out anywhere else.

The cardinality is fixed: every series has the host label (ADR 0022), every other label
takes values from a closed list below, and no series has a ``session_id`` label, ever.
Per-session data lives in the app's database and in Claude Code's own metrics (ADR 0016).

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

from ccobs.domain.hook_config import ASYNC_COMMAND_EVENTS, REGISTERED_HTTP_EVENTS
from ccobs.domain.sessions import BlockKind, SessionStatus
from ccobs.domain.telemetry import HOST_LABEL

__all__ = [
    "AGENTS_RUNNING",
    "ALERTS_FIRED",
    "APP_METRICS",
    "BLOCKS_OPEN",
    "BLOCK_DURATION",
    "BLOCK_DURATION_BUCKETS",
    "BLOCK_KINDS",
    "BUILD_INFO",
    "DATABASE_WAL",
    "EVENT_LABEL",
    "EVENT_LABELS",
    "HOOK_EVENTS_DROPPED",
    "HOOK_EVENTS_PROCESSED",
    "HOOK_EVENTS_RECEIVED",
    "HOOK_QUEUE_BYTES",
    "HOOK_QUEUE_DEPTH",
    "KIND_LABEL",
    "LOKI_QUERIES",
    "LOKI_RESULTS",
    "METRICS_PATH",
    "OTHER_EVENT",
    "PROMETHEUS_QUERIES",
    "PROMETHEUS_RESULTS",
    "REASON_LABEL",
    "RECEIVED_EVENTS",
    "RESULT_LABEL",
    "RULE_LABEL",
    "SCRAPE_JOB",
    "SESSIONS",
    "SESSIONS_ENDED",
    "SESSION_STATES",
    "STATE_LABEL",
    "UI_STREAMS",
    "UI_STREAMS_REFUSED",
    "VERSION_LABEL",
    "AppMetric",
    "LokiQueryResult",
    "MetricType",
    "PrometheusQueryResult",
    "event_label",
]

# The only path the metrics listener serves (ADR 0028).
METRICS_PATH: Final = "/metrics"
# The Prometheus scrape job of the app (target app:9464, every 15 s; ADR 0028).
SCRAPE_JOB: Final = "ccobs-app"

STATE_LABEL: Final = "state"
KIND_LABEL: Final = "kind"
EVENT_LABEL: Final = "event"
REASON_LABEL: Final = "reason"
VERSION_LABEL: Final = "version"
RESULT_LABEL: Final = "result"
RULE_LABEL: Final = "rule"

# Every status but ended, the values of the state label: ended sessions leave the
# writer's memory, and the counter of sessions ended stands for them.
SESSION_STATES: Final[tuple[SessionStatus, ...]] = tuple(
    status for status in get_args(SessionStatus.__value__) if status != "ended"
)
BLOCK_KINDS: Final[tuple[BlockKind, ...]] = get_args(BlockKind.__value__)
# The hook events ccobs setup registers (ADR 0025): the values of the event label, plus
# OTHER_EVENT for any other name a payload carries, so a client cannot add series.
RECEIVED_EVENTS: Final[tuple[str, ...]] = (*ASYNC_COMMAND_EVENTS, *REGISTERED_HTTP_EVENTS)
OTHER_EVENT: Final = "other"
EVENT_LABELS: Final[tuple[str, ...]] = (*RECEIVED_EVENTS, OTHER_EVENT)
_RECEIVED: Final = frozenset(RECEIVED_EVENTS)

# Seconds a block stays open, waiting on a person: an answer at the keyboard takes
# seconds, a glance back from another window a minute or two, a return from a break up
# to an hour. Finer below a minute, where the people at their desk are; anything over an
# hour falls in +Inf, which prometheus_client adds. Thirteen series per kind: eleven
# buckets, _sum and _count.
BLOCK_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    1,
    5,
    15,
    30,
    60,
    120,
    300,
    600,
    1800,
    3600,
)

# The result of a query the app sends to Loki, to close Esc denials (ADR 0029) or for the
# diagnostics route's hook failure counts (ADR 0038): a usable answer, or not (no
# connection, a timeout, a non-2xx status, a body that is not the expected result). A
# poll with nothing to ask sends no query and counts nothing.
type LokiQueryResult = Literal["ok", "error"]
LOKI_RESULTS: Final[tuple[LokiQueryResult, ...]] = get_args(LokiQueryResult.__value__)

# The result of a query the app sends to Prometheus for a session's tokens and cost (ADR
# 0035): a valid vector (ok); no connection, a non-2xx status or an error status (error);
# no complete answer within the timeout (timeout); or an answer that is too large, not
# JSON or not a vector of finite, non-negative values (invalid). A session page whose
# totals come from the cache sends no query and counts nothing.
type PrometheusQueryResult = Literal["ok", "error", "timeout", "invalid"]
PROMETHEUS_RESULTS: Final[tuple[PrometheusQueryResult, ...]] = get_args(
    PrometheusQueryResult.__value__
)

type MetricType = Literal["gauge", "counter", "histogram"]


@dataclass(frozen=True, slots=True)
class AppMetric:
    """One series family of the app: its stored name (a counter's ends in ``_total``, a
    histogram's series add ``_bucket``, ``_sum`` and ``_count``), type, help text and
    labels, the host label first (a histogram's ``le`` is not listed)."""

    name: str
    type: MetricType
    help: str
    labels: tuple[str, ...]


SESSIONS: Final = AppMetric(
    "ccobs_sessions",
    "gauge",
    "Sessions that have not ended, by state.",
    (HOST_LABEL, STATE_LABEL),
)
SESSIONS_ENDED: Final = AppMetric(
    "ccobs_sessions_ended_total",
    "counter",
    "Sessions that moved to ended since the app started.",
    (HOST_LABEL,),
)
BLOCKS_OPEN: Final = AppMetric(
    "ccobs_blocks_open",
    "gauge",
    "Open blocks (sessions waiting on a person), by kind.",
    (HOST_LABEL, KIND_LABEL),
)
AGENTS_RUNNING: Final = AppMetric(
    "ccobs_agents_running",
    "gauge",
    "Running agents and subagents, in sessions that are neither ended nor stale.",
    (HOST_LABEL,),
)
BLOCK_DURATION: Final = AppMetric(
    "ccobs_block_duration_seconds",
    "histogram",
    "How long each closed block stayed open, by kind.",
    (HOST_LABEL, KIND_LABEL),
)
HOOK_EVENTS_PROCESSED: Final = AppMetric(
    "ccobs_hook_events_processed_total",
    "counter",
    "Hook events stored in the database since the app started.",
    (HOST_LABEL,),
)
HOOK_EVENTS_RECEIVED: Final = AppMetric(
    "ccobs_hook_events_received_total",
    "counter",
    "Hook payloads parsed by the writer since the app started, by event"
    " (other for events ccobs setup does not register).",
    (HOST_LABEL, EVENT_LABEL),
)
HOOK_EVENTS_DROPPED: Final = AppMetric(
    "ccobs_hook_events_dropped_total",
    "counter",
    "Hooks dropped instead of stored since the app started, by reason.",
    (HOST_LABEL, REASON_LABEL),
)
HOOK_QUEUE_DEPTH: Final = AppMetric(
    "ccobs_hook_queue_depth",
    "gauge",
    "Hooks received and not yet handled by the writer.",
    (HOST_LABEL,),
)
HOOK_QUEUE_BYTES: Final = AppMetric(
    "ccobs_hook_queue_bytes",
    "gauge",
    "Body bytes of the hooks received and not yet handled by the writer.",
    (HOST_LABEL,),
)
BUILD_INFO: Final = AppMetric(
    "ccobs_build_info",
    "gauge",
    "Always 1; the version label is the running cc-observatory version.",
    (HOST_LABEL, VERSION_LABEL),
)
DATABASE_WAL: Final = AppMetric(
    "ccobs_database_wal",
    "gauge",
    "1 if the SQLite database runs in WAL mode, 0 if its filesystem refused WAL.",
    (HOST_LABEL,),
)

LOKI_QUERIES: Final = AppMetric(
    "ccobs_loki_queries_total",
    "counter",
    "Queries sent to Loki (to close permission blocks denied with Esc, and for the"
    " diagnostics route) since the app started, by result.",
    (HOST_LABEL, RESULT_LABEL),
)

PROMETHEUS_QUERIES: Final = AppMetric(
    "ccobs_prometheus_queries_total",
    "counter",
    "Queries sent to Prometheus for the tokens and cost of a session page since the app"
    " started, by result.",
    (HOST_LABEL, RESULT_LABEL),
)

# The web UI's live update streams (ADR 0032): how many are open, and how many were
# refused because the cap of open streams was reached.
UI_STREAMS: Final = AppMetric(
    "ccobs_ui_streams",
    "gauge",
    "Live update streams (server-sent events) open to the web UI.",
    (HOST_LABEL,),
)
UI_STREAMS_REFUSED: Final = AppMetric(
    "ccobs_ui_streams_refused_total",
    "counter",
    "Live update streams refused because the cap of open streams was reached, since the"
    " app started.",
    (HOST_LABEL,),
)

# The alerts that fired since the app started (ADR 0041), by rule: every rule of
# ALERT_RULES is exported, with zeros. A fire is counted once its row is committed.
ALERTS_FIRED: Final = AppMetric(
    "ccobs_alerts_fired_total",
    "counter",
    "Alerts that fired since the app started, by rule.",
    (HOST_LABEL, RULE_LABEL),
)

APP_METRICS: Final = (
    SESSIONS,
    SESSIONS_ENDED,
    BLOCKS_OPEN,
    AGENTS_RUNNING,
    BLOCK_DURATION,
    HOOK_EVENTS_PROCESSED,
    HOOK_EVENTS_RECEIVED,
    HOOK_EVENTS_DROPPED,
    HOOK_QUEUE_DEPTH,
    HOOK_QUEUE_BYTES,
    BUILD_INFO,
    DATABASE_WAL,
    LOKI_QUERIES,
    PROMETHEUS_QUERIES,
    UI_STREAMS,
    UI_STREAMS_REFUSED,
    ALERTS_FIRED,
)


def event_label(name: str) -> str:
    """The event label of a hook named ``name``: itself if registered, else ``other``."""
    return name if name in _RECEIVED else OTHER_EVENT
