"""Links from a session to Grafana: its events in Explore on Loki, its tokens and cost in
Explore on Prometheus, and the provisioned overview dashboard over its time range.

Facts the links rely on, each checked by ``tests/test_domain_grafana_links.py``:

- the datasource UIDs ``prometheus`` and ``loki``, fixed by
  ``stack/grafana/provisioning/datasources/datasources.yaml`` (ADR 0017);
- the dashboard UID ``claude-code-overview`` and its variables ``host`` and ``model``
  (``stack/grafana/dashboards/claude-code-overview.json``); the links set ``host`` and
  leave ``model`` at its default, All;
- Loki's index labels ``service_name`` and ``host`` and the ``session_id`` structured
  metadata key, and the Prometheus names and labels, from ``ccobs.domain.telemetry``
  (ADRs 0014, 0015 and 0022); every value is quoted with ``telemetry.quote``, so a
  session ID or host cannot leave its LogQL or PromQL string;
- Grafana's line limit for a Loki query: Explore's frontend asks for the datasource's
  ``maxLines``, 1000 by default (the provisioned Loki datasource does not set it), while
  a query that names none gets the backend's 100 lines; the Logs link names
  ``LOGS_MAX_LINES`` so it shows the same up to 1000 events wherever it runs;
- Grafana 13.2's Explore URL, ``/explore?schemaVersion=1&panes=<JSON>&orgId=1``, each
  pane holding its datasource, its queries and its range (epoch milliseconds as strings,
  or ``now``); the JSON is ASCII-only and the whole query string percent-encoded.

The tokens and cost queries are the dashboard's per-interval form,
``sum by (<label>) (increase(<selector>[$__interval] anchored offset -$__interval))``
(ADR 0013's exact increase over each interval, shifted onto the interval it covers).

Grafana answers only on ``localhost`` (ADR 0017), so the base URL comes from the app's
``CCOBS_GRAFANA_URL`` setting, which the app checks.

Data and string building only: no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote as url_quote
from urllib.parse import urlencode

from ccobs.domain.telemetry import (
    COST_USAGE,
    HOST_LABEL,
    SERVICE_NAME,
    TOKEN_USAGE,
    Metric,
    quote,
    selector,
)

__all__ = [
    "DASHBOARD_UID",
    "LOGS_MAX_LINES",
    "LOKI_UID",
    "MAX_DATA_POINTS",
    "MIN_INTERVAL",
    "NOW",
    "PROMETHEUS_UID",
    "SERVICE_NAME_LABEL",
    "SESSION_ID_KEY",
    "WINDOW_AFTER_MS",
    "WINDOW_BEFORE_MS",
    "SessionLinks",
    "TimeRange",
    "dashboard_url",
    "session_cost_query",
    "session_events_query",
    "session_links",
    "session_logs_url",
    "session_range",
    "session_tokens_query",
    "session_usage_url",
]

# The provisioned datasources and dashboard (stack/grafana).
PROMETHEUS_UID: Final = "prometheus"
LOKI_UID: Final = "loki"
DASHBOARD_UID: Final = "claude-code-overview"
# Grafana's single organization (ADR 0017).
ORG_ID: Final = 1
# Grafana 13.2's Explore URL schema.
EXPLORE_SCHEMA_VERSION: Final = 1
# Loki's stream label for service.name (ADR 0015), and the session's key: structured
# metadata in Loki, a label in Prometheus (ADR 0014).
SERVICE_NAME_LABEL: Final = "service_name"
SESSION_ID_KEY: Final = "session_id"
# The range around a session: a minute before its start, for a clock difference between
# the app and the machine that stamps telemetry, and two minutes after its last moment,
# for the metrics Claude Code exports late (its export interval) and the same clock.
WINDOW_BEFORE_MS: Final = 60_000
WINDOW_AFTER_MS: Final = 120_000
# Grafana's relative time for the current moment: the range of a live session keeps
# growing while its page stays open.
NOW: Final = "now"
# The Logs link's line limit: Grafana's default for Explore on Loki (see the docstring).
# A longer session shows its newest events first; Explore's options raise the limit.
LOGS_MAX_LINES: Final = 1000
# The dashboard's time series panels' minimum interval and data points, so a session's
# chart has the same steps (one per minute at least) as the overview's.
MIN_INTERVAL: Final = "1m"
MAX_DATA_POINTS: Final = 100
# Grafana's interval macro, in the dashboard's per-interval form.
_INTERVAL_FORM: Final = "[$__interval] anchored offset -$__interval"


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A range in epoch milliseconds; ``end_ms`` is ``None`` up to now."""

    start_ms: int
    end_ms: int | None

    @property
    def grafana_from(self) -> str:
        return str(self.start_ms)

    @property
    def grafana_to(self) -> str:
        return NOW if self.end_ms is None else str(self.end_ms)


@dataclass(frozen=True, slots=True)
class SessionLinks:
    """The session page's links: events, tokens and cost, and the dashboard."""

    logs: str
    usage: str
    dashboard: str


