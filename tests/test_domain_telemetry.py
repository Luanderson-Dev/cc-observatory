"""Unit tests of ``ccobs.domain.telemetry``, the stored telemetry names (ADR 0014).

They compare the constants with the OTLP fixtures (ADR 0012) and with the translation
Prometheus applied to them (ADR 0013), the privacy lists with the replayer's canaries and
the keys the collector config deletes (ADR 0021), and the host label with the configs
that set, promote and index it (ADR 0022), and check the PromQL that ``total_query``
builds. ``tests/integration/test_metric_names.py`` compares the same
constants with what a running stack stores.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
import yaml

import ccobs.domain
from ccobs.domain import telemetry
from ccobs.domain.telemetry import (
    CODE_EDIT_TOOL_DECISION,
    COMMIT_COUNT,
    COMMON_LABELS,
    CONTENT_EVENTS,
    CONTENT_SPAN_EVENT,
    COST_USAGE,
    DATA_POINT_CONTENT_KEYS,
    DEFAULT_HOST,
    EVENT_BODY_PREFIX,
    EVENTS,
    HOST_LABEL,
    IDENTITY_KEYS,
    IDENTITY_PREFIXES,
    JOB_LABEL,
    LOG_CONTENT_KEYS,
    LOKI_INDEX_LABELS,
    LOKI_METADATA,
    LOKI_OPTIONAL_METADATA,
    METRICS,
    OPTIONAL_COMMON_LABELS,
    PROMOTED_LABELS,
    PULL_REQUEST_COUNT,
    RESOURCE_CONTENT_KEYS,
    SERVICE_NAME,
    SESSION_COUNT,
    SPAN_CONTENT_KEYS,
    SPAN_EVENT_CONTENT_KEYS,
    TARGET_INFO,
    TARGET_INFO_LABELS,
    TOKEN_USAGE,
    Metric,
    is_identity_key,
    selector,
    total_query,
)
from tools.stack import replay

ROOT: Final = Path(__file__).resolve().parents[1]
DOMAIN: Final = ROOT / "src" / "ccobs" / "domain"
COLLECTOR_CONFIG: Final = ROOT / "stack" / "otel-collector" / "config.yaml"
PROMETHEUS_CONFIG: Final = ROOT / "stack" / "prometheus" / "prometheus.yaml"
LOKI_CONFIG: Final = ROOT / "stack" / "loki" / "config.yaml"
OTLP_FIXTURES: Final = replay.DEFAULT_FIXTURES / "otlp"
# The collector statement that sets the host label from CCOBS_HOST_LABEL (ADR 0022).
HOST_SET: Final = re.compile(
    r'^set\(resource\.attributes\["(?P<key>[^"]+)"\], '
    r'"\$\{env:CCOBS_HOST_LABEL:-(?P<default>[^}"]+)\}"\)$'
)

# A classic Prometheus label name, which PromQL needs no quotes for.
LABEL_NAME: Final = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# The unit suffix Prometheus appended to each unit the fixtures carry (ADR 0013).
UNIT_SUFFIXES: Final = {"": "", "s": "seconds", "tokens": "tokens", "USD": "USD"}
# Modules ccobs.domain may import: none of them does I/O.
# json is there for json.dumps, which the hook parser uses to size tool inputs and
# responses (ADR 0023); with open banned below, the domain has no file to dump to or load.
# ccobs.domain.hooks is a domain module, held to these same rules: the session reducer
# consumes its events (ADR 0024). urllib.parse is string handling only (quoting and
# encoding query strings, for the Grafana links); it opens no connection.
ALLOWED_IMPORTS: Final = frozenset(
    {"__future__", "collections.abc", "dataclasses", "datetime", "enum", "json", "re", "typing"}
    | {"urllib.parse"}
    # Pure arithmetic: math.isfinite and math.fsum (ccobs.domain.usage).
    | {"math"}
    # copy.deepcopy of parsed JSON (ccobs.domain.settings_merge).
    | {"copy"}
    # Domain modules, each one checked by the same test.
    | {
        "ccobs.domain.alerts",
        "ccobs.domain.denials",
        "ccobs.domain.diagnostics",
        "ccobs.domain.durations",
        "ccobs.domain.grafana_links",
        "ccobs.domain.hook_config",
        "ccobs.domain.hooks",
        "ccobs.domain.sessions",
        "ccobs.domain.settings_merge",
        "ccobs.domain.telemetry",
        "ccobs.domain.telemetry_settings",
        "ccobs.domain.usage",
    }
)
# Builtins that read or write outside the process, or run code from strings.
IO_BUILTINS: Final = frozenset({"open", "print", "input", "__import__", "exec", "eval", "compile"})
# The resource attribute that becomes Prometheus's job label instead of a target_info
# label, and Loki's service_name index label instead of structured metadata.
SERVICE_NAME_ATTRIBUTE: Final = "service.name"
# Fields Loki adds to every entry (ADR 0015).
LOKI_ADDED: Final = frozenset(
    {"scope_name", "scope_version", "observed_timestamp", "detected_level"}
)
# The session, event and trace fields queries rely on (ADR 0015).
QUERY_FIELDS: Final = frozenset(
    {"session_id", "event_name", "event_sequence", "event_timestamp", "prompt_id"}
    | {"trace_id", "span_id"}
)
# Keys near the identity rule: identity or not, the domain must agree with the replayer.
NEAR_IDENTITY: Final = (
    "user",
    "user_prompt",
    "username",
    "enduser",
    "enduser_id",
    "xuser.id",
    "superuser.id",
    "host.arch",
    "host.id",
    "organization",
    "organization.name",
    "identity",
    "session.id",
    "service.name",
)
# A value with every character class that total_query must escape or keep; U+FFFD is
# escaped because Prometheus's lexer refuses it raw ("invalid UTF-8 rune").
TRICKY_VALUE: Final = 'q"b\\n\nt\tr\rc\x01d\x7fé😀\ufffdz'
TRICKY_LITERAL: Final = r'"q\"b\\n\nt\tr\rc\x01d\x7fé😀\ufffdz"'


def _label(key: str) -> str:
    """The Prometheus label, or Loki structured metadata key, of an OTel attribute."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def _prometheus_name(otel_name: str, unit: str) -> str:
    """The translation Prometheus applied to Claude Code's monotonic sums: words joined
    by underscores, a ``total`` word moved to the end, the unit's suffix, then
    ``_total``."""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", otel_name) if word != "total"]
    suffix = UNIT_SUFFIXES[unit]
    if suffix and suffix not in words:
        words.append(suffix)
    return "_".join([*words, "total"])


