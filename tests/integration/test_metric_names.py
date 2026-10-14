"""Telemetry names as Prometheus and Loki store them, compared with
``ccobs.domain.telemetry`` (ADR 0014), in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. The metrics and events of every
fixture scenario are replayed through the collector with fresh session IDs, plus one
synthetic ``claude_code.pull_request.count`` point, which no fixture has: a copy of the
fixtures' ``claude_code.commit.count`` point (same resource, scope, attributes, unit and
temporality) under the name Claude Code's docs give, in a session of its own. The tests
read back metric names, labels and totals from Prometheus, and index labels, structured
metadata and event names from Loki, for the run's sessions only (every module of the
test session shares the stack), and compare them with the domain's constants.
"""

from __future__ import annotations

import copy
import http.client
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest

from ccobs.domain.telemetry import (
    COMMIT_COUNT,
    DEFAULT_HOST,
    EVENT_BODY_PREFIX,
    EVENTS,
    HOST_LABEL,
    JOB_LABEL,
    LOG_CONTENT_KEYS,
    LOKI_INDEX_LABELS,
    LOKI_METADATA,
    LOKI_OPTIONAL_METADATA,
    METRICS,
    PULL_REQUEST_COUNT,
    SERVICE_NAME,
    TARGET_INFO,
    TARGET_INFO_LABELS,
    Metric,
    is_identity_key,
    total_query,
)
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    Prepared,
    expected_totals,
    load_scenario,
    post_otlp,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

# The scenario whose commit.count point becomes the synthetic pull_request.count point,
# and the description Claude Code's monitoring docs give the metric.
COMMIT_SCENARIO: Final = "headless-edit-commit-tasks"
PULL_REQUEST_DESCRIPTION: Final = "Number of pull requests created"
# Totals use the range of ADR 0013's examples; a replayed session spans at most 150 s.
WINDOW: Final = "1h"
RANGE_SECONDS: Final = 3600
# Queries are evaluated this many seconds ahead of this machine's clock, a margin past
# the latest replayed sample (as in test_stack_metrics.py).
EVALUATION_MARGIN: Final = 60.0
# Beyond this skew between Prometheus's clock and this machine's, the replayed samples
# land outside the out-of-order window or the query ranges.
MAX_CLOCK_SKEW: Final = 120.0
# Seconds to wait until every replayed series and record can be queried, and between
# two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
HTTP_TIMEOUT: Final = 10.0
REL_TOL: Final = 1e-9
NANOS_PER_SECOND: Final = 1_000_000_000
# Loki's default cap on the entries one query returns; every scenario has fewer records.
QUERY_LIMIT: Final = 5000
# Separates index labels ("stream") from structured metadata in Loki's answers.
CATEGORIZE: Final = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}
# A label value with every character class the PromQL string literal must escape,
# U+FFFD included: Prometheus's lexer refuses it raw ("invalid UTF-8 rune").
TRICKY_VALUE: Final = 'q"b\\n\nt\tr\rc\x01d\x7fé😀\ufffdz'

type Series = dict[str, str]


@dataclass(frozen=True)
class Sent:
    """The scenarios replayed, the synthetic pull request point's session and the total
    its copied points add up to, and the start of the window that holds everything
    sent."""

    runs: tuple[Prepared, ...]
    pull_request_session: str
    pull_request_total: float
    start_ns: int

    def session_ids(self) -> list[str]:
        return [*(run.session_id for run in self.runs), self.pull_request_session]

    def expected_series(self) -> dict[str, int]:
        """How many metric series each session should have in Prometheus."""
        counts = {run.session_id: len(run.expected) for run in self.runs}
        return {**counts, self.pull_request_session: 1}


@dataclass(frozen=True)
class Entry:
    """One Loki entry: index labels, line and structured metadata."""

    labels: Mapping[str, str]
    line: str
    metadata: Mapping[str, str]


# OTLP requests.


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _attributes(owner: JSONObject) -> dict[str, JSON]:
    return {str(item.get("key")): item.get("value") for item in _objects(owner.get("attributes"))}


