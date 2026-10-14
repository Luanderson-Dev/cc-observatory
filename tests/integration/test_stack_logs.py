"""Claude Code's events in Loki, in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. The log records of every
fixture scenario are replayed through the collector with fresh session IDs and read
back from Loki on its debug port (ADR 0015): every record is stored once, only
``service_name`` is an index label, and the session, event and trace fields come back
as structured metadata that LogQL can filter on. The last test stops Loki and checks
that the collector keeps what it receives meanwhile and delivers it once Loki is back.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest
import yaml

from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    LOG_CONTENT_CANARY_KEYS,
    Prepared,
    load_scenario,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

LOKI: Final = "loki"
SERVICE: Final = "claude-code"
# Seconds to wait until every replayed record can be queried, and between two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
# Seconds for Loki to be healthy again after a start (its /ready waits 15 s), and for
# the collector to deliver what it queued meanwhile: its retries back off up to 30 s
# between attempts (stack/otel-collector/config.yaml).
HEALTHY_DEADLINE: Final = 120.0
REDELIVERY_DEADLINE: Final = 120.0
HTTP_TIMEOUT: Final = 10.0
# Loki's default cap on the entries one query returns; every scenario has fewer records.
QUERY_LIMIT: Final = 5000
NANOS_PER_SECOND: Final = 1_000_000_000
# The index labels Loki may have: service_name now, host from a later stage (ADR 0015).
ALLOWED_LABELS: Final = frozenset({"service_name", "host"})
# Per-session, per-event and per-span fields, as Loki names them: never index labels.
SESSION_FIELDS: Final = ("session_id", "prompt_id", "event_name", "trace_id", "span_id")
# Structured metadata keys that queries rely on (ADR 0015).
EXPECTED_METADATA: Final = frozenset(
    {"session_id", "event_name", "event_sequence", "event_timestamp", "prompt_id"}
    | {"trace_id", "span_id"}
)
# Identity attributes as Loki would name them; the collector deletes them (ADR 0021).
IDENTITY_METADATA: Final = (
    "user_id",
    "user_email",
    "user_account_uuid",
    "user_account_id",
    "organization_id",
)
# Content attributes as Loki would name them (prompt, prompt_text, response, error,
# tool_input, tool_parameters, ...); the collector deletes them (ADR 0021).
CONTENT_METADATA: Final = tuple(key.replace(".", "_") for key in LOG_CONTENT_CANARY_KEYS)
# Separates index labels ("stream") from structured metadata in query results.
CATEGORIZE: Final = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}
# A session ID that no replay uses.
UNKNOWN_SESSION: Final = "00000000-0000-4000-8000-000000000000"


@dataclass(frozen=True)
class Entry:
    """One log entry as Loki returns it: index labels, line and structured metadata."""

    labels: Mapping[str, str]
    timestamp: int
    line: str
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class Replay:
    """The scenarios sent, and the start of the window that holds their records."""

    runs: tuple[Prepared, ...]
    start_ns: int


# Reading the prepared requests.


def _records(run: Prepared) -> Iterator[JSONObject]:
    """Every log record ``run`` sent."""
    for request in run.logs:
        for resource in _objects(request.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                yield from _objects(scope.get("logRecords"))


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _attribute(record: JSONObject, key: str) -> str | None:
    for attribute in _objects(record.get("attributes")):
        value = attribute.get("value")
        if attribute.get("key") == key and isinstance(value, dict):
            text = value.get("stringValue")
            return text if isinstance(text, str) else None
    return None


def _ids(run: Prepared, field: str) -> set[str]:
    """The non-empty ``traceId`` or ``spanId`` values of ``run``'s log records."""
    values = (record.get(field) for record in _records(run))
    return {value for value in values if isinstance(value, str) and value}


# Talking to Loki.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _strings(value: object, where: str) -> dict[str, str]:
    return {key: str(item) for key, item in _mapping(value, where).items()}


