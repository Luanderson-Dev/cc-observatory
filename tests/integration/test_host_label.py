"""The stack's host label (ADR 0022) on every signal, in the isolated ``ccobs-it``
compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. The collector sets the resource
attribute ``host`` from ``CCOBS_HOST_LABEL`` (``local``, from ``.env.example``) right
after the privacy processors. Two replays go through it with fresh session IDs: every
fixture scenario as Claude Code sent it, and one scenario whose copy carries a client's
own ``host`` and ``host.name`` on every resource, data point, log record, span and span
event, as ``OTEL_RESOURCE_ATTRIBUTES`` would put them. Both must end with
``host="local"`` only: on every Prometheus series of the run's sessions, as an index
label of their Loki streams, and as the resource attribute of their Tempo traces, which
TraceQL finds by it. ``host.name`` stays deleted (ADR 0021). Every query is limited to
the run's sessions or traces, since the other modules share the stack.
"""

from __future__ import annotations

import base64
import copy
import http.client
import json
import operator
import re
import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest

from ccobs.domain.telemetry import (
    DEFAULT_HOST,
    HOST_LABEL,
    JOB_LABEL,
    METRICS,
    SERVICE_NAME,
)
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    SIGNALS,
    Prepared,
    Signal,
    load_scenario,
    post_otlp,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

# The attribute a client could send under the machine's name; always deleted (ADR 0021).
HOST_NAME: Final = "host.name"
# Export request layout per signal: resource list, scope list, item list.
LAYOUT: Final[Mapping[Signal, tuple[str, str, str]]] = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}
# Every level where the client's host is planted.
LEVELS: Final = ("resource", "datapoint", "log", "span", "spanevent")
# Prometheus names, by OpenTelemetry name (ADR 0014).
PROMETHEUS_NAMES: Final = {metric.otel_name: metric.name for metric in METRICS}
# Queries cover the last hour; a replayed session spans at most 150 s, and queries reach
# this far past now.
RANGE_SECONDS: Final = 3600
MARGIN_SECONDS: Final = 60
# Beyond this skew between Prometheus's clock and this machine's, the replayed samples
# land outside the out-of-order window or the query ranges.
MAX_CLOCK_SKEW: Final = 120.0
# Seconds to wait until every replayed series, record and trace can be read, and between
# two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
HTTP_TIMEOUT: Final = 10.0
NANOS_PER_SECOND: Final = 1_000_000_000
QUERY_LIMIT: Final = 5000
SEARCH_LIMIT: Final = 100
# Separates index labels ("stream") from structured metadata in Loki's answers.
CATEGORIZE: Final = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}

type Labels = dict[str, str]


@dataclass(frozen=True)
class Replay:
    """The scenarios sent and the requests that went out, per signal (the client's host
    planted in them, for the planted replay), with the value planted, if any."""

    runs: tuple[Prepared, ...]
    requests: Mapping[Signal, tuple[JSONObject, ...]]
    planted: str | None = None

    def session_ids(self) -> list[str]:
        return [run.session_id for run in self.runs]


@dataclass(frozen=True)
class Entry:
    """One Loki entry: index labels, line and structured metadata."""

    labels: Labels
    line: str
    metadata: Labels


@dataclass(frozen=True)
class Trace:
    """A trace as ``/api/v2/traces/<id>`` returns it: the span IDs, the resource
    attributes (as text) and attribute keys of its spans and span events, and the body."""

    spans: frozenset[str]
    resources: tuple[Labels, ...]
    span_keys: frozenset[str]
    event_keys: frozenset[str]
    body: bytes


# OTLP requests.


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _items(request: JSONObject, signal: Signal) -> Iterator[tuple[JSONObject, JSONObject]]:
    """(resource entry, item) for every metric, log record or span of ``request``."""
    resources, scopes, items = LAYOUT[signal]
    for entry in _objects(request.get(resources)):
        for scope in _objects(entry.get(scopes)):
            for item in _objects(scope.get(items)):
                yield entry, item


def _set(owner: JSONObject, key: str, value: str) -> None:
    """Set the string attribute ``key`` of ``owner``, replacing any value it had."""
    kept: list[JSON] = [
        item for item in _objects(owner.get("attributes")) if item.get("key") != key
    ]
    kept.append({"key": key, "value": {"stringValue": value}})
    owner["attributes"] = kept


