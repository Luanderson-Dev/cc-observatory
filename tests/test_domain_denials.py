"""Unit tests of ``ccobs.domain.denials``: the LogQL query, the parser of Loki's answer
and the matching that close Esc-denied permission blocks from telemetry (ADR 0029).

The Loki answers are built from the real ``tool_decision`` records of the
``interactive-permission-compact`` OTLP fixture, in the shape that
``tests/integration/test_stack_logs.py`` reads from Loki with the ``categorize-labels``
header: index labels under ``stream``, each entry ``[ns, line, {"structuredMetadata":
{...}}]``, attribute names with dots turned into underscores (ADR 0015).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest

from ccobs.domain.denials import (
    CATEGORIZE_LABELS,
    DECISION_EVENT,
    DECISION_KEY,
    ENCODING_FLAGS_HEADER,
    EVENT_NAME_KEY,
    REJECT,
    SESSION_ID_KEY,
    TOOL_USE_ID_KEY,
    Denial,
    OpenBlock,
    due_blocks,
    match_denials,
    parse_rejections,
    query_window_ns,
    rejections_query,
)
from ccobs.domain.telemetry import (
    DEFAULT_HOST,
    HOST_LABEL,
    LOKI_METADATA,
    SERVICE_NAME,
    is_identity_key,
)

FIXTURES: Final = Path(__file__).parent / "fixtures"
OTLP_LOGS: Final = FIXTURES / "otlp" / "interactive-permission-compact" / "logs.jsonl"
MANIFEST: Final = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
# The fixture's session, and the call denied with Esc (ADR 0012, tests/fixtures/README.md).
SESSION: Final = next(
    scenario["session_id"]
    for scenario in MANIFEST["scenarios"]
    if scenario["name"] == "interactive-permission-compact"
)
DENIED_CALL: Final = "toolu_01EXAMPLE000000000000023"
APPROVED_CALL: Final = "toolu_01EXAMPLE000000000000022"
# The reject's timeUnixNano and event.timestamp (2026-10-13T23:20:41.510Z), in ms.
DENIED_AT_MS: Final = 1_791_933_641_510
NS_PER_MS: Final = 1_000_000

type Json = dict[str, Any]


def _value(value: Json) -> str:
    """An OTLP AnyValue as Loki stores it in structured metadata: a string."""
    (kind, inner), *_ = value.items()
    return str(inner) if kind != "boolValue" else str(inner).lower()


def tool_decision_records() -> list[tuple[Json, Json]]:
    """``(resource, record)`` for every ``tool_decision`` record of the fixture."""
    found = []
    for line in OTLP_LOGS.read_text(encoding="utf-8").splitlines():
        for resource_logs in json.loads(line)["resourceLogs"]:
            for scope_logs in resource_logs["scopeLogs"]:
                for record in scope_logs["logRecords"]:
                    attributes = {item["key"]: item["value"] for item in record["attributes"]}
                    if attributes["event.name"] == {"stringValue": DECISION_EVENT}:
                        found.append((resource_logs["resource"], record))
    return found


def loki_entry(resource: Json, record: Json) -> list[Any]:
    """One record as Loki returns it with ``categorize-labels``: the collector has
    deleted identity keys (ADR 0021) and Loki has turned dots into underscores (ADR 0015)."""
    metadata = {
        key.replace(".", "_"): _value(item["value"])
        for item in [*resource["attributes"], *record["attributes"]]
        if (key := item["key"]) != "service.name" and not is_identity_key(key)
    }
    metadata |= {"trace_id": record["traceId"], "span_id": record["spanId"]}
    metadata |= {"flags": str(record["flags"]), "detected_level": "unknown"}
    metadata |= {"observed_timestamp": record["observedTimeUnixNano"]}
    metadata |= {"scope_name": "com.anthropic.claude_code.events", "scope_version": "2.1.296"}
    return [record["timeUnixNano"], record["body"]["stringValue"], {"structuredMetadata": metadata}]


def loki_answer(entries: list[list[Any]]) -> Json:
    """A ``query_range`` answer with ``entries`` in the one stream of the default host."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": SERVICE_NAME, HOST_LABEL: DEFAULT_HOST},
                    "values": entries,
                }
            ],
            "stats": {},
            "encodingFlags": [CATEGORIZE_LABELS],
        },
    }


