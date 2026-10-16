"""Tests for the OTLP fixture replayer (``tools/stack/replay.py``).

Pure unit tests: small synthetic requests for the exact rules, the real fixtures for the
shapes, and a throwaway HTTP server on 127.0.0.1 for the sending side.
"""

from __future__ import annotations

import dataclasses
import json
import re
import socket
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Final, Literal, override

import pytest

import tools.stack.replay as replay_module
from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import Effect, SessionState, apply
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    CANARY_PREFIX,
    CONTENT_EVENT_NAMES,
    CONTENT_SPAN_EVENT,
    CONTENT_SPAN_EVENT_CANARY_KEYS,
    DATA_POINT_CANARY_KEYS,
    DEFAULT_FIXTURES,
    DEFAULT_HOOKS_ENDPOINT,
    HOOK_CONTENT_CANARY_KEYS,
    HOOK_NESTED_CANARY_KEYS,
    HOOK_NESTED_KEY,
    HOOKS_PATH,
    IDENTITY_CANARY_KEYS,
    LOG_CONTENT_CANARY_KEYS,
    LOG_RECORD_CANARY_KEYS,
    RESOURCE_CANARY_KEYS,
    SIGNALS,
    SPAN_CANARY_KEYS,
    SPAN_CONTENT_CANARY_KEYS,
    SPAN_EVENT_CANARY_KEYS,
    HookLine,
    Prepared,
    ReplayError,
    Scenario,
    SeriesKey,
    Signal,
    contiguous_series,
    expected_totals,
    fresh_session_id,
    is_identity_key,
    load_scenario,
    main,
    post_hook,
    post_otlp,
    prepare,
    prepare_hooks,
    scenario_names,
    send,
    send_hooks,
    shift_iso_timestamp,
)

# Synthetic data. T0 is 2023-11-14T22:13:20Z; no value here comes from the fixtures.
SECOND: Final = 1_000_000_000
T0: Final = 1_700_000_000 * SECOND
OLD_SESSION: Final = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION: Final = "66666666-7777-4888-9999-aaaaaaaaaaaa"
TRACE_MAIN: Final = "0af7651916cd43dd8448eb211c80319c"
TRACE_LINKED: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ROOT: Final = "b7ad6b7169203331"
SPAN_CHILD: Final = "00f067aa0ba902b7"
SPAN_LINKED: Final = "53995c3f42cd8ad8"
ORIGINAL_IDS: Final = (TRACE_MAIN, TRACE_LINKED, SPAN_ROOT, SPAN_CHILD, SPAN_LINKED)
# The latest synthetic timestamp is the root span's end, 50 s after T0.
LATEST: Final = T0 + 50 * SECOND
# Not a whole number of milliseconds, so event.timestamp has to round.
NOW: Final = T0 + 1_000 * SECOND + 123_456_789
IDENTITY_FAKES: Final = {
    "user.email": "user@example.com",
    "user.id": "0" * 64,
    "organization.id": "00000000-0000-4000-8000-000000000002",
    "user.account_uuid": "00000000-0000-4000-8000-000000000001",
    "user.account_id": "user_01EXAMPLE",
}
LAYOUT: Final[dict[Signal, tuple[str, str, str]]] = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}
NAMES: Final = scenario_names()
OTLP_NAMES: Final = scenario_names(with_otlp=True)


def attr(key: str, value: str) -> JSONObject:
    return {"key": key, "value": {"stringValue": value}}


def attr_list(labels: Mapping[str, str]) -> list[JSON]:
    return [attr(key, value) for key, value in labels.items()]


def nanos(offset_s: int) -> str:
    return str(T0 + offset_s * SECOND)


def iso(offset_s: int) -> str:
    moment = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC) + timedelta(seconds=offset_s)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def data_point(start_s: int, time_s: int, value: JSON, labels: Mapping[str, str]) -> JSONObject:
    point: JSONObject = {
        "attributes": attr_list({**IDENTITY_FAKES, "session.id": OLD_SESSION, **labels}),
        "startTimeUnixNano": nanos(start_s),
        "timeUnixNano": nanos(time_s),
    }
    point["asInt" if isinstance(value, str) else "asDouble"] = value
    return point


def sum_metric(name: str, *points: JSONObject) -> JSONObject:
    return {
        "name": name,
        "sum": {"dataPoints": list(points), "aggregationTemporality": 1, "isMonotonic": True},
    }


def resource() -> JSONObject:
    return {"attributes": attr_list({"service.name": "claude-code", "os.type": "linux"})}


def metrics_request(*metrics: JSONObject) -> JSONObject:
    scope: JSONObject = {"scope": {"name": "test.metrics"}, "metrics": list(metrics)}
    return {"resourceMetrics": [{"resource": resource(), "scopeMetrics": [scope]}]}


def log_record(
    time_s: int, event: str, *, trace_id: str | None = None, span_id: str | None = None
) -> JSONObject:
    record: JSONObject = {
        "timeUnixNano": nanos(time_s),
        "observedTimeUnixNano": nanos(time_s),
        "body": {"stringValue": f"claude_code.{event}"},
        "attributes": attr_list(
            {
                **IDENTITY_FAKES,
                "session.id": OLD_SESSION,
                "event.name": event,
                "event.timestamp": iso(time_s),
            }
        ),
    }
    if trace_id is not None and span_id is not None:
        record.update(traceId=trace_id, spanId=span_id, flags=1)
    return record


def logs_request(*records: JSONObject) -> JSONObject:
    scope: JSONObject = {"scope": {"name": "test.logs"}, "logRecords": list(records)}
    return {"resourceLogs": [{"resource": resource(), "scopeLogs": [scope]}]}


def span(
    span_id: str, parent: str, start_s: int, end_s: int, *, extra: JSONObject | None = None
) -> JSONObject:
    item: JSONObject = {
        "traceId": TRACE_MAIN,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": "claude_code.test",
        "kind": 1,
        "startTimeUnixNano": nanos(start_s),
        "endTimeUnixNano": nanos(end_s),
        "attributes": attr_list({**IDENTITY_FAKES, "session.id": OLD_SESSION}),
        "status": {},
    }
    item.update(extra or {})
    return item


def traces_request(*spans: JSONObject) -> JSONObject:
    scope: JSONObject = {"scope": {"name": "test.traces"}, "spans": list(spans)}
    return {"resourceSpans": [{"resource": resource(), "scopeSpans": [scope]}]}


def synthetic() -> Scenario:
    """A small session: two token series with a gap, a float and an int series, logs
    linked to a span, a zero timestamp, a root and a child span with an event and a link."""
    tokens = "claude_code.token.usage"
    first = metrics_request(
        sum_metric(
            tokens,
            data_point(0, 10, 5, {"type": "input", "model": "m"}),
            data_point(0, 10, 3, {"type": "output", "model": "m"}),
        ),
        sum_metric("claude_code.lines_of_code.count", data_point(0, 10, "3", {"type": "added"})),
    )
    second = metrics_request(
        # Starts 10 s after the previous point of the series ended: a gap.
        sum_metric(tokens, data_point(20, 30, 7, {"type": "input", "model": "m"})),
        sum_metric("claude_code.cost.usage", data_point(0, 30, 0.25, {"model": "m"})),
    )
    third = metrics_request(
        sum_metric(tokens, data_point(30, 40, 2, {"type": "input", "model": "m"})),
        sum_metric("claude_code.lines_of_code.count", data_point(10, 40, "4", {"type": "added"})),
        sum_metric("claude_code.cost.usage", data_point(30, 40, 0.5, {"model": "m"})),
    )
    startup = log_record(1, "hook_registered")
    startup["observedTimeUnixNano"] = "0"
    linked = log_record(5, "tool_result", trace_id=TRACE_MAIN, span_id=SPAN_CHILD)
    root = span(SPAN_ROOT, "", 2, 50)
    child = span(
        SPAN_CHILD,
        SPAN_ROOT,
        4,
        8,
        extra={
            "events": [{"timeUnixNano": nanos(6), "name": "gen_ai.request.attempt"}],
            "links": [
                {
                    "traceId": TRACE_LINKED,
                    "spanId": SPAN_LINKED,
                    "attributes": [attr("link.type", "parent_of")],
                }
            ],
        },
    )
    return Scenario(
        name="synthetic",
        session_id=OLD_SESSION,
        metrics=(first, second, third),
        logs=(logs_request(startup, linked), logs_request(log_record(45, "api_request"))),
        traces=(traces_request(child), traces_request(root)),
    )


class CountingBits:
    """A deterministic random source: 1, 2, 3, ... on every call."""

    def __init__(self) -> None:
        self.calls = 0

    def getrandbits(self, k: int, /) -> int:
        self.calls += 1
        return self.calls % (1 << k)


# Walking the requests.


def objects(value: JSON) -> list[JSONObject]:
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def resource_entries(requests: Iterable[JSONObject], signal: Signal) -> Iterator[JSONObject]:
    for request in requests:
        yield from objects(request[LAYOUT[signal][0]])