def _plant(requests: Mapping[Signal, Sequence[JSONObject]], value: str) -> Counter[str]:
    """Give every resource, data point, log record, span and span event of
    ``requests`` (changed in place) a ``host`` and a ``host.name``; return how many
    owners each level had."""
    planted: Counter[str] = Counter()

    def plant(owner: JSONObject, level: str) -> None:
        _set(owner, HOST_LABEL, value)
        _set(owner, HOST_NAME, f"{value}-name")
        planted[level] += 1

    for signal in SIGNALS:
        for request in requests[signal]:
            for entry in _objects(request.get(LAYOUT[signal][0])):
                resource = entry.get("resource")
                if not isinstance(resource, dict):
                    resource = {}
                    entry["resource"] = resource
                plant(resource, "resource")
            for _, item in _items(request, signal):
                if signal == "metrics":
                    for data in item.values():
                        if isinstance(data, dict):
                            for point in _objects(data.get("dataPoints")):
                                plant(point, "datapoint")
                elif signal == "logs":
                    plant(item, "log")
                else:
                    plant(item, "span")
                    for event in _objects(item.get("events")):
                        plant(event, "spanevent")
    return planted


def _record_count(requests: Sequence[JSONObject]) -> int:
    return sum(1 for request in requests for _ in _items(request, "logs"))


def _spans(requests: Sequence[JSONObject]) -> dict[str, set[str]]:
    """The span IDs of ``requests``, by trace ID (hex, as in OTLP JSON)."""
    traces: dict[str, set[str]] = {}
    for request in requests:
        for _, span in _items(request, "traces"):
            trace_id, span_id = span.get("traceId"), span.get("spanId")
            assert isinstance(trace_id, str), span
            assert isinstance(span_id, str), span
            traces.setdefault(trace_id, set()).add(span_id)
    return traces


def _first_record_ns(requests: Sequence[JSONObject]) -> int:
    """Just before the earliest log record of ``requests``."""
    times = [
        int(str(record.get("timeUnixNano")))
        for request in requests
        for _, record in _items(request, "logs")
    ]
    return min(times, default=time.time_ns()) - NANOS_PER_SECOND


# HTTP.


def _get(
    base: str, path: str, params: Sequence[tuple[str, str | int]] = (), **headers: str
) -> tuple[int, bytes]:
    """The status and body of ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        target = f"{path}?{urlencode(params)}" if params else path
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _document(
    base: str, path: str, params: Sequence[tuple[str, str | int]] = (), **headers: str
) -> JSONObject:
    status, body = _get(base, path, params, **headers)
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    document = json.loads(body)
    assert isinstance(document, dict), f"GET {path}: {document!r}"
    return document


def _data(base: str, path: str, params: Sequence[tuple[str, str | int]], **headers: str) -> JSON:
    """The ``data`` field of a successful Prometheus or Loki answer."""
    document = _document(base, path, params, **headers)
    assert document.get("status") == "success", document
    return document.get("data")


def _strings(value: JSON) -> Labels:
    assert isinstance(value, dict), value
    return {key: str(item) for key, item in value.items()}


def _poll[T](read: Callable[[], T], done: Callable[[T], bool]) -> T:
    """Call ``read`` until ``done`` accepts its result or ``DEADLINE`` has passed;
    return the last result, for the caller to assert on."""
    deadline = time.monotonic() + DEADLINE
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


# Prometheus.


def _series(base: str, session_ids: Sequence[str]) -> list[Labels]:
    """Every series of these sessions, whatever its name; session IDs are UUIDs, with
    nothing to escape in the regular expression."""
    now = time.time()
    matcher = f'{{{JOB_LABEL}="{SERVICE_NAME}",session_id=~"{"|".join(session_ids)}"}}'
    params: list[tuple[str, str | int]] = [
        ("match[]", matcher),
        ("start", f"{now - RANGE_SECONDS:.3f}"),
        ("end", f"{now + MARGIN_SECONDS:.3f}"),
    ]
    found = _data(base, "/api/v1/series", params)
    assert isinstance(found, list), found
    return [_strings(item) for item in found]


def _expected_series(run: Prepared) -> set[tuple[str, frozenset[tuple[str, str]]]]:
    """``run``'s series as Prometheus names them, without ``job`` and ``host``."""
    return {
        (
            PROMETHEUS_NAMES[series.metric],
            frozenset((re.sub(r"[^A-Za-z0-9_]", "_", key), value) for key, value in series.labels),
        )
        for series in run.expected
    }


