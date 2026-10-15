"""Claude Code's traces in Tempo, in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. The project starts with the
``traces`` profile (``tools/stack/isolated.py``), so Tempo runs there. The spans of every
fixture scenario are replayed through the collector with fresh session, trace and span
IDs, and with the privacy canaries planted, then read back from Tempo on its debug port:
every trace comes back by ID with exactly the spans sent and their parent links, a
TraceQL search by session finds the run's traces, and no identity or content attribute
and no ``tool.output`` span event is stored (ADR 0021; ``test_stack_privacy.py``
searches every backend for every canary). Tempo is optional (ADR 0003): the last test
stops it and checks that the collector keeps storing metrics and logs, drops the traces
it cannot deliver within its short retry, and reaches Tempo again once it is back.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest
import yaml

from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    CANARY_PREFIX,
    CONTENT_SPAN_EVENT,
    HOST_PATHS_CANARY_KEYS,
    SPAN_CONTENT_CANARY_KEYS,
    Prepared,
    is_identity_key,
    load_scenario,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

TEMPO: Final = "tempo"
COLLECTOR: Final = "otel-collector"
TEMPO_EXPORTER: Final = "otlp_http/tempo"
JOB: Final = "claude-code"
SERVICE: Final = "claude-code"
# Seconds to wait until every replayed trace (or search result, series or record) can
# be read, and between two polls. Tempo serves new spans from its live store at once,
# and its search sees a trace once no span has arrived for a few seconds.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
# Seconds for Tempo to be healthy again after a start, and for the collector to drop
# the traces it could not deliver: its retry gives up after 30 s
# (stack/otel-collector/config.yaml).
HEALTHY_DEADLINE: Final = 120.0
DROP_DEADLINE: Final = 90.0
HTTP_TIMEOUT: Final = 10.0
NANOS_PER_SECOND: Final = 1_000_000_000
# Queries cover the last hour; the oldest replayed span starts less than 3 minutes ago.
WINDOW_SECONDS: Final = 3600
MARGIN_SECONDS: Final = 60
SEARCH_LIMIT: Final = 100
QUERY_LIMIT: Final = 5000
# The image's data directory, where the tempo-data volume is mounted.
DATA_DIR: Final = PurePosixPath("/var/tempo")
# 168h from .env.example, through -config.expand-env; Tempo prints Go durations.
RETENTION: Final = "168h0m0s"
# Content attributes as Claude Code puts them on spans (ADR 0021).
CONTENT_KEYS: Final = frozenset((*SPAN_CONTENT_CANARY_KEYS, *HOST_PATHS_CANARY_KEYS))
# The span event Claude Code sends for every API attempt: metadata, kept.
KEPT_SPAN_EVENT: Final = "gen_ai.request.attempt"
# A session ID that no replay uses.
UNKNOWN_SESSION: Final = "00000000-0000-4000-8000-000000000000"
# Tempo 3.1.0's backend worker logs this at error level each time it finds the job
# queue empty while idle: every 15 s or so at first (a 15 s long poll, then a backoff
# that grows), settling near once a minute after about two minutes. Fixed upstream by
# grafana/tempo#8039 (the scheduler's "no jobs found" is no longer a failure); remove
# this exception once a release has it. Every other error line fails the test.
IDLE_WORKER_ERROR: Final = re.compile(
    r'msg="error calling scheduler" err="rpc error: code = NotFound desc = no jobs found"'
)
# The collector's console log: <time>\t<level>\t<caller>\t<message>\t<JSON fields>.
COLLECTOR_ERROR: Final = re.compile(r"\terror\t")
DROPPED: Final = "Exporting failed. Dropping data."


@dataclass(frozen=True)
class Span:
    """A span's identity: hex trace, span and parent IDs (empty for a root) and name."""

    trace_id: str
    span_id: str
    parent_id: str
    name: str


@dataclass(frozen=True)
class Stored:
    """A trace as ``/api/v2/traces/<id>`` returns it: its spans by ID, how many span
    entries came back (duplicates included), the attribute keys and event names at each
    level, and the raw body."""

    spans: Mapping[str, Span]
    entries: int
    resource_keys: frozenset[str]
    span_keys: frozenset[str]
    event_names: frozenset[str]
    event_keys: frozenset[str]
    body: bytes


# Reading the prepared requests.


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _spans_of(document: JSONObject) -> Iterator[tuple[JSONObject, JSONObject]]:
    """(resource, span) for every span of an OTLP traces document."""
    for resource_spans in _objects(document.get("resourceSpans")):
        resource = resource_spans.get("resource")
        owner = resource if isinstance(resource, dict) else {}
        for scope in _objects(resource_spans.get("scopeSpans")):
            for span in _objects(scope.get("spans")):
                yield owner, span