def items(requests: Iterable[JSONObject], signal: Signal) -> Iterator[JSONObject]:
    _, scopes, entries = LAYOUT[signal]
    for entry in resource_entries(requests, signal):
        for scope in objects(entry[scopes]):
            yield from objects(scope[entries])


def data_points(requests: Iterable[JSONObject]) -> Iterator[tuple[str, JSONObject, JSONObject]]:
    """(metric name, sum, data point) for every data point."""
    for metric in items(requests, "metrics"):
        name, data = metric["name"], metric["sum"]
        assert isinstance(name, str)
        assert isinstance(data, dict)
        for point in objects(data["dataPoints"]):
            yield name, data, point


def attributes(owner: JSONObject) -> dict[str, JSON]:
    found: dict[str, JSON] = {}
    for item in objects(owner.get("attributes", [])):
        key = item["key"]
        assert isinstance(key, str)
        found[key] = item["value"]
    return found


def string_attr(owner: JSONObject, key: str) -> str | None:
    value = attributes(owner).get(key)
    if isinstance(value, dict) and isinstance(value.get("stringValue"), str):
        text = value["stringValue"]
        assert isinstance(text, str)
        return text
    return None


def resource_of(entry: JSONObject) -> JSONObject:
    found = entry["resource"]
    assert isinstance(found, dict)
    return found


def all_requests(prepared: Prepared | Scenario) -> list[JSONObject]:
    return [*prepared.metrics, *prepared.logs, *prepared.traces]