def _stored_series(series: list[Labels]) -> set[tuple[str, frozenset[tuple[str, str]]]]:
    return {
        (
            labels["__name__"],
            frozenset(
                (key, value)
                for key, value in labels.items()
                if key not in ("__name__", JOB_LABEL, HOST_LABEL)
            ),
        )
        for labels in series
    }


def _wait_for_series(base: str, replay: Replay) -> list[Labels]:
    expected = {run.session_id: len(run.expected) for run in replay.runs}

    def done(found: list[Labels]) -> bool:
        return Counter(labels.get("session_id", "") for labels in found) == expected

    return _poll(lambda: _series(base, replay.session_ids()), done)


# Loki.


def _entries(base: str, session_id: str, start_ns: int) -> list[Entry]:
    logql = f'{{service_name="{SERVICE_NAME}"}} | session_id="{session_id}"'
    params: list[tuple[str, str | int]] = [
        ("query", logql),
        ("start", start_ns),
        ("end", time.time_ns() + MARGIN_SECONDS * NANOS_PER_SECOND),
        ("limit", QUERY_LIMIT),
        ("direction", "forward"),
    ]
    data = _data(base, "/loki/api/v1/query_range", params, **CATEGORIZE)
    assert isinstance(data, dict), data
    assert data.get("resultType") == "streams", data.get("resultType")
    entries: list[Entry] = []
    for stream in _objects(data.get("result")):
        labels = _strings(stream.get("stream"))
        values = stream.get("values")
        assert isinstance(values, list), stream
        for value in values:
            assert isinstance(value, list), value
            categories = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
            metadata = _strings(categories.get("structuredMetadata") or {})
            entries.append(Entry(labels, str(value[1]), metadata))
    assert len(entries) < QUERY_LIMIT, f"{logql} hit the query limit"
    return entries


def _wait_for_entries(base: str, replay: Replay) -> dict[str, list[Entry]]:
    start_ns = _first_record_ns(replay.requests["logs"])
    expected = {run.session_id: _record_count(run.logs) for run in replay.runs}

    def read() -> dict[str, list[Entry]]:
        return {session_id: _entries(base, session_id, start_ns) for session_id in expected}

    def done(found: dict[str, list[Entry]]) -> bool:
        return {session_id: len(entries) for session_id, entries in found.items()} == expected

    return _poll(read, done)


# Tempo.


def _hex(value: JSON) -> str:
    """A trace or span ID as Tempo's v2 API writes it (protobuf JSON: base64), in hex."""
    return base64.b64decode(value).hex() if isinstance(value, str) and value else ""


def _text(value: JSON) -> str:
    """An OTLP JSON AnyValue's string, or its JSON form."""
    if isinstance(value, dict) and isinstance(value.get("stringValue"), str):
        return str(value["stringValue"])
    return json.dumps(value, sort_keys=True)


def _trace(base: str, trace_id: str) -> Trace | None:
    """The trace ``trace_id`` from Tempo, or ``None`` while it is not there."""
    status, body = _get(base, f"/api/v2/traces/{trace_id}")
    if status == 404:
        return None
    assert status == 200, f"GET /api/v2/traces/{trace_id} answered {status}: {body[:300]!r}"
    document = json.loads(body)
    trace = document.get("trace") if isinstance(document, dict) else None
    spans: set[str] = set()
    resources: list[Labels] = []
    span_keys: set[str] = set()
    event_keys: set[str] = set()
    for entry in _objects(trace.get("resourceSpans") if isinstance(trace, dict) else None):
        resource = entry.get("resource")
        attributes = _objects(resource.get("attributes")) if isinstance(resource, dict) else []
        resources.append({str(item.get("key")): _text(item.get("value")) for item in attributes})
        for scope in _objects(entry.get("scopeSpans")):
            for span in _objects(scope.get("spans")):
                spans.add(_hex(span.get("spanId")))
                span_keys |= {str(item.get("key")) for item in _objects(span.get("attributes"))}
                for event in _objects(span.get("events")):
                    event_keys |= {
                        str(item.get("key")) for item in _objects(event.get("attributes"))
                    }
    return Trace(
        frozenset(spans), tuple(resources), frozenset(span_keys), frozenset(event_keys), body
    )