def _text(value: JSON) -> str:
    return value if isinstance(value, str) else ""


def _sent(runs: tuple[Prepared, ...]) -> dict[str, dict[str, Span]]:
    """The spans ``runs`` sent, by trace ID and span ID (IDs are hex in OTLP JSON)."""
    traces: dict[str, dict[str, Span]] = {}
    for run in runs:
        for request in run.traces:
            for _, span in _spans_of(request):
                found = Span(
                    _text(span.get("traceId")),
                    _text(span.get("spanId")),
                    _text(span.get("parentSpanId")),
                    _text(span.get("name")),
                )
                assert found.span_id not in traces.get(found.trace_id, {}), found
                traces.setdefault(found.trace_id, {})[found.span_id] = found
    return traces


def _trace_ids(run: Prepared) -> set[str]:
    return set(_sent((run,)))


def _depth(spans: Mapping[str, Span], span: Span) -> int:
    """How many ancestors ``span`` has among ``spans``."""
    depth = 0
    while span.parent_id in spans:
        span = spans[span.parent_id]
        depth += 1
    return depth


def _keys(owner: JSONObject) -> set[str]:
    return {_text(attribute.get("key")) for attribute in _objects(owner.get("attributes"))}


# Talking to the backends.


def _request(
    base: str, path: str, params: Mapping[str, str | int] | None = None
) -> tuple[int, bytes]:
    """The status and body of ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        target = f"{path}?{urlencode(params)}" if params else path
        connection.request("GET", target)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _json(base: str, path: str, params: Mapping[str, str | int] | None = None) -> JSONObject:
    """The JSON object of a 200 answer."""
    status, body = _request(base, path, params)
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    document = json.loads(body)
    assert isinstance(document, dict), f"GET {path}: {document!r}"
    return document


def _hex(value: JSON) -> str:
    """A trace or span ID as Tempo's v2 API writes it (protobuf JSON: base64), in hex."""
    return base64.b64decode(value).hex() if isinstance(value, str) and value else ""


def _stored(base: str, trace_id: str) -> Stored | None:
    """The trace ``trace_id`` from Tempo, or ``None`` while it is not there."""
    status, body = _request(base, f"/api/v2/traces/{trace_id}")
    if status == 404:
        return None
    assert status == 200, f"GET /api/v2/traces/{trace_id} answered {status}: {body[:300]!r}"
    document = json.loads(body)
    trace = document.get("trace") if isinstance(document, dict) else None
    spans: dict[str, Span] = {}
    entries = 0
    resource_keys: set[str] = set()
    span_keys: set[str] = set()
    event_names: set[str] = set()
    event_keys: set[str] = set()
    for resource, span in _spans_of(trace if isinstance(trace, dict) else {}):
        entries += 1
        found = Span(
            _hex(span.get("traceId")),
            _hex(span.get("spanId")),
            _hex(span.get("parentSpanId")),
            _text(span.get("name")),
        )
        spans[found.span_id] = found
        resource_keys |= _keys(resource)
        span_keys |= _keys(span)
        for event in _objects(span.get("events")):
            event_names.add(_text(event.get("name")))
            event_keys |= _keys(event)
    return Stored(
        spans,
        entries,
        frozenset(resource_keys),
        frozenset(span_keys),
        frozenset(event_names),
        frozenset(event_keys),
        body,
    )


def _complete(found: Mapping[str, Stored | None], sent: Mapping[str, Mapping[str, Span]]) -> bool:
    return all(
        (stored := found.get(trace_id)) is not None and stored.entries == len(spans)
        for trace_id, spans in sent.items()
    )


def _wait_for_traces(base: str, sent: Mapping[str, Mapping[str, Span]]) -> dict[str, Stored | None]:
    """Each sent trace from Tempo, once all are complete or the deadline has passed;
    complete traces are not read again."""
    found: dict[str, Stored | None] = {}

    def read() -> dict[str, Stored | None]:
        for trace_id, spans in sent.items():
            stored = found.get(trace_id)
            if stored is None or stored.entries != len(spans):
                found[trace_id] = _stored(base, trace_id)
        return found

    return _poll(read, lambda result: _complete(result, sent), DEADLINE)