def fixture_answer(*, rejects_only: bool = True) -> Json:
    entries = [loki_entry(resource, record) for resource, record in tool_decision_records()]
    if rejects_only:
        entries = [
            entry for entry in entries if entry[2]["structuredMetadata"]["decision"] == REJECT
        ]
    return loki_answer(entries)


# The fixture holds what the design relies on.


def test_the_fixture_has_one_approval_and_one_esc_rejection_with_their_call_ids() -> None:
    entries = [loki_entry(resource, record) for resource, record in tool_decision_records()]
    metadata = [entry[2]["structuredMetadata"] for entry in entries]

    assert [(m["decision"], m["source"], m["tool_use_id"]) for m in metadata] == [
        ("accept", "user_temporary", APPROVED_CALL),
        (REJECT, "user_reject", DENIED_CALL),
    ]
    # The fields the query and the parser name are on the records, under these names.
    for item in metadata:
        assert {SESSION_ID_KEY, EVENT_NAME_KEY, DECISION_KEY, TOOL_USE_ID_KEY} <= set(item)
        assert item[SESSION_ID_KEY] == SESSION
        assert set(item) >= LOKI_METADATA


def test_the_entry_timestamp_is_the_event_timestamp_on_every_tool_decision() -> None:
    for _, record in tool_decision_records():
        attributes = {item["key"]: item["value"] for item in record["attributes"]}
        stamp = attributes["event.timestamp"]["stringValue"]
        assert stamp.endswith("Z")
        # The record's time and its event.timestamp attribute agree to the millisecond.
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert int(record["timeUnixNano"]) == round(moment.timestamp() * 1000) * NS_PER_MS


# The query.


def test_the_query_is_exact_and_has_no_session_id() -> None:
    query = rejections_query(DEFAULT_HOST)

    assert query == (
        '{service_name="claude-code", host="local"} | event_name="tool_decision"'
        ' | decision="reject"'
    )
    assert SESSION_ID_KEY not in query
    assert SESSION not in query


def test_the_query_names_come_from_the_adrs() -> None:
    assert (SERVICE_NAME, HOST_LABEL) == ("claude-code", "host")
    assert (EVENT_NAME_KEY, DECISION_KEY, TOOL_USE_ID_KEY) == (
        "event_name",
        "decision",
        "tool_use_id",
    )
    assert (DECISION_EVENT, REJECT) == ("tool_decision", "reject")
    assert (ENCODING_FLAGS_HEADER, CATEGORIZE_LABELS) == (
        "X-Loki-Response-Encoding-Flags",
        "categorize-labels",
    )


@pytest.mark.parametrize(
    ("host", "literal"),
    [
        ("my-laptop.lan_2", '"my-laptop.lan_2"'),
        ('a"b', r'"a\"b"'),
        ("a\\b", r'"a\\b"'),
        ('x"} | session_id="y', r'"x\"} | session_id=\"y"'),
        ("new\nline", r'"new\nline"'),
    ],
)
def test_the_host_is_escaped(host: str, literal: str) -> None:
    query = rejections_query(host)

    assert query.startswith(f'{{service_name="claude-code", host={literal}}} | ')
    assert query.endswith(' | event_name="tool_decision" | decision="reject"')


def test_a_host_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        rejections_query("\ud800")


# Which blocks are due, and the window.


def block(opened_at_ms: int, *, session: str = SESSION, call: str = DENIED_CALL) -> OpenBlock:
    return OpenBlock(session_id=session, tool_use_id=call, opened_at_ms=opened_at_ms)


def test_only_blocks_between_the_grace_period_and_the_horizon_are_due() -> None:
    now = 100_000
    blocks = [
        block(9_999, call="too old"),
        block(10_000, call="at the horizon"),
        block(50_000, call="due"),
        block(98_000, call="at the grace period"),
        block(98_001, call="too young"),
    ]

    due = due_blocks(blocks, now_ms=now, grace_ms=2_000, horizon_ms=90_000)

    assert [item.tool_use_id for item in due] == ["at the horizon", "due", "at the grace period"]
    assert due_blocks([], now_ms=now, grace_ms=2_000, horizon_ms=90_000) == []