def _wait_for_traces(base: str, replay: Replay) -> dict[str, Trace | None]:
    expected = _spans(replay.requests["traces"])

    def read() -> dict[str, Trace | None]:
        return {trace_id: _trace(base, trace_id) for trace_id in expected}

    def done(found: dict[str, Trace | None]) -> bool:
        return all(
            (trace := found[trace_id]) is not None and trace.spans == spans
            for trace_id, spans in expected.items()
        )

    return _poll(read, done)


def _search(base: str, traceql: str) -> set[str]:
    """The IDs of the traces a TraceQL search finds in the last hour, in hex (Tempo
    leaves out leading zeros)."""
    now = int(time.time())
    params: list[tuple[str, str | int]] = [
        ("q", traceql),
        ("start", now - RANGE_SECONDS),
        ("end", now + MARGIN_SECONDS),
        ("limit", SEARCH_LIMIT),
    ]
    traces = _objects(_document(base, "/api/search", params).get("traces"))
    assert len(traces) < SEARCH_LIMIT, f"{traceql} hit the search limit"
    return {str(trace.get("traceID")).rjust(32, "0") for trace in traces}


# Fixtures.


@pytest.fixture(scope="module")
def clock(prometheus_url: str) -> float:
    """Seconds by which Prometheus's clock is ahead of this machine's; fails at once
    beyond ``MAX_CLOCK_SKEW``, since the replayed samples carry this machine's time."""
    before = time.time()
    data = _data(prometheus_url, "/api/v1/query", [("query", "time()")])
    assert isinstance(data, dict), data
    result = data.get("result")
    assert isinstance(result, list), data
    skew = float(str(result[1])) - (before + time.time()) / 2
    if abs(skew) > MAX_CLOCK_SKEW:
        pytest.fail(
            f"Prometheus's clock (the Docker VM's) is {skew:+.0f} s off this machine's: "
            "restart Docker Desktop (on Windows, run `wsl --shutdown` first) and run the "
            "tests again.",
            pytrace=False,
        )
    return skew


@pytest.fixture(scope="module")
def fresh(otlp_http_url: str, clock: float) -> Replay:
    """Every scenario's metrics, events and spans, as Claude Code sent them."""
    now_ns = time.time_ns()
    runs = tuple(
        prepare(load_scenario(name), now_ns=now_ns) for name in scenario_names(with_otlp=True)
    )
    for run in runs:
        send(otlp_http_url, run)
    requests = {
        signal: tuple(r for run in runs for r in run.requests(signal)) for signal in SIGNALS
    }
    return Replay(runs, requests)


@pytest.fixture(scope="module")
def planted(otlp_http_url: str, clock: float) -> Replay:
    """The smallest scenario with metrics, events, spans and span events, sent with a
    client's own host and host.name planted at every level."""
    now_ns = time.time_ns()
    scenarios = [load_scenario(name) for name in scenario_names(with_otlp=True)]
    candidates = [
        prepare(scenario, now_ns=now_ns)
        for scenario in scenarios
        if scenario.metrics and scenario.logs and scenario.traces
    ]
    with_events = [
        run
        for run in candidates
        if any(item.get("events") for r in run.traces for _, item in _items(r, "traces"))
    ]
    assert with_events, "no fixture scenario has metrics, events, spans and span events"
    run = min(with_events, key=lambda item: sum(len(item.requests(s)) for s in SIGNALS))
    value = f"ccobs-client-host-{secrets.token_hex(8)}"
    requests = {signal: copy.deepcopy(run.requests(signal)) for signal in SIGNALS}
    levels = _plant(requests, value)
    assert set(levels) == set(LEVELS), levels
    for signal in SIGNALS:
        for request in requests[signal]:
            post_otlp(otlp_http_url, signal, request)
    return Replay((run,), requests, value)


@pytest.fixture(scope="module")
def fresh_series(prometheus_url: str, fresh: Replay) -> list[Labels]:
    return _wait_for_series(prometheus_url, fresh)


@pytest.fixture(scope="module")
def planted_series(prometheus_url: str, planted: Replay) -> list[Labels]:
    return _wait_for_series(prometheus_url, planted)


# A replay as Claude Code sends it.