def unix_nanos(value: JSON) -> list[tuple[str, int]]:
    """Every ``*UnixNano`` field under ``value``, as (field, integer), in document order."""
    found: list[tuple[str, int]] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key.endswith("UnixNano"):
                assert isinstance(inner, str), f"{key} must stay a string"
                found.append((key, int(inner)))
            else:
                found.extend(unix_nanos(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(unix_nanos(inner))
    return found


def request_nanos(requests: Iterable[JSONObject]) -> list[tuple[str, int]]:
    return [found for request in requests for found in unix_nanos(request)]


def event_timestamps(requests: Iterable[JSONObject]) -> list[str]:
    stamps = [string_attr(record, "event.timestamp") for record in items(requests, "logs")]
    return [stamp for stamp in stamps if stamp is not None]


def dumps(requests: Iterable[JSONObject]) -> str:
    return json.dumps(list(requests))


def id_values(requests: Iterable[JSONObject]) -> set[str]:
    """Every traceId, spanId and parentSpanId value under the requests."""
    found: set[str] = set()

    def walk(value: JSON) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ("traceId", "spanId", "parentSpanId") and isinstance(inner, str):
                    found.add(inner)
                else:
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    for request in requests:
        walk(request)
    return found


def full_series(requests: Iterable[JSONObject]) -> dict[tuple[str, str, str], list[JSONObject]]:
    """Data points by full identity: metric name, resource and point attributes."""
    series: dict[tuple[str, str, str], list[JSONObject]] = defaultdict(list)
    for entry in resource_entries(requests, "metrics"):
        own = json.dumps(sorted(attributes(resource_of(entry)).items(), key=str))
        for name, _, point in data_points([{"resourceMetrics": [entry]}]):
            labels = json.dumps(sorted(attributes(point).items(), key=str))
            series[name, own, labels].append(point)
    return series


def value_of(point: JSONObject) -> int | float:
    if "asInt" in point:
        raw = point["asInt"]
        assert isinstance(raw, str), "asInt must stay a string"
        return int(raw)
    raw = point["asDouble"]
    assert isinstance(raw, int | float)
    assert not isinstance(raw, bool)
    return raw


# Loading.


def test_scenario_names_come_from_the_manifest() -> None:
    manifest = json.loads((DEFAULT_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    listed = [scenario["name"] for scenario in manifest["scenarios"]]

    assert scenario_names() == listed
    with_otlp = [name for name in listed if (DEFAULT_FIXTURES / "otlp" / name).is_dir()]
    assert scenario_names(with_otlp=True) == with_otlp
    assert len(with_otlp) < len(listed)


@pytest.mark.parametrize("name", NAMES)
def test_every_manifest_scenario_loads_and_prepares(name: str) -> None:
    scenario = load_scenario(name)
    folder = DEFAULT_FIXTURES / "otlp" / name

    prepared = prepare(scenario, now_ns=NOW)

    assert scenario.name == prepared.scenario == name
    for signal in SIGNALS:
        path = folder / f"{signal}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        assert len(scenario.requests(signal)) == len(lines)
        assert len(prepared.requests(signal)) == len(lines)
    if name in OTLP_NAMES:
        assert prepared.expected
    else:
        assert all_requests(prepared) == []
        assert prepared.expected == {}


def test_unknown_scenario_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        load_scenario("no-such-scenario")


# Time shift.


def test_time_shift_moves_the_latest_timestamp_to_now_and_keeps_the_spacing() -> None:
    scenario = synthetic()
    before = request_nanos(all_requests(scenario))

    after = request_nanos(all_requests(prepare(scenario, now_ns=NOW)))

    assert max(value for _, value in after) == NOW
    assert [key for key, _ in after] == [key for key, _ in before]
    shifts = {new - old for (_, old), (_, new) in zip(before, after, strict=True) if old}
    assert shifts == {NOW - LATEST}
    # The zero observed time of the startup record stays zero.
    assert ("observedTimeUnixNano", 0) in after


def test_time_shift_moves_event_timestamps_by_the_same_delta() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    # 950.123456789 s later, truncated to the original millisecond precision.
    assert event_timestamps(prepared.logs) == [
        "2023-11-14T22:29:11.123Z",
        "2023-11-14T22:29:15.123Z",
        "2023-11-14T22:29:55.123Z",
    ]


def test_shift_iso_timestamp_keeps_the_precision_and_suffix() -> None:
    assert shift_iso_timestamp("2023-11-14T22:13:20.000Z", 1_500_000) == "2023-11-14T22:13:20.001Z"
    assert shift_iso_timestamp("2023-11-14T22:13:20.123456Z", 1_000) == (
        "2023-11-14T22:13:20.123457Z"
    )
    assert shift_iso_timestamp("2023-11-14T22:13:20Z", 3 * SECOND) == "2023-11-14T22:13:23Z"
    assert shift_iso_timestamp("2023-11-14T23:59:59.999Z", 2_000_000) == (
        "2023-11-15T00:00:00.001Z"
    )
    assert shift_iso_timestamp("2023-11-14T22:13:20.000+02:00", -SECOND) == (
        "2023-11-14T22:13:19.000+02:00"
    )
    with pytest.raises(ValueError, match="ISO 8601"):
        shift_iso_timestamp("yesterday", SECOND)


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixtures_shift_every_signal_together(name: str) -> None:
    scenario = load_scenario(name)
    before = request_nanos(all_requests(scenario))
    stamps = event_timestamps(scenario.logs)

    prepared = prepare(scenario, now_ns=NOW)
    after = request_nanos(all_requests(prepared))

    delta = NOW - max(value for _, value in before)
    assert max(value for _, value in after) == NOW
    assert {new - old for (_, old), (_, new) in zip(before, after, strict=True)} == {delta}
    shifted = event_timestamps(prepared.logs)
    assert len(shifted) == len(stamps)
    for old, new in zip(stamps, shifted, strict=True):
        assert new == shift_iso_timestamp(old, delta)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", new)


# Session ID.


def test_session_id_is_replaced_everywhere_with_a_fresh_uuid4() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    assert uuid.UUID(prepared.session_id).version == 4
    assert prepared.session_id != OLD_SESSION
    assert OLD_SESSION not in dumps(all_requests(prepared))
    owners = [
        *(point for _, _, point in data_points(prepared.metrics)),
        *items(prepared.logs, "logs"),
        *items(prepared.traces, "traces"),
    ]
    assert {string_attr(owner, "session.id") for owner in owners} == {prepared.session_id}


def test_each_run_gets_its_own_session_id() -> None:
    scenario = synthetic()

    first, second = prepare(scenario, now_ns=NOW), prepare(scenario, now_ns=NOW)

    assert first.session_id != second.session_id


def test_explicit_session_id_is_honoured() -> None:
    chosen = str(uuid.uuid4())

    prepared = prepare(synthetic(), now_ns=NOW, session_id=chosen)

    assert prepared.session_id == chosen
    text = dumps(all_requests(prepared))
    assert OLD_SESSION not in text
    assert text.count(chosen) == 8 + 3 + 2  # data points, log records, spans


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixtures_lose_their_session_id_and_are_not_mutated(name: str) -> None:
    scenario = load_scenario(name)
    original = dumps(all_requests(scenario))

    prepared = prepare(scenario, now_ns=NOW, temporality="cumulative", canaries=True)

    assert dumps(all_requests(scenario)) == original
    text = dumps(all_requests(prepared))
    assert scenario.session_id not in text
    assert prepared.session_id in text


def test_synthetic_scenario_is_not_mutated() -> None:
    scenario = synthetic()
    original = dumps(all_requests(scenario))

    prepare(scenario, now_ns=NOW, temporality="cumulative", canaries=True)

    assert dumps(all_requests(scenario)) == original


# Trace and span IDs.


def test_trace_and_span_ids_are_rewritten_consistently() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)
    child, root = items(prepared.traces, "traces")
    (linked,) = (record for record in items(prepared.logs, "logs") if "traceId" in record)
    unlinked = next(items(prepared.logs, "logs"))

    assert root["parentSpanId"] == ""
    assert child["parentSpanId"] == root["spanId"]
    assert child["traceId"] == root["traceId"]
    assert linked["traceId"] == child["traceId"]
    assert linked["spanId"] == child["spanId"]
    assert "traceId" not in unlinked
    assert "spanId" not in unlinked
    (link,) = objects(child["links"])
    new_ids = [root["traceId"], root["spanId"], child["spanId"], link["traceId"], link["spanId"]]
    originals = (TRACE_MAIN, SPAN_ROOT, SPAN_CHILD, TRACE_LINKED, SPAN_LINKED)
    for new, old in zip(new_ids, originals, strict=True):
        assert isinstance(new, str)
        assert re.fullmatch(f"[0-9a-f]{{{len(old)}}}", new)
        assert new != old
    assert link["traceId"] != root["traceId"]
    text = dumps(all_requests(prepared))
    assert not [old for old in ORIGINAL_IDS if old in text]


def test_each_run_gets_fresh_trace_ids() -> None:
    scenario = synthetic()

    first, second = prepare(scenario, now_ns=NOW), prepare(scenario, now_ns=NOW)

    assert id_values(first.traces) - {""}
    assert not (id_values(first.traces) - {""}) & id_values(second.traces)


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixtures_keep_their_links_with_new_ids(name: str) -> None:
    scenario = load_scenario(name)

    prepared = prepare(scenario, now_ns=NOW)

    def links(source: Prepared | Scenario) -> tuple[int, int]:
        span_ids = {item["spanId"] for item in items(source.traces, "traces")}
        parents = sum(
            1 for item in items(source.traces, "traces") if item.get("parentSpanId") in span_ids
        )
        records = sum(1 for item in items(source.logs, "logs") if item.get("spanId") in span_ids)
        return parents, records

    assert links(prepared) == links(scenario)
    assert links(scenario) != (0, 0)
    originals = id_values(all_requests(scenario)) - {""}
    assert originals
    assert not originals & id_values(all_requests(prepared))
    text = dumps(all_requests(prepared))
    assert not [old for old in originals if old in text]


def test_a_custom_random_source_makes_the_run_reproducible() -> None:
    scenario = synthetic()

    first = prepare(scenario, now_ns=NOW, canaries=True, rng=CountingBits())
    second = prepare(scenario, now_ns=NOW, canaries=True, rng=CountingBits())

    assert first == second
    assert dumps(all_requests(first)) == dumps(all_requests(second))


# Expected totals and temporality.


def test_expected_totals_sum_each_delta_series_even_with_gaps() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)
    sid = prepared.session_id
    tokens = "claude_code.token.usage"
    base = {"session.id": sid, "model": "m"}

    assert prepared.expected == {
        SeriesKey.of(tokens, {**base, "type": "input"}): 14,
        SeriesKey.of(tokens, {**base, "type": "output"}): 3,
        SeriesKey.of("claude_code.lines_of_code.count", {"session.id": sid, "type": "added"}): 7,
        SeriesKey.of("claude_code.cost.usage", base): 0.75,
    }
    assert prepared.expected_total(tokens, {"type": "input"}) == 14
    assert prepared.expected_total(tokens, {"session.id": sid}) == 17
    assert prepared.expected_total(tokens) == 17
    assert set(prepared.expected_series(tokens, {"type": "output"})) == {
        SeriesKey.of(tokens, {**base, "type": "output"})
    }
    with pytest.raises(KeyError):
        prepared.expected_total(tokens, {"type": "cacheRead"})


def test_delta_mode_leaves_the_points_as_they_were() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    assert prepared.temporality == "delta"
    values = [value_of(point) for _, _, point in data_points(prepared.metrics)]
    assert values == [5, 3, 3, 7, 0.25, 2, 4, 0.5]
    assert {data["aggregationTemporality"] for _, data, _ in data_points(prepared.metrics)} == {1}


def test_cumulative_mode_turns_each_series_into_running_totals() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, temporality="cumulative")
    shift = NOW - LATEST
    points = list(data_points(prepared.metrics))

    assert prepared.temporality == "cumulative"
    assert {data["aggregationTemporality"] for _, data, _ in points} == {2}
    assert {data["isMonotonic"] for _, data, _ in points} == {True}
    by_metric: dict[str, list[tuple[str | None, JSON, JSON, JSON]]] = defaultdict(list)
    for name, _, point in points:
        value = point.get("asInt", point.get("asDouble"))
        start, end = point["startTimeUnixNano"], point["timeUnixNano"]
        by_metric[name].append((string_attr(point, "type"), value, start, end))
    start = str(T0 + shift)
    assert by_metric["claude_code.token.usage"] == [
        ("input", 5, start, str(T0 + 10 * SECOND + shift)),
        ("output", 3, start, str(T0 + 10 * SECOND + shift)),
        ("input", 12, start, str(T0 + 30 * SECOND + shift)),
        ("input", 14, start, str(T0 + 40 * SECOND + shift)),
    ]
    assert by_metric["claude_code.lines_of_code.count"] == [
        ("added", "3", start, str(T0 + 10 * SECOND + shift)),
        ("added", "7", start, str(T0 + 40 * SECOND + shift)),
    ]
    assert by_metric["claude_code.cost.usage"] == [
        (None, 0.25, start, str(T0 + 30 * SECOND + shift)),
        (None, 0.75, start, str(T0 + 40 * SECOND + shift)),
    ]
    assert [type(value) for _, value, _, _ in by_metric["claude_code.token.usage"]] == [int] * 4
    # The expected totals do not depend on the temporality.
    assert (
        prepared.expected
        == prepare(synthetic(), now_ns=NOW, session_id=prepared.session_id).expected
    )


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixtures_end_each_cumulative_series_at_its_expected_total(name: str) -> None:
    prepared = prepare(load_scenario(name), now_ns=NOW, temporality="cumulative")

    series = full_series(prepared.metrics)
    assert len(series) == len(prepared.expected)
    for (metric, _, _), points in series.items():
        ordered = sorted(points, key=lambda point: int(str(point["timeUnixNano"])))
        last = ordered[-1]
        labels = {
            key: label
            for key in attributes(last)
            if not is_identity_key(key) and (label := string_attr(last, key)) is not None
        }
        assert value_of(last) == pytest.approx(prepared.expected[SeriesKey.of(metric, labels)])
        assert len({point["startTimeUnixNano"] for point in points}) == 1
        values = [value_of(point) for point in ordered]
        assert values == sorted(values)


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixture_totals_match_a_plain_sum(name: str) -> None:
    scenario = load_scenario(name)
    by_metric: defaultdict[str, float] = defaultdict(float)
    tokens: Counter[str] = Counter()
    for metric, _, point in data_points(scenario.metrics):
        by_metric[metric] += value_of(point)
        if metric == "claude_code.token.usage":
            tokens[string_attr(point, "type") or ""] += int(value_of(point))

    prepared = prepare(scenario, now_ns=NOW)

    assert by_metric
    for metric, total in by_metric.items():
        assert prepared.expected_total(metric) == pytest.approx(total)
    for kind, count in tokens.items():
        labels = {"session.id": prepared.session_id, "type": kind}
        assert prepared.expected_total("claude_code.token.usage", labels) == count


def merged(*parts: Mapping[SeriesKey, int | float]) -> dict[SeriesKey, int | float]:
    totals: dict[SeriesKey, int | float] = {}
    for part in parts:
        for key, value in part.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def test_expected_totals_of_request_subsets_add_up_to_the_whole() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, canaries=True)
    tokens = SeriesKey.of(
        "claude_code.token.usage",
        {"session.id": prepared.session_id, "model": "m", "type": "input"},
    )

    assert expected_totals(prepared.metrics) == prepared.expected
    head, tail = expected_totals(prepared.metrics[:2]), expected_totals(prepared.metrics[2:])
    assert (head[tokens], tail[tokens]) == (12, 2)
    for split in range(len(prepared.metrics) + 1):
        parts = expected_totals(prepared.metrics[:split]), expected_totals(prepared.metrics[split:])
        assert merged(*parts) == pytest.approx(dict(prepared.expected))
    assert expected_totals([]) == {}


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixture_halves_add_up_to_the_expected_totals(name: str) -> None:
    prepared = prepare(load_scenario(name), now_ns=NOW, canaries=True)
    half = len(prepared.metrics) // 2

    head, tail = expected_totals(prepared.metrics[:half]), expected_totals(prepared.metrics[half:])

    assert expected_totals(prepared.metrics) == prepared.expected
    assert merged(head, tail) == pytest.approx(dict(prepared.expected))
    for key in (*head, *tail):
        assert key.label("workspace.host_paths") is None
        assert CANARY_PREFIX not in json.dumps(key.labels)