def test_the_window_runs_from_the_oldest_opening_minus_the_margin_to_now_plus_it() -> None:
    blocks = [block(50_000_000, call="a"), block(40_000_000, call="b")]

    start, end = query_window_ns(blocks, now_ms=60_000_000, margin_ms=600_000)

    assert start == (40_000_000 - 600_000) * NS_PER_MS
    assert end == (60_000_000 + 600_000) * NS_PER_MS


def test_the_window_needs_a_block() -> None:
    with pytest.raises(ValueError, match="block"):
        query_window_ns([], now_ms=1, margin_ms=1)


def test_the_window_never_starts_before_the_epoch() -> None:
    start, _ = query_window_ns([block(5)], now_ms=10, margin_ms=600_000)

    assert start == 0


# The parser.


def test_the_parser_reads_the_fixture_rejection() -> None:
    parsed = parse_rejections(fixture_answer())

    assert parsed.valid
    assert parsed.skipped == 0
    assert parsed.rejections == (Denial(SESSION, DENIED_CALL, DENIED_AT_MS),)


def test_the_parser_ignores_approvals_without_counting_them() -> None:
    parsed = parse_rejections(fixture_answer(rejects_only=False))

    assert parsed.valid
    assert parsed.skipped == 0
    assert parsed.rejections == (Denial(SESSION, DENIED_CALL, DENIED_AT_MS),)


def test_the_parser_reads_metadata_merged_into_the_stream_labels() -> None:
    # Without categorize-labels, Loki merges structured metadata into the labels.
    answer = fixture_answer()
    stream = answer["data"]["result"][0]
    (entry,) = stream["values"]
    stream["stream"] = {**stream["stream"], **entry[2]["structuredMetadata"]}
    stream["values"] = [entry[:2]]

    assert parse_rejections(answer).rejections == (Denial(SESSION, DENIED_CALL, DENIED_AT_MS),)


def test_an_empty_answer_is_valid() -> None:
    parsed = parse_rejections(loki_answer([]))

    assert parsed.valid
    assert parsed.rejections == ()
    assert parsed.skipped == 0
    no_streams = {"status": "success", "data": {"resultType": "streams", "result": []}}
    assert parse_rejections(no_streams).valid


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "streams",
        {},
        {"status": "error", "data": {"resultType": "streams", "result": []}},
        {"status": "success"},
        {"status": "success", "data": []},
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
        {"status": "success", "data": {"resultType": "streams", "result": {}}},
    ],
)
def test_an_answer_that_is_not_a_streams_result_is_invalid(document: object) -> None:
    parsed = parse_rejections(document)

    assert not parsed.valid
    assert parsed.rejections == ()


def _broken(change: str) -> Any:
    (entry,) = fixture_answer()["data"]["result"][0]["values"]
    entry = copy.deepcopy(entry)
    metadata = entry[2]["structuredMetadata"]
    match change:
        case "not a list":
            return {"0": entry[0]}
        case "too short":
            return entry[:1]
        case "timestamp not a string":
            entry[0] = int(entry[0])
        case "timestamp not digits":
            entry[0] = "1.7e18"
        case "timestamp negative":
            entry[0] = "-5"
        case "timestamp too long":
            entry[0] = "9" * 30
        case "timestamp unicode digits":
            entry[0] = "١٢٣"
        case "categories not a mapping":
            entry[2] = ["structuredMetadata"]
        case "metadata not a mapping":
            entry[2] = {"structuredMetadata": [["session_id", SESSION]]}
        case "no session":
            del metadata["session_id"]
        case "empty session":
            metadata["session_id"] = ""
        case "session not a string":
            metadata["session_id"] = 7
        case "no call":
            del metadata["tool_use_id"]
        case "empty call":
            metadata["tool_use_id"] = ""
        case "no event name":
            del metadata["event_name"]
        case "no decision":
            del metadata["decision"]
    return entry


BROKEN: Final = (
    "not a list",
    "too short",
    "timestamp not a string",
    "timestamp not digits",
    "timestamp negative",
    "timestamp too long",
    "timestamp unicode digits",
    "categories not a mapping",
    "metadata not a mapping",
    "no session",
    "empty session",
    "session not a string",
    "no call",
    "empty call",
    "no event name",
    "no decision",
)