def _metric_names(request: JSONObject) -> Iterator[str]:
    for resource in _objects(request.get("resourceMetrics")):
        for scope in _objects(resource.get("scopeMetrics")):
            for metric in _objects(scope.get("metrics")):
                yield str(metric.get("name"))


def _records(run: Prepared) -> Iterator[JSONObject]:
    for request in run.logs:
        for resource in _objects(request.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                yield from _objects(scope.get("logRecords"))


def _pull_request_request(run: Prepared, session_id: str) -> JSONObject:
    """An export request with ``run``'s commit.count point renamed to
    ``claude_code.pull_request.count``, in the session ``session_id``."""
    for request in run.metrics:
        for resource in _objects(request.get("resourceMetrics")):
            for scope in _objects(resource.get("scopeMetrics")):
                for metric in _objects(scope.get("metrics")):
                    if metric.get("name") != COMMIT_COUNT.otel_name:
                        continue
                    renamed = copy.deepcopy(metric)
                    renamed["name"] = PULL_REQUEST_COUNT.otel_name
                    renamed["description"] = PULL_REQUEST_DESCRIPTION
                    data = renamed.get("sum")
                    points = _objects(data.get("dataPoints")) if isinstance(data, dict) else []
                    assert points, f"{COMMIT_COUNT.otel_name} has no data point"
                    for point in points:
                        for attribute in _objects(point.get("attributes")):
                            if attribute.get("key") == "session.id":
                                attribute["value"] = {"stringValue": session_id}
                    entry: JSONObject = {
                        "resource": copy.deepcopy(resource.get("resource")),
                        "scopeMetrics": [
                            {"scope": copy.deepcopy(scope.get("scope")), "metrics": [renamed]}
                        ],
                    }
                    return {"resourceMetrics": [entry]}
    pytest.fail(f"{run.scenario} sent no {COMMIT_COUNT.otel_name} point")


def _metadata_key(attribute: str) -> str:
    """The structured metadata key Loki gives a log record attribute (ADR 0015)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", attribute)


# HTTP.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _strings(value: object, where: str) -> dict[str, str]:
    return {key: str(item) for key, item in _mapping(value, where).items()}


def _data(
    base: str,
    path: str,
    params: Sequence[tuple[str, str | int]],
    headers: Mapping[str, str] | None = None,
) -> object:
    """The ``data`` field of a successful answer to ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        connection.request("GET", f"{path}?{urlencode(params)}", headers=dict(headers or {}))
        response = connection.getresponse()
        status, body = response.status, response.read()
    finally:
        connection.close()
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    document = _mapping(json.loads(body), path)
    assert document.get("status") == "success", document
    return document.get("data")


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


# Prometheus.


def _range(end: float) -> list[tuple[str, str | int]]:
    return [("start", f"{end - RANGE_SECONDS:.3f}"), ("end", f"{end:.3f}")]


def _sessions_matcher(session_ids: Sequence[str]) -> str:
    """Every series of these sessions, whatever its name; session IDs are UUIDs, with
    nothing to escape in the regular expression."""
    return f'{{{JOB_LABEL}="{SERVICE_NAME}",session_id=~"{"|".join(session_ids)}"}}'


def _series(base: str, matcher: str) -> list[Series]:
    params: list[tuple[str, str | int]] = [("match[]", matcher)]
    found = _data(base, "/api/v1/series", params + _range(time.time() + EVALUATION_MARGIN))
    return [_strings(item, "series") for item in _list(found, "series")]


def _query(base: str, promql: str) -> list[tuple[Series, float]]:
    """Each series of the instant vector ``promql``, evaluated ``EVALUATION_MARGIN``
    seconds ahead, with its value."""
    params: list[tuple[str, str | int]] = [
        ("query", promql),
        ("time", f"{time.time() + EVALUATION_MARGIN:.3f}"),
    ]
    data = _mapping(_data(base, "/api/v1/query", params), "data")
    assert data.get("resultType") == "vector", f"{promql}: {data}"
    found: list[tuple[Series, float]] = []
    for item in _list(data.get("result"), "result"):
        entry = _mapping(item, "result entry")
        value = _list(entry.get("value"), "value")
        found.append((_strings(entry.get("metric"), "metric"), float(str(value[1]))))
    return found


def _of(series: list[Series], metric: Metric) -> list[Series]:
    return [labels for labels in series if labels.get("__name__") == metric.name]


# Loki.


def _entries(base: str, session_id: str, start_ns: int) -> list[Entry]:
    logql = f'{{service_name="{SERVICE_NAME}"}} | session_id="{session_id}"'
    params: list[tuple[str, str | int]] = [
        ("query", logql),
        ("start", start_ns),
        ("end", time.time_ns() + 60 * NANOS_PER_SECOND),
        ("limit", QUERY_LIMIT),
        ("direction", "forward"),
    ]
    data = _mapping(_data(base, "/loki/api/v1/query_range", params, CATEGORIZE), "data")
    assert data.get("resultType") == "streams", data.get("resultType")
    entries: list[Entry] = []
    for stream in _list(data.get("result"), "result"):
        item = _mapping(stream, "stream")
        labels = _strings(item.get("stream"), "stream labels")
        for value in _list(item.get("values"), "values"):
            parts = _list(value, "value")
            categories = _mapping(parts[2], "categories") if len(parts) > 2 else {}
            metadata = _strings(categories.get("structuredMetadata") or {}, "structuredMetadata")
            entries.append(Entry(labels, str(parts[1]), metadata))
    assert len(entries) < QUERY_LIMIT, f"{logql} hit the query limit"
    return entries


# Fixtures.


@pytest.fixture(scope="module")
def clock(prometheus_url: str) -> float:
    """Seconds by which Prometheus's clock is ahead of this machine's; fails at once
    beyond ``MAX_CLOCK_SKEW``, since the replayed samples carry this machine's time."""
    before = time.time()
    data = _mapping(_data(prometheus_url, "/api/v1/query", [("query", "time()")]), "data")
    server = float(str(_list(data.get("result"), "result")[1]))
    skew = server - (before + time.time()) / 2
    if abs(skew) > MAX_CLOCK_SKEW:
        pytest.fail(
            f"Prometheus's clock (the Docker VM's) is {skew:+.0f} s off this machine's: "
            "restart Docker Desktop (on Windows, run `wsl --shutdown` first) and run the "
            "tests again.",
            pytrace=False,
        )
    return skew


@pytest.fixture(scope="module")
def sent(otlp_http_url: str, clock: float) -> Sent:
    """Every scenario's metrics and events, and the synthetic pull request point."""
    now_ns = time.time_ns()
    names = scenario_names(with_otlp=True)
    assert COMMIT_SCENARIO in names
    runs = tuple(prepare(load_scenario(name), now_ns=now_ns) for name in names)
    for run in runs:
        send(otlp_http_url, run, signals=("metrics", "logs"))
    commit = next(run for run in runs if run.scenario == COMMIT_SCENARIO)
    session_id = str(uuid.uuid4())
    request = _pull_request_request(commit, session_id)
    # The replayer's totals of the copied points: the same as commit.count's.
    totals = expected_totals([request])
    assert {series.metric for series in totals} == {PULL_REQUEST_COUNT.otel_name}
    total = float(sum(totals.values()))
    assert total == float(commit.expected_total(COMMIT_COUNT.otel_name))
    post_otlp(otlp_http_url, "metrics", request)
    times = [int(str(record.get("timeUnixNano"))) for run in runs for record in _records(run)]
    return Sent(runs, session_id, total, min(times, default=now_ns) - NANOS_PER_SECOND)


@pytest.fixture(scope="module")
def stored_series(prometheus_url: str, sent: Sent) -> list[Series]:
    """Every metric series of the sent sessions, once each session has all of them or
    the deadline has passed (the tests then report the difference)."""
    expected = sent.expected_series()

    def read() -> list[Series]:
        return _series(prometheus_url, _sessions_matcher(sent.session_ids()))

    def done(found: list[Series]) -> bool:
        counts = dict.fromkeys(expected, 0)
        for labels in found:
            counts[labels.get("session_id", "")] = counts.get(labels.get("session_id", ""), 0) + 1
        return counts == expected

    return _poll(read, done, DEADLINE)


@pytest.fixture(scope="module")
def stored_entries(loki_url: str, sent: Sent) -> dict[str, list[Entry]]:
    """Each sent session's Loki entries, once every record is there or the deadline has
    passed."""
    expected = {run.session_id: sum(1 for _ in _records(run)) for run in sent.runs}

    def read() -> dict[str, list[Entry]]:
        return {
            session_id: _entries(loki_url, session_id, sent.start_ns) for session_id in expected
        }

    def done(found: dict[str, list[Entry]]) -> bool:
        return {session_id: len(entries) for session_id, entries in found.items()} == expected

    return _poll(read, done, DEADLINE)


# Prometheus names.


def test_every_domain_metric_was_sent(sent: Sent) -> None:
    names = {
        name for run in sent.runs for request in run.metrics for name in _metric_names(request)
    }
    assert names | {PULL_REQUEST_COUNT.otel_name} == {metric.otel_name for metric in METRICS}


def test_stored_metric_names_are_the_domain_names(
    prometheus_url: str, sent: Sent, stored_series: list[Series]
) -> None:
    assert set(sent.expected_series()) == {labels.get("session_id") for labels in stored_series}
    params: list[tuple[str, str | int]] = [("match[]", _sessions_matcher(sent.session_ids()))]
    found = _data(
        prometheus_url,
        "/api/v1/label/__name__/values",
        params + _range(time.time() + EVALUATION_MARGIN),
    )
    names = set(map(str, _list(found, "names")))
    assert {name for name in names if name.startswith("claude_code_")} == names
    assert names == {metric.name for metric in METRICS}


def test_pull_request_count_is_stored_under_its_domain_name(
    prometheus_url: str, sent: Sent, stored_series: list[Series]
) -> None:
    # Its name is observed through the pipeline with the synthetic point; its labels are
    # those of the copied commit.count point (ADR 0014).
    found = [
        labels for labels in stored_series if labels.get("session_id") == sent.pull_request_session
    ]
    assert [labels.get("__name__") for labels in found] == [PULL_REQUEST_COUNT.name]
    assert set(found[0]) - {"__name__"} == PULL_REQUEST_COUNT.all_labels
    query = total_query(
        PULL_REQUEST_COUNT,
        by=("session_id",),
        matchers={"session_id": sent.pull_request_session},
        window=WINDOW,
    )
    totals = _query(prometheus_url, query)
    assert [labels for labels, _ in totals] == [{"session_id": sent.pull_request_session}]
    assert math.isclose(totals[0][1], sent.pull_request_total, rel_tol=REL_TOL), totals


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.otel_name)
def test_each_metric_carries_the_domain_labels(metric: Metric, stored_series: list[Series]) -> None:
    series = _of(stored_series, metric)
    assert series, f"no {metric.name} series"
    seen: set[str] = set()
    for labels in series:
        names = set(labels) - {"__name__"}
        assert labels.get(JOB_LABEL) == SERVICE_NAME
        # The value .env.example gives the host label (ADR 0022).
        assert labels.get(HOST_LABEL) == DEFAULT_HOST
        missing = ({JOB_LABEL} | metric.labels) - names
        assert not missing, f"{labels} lacks {sorted(missing)}"
        seen |= names
    # Nothing outside the domain's labels, and every label the domain lists was seen.
    assert seen == metric.all_labels