def test_expected_totals_refuse_cumulative_sums() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, temporality="cumulative")

    with pytest.raises(ValueError, match="cumulative") as error:
        expected_totals(prepared.metrics[1:])

    assert "last value" in str(error.value)


def test_contiguous_series_are_those_that_go_on_with_no_gap() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)
    sid = prepared.session_id
    tokens = SeriesKey.of(
        "claude_code.token.usage", {"session.id": sid, "model": "m", "type": "input"}
    )
    lines = SeriesKey.of("claude_code.lines_of_code.count", {"session.id": sid, "type": "added"})
    cost = SeriesKey.of("claude_code.cost.usage", {"session.id": sid, "model": "m"})
    metrics = prepared.metrics

    # After the first request, the input tokens go on 10 s later (a gap), the lines go on
    # at once, and the output tokens do not go on at all.
    assert contiguous_series(metrics[:1], metrics[1:]) == (lines,)
    # After the second, every continuing series goes on at once.
    assert contiguous_series(metrics[:2], metrics[2:]) == tuple(sorted((tokens, lines, cost)))
    assert contiguous_series([], metrics) == ()
    assert contiguous_series(metrics, []) == ()


def test_contiguous_series_compare_the_latest_point_before_with_the_earliest_after() -> None:
    labels = {"type": "input"}
    # Out of time order in both parts: the latest point before ends at 20 s, and the
    # earliest point after starts at 20 s.
    before = [
        metrics_request(sum_metric("m", data_point(10, 20, 2, labels))),
        metrics_request(sum_metric("m", data_point(0, 10, 1, labels))),
    ]
    after = [
        metrics_request(sum_metric("m", data_point(30, 40, 4, labels))),
        metrics_request(sum_metric("m", data_point(20, 30, 3, labels))),
    ]

    (key,) = contiguous_series(before, after)

    assert key.label("type") == "input"
    assert "user.email" not in key.label_dict()
    assert contiguous_series(before[1:], after) == ()
    assert contiguous_series(before, after[:1]) == ()


def test_contiguous_series_refuse_cumulative_sums() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, temporality="cumulative")

    with pytest.raises(ValueError, match="cumulative") as error:
        contiguous_series(prepared.metrics[:1], prepared.metrics[1:])

    assert "start time" in str(error.value)


@pytest.mark.parametrize(
    ("name", "split", "contiguous"),
    [("interactive-permission-compact", 5, 0), ("headless-edit-commit-tasks", 1, 7)],
)
def test_real_fixture_restart_splits(name: str, split: int, contiguous: int) -> None:
    # The splits of the integration tests' collector restarts (ADR 0013): one where no
    # continuing series is contiguous, one where every continuing series is.
    prepared = prepare(load_scenario(name), now_ns=NOW, canaries=True)
    first, rest = prepared.metrics[:split], prepared.metrics[split:]

    found = contiguous_series(first, rest)

    continuing = set(expected_totals(first)) & set(expected_totals(rest))
    assert len(found) == contiguous
    assert set(found) <= continuing
    assert continuing


def test_expected_totals_skip_metrics_that_are_not_sums() -> None:
    gauge: JSONObject = {
        "name": "claude_code.some.gauge",
        "gauge": {"dataPoints": [data_point(0, 1, 3, {"type": "x"})]},
    }

    assert expected_totals([metrics_request(gauge)]) == {}


def test_expected_total_keys_leave_out_identity_attributes() -> None:
    labels = {
        "type": "input",
        "user.groups": "group",
        "identity.source": "idp",
        "host.name": "box",
        "enduser.id": "someone",
        "enduser.role": "admin",
    }
    scenario = Scenario(
        name="identity",
        session_id=OLD_SESSION,
        metrics=(metrics_request(sum_metric("m", data_point(0, 1, 2, labels))),),
        logs=(),
        traces=(),
    )

    prepared = prepare(scenario, now_ns=NOW)

    (key,) = prepared.expected
    assert key.label_dict() == {"session.id": prepared.session_id, "type": "input"}
    assert key.label("type") == "input"
    assert key.label("user.email") is None
    assert key.matches("m", {"type": "input"})
    assert not key.matches("m", {"type": "output"})
    assert not key.matches("other", {})


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixture_totals_carry_no_identity_labels(name: str) -> None:
    prepared = prepare(load_scenario(name), now_ns=NOW, canaries=True)

    for key in prepared.expected:
        assert not [label for label, _ in key.labels if is_identity_key(label)]
        assert key.label("session.id") == prepared.session_id
        assert CANARY_PREFIX not in json.dumps(key.labels)


def test_identity_keys() -> None:
    for key in (*IDENTITY_CANARY_KEYS, *IDENTITY_FAKES, "user.anything", "enduser.scope"):
        assert is_identity_key(key)
    for key in ("session.id", "type", "model", "user_prompt", "terminal.type", "host.arch"):
        assert not is_identity_key(key)


def test_series_keys_sort_their_labels() -> None:
    key = SeriesKey.of("m", {"b": "2", "a": "1"})

    assert key == SeriesKey("m", (("a", "1"), ("b", "2")))


# Canaries.


def canary_check(value: str | None, marker: str) -> bool:
    return value is not None and marker in value


@pytest.mark.parametrize("name", [*OTLP_NAMES, "synthetic"])
def test_canaries_reach_every_level(name: str) -> None:
    scenario = synthetic() if name == "synthetic" else load_scenario(name)

    prepared = prepare(scenario, now_ns=NOW, canaries=True)

    marker = prepared.canary_marker
    assert marker is not None
    assert re.fullmatch(f"{CANARY_PREFIX}[0-9a-f]{{16}}", marker)
    for signal in SIGNALS:
        for entry in resource_entries(prepared.requests(signal), signal):
            for key in RESOURCE_CANARY_KEYS:
                assert canary_check(string_attr(resource_of(entry), key), marker), (signal, key)
    point_values: dict[str, set[str | None]] = defaultdict(set)
    for _, _, point in data_points(prepared.metrics):
        for key in DATA_POINT_CANARY_KEYS:
            assert canary_check(string_attr(point, key), marker), key
            point_values[key].add(string_attr(point, key))
    assert all(len(values) == 1 for values in point_values.values())
    for record in items(prepared.logs, "logs"):
        for key in LOG_RECORD_CANARY_KEYS:
            assert canary_check(string_attr(record, key), marker), key
    spans = list(items(prepared.traces, "traces"))
    tool_outputs = kept_events = 0
    for item in spans:
        for key in SPAN_CANARY_KEYS:
            assert canary_check(string_attr(item, key), marker), key
        for event in objects(item.get("events", [])):
            if event["name"] == CONTENT_SPAN_EVENT:
                assert set(attributes(event)) == set(CONTENT_SPAN_EVENT_CANARY_KEYS)
                values = [string_attr(event, key) for key in attributes(event)]
                assert all(canary_check(value, marker) for value in values)
                tool_outputs += 1
            else:
                # The span events Claude Code sends, which the collector keeps.
                for key in SPAN_EVENT_CANARY_KEYS:
                    assert canary_check(string_attr(event, key), marker), key
                kept_events += 1
    assert tool_outputs == len(spans) >= 1
    # Every scenario has gen_ai.request.attempt events; the synthetic one has one.
    assert kept_events >= 1


@pytest.mark.parametrize("name", [*OTLP_NAMES, "synthetic"])
def test_canaries_add_the_content_events(name: str) -> None:
    scenario = synthetic() if name == "synthetic" else load_scenario(name)

    prepared = prepare(scenario, now_ns=NOW, canaries=True)

    marker = prepared.canary_marker
    assert marker is not None
    times = [value for _, value in request_nanos(all_requests(prepared)) if value]
    records = {string_attr(record, "event.name"): record for record in items(prepared.logs, "logs")}
    plain = prepare(scenario, now_ns=NOW)
    assert len(list(items(prepared.logs, "logs"))) == (
        len(list(items(plain.logs, "logs"))) + len(CONTENT_EVENT_NAMES)
    )
    for event in CONTENT_EVENT_NAMES:
        record = records[event]
        assert record["body"] == {"stringValue": f"claude_code.{event}"}
        assert string_attr(record, "session.id") == prepared.session_id
        for field in ("timeUnixNano", "observedTimeUnixNano"):
            value = record[field]
            assert isinstance(value, str)
            assert min(times) <= int(value) <= NOW
        stamp = string_attr(record, "event.timestamp")
        assert stamp is not None
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp)


