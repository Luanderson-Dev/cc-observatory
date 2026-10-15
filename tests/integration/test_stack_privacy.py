"""Identity and content never reach Prometheus, Loki or Tempo (ADR 0006, ADR 0021).

Needs Docker: ``uv run pytest -m integration --no-cov``. Every fixture scenario with OTLP
data is replayed through the collector with fresh session, trace and span IDs and with
the canaries of ``tools/stack/replay.py``: identity and ``workspace.host_paths`` on
every resource, data point, log record, span and span event; the other content
attributes on log records and spans; the three content events as extra log records; and
a ``tool.output`` event on every span.

The run is first read back in full (every expected series, every record but the
content events, every span), so that the searches cannot pass on missing data. Then the
raw answers of each backend are searched: no planted value and no canary prefix, no
identity or content key as a label, structured metadata key or attribute key, no
content event and no ``tool.output`` event. The metadata the run sent must survive, so
a filter that deletes too much fails too. Queries by session or trace ID keep the run
apart from the other modules, which share the stack; the searches over the whole window
(label and tag values, LogQL and TraceQL filters) also cover their data, since no module
may store a canary or an identity or content key either.
"""

from __future__ import annotations

import base64
import http.client
import json
import operator
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final
from urllib.parse import quote, urlencode, urlsplit

import pytest

from ccobs.domain.telemetry import (
    CONTENT_EVENTS,
    CONTENT_SPAN_EVENT,
    DATA_POINT_CONTENT_KEYS,
    DEFAULT_HOST,
    EVENT_BODY_PREFIX,
    HOST_LABEL,
    IDENTITY_KEYS,
    JOB_LABEL,
    LOG_CONTENT_KEYS,
    LOKI_INDEX_LABELS,
    METRICS,
    RESOURCE_CONTENT_KEYS,
    SERVICE_NAME,
    SPAN_CONTENT_KEYS,
    SPAN_EVENT_CONTENT_KEYS,
    TARGET_INFO,
    TARGET_INFO_LABELS,
    is_identity_key,
)
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    CANARY_PREFIX,
    CONTENT_SPAN_EVENT_CANARY_KEYS,
    IDENTITY_CANARY_KEYS,
    RESOURCE_CANARY_KEYS,
    SIGNALS,
    SPAN_CANARY_KEYS,
    SPAN_EVENT_CANARY_KEYS,
    Prepared,
    Signal,
    load_scenario,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

# Export request layout per signal: resource list, scope list, item list.
LAYOUT: Final[Mapping[Signal, tuple[str, str, str]]] = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}
# What the collector deletes as content at each level while the opt-in is off (ADR
# 0021); it also drops tool.output span events whole.
CONTENT_BY_LEVEL: Final[Mapping[str, frozenset[str]]] = {
    "resource": RESOURCE_CONTENT_KEYS,
    "datapoint": DATA_POINT_CONTENT_KEYS,
    "log": LOG_CONTENT_KEYS,
    "span": SPAN_CONTENT_KEYS,
    "spanevent": SPAN_EVENT_CONTENT_KEYS,
}
# Every content key of any level: none may be stored at any level.
CONTENT_KEYS: Final = frozenset().union(*CONTENT_BY_LEVEL.values())
# The keys planted at each TraceQL scope, searched for over the whole window.
TRACEQL_PLANTED: Final[Mapping[str, tuple[str, ...]]] = {
    "resource": RESOURCE_CANARY_KEYS,
    "span": SPAN_CANARY_KEYS,
    "event": (*SPAN_EVENT_CANARY_KEYS, *CONTENT_SPAN_EVENT_CANARY_KEYS),
}
# Prometheus names, by OpenTelemetry name (ADR 0014).
PROMETHEUS_NAMES: Final = {metric.otel_name: metric.name for metric in METRICS}
# The resource attribute that becomes Prometheus's job label and Loki's service_name.
SERVICE_NAME_KEY: Final = "service.name"
# Seconds to wait until the whole run can be read, and between two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
HTTP_TIMEOUT: Final = 10.0
# Windows start this long before the run's earliest timestamp and end this long from now.
MARGIN_SECONDS: Final = 60
NANOS_PER_SECOND: Final = 1_000_000_000
# Beyond this skew between Prometheus's clock and this machine's, the replayed samples
# land outside the out-of-order window or the query ranges.
MAX_CLOCK_SKEW: Final = 120.0
QUERY_LIMIT: Final = 5000
SEARCH_LIMIT: Final = 100
# Separates index labels ("stream") from structured metadata in Loki's answers.
CATEGORIZE: Final = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}
# A label or structured metadata key that LogQL takes unquoted.
LABEL_NAME: Final = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# Characters of a raw answer shown around a leak.
EXCERPT: Final = 160