def _search(base: str, session_id: str) -> set[str]:
    """The IDs of the traces a TraceQL search for spans of ``session_id`` finds in the
    last hour. Tempo writes search results' trace IDs in hex without leading zeros."""
    now = int(time.time())
    params: dict[str, str | int] = {
        "q": f'{{ span."session.id" = "{session_id}" }}',
        "start": now - WINDOW_SECONDS,
        "end": now + MARGIN_SECONDS,
        "limit": SEARCH_LIMIT,
    }
    traces = _objects(_json(base, "/api/search", params).get("traces"))
    assert len(traces) < SEARCH_LIMIT, "the search hit its limit"
    return {_text(trace.get("traceID")).rjust(32, "0") for trace in traces}


def _status_config(base: str) -> dict[str, object]:
    """Tempo's running configuration: ``/status/config`` writes a title line, then the
    YAML document."""
    status, body = _request(base, "/status/config")
    assert status == 200, f"GET /status/config answered {status}"
    documents = [item for item in yaml.safe_load_all(body) if isinstance(item, dict)]
    assert len(documents) == 1, body[:300]
    return {str(key): value for key, value in documents[0].items()}


def _at(node: object, *path: str) -> object:
    for key in path:
        assert isinstance(node, dict), f"{'.'.join(path)}: {node!r}"
        node = node.get(key)
    return node


def _paths(node: object) -> Iterator[str]:
    """Every non-empty string under a key that names a path or a directory."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and str(key).endswith(("path", "_dir", "directory")):
                if value:
                    yield value
            else:
                yield from _paths(value)
    elif isinstance(node, list):
        for value in node:
            yield from _paths(value)


def _series_count(base: str, session_id: str) -> int:
    """How many Claude Code metric series Prometheus has for ``session_id`` in the last
    hour."""
    now = time.time()
    params: dict[str, str | int] = {
        "match[]": f'{{job="{JOB}",session_id="{session_id}"}}',
        "start": f"{now - WINDOW_SECONDS:.3f}",
        "end": f"{now + MARGIN_SECONDS:.3f}",
    }
    document = _json(base, "/api/v1/series", params)
    assert document.get("status") == "success", document
    data = document.get("data")
    assert isinstance(data, list), document
    return len(data)


def _records(run: Prepared) -> list[JSONObject]:
    return [
        record
        for request in run.logs
        for resource in _objects(request.get("resourceLogs"))
        for scope in _objects(resource.get("scopeLogs"))
        for record in _objects(scope.get("logRecords"))
    ]


def _log_count(base: str, run: Prepared) -> int:
    """How many of ``run``'s events Loki holds."""
    first = min(int(_text(record.get("timeUnixNano")) or 0) for record in _records(run))
    params: dict[str, str | int] = {
        "query": f'{{service_name="{SERVICE}"}} | session_id="{run.session_id}"',
        "start": first - NANOS_PER_SECOND,
        "end": time.time_ns() + MARGIN_SECONDS * NANOS_PER_SECOND,
        "limit": QUERY_LIMIT,
        "direction": "forward",
    }
    document = _json(base, "/loki/api/v1/query_range", params)
    data = document.get("data")
    assert isinstance(data, dict), document
    streams = _objects(data.get("result"))
    return sum(
        len(values) if isinstance(values := stream.get("values"), list) else 0 for stream in streams
    )


def _exited(compose: Callable[..., subprocess.CompletedProcess[str]], service: str) -> object:
    """The exit code of ``service``'s container, which must have stopped."""
    output = compose("ps", "--all", "--format", "json", service).stdout
    # Compose prints one JSON object per line (older versions: one JSON list).
    documents = [json.loads(line) for line in output.splitlines() if line.strip()]
    entries = [
        item
        for document in documents
        for item in (document if isinstance(document, list) else [document])
        if isinstance(item, dict)
    ]
    assert len(entries) == 1, f"expected one {service} container, got {output!r}"
    assert entries[0].get("State") == "exited", entries[0]
    return entries[0].get("ExitCode")


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


# Fixtures.


@pytest.fixture(scope="module")
def replay(otlp_http_url: str) -> tuple[Prepared, ...]:
    """The spans of every scenario that has them, with canaries, sent to the collector."""
    now_ns = time.time_ns()
    names = [name for name in scenario_names(with_otlp=True) if load_scenario(name).traces]
    assert names, "no fixture scenario has OTLP spans"
    runs = tuple(prepare(load_scenario(name), now_ns=now_ns, canaries=True) for name in names)
    for run in runs:
        send(otlp_http_url, run, signals=("traces",))
    return runs


@pytest.fixture(scope="module")
def sent(replay: tuple[Prepared, ...]) -> dict[str, dict[str, Span]]:
    return _sent(replay)