def test_target_info_carries_the_other_resource_attributes(
    prometheus_url: str, stored_series: list[Series]
) -> None:
    assert stored_series
    found = _series(prometheus_url, f'{TARGET_INFO}{{{JOB_LABEL}="{SERVICE_NAME}"}}')
    assert found, f"no {TARGET_INFO} series for {SERVICE_NAME}"
    for labels in found:
        assert set(labels) - {"__name__"} == TARGET_INFO_LABELS, labels
        assert labels.get(HOST_LABEL) == DEFAULT_HOST, labels


def test_prometheus_has_no_metadata_for_otlp_metrics(
    prometheus_url: str, stored_series: list[Series]
) -> None:
    # Types and units come from ADR 0012 and the domain, not from Prometheus (ADR 0014).
    assert stored_series
    for metric in METRICS:
        found = _data(prometheus_url, "/api/v1/metadata", [("metric", metric.name)])
        assert found == {}, metric.name


@pytest.mark.parametrize(
    "metric",
    [metric for metric in METRICS if metric is not PULL_REQUEST_COUNT],
    ids=lambda metric: metric.otel_name,
)
def test_total_query_gives_each_sessions_replayed_total(
    prometheus_url: str, sent: Sent, stored_series: list[Series], metric: Metric
) -> None:
    assert stored_series
    for run in sent.runs:
        series = run.expected_series(metric.otel_name)
        query = total_query(
            metric, by=("session_id",), matchers={"session_id": run.session_id}, window=WINDOW
        )
        found = _query(prometheus_url, query)
        if not series:
            assert found == [], f"{run.scenario}: {query} gave {found}"
            continue
        expected = float(sum(series.values()))
        assert len(found) == 1, f"{run.scenario}: {query} gave {found}"
        labels, value = found[0]
        assert labels == {"session_id": run.session_id}
        assert math.isclose(value, expected, rel_tol=REL_TOL), (
            f"{run.scenario}: {query} = {value!r}, expected {expected!r}"
        )