def session_range(started_at_ms: int, last_ms: int | None) -> TimeRange:
    """The range of a session that started at ``started_at_ms``: from a minute before
    (never before the epoch) to two minutes after ``last_ms``, its last moment, or up to
    now when it is still live (``None``). The range never ends before it starts."""
    start = max(0, started_at_ms - WINDOW_BEFORE_MS)
    if last_ms is None:
        return TimeRange(start_ms=start, end_ms=None)
    return TimeRange(start_ms=start, end_ms=max(start, last_ms + WINDOW_AFTER_MS))


def session_events_query(host: str, session_id: str) -> str:
    """LogQL for the session's events: ``{service_name="claude-code", host="<host>"}
    | session_id="<id>"``. Raises ``ValueError`` for a value that is not valid UTF-8."""
    stream = f"{{{SERVICE_NAME_LABEL}={quote(SERVICE_NAME)}, {HOST_LABEL}={quote(host)}}}"
    return f"{stream} | {SESSION_ID_KEY}={quote(session_id)}"


def _per_interval(metric: Metric, by: str, host: str, session_id: str) -> str:
    matchers = {HOST_LABEL: host, SESSION_ID_KEY: session_id}
    return f"sum by ({by}) (increase({selector(metric, matchers)}{_INTERVAL_FORM}))"


def session_tokens_query(host: str, session_id: str) -> str:
    """PromQL for the session's tokens in each interval, by type."""
    return _per_interval(TOKEN_USAGE, "type", host, session_id)


def session_cost_query(host: str, session_id: str) -> str:
    """PromQL for the session's cost in each interval, by model."""
    return _per_interval(COST_USAGE, "model", host, session_id)


def _base(grafana_url: str) -> str:
    return grafana_url.rstrip("/")


def _query_string(params: Sequence[tuple[str, object]]) -> str:
    # Every reserved character percent-encoded, "/" included.
    return urlencode([(key, str(value)) for key, value in params], quote_via=url_quote, safe="")


def _pane(datasource: str, query: Mapping[str, object], time_range: TimeRange) -> dict[str, object]:
    return {
        "datasource": datasource,
        "queries": [{"refId": "A", **query}],
        "range": {"from": time_range.grafana_from, "to": time_range.grafana_to},
    }


def _explore(grafana_url: str, panes: Mapping[str, object]) -> str:
    # ASCII-only JSON: any other character is a \u escape before percent-encoding.
    encoded = json.dumps(panes, ensure_ascii=True, separators=(",", ":"))
    params = [("schemaVersion", EXPLORE_SCHEMA_VERSION), ("panes", encoded), ("orgId", ORG_ID)]
    return f"{_base(grafana_url)}/explore?{_query_string(params)}"


def _prometheus_query(expr: str, legend: str) -> dict[str, object]:
    return {
        "datasource": {"type": "prometheus", "uid": PROMETHEUS_UID},
        "expr": expr,
        "range": True,
        "instant": False,
        "editorMode": "code",
        "legendFormat": legend,
        "interval": MIN_INTERVAL,
        "maxDataPoints": MAX_DATA_POINTS,
    }


def session_logs_url(grafana_url: str, *, host: str, session_id: str, time_range: TimeRange) -> str:
    """Explore on Loki with the session's events over ``time_range``."""
    query = {
        "datasource": {"type": "loki", "uid": LOKI_UID},
        "expr": session_events_query(host, session_id),
        "queryType": "range",
        "editorMode": "code",
        "maxLines": LOGS_MAX_LINES,
    }
    return _explore(grafana_url, {"logs": _pane(LOKI_UID, query, time_range)})


def session_usage_url(
    grafana_url: str, *, host: str, session_id: str, time_range: TimeRange
) -> str:
    """Explore on Prometheus, split in two panes: the session's tokens by type, and its
    cost by model (each on its own scale)."""
    tokens = _prometheus_query(session_tokens_query(host, session_id), "{{type}}")
    cost = _prometheus_query(session_cost_query(host, session_id), "{{model}}")
    panes = {
        "tokens": _pane(PROMETHEUS_UID, tokens, time_range),
        "cost": _pane(PROMETHEUS_UID, cost, time_range),
    }
    return _explore(grafana_url, panes)


def dashboard_url(grafana_url: str, *, host: str, time_range: TimeRange | None = None) -> str:
    """The overview dashboard for ``host``, over ``time_range`` or its own default."""
    params: list[tuple[str, object]] = [("orgId", ORG_ID)]
    if time_range is not None:
        params += [("from", time_range.grafana_from), ("to", time_range.grafana_to)]
    params.append((f"var-{HOST_LABEL}", host))
    return f"{_base(grafana_url)}/d/{DASHBOARD_UID}?{_query_string(params)}"


def session_links(
    grafana_url: str, *, host: str, session_id: str, time_range: TimeRange
) -> SessionLinks:
    """The three links of a session page."""
    return SessionLinks(
        logs=session_logs_url(grafana_url, host=host, session_id=session_id, time_range=time_range),
        usage=session_usage_url(
            grafana_url, host=host, session_id=session_id, time_range=time_range
        ),
        dashboard=dashboard_url(grafana_url, host=host, time_range=time_range),
    )