def test_canary_key_lists_cover_every_level() -> None:
    host_paths = "workspace.host_paths"

    assert set(RESOURCE_CANARY_KEYS) == {*IDENTITY_CANARY_KEYS, host_paths}
    assert set(DATA_POINT_CANARY_KEYS) == {*IDENTITY_CANARY_KEYS, host_paths}
    assert set(LOG_RECORD_CANARY_KEYS) == {*IDENTITY_CANARY_KEYS, *LOG_CONTENT_CANARY_KEYS}
    assert set(SPAN_CANARY_KEYS) == {*IDENTITY_CANARY_KEYS, *SPAN_CONTENT_CANARY_KEYS, host_paths}
    # On span events the collector deletes identity and host paths; their other content is
    # the tool.output event, dropped whole (ADR 0021).
    assert set(SPAN_EVENT_CANARY_KEYS) == {*IDENTITY_CANARY_KEYS, host_paths}
    assert CONTENT_SPAN_EVENT_CANARY_KEYS == ("output",)
    assert host_paths in LOG_CONTENT_CANARY_KEYS


def test_span_event_canaries_go_on_each_event_sent_and_the_tool_output_event() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, canaries=True)
    marker = prepared.canary_marker
    assert marker is not None
    child, root = items(prepared.traces, "traces")

    attempt, output = objects(child["events"])
    (root_output,) = objects(root["events"])

    assert attempt["name"] == "gen_ai.request.attempt"
    assert attempt["timeUnixNano"] == str(T0 + 6 * SECOND + NOW - LATEST)
    assert {key: string_attr(attempt, key) for key in attributes(attempt)} == {
        key: f"{marker}:traces.span0.event0:{key}" for key in SPAN_EVENT_CANARY_KEYS
    }
    for number, event in ((0, output), (1, root_output)):
        assert event["name"] == CONTENT_SPAN_EVENT
        assert {key: string_attr(event, key) for key in attributes(event)} == {
            key: f"{marker}:traces.span{number}.{CONTENT_SPAN_EVENT}:{key}"
            for key in CONTENT_SPAN_EVENT_CANARY_KEYS
        }
    assert f"{marker}:traces.span0.event0:user.email" in prepared.canary_values


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_real_fixture_span_events_keep_their_attributes_next_to_the_canaries(name: str) -> None:
    scenario = load_scenario(name)

    plain = prepare(scenario, now_ns=NOW, rng=CountingBits())
    marked = prepare(scenario, now_ns=NOW, canaries=True, rng=CountingBits())

    marker = marked.canary_marker
    assert marker is not None
    pairs = list(zip(items(plain.traces, "traces"), items(marked.traces, "traces"), strict=True))
    sent = 0
    for number, (before, after) in enumerate(pairs):
        events = objects(before.get("events", []))
        *kept, output = objects(after["events"])
        assert output["name"] == CONTENT_SPAN_EVENT
        assert [event["name"] for event in kept] == [event["name"] for event in events]
        for index, (old, new) in enumerate(zip(events, kept, strict=True)):
            assert {key: attributes(new)[key] for key in attributes(old)} == attributes(old)
            assert set(attributes(new)) - set(attributes(old)) == set(SPAN_EVENT_CANARY_KEYS)
            place = f"traces.span{number}.event{index}"
            assert string_attr(new, "user.id") == f"{marker}:{place}:user.id"
            sent += 1
    # Every scenario has gen_ai.request.attempt span events, so at least two were seen.
    assert sent >= 2


def test_content_canaries_include_the_names_only_the_monitoring_docs_give() -> None:
    # hook_definitions on hook_execution_start/complete events, and full_command and
    # file_path on claude_code.tool spans, all set by OTEL_LOG_TOOL_DETAILS (ADR 0021).
    assert "hook_definitions" in LOG_CONTENT_CANARY_KEYS
    assert {"full_command", "file_path", "hook_definitions"} <= set(SPAN_CONTENT_CANARY_KEYS)
    metadata = {"skill_name", "subagent_type", "workflow.name", "vcs.repository.name"}
    metadata |= {"managed_settings.helper.path"}
    assert not metadata & {*LOG_CONTENT_CANARY_KEYS, *SPAN_CONTENT_CANARY_KEYS}


def test_host_path_canaries_reach_every_level() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, canaries=True)
    marker = prepared.canary_marker
    assert marker is not None
    key = "workspace.host_paths"
    resources = [
        resource_of(entry)
        for signal in SIGNALS
        for entry in resource_entries(prepared.requests(signal), signal)
    ]
    points = [point for _, _, point in data_points(prepared.metrics)]
    spans = list(items(prepared.traces, "traces"))
    events = [
        event
        for span in spans
        for event in objects(span.get("events", []))
        if event["name"] != CONTENT_SPAN_EVENT
    ]
    owners = [*resources, *points, *items(prepared.logs, "logs"), *spans, *events]

    assert len(resources) == 7
    assert len(events) == 1
    assert all(canary_check(string_attr(owner, key), marker) for owner in owners)
    assert {string_attr(point, key) for point in points} == {f"{marker}:metrics.datapoint:{key}"}
    assert f"{marker}:traces.span0:{key}" in prepared.canary_values
    assert f"{marker}:traces.span0.event0:{key}" in prepared.canary_values


def test_canary_values_are_recorded_and_name_their_place() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, canaries=True)
    marker = prepared.canary_marker
    assert marker is not None

    found: set[str] = set()

    def walk(value: JSON) -> None:
        if isinstance(value, str) and marker in value:
            found.add(value)
        elif isinstance(value, dict):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    for request in all_requests(prepared):
        walk(request)
    assert found == prepared.canary_values
    assert all(value.startswith(marker) for value in found)
    # Log records and spans get one value per item and key.
    record_values = {string_attr(record, "prompt") for record in items(prepared.logs, "logs")}
    assert len(record_values) == len(list(items(prepared.logs, "logs")))
    span_values = {string_attr(item, "tool_input") for item in items(prepared.traces, "traces")}
    assert len(span_values) == len(list(items(prepared.traces, "traces")))


def test_each_run_gets_its_own_canary_marker() -> None:
    scenario = synthetic()

    first = prepare(scenario, now_ns=NOW, canaries=True)
    second = prepare(scenario, now_ns=NOW, canaries=True)

    assert first.canary_marker != second.canary_marker


def test_without_canaries_there_is_no_marker() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    assert prepared.canary_marker is None
    assert prepared.canary_values == frozenset()
    assert CANARY_PREFIX not in dumps(all_requests(prepared))


@pytest.mark.parametrize("name", OTLP_NAMES)
def test_data_point_canaries_do_not_split_series(name: str) -> None:
    scenario = load_scenario(name)
    sid = str(uuid.uuid4())

    plain = prepare(scenario, now_ns=NOW, session_id=sid, temporality="cumulative")
    marked = prepare(scenario, now_ns=NOW, session_id=sid, temporality="cumulative", canaries=True)

    assert len(full_series(marked.metrics)) == len(full_series(plain.metrics))
    assert marked.expected == plain.expected
    plain_values = [value_of(point) for _, _, point in data_points(plain.metrics)]
    assert [value_of(point) for _, _, point in data_points(marked.metrics)] == plain_values


def test_canaries_without_logs_add_no_records() -> None:
    scenario = synthetic()
    metrics_only = Scenario(scenario.name, scenario.session_id, metrics=scenario.metrics)

    prepared = prepare(metrics_only, now_ns=NOW, canaries=True)

    assert prepared.canary_marker is not None
    assert prepared.logs == ()
    assert prepared.traces == ()
    places = {value.split(":")[1] for value in prepared.canary_values}
    assert places == {"metrics.resource", "metrics.datapoint"}


def test_canaries_on_a_hooks_only_scenario_plant_nothing() -> None:
    prepared = prepare(
        load_scenario("headless-sessionstart-http-ignored"), now_ns=NOW, canaries=True
    )

    assert prepared.canary_marker is not None
    assert prepared.canary_values == frozenset()
    assert all_requests(prepared) == []


# Hooks.

HOOK_NAMES: Final = scenario_names(with_hooks=True)


def hook_line(event: str, offset_ms: int = 0, **fields: JSON) -> JSONObject:
    payload: JSONObject = {
        "session_id": OLD_SESSION,
        "hook_event_name": event,
        "transcript_path": f"/home/user/.claude/projects/p/{OLD_SESSION}.jsonl",
        "cwd": "/home/user/project",
        **fields,
    }
    return {"offset_ms": offset_ms, "transport": "http", "payload": payload}


def hook_scenario(*lines: JSONObject) -> Scenario:
    return Scenario("synthetic-hooks", OLD_SESSION, hooks=lines)


def reduce(lines: Iterable[HookLine]) -> tuple[SessionState | None, list[Effect]]:
    """The reducer's replay of ``lines``, as the app would fold them (ADR 0024)."""
    state: SessionState | None = None
    effects: list[Effect] = []
    for seq, line in enumerate(lines, start=1):
        state, changes = apply(state, parse_hook(line.payload), 1_000 + line.offset_ms, seq)
        effects.extend(changes)
    return state, effects


