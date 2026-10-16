"""Permission blocks denied with Esc, closed from Loki, in the isolated ``ccobs-it``
compose project (ADR 0029).

Needs Docker: ``uv run pytest -m integration --no-cov``. The hooks and the log records of
``interactive-permission-compact`` are replayed with one fresh session ID: the hooks up
to just before ``PreCompact``, which would close the denied dialog's block as
superseded, then the records, timed so that the ``tool_decision`` reject lands one
second after the block opened by the app's clock. The reconciler must close the block as
``denied`` from ``telemetry`` at the reject's own time, before the rest of the hooks go.

The other tests check, against the real Loki, what ADR 0029 left unverified: that
``tool_use_id`` comes back as structured metadata under that name, that an entry's
timestamp is the record's ``timeUnixNano``, that the app's exact query selects only
rejects, that Loki accepts an ``end`` 10 minutes ahead and does not clamp it to now (as
far as records about 2 minutes ahead show), and that a
``direction=backward`` answer has the shape ``denials.parse_rejections`` reads. Every
query here is host-wide, like the app's: none names a session, so none puts a session ID
in Loki's query log (test_app_privacy.py checks that log).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import pytest
from app_support import (
    detail,
    events,
    expected_shape,
    get_json,
    items,
    mapping,
    poll,
)

from ccobs.domain.denials import (
    CATEGORIZE_LABELS,
    DECISION_EVENT,
    ENCODING_FLAGS_HEADER,
    REJECT,
    SERVICE_NAME_LABEL,
    Denial,
    parse_rejections,
    rejections_query,
)
from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL, SERVICE_NAME
from ccobs.server.reconcile import QUERY_LIMIT, QUERY_RANGE_PATH, WINDOW_MARGIN_S
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    Prepared,
    PreparedHooks,
    fresh_session_id,
    load_scenario,
    prepare,
    prepare_hooks,
    send,
    send_hooks,
)

pytestmark = pytest.mark.integration

SCENARIO: Final = "interactive-permission-compact"
NS_PER_MS: Final = 1_000_000
# When the reject is stamped, after the block opened (by the app's clock): inside the
# reconciler's window, and early enough that the close is not clamped to its query time.
ESC_AFTER_MS: Final = 1_000
# Seconds for the block to open once the hooks are sent, and for the close: the grace
# period (2 s), a poll (2 s), the collector and Loki's ingestion, with a margin.
OPEN_DEADLINE: Final = 15.0
CLOSE_DEADLINE: Final = 30.0
STORED_DEADLINE: Final = 15.0
MARGIN_NS: Final = int(WINDOW_MARGIN_S * 1_000_000_000)
# The selector of the app's query, and its tool_decision filter without the decision.
SELECTOR: Final = f'{{{SERVICE_NAME_LABEL}="{SERVICE_NAME}", {HOST_LABEL}="{DEFAULT_HOST}"}}'
DECISIONS: Final = f'{SELECTOR} | event_name="{DECISION_EVENT}"'


@dataclass(frozen=True)
class Entry:
    labels: dict[str, str]
    timestamp: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class EscReplay:
    """The replay: what was sent, the denied block when it opened, once it closed and
    after the rest of the hooks, and how long the close took."""

    hooks: PreparedHooks
    telemetry: Prepared
    denied_call: str
    reject_ns: int
    opened: dict[str, object]
    closed: dict[str, object]
    after: dict[str, object]
    status_after_close: object
    status_at_end: object
    # Seconds from the reject's timestamp, and from sending the records, to the close
    # showing in the API.
    reject_to_close_s: float
    send_to_close_s: float


# Reading the prepared records.


def _records(run: Prepared) -> Iterator[JSONObject]:
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


def _rejects(run: Prepared) -> list[JSONObject]:
    return [
        record
        for record in _records(run)
        if _attribute(record, "event.name") == DECISION_EVENT
        and _attribute(record, "decision") == REJECT
    ]


# Talking to Loki and the app.


def _loki(
    loki_url: str, query: str, start_ns: int, end_ns: int, direction: str = "backward"
) -> object:
    """The decoded ``query_range`` answer, asked as the reconciler asks."""
    return get_json(
        loki_url,
        QUERY_RANGE_PATH,
        {
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(QUERY_LIMIT),
            "direction": direction,
        },
        {ENCODING_FLAGS_HEADER: CATEGORIZE_LABELS},
    )


def _entries(document: object) -> list[Entry]:
    data = mapping(mapping(document, "answer").get("data"), "data")
    assert data.get("resultType") == "streams", data.get("resultType")
    found: list[Entry] = []
    for stream in items(data.get("result"), "result"):
        item = mapping(stream, "stream")
        labels = {key: str(value) for key, value in mapping(item["stream"], "labels").items()}
        for value in items(item.get("values"), "values"):
            parts = items(value, "value")
            categories = mapping(parts[2], "categories") if len(parts) > 2 else {}
            raw = mapping(categories.get("structuredMetadata") or {}, "structuredMetadata")
            metadata = {key: str(text) for key, text in raw.items()}
            found.append(Entry(labels, str(parts[0]), metadata))
    return found


def _ours(entries: list[Entry], esc: EscReplay) -> list[Entry]:
    """The entries of the replayed session, filtered here: no query names a session."""
    return [entry for entry in entries if entry.metadata.get("session_id") == esc.hooks.session_id]


def _block(app_url: str, session_id: str, call: str) -> dict[str, object] | None:
    found = detail(app_url, session_id)
    if found is None:
        return None
    for item in items(found["blocks"], "blocks"):
        block = mapping(item, "block")
        if block["tool_use_id"] == call and block["kind"] == "permission":
            return block
    return None


def _status(app_url: str, session_id: str) -> object:
    found = detail(app_url, session_id)
    assert found is not None
    return found["status"]


@pytest.fixture(scope="module")
def esc(app_url: str, otlp_http_url: str) -> EscReplay:
    scenario = load_scenario(SCENARIO)
    session_id = fresh_session_id()
    hooks = prepare_hooks(scenario, session_id=session_id)
    names = [line.payload.get("hook_event_name") for line in hooks.lines]
    split = names.index("PreCompact")
    # Where the reject sits before the scenario's latest timestamp, which prepare moves
    # to now_ns (the latest of all three signals).
    probe_ns = time.time_ns()
    probe = prepare(scenario, now_ns=probe_ns, session_id=session_id)
    (probe_reject,) = _rejects(probe)
    denied_call = _attribute(probe_reject, "tool_use_id")
    assert denied_call
    lead_ns = probe_ns - int(str(probe_reject.get("timeUnixNano")))

    send_hooks(app_url, hooks.lines[:split])
    opened = poll(
        lambda: _block(app_url, session_id, denied_call),
        lambda block: block is not None and block["closed_at_ms"] is None,
        OPEN_DEADLINE,
    )
    assert opened is not None, "the denied block did not open"
    assert opened["closed_at_ms"] is None, opened
    assert _status(app_url, session_id) == "waiting_user"
    reject_ns = (int(str(opened["opened_at_ms"])) + ESC_AFTER_MS) * NS_PER_MS
    telemetry = prepare(scenario, now_ns=reject_ns + lead_ns, session_id=session_id)
    (reject,) = _rejects(telemetry)
    assert int(str(reject.get("timeUnixNano"))) == reject_ns
    sent_at = time.monotonic()
    send(otlp_http_url, telemetry, signals=("logs",))

    closed = poll(
        lambda: _block(app_url, session_id, denied_call),
        lambda block: block is not None and block["closed_at_ms"] is not None,
        CLOSE_DEADLINE,
    )
    send_to_close_s = time.monotonic() - sent_at
    reject_to_close_s = (time.time_ns() - reject_ns) / 1e9
    assert closed is not None
    assert closed["closed_at_ms"] is not None, "the block stayed open"
    status_after_close = _status(app_url, session_id)

    send_hooks(app_url, hooks.lines[split:])
    stored = poll(
        lambda: len(events(app_url, session_id)),
        lambda count: count == len(hooks.lines),
        STORED_DEADLINE,
    )
    assert stored == len(hooks.lines), "the rest of the replay was not stored"
    after = _block(app_url, session_id, denied_call)
    assert after is not None
    return EscReplay(
        hooks=hooks,
        telemetry=telemetry,
        denied_call=denied_call,
        reject_ns=reject_ns,
        opened=opened,
        closed=closed,
        after=after,
        status_after_close=status_after_close,
        status_at_end=_status(app_url, session_id),
        reject_to_close_s=reject_to_close_s,
        send_to_close_s=send_to_close_s,
    )


@pytest.fixture(scope="module")
def window(esc: EscReplay) -> tuple[int, int]:
    """The reconciler's window for the denied block: its opening minus the margin to now
    plus the margin (``denials.query_window_ns``)."""
    opened_ns = int(str(esc.opened["opened_at_ms"])) * NS_PER_MS
    return opened_ns - MARGIN_NS, time.time_ns() + MARGIN_NS


# The close.


def test_an_esc_denial_closes_its_block_from_telemetry_before_precompact(esc: EscReplay) -> None:
    print(
        f"Esc to close: {esc.reject_to_close_s:.1f} s from the reject's timestamp, "
        f"{esc.send_to_close_s:.1f} s from sending the records"
    )
    assert esc.closed["close_reason"] == "denied"
    assert esc.closed["close_source"] == "telemetry"
    # At the reject's own time, to the millisecond.
    assert esc.closed["closed_at_ms"] == esc.reject_ns // NS_PER_MS
    assert esc.status_after_close != "waiting_user"
    # PreCompact, which would have superseded it, changes nothing.
    assert esc.after == esc.closed
    assert esc.status_at_end == expected_shape(esc.hooks.lines).status


# What ADR 0029 left unverified.


def test_tool_use_id_comes_back_as_structured_metadata(
    loki_url: str, esc: EscReplay, window: tuple[int, int]
) -> None:
    document = _loki(loki_url, rejections_query(DEFAULT_HOST), *window)
    ours = _ours(_entries(document), esc)
    assert [entry.metadata.get("tool_use_id") for entry in ours] == [esc.denied_call]
    (entry,) = ours
    assert "tool_use_id" not in entry.labels
    assert entry.labels == {SERVICE_NAME_LABEL: SERVICE_NAME, HOST_LABEL: DEFAULT_HOST}
    assert Denial(esc.hooks.session_id, esc.denied_call, esc.reject_ns // NS_PER_MS) in (
        parse_rejections(document).rejections
    )


def test_the_entry_timestamp_is_the_records_time_unix_nano(
    loki_url: str, esc: EscReplay, window: tuple[int, int]
) -> None:
    document = _loki(loki_url, rejections_query(DEFAULT_HOST), *window)
    (entry,) = _ours(_entries(document), esc)
    (reject,) = _rejects(esc.telemetry)
    # To the nanosecond, so to the millisecond the reconciler cuts it to.
    assert entry.timestamp == str(reject.get("timeUnixNano"))
    assert int(entry.timestamp) // NS_PER_MS == esc.reject_ns // NS_PER_MS


def test_the_exact_query_selects_only_rejects(
    loki_url: str, esc: EscReplay, window: tuple[int, int]
) -> None:
    query = rejections_query(DEFAULT_HOST)
    assert query == f'{SELECTOR} | event_name="{DECISION_EVENT}" | decision="{REJECT}"'
    rejects = _entries(_loki(loki_url, query, *window))
    assert rejects
    assert {(e.metadata.get("event_name"), e.metadata.get("decision")) for e in rejects} == {
        (DECISION_EVENT, REJECT)
    }
    # The control: the session's accept is there without the decision filter, and only
    # the reject comes through with it.
    decisions = _entries(_loki(loki_url, DECISIONS, *window))
    assert sorted(e.metadata.get("decision", "") for e in _ours(decisions, esc)) == [
        "accept",
        REJECT,
    ]
    assert [e.metadata.get("decision") for e in _ours(rejects, esc)] == [REJECT]


def test_loki_accepts_an_end_ten_minutes_ahead_without_clamping_it(
    loki_url: str, esc: EscReplay
) -> None:
    # Loki accepts an end 10 minutes ahead, and does not clamp it to now, as far as
    # records about two minutes ahead can show: the records after the reject are stamped
    # up to that far ahead, so only a range that really reaches into the future returns
    # them. Beyond the latest record, nothing tells a clamp from an empty range.
    now_ns = time.time_ns()
    ahead = sorted(
        str(record.get("timeUnixNano"))
        for record in _records(esc.telemetry)
        if int(str(record.get("timeUnixNano"))) > now_ns
    )
    assert ahead, "no replayed record is still in the future"
    document = _loki(loki_url, SELECTOR, now_ns, now_ns + MARGIN_NS, direction="forward")
    assert sorted(entry.timestamp for entry in _ours(_entries(document), esc)) == ahead


def test_a_backward_answer_has_the_shape_the_parser_reads(
    loki_url: str, esc: EscReplay, window: tuple[int, int]
) -> None:
    document = _loki(loki_url, rejections_query(DEFAULT_HOST), *window, direction="backward")
    parsed = parse_rejections(document)
    assert parsed.valid
    assert parsed.skipped == 0
    assert parsed.entries == len(_entries(document)) >= 1
    assert esc.denied_call in {denial.tool_use_id for denial in parsed.rejections}
    # Newest first within each stream.
    data = mapping(mapping(document, "answer").get("data"), "data")
    for stream in items(data.get("result"), "result"):
        values = items(mapping(stream, "stream")["values"], "values")
        stamps = [int(str(items(value, "value")[0])) for value in values]
        assert stamps == sorted(stamps, reverse=True)
