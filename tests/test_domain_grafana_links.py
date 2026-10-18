"""Unit tests of ``ccobs.domain.grafana_links``: the links from a session to Grafana.

Each URL is decoded back: the Explore links to their ``panes`` JSON (Grafana 13.2's
schema, ``schemaVersion=1``), with the LogQL and PromQL they carry, and the dashboard
link to its parameters. The datasource UIDs, the dashboard UID and its variables are
read from the provisioned files under ``stack/grafana``, and the names in the queries
from ``ccobs.domain.telemetry`` (ADR 0014), so a change on either side fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
import yaml

from ccobs.domain import grafana_links
from ccobs.domain.grafana_links import (
    DASHBOARD_UID,
    LOKI_UID,
    MAX_DATA_POINTS,
    MIN_INTERVAL,
    NOW,
    PROMETHEUS_UID,
    WINDOW_AFTER_MS,
    WINDOW_BEFORE_MS,
    SessionLinks,
    TimeRange,
    dashboard_url,
    session_cost_query,
    session_events_query,
    session_links,
    session_logs_url,
    session_range,
    session_tokens_query,
    session_usage_url,
)
from ccobs.domain.telemetry import (
    COST_USAGE,
    HOST_LABEL,
    LOKI_INDEX_LABELS,
    LOKI_METADATA,
    SERVICE_NAME,
    TOKEN_USAGE,
    Metric,
)

ROOT: Final = Path(__file__).resolve().parents[1]
GRAFANA: Final = ROOT / "stack" / "grafana"
DATASOURCES: Final = GRAFANA / "provisioning" / "datasources" / "datasources.yaml"
DASHBOARD: Final = GRAFANA / "dashboards" / "claude-code-overview.json"
BASE: Final = "http://localhost:4330"
HOST: Final = "local"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
STARTED: Final = 1_800_000_000_000
ENDED: Final = STARTED + 3_600_000
RANGE: Final = TimeRange(start_ms=STARTED - WINDOW_BEFORE_MS, end_ms=ENDED + WINDOW_AFTER_MS)
# Values that would break out of a LogQL or PromQL string, a JSON string or a URL if
# they were not escaped at every level.
HOSTILE: Final = [
    'x"} or vector(1) #',
    "back\\slash",
    'quote" | line_format "{{.prompt}}',
    "new\nline",
    "tab\there",
    "amp&orgId=2#frag",
    "percent%22%7D",
    "space and +plus",
    "unicode-é-中-\U0001f600",
    "�-replacement",
]


def _decoded(url: str) -> tuple[str, dict[str, list[str]]]:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qs(
        parts.query, keep_blank_values=True, strict_parsing=True
    )


def _panes(url: str) -> dict[str, Any]:
    where, query = _decoded(url)
    assert where == f"{BASE}/explore"
    assert query["schemaVersion"] == ["1"]
    assert query["orgId"] == ["1"]
    assert set(query) == {"schemaVersion", "panes", "orgId"}
    panes = json.loads(query["panes"][0])
    assert isinstance(panes, dict)
    return panes


def _go_unquote(literal: str) -> str:
    """A double-quoted LogQL or PromQL string literal read back (Go's quoting, which
    ``telemetry.quote`` writes); JSON reads the same escapes except ``\\xNN``."""
    assert literal.startswith('"'), literal
    assert literal.endswith('"'), literal
    inner = re.sub(
        r"\\(x[0-9a-f]{2}|.)",
        lambda match: "\\u00" + match[1][1:] if match[1].startswith("x") else match[0],
        literal[1:-1],
    )
    value = json.loads(f'"{inner}"')
    assert isinstance(value, str)
    return value


# The facts the module relies on, read from the provisioned files and ADR 0014.


def test_the_datasource_uids_are_the_provisioned_ones() -> None:
    provisioned = yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))["datasources"]
    uids = {item["type"]: item["uid"] for item in provisioned}
    assert uids["prometheus"] == PROMETHEUS_UID
    assert uids["loki"] == LOKI_UID


def test_the_dashboard_uid_and_variables_are_the_provisioned_ones() -> None:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert dashboard["uid"] == DASHBOARD_UID
    variables = {item["name"] for item in dashboard["templating"]["list"]}
    # host is set from the app's label; model keeps its default (All).
    assert variables == {"host", "model"}


@pytest.mark.parametrize("metric", [TOKEN_USAGE, COST_USAGE], ids=lambda metric: metric.name)
def test_the_usage_panes_step_like_the_dashboards_panels(metric: Metric) -> None:
    # The time series panels that chart the same metric per interval.
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panels = [
        panel
        for panel in dashboard["panels"]
        if panel.get("type") == "timeseries"
        and any(f"{metric.name}{{" in target.get("expr", "") for target in panel.get("targets", []))
    ]
    assert panels
    for panel in panels:
        assert panel["interval"] == MIN_INTERVAL, panel["title"]
        assert panel["maxDataPoints"] == MAX_DATA_POINTS, panel["title"]


def test_the_queries_use_the_stored_names_of_adr_0014() -> None:
    assert {grafana_links.SERVICE_NAME_LABEL, HOST_LABEL} == LOKI_INDEX_LABELS
    assert grafana_links.SESSION_ID_KEY in LOKI_METADATA
    assert grafana_links.SESSION_ID_KEY in TOKEN_USAGE.labels
    assert grafana_links.SESSION_ID_KEY in COST_USAGE.labels
    assert "type" in TOKEN_USAGE.labels
    assert "model" in COST_USAGE.labels


# The time range.


def test_an_ended_session_spans_a_minute_before_to_two_minutes_after() -> None:
    assert session_range(STARTED, ENDED) == RANGE
    assert WINDOW_BEFORE_MS == 60_000
    assert WINDOW_AFTER_MS == 120_000


def test_a_live_session_runs_to_now() -> None:
    window = session_range(STARTED, None)
    assert window == TimeRange(start_ms=STARTED - WINDOW_BEFORE_MS, end_ms=None)
    assert window.grafana_from == str(STARTED - WINDOW_BEFORE_MS)
    assert window.grafana_to == NOW == "now"


def test_the_range_never_starts_before_the_epoch_or_ends_before_it_starts() -> None:
    assert session_range(10, 20) == TimeRange(start_ms=0, end_ms=20 + WINDOW_AFTER_MS)
    # A last moment before the start (a clock step back) still gives a forward range.
    window = session_range(STARTED, STARTED - 10 * WINDOW_AFTER_MS)
    assert window.end_ms is not None
    assert window.end_ms >= window.start_ms


def test_the_range_is_written_in_epoch_milliseconds() -> None:
    assert RANGE.grafana_from == str(STARTED - WINDOW_BEFORE_MS)
    assert RANGE.grafana_to == str(ENDED + WINDOW_AFTER_MS)


# The queries.


def test_the_events_query_selects_the_hosts_stream_and_filters_the_session() -> None:
    assert session_events_query(HOST, SID) == (
        f'{{service_name="{SERVICE_NAME}", host="{HOST}"}} | session_id="{SID}"'
    )


def test_the_tokens_query_sums_each_interval_by_type_like_the_dashboard() -> None:
    assert session_tokens_query(HOST, SID) == (
        "sum by (type) (increase(claude_code_token_usage_tokens_total"
        f'{{host="{HOST}",session_id="{SID}"}}[$__interval] anchored offset -$__interval))'
    )


def test_the_cost_query_sums_each_interval_by_model_like_the_dashboard() -> None:
    assert session_cost_query(HOST, SID) == (
        "sum by (model) (increase(claude_code_cost_usage_USD_total"
        f'{{host="{HOST}",session_id="{SID}"}}[$__interval] anchored offset -$__interval))'
    )


def test_the_dashboard_uses_the_same_interval_form() -> None:
    # The panels' form, so a session's chart reads like the overview's.
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "[$__interval] anchored offset -$__interval))" in text


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_session_id_stays_one_string_in_every_query(value: str) -> None:
    events = session_events_query(HOST, value)
    prefix = f'{{service_name="{SERVICE_NAME}", host="{HOST}"}} | session_id='
    assert events.startswith(prefix)
    assert _go_unquote(events.removeprefix(prefix)) == value
    for query, metric in (
        (session_tokens_query(HOST, value), TOKEN_USAGE),
        (session_cost_query(HOST, value), COST_USAGE),
    ):
        head = f'{metric.name}{{host="{HOST}",session_id='
        tail = "}[$__interval] anchored offset -$__interval))"
        selector = query[query.index(head) + len(head) : query.rindex(tail)]
        assert _go_unquote(selector) == value
        assert "\n" not in query


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_host_stays_one_string(value: str) -> None:
    # The settings allow only HOST_VALUE_PATTERN; the module quotes it anyway.
    events = session_events_query(value, SID)
    head = f'{{service_name="{SERVICE_NAME}", host='
    tail = f'}} | session_id="{SID}"'
    assert _go_unquote(events[len(head) : events.rindex(tail)]) == value


def test_a_value_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        session_events_query(HOST, "lone-\ud800")
    with pytest.raises(ValueError, match="UTF-8"):
        session_tokens_query(HOST, "lone-\ud800")


# The Explore links.


def test_the_logs_link_opens_explore_on_loki_with_the_session_query() -> None:
    panes = _panes(session_logs_url(BASE, host=HOST, session_id=SID, time_range=RANGE))
    assert panes == {
        "logs": {
            "datasource": LOKI_UID,
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "loki", "uid": LOKI_UID},
                    "expr": session_events_query(HOST, SID),
                    "queryType": "range",
                    "editorMode": "code",
                }
            ],
            "range": {"from": RANGE.grafana_from, "to": RANGE.grafana_to},
        }
    }


def test_the_usage_link_splits_tokens_and_cost_in_two_panes() -> None:
    panes = _panes(session_usage_url(BASE, host=HOST, session_id=SID, time_range=RANGE))
    assert list(panes) == ["tokens", "cost"]
    for key, query, legend in (
        ("tokens", session_tokens_query(HOST, SID), "{{type}}"),
        ("cost", session_cost_query(HOST, SID), "{{model}}"),
    ):
        assert panes[key] == {
            "datasource": PROMETHEUS_UID,
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "prometheus", "uid": PROMETHEUS_UID},
                    "expr": query,
                    "range": True,
                    "instant": False,
                    "editorMode": "code",
                    "legendFormat": legend,
                    "interval": MIN_INTERVAL,
                    "maxDataPoints": MAX_DATA_POINTS,
                }
            ],
            "range": {"from": RANGE.grafana_from, "to": RANGE.grafana_to},
        }


def test_a_live_session_links_explore_up_to_now() -> None:
    live = session_range(STARTED, None)
    panes = _panes(session_logs_url(BASE, host=HOST, session_id=SID, time_range=live))
    assert panes["logs"]["range"] == {"from": str(STARTED - WINDOW_BEFORE_MS), "to": "now"}


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_session_id_reads_back_from_the_explore_links(value: str) -> None:
    logs = session_logs_url(BASE, host=HOST, session_id=value, time_range=RANGE)
    usage = session_usage_url(BASE, host=HOST, session_id=value, time_range=RANGE)
    for url in (logs, usage):
        # Only URL-safe characters: nothing ends the attribute, the query or the URL.
        query = urlsplit(url).query
        assert all(char.isascii() and char.isprintable() for char in url), url
        assert not set(query) & set(" \"'<>#\\{}|^`"), url
    assert _panes(logs)["logs"]["queries"][0]["expr"] == session_events_query(HOST, value)
    assert _panes(usage)["tokens"]["queries"][0]["expr"] == session_tokens_query(HOST, value)


# The dashboard link.


def test_the_dashboard_link_sets_the_host_and_the_range() -> None:
    where, query = _decoded(dashboard_url(BASE, host=HOST, time_range=RANGE))
    assert where == f"{BASE}/d/{DASHBOARD_UID}"
    assert query == {
        "orgId": ["1"],
        "from": [RANGE.grafana_from],
        "to": [RANGE.grafana_to],
        "var-host": [HOST],
    }


def test_the_dashboard_link_without_a_range_keeps_the_dashboards_default() -> None:
    url = dashboard_url(BASE, host=HOST)
    where, query = _decoded(url)
    assert where == f"{BASE}/d/{DASHBOARD_UID}"
    assert query == {"orgId": ["1"], "var-host": [HOST]}
    # In order: the organization first, the variables last.
    assert parse_qsl(urlsplit(url).query) == [("orgId", "1"), ("var-host", HOST)]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_host_reads_back_from_the_dashboard_link(value: str) -> None:
    url = dashboard_url(BASE, host=value, time_range=RANGE)
    assert all(char.isascii() and char.isprintable() for char in url), url
    assert _decoded(url)[1]["var-host"] == [value]


def test_a_base_with_a_path_or_a_trailing_slash_joins_cleanly() -> None:
    assert dashboard_url("http://localhost:4330/", host=HOST).startswith(
        f"http://localhost:4330/d/{DASHBOARD_UID}?"
    )
    assert session_logs_url(
        "http://localhost:4330/grafana/", host=HOST, session_id=SID, time_range=RANGE
    ).startswith("http://localhost:4330/grafana/explore?")


# Everything for the session page.


def test_session_links_bundles_the_three_links() -> None:
    links = session_links(BASE, host=HOST, session_id=SID, time_range=RANGE)
    assert links == SessionLinks(
        logs=session_logs_url(BASE, host=HOST, session_id=SID, time_range=RANGE),
        usage=session_usage_url(BASE, host=HOST, session_id=SID, time_range=RANGE),
        dashboard=dashboard_url(BASE, host=HOST, time_range=RANGE),
    )