def test_scenario_names_can_ask_for_hooks() -> None:
    manifest = json.loads((DEFAULT_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    with_hooks = [
        entry["name"]
        for entry in manifest["scenarios"]
        if f"hooks/{entry['name']}.jsonl" in entry["files"]
    ]

    assert with_hooks == HOOK_NAMES
    assert "headless-sessionstart-http-ignored" in HOOK_NAMES
    assert "hooks-endpoint-down" not in HOOK_NAMES
    # Either kind, in manifest order.
    assert scenario_names(with_otlp=True, with_hooks=True) == [
        name for name in NAMES if name in HOOK_NAMES or name in OTLP_NAMES
    ]
    assert set(scenario_names(with_otlp=True, with_hooks=True)) == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_load_scenario_reads_the_hook_lines_in_file_order(name: str) -> None:
    scenario = load_scenario(name)
    path = DEFAULT_FIXTURES / "hooks" / f"{name}.jsonl"

    if name in HOOK_NAMES:
        lines = path.read_text(encoding="utf-8").splitlines()
        assert list(scenario.hooks) == [json.loads(line) for line in lines]
    else:
        assert scenario.hooks == ()


def test_a_hook_line_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "bad.jsonl").write_text('{"offset_ms": 0}\n\n[1]\n', encoding="utf-8")
    manifest = {
        "scenarios": [{"name": "bad", "session_id": OLD_SESSION, "files": ["hooks/bad.jsonl"]}]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=r"bad\.jsonl:3: expected a hook line object"):
        load_scenario("bad", tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        {"offset_ms": "0", "transport": "http", "payload": {}},
        {"offset_ms": True, "transport": "http", "payload": {}},
        {"offset_ms": 0, "transport": None, "payload": {}},
        {"offset_ms": 0, "transport": "http", "payload": []},
    ],
)
def test_prepare_hooks_refuses_a_malformed_line(line: JSONObject) -> None:
    with pytest.raises(ValueError, match="synthetic-hooks hook line 2"):
        prepare_hooks(hook_scenario(hook_line("Stop"), line))


def test_fresh_session_ids_are_uuid4() -> None:
    first, second = fresh_session_id(), fresh_session_id()

    assert first != second
    assert uuid.UUID(first).version == 4
    assert fresh_session_id(CountingBits()) == fresh_session_id(CountingBits())


def test_prepare_hooks_replaces_the_session_id_in_every_string() -> None:
    scenario = hook_scenario(
        hook_line("SessionStart", 0, source="startup"),
        hook_line("Stop", 1_500, nested={"items": [OLD_SESSION, 7, None]}),
    )
    original = json.dumps(scenario.hooks)

    prepared = prepare_hooks(scenario, session_id=OTHER_SESSION)

    assert json.dumps(scenario.hooks) == original
    assert prepared.session_id == OTHER_SESSION
    assert OLD_SESSION not in json.dumps([line.payload for line in prepared.lines])
    first, second = prepared.lines
    assert (first.offset_ms, first.transport) == (0, "http")
    assert second.offset_ms == 1_500
    assert first.payload["session_id"] == OTHER_SESSION
    path = first.payload["transcript_path"]
    assert path == f"/home/user/.claude/projects/p/{OTHER_SESSION}.jsonl"
    assert second.payload["nested"] == {"items": [OTHER_SESSION, 7, None]}
    assert prepared.canary_marker is None
    assert prepared.canary_values == frozenset()


def test_prepare_hooks_defaults_to_a_fresh_session_id() -> None:
    scenario = hook_scenario(hook_line("Stop"))

    first, second = prepare_hooks(scenario), prepare_hooks(scenario)

    assert first.session_id != second.session_id
    assert uuid.UUID(first.session_id).version == 4
    again = prepare_hooks(scenario, rng=CountingBits())
    assert prepare_hooks(scenario, rng=CountingBits()) == again


def test_prepare_hooks_without_an_original_session_id_only_sets_the_field() -> None:
    original: JSONObject = {"hook_event_name": "Stop", "note": "kept as is"}
    item: JSONObject = {"offset_ms": 0, "transport": "http", "payload": original}
    scenario = Scenario("no-id", "", hooks=(item,))

    (line,) = prepare_hooks(scenario, session_id=OTHER_SESSION).lines

    assert line.payload == {**original, "session_id": OTHER_SESSION}
    assert original == {"hook_event_name": "Stop", "note": "kept as is"}


def test_prepare_hooks_refuses_a_payload_of_another_session() -> None:
    stray = hook_line("Stop", 10)
    payload = stray["payload"]
    assert isinstance(payload, dict)
    payload["session_id"] = OTHER_SESSION

    with pytest.raises(ValueError, match="synthetic-hooks hook line 2: the payload's session_id"):
        prepare_hooks(hook_scenario(hook_line("UserPromptSubmit"), stray))


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hooks_and_telemetry_of_a_scenario_share_one_session_id(name: str) -> None:
    scenario = load_scenario(name)
    session_id = fresh_session_id()

    telemetry = prepare(scenario, now_ns=NOW, session_id=session_id)
    hooks = prepare_hooks(scenario, session_id=session_id)

    assert hooks.session_id == telemetry.session_id == session_id
    assert {line.payload["session_id"] for line in hooks.lines} == {session_id}
    assert scenario.session_id not in json.dumps([line.payload for line in hooks.lines])
    assert len(hooks.lines) == len(scenario.hooks)


def test_hook_canaries_go_in_every_content_field() -> None:
    scenario = hook_scenario(
        hook_line("PostToolUse", tool_name="Agent", tool_response={"status": "async_launched"}),
        hook_line("StopFailure", 10, error="model_not_found"),
        hook_line("PermissionDenied", 20, reason="denied by a rule", tool_response="plain text"),
        hook_line(
            "Stop",
            30,
            effort={"level": "high"},
            background_tasks=[{"id": "t1", "type": "shell"}, "not a task"],
        ),
    )

    prepared = prepare_hooks(scenario, canaries=True, rng=CountingBits())

    marker = prepared.canary_marker
    assert marker is not None
    assert marker.startswith(CANARY_PREFIX)
    post, failure, denied, stop = (line.payload for line in prepared.lines)
    for number, payload in enumerate((post, failure, denied, stop)):
        for key in HOOK_CONTENT_CANARY_KEYS:
            assert payload[key] == f"{marker}:hooks.line{number}:{key}"
        for key in ("tool_response", "effort", "mcp_server"):
            nested = payload[key]
            assert isinstance(nested, dict)
            assert nested[HOOK_NESTED_KEY] == f"{marker}:hooks.line{number}:{key}"
    # Metadata next to the canaries is kept.
    assert post["tool_response"] == {
        "status": "async_launched",
        HOOK_NESTED_KEY: f"{marker}:hooks.line0:tool_response",
    }
    assert stop["effort"] == {"level": "high", HOOK_NESTED_KEY: f"{marker}:hooks.line3:effort"}
    assert stop["background_tasks"] == [
        {"id": "t1", "type": "shell", HOOK_NESTED_KEY: f"{marker}:hooks.line3:background_tasks.0"},
        "not a task",
    ]
    assert "background_tasks" not in post
    # error is a code on StopFailure only; reason is free text on PermissionDenied only.
    assert failure["error"] == "model_not_found"
    assert post["error"] == f"{marker}:hooks.line0:error"
    assert denied["reason"] == f"{marker}:hooks.line2:reason"
    assert "reason" not in post
    values = {
        str(value)
        for payload in (post, failure, denied, stop)
        for value in walk_strings(payload)
        if str(value).startswith(marker)
    }
    assert values == prepared.canary_values
    nested_keys = {"tool_response", "effort", "mcp_server", "background_tasks"}
    assert set(HOOK_NESTED_CANARY_KEYS) == nested_keys


def walk_strings(node: JSON) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for value in node:
            yield from walk_strings(value)
    elif isinstance(node, dict):
        for value in node.values():
            yield from walk_strings(value)


def test_hook_canaries_can_share_the_telemetry_marker() -> None:
    telemetry = prepare(load_scenario("interactive-permission-compact"), now_ns=NOW, canaries=True)

    hooks = prepare_hooks(
        load_scenario("interactive-permission-compact"),
        session_id=telemetry.session_id,
        canaries=True,
        canary_marker=telemetry.canary_marker,
    )

    assert hooks.canary_marker == telemetry.canary_marker
    assert not hooks.canary_values & telemetry.canary_values


