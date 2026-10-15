"""Static checks of the Claude Code overview dashboard (ADR 0003, ADR 0013, ADR 0014,
ADR 0015, ADR 0022).

Unit tests: they parse ``stack/grafana/dashboards/claude-code-overview.json``, its
provider in ``stack/grafana/provisioning/dashboards/`` and the Grafana service in
``compose.yaml``; Docker is not needed. They check the layout, the queries against the
stored names of ``ccobs.domain.telemetry`` and the exact totals of ADR 0013, the
variables, and the visual rules: Grafana's named, theme-aware colours with the same
meaning on every panel, sentence-case titles and a description on every panel.
``tests/integration/test_stack_dashboard.py`` runs every query against a running stack.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

from ccobs.domain.telemetry import (
    ACTIVE_TIME,
    CODE_EDIT_TOOL_DECISION,
    COST_USAGE,
    EVENTS,
    HOST_LABEL,
    JOB_LABEL,
    LINES_OF_CODE,
    LOG_CONTENT_KEYS,
    LOKI_INDEX_LABELS,
    METRICS,
    SERVICE_NAME,
    SESSION_COUNT,
    TARGET_INFO,
    TOKEN_USAGE,
    Metric,
)
from tools.capture.jsontypes import JSON, JSONObject

ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE: Final = ROOT / "compose.yaml"
FIXTURES: Final = ROOT / "tests" / "fixtures" / "otlp"
GRAFANA_DIR: Final = ROOT / "stack" / "grafana"
DASHBOARDS_DIR: Final = GRAFANA_DIR / "dashboards"
DASHBOARD: Final = DASHBOARDS_DIR / "claude-code-overview.json"
PROVIDER: Final = GRAFANA_DIR / "provisioning" / "dashboards" / "dashboards.yaml"

# compose.yaml mounts stack/grafana here, read-only; the provider reads the dashboards
# from its subdirectory, and the home page is the overview's file.
MOUNT: Final = "./stack/grafana:/etc/ccobs/grafana:ro"
CONTAINER_DASHBOARDS: Final = "/etc/ccobs/grafana/dashboards"
HOME_VARIABLE: Final = "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"

UID: Final = "claude-code-overview"
TITLE: Final = "Claude Code overview"
TAGS: Final = ["claude-code", "cc-observatory"]
# The version Grafana 13.2.4 writes: the last step of its dashboard schema migrations
# (LATEST_VERSION in apps/dashboard/pkg/migration/schemaversion, and the frontend's
# DashboardMigrator), so the file is not migrated when Grafana loads it.
SCHEMA_VERSION: Final = 42
GRID_COLUMNS: Final = 24
# Grafana's own fields, which a provisioned file leaves out: the database id and
# version, and the inputs and requirements of an exported dashboard.
FORBIDDEN_FIELDS: Final = ("id", "version", "__inputs", "__requires", "__elements")

# Datasource uids (stack/grafana/provisioning/datasources) and their plugin types. Tempo
# runs only with the traces profile, so no panel queries it.
DATASOURCE_TYPES: Final = {"prometheus": "prometheus", "loki": "loki"}

# The layout on Grafana's 24-column grid, in priority order: on a phone Grafana stacks
# the panels by y, then x, so cost comes first and the events log last.
LAYOUT: Final = (
    ("stat", "Cost", 0, 0, 6, 5),
    ("stat", "Tokens", 6, 0, 6, 5),
    ("stat", "Sessions started", 12, 0, 4, 5),
    ("stat", "Active time", 16, 0, 4, 5),
    ("stat", "API errors", 20, 0, 4, 5),
    ("row", "Spend and tokens", 0, 5, 24, 1),
    ("timeseries", "Cost by model over time", 0, 6, 12, 9),
    ("timeseries", "Tokens by type over time", 12, 6, 12, 9),
    ("bargauge", "Cost by model", 0, 15, 8, 8),
    ("table", "Tokens by model and type", 8, 15, 16, 8),
    ("row", "Code", 0, 23, 24, 1),
    ("timeseries", "Lines changed", 0, 24, 12, 8),
    ("bargauge", "Edit decisions", 12, 24, 6, 8),
    ("stat", "Commits and pull requests", 18, 24, 6, 8),
    ("row", "Activity", 0, 32, 24, 1),
    ("timeseries", "Sessions started over time", 0, 33, 12, 8),
    ("timeseries", "Who was active", 12, 33, 12, 8),
    ("row", "Errors and events", 0, 41, 24, 1),
    ("timeseries", "API errors by status code", 0, 42, 24, 6),
    ("logs", "Recent events", 0, 48, 24, 12),
)
# Words that keep their capitals in a sentence-case title.
ACRONYMS: Final = frozenset({"API"})

# PromQL (ADR 0013, in the form of ccobs.domain.telemetry.total_query): one sum of one
# anchored increase over the dashboard's range (totals) or over the interval (bars), and
# `or vector(0)` so an idle range reads 0. A bar's point is the start of its bucket:
# Grafana floors both ends of a range query to the step, so a window that ended at the
# point would leave the newest bucket out; `offset -$__interval` makes the point at t
# cover (t, t + interval].
PROMQL: Final = re.compile(
    r"sum(?: by \((?P<by>\w+(?:, \w+)*)\) )?"
    r"\(increase\((?P<name>\w+)\{(?P<matchers>[^{}]*)\}"
    r"\[\$__(?P<window>range|interval)\] anchored(?P<offset> offset -\$__interval)?\)\)"
    r"(?P<zero> or vector\(0\))?"
)
MATCHER: Final = re.compile(r'(?P<label>\w+)(?P<op>=~|!~|!=|=)"(?P<value>[^"]*)"')
HOST_MATCHER: Final = f'{HOST_LABEL}=~"$host"'
MODEL_LABEL: Final = "model"
MODEL_MATCHER: Final = f'{MODEL_LABEL}=~"$model"'
LEGEND_LABEL: Final = re.compile(r"\{\{\s*(\w+)\s*\}\}")
PROMETHEUS_METRICS: Final = {metric.name: metric for metric in METRICS}
# The variables' queries, as Grafana 13.2's Prometheus variable editor writes them.
HOST_QUERY: Final = f'label_values({TARGET_INFO}{{{JOB_LABEL}="{SERVICE_NAME}"}},{HOST_LABEL})'
MODEL_QUERY: Final = f"label_values({TOKEN_USAGE.name}{{{HOST_MATCHER}}},{MODEL_LABEL})"

# LogQL (ADR 0015): Claude Code's stream on the selected hosts, then filters on
# structured metadata.
STREAM_SELECTOR: Final = f'{{service_name="{SERVICE_NAME}", {HOST_LABEL}=~"$host"}}'
# The same bucket convention as PromQL. Loki 3.7.8 refuses a negative offset in days
# (`offset -1d`: "unexpected -, expecting DURATION"), which $__interval becomes for
# ranges over 100 days, so the offset is the interval in milliseconds.
LOGQL_COUNT: Final = re.compile(
    r"sum(?: by \((?P<by>\w+(?:, \w+)*)\) )?\(count_over_time\("
    + re.escape(STREAM_SELECTOR)
    + r'(?P<filters>(?: \| \w+(?:=~|!~|!=|=)"[^"]*")*)'
    r" \[\$__(?P<window>range|interval)\](?P<offset> offset -\$\{__interval_ms\}ms)?\)\)"
    r"(?P<zero> or vector\(0\))?"
)
LOGQL_LINES: Final = re.compile(
    re.escape(STREAM_SELECTOR) + r"(?: \| line_format `(?P<template>[^`]*)`)?"
)
LOGQL_FILTER: Final = re.compile(r' \| (?P<label>\w+)(?P<op>=~|!~|!=|=)"(?P<value>[^"]*)"')
TEMPLATE_ACTION: Final = re.compile(r"\{\{(.*?)\}\}")
# A field in a template action, such as .tool_name; not a printf verb such as "%.4f".
TEMPLATE_FIELD: Final = re.compile(r"(?<![\w%])\.([A-Za-z_]\w*)")
COST_FORMAT: Final = '{{with .cost_usd}} cost_usd={{printf "%.4f" (float64 .)}}{{end}}'
API_ERROR_FILTERS: Final = ' | event_name="api_error" | model=~"$model"'
# The metadata the events log may show in its line: never a content field (ADR 0006).
EVENT_LINE_FIELDS: Final = frozenset(
    {"event_name", "hook_event", "tool_name", "model", "status_code", "cost_usd"}
)

# Grafana's named colours: each hue in five shades, which each theme draws in its own
# readable tone, plus "text", the theme's text colour. Never hex, rgb() or hsl().
HUES: Final = ("red", "orange", "yellow", "green", "blue", "purple")
SHADES: Final = ("super-light-", "light-", "", "semi-dark-", "dark-")
NAMED_COLOURS: Final = frozenset({f"{shade}{hue}" for hue in HUES for shade in SHADES} | {"text"})
COLOUR_MODES: Final = frozenset({"fixed", "thresholds", "palette-classic-by-name"})
LITERAL_COLOUR: Final = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
# Colour carries the same meaning on every panel: series label value -> (display name,
# colour), per panel. Lines use the git diff convention; red is only for removals,
# rejections and errors.
TOKEN_TYPES: Final = {
    "input": ("Input", "blue"),
    "output": ("Output", "orange"),
    "cacheRead": ("Cache read", "green"),
    "cacheCreation": ("Cache write", "purple"),
}
LINE_TYPES: Final = {"added": ("Added", "green"), "removed": ("Removed", "red")}
ACTIVE_TYPES: Final = {"user": ("You", "blue"), "cli": ("Claude Code", "purple")}
DECISION_COLOURS: Final = {"accept": "green", "reject": "red"}
# API errors by status code, in order: a later override wins over an earlier one.
# Grafana anchors a byRegexp option that has no slashes (stringToJsRegex wraps it in
# ^...$), so "4.*" matches every 4xx code and "429" only 429.
STATUS_COLOURS: Final = (("4.*", "orange"), ("429", "yellow"), ("5.*", "red"))
STATUS_SAMPLES: Final = {"400": "orange", "404": "orange", "429": "yellow", "503": "red"}
JS_REGEX: Final = re.compile(r"/(?P<pattern>.*?)/(?P<flags>[gimys]*)")
# The start_type values Claude Code documents (ADR 0012), with their display names.
START_TYPES: Final = {
    "fresh": "New",
    "resume": "Resumed",
    "continue": "Continued",
    "agents_view": "Agents view",
}
# The built-in annotation Grafana adds to every dashboard, declared with a named colour so
# that Grafana does not add its own, an rgba() colour.
BUILT_IN_ANNOTATION: Final = {
    "builtIn": 1,
    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
    "enable": True,
    "hide": True,
    "iconColor": "blue",
    "name": "Annotations & Alerts",
    "type": "dashboard",
}

# Bars over time: one per interval of at least a minute, about 100 across the range.
MIN_INTERVAL: Final = "1m"
MAX_DATA_POINTS: Final = 100

type Panel = dict[str, JSON]


@dataclass(frozen=True)
class Target:
    """One query of a panel, with the panel it belongs to."""

    panel: Panel
    query: JSONObject

    def belongs_to(self, panel: Panel) -> bool:
        # Each helper parses the file again, so panels are compared by id.
        return self.panel.get("id") == panel.get("id")

    @property
    def where(self) -> str:
        return f"{self.panel.get('title')} ({self.query.get('refId')})"

    @property
    def expr(self) -> str:
        expr = self.query.get("expr")
        assert isinstance(expr, str), f"{self.where} has no expr"
        return expr


@dataclass(frozen=True)
class PromQuery:
    """A Prometheus target parsed by ``PROMQL``."""

    target: Target
    metric: Metric
    by: tuple[str, ...]
    matchers: dict[str, tuple[str, str]]
    window: str
    zero: bool
    offset: bool


# Loading.


def _mapping(value: object, where: str) -> dict[str, JSON]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[JSON]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _dashboard() -> JSONObject:
    return _mapping(json.loads(DASHBOARD.read_text(encoding="utf-8")), DASHBOARD.name)


def _yaml(path: Path) -> dict[str, JSON]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


def _panels() -> list[Panel]:
    """Every panel, rows included, in file order."""
    return [_mapping(item, "panel") for item in _list(_dashboard().get("panels"), "panels")]


def _visual_panels() -> list[Panel]:
    """Every panel but the rows."""
    return [panel for panel in _panels() if panel.get("type") != "row"]


def _panel(title: str) -> Panel:
    found = [panel for panel in _panels() if panel.get("title") == title]
    assert len(found) == 1, f"expected one panel titled {title!r}, got {len(found)}"
    return found[0]


def _grid(panel: Panel) -> tuple[int, int, int, int]:
    grid = _mapping(panel.get("gridPos"), f"{panel.get('title')} gridPos")
    values = [grid.get(key) for key in ("x", "y", "w", "h")]
    assert all(isinstance(value, int) for value in values), grid
    x, y, w, h = (int(str(value)) for value in values)
    return x, y, w, h


def _targets(datasource: str | None = None) -> list[Target]:
    targets = [
        Target(panel, _mapping(query, "target"))
        for panel in _visual_panels()
        for query in _list(panel.get("targets"), f"{panel.get('title')} targets")
    ]
    if datasource is None:
        return targets
    return [
        t for t in targets if _mapping(t.panel.get("datasource"), "ds").get("uid") == datasource
    ]


def _prom_query(target: Target) -> PromQuery:
    match = PROMQL.fullmatch(target.expr)
    assert match, f"{target.where}: {target.expr!r} is not a sum of one anchored increase"
    metric = PROMETHEUS_METRICS.get(match["name"])
    assert metric is not None, f"{target.where}: {match['name']} is not a stored name (ADR 0014)"
    matchers: dict[str, tuple[str, str]] = {}
    for part in match["matchers"].split(", "):
        found = MATCHER.fullmatch(part)
        assert found, f"{target.where}: {part!r} is not a label matcher"
        assert found["label"] not in matchers, f"{target.where}: {found['label']} twice"
        matchers[found["label"]] = (found["op"], found["value"])
    by = tuple(match["by"].split(", ")) if match["by"] else ()
    window, zero, offset = match["window"], bool(match["zero"]), bool(match["offset"])
    return PromQuery(target, metric, by, matchers, window, zero, offset)


def _prom_queries() -> list[PromQuery]:
    return [_prom_query(target) for target in _targets("prometheus")]


def _prom_queries_of(panel: Panel) -> list[PromQuery]:
    return [query for query in _prom_queries() if query.target.belongs_to(panel)]


def _overrides(panel: Panel) -> list[tuple[JSONObject, dict[str, JSON]]]:
    """Each override of ``panel``: its matcher, and its properties by id. The logs panel
    has no field config, which Grafana 13.2 leaves out."""
    config = _mapping(panel.get("fieldConfig", {"overrides": []}), f"{panel.get('title')} config")
    found = []
    for item in _list(config.get("overrides"), "overrides"):
        override = _mapping(item, "override")
        properties = {
            str(prop.get("id")): prop.get("value")
            for prop in (_mapping(p, "property") for p in _list(override.get("properties"), "p"))
        }
        found.append((_mapping(override.get("matcher"), "matcher"), properties))
    return found


def _defaults(panel: Panel) -> dict[str, JSON]:
    config = _mapping(panel.get("fieldConfig"), f"{panel.get('title')} fieldConfig")
    return _mapping(config.get("defaults"), f"{panel.get('title')} defaults")


def _options(panel: Panel) -> dict[str, JSON]:
    return _mapping(panel.get("options"), f"{panel.get('title')} options")


def _walk(node: JSON, path: str = "$") -> Iterator[tuple[str, JSON]]:
    """Every value under ``node`` with its JSON path."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _fixture_values(metric: Metric, label: str) -> set[str]:
    """The values of the data point attribute ``label`` of ``metric`` in the fixtures."""
    values: set[str] = set()
    for path in FIXTURES.glob("*/metrics.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for _, value in _walk(json.loads(line)):
                if not isinstance(value, dict) or value.get("name") != metric.otel_name:
                    continue
                for point in _list(_mapping(value.get("sum"), "sum").get("dataPoints"), "pts"):
                    for attribute in _list(_mapping(point, "point").get("attributes"), "attrs"):
                        item = _mapping(attribute, "attribute")
                        if item.get("key") == label:
                            values.add(str(_mapping(item.get("value"), "value").get("stringValue")))
    return values


def _environment() -> dict[str, str]:
    services = _mapping(_yaml(COMPOSE).get("services"), "services")
    service = _mapping(services.get("grafana"), "grafana")
    return {k: str(v) for k, v in _mapping(service.get("environment"), "environment").items()}


def _host_path(container_path: str) -> Path:
    """The file in the repository that compose.yaml mounts at ``container_path``."""
    source, target, mode = MOUNT.split(":")
    services = _mapping(_yaml(COMPOSE).get("services"), "services")
    assert MOUNT in _list(_mapping(services.get("grafana"), "grafana").get("volumes"), "volumes")
    assert mode == "ro"
    assert container_path.startswith(f"{target}/"), container_path
    return ROOT / source / container_path.removeprefix(f"{target}/")


# The dashboard.


def test_dashboard_identity_and_defaults() -> None:
    dashboard = _dashboard()
    assert dashboard.get("uid") == UID
    assert dashboard.get("title") == TITLE
    assert dashboard.get("tags") == TAGS
    assert dashboard.get("schemaVersion") == SCHEMA_VERSION
    assert dashboard.get("time") == {"from": "now-24h", "to": "now"}
    assert dashboard.get("refresh") == "1m"
    assert dashboard.get("timezone") == "browser"
    # A shared crosshair across panels.
    assert dashboard.get("graphTooltip") == 1
    # Editable, so the anonymous Editor can change a copy; the file stays the source.
    assert dashboard.get("editable") is True
    assert dashboard.get("liveNow") is False
    assert isinstance(dashboard.get("description"), str)


def test_dashboard_has_no_database_fields_or_imports() -> None:
    dashboard = _dashboard()
    for field in FORBIDDEN_FIELDS:
        assert field not in dashboard, field
    assert dashboard.get("links", []) == []


def test_dashboard_is_the_only_file_the_provider_loads() -> None:
    assert sorted(path.name for path in DASHBOARDS_DIR.iterdir()) == [DASHBOARD.name]


def test_dashboard_has_no_url() -> None:
    # No links to external sites, and nothing that would load from one (ADR 0006).
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "http://" not in text
    assert "https://" not in text


# Layout.


def test_layout_is_the_planned_one() -> None:
    found = [(p.get("type"), p.get("title"), *_grid(p)) for p in _panels()]
    assert found == list(LAYOUT)


def test_grid_stays_within_24_columns() -> None:
    for panel in _panels():
        x, y, w, h = _grid(panel)
        assert min(x, y) >= 0, panel.get("title")
        assert min(w, h) >= 1, panel.get("title")
        assert x + w <= GRID_COLUMNS, panel.get("title")


def test_no_two_panels_overlap() -> None:
    boxes = [(str(panel.get("title")), _grid(panel)) for panel in _panels()]
    for index, (first, (x1, y1, w1, h1)) in enumerate(boxes):
        for second, (x2, y2, w2, h2) in boxes[index + 1 :]:
            overlap = x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1
            assert not overlap, f"{first} overlaps {second}"


def test_priority_order_starts_with_cost_and_ends_with_the_events_log() -> None:
    ordered = sorted(_panels(), key=lambda panel: (_grid(panel)[1], _grid(panel)[0]))
    assert (ordered[0].get("type"), ordered[0].get("title")) == ("stat", "Cost")
    assert ordered[-1].get("type") == "logs"


def test_rows_are_expanded() -> None:
    rows = [panel for panel in _panels() if panel.get("type") == "row"]
    assert rows
    for row in rows:
        assert row.get("collapsed") is False, row.get("title")
        assert row.get("panels") == [], row.get("title")


def test_panel_ids_are_unique() -> None:
    ids = [panel.get("id") for panel in _panels()]
    assert all(isinstance(value, int) and value > 0 for value in ids), ids
    assert len(set(map(str, ids))) == len(ids)


# Words.


def test_titles_are_sentence_case() -> None:
    for panel in _panels():
        title = panel.get("title")
        assert isinstance(title, str), panel
        assert title.strip(), panel
        assert not title.isupper(), title
        first, *rest = title.split()
        assert first[0].isupper(), title
        assert first in ACRONYMS or first[1:] == first[1:].lower(), title
        assert all(word in ACRONYMS or word == word.lower() for word in rest), title


def test_titles_are_unique() -> None:
    # A title names one panel: the views of one subject say how they differ ("over
    # time"), and links and searches can rely on it.
    titles = [str(panel.get("title")) for panel in _panels()]
    assert len(set(titles)) == len(titles), titles


def test_every_panel_has_a_one_sentence_description() -> None:
    # Rows show no description; every other panel says what it shows, in user terms.
    for panel in _visual_panels():
        description = panel.get("description")
        assert isinstance(description, str), panel.get("title")
        assert description.strip(), panel.get("title")
        assert description.endswith("."), panel.get("title")
        assert not re.search(r"[.!?] ", description), f"{panel.get('title')}: {description}"


def test_caveats_are_in_the_descriptions() -> None:
    # Cost is Claude Code's estimate, and a panel that ignores the model variable says
    # so, since picking a model changes the panels around it.
    for panel in _visual_panels():
        description = str(panel.get("description"))
        if any(query.metric is COST_USAGE for query in _prom_queries_of(panel)):
            assert "not your bill" in description, panel.get("title")
        targets = [target for target in _targets() if target.belongs_to(panel)]
        if not any("$model" in target.expr for target in targets):
            assert "not filtered by model" in description, panel.get("title")


def test_descriptions_explain_what_is_counted() -> None:
    # An api_error event is emitted per attempt (it carries `attempt`), cache reads are
    # most of Claude Code's tokens, and the session count is of sessions that start.
    for title in ("API errors", "API errors by status code"):
        assert "each retry" in str(_panel(title).get("description")), title
    assert "cache reads usually" in str(_panel("Tokens").get("description"))
    assert "already running" in str(_panel("Sessions started").get("description"))


# Datasources and targets.


def test_every_target_uses_its_panels_datasource_by_uid() -> None:
    for panel in _visual_panels():
        datasource = _mapping(panel.get("datasource"), f"{panel.get('title')} datasource")
        uid = datasource.get("uid")
        assert isinstance(uid, str), panel.get("title")
        assert uid in DATASOURCE_TYPES, panel.get("title")
        assert datasource == {"type": DATASOURCE_TYPES[uid], "uid": uid}, panel.get("title")
        targets = _list(panel.get("targets"), "targets")
        assert targets, panel.get("title")
        ref_ids = [_mapping(target, "target").get("refId") for target in targets]
        assert len(set(map(str, ref_ids))) == len(ref_ids), panel.get("title")
        for target in targets:
            assert _mapping(target, "target").get("datasource") == datasource, panel.get("title")


def test_prometheus_queries_are_exact_totals_of_stored_metrics() -> None:
    # Every expression parses as one sum of one anchored increase of a name from
    # ccobs.domain.telemetry (ADR 0013, ADR 0014): never rate, irate or plain increase.
    queries = _prom_queries()
    assert queries
    for query in queries:
        expr = query.target.expr
        assert "rate(" not in expr, query.target.where
        assert expr.count("increase(") == expr.count("anchored") == 1, query.target.where
        assert set(query.by) <= query.metric.labels, query.target.where
        assert set(query.matchers) <= query.metric.all_labels, query.target.where


def test_prometheus_queries_filter_the_host_variable() -> None:
    for query in _prom_queries():
        assert query.matchers.get(HOST_LABEL) == ("=~", "$host"), query.target.where


def test_model_variable_filters_exactly_the_metrics_with_a_model_label() -> None:
    # Picking one model would empty a panel whose metric has no model label.
    with_model = {metric.name for metric in METRICS if MODEL_LABEL in metric.labels}
    assert with_model == {TOKEN_USAGE.name, COST_USAGE.name, LINES_OF_CODE.name}
    for query in _prom_queries():
        filtered = query.matchers.get(MODEL_LABEL) == ("=~", "$model")
        assert filtered == (query.metric.name in with_model), query.target.where


def test_other_matchers_are_fixed_equalities() -> None:
    for query in _prom_queries():
        for label, (op, value) in query.matchers.items():
            if label not in (HOST_LABEL, MODEL_LABEL):
                assert op == "=", f"{query.target.where}: {label}"
                assert "$" not in value, f"{query.target.where}: {label}"


def test_totals_are_instant_and_bars_are_ranges_of_the_interval() -> None:
    for query in _prom_queries():
        target, panel = query.target.query, query.target.panel
        if query.window == "range":
            assert target.get("instant") is True, query.target.where
            assert target.get("range") is False, query.target.where
            assert not query.offset, query.target.where
        else:
            assert target.get("range") is True, query.target.where
            assert target.get("instant") is False, query.target.where
            assert panel.get("type") == "timeseries", query.target.where
            assert not query.zero, query.target.where
            # The point at t is the bucket (t, t + interval].
            assert query.offset, query.target.where


def test_headline_stats_read_zero_when_idle() -> None:
    for panel in _visual_panels():
        if _grid(panel)[1] != 0:
            continue
        for target in _targets():
            if target.belongs_to(panel):
                assert target.expr.endswith(" or vector(0)"), target.where


def test_legends_name_only_grouped_labels() -> None:
    for query in _prom_queries():
        legend = str(query.target.query.get("legendFormat", ""))
        assert set(LEGEND_LABEL.findall(legend)) <= set(query.by), query.target.where


def test_loki_queries_select_claude_codes_stream_and_filter_metadata() -> None:
    targets = _targets("loki")
    assert targets
    for target in targets:
        expr = target.expr
        count, lines = LOGQL_COUNT.fullmatch(expr), LOGQL_LINES.fullmatch(expr)
        assert count or lines, f"{target.where}: {expr!r}"
        if count:
            filters = LOGQL_FILTER.findall(count["filters"])
            assert filters, target.where
            for label, _, value in filters:
                # Structured metadata, never an index label (ADR 0015).
                assert label not in LOKI_INDEX_LABELS, target.where
                if label == "event_name":
                    assert value in EVENTS, target.where
            instant = count["window"] == "range"
            assert target.query.get("queryType") == ("instant" if instant else "range")
            # Totals end at the range's end; bars start at their point, as in PromQL.
            assert bool(count["offset"]) is not instant, target.where
            assert set(count["by"].split(", ") if count["by"] else ()) <= {"status_code"}
        else:
            assert target.panel.get("type") == "logs", target.where
            assert target.query.get("queryType") == "range", target.where


def test_api_error_queries_filter_the_model_variable() -> None:
    # An event without a model has an empty one, which `=~".*"` (All) matches.
    for title in ("API errors", "API errors by status code"):
        for target in _targets("loki"):
            if target.belongs_to(_panel(title)):
                count = LOGQL_COUNT.fullmatch(target.expr)
                assert count, target.where
                assert count["filters"] == API_ERROR_FILTERS, target.where


def test_events_log_shows_only_metadata() -> None:
    panel = _panel("Recent events")
    (target,) = [target for target in _targets("loki") if target.belongs_to(panel)]
    lines = LOGQL_LINES.fullmatch(target.expr)
    assert lines, target.expr
    template = lines["template"] or ""
    fields = {
        field
        for action in TEMPLATE_ACTION.findall(template)
        for field in TEMPLATE_FIELD.findall(action)
    }
    assert fields <= EVENT_LINE_FIELDS, fields
    content = {key.replace(".", "_") for key in LOG_CONTENT_KEYS}
    assert not fields & content
    assert MODEL_MATCHER not in target.expr
    # Costs read with 4 decimals, not with every digit of a float.
    assert COST_FORMAT in template


# Variables.


def _variables() -> dict[str, dict[str, JSON]]:
    templating = _mapping(_dashboard().get("templating"), "templating")
    found = [_mapping(item, "variable") for item in _list(templating.get("list"), "list")]
    return {str(variable.get("name")): variable for variable in found}


def test_variables_are_host_then_model() -> None:
    # The model variable reads the selected hosts, so host comes first.
    assert list(_variables()) == ["host", "model"]


@pytest.mark.parametrize(
    ("name", "label", "query"),
    [("host", "Host", HOST_QUERY), ("model", "Model", MODEL_QUERY)],
)
def test_variable(name: str, label: str, query: str) -> None:
    variable = _variables()[name]
    assert variable.get("label") == label
    assert variable.get("type") == "query"
    assert variable.get("datasource") == {"type": "prometheus", "uid": "prometheus"}
    # qryType 1 is LabelValues in Grafana 13.2's Prometheus variable editor.
    assert variable.get("definition") == query
    assert variable.get("query") == {
        "qryType": 1,
        "query": query,
        "refId": "PrometheusVariableQueryEditor-VariableQuery",
    }
    assert variable.get("multi") is True
    assert variable.get("includeAll") is True
    # All is a regex that matches every value, the empty one included.
    assert variable.get("allValue") == ".*"
    current = _mapping(variable.get("current"), "current")
    assert current.get("value") in ("$__all", ["$__all"])
    # 2: refresh on time range change; 1: sorted alphabetically.
    assert variable.get("refresh") == 2
    assert variable.get("sort") == 1


# Colours.


def test_colours_are_named_and_theme_aware() -> None:
    dashboard = _dashboard()
    assert not LITERAL_COLOUR.search(DASHBOARD.read_text(encoding="utf-8"))
    for path, value in _walk(dashboard):
        if not isinstance(value, dict):
            continue
        if "fixedColor" in value:
            assert value["fixedColor"] in NAMED_COLOURS, path
        if "iconColor" in value:
            assert value["iconColor"] in NAMED_COLOURS, path
        if "mode" in value and path.endswith(".color"):
            assert value["mode"] in COLOUR_MODES, path
        if path.endswith(".thresholds"):
            for step in _list(value.get("steps"), f"{path}.steps"):
                assert _mapping(step, "step").get("color") in NAMED_COLOURS, path
    for panel in _visual_panels():
        for _, properties in _overrides(panel):
            if "color" in properties:
                colour = _mapping(properties["color"], "color")
                assert colour.get("mode") == "fixed", panel.get("title")
                assert colour.get("fixedColor") in NAMED_COLOURS, panel.get("title")


def test_models_keep_their_colour_across_panels() -> None:
    # Every panel that draws one series per model over time. The token table lists
    # models as rows of text, and the bar gauge of totals is neutral: in Grafana 13.2 it
    # hashes another name than the time series do, so a model would change colour
    # between the two (seen in the browser).
    panels = [
        query.target.panel
        for query in _prom_queries()
        if MODEL_LABEL in query.by and query.target.panel.get("type") == "timeseries"
    ]
    assert panels
    for panel in panels:
        assert _defaults(panel).get("color") == {"mode": "palette-classic-by-name"}
    for query in _prom_queries():
        if MODEL_LABEL in query.by and query.target.panel.get("type") == "bargauge":
            colour = _defaults(query.target.panel).get("color")
            assert colour == {"fixedColor": "text", "mode": "fixed"}


def _series_styles(panel: Panel) -> dict[str, tuple[str, str]]:
    """``byName`` overrides of ``panel``: series name -> (display name, colour)."""
    styles: dict[str, tuple[str, str]] = {}
    for matcher, properties in _overrides(panel):
        if matcher.get("id") == "byName":
            colour = _mapping(properties.get("color"), "color")
            name, display = str(matcher.get("options")), properties.get("displayName")
            styles[name] = (str(display), str(colour.get("fixedColor")))
    return styles


@pytest.mark.parametrize(
    ("title", "metric", "label", "styles"),
    [
        ("Tokens by type over time", TOKEN_USAGE, "type", TOKEN_TYPES),
        ("Lines changed", LINES_OF_CODE, "type", LINE_TYPES),
        ("Who was active", ACTIVE_TIME, "type", ACTIVE_TYPES),
    ],
)
def test_series_have_their_semantic_names_and_colours(
    title: str, metric: Metric, label: str, styles: dict[str, tuple[str, str]]
) -> None:
    # The label values are those Claude Code sends (the fixtures), not guesses.
    assert set(styles) == _fixture_values(metric, label)
    panel = _panel(title)
    assert _series_styles(panel) == styles
    (query,) = _prom_queries_of(panel)
    assert query.by == (label,)
    assert query.target.query.get("legendFormat") == f"{{{{{label}}}}}"


def test_lines_removed_are_drawn_below_zero() -> None:
    panel = _panel("Lines changed")
    transforms = {
        str(matcher.get("options")): properties.get("custom.transform")
        for matcher, properties in _overrides(panel)
    }
    assert transforms == {"added": None, "removed": "negative-Y"}


def test_token_table_uses_the_token_type_names() -> None:
    panel = _panel("Tokens by model and type")
    # groupingToMatrix reads the column "Value", the name Grafana gives the value column
    # of a table fed by one query; a second query would make it "Value #A" and empty the
    # table, so the panel keeps a single target.
    (query,) = _prom_queries_of(panel)
    assert query.by == (MODEL_LABEL, "type")
    assert query.target.query.get("format") == "table"
    transformations = [
        _mapping(t, "transformation") for t in _list(panel.get("transformations"), "t")
    ]
    assert [t.get("id") for t in transformations] == ["groupingToMatrix", "organize"]
    matrix = _mapping(transformations[0].get("options"), "groupingToMatrix")
    assert matrix == {
        "columnField": "type",
        "rowField": MODEL_LABEL,
        "valueField": "Value",
        "emptyValue": "zero",
    }
    organize = _mapping(transformations[1].get("options"), "organize")
    renames = _mapping(organize.get("renameByName"), "renameByName")
    # groupingToMatrix names its first column "<row field>\<column field>".
    first_column = f"{MODEL_LABEL}\\type"
    assert renames == {
        first_column: "Model",
        **{name: display for name, (display, _) in TOKEN_TYPES.items()},
    }
    # Model first, then the token types in the order of the "Tokens by type" panel.
    order = _mapping(organize.get("indexByName"), "indexByName")
    columns = sorted(order, key=lambda name: int(str(order[name])))
    assert columns == [first_column, *TOKEN_TYPES]


def test_edit_decisions_are_green_when_accepted_and_red_when_rejected() -> None:
    assert set(DECISION_COLOURS) == _fixture_values(CODE_EDIT_TOOL_DECISION, "decision")
    panel = _panel("Edit decisions")
    colours = {
        str(matcher.get("options")): _mapping(properties.get("color"), "color").get("fixedColor")
        for matcher, properties in _overrides(panel)
        if matcher.get("id") == "byFrameRefID"
    }
    queries = _prom_queries_of(panel)
    assert len(queries) == len(DECISION_COLOURS)
    for query in queries:
        _, decision = query.matchers["decision"]
        ref_id = str(query.target.query.get("refId"))
        assert colours.get(ref_id) == DECISION_COLOURS[decision], ref_id
        assert query.by == ("tool_name",)


def _js_regex(option: str) -> re.Pattern[str]:
    """A byRegexp option as Grafana 13.2 compiles it (stringToJsRegex): without slashes
    it is anchored, ^option$; /pattern/flags is used as it is."""
    match = JS_REGEX.fullmatch(option)
    if match is None:
        return re.compile(f"^{option}$")
    return re.compile(match["pattern"], re.IGNORECASE if "i" in match["flags"] else 0)


def test_regexp_overrides_match_the_series_grafana_draws() -> None:
    # The last override that matches a series sets its colour.
    found = False
    for panel in _visual_panels():
        regexps = [
            (_js_regex(str(matcher.get("options"))), properties)
            for matcher, properties in _overrides(panel)
            if matcher.get("id") == "byRegexp"
        ]
        found = found or bool(regexps)
        for name, expected in STATUS_SAMPLES.items() if regexps else ():
            colours = [
                _mapping(props.get("color"), "color").get("fixedColor")
                for pattern, props in regexps
                if pattern.search(name)
            ]
            assert colours, f"{panel.get('title')}: no override matches {name}"
            assert colours[-1] == expected, f"{panel.get('title')}: {name} is {colours[-1]}"
    assert found


def test_start_types_have_display_names() -> None:
    # The values the fixtures carry are among the documented ones (ADR 0012).
    assert _fixture_values(SESSION_COUNT, "start_type") <= set(START_TYPES)
    panel = _panel("Sessions started over time")
    names = {
        str(matcher.get("options")): properties.get("displayName")
        for matcher, properties in _overrides(panel)
        if matcher.get("id") == "byName"
    }
    assert names == START_TYPES
    (query,) = _prom_queries_of(panel)
    assert query.by == ("start_type",)


def test_bar_gauges_draw_their_colour_as_meant() -> None:
    # The basic display mode fills a bar with its colour at 35% opacity and a solid
    # edge, a quiet grey for the neutral totals; gradient draws one solid colour when
    # the colour mode is neither thresholds nor continuous, for the green and red of
    # the edit decisions.
    bar_gauges = [panel for panel in _visual_panels() if panel.get("type") == "bargauge"]
    assert bar_gauges
    for panel in bar_gauges:
        colour = _mapping(_defaults(panel).get("color"), "color")
        assert colour == {"fixedColor": "text", "mode": "fixed"}, panel.get("title")
        neutral = not _overrides(panel)
        expected = "basic" if neutral else "gradient"
        assert _options(panel).get("displayMode") == expected, panel.get("title")


def test_built_in_annotation_has_a_named_colour() -> None:
    annotations = _mapping(_dashboard().get("annotations"), "annotations")
    assert annotations.get("list") == [BUILT_IN_ANNOTATION]


def test_api_errors_are_coloured_by_status_class() -> None:
    panel = _panel("API errors by status code")
    found = [
        (str(matcher.get("options")), _mapping(props.get("color"), "c").get("fixedColor"))
        for matcher, props in _overrides(panel)
        if matcher.get("id") == "byRegexp"
    ]
    assert found == list(STATUS_COLOURS)


# Panel options.


def test_stats_stay_neutral() -> None:
    # No thresholds that would imply a budget nobody set; API errors turns red at one.
    for panel in _visual_panels():
        if panel.get("type") != "stat":
            continue
        options, defaults = _options(panel), _defaults(panel)
        assert options.get("graphMode") == "none", panel.get("title")
        assert not {"titleSize", "valueSize"} & set(_mapping(options.get("text", {}), "text"))
        if panel.get("title") == "API errors":
            assert options.get("colorMode") == "value"
            assert defaults.get("color") == {"mode": "thresholds"}
            thresholds = _mapping(defaults.get("thresholds"), "thresholds")
            assert thresholds.get("mode") == "absolute"
            # The base step has no value, as Grafana 13.2 writes it.
            assert thresholds.get("steps") == [{"color": "text"}, {"color": "red", "value": 1}]
        else:
            assert options.get("colorMode") == "none", panel.get("title")
            assert defaults.get("color") == {"mode": "fixed", "fixedColor": "text"}


@pytest.mark.parametrize(
    ("title", "unit", "decimals"),
    [
        ("Cost", "currencyUSD", 2),
        ("Tokens", "short", None),
        # A duration such as "1 hour, 5 minutes" on the stat (dtdurations shows decimals
        # + 1 units); seconds on the bars' axis.
        ("Active time", "dtdurations", 1),
        ("Cost by model over time", "currencyUSD", None),
        ("Cost by model", "currencyUSD", 2),
        ("Who was active", "s", None),
    ],
)
def test_units(title: str, unit: str, decimals: int | None) -> None:
    defaults = _defaults(_panel(title))
    assert defaults.get("unit") == unit
    assert defaults.get("decimals") == decimals


def test_bars_over_time_tile_the_range() -> None:
    # The point at t covers (t, t + interval] (`offset -$__interval`): each bar is drawn
    # after its timestamp, and the bars of one panel stack.
    for panel in _visual_panels():
        if panel.get("type") != "timeseries":
            continue
        assert panel.get("interval") == MIN_INTERVAL, panel.get("title")
        assert panel.get("maxDataPoints") == MAX_DATA_POINTS, panel.get("title")
        custom = _mapping(_defaults(panel).get("custom"), "custom")
        assert custom.get("drawStyle") == "bars", panel.get("title")
        assert custom.get("barAlignment") == 1, panel.get("title")
        assert _mapping(custom.get("stacking"), "stacking").get("mode") == "normal"
        options = _options(panel)
        legend = _mapping(options.get("legend"), "legend")
        assert (legend.get("displayMode"), legend.get("placement")) == ("list", "bottom")
        assert legend.get("showLegend") is True
        tooltip = _mapping(options.get("tooltip"), "tooltip")
        assert (tooltip.get("mode"), tooltip.get("sort")) == ("multi", "desc")


def test_bars_over_time_start_at_zero() -> None:
    # Grafana fits an axis to the data, so a single bucket of stacked bars started at
    # 1.27 minutes and hid most of the lower series (seen in the browser). The lines
    # panel goes below zero for removed lines.
    for panel in _visual_panels():
        if panel.get("type") == "timeseries":
            expected = None if panel.get("title") == "Lines changed" else 0
            assert _defaults(panel).get("min") == expected, panel.get("title")


def test_events_log_options() -> None:
    options = _options(_panel("Recent events"))
    assert options.get("sortOrder") == "Descending"
    assert options.get("enableLogDetails") is True
    assert options.get("wrapLogMessage") is True
    assert options.get("showTime") is True
    assert options.get("showLabels") is False


# Provisioning.


def test_provider_loads_the_dashboards_directory_read_only() -> None:
    document = _yaml(PROVIDER)
    assert document.get("apiVersion") == 1
    providers = _list(document.get("providers"), "providers")
    assert len(providers) == 1
    provider = _mapping(providers[0], "provider")
    assert provider == {
        "name": "cc-observatory",
        "type": "file",
        "folder": "Claude Code",
        "folderUid": "claude-code",
        # The file is the source of truth: Grafana refuses to save or delete the
        # dashboard; the anonymous Editor can save a copy under another uid.
        "disableDeletion": True,
        "allowUiUpdates": False,
        "updateIntervalSeconds": 30,
        "options": {"path": CONTAINER_DASHBOARDS, "foldersFromFilesStructure": False},
    }
    assert _host_path(f"{CONTAINER_DASHBOARDS}/{DASHBOARD.name}") == DASHBOARD


def test_home_page_is_the_overview() -> None:
    home = _environment().get(HOME_VARIABLE)
    assert home == f"{CONTAINER_DASHBOARDS}/{DASHBOARD.name}"
    assert _host_path(home).is_file()