type Labels = dict[str, str]
type Params = Sequence[tuple[str, str | int]]
type SeriesKey = tuple[str, frozenset[tuple[str, str]]]


@dataclass(frozen=True)
class Replay:
    """The scenarios sent, and the earliest timestamp they carry."""

    runs: tuple[Prepared, ...]
    start_ns: int

    def markers(self) -> frozenset[str]:
        return frozenset(run.canary_marker for run in self.runs if run.canary_marker)

    def session_ids(self) -> list[str]:
        return [run.session_id for run in self.runs]


@dataclass(frozen=True)
class Answer[T]:
    """What a backend returned: the raw body, and what the test parsed from it."""

    body: bytes
    parsed: T


@dataclass(frozen=True)
class Entry:
    """One Loki entry: index labels, line and structured metadata."""

    labels: Labels
    line: str
    metadata: Labels


@dataclass(frozen=True)
class Trace:
    """A trace as ``/api/v2/traces/<id>`` returns it: how many span entries came back,
    the resource attributes (as text), the attribute keys by TraceQL scope (resource,
    span and event), the span event names and the raw body."""

    entries: int
    resources: tuple[Labels, ...]
    keys: Mapping[str, frozenset[str]]
    event_names: frozenset[str]
    body: bytes


# The requests sent.


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _attributes(owner: JSONObject) -> Labels:
    """The attributes of ``owner`` as text: a string value as it is, any other as JSON."""
    found: Labels = {}
    for attribute in _objects(owner.get("attributes")):
        key, value = attribute.get("key"), attribute.get("value")
        if isinstance(key, str):
            text = value.get("stringValue") if isinstance(value, dict) else None
            found[key] = text if isinstance(text, str) else json.dumps(value, sort_keys=True)
    return found


def _owners(request: JSONObject, signal: Signal) -> Iterator[tuple[str, JSONObject]]:
    """(level, owner) for every resource, data point, log record, span and span event of
    an export request."""
    resources, scopes, items = LAYOUT[signal]
    for entry in _objects(request.get(resources)):
        resource = entry.get("resource")
        if isinstance(resource, dict):
            yield "resource", resource
        for scope in _objects(entry.get(scopes)):
            for item in _objects(scope.get(items)):
                if signal == "metrics":
                    for data in item.values():
                        if isinstance(data, dict):
                            for point in _objects(data.get("dataPoints")):
                                yield "datapoint", point
                elif signal == "logs":
                    yield "log", item
                else:
                    yield "span", item
                    for event in _objects(item.get("events")):
                        yield "spanevent", event


def _items(run: Prepared, signal: Signal) -> Iterator[tuple[JSONObject, JSONObject]]:
    """(resource, item) for every metric, log record or span ``run`` sent."""
    resources, scopes, items = LAYOUT[signal]
    for request in run.requests(signal):
        for entry in _objects(request.get(resources)):
            resource = entry.get("resource")
            for scope in _objects(entry.get(scopes)):
                for item in _objects(scope.get(items)):
                    yield (resource if isinstance(resource, dict) else {}), item


def _is_content_event(record: JSONObject) -> bool:
    return _attributes(record).get("event.name") in CONTENT_EVENTS


def _text(value: JSON) -> str:
    return value if isinstance(value, str) else ""


def _traces(run: Prepared) -> dict[str, list[tuple[JSONObject, JSONObject]]]:
    """``run``'s (resource, span) pairs by trace ID (hex, as in OTLP JSON)."""
    traces: dict[str, list[tuple[JSONObject, JSONObject]]] = {}
    for resource, span in _items(run, "traces"):
        traces.setdefault(_text(span.get("traceId")), []).append((resource, span))
    return traces


def _kept_events(pairs: Iterable[tuple[JSONObject, JSONObject]]) -> list[JSONObject]:
    """The span events of the (resource, span) ``pairs`` that the collector keeps: all
    but tool.output."""
    return [
        event
        for _, span in pairs
        for event in _objects(span.get("events"))
        if event.get("name") != CONTENT_SPAN_EVENT
    ]