def test_a_canary_marker_without_canaries_plants_nothing() -> None:
    hooks = prepare_hooks(load_scenario("headless-api-error"), canary_marker="ccobs-canary-0")

    assert hooks.canary_marker is None
    assert hooks.canary_values == frozenset()
    assert "ccobs-canary-0" not in json.dumps([line.payload for line in hooks.lines])


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_canaries_change_no_state_and_reach_no_parsed_event(name: str) -> None:
    scenario = load_scenario(name)
    session_id = fresh_session_id()
    plain = prepare_hooks(scenario, session_id=session_id)
    poisoned = prepare_hooks(scenario, session_id=session_id, canaries=True)
    marker = poisoned.canary_marker
    assert marker is not None

    assert reduce(poisoned.lines) == reduce(plain.lines)
    assert poisoned.canary_values
    for line in poisoned.lines:
        event = parse_hook(line.payload)
        assert marker not in repr(event)
        assert marker not in repr(dataclasses.asdict(event))


def test_hook_summary_counts_the_lines() -> None:
    prepared = prepare_hooks(load_scenario("headless-api-error"), canaries=True)

    assert prepared.summary() == {
        "scenario": "headless-api-error",
        "session_id": prepared.session_id,
        "hook_lines": len(load_scenario("headless-api-error").hooks),
        "canary_marker": prepared.canary_marker,
        "canary_value_count": len(prepared.canary_values),
    }


# Sending.


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    content_type: str | None
    body: bytes


type Behaviour = Literal["answer", "hang", "close"]


@contextmanager
def collector(
    status: int = 200,
    reply: bytes = b"{}",
    *,
    behaviour: Behaviour = "answer",
    fail_after: int | None = None,
) -> Iterator[tuple[str, list[Received]]]:
    """A local stand-in for the collector's OTLP/HTTP receiver: (endpoint, requests).

    ``hang`` reads the request and never answers; ``close`` reads it and closes the
    connection without a response; ``fail_after`` answers 503 after that many requests."""
    received: list[Received] = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type")
            received.append(Received(self.command, self.path, content_type, body))
            if behaviour == "hang":
                release.wait(timeout=5)
                return
            if behaviour == "close":
                return
            failing = fail_after is not None and len(received) > fail_after
            self.send_response(503 if failing else status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.mark.parametrize("signal", SIGNALS)
def test_post_otlp_sends_json_to_the_signal_path(signal: Signal) -> None:
    request = prepare(synthetic(), now_ns=NOW).requests(signal)[0]

    with collector() as (endpoint, received):
        status = post_otlp(endpoint + "/", signal, request, timeout=5)

    assert status == 200
    (got,) = received
    assert (got.method, got.path, got.content_type) == ("POST", f"/v1/{signal}", "application/json")
    assert json.loads(got.body) == request


def test_post_otlp_reports_a_refusal_with_its_status_and_part_of_the_body() -> None:
    reply = b'{"code": 3, "message": "' + b"x" * 5000 + b'"}'

    with collector(status=400, reply=reply) as (endpoint, _), pytest.raises(ReplayError) as error:
        post_otlp(endpoint, "logs", {"resourceLogs": []}, timeout=5)

    message = str(error.value)
    assert "400" in message
    assert "/v1/logs" in message
    assert '"code": 3' in message
    assert len(message) < 1000


def test_post_otlp_reports_a_closed_port() -> None:
    with pytest.raises(ReplayError, match="could not reach"):
        # Windows retries a refused connection for about 2 s; a short timeout is enough.
        post_otlp(f"http://127.0.0.1:{closed_port()}", "metrics", {}, timeout=0.5)


@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [("hang", "timed out"), ("close", "Remote end closed connection without response")],
)
def test_post_otlp_reports_a_collector_that_does_not_answer(
    behaviour: Behaviour, reason: str
) -> None:
    with (
        collector(behaviour=behaviour) as (endpoint, received),
        pytest.raises(ReplayError) as error,
    ):
        post_otlp(endpoint, "metrics", {"resourceMetrics": []}, timeout=0.2)

    message = str(error.value)
    assert "/v1/metrics" in message
    assert reason in message
    assert len(received) == 1


@pytest.mark.parametrize(
    ("signal", "field", "noun"),
    [
        ("metrics", "rejectedDataPoints", "data points"),
        ("logs", "rejectedLogRecords", "log records"),
        ("traces", "rejectedSpans", "spans"),
    ],
)
def test_post_otlp_reports_a_partial_success(signal: Signal, field: str, noun: str) -> None:
    partial = {"partialSuccess": {field: "2", "errorMessage": "dropped by a test"}}

    with (
        collector(reply=json.dumps(partial).encode()) as (endpoint, _),
        pytest.raises(ReplayError) as error,
    ):
        post_otlp(endpoint, signal, {}, timeout=5)

    message = str(error.value)
    assert f"/v1/{signal}" in message
    assert f"rejected 2 {noun}" in message
    assert "dropped by a test" in message


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"{}",
        b'{"partialSuccess": {}}',
        b'{"partialSuccess": {"rejectedSpans": "0", "errorMessage": ""}}',
        b'{"partialSuccess": {"rejectedLogRecords": 0, "errorMessage": "just a warning"}}',
        b"not json",
    ],
)
def test_post_otlp_treats_a_reply_without_rejections_as_success(reply: bytes) -> None:
    with collector(reply=reply) as (endpoint, _):
        assert post_otlp(endpoint, "logs", {}, timeout=5) == 200


@pytest.mark.parametrize("endpoint", ["ftp://127.0.0.1:4318", "127.0.0.1:4318", "http://"])
def test_post_otlp_refuses_an_endpoint_that_is_not_http(endpoint: str) -> None:
    with pytest.raises(ReplayError, match="endpoint"):
        post_otlp(endpoint, "metrics", {}, timeout=5)


def test_send_posts_every_request_in_file_order() -> None:
    prepared = prepare(synthetic(), now_ns=NOW, canaries=True)

    with collector() as (endpoint, received):
        count = send(endpoint, prepared, timeout=5)

    expected = [(signal, request) for signal in SIGNALS for request in prepared.requests(signal)]
    assert count == len(expected) == 7
    assert [(got.path, json.loads(got.body)) for got in received] == [
        (f"/v1/{signal}", request) for signal, request in expected
    ]


def test_send_can_limit_the_signals() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    with collector() as (endpoint, received):
        count = send(endpoint, prepared, signals=("metrics",), timeout=5)

    assert count == 3
    assert {got.path for got in received} == {"/v1/metrics"}


def test_send_stops_at_the_first_refusal() -> None:
    prepared = prepare(synthetic(), now_ns=NOW)

    with collector(status=503) as (endpoint, received), pytest.raises(ReplayError, match="503"):
        send(endpoint, prepared, timeout=5)

    assert len(received) == 1


@contextmanager
def app(status: int = 204, reply: bytes = b"") -> Iterator[tuple[str, list[Received]]]:
    """A local stand-in for the app's hook route, answering an empty 204 by default."""
    with collector(status=status, reply=reply) as (endpoint, received):
        yield endpoint, received


def test_post_hook_sends_the_payload_as_json_to_the_hook_route() -> None:
    payload = hook_line("Stop")["payload"]
    assert isinstance(payload, dict)

    with app() as (endpoint, received):
        post_hook(endpoint + "/", payload, timeout=5)

    (got,) = received
    assert (got.method, got.path, got.content_type) == ("POST", HOOKS_PATH, "application/json")
    assert json.loads(got.body) == payload


@pytest.mark.parametrize(("status", "reply"), [(200, b""), (200, b"{}"), (500, b"boom")])
def test_post_hook_accepts_only_an_empty_204(status: int, reply: bytes) -> None:
    with app(status, reply) as (endpoint, _), pytest.raises(ReplayError) as error:
        post_hook(endpoint, {"hook_event_name": "Stop"}, timeout=5)

    message = str(error.value)
    assert f"{HOOKS_PATH} answered {status} with a {len(reply)}-byte body" in message
    assert "not an empty 204" in message


def test_post_hook_reports_a_closed_port() -> None:
    with pytest.raises(ReplayError, match="could not reach"):
        post_hook(f"http://127.0.0.1:{closed_port()}", {}, timeout=0.5)


def test_send_hooks_posts_every_line_in_file_order_back_to_back() -> None:
    prepared = prepare_hooks(load_scenario("interactive-permission-compact"), canaries=True)
    sleeps: list[float] = []

    with app() as (endpoint, received):
        durations = send_hooks(endpoint, prepared.lines, timeout=5, sleep=sleeps.append)

    assert [json.loads(got.body) for got in received] == [line.payload for line in prepared.lines]
    assert {got.path for got in received} == {HOOKS_PATH}
    assert len(durations) == len(prepared.lines)
    assert all(duration >= 0 for duration in durations)
    assert sleeps == []