# The fixtures.


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {str(key): item for key, item in entry.items()}
        for entry in value
        if isinstance(entry, dict)
    ]


def _requests(signal: str) -> Iterator[dict[str, object]]:
    for path in sorted(OTLP_FIXTURES.glob(f"*/{signal}.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                document = json.loads(line)
                assert isinstance(document, dict), path
                yield document


def _keys(owner: dict[str, object]) -> set[str]:
    return {str(item.get("key")) for item in _objects(owner.get("attributes"))}


def _string(owner: dict[str, object], key: str) -> str | None:
    for item in _objects(owner.get("attributes")):
        value = item.get("value")
        if item.get("key") == key and isinstance(value, dict):
            text = value.get("stringValue")
            return text if isinstance(text, str) else None
    return None


@dataclass
class FixtureMetric:
    """One metric across the fixtures: its units, and the attribute keys of each point."""

    units: set[str] = field(default_factory=set)
    points: list[set[str]] = field(default_factory=list)

    def labels(self) -> tuple[set[str], set[str]]:
        """The labels on every point, and on any point, without identity attributes."""
        kept = [{_label(key) for key in point if not is_identity_key(key)} for point in self.points]
        return set.intersection(*kept), set.union(*kept)


def _fixture_metrics() -> dict[str, FixtureMetric]:
    found: dict[str, FixtureMetric] = defaultdict(FixtureMetric)
    for request in _requests("metrics"):
        for resource in _objects(request.get("resourceMetrics")):
            for scope in _objects(resource.get("scopeMetrics")):
                for metric in _objects(scope.get("metrics")):
                    entry = found[str(metric.get("name"))]
                    entry.units.add(str(metric.get("unit") or ""))
                    data = metric.get("sum")
                    assert isinstance(data, dict), metric.get("name")
                    assert data.get("isMonotonic") is True, metric.get("name")
                    entry.points += [_keys(point) for point in _objects(data.get("dataPoints"))]
    return dict(found)


def _resource_keys(signal: str) -> set[str]:
    layout = {"metrics": "resourceMetrics", "logs": "resourceLogs"}
    return {
        key
        for request in _requests(signal)
        for resource in _objects(request.get(layout[signal]))
        for key in _keys(_mapping(resource.get("resource")))
    }


def _log_records() -> Iterator[dict[str, object]]:
    for request in _requests("logs"):
        for resource in _objects(request.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                yield from _objects(scope.get("logRecords"))


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return {str(key): item for key, item in value.items()}


FIXTURE_METRICS: Final = _fixture_metrics()


# Metrics.


def test_metric_names_are_unique() -> None:
    assert len({metric.otel_name for metric in METRICS}) == len(METRICS)
    assert len({metric.name for metric in METRICS}) == len(METRICS)


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.otel_name)
def test_prometheus_names_follow_the_observed_translation(metric: Metric) -> None:
    assert metric.otel_name.startswith("claude_code.")
    assert metric.name == _prometheus_name(metric.otel_name, metric.unit)
    assert LABEL_NAME.fullmatch(metric.name)


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.otel_name)
def test_labels_are_valid_and_split_into_always_and_optional(metric: Metric) -> None:
    assert all(LABEL_NAME.fullmatch(label) for label in metric.all_labels)
    assert not any(label.startswith("__") for label in metric.all_labels)
    assert not metric.labels & metric.optional_labels
    assert metric.all_labels == {JOB_LABEL} | metric.labels | metric.optional_labels
    # Every metric carries the standard attributes (ADR 0012) and the host label that
    # Prometheus promotes from the resource (ADR 0022).
    assert metric.labels >= COMMON_LABELS
    assert metric.labels >= PROMOTED_LABELS
    assert metric.optional_labels >= OPTIONAL_COMMON_LABELS
    # job comes from service.name, never from a data point attribute.
    assert JOB_LABEL not in metric.labels | metric.optional_labels


def test_common_labels() -> None:
    assert {"session_id", "terminal_type"} == COMMON_LABELS
    # app.version only with OTEL_METRICS_INCLUDE_VERSION=true, as in the capture.
    assert {"app_version"} == OPTIONAL_COMMON_LABELS
    assert JOB_LABEL == "job"
    assert SERVICE_NAME == "claude-code"


def test_the_host_label() -> None:
    # A constant the user can change through CCOBS_HOST_LABEL, never the machine's
    # hostname, which is personal data (ADR 0021, ADR 0022).
    assert HOST_LABEL == "host"
    assert DEFAULT_HOST == "local"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", DEFAULT_HOST)
    # The only resource attribute Prometheus promotes (ADR 0013), on every series of
    # every metric, and on target_info too; and one of Loki's two index labels.
    assert {HOST_LABEL} == PROMOTED_LABELS
    assert all(HOST_LABEL in metric.labels for metric in METRICS)
    assert HOST_LABEL in TARGET_INFO_LABELS
    assert HOST_LABEL in LOKI_INDEX_LABELS
    assert not is_identity_key(HOST_LABEL)
    # The attribute comes from the collector, never from a data point or a record.
    assert HOST_LABEL not in COMMON_LABELS | OPTIONAL_COMMON_LABELS
    assert HOST_LABEL not in LOKI_METADATA | LOKI_OPTIONAL_METADATA


def test_the_fixtures_carry_every_metric_but_pull_request_count() -> None:
    assert set(FIXTURE_METRICS) == {metric.otel_name for metric in METRICS} - {
        PULL_REQUEST_COUNT.otel_name
    }


@pytest.mark.parametrize(
    "metric",
    [metric for metric in METRICS if metric is not PULL_REQUEST_COUNT],
    ids=lambda metric: metric.otel_name,
)
def test_units_and_labels_are_those_of_the_fixtures(metric: Metric) -> None:
    fixture = FIXTURE_METRICS[metric.otel_name]
    assert fixture.units == {metric.unit}
    # The data point attributes, plus the host label Prometheus promotes from the
    # resource, which the collector sets (ADR 0022).
    on_points, on_any_point = fixture.labels()
    always, anywhere = on_points | PROMOTED_LABELS, on_any_point | PROMOTED_LABELS
    # Optional labels are those some points lack, plus the standard attributes that
    # depend on a setting (the capture turned app.version on).
    assert always == metric.labels | (always & OPTIONAL_COMMON_LABELS)
    assert anywhere == metric.labels | metric.optional_labels


def test_pull_request_count_has_the_shape_of_commit_count() -> None:
    # Documented (ADR 0012) but never captured; its stored name was observed by sending
    # a copy of the commit.count point under its name (ADR 0014).
    assert PULL_REQUEST_COUNT.otel_name == "claude_code.pull_request.count"
    assert PULL_REQUEST_COUNT.name == "claude_code_pull_request_count_total"
    assert PULL_REQUEST_COUNT.unit == COMMIT_COUNT.unit == ""
    assert PULL_REQUEST_COUNT.labels == COMMIT_COUNT.labels
    assert PULL_REQUEST_COUNT.optional_labels == COMMIT_COUNT.optional_labels


def test_the_observed_names() -> None:
    assert {metric.otel_name: metric.name for metric in METRICS} == {
        "claude_code.session.count": "claude_code_session_count_total",
        "claude_code.active_time.total": "claude_code_active_time_seconds_total",
        "claude_code.token.usage": "claude_code_token_usage_tokens_total",
        "claude_code.cost.usage": "claude_code_cost_usage_USD_total",
        "claude_code.lines_of_code.count": "claude_code_lines_of_code_count_total",
        "claude_code.code_edit_tool.decision": "claude_code_code_edit_tool_decision_total",
        "claude_code.commit.count": "claude_code_commit_count_total",
        "claude_code.pull_request.count": "claude_code_pull_request_count_total",
    }


def test_target_info_holds_the_other_resource_attributes() -> None:
    assert TARGET_INFO == "target_info"
    resource = _resource_keys("metrics") - {SERVICE_NAME_ATTRIBUTE}
    # The promoted host label stays on target_info as well (ADR 0022).
    labels = {JOB_LABEL} | {_label(key) for key in resource} | PROMOTED_LABELS
    assert labels == TARGET_INFO_LABELS
    # No instance: Claude Code sends no service.instance.id.
    assert "instance" not in TARGET_INFO_LABELS


# Loki.


def test_loki_indexes_the_service_name_and_the_host() -> None:
    # One stream per machine (ADR 0015), told apart by the host label (ADR 0022).
    assert {"service_name", HOST_LABEL} == LOKI_INDEX_LABELS
    assert not LOKI_INDEX_LABELS & (LOKI_METADATA | LOKI_OPTIONAL_METADATA)


def test_loki_metadata_holds_the_record_and_resource_fields() -> None:
    assert not LOKI_METADATA & LOKI_OPTIONAL_METADATA
    keys = LOKI_METADATA | LOKI_OPTIONAL_METADATA
    assert keys >= QUERY_FIELDS
    assert keys >= LOKI_ADDED
    assert all(LABEL_NAME.fullmatch(key) for key in keys)
    # Resource attributes other than service.name are structured metadata.
    resource = _resource_keys("logs") - {SERVICE_NAME_ATTRIBUTE}
    assert {_label(key) for key in resource} <= LOKI_METADATA
    # Record attributes on every record are on every entry (app.version depends on a
    # setting); prompt.id is on the records after the first prompt only (ADR 0012).
    records = [
        {_label(key) for key in _keys(record) if not is_identity_key(key)}
        for record in _log_records()
    ]
    on_every = set.intersection(*records) - OPTIONAL_COMMON_LABELS
    assert on_every <= LOKI_METADATA
    assert set.union(*records) & LOKI_OPTIONAL_METADATA == {"app_version", "prompt_id"}


def test_events_are_those_of_the_fixtures() -> None:
    names: set[str] = set()
    for record in _log_records():
        name = _string(record, "event.name")
        assert name is not None
        body = record.get("body")
        assert body == {"stringValue": f"{EVENT_BODY_PREFIX}{name}"}
        names.add(name)
    assert names == EVENTS
    assert not EVENTS & CONTENT_EVENTS


# Privacy lists (ADR 0021).


def test_no_identity_or_content_name_is_stored() -> None:
    identity_labels = {_label(key) for key in IDENTITY_KEYS}
    identity_prefixes = tuple(_label(prefix) for prefix in IDENTITY_PREFIXES)
    content = {_label(key) for key in LOG_CONTENT_KEYS | SPAN_CONTENT_KEYS}
    stored = {label for metric in METRICS for label in metric.all_labels}
    stored |= TARGET_INFO_LABELS | LOKI_INDEX_LABELS | LOKI_METADATA | LOKI_OPTIONAL_METADATA
    assert not [label for label in stored if label.startswith(identity_prefixes)]
    assert not stored & identity_labels
    assert not stored & content


def test_identity_lists_are_the_replayers() -> None:
    assert IDENTITY_PREFIXES == replay.IDENTITY_PREFIXES
    assert IDENTITY_KEYS == replay.IDENTITY_KEYS
    assert all(is_identity_key(key) for key in replay.IDENTITY_CANARY_KEYS)


def test_identity_rule_agrees_with_the_replayer() -> None:
    corpus = _fixture_keys() | set(replay.IDENTITY_CANARY_KEYS) | set(NEAR_IDENTITY)
    wrong = sorted(key for key in corpus if is_identity_key(key) != replay.is_identity_key(key))
    assert not wrong
    assert is_identity_key("user.email")
    assert is_identity_key("enduser.id")
    assert is_identity_key("host.name")
    assert not is_identity_key("user_prompt")
    assert not is_identity_key("host.arch")


def test_content_lists_are_the_replayers() -> None:
    host_paths = set(replay.HOST_PATHS_CANARY_KEYS)
    assert set(replay.LOG_CONTENT_CANARY_KEYS) == LOG_CONTENT_KEYS
    assert set(replay.SPAN_CONTENT_CANARY_KEYS) | host_paths == SPAN_CONTENT_KEYS
    assert host_paths == RESOURCE_CONTENT_KEYS
    assert host_paths == DATA_POINT_CONTENT_KEYS
    span_event_content = {
        key for key in replay.SPAN_EVENT_CANARY_KEYS if not replay.is_identity_key(key)
    }
    assert span_event_content == host_paths == SPAN_EVENT_CONTENT_KEYS
    assert set(replay.CONTENT_EVENT_NAMES) == CONTENT_EVENTS
    assert CONTENT_SPAN_EVENT == replay.CONTENT_SPAN_EVENT


# The collector config: read here, never changed.

DELETE_KEY: Final = re.compile(r'^delete_key\((?P<context>\w+)\.attributes, "(?P<key>[^"\\]+)"\)$')
DELETE_MATCHING: Final = re.compile(
    r'^delete_matching_keys\((?P<context>\w+)\.attributes, "(?P<pattern>(?:[^"\\]|\\.)+)"\)$'
)
LOG_CONDITION: Final = re.compile(
    r'^(?:log\.attributes\["event\.name"\]|log\.event_name) == "(?P<name>[^"]+)"$'
    r'|^log\.body == "claude_code\.(?P<body>[^"]+)"$'
)
SPAN_EVENT_CONDITION: Final = re.compile(r'^spanevent\.name == "(?P<name>[^"]+)"$')
STATEMENTS: Final = {
    "metrics": "metric_statements",
    "logs": "log_statements",
    "traces": "trace_statements",
}
# Where transform/content deletes content, per signal and OTTL context (ADR 0021).
CONTENT_CONTEXTS: Final = (
    ("metrics", "resource", RESOURCE_CONTENT_KEYS),
    ("metrics", "datapoint", DATA_POINT_CONTENT_KEYS),
    ("logs", "resource", RESOURCE_CONTENT_KEYS),
    ("logs", "log", LOG_CONTENT_KEYS),
    ("traces", "resource", RESOURCE_CONTENT_KEYS),
    ("traces", "span", SPAN_CONTENT_KEYS),
    ("traces", "spanevent", SPAN_EVENT_CONTENT_KEYS),
)


@dataclass
class Deletions:
    """What the statements of one OTTL context delete: exact keys and key patterns."""

    keys: set[str] = field(default_factory=set)
    patterns: list[re.Pattern[str]] = field(default_factory=list)


def _processor(name: str) -> dict[str, object]:
    config = _mapping(yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8")))
    return _mapping(_mapping(config.get("processors")).get(name))


def _strings(value: object) -> list[str]:
    assert isinstance(value, list), value
    return [str(item) for item in value]


def _deletions(processor: str, signal: str) -> dict[str, Deletions]:
    """The deletions of a transform processor for ``signal``, per OTTL context; any
    other statement fails the test."""
    found: dict[str, Deletions] = {}
    for group in _objects(_processor(processor).get(STATEMENTS[signal])):
        context = str(group.get("context"))
        deletions = found.setdefault(context, Deletions())
        for statement in _strings(group.get("statements")):
            if (match := DELETE_KEY.fullmatch(statement)) and match["context"] == context:
                deletions.keys.add(match["key"])
            elif (match := DELETE_MATCHING.fullmatch(statement)) and match["context"] == context:
                # An OTTL string needs \\ for each backslash of the regular expression.
                deletions.patterns.append(re.compile(re.sub(r"\\(.)", r"\1", match["pattern"])))
            else:
                pytest.fail(f"{processor} {context}: unexpected statement {statement!r}")
    return found


def _fixture_keys() -> set[str]:
    def walk(node: object) -> Iterator[str]:
        if isinstance(node, dict):
            if isinstance(node.get("key"), str) and "value" in node:
                yield str(node["key"])
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return {
        key for signal in replay.SIGNALS for request in _requests(signal) for key in walk(request)
    }


@pytest.mark.parametrize(
    ("signal", "context"),
    [
        ("metrics", "resource"),
        ("metrics", "datapoint"),
        ("logs", "resource"),
        ("logs", "log"),
        ("traces", "resource"),
        ("traces", "span"),
        ("traces", "spanevent"),
    ],
)
def test_collector_deletes_the_domain_identity(signal: str, context: str) -> None:
    deletions = _deletions("transform/identity", signal)[context]
    assert deletions.keys == IDENTITY_KEYS
    assert deletions.patterns
    corpus = _fixture_keys() | set(replay.IDENTITY_CANARY_KEYS) | set(NEAR_IDENTITY)
    for key in sorted(corpus - IDENTITY_KEYS):
        matched = any(pattern.search(key) for pattern in deletions.patterns)
        assert matched == key.startswith(IDENTITY_PREFIXES), key


@pytest.mark.parametrize(("signal", "context", "keys"), CONTENT_CONTEXTS)
def test_collector_deletes_the_domain_content_keys(
    signal: str, context: str, keys: frozenset[str]
) -> None:
    deletions = _deletions("transform/content", signal)
    assert set(deletions) == {where for kind, where, _ in CONTENT_CONTEXTS if kind == signal}
    assert deletions[context].keys == keys
    assert not deletions[context].patterns


def test_collector_drops_the_domain_content_events() -> None:
    content = _processor("filter/content")
    names: dict[str, set[str]] = {"event": set(), "body": set()}
    for condition in _strings(content.get("log_conditions")):
        match = LOG_CONDITION.fullmatch(condition)
        assert match, condition
        if match["name"]:
            names["event"].add(match["name"])
        else:
            names["body"].add(match["body"])
    assert names == {"event": CONTENT_EVENTS, "body": CONTENT_EVENTS}
    span_events = set()
    for condition in _strings(content.get("trace_conditions")):
        match = SPAN_EVENT_CONDITION.fullmatch(condition)
        assert match, condition
        span_events.add(match["name"])
    assert span_events == {CONTENT_SPAN_EVENT}


def test_the_stack_sets_promotes_and_indexes_the_domain_host_label() -> None:
    # The collector sets the resource attribute on every signal with the domain's
    # default (ADR 0022), Prometheus promotes exactly it, and Loki indexes it.
    statements = [
        statement
        for signal in STATEMENTS.values()
        for group in _objects(_processor("transform/host").get(signal))
        if group.get("context") == "resource"
        for statement in _strings(group.get("statements"))
    ]
    assert len(statements) == len(STATEMENTS), statements
    for statement in statements:
        match = HOST_SET.fullmatch(statement)
        assert match, statement
        assert (match["key"], match["default"]) == (HOST_LABEL, DEFAULT_HOST)
    prometheus = _mapping(yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8")))
    promoted = _mapping(prometheus.get("otlp")).get("promote_resource_attributes")
    assert promoted == sorted(PROMOTED_LABELS)
    loki: object = yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))
    for key in ("limits_config", "otlp_config", "resource_attributes"):
        loki = _mapping(loki).get(key)
    indexed = {
        _label(str(attribute))
        for rule in _objects(_mapping(loki).get("attributes_config"))
        if rule.get("action") == "index_label"
        for attribute in _strings(rule.get("attributes"))
    }
    assert indexed == LOKI_INDEX_LABELS


# PromQL.


def test_total_query_sums_increase_anchored_by_the_given_labels() -> None:
    query = total_query(COST_USAGE, by=("session_id",), matchers={"session_id": "abc"}, window="1h")
    assert query == (
        'sum by (session_id) (increase(claude_code_cost_usage_USD_total{session_id="abc"}'
        "[1h] anchored))"
    )


def test_total_query_without_labels_sums_everything() -> None:
    assert total_query(SESSION_COUNT, window="90d") == (
        "sum(increase(claude_code_session_count_total[90d] anchored))"
    )


def test_total_query_keeps_the_order_of_labels_and_matchers() -> None:
    query = total_query(
        TOKEN_USAGE,
        by=("model", "type"),
        matchers={"session_id": "s", "job": SERVICE_NAME, "agent_name": ""},
        window="1h30m",
    )
    assert query == (
        "sum by (model, type) (increase(claude_code_token_usage_tokens_total"
        '{session_id="s",job="claude-code",agent_name=""}[1h30m] anchored))'
    )


def test_total_query_escapes_label_values() -> None:
    query = total_query(COST_USAGE, matchers={"model": TRICKY_VALUE}, window="5m")
    assert query == (
        "sum(increase(claude_code_cost_usage_USD_total{model=" + TRICKY_LITERAL + "}[5m] anchored))"
    )
    assert "\n" not in query
    assert "\r" not in query


def test_selector_quotes_matchers() -> None:
    assert selector(COMMIT_COUNT) == "claude_code_commit_count_total"
    assert selector(COMMIT_COUNT, {}) == "claude_code_commit_count_total"
    assert selector(COMMIT_COUNT, {"session_id": 'a"b'}) == (
        'claude_code_commit_count_total{session_id="a\\"b"}'
    )


@pytest.mark.parametrize(
    "label",
    ["session.id", "1type", "", "ty pe", "type-name", "__name__", "__x", "tÿpe"],
)
def test_invalid_label_names_are_refused(label: str) -> None:
    with pytest.raises(ValueError, match="label"):
        total_query(TOKEN_USAGE, by=(label,), window="1h")
    with pytest.raises(ValueError, match="label"):
        total_query(TOKEN_USAGE, matchers={label: "x"}, window="1h")


@pytest.mark.parametrize(
    ("metric", "label"),
    [
        (COMMIT_COUNT, "model"),
        (SESSION_COUNT, "type"),
        (CODE_EDIT_TOOL_DECISION, "sessionid"),
        (COST_USAGE, "type"),
        (TOKEN_USAGE, "instance"),
    ],
)
def test_labels_a_metric_never_has_are_refused(metric: Metric, label: str) -> None:
    with pytest.raises(ValueError, match=f"{metric.name} has no label {label!r}"):
        total_query(metric, by=(label,), window="1h")
    with pytest.raises(ValueError, match=f"{metric.name} has no label {label!r}"):
        selector(metric, {label: "x"})


def test_repeated_by_labels_are_refused() -> None:
    with pytest.raises(ValueError, match="more than once"):
        total_query(TOKEN_USAGE, by=("model", "model"), window="1h")


def test_by_must_be_a_sequence_of_labels_not_one_string() -> None:
    with pytest.raises(TypeError, match="sequence of label names"):
        total_query(TOKEN_USAGE, by="model", window="1h")


@pytest.mark.parametrize(
    "window", ["", "0s", "0h0m", "1", "1.5h", "h", "1H", "1m1h", " 1h", "1h ", "-1h", "1h]"]
)
def test_invalid_windows_are_refused(window: str) -> None:
    with pytest.raises(ValueError, match="window"):
        total_query(TOKEN_USAGE, window=window)


@pytest.mark.parametrize("window", ["1h", "90d", "5m", "30s", "500ms", "1w", "1y", "1h30m", "0h5m"])
def test_valid_windows_are_kept(window: str) -> None:
    assert total_query(TOKEN_USAGE, window=window).endswith(f"[{window}] anchored))")


def test_values_that_are_not_valid_utf_8_are_refused() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        total_query(TOKEN_USAGE, matchers={"model": "a\udcffb"}, window="1h")


def test_every_label_of_a_metric_can_be_matched() -> None:
    for metric in METRICS:
        matchers = dict.fromkeys(sorted(metric.all_labels), "")
        assert selector(metric, matchers).startswith(metric.name + "{")
        assert total_query(metric, by=sorted(metric.all_labels), window="1h")


# No I/O (CLAUDE.md).


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
    return found


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


@pytest.mark.parametrize("path", sorted(DOMAIN.glob("*.py")), ids=lambda path: path.name)
def test_the_domain_does_no_io(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _imports(tree) <= ALLOWED_IMPORTS, sorted(_imports(tree) - ALLOWED_IMPORTS)
    assert not _called_names(tree) & IO_BUILTINS


def test_the_domain_package_documents_its_rule() -> None:
    # The parametrized test above would pass with no file at all.
    assert {"__init__.py", "telemetry.py"} <= {path.name for path in DOMAIN.glob("*.py")}
    assert telemetry.__doc__
    assert "no I/O" in str(ccobs.domain.__doc__)