@pytest.fixture(scope="module")
def stored(tempo_url: str, sent: dict[str, dict[str, Span]]) -> dict[str, Stored | None]:
    """Each replayed trace as Tempo returns it, once all are complete or the deadline has
    passed (the tests then report the difference)."""
    return _wait_for_traces(tempo_url, sent)


# Tests.


def test_tempo_is_healthy(service_health: Callable[[str], str]) -> None:
    assert service_health(TEMPO) == "healthy"


def test_the_fixtures_carry_traces_with_parent_chains(sent: dict[str, dict[str, Span]]) -> None:
    assert len(sent) >= 2, "the fixtures should hold several traces"
    deepest = max(_depth(spans, span) for spans in sent.values() for span in spans.values())
    # Such as interaction > tool > tool.execution > subagent interaction > llm_request.
    assert deepest >= 3, deepest


def test_every_trace_comes_back_with_exactly_the_spans_sent(
    sent: dict[str, dict[str, Span]], stored: dict[str, Stored | None]
) -> None:
    for trace_id, spans in sent.items():
        found = stored.get(trace_id)
        assert found is not None, f"trace {trace_id} is not in Tempo"
        assert found.entries == len(spans), f"trace {trace_id}: {found.entries} of {len(spans)}"
        assert set(found.spans) == set(spans), trace_id


def test_parent_links_and_names_survive(
    sent: dict[str, dict[str, Span]], stored: dict[str, Stored | None]
) -> None:
    chains = 0
    for trace_id, spans in sent.items():
        found = stored.get(trace_id)
        assert found is not None, f"trace {trace_id} is not in Tempo"
        assert dict(found.spans) == spans, trace_id
        roots = [span for span in found.spans.values() if not span.parent_id]
        assert len(roots) == 1, f"trace {trace_id} has roots {roots}"
        chains += any(_depth(found.spans, span) >= 2 for span in found.spans.values())
    assert chains, "no stored trace has a grandchild span"


def test_traceql_search_by_session_finds_the_runs_traces(
    tempo_url: str, replay: tuple[Prepared, ...], stored: dict[str, Stored | None]
) -> None:
    # The app will link a session to its traces this way.
    assert stored
    expected = {run.session_id: _trace_ids(run) for run in replay}
    found = _poll(
        lambda: {session_id: _search(tempo_url, session_id) for session_id in expected},
        lambda result: result == expected,
        DEADLINE,
    )
    assert found == expected
    assert _search(tempo_url, UNKNOWN_SESSION) == set()


def test_no_identity_or_content_reaches_tempo(
    replay: tuple[Prepared, ...], stored: dict[str, Stored | None]
) -> None:
    # The replay planted identity and content canaries on every resource and span, and a
    # tool.output event on every span; the fixtures already carry user.id, user.email,
    # the account IDs, organization.id and free-text errors.
    assert all(run.canary_values for run in replay)
    traces = [trace for trace in stored.values() if trace is not None]
    assert traces
    keys = {
        key for trace in traces for key in trace.resource_keys | trace.span_keys | trace.event_keys
    }
    assert not [key for key in keys if is_identity_key(key)], sorted(keys)
    assert not keys & CONTENT_KEYS, sorted(keys & CONTENT_KEYS)
    events = {name for trace in traces for name in trace.event_names}
    assert CONTENT_SPAN_EVENT not in events
    assert not [trace for trace in traces if CANARY_PREFIX.encode() in trace.body]
    # Metadata stays: the session on every span, and the other span events.
    assert all("session.id" in trace.span_keys for trace in traces)
    assert KEPT_SPAN_EVENT in events


def test_tempo_runs_single_tenant_without_extras_and_with_retention(tempo_url: str) -> None:
    # That it sends no usage reports is checked in test_stack_no_outbound.py.
    config = _status_config(tempo_url)
    assert config.get("target") == "all"
    # Both are written only when true.
    assert not config.get("multitenancy_enabled")
    assert not config.get("auth_enabled")
    assert _at(config, "query_frontend", "mcp_server", "enabled") is False
    # No storage path: Tempo leaves the metrics generator disabled.
    assert _at(config, "metrics_generator", "storage", "path") == ""
    assert _at(config, "backend_worker", "compaction", "block_retention") == RETENTION
    paths = list(_paths(config))
    assert paths
    outside = [path for path in paths if not PurePosixPath(path).is_relative_to(DATA_DIR)]
    assert not outside, f"paths outside {DATA_DIR}: {outside}"