@pytest.mark.parametrize("change", BROKEN)
def test_a_malformed_entry_is_skipped_and_counted(change: str) -> None:
    good = fixture_answer()["data"]["result"][0]["values"][0]
    answer = loki_answer([_broken(change), good])

    parsed = parse_rejections(answer)

    assert parsed.valid
    assert parsed.skipped == 1
    assert parsed.rejections == (Denial(SESSION, DENIED_CALL, DENIED_AT_MS),)


def test_malformed_streams_are_skipped_and_counted() -> None:
    answer = fixture_answer()
    good = answer["data"]["result"][0]
    answer["data"]["result"] = [
        "a stream",
        {"stream": {}, "values": "not a list"},
        {"stream": "not a mapping", "values": good["values"]},
        good,
    ]

    parsed = parse_rejections(answer)

    assert parsed.valid
    assert parsed.skipped == 2
    # A stream without labels still holds its entries' metadata.
    assert parsed.rejections == (Denial(SESSION, DENIED_CALL, DENIED_AT_MS),) * 2


@pytest.mark.parametrize(
    ("key", "value"), [(EVENT_NAME_KEY, "tool_result"), (DECISION_KEY, "accept")]
)
def test_an_entry_of_another_event_or_decision_is_ignored_not_counted(key: str, value: str) -> None:
    entry = copy.deepcopy(fixture_answer()["data"]["result"][0]["values"][0])
    entry[2]["structuredMetadata"][key] = value

    parsed = parse_rejections(loki_answer([entry]))

    assert parsed.valid
    assert parsed.skipped == 0
    assert parsed.rejections == ()


def test_the_parser_never_raises() -> None:
    weird: list[object] = [
        {"status": "success", "data": {"resultType": "streams", "result": [None, 1, [], {}]}},
        {"status": "success", "data": {"resultType": "streams", "result": [{"values": [None]}]}},
        {"status": "success", "data": {"resultType": "streams", "result": [{"values": [[]]}]}},
    ]
    for document in weird:
        parsed = parse_rejections(document)
        assert parsed.rejections == ()
        assert parsed.skipped >= 1


# Matching.


def test_a_rejection_closes_its_open_block_at_the_event_time() -> None:
    opened = DENIED_AT_MS - 2_300
    now = DENIED_AT_MS + 5_000

    closes = match_denials(
        [Denial(SESSION, DENIED_CALL, DENIED_AT_MS)], [block(opened)], now_ms=now
    )

    assert closes == [Denial(SESSION, DENIED_CALL, DENIED_AT_MS)]


def test_matching_needs_both_the_session_and_the_call() -> None:
    rejections = [
        Denial("other-session", DENIED_CALL, DENIED_AT_MS),
        Denial(SESSION, APPROVED_CALL, DENIED_AT_MS),
    ]

    assert match_denials(rejections, [block(DENIED_AT_MS - 1)], now_ms=DENIED_AT_MS + 1) == []


def test_the_close_time_is_clamped_to_the_opening_and_to_now() -> None:
    opened, now = 1_000_000, 2_000_000

    early = match_denials([Denial(SESSION, DENIED_CALL, 10)], [block(opened)], now_ms=now)
    late = match_denials([Denial(SESSION, DENIED_CALL, 9 * now)], [block(opened)], now_ms=now)
    # A clock that went back below the opening: the opening wins.
    back = match_denials([Denial(SESSION, DENIED_CALL, now)], [block(opened)], now_ms=opened - 5)

    assert early == [Denial(SESSION, DENIED_CALL, opened)]
    assert late == [Denial(SESSION, DENIED_CALL, now)]
    assert back == [Denial(SESSION, DENIED_CALL, opened)]


def test_the_earliest_rejection_of_a_call_wins_and_each_block_closes_once() -> None:
    opened = 1_000
    rejections = [
        Denial(SESSION, DENIED_CALL, 3_000),
        Denial(SESSION, DENIED_CALL, 2_000),
        Denial(SESSION, "other-call", 2_500),
    ]
    blocks = [block(opened), block(opened), block(opened + 1, call="other-call")]

    closes = match_denials(rejections, blocks, now_ms=10_000)

    assert closes == [
        Denial(SESSION, DENIED_CALL, 2_000),
        Denial(SESSION, "other-call", 2_500),
    ]