def test_every_metric_series_of_a_replay_has_the_host_label(
    fresh: Replay, fresh_series: list[Labels]
) -> None:
    sessions = Counter(labels.get("session_id", "") for labels in fresh_series)
    assert sessions == {run.session_id: len(run.expected) for run in fresh.runs}
    for labels in fresh_series:
        assert labels["__name__"].startswith("claude_code_"), labels
        assert labels.get(HOST_LABEL) == DEFAULT_HOST, labels
        assert "host_name" not in labels, labels


def test_loki_streams_of_a_replay_carry_the_host_label(loki_url: str, fresh: Replay) -> None:
    stored = _wait_for_entries(loki_url, fresh)
    for run in fresh.runs:
        entries = stored[run.session_id]
        assert len(entries) == _record_count(run.logs), run.scenario
        for entry in entries:
            assert entry.labels == {"service_name": SERVICE_NAME, HOST_LABEL: DEFAULT_HOST}
            assert not {HOST_LABEL, "host_name"} & set(entry.metadata), entry.metadata


def test_tempo_finds_the_traces_of_a_replay_by_the_host_label(
    tempo_url: str, fresh: Replay
) -> None:
    stored = _wait_for_traces(tempo_url, fresh)
    for trace_id, trace in stored.items():
        assert trace is not None, f"trace {trace_id} is not in Tempo"
        assert trace.resources, trace_id
        assert all(resource.get(HOST_LABEL) == DEFAULT_HOST for resource in trace.resources)
        assert not [resource for resource in trace.resources if HOST_NAME in resource]
    for run in fresh.runs:
        expected = set(_spans(run.traces))
        query = (
            f'{{ resource.{HOST_LABEL} = "{DEFAULT_HOST}" && '
            f'span."session.id" = "{run.session_id}" }}'
        )
        found = _poll(partial(_search, tempo_url, query), partial(operator.eq, expected))
        assert found == expected, run.scenario


# A replay whose client sent its own host.


def test_prometheus_keeps_only_the_collectors_host(
    planted: Replay, planted_series: list[Labels]
) -> None:
    # Deleting the data points' host neither splits nor merges series: the stored ones
    # are those the replayer expects, each with the collector's host.
    (run,) = planted.runs
    assert planted.planted
    assert _stored_series(planted_series) == _expected_series(run)
    for labels in planted_series:
        assert labels.get(HOST_LABEL) == DEFAULT_HOST, labels
        assert "host_name" not in labels, labels
        assert not [value for value in labels.values() if planted.planted in value], labels


def test_loki_keeps_only_the_collectors_host(loki_url: str, planted: Replay) -> None:
    (run,) = planted.runs
    assert planted.planted
    entries = _wait_for_entries(loki_url, planted)[run.session_id]
    assert len(entries) == _record_count(run.logs), run.scenario
    for entry in entries:
        assert entry.labels == {"service_name": SERVICE_NAME, HOST_LABEL: DEFAULT_HOST}
        assert not {HOST_LABEL, "host_name"} & set(entry.metadata), entry.metadata
        assert planted.planted not in json.dumps([entry.labels, entry.metadata, entry.line])


def test_tempo_keeps_only_the_collectors_host(tempo_url: str, planted: Replay) -> None:
    assert planted.planted
    stored = _wait_for_traces(tempo_url, planted)
    for trace_id, trace in stored.items():
        assert trace is not None, f"trace {trace_id} is not in Tempo"
        assert all(resource.get(HOST_LABEL) == DEFAULT_HOST for resource in trace.resources)
        assert not [resource for resource in trace.resources if HOST_NAME in resource]
        assert not {HOST_LABEL, HOST_NAME} & (trace.span_keys | trace.event_keys), trace_id
        assert planted.planted.encode() not in trace.body, trace_id
    # Searchable by the collector's host first, so that the searches for the client's
    # value below run once Tempo's search sees the traces.
    (run,) = planted.runs
    expected = set(stored)
    query = (
        f'{{ resource.{HOST_LABEL} = "{DEFAULT_HOST}" && span."session.id" = "{run.session_id}" }}'
    )
    found = _poll(partial(_search, tempo_url, query), partial(operator.eq, expected))
    assert found == expected, run.scenario
    for scope in ("resource", "span"):
        query = f'{{ {scope}.{HOST_LABEL} = "{planted.planted}" }}'
        assert _search(tempo_url, query) == set(), query