def test_prometheus_parses_the_escaped_label_values(
    prometheus_url: str, stored_series: list[Series]
) -> None:
    assert stored_series
    for metric in METRICS:
        query = total_query(
            metric, by=("session_id",), matchers={"session_id": TRICKY_VALUE}, window=WINDOW
        )
        assert _query(prometheus_url, query) == [], query


# Loki names.


def test_loki_index_labels_are_the_domain_labels(
    loki_url: str, sent: Sent, stored_entries: dict[str, list[Entry]]
) -> None:
    window: list[tuple[str, str | int]] = [
        ("start", sent.start_ns),
        ("end", time.time_ns() + 60 * NANOS_PER_SECOND),
    ]
    labels = set(map(str, _list(_data(loki_url, "/loki/api/v1/labels", window), "labels")))
    assert labels == LOKI_INDEX_LABELS
    for run in sent.runs:
        entries = stored_entries[run.session_id]
        assert entries, run.scenario
        for entry in entries:
            assert set(entry.labels) == LOKI_INDEX_LABELS
            assert entry.labels.get("service_name") == SERVICE_NAME
            assert entry.labels.get(HOST_LABEL) == DEFAULT_HOST


def test_loki_metadata_is_the_domain_keys_plus_the_event_attributes(
    sent: Sent, stored_entries: dict[str, list[Entry]]
) -> None:
    domain = LOKI_METADATA | LOKI_OPTIONAL_METADATA
    seen: set[str] = set()
    for run in sent.runs:
        # event.sequence is unique within a session: it pairs each entry with its record.
        sent_keys: dict[str, set[str]] = {}
        for record in _records(run):
            attributes = _attributes(record)
            sequence = attributes.get("event.sequence")
            kept = {
                _metadata_key(key)
                for key in attributes
                if not is_identity_key(key) and key not in LOG_CONTENT_KEYS
            }
            assert isinstance(sequence, dict), record
            sent_keys[str(sequence.get("intValue", sequence.get("stringValue")))] = kept
        entries = stored_entries[run.session_id]
        assert len(entries) == len(sent_keys), run.scenario
        for entry in entries:
            keys = set(entry.metadata)
            seen |= keys
            missing = LOKI_METADATA - keys
            assert not missing, f"{run.scenario}: an entry lacks {sorted(missing)}"
            # Outside the domain's keys, an entry has its own event's attributes, with
            # dots turned into underscores, and nothing else.
            own = sent_keys[entry.metadata["event_sequence"]]
            assert keys - domain == own - domain, f"{run.scenario}: {entry.line}"
    assert seen & LOKI_OPTIONAL_METADATA == LOKI_OPTIONAL_METADATA


def test_loki_event_names_and_bodies_are_the_domain_events(
    sent: Sent, stored_entries: dict[str, list[Entry]]
) -> None:
    names: set[str] = set()
    for run in sent.runs:
        for entry in stored_entries[run.session_id]:
            name = entry.metadata.get("event_name", "")
            assert entry.line == f"{EVENT_BODY_PREFIX}{name}"
            names.add(name)
    assert names == EVENTS