def _get(
    base: str, path: str, params: Mapping[str, str | int], headers: Mapping[str, str] | None = None
) -> bytes:
    """The body of a 200 answer to ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        target = f"{path}?{urlencode(params)}" if params else path
        connection.request("GET", target, headers=dict(headers or {}))
        response = connection.getresponse()
        status, body = response.status, response.read()
    finally:
        connection.close()
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    return body


def _data(base: str, path: str, params: Mapping[str, str | int], **headers: str) -> object:
    """The ``data`` field of a successful Loki API answer."""
    document = _mapping(json.loads(_get(base, path, params, headers)), path)
    assert document.get("status") == "success", document
    return document.get("data")


def _window(replay: Replay) -> dict[str, str | int]:
    """From just before the first replayed record to a minute from now."""
    return {"start": replay.start_ns, "end": time.time_ns() + 60 * NANOS_PER_SECOND}


def _query(base: str, logql: str, replay: Replay) -> list[Entry]:
    """Every entry ``logql`` selects in the replay window, oldest first."""
    params = {**_window(replay), "query": logql, "limit": QUERY_LIMIT, "direction": "forward"}
    data = _mapping(_data(base, "/loki/api/v1/query_range", params, **CATEGORIZE), "data")
    assert data.get("resultType") == "streams", data.get("resultType")
    entries: list[Entry] = []
    for stream in _list(data.get("result"), "result"):
        item = _mapping(stream, "stream")
        labels = _strings(item.get("stream"), "stream labels")
        for value in _list(item.get("values"), "values"):
            parts = _list(value, "value")
            categories = _mapping(parts[2], "categories") if len(parts) > 2 else {}
            metadata = _strings(categories.get("structuredMetadata") or {}, "structuredMetadata")
            entries.append(Entry(labels, int(str(parts[0])), str(parts[1]), metadata))
    assert len(entries) < QUERY_LIMIT, f"{logql} hit the query limit"
    return sorted(entries, key=lambda entry: entry.timestamp)


def _stored_ids(entries: list[Entry], key: str) -> set[str]:
    """The ``trace_id`` or ``span_id`` values in the structured metadata of ``entries``."""
    return {entry.metadata[key] for entry in entries if key in entry.metadata}


def _session_query(session_id: str, *filters: str) -> str:
    return " | ".join([f'{{service_name="{SERVICE}"}}', f'session_id="{session_id}"', *filters])


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


def _record_count(run: Prepared) -> int:
    return sum(1 for _ in _records(run))


def _replay_of(run: Prepared) -> Replay:
    """A replay of ``run`` alone, with the window that holds its records."""
    times = (int(str(record.get("timeUnixNano"))) for record in _records(run))
    return Replay((run,), min(times, default=time.time_ns()) - NANOS_PER_SECOND)


# Fixtures.


@pytest.fixture(scope="module")
def replay(otlp_http_url: str) -> Replay:
    """The log records of every scenario that has them, sent to the collector."""
    now_ns = time.time_ns()
    names = [name for name in scenario_names(with_otlp=True) if load_scenario(name).logs]
    assert names, "no fixture scenario has OTLP log records"
    runs = tuple(prepare(load_scenario(name), now_ns=now_ns) for name in names)
    for run in runs:
        send(otlp_http_url, run, signals=("logs",))
    records = [record for run in runs for record in _records(run)]
    first = min((int(str(record.get("timeUnixNano"))) for record in records), default=now_ns)
    return Replay(runs, first - NANOS_PER_SECOND)


@pytest.fixture(scope="module")
def stored(loki_url: str, replay: Replay) -> dict[str, list[Entry]]:
    """Each replayed session's entries in Loki, once every record is there or the
    deadline has passed (the tests then report the difference)."""
    expected = {run.session_id: _record_count(run) for run in replay.runs}

    def read() -> dict[str, list[Entry]]:
        return {
            session_id: _query(loki_url, _session_query(session_id), replay)
            for session_id in expected
        }

    def done(found: dict[str, list[Entry]]) -> bool:
        return {session_id: len(entries) for session_id, entries in found.items()} == expected

    return _poll(read, done, DEADLINE)


# Tests.


def test_loki_is_healthy(service_health: Callable[[str], str]) -> None:
    assert service_health(LOKI) == "healthy"


def test_every_replayed_record_is_stored_once(
    replay: Replay, stored: dict[str, list[Entry]]
) -> None:
    # The fixtures hold records that share their timestamp and body (hook_registered,
    # plugin_loaded); Loki must keep each one, since their attributes differ.
    for run in replay.runs:
        assert len(stored[run.session_id]) == _record_count(run), run.scenario


def test_only_service_name_and_host_are_index_labels(
    loki_url: str, replay: Replay, stored: dict[str, list[Entry]]
) -> None:
    assert stored
    labels = set(map(str, _list(_data(loki_url, "/loki/api/v1/labels", _window(replay)), "")))
    assert "service_name" in labels
    assert labels <= ALLOWED_LABELS, f"unexpected index labels {sorted(labels - ALLOWED_LABELS)}"
    assert not labels & set(SESSION_FIELDS)
    values = _data(loki_url, "/loki/api/v1/label/service_name/values", _window(replay))
    assert SERVICE in _list(values, "service_name values")
    for entries in stored.values():
        for entry in entries:
            assert entry.labels.get("service_name") == SERVICE
            assert set(entry.labels) <= ALLOWED_LABELS


def test_session_event_and_trace_fields_are_structured_metadata(
    replay: Replay, stored: dict[str, list[Entry]]
) -> None:
    keys: set[str] = set()
    for run in replay.runs:
        entries = stored[run.session_id]
        assert entries, run.scenario
        for entry in entries:
            keys.update(entry.metadata)
            assert entry.metadata.get("session_id") == run.session_id
            # Claude Code's body is claude_code.<event.name>.
            assert entry.line == f"claude_code.{entry.metadata.get('event_name')}"
        assert _stored_ids(entries, "trace_id") == _ids(run, "traceId"), run.scenario
        assert _stored_ids(entries, "span_id") == _ids(run, "spanId"), run.scenario
    missing = EXPECTED_METADATA - keys
    assert not missing, f"structured metadata misses {sorted(missing)}; it has {sorted(keys)}"
    assert not keys & set(IDENTITY_METADATA)
    # The fixtures carry prompt, prompt_text, response and error; none may get through.
    assert not keys & set(CONTENT_METADATA), sorted(keys & set(CONTENT_METADATA))


def test_logql_filters_on_structured_metadata(
    loki_url: str, replay: Replay, stored: dict[str, list[Entry]]
) -> None:
    assert stored
    run = max(replay.runs, key=_record_count)
    events = Counter(_attribute(record, "event.name") for record in _records(run))
    event, count = events.most_common(1)[0]
    assert event
    entries = _query(loki_url, _session_query(run.session_id, f'event_name="{event}"'), replay)
    assert len(entries) == count
    assert {entry.metadata.get("event_name") for entry in entries} == {event}
    assert _query(loki_url, _session_query(UNKNOWN_SESSION), replay) == []


def test_loki_runs_without_usage_reports_and_with_retention(loki_url: str) -> None:
    config = _mapping(yaml.safe_load(_get(loki_url, "/config", {})), "/config")
    assert _mapping(config.get("analytics"), "analytics").get("reporting_enabled") is False
    limits = _mapping(config.get("limits_config"), "limits_config")
    # 720h from .env.example, through -config.expand-env; Loki prints it in days.
    assert limits.get("retention_period") == "30d"
    assert _mapping(config.get("compactor"), "compactor").get("retention_enabled") is True


def test_loki_logs_no_errors(
    service_logs: Callable[[str], str], stored: dict[str, list[Entry]]
) -> None:
    assert stored
    logs = service_logs(LOKI)
    assert logs.strip(), "Loki logged nothing at all"
    errors = [line for line in logs.splitlines() if "level=error" in line]
    assert not errors, "\n".join(errors[:5])


# Last in this module: it stops Loki, which logs errors while it shuts down, so it runs
# after test_loki_logs_no_errors, and it leaves Loki healthy for the modules that follow.


def test_events_sent_while_loki_is_stopped_are_stored_once_it_is_back(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_health: Callable[[str], str],
    otlp_http_url: str,
    loki_url: str,
) -> None:
    # Backs the collector's missing depends_on (ADR 0015): it accepts events while Loki
    # is down, queues them and delivers them once Loki is back.
    names = [name for name in scenario_names(with_otlp=True) if load_scenario(name).logs]
    runs = [prepare(load_scenario(name), now_ns=time.time_ns()) for name in names]
    run = min(runs, key=_record_count)
    compose("stop", LOKI)
    try:
        assert service_health(LOKI) != "healthy"
        # Raises ReplayError if the collector refuses any request.
        assert send(otlp_http_url, run, signals=("logs",)) == len(run.logs)
    finally:
        compose("start", LOKI)
        health = _poll(
            lambda: service_health(LOKI), lambda state: state == "healthy", HEALTHY_DEADLINE
        )
    assert health == "healthy"
    replay = _replay_of(run)
    entries = _poll(
        lambda: _query(loki_url, _session_query(run.session_id), replay),
        lambda found: len(found) == _record_count(run),
        REDELIVERY_DEADLINE,
    )
    assert len(entries) == _record_count(run), run.scenario
    assert {entry.metadata.get("session_id") for entry in entries} == {run.session_id}