def _earliest_ns(runs: Iterable[Prepared]) -> int:
    """The earliest non-zero ``*UnixNano`` of ``runs``, start times included."""

    def walk(node: JSON) -> Iterator[int]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("UnixNano") and isinstance(value, str | int) and int(value):
                    yield int(value)
                else:
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return min(
        nanos
        for run in runs
        for signal in SIGNALS
        for request in run.requests(signal)
        for nanos in walk(request)
    )


def _label(key: str) -> str:
    """The Prometheus label, or Loki structured metadata key, of an OTel attribute."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def _kept(keys: Iterable[str], level: str) -> set[str]:
    """The keys of ``level`` that the collector keeps: not identity, not content at that
    level, and not ``host``, which it keeps on resources only (ADR 0022)."""
    return {
        key
        for key in keys
        if not is_identity_key(key)
        and key not in CONTENT_BY_LEVEL[level]
        and (level == "resource" or key != HOST_LABEL)
    }


def _kept_trace_keys(pairs: Sequence[tuple[JSONObject, JSONObject]]) -> dict[str, set[str]]:
    """The attribute keys that the (resource, span) ``pairs`` sent and the collector
    keeps, by TraceQL scope."""
    events = _kept_events(pairs)
    return {
        "resource": _kept((key for owner, _ in pairs for key in _attributes(owner)), "resource"),
        "span": _kept((key for _, span in pairs for key in _attributes(span)), "span"),
        "event": _kept((key for event in events for key in _attributes(event)), "spanevent"),
    }


def _forbidden_names(replay: Replay) -> frozenset[str]:
    """Identity and content keys as Prometheus labels and Loki structured metadata keys.
    Dots become underscores there, so the identity prefix rule no longer applies
    (``user.email`` and a ``user_email`` key look alike): the names are listed instead,
    the domain's identity and content keys, every identity canary and every identity key
    the run sent. The canary searches catch a value under any other name."""
    sent = {
        key
        for run in replay.runs
        for signal in SIGNALS
        for request in run.requests(signal)
        for _, owner in _owners(request, signal)
        for key in _attributes(owner)
    }
    keys = IDENTITY_KEYS | set(IDENTITY_CANARY_KEYS) | CONTENT_KEYS
    keys |= {key for key in sent if is_identity_key(key)}
    return frozenset(_label(key) for key in keys)


def _is_forbidden_key(key: str) -> bool:
    """Whether an OTel attribute key (as Tempo stores it) is identity or content."""
    return is_identity_key(key) or key in CONTENT_KEYS or key in CONTENT_SPAN_EVENT_CANARY_KEYS


# Canaries in raw answers.


def _excerpt(body: bytes, needle: bytes) -> str:
    at = body.find(needle)
    return body[max(at - EXCERPT, 0) : at + EXCERPT].decode("utf-8", errors="replace")


def _assert_no_canary(body: bytes, replay: Replay, where: str) -> None:
    """``body`` holds no marker of the run and no canary prefix, which every planted
    value of any run starts with."""
    found = sorted(marker for marker in replay.markers() if marker.encode() in body)
    assert not found, (
        f"{where} holds the run's canaries {found}: {_excerpt(body, found[0].encode())}"
    )
    prefix = CANARY_PREFIX.encode()
    assert prefix not in body, f"{where} holds a canary: {_excerpt(body, prefix)}"


# HTTP.


def _get(base: str, path: str, params: Params = (), **headers: str) -> tuple[int, bytes]:
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


def _body(base: str, path: str, params: Params = (), **headers: str) -> bytes:
    """The body of a 200 answer."""
    status, body = _get(base, path, params, **headers)
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    return body


def _document(body: bytes, where: str) -> JSONObject:
    document = json.loads(body)
    assert isinstance(document, dict), f"{where}: {document!r}"
    return document


def _data(body: bytes, where: str) -> JSON:
    """The ``data`` field of a successful Prometheus or Loki answer (``None`` when Loki
    has nothing to list)."""
    document = _document(body, where)
    assert document.get("status") == "success", document
    return document.get("data")


def _strings(value: JSON, where: str) -> Labels:
    assert isinstance(value, dict), f"{where}: {value!r}"
    return {key: str(item) for key, item in value.items()}


def _names(value: JSON, where: str) -> list[str]:
    """A list of names (``[]`` for Loki's missing ``data``)."""
    if value is None:
        return []
    assert isinstance(value, list), f"{where}: {value!r}"
    return [str(item) for item in value]


def _poll[T](read: Callable[[], T], done: Callable[[T], bool]) -> T:
    """Call ``read`` until ``done`` accepts its result or ``DEADLINE`` has passed;
    return the last result, for the caller to assert on."""
    deadline = time.monotonic() + DEADLINE
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


def _seconds(replay: Replay) -> list[tuple[str, str | int]]:
    """From before the run's earliest timestamp to after now, in seconds."""
    start = replay.start_ns / NANOS_PER_SECOND - MARGIN_SECONDS
    return [("start", f"{start:.3f}"), ("end", f"{time.time() + MARGIN_SECONDS:.3f}")]


def _whole_seconds(replay: Replay) -> list[tuple[str, str | int]]:
    """The same window in whole seconds, as Tempo takes it."""
    start = replay.start_ns // NANOS_PER_SECOND - MARGIN_SECONDS
    return [("start", start), ("end", int(time.time()) + MARGIN_SECONDS)]


def _nanos(replay: Replay) -> list[tuple[str, str | int]]:
    """The same window in nanoseconds, as Loki takes it."""
    start = replay.start_ns - MARGIN_SECONDS * NANOS_PER_SECOND
    return [("start", start), ("end", time.time_ns() + MARGIN_SECONDS * NANOS_PER_SECOND)]


# Prometheus.


def _sessions_matcher(replay: Replay) -> str:
    """Every Claude Code series of the run's sessions; session IDs are UUIDs, with nothing
    to escape in the regular expression."""
    sessions = "|".join(replay.session_ids())
    return f'{{__name__=~"claude_code_.*",{JOB_LABEL}="{SERVICE_NAME}",session_id=~"{sessions}"}}'


def _series(base: str, replay: Replay, *matchers: str) -> Answer[list[Labels]]:
    params = [("match[]", matcher) for matcher in matchers] + _seconds(replay)
    body = _body(base, "/api/v1/series", params)
    found = _data(body, "/api/v1/series")
    assert isinstance(found, list), found
    return Answer(body, [_strings(item, "series") for item in found])


def _expected_series(replay: Replay) -> set[SeriesKey]:
    """The run's series as Prometheus names them, without ``job`` and ``host``."""
    return {
        (
            PROMETHEUS_NAMES[series.metric],
            frozenset((_label(key), value) for key, value in series.labels),
        )
        for run in replay.runs
        for series in run.expected
    }


def _stored_series(found: Iterable[Labels]) -> set[SeriesKey]:
    return {
        (
            labels["__name__"],
            frozenset(
                (key, value)
                for key, value in labels.items()
                if key not in ("__name__", JOB_LABEL, HOST_LABEL)
            ),
        )
        for labels in found
    }


# Loki.


def _entries(
    base: str, logql: str, replay: Replay, limit: int = QUERY_LIMIT
) -> Answer[list[Entry]]:
    """Every entry ``logql`` selects in the run's window, with the raw answer."""
    params = [("query", logql), *_nanos(replay), ("limit", limit), ("direction", "forward")]
    body = _body(base, "/loki/api/v1/query_range", params, **CATEGORIZE)
    data = _data(body, logql)
    assert isinstance(data, dict), data
    assert data.get("resultType") == "streams", data.get("resultType")
    entries: list[Entry] = []
    for stream in _objects(data.get("result")):
        labels = _strings(stream.get("stream"), "stream labels")
        values = stream.get("values")
        assert isinstance(values, list), stream
        for value in values:
            assert isinstance(value, list), value
            categories = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
            metadata = _strings(categories.get("structuredMetadata") or {}, "metadata")
            entries.append(Entry(labels, str(value[1]), metadata))
    if limit == QUERY_LIMIT:
        assert len(entries) < QUERY_LIMIT, f"{logql} hit the query limit"
    return Answer(body, entries)


def _session_query(session_id: str) -> str:
    return f'{{service_name="{SERVICE_NAME}"}} | session_id="{session_id}"'


def _stored_count(run: Prepared) -> int:
    """How many of ``run``'s records Loki must hold: all but the content events."""
    return sum(1 for _, record in _items(run, "logs") if not _is_content_event(record))


# Tempo.


def _hex(value: JSON) -> str:
    """A trace or span ID as Tempo's v2 API writes it (protobuf JSON: base64), in hex."""
    return base64.b64decode(value).hex() if isinstance(value, str) and value else ""


def _trace(base: str, trace_id: str) -> Trace | None:
    """The trace ``trace_id`` from Tempo, or ``None`` while it is not there."""
    status, body = _get(base, f"/api/v2/traces/{trace_id}")
    if status == 404:
        return None
    assert status == 200, f"GET /api/v2/traces/{trace_id} answered {status}: {body[:300]!r}"
    trace = _document(body, trace_id).get("trace")
    entries = 0
    resources: list[Labels] = []
    keys: dict[str, set[str]] = {"resource": set(), "span": set(), "event": set()}
    event_names: set[str] = set()
    for entry in _objects(trace.get("resourceSpans") if isinstance(trace, dict) else None):
        resource = entry.get("resource")
        resources.append(_attributes(resource if isinstance(resource, dict) else {}))
        keys["resource"] |= set(resources[-1])
        for scope in _objects(entry.get("scopeSpans")):
            for span in _objects(scope.get("spans")):
                assert _hex(span.get("traceId")) == trace_id, span.get("traceId")
                entries += 1
                keys["span"] |= set(_attributes(span))
                for event in _objects(span.get("events")):
                    event_names.add(_text(event.get("name")))
                    keys["event"] |= set(_attributes(event))
    stored = {scope: frozenset(found) for scope, found in keys.items()}
    return Trace(entries, tuple(resources), stored, frozenset(event_names), body)


def _search(base: str, traceql: str, replay: Replay) -> set[str]:
    """The IDs of the traces a TraceQL search finds in the run's window, in hex (Tempo
    leaves out leading zeros)."""
    params = [("q", traceql), *_whole_seconds(replay), ("limit", SEARCH_LIMIT)]
    traces = _objects(_document(_body(base, "/api/search", params), traceql).get("traces"))
    assert len(traces) < SEARCH_LIMIT, f"{traceql} hit the search limit"
    return {_text(trace.get("traceID")).rjust(32, "0") for trace in traces}


def _tags(base: str, scope: str, replay: Replay) -> list[str]:
    """The attribute names Tempo holds in ``scope`` over the run's window."""
    params = [("scope", scope), *_whole_seconds(replay)]
    scopes = _objects(_document(_body(base, "/api/v2/search/tags", params), scope).get("scopes"))
    return [
        str(tag)
        for item in scopes
        if item.get("name") == scope
        for tag in _names(item.get("tags"), scope)
    ]


# Fixtures.


@pytest.fixture(scope="module")
def clock(prometheus_url: str) -> float:
    """Seconds by which Prometheus's clock is ahead of this machine's; fails at once
    beyond ``MAX_CLOCK_SKEW``, since the replayed samples carry this machine's time."""
    before = time.time()
    data = _data(_body(prometheus_url, "/api/v1/query", [("query", "time()")]), "time()")
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
def replay(otlp_http_url: str, clock: float) -> Replay:
    """Every scenario with OTLP data, with the canaries planted, sent to the collector."""
    now_ns = time.time_ns()
    runs = tuple(
        prepare(load_scenario(name), now_ns=now_ns, canaries=True)
        for name in scenario_names(with_otlp=True)
    )
    assert runs, "no fixture scenario has OTLP data"
    for run in runs:
        # Raises ReplayError if the collector refuses a request, as the privacy
        # processors do when a statement fails (error_mode: propagate).
        send(otlp_http_url, run)
    return Replay(runs, _earliest_ns(runs))


@pytest.fixture(scope="module")
def series(prometheus_url: str, replay: Replay) -> Answer[list[Labels]]:
    """The run's metric series in Prometheus, once they are exactly the expected ones or
    the deadline has passed (the tests then report the difference)."""
    expected = _expected_series(replay)
    return _poll(
        lambda: _series(prometheus_url, replay, _sessions_matcher(replay)),
        lambda found: _stored_series(found.parsed) == expected,
    )


@pytest.fixture(scope="module")
def entries(loki_url: str, replay: Replay) -> dict[str, Answer[list[Entry]]]:
    """Each session's entries in Loki, once every record but the content events is there
    or the deadline has passed."""
    expected = {run.session_id: _stored_count(run) for run in replay.runs}

    def read() -> dict[str, Answer[list[Entry]]]:
        return {
            session_id: _entries(loki_url, _session_query(session_id), replay)
            for session_id in expected
        }

    def done(found: dict[str, Answer[list[Entry]]]) -> bool:
        return {session_id: len(answer.parsed) for session_id, answer in found.items()} == expected

    return _poll(read, done)


@pytest.fixture(scope="module")
def traces(tempo_url: str, replay: Replay) -> dict[str, Trace | None]:
    """Each trace of the run from Tempo, once every one holds all its spans or the
    deadline has passed; complete traces are not read again."""
    expected = {
        trace_id: len(spans) for run in replay.runs for trace_id, spans in _traces(run).items()
    }
    found: dict[str, Trace | None] = {}

    def read() -> dict[str, Trace | None]:
        for trace_id, count in expected.items():
            trace = found.get(trace_id)
            if trace is None or trace.entries != count:
                found[trace_id] = _trace(tempo_url, trace_id)
        return found

    def done(result: dict[str, Trace | None]) -> bool:
        return all(
            (trace := result.get(trace_id)) is not None and trace.entries == count
            for trace_id, count in expected.items()
        )

    return _poll(read, done)


# What the run planted.


def test_the_run_plants_identity_and_content_at_every_level(replay: Replay) -> None:
    # Otherwise the searches below could pass because a level carried nothing.
    planted: Counter[str] = Counter()
    for run in replay.runs:
        marker = run.canary_marker
        assert marker is not None, run.scenario
        for signal in SIGNALS:
            for request in run.requests(signal):
                for level, owner in _owners(request, signal):
                    values = _attributes(owner)
                    keys = {key for key, value in values.items() if value.startswith(marker)}
                    if not keys:
                        continue
                    if level == "log" and _is_content_event(owner):
                        planted["content event"] += 1
                    elif level == "spanevent" and owner.get("name") == CONTENT_SPAN_EVENT:
                        planted[CONTENT_SPAN_EVENT] += 1
                    else:
                        # Identity, and the content the collector deletes at this level.
                        planted[f"{signal} {level}"] += 1
                        missing = {*IDENTITY_CANARY_KEYS, *CONTENT_BY_LEVEL[level]} - keys
                        assert not missing, (run.scenario, signal, level, sorted(missing))
    assert set(planted) == {
        "metrics resource",
        "metrics datapoint",
        "logs resource",
        "logs log",
        "content event",
        "traces resource",
        "traces span",
        "traces spanevent",
        CONTENT_SPAN_EVENT,
    }, planted
    with_logs = sum(1 for run in replay.runs if run.logs)
    assert planted["content event"] == with_logs * len(CONTENT_EVENTS)
    assert planted[CONTENT_SPAN_EVENT] == planted["traces span"]


# Prometheus.


def test_prometheus_holds_the_run_without_identity_or_content(
    prometheus_url: str, replay: Replay, series: Answer[list[Labels]]
) -> None:
    # The planted keys neither leave a label nor split a series: the stored series are
    # exactly the replayer's, with every label of the metadata the run sent.
    assert _stored_series(series.parsed) == _expected_series(replay)
    forbidden = _forbidden_names(replay)
    target_info = _series(prometheus_url, replay, f'{TARGET_INFO}{{{JOB_LABEL}="{SERVICE_NAME}"}}')
    assert target_info.parsed, "no target_info series for Claude Code"
    for answer, what in ((series, "run series"), (target_info, TARGET_INFO)):
        _assert_no_canary(answer.body, replay, f"Prometheus /api/v1/series ({what})")
        for labels in answer.parsed:
            assert labels.get(HOST_LABEL) == DEFAULT_HOST, labels
            assert not set(labels) & forbidden, sorted(set(labels) & forbidden)
    for labels in target_info.parsed:
        # The resource attributes other than service.name, which is the job: no other.
        assert set(labels) - {"__name__"} <= TARGET_INFO_LABELS, labels
        assert {"service_version", "os_type", HOST_LABEL} <= set(labels), labels


def test_prometheus_label_names_and_values_hold_no_canary(
    prometheus_url: str, replay: Replay, series: Answer[list[Labels]]
) -> None:
    assert series.parsed
    forbidden = _forbidden_names(replay)
    # Every label of Claude Code's series in the window, target_info's included.
    params = [("match[]", f'{{{JOB_LABEL}="{SERVICE_NAME}"}}'), *_seconds(replay)]
    names = set(_names(_data(_body(prometheus_url, "/api/v1/labels", params), "labels"), ""))
    assert not names & forbidden, sorted(names & forbidden)
    assert {"session_id", JOB_LABEL, HOST_LABEL} <= names, sorted(names)
    # Every value of every label Prometheus holds in the window, its own and the
    # collector's metrics included.
    every = _names(_data(_body(prometheus_url, "/api/v1/labels", _seconds(replay)), "labels"), "")
    assert "session_id" in every
    for name in every:
        path = f"/api/v1/label/{quote(name, safe='')}/values"
        body = _body(prometheus_url, path, _seconds(replay))
        _assert_no_canary(body, replay, f"Prometheus {path}")
        if name == "session_id":
            values = set(_names(_data(body, path), path))
            assert set(replay.session_ids()) <= values


# Loki.


def test_loki_holds_the_run_without_identity_or_content(
    replay: Replay, entries: dict[str, Answer[list[Entry]]]
) -> None:
    forbidden = _forbidden_names(replay)
    for run in replay.runs:
        answer = entries[run.session_id]
        assert len(answer.parsed) == _stored_count(run), run.scenario
        _assert_no_canary(answer.body, replay, f"Loki entries of {run.scenario}")
        stored: set[str] = set()
        for entry in answer.parsed:
            assert entry.labels == {"service_name": SERVICE_NAME, HOST_LABEL: DEFAULT_HOST}
            assert not set(entry.metadata) & forbidden, sorted(set(entry.metadata) & forbidden)
            assert entry.metadata.get("session_id") == run.session_id
            event = entry.metadata.get("event_name")
            assert event not in CONTENT_EVENTS, event
            assert entry.line == f"{EVENT_BODY_PREFIX}{event}"
            stored |= set(entry.metadata)
        # Every key of the metadata the run sent survives: event attributes such as
        # prompt_length and tool_name, and the resource attributes but service.name.
        expected: set[str] = set()
        for resource, record in _items(run, "logs"):
            if not _is_content_event(record):
                expected |= {_label(key) for key in _kept(_attributes(record), "log")}
                resource_keys = _kept(_attributes(resource), "resource")
                expected |= {_label(key) for key in resource_keys - {SERVICE_NAME_KEY, HOST_LABEL}}
        assert expected <= stored, f"{run.scenario} lost {sorted(expected - stored)}"
        assert {"session_id", "event_name", "event_sequence"} <= stored, run.scenario


def test_loki_finds_no_canary_identity_or_content_in_the_window(
    loki_url: str, replay: Replay, entries: dict[str, Answer[list[Entry]]]
) -> None:
    assert all(answer.parsed for answer in entries.values())
    stream = f'{{service_name="{SERVICE_NAME}"}}'
    # Positive controls: a line filter and a structured metadata filter that select.
    assert _entries(loki_url, f'{stream} |= "{EVENT_BODY_PREFIX}"', replay, limit=1).parsed
    assert _entries(loki_url, f'{stream} | session_id != ""', replay, limit=1).parsed
    # No line holds a canary, and no content event is stored, by line or by event name.
    assert _entries(loki_url, f'{stream} |= "{CANARY_PREFIX}"', replay).parsed == []
    events = "|".join(sorted(CONTENT_EVENTS))
    lines = f"{stream} |~ `^{re.escape(EVENT_BODY_PREFIX)}({events})$`"
    assert _entries(loki_url, lines, replay).parsed == []
    assert _entries(loki_url, f'{stream} | event_name=~"{events}"', replay).parsed == []
    # No entry of any session has an identity or content key: a missing key is empty.
    for name in sorted(_forbidden_names(replay)):
        assert LABEL_NAME.fullmatch(name), name
        found = _entries(loki_url, f'{stream} | {name} != ""', replay).parsed
        assert found == [], f"Loki holds {name} on {len(found)} entries: {found[0].metadata}"
    # Index labels: only service_name and the host label, with no canary in any value.
    labels = _names(_data(_body(loki_url, "/loki/api/v1/labels", _nanos(replay)), "labels"), "")
    assert set(labels) == LOKI_INDEX_LABELS, labels
    for name in labels:
        path = f"/loki/api/v1/label/{quote(name, safe='')}/values"
        body = _body(loki_url, path, _nanos(replay))
        _assert_no_canary(body, replay, f"Loki {path}")
        if name == HOST_LABEL:
            assert _names(_data(body, path), path) == [DEFAULT_HOST]


# Tempo.


def test_tempo_holds_the_run_without_identity_or_content(
    replay: Replay, traces: dict[str, Trace | None]
) -> None:
    for run in replay.runs:
        for trace_id, sent in _traces(run).items():
            trace = traces.get(trace_id)
            assert trace is not None, f"{run.scenario}: trace {trace_id} is not in Tempo"
            assert trace.entries == len(sent), f"{run.scenario} {trace_id}: {trace.entries}"
            where = f"Tempo trace {trace_id} ({run.scenario})"
            _assert_no_canary(trace.body, replay, where)
            for scope, keys in trace.keys.items():
                leaked = sorted(key for key in keys if _is_forbidden_key(key))
                assert not leaked, f"{where} keeps {leaked} as {scope} attributes"
            assert CONTENT_SPAN_EVENT not in trace.event_names, where
            assert all(resource.get(HOST_LABEL) == DEFAULT_HOST for resource in trace.resources)
            # Every key of the metadata the trace sent survives, at each scope, and every
            # span event but tool.output.
            for scope, kept in _kept_trace_keys(sent).items():
                lost = sorted(kept - trace.keys[scope])
                assert not lost, f"{where} lost the {scope} attributes {lost}"
            names = {_text(event.get("name")) for event in _kept_events(sent)}
            assert trace.event_names == names, where


def test_tempo_search_finds_no_canary_identity_or_content(
    tempo_url: str, replay: Replay, traces: dict[str, Trace | None]
) -> None:
    assert traces
    assert all(trace is not None for trace in traces.values())
    # Searchable by session first, so that the searches that must find nothing run once
    # Tempo's search sees the run.
    for run in replay.runs:
        expected = set(_traces(run))
        query = f'{{ span."session.id" = "{run.session_id}" }}'
        found = _poll(partial(_search, tempo_url, query, replay), partial(operator.eq, expected))
        assert found == expected, run.scenario
    # A span event attribute the run sent is searchable too: a positive control for the
    # event scope.
    pairs = [pair for run in replay.runs for pair in _items(run, "traces")]
    event_key = min(_kept_trace_keys(pairs)["event"])
    assert _search(tempo_url, f'{{ event."{event_key}" != nil }}', replay)
    # No planted key exists at its scope, with a canary or with any other value, and no
    # tool.output event is stored, in any trace of the window. The prefix goes into the
    # regular expression as it is: TraceQL refuses the escape re.escape gives "-".
    assert re.fullmatch(r"[a-z-]+", CANARY_PREFIX), CANARY_PREFIX
    for scope, keys in TRACEQL_PLANTED.items():
        for key in keys:
            query = f'{{ {scope}."{key}" != nil }}'
            assert _search(tempo_url, query, replay) == set(), query
        query = f'{{ {scope}."user.email" =~ "{CANARY_PREFIX}.*" }}'
        assert _search(tempo_url, query, replay) == set(), query
    query = f'{{ event:name = "{CONTENT_SPAN_EVENT}" }}'
    assert _search(tempo_url, query, replay) == set(), query


def test_tempo_tag_names_and_values_hold_no_canary_identity_or_content(
    tempo_url: str, replay: Replay, traces: dict[str, Trace | None]
) -> None:
    assert all(trace is not None for trace in traces.values())
    # The metadata attributes the run sent, at each scope, must be among the names.
    pairs = [pair for run in replay.runs for pair in _items(run, "traces")]
    expected = _kept_trace_keys(pairs)
    expected["resource"].add(HOST_LABEL)
    assert all(expected.values()), expected
    for scope, kept in expected.items():
        tags = _tags(tempo_url, scope, replay)
        leaked = sorted(tag for tag in tags if _is_forbidden_key(tag))
        assert not leaked, f"Tempo holds {leaked} as {scope} attributes"
        assert kept <= set(tags), f"{scope} tags lack {sorted(kept - set(tags))}"
        for tag in tags:
            path = f"/api/v2/search/tag/{quote(f'{scope}.{tag}', safe='')}/values"
            body = _body(tempo_url, path, _whole_seconds(replay))
            _assert_no_canary(body, replay, f"Tempo {path}")