def test_tempo_logs_no_errors(
    service_logs: Callable[[str], str], stored: dict[str, Stored | None]
) -> None:
    assert stored
    # Only this session's lines; Tempo logs every query, and this module makes many.
    logs = service_logs(TEMPO)
    assert logs.strip(), "Tempo logged nothing at all"
    errors = [
        line
        for line in logs.splitlines()
        if "level=error" in line and not IDLE_WORKER_ERROR.search(line)
    ]
    assert not errors, "\n".join(errors[:5])


# Last in this module: it stops Tempo, which logs errors while it shuts down, so it runs
# after test_tempo_logs_no_errors, and it leaves Tempo healthy. Tempo exits cleanly on
# SIGTERM because its backend worker's finish_on_shutdown_timeout is short: in 3.1.0
# the worker keeps calling the stopped scheduler ("nil job received") until it runs out,
# and the upstream default (30s) would outlast Compose's grace period, so Tempo would be
# killed (exit 137). Even after a clean exit, the next start logs "failed to replay
# block. removing." when Tempo had received traces: it is the empty head block (one
# empty segment, no meta.json) that the live store opens after its last cut, and no
# trace is lost.


def test_metrics_and_logs_are_stored_while_tempo_is_stopped(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_health: Callable[[str], str],
    service_logs: Callable[[str], str],
    otlp_http_url: str,
    prometheus_url: str,
    loki_url: str,
    tempo_url: str,
    sent: dict[str, dict[str, Span]],
    stored: dict[str, Stored | None],
) -> None:
    # Tempo runs only in the traces profile, and the collector has no depends_on: with
    # Tempo off, the traces exporter must not hold back the metrics and logs pipelines.
    # The module's traces are all in Tempo before it stops, so that the end of the test
    # can find them again after the restart.
    assert _complete(stored, sent), "the module's traces are not all in Tempo"
    names = [
        name
        for name in scenario_names(with_otlp=True)
        if (scenario := load_scenario(name)).metrics and scenario.logs and scenario.traces
    ]
    assert names, "no fixture scenario has metrics, logs and spans"
    runs = [prepare(load_scenario(name), now_ns=time.time_ns()) for name in names]
    run = min(runs, key=lambda item: len(item.metrics) + len(item.logs) + len(item.traces))
    before = len(service_logs(COLLECTOR).splitlines())
    compose("stop", TEMPO)
    try:
        exit_code = _exited(compose, TEMPO)
        assert service_health(TEMPO) != "healthy"
        # The collector accepts the spans too: its traces exporter queues them.
        total = len(run.metrics) + len(run.logs) + len(run.traces)
        assert send(otlp_http_url, run) == total
        series = _poll(
            lambda: _series_count(prometheus_url, run.session_id),
            lambda count: count == len(run.expected),
            DEADLINE,
        )
        records = _poll(
            lambda: _log_count(loki_url, run),
            lambda count: count == len(_records(run)),
            DEADLINE,
        )
        # The undeliverable spans leave the collector once the short retry gives up.
        new = _poll(
            lambda: service_logs(COLLECTOR).splitlines()[before:],
            lambda lines: any(DROPPED in line and TEMPO_EXPORTER in line for line in lines),
            DROP_DEADLINE,
        )
        collector = service_health(COLLECTOR)
    finally:
        compose("start", TEMPO)
        health = _poll(
            lambda: service_health(TEMPO), lambda state: state == "healthy", HEALTHY_DEADLINE
        )
    assert health == "healthy"
    # Tempo exited by itself within the grace period: not killed (137).
    assert exit_code == 0, f"Tempo exited with {exit_code}"
    assert series == len(run.expected), run.scenario
    assert records == len(_records(run)), run.scenario
    assert collector == "healthy"
    errors = [line for line in new if COLLECTOR_ERROR.search(line)]
    assert [line for line in errors if DROPPED in line and TEMPO_EXPORTER in line], new[-5:]
    # Every error names the traces exporter: the metrics and logs exporters had none.
    assert all(TEMPO_EXPORTER in line for line in errors), [
        line for line in errors if TEMPO_EXPORTER not in line
    ][:5]
    # The traces stored before Tempo stopped survive the restart: Tempo replays its WAL
    # and reads its blocks from the tempo-data volume.
    survived = _wait_for_traces(tempo_url, sent)
    assert _complete(survived, sent), [
        trace_id for trace_id in sent if survived.get(trace_id) is None
    ][:5]
    # The exporter reaches Tempo again with no collector restart.
    again = prepare(load_scenario(run.scenario), now_ns=time.time_ns())
    send(otlp_http_url, again, signals=("traces",))
    expected = _sent((again,))
    found = _wait_for_traces(tempo_url, expected)
    assert _complete(found, expected), run.scenario