class FakeClock:
    """A monotonic clock that only moves when slept on."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_send_hooks_can_keep_the_spacing_scaled_by_a_speed() -> None:
    # A part of a scenario: offsets count from the first line sent.
    scenario = hook_scenario(
        hook_line("UserPromptSubmit", 5_000),
        hook_line("PreToolUse", 6_000),
        hook_line("PostToolUse", 6_000),
        hook_line("Stop", 9_000),
    )
    clock = FakeClock()

    with app() as (endpoint, received):
        durations = send_hooks(
            endpoint,
            prepare_hooks(scenario).lines,
            speed=2.0,
            timeout=5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.sleeps == [0.5, 1.5]
    assert len(received) == 4
    assert durations == [0.0] * 4


@pytest.mark.parametrize("speed", [0.0, -1.0])
def test_send_hooks_refuses_a_speed_that_is_not_positive(speed: float) -> None:
    with pytest.raises(ValueError, match="speed must be positive"):
        send_hooks("http://127.0.0.1:1", [], speed=speed)


def test_send_hooks_stops_at_the_first_refusal() -> None:
    prepared = prepare_hooks(load_scenario("headless-api-error"))

    with app(status=500) as (endpoint, received), pytest.raises(ReplayError, match="500"):
        send_hooks(endpoint, prepared.lines, timeout=5)

    assert len(received) == 1


# Command line.


def request_counts(name: str) -> dict[str, int]:
    scenario = load_scenario(name)
    return {signal: len(scenario.requests(signal)) for signal in SIGNALS}


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, JSONObject | None, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    output = json.loads(captured.out) if captured.out else None
    assert output is None or isinstance(output, dict)
    return code, output, captured.err


def test_cli_dry_run_prints_a_json_summary(capsys: pytest.CaptureFixture[str]) -> None:
    code, output, _ = run_cli(capsys, "--dry-run", "--scenario", "headless-api-error")

    assert code == 0
    assert output is not None
    assert output["sent"] is False
    (summary,) = objects(output["scenarios"])
    assert summary["scenario"] == "headless-api-error"
    session = summary["session_id"]
    assert isinstance(session, str)
    assert uuid.UUID(session).version == 4
    assert summary["requests"] == request_counts("headless-api-error")
    assert summary["canary_marker"] is None
    totals = objects(summary["expected_totals"])
    assert totals
    for total in totals:
        assert set(total) == {"metric", "labels", "total"}
        labels = total["labels"]
        assert isinstance(labels, dict)
        assert labels["session.id"] == session


def test_cli_defaults_to_every_scenario_with_otlp_data(capsys: pytest.CaptureFixture[str]) -> None:
    code, output, _ = run_cli(capsys, "--dry-run", "--temporality", "cumulative", "--canaries")

    assert code == 0
    assert output is not None
    summaries = objects(output["scenarios"])
    assert [summary["scenario"] for summary in summaries] == OTLP_NAMES
    for summary in summaries:
        assert summary["temporality"] == "cumulative"
        marker = summary["canary_marker"]
        assert isinstance(marker, str)
        assert marker.startswith(CANARY_PREFIX)


def test_cli_sends_to_the_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    names = ("headless-api-error", "hooks-endpoint-down")

    with collector() as (endpoint, received):
        code, output, _ = run_cli(
            capsys, "--endpoint", endpoint, "--scenario", names[0], "--scenario", names[1]
        )

    assert code == 0
    assert output is not None
    assert output["sent"] is True
    assert output["failed"] is None
    assert [summary["scenario"] for summary in objects(output["scenarios"])] == list(names)
    assert len(received) == sum(sum(request_counts(name).values()) for name in names)


def test_cli_prints_the_scenarios_sent_before_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = "headless-api-error", "hooks-endpoint-down"
    first_requests = sum(request_counts(first).values())

    with collector(fail_after=first_requests) as (endpoint, received):
        code, output, err = run_cli(
            capsys, "--endpoint", endpoint, "--scenario", first, "--scenario", second
        )

    assert code == 1
    assert len(received) == first_requests + 1
    assert output is not None
    assert [summary["scenario"] for summary in objects(output["scenarios"])] == [first]
    failed = output["failed"]
    assert isinstance(failed, dict)
    assert failed["scenario"] == second
    assert isinstance(failed["session_id"], str)
    assert "503" in str(failed["error"])
    assert err.startswith(f"replay failed: {second}: ")
    assert len(err.splitlines()) == 1


def test_cli_reports_a_send_error_briefly(capsys: pytest.CaptureFixture[str]) -> None:
    endpoint = f"http://127.0.0.1:{closed_port()}"

    code, output, err = run_cli(
        capsys, "--endpoint", endpoint, "--timeout", "0.5", "--scenario", "headless-api-error"
    )

    assert code == 1
    assert output is not None
    assert output["scenarios"] == []
    assert err.startswith("replay failed: ")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1


def test_cli_replays_hooks_and_telemetry_with_one_session_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = "headless-api-error"

    with collector() as (endpoint, otlp), app() as (hooks_endpoint, hooks):
        code, output, _ = run_cli(
            capsys,
            "--endpoint",
            endpoint,
            "--hooks-endpoint",
            hooks_endpoint,
            "--canaries",
            "--scenario",
            name,
        )

    assert code == 0
    assert output is not None
    assert output["hooks_endpoint"] == hooks_endpoint
    (summary,) = objects(output["scenarios"])
    session = summary["session_id"]
    assert isinstance(session, str)
    assert summary["hook_lines"] == len(load_scenario(name).hooks) == len(hooks)
    assert summary["hook_canary_value_count"]
    assert len(otlp) == sum(request_counts(name).values())
    assert {json.loads(got.body)["session_id"] for got in hooks} == {session}
    sent_logs = [json.loads(got.body) for got in otlp if got.path == "/v1/logs"]
    assert session in json.dumps(sent_logs)
    marker = summary["canary_marker"]
    assert isinstance(marker, str)
    assert all(marker in got.body.decode() for got in hooks)


def test_cli_can_replay_only_the_hooks(capsys: pytest.CaptureFixture[str]) -> None:
    with app() as (hooks_endpoint, hooks):
        code, output, _ = run_cli(capsys, "--hooks-endpoint", hooks_endpoint, "--hooks-only")

    assert code == 0
    assert output is not None
    assert output["endpoint"] is None
    summaries = objects(output["scenarios"])
    assert [summary["scenario"] for summary in summaries] == HOOK_NAMES
    for summary in summaries:
        assert summary["requests"] == dict.fromkeys(SIGNALS, 0)
        assert summary["canary_marker"] is None
    assert len(hooks) == sum(len(load_scenario(name).hooks) for name in HOOK_NAMES)


def test_cli_with_hooks_defaults_to_every_scenario_with_either(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, output, _ = run_cli(capsys, "--dry-run", "--hooks-endpoint", DEFAULT_HOOKS_ENDPOINT)

    assert code == 0
    assert output is not None
    summaries = objects(output["scenarios"])
    assert [summary["scenario"] for summary in summaries] == NAMES
    lines = {summary["scenario"]: summary["hook_lines"] for summary in summaries}
    assert lines["hooks-endpoint-down"] == 0
    assert lines["headless-sessionstart-http-ignored"] == 4


def test_cli_paces_the_hooks_with_a_speed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    fake_time = SimpleNamespace(
        time_ns=time.time_ns, sleep=clock.sleep, perf_counter=clock.monotonic
    )
    monkeypatch.setattr(replay_module, "time", fake_time)
    name = "headless-api-error"
    offsets = [int(str(line["offset_ms"])) for line in load_scenario(name).hooks]

    with app() as (hooks_endpoint, _):
        code, _, _ = run_cli(
            capsys,
            "--hooks-endpoint",
            hooks_endpoint,
            "--hooks-only",
            "--speed",
            "1000",
            "--scenario",
            name,
        )

    assert code == 0
    assert sum(clock.sleeps) == pytest.approx((offsets[-1] - offsets[0]) / 1000 / 1000)


def test_cli_reports_a_hook_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    with app(status=500) as (hooks_endpoint, _):
        code, output, err = run_cli(
            capsys,
            "--hooks-endpoint",
            hooks_endpoint,
            "--hooks-only",
            "--scenario",
            "headless-api-error",
        )

    assert code == 1
    assert output is not None
    failed = output["failed"]
    assert isinstance(failed, dict)
    assert failed["scenario"] == "headless-api-error"
    assert "not an empty 204" in str(failed["error"])
    assert err.startswith("replay failed: headless-api-error: ")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--hooks-only"], "--hooks-only needs --hooks-endpoint"),
        (["--speed", "1"], "--speed needs --hooks-endpoint"),
        (["--hooks-endpoint", DEFAULT_HOOKS_ENDPOINT, "--speed", "0"], "--speed must be positive"),
        (
            ["--hooks-endpoint", DEFAULT_HOOKS_ENDPOINT, "--hooks-only", "--endpoint", "http://x"],
            "--endpoint does not go with --hooks-only",
        ),
    ],
)
def test_cli_rejects_inconsistent_hook_options(
    capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--dry-run", *args])

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_rejects_an_unknown_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--dry-run", "--scenario", "no-such-scenario"])

    assert exit_info.value.code == 2
    assert "no-such-scenario" in capsys.readouterr().err
