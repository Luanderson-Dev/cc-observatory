"""Permission denials that only telemetry reports, read back from Loki (ADR 0029).

A permission dialog denied with Esc fires no closing hook (ADRs 0012 and 0024): its
block stays open until the next progress event. Claude Code's telemetry does record the
denial, as a ``claude_code.tool_decision`` event with ``decision=reject`` and the call's
``tool_use_id`` (the ``interactive-permission-compact`` fixture). This module holds the
pure half of closing those blocks from Loki: the LogQL query, which blocks are due, the
query's time window, the parser of Loki's ``query_range`` answer, and the matching of
rejections with open blocks. ``ccobs.server.reconcile`` does the polling and the HTTP.

Names, from the ADRs and the fixtures: Loki's stream has the index labels
``service_name`` and ``host`` (ADRs 0015 and 0022); ``event_name``, ``session_id``,
``decision`` and ``tool_use_id`` are structured metadata, the OpenTelemetry attribute
names with dots turned into underscores (ADRs 0014 and 0015), and the attribute values
are those of ADR 0012. The query carries no session ID, so Loki's info-level query log
never records one (ADR 0015's note, decided in ADR 0029).

The event time is Loki's entry timestamp, not the ``event_timestamp`` attribute: Loki
stores each OTLP record at its ``timeUnixNano``, which every ``tool_decision`` record of
the fixtures sets to the same millisecond as ``event.timestamp``; the entry timestamp is
always present, an integer in nanoseconds, and the very field the query's ``start`` and
``end`` bound, while ``event_timestamp`` is an ISO 8601 string in structured metadata.

Data and string building only: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ccobs.domain.telemetry import HOST_LABEL, SERVICE_NAME, quote

# Loki's stream label for service.name (ADR 0015).
SERVICE_NAME_LABEL: Final = "service_name"
# Structured metadata keys (ADRs 0014 and 0015) and the values of ADR 0012.
EVENT_NAME_KEY: Final = "event_name"
SESSION_ID_KEY: Final = "session_id"
DECISION_KEY: Final = "decision"
TOOL_USE_ID_KEY: Final = "tool_use_id"
DECISION_EVENT: Final = "tool_decision"
REJECT: Final = "reject"
# The request header that makes Loki return structured metadata apart from the stream
# labels (ADR 0015).
ENCODING_FLAGS_HEADER: Final = "X-Loki-Response-Encoding-Flags"
CATEGORIZE_LABELS: Final = "categorize-labels"

_NS_PER_MS: Final = 1_000_000
# Loki's timestamps are Unix nanoseconds as a decimal string: 19 digits until 2286.
_TIMESTAMP: Final = re.compile(r"[0-9]{1,20}")
_SUCCESS: Final = "success"
_STREAMS: Final = "streams"
_METADATA: Final = "structuredMetadata"


@dataclass(frozen=True, slots=True)
class OpenBlock:
    """An open permission block that a rejection can close: its session, the call it
    borrowed from ``PreToolUse`` (ADR 0024) and when it opened, in ms."""

    session_id: str
    tool_use_id: str
    opened_at_ms: int


@dataclass(frozen=True, slots=True)
class Denial:
    """A call that was denied, in a session, at ``at_ms``: a rejection read from Loki
    (at its event time), or a close to submit (at the clamped time)."""

    session_id: str
    tool_use_id: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class ParsedRejections:
    """What ``parse_rejections`` read. ``valid`` is False when the answer is not a
    successful streams result at all; ``skipped`` counts the malformed streams and
    entries left out (entries of another event or decision are left out uncounted)."""

    rejections: tuple[Denial, ...]
    skipped: int
    entries: int
    valid: bool


class _MalformedError(Exception):
    """An entry or stream that does not have the shape of Loki's answer."""


def rejections_query(host: str) -> str:
    """The LogQL that selects every ``tool_decision`` reject of ``host``'s stream:
    ``{service_name="claude-code", host="<host>"} | event_name="tool_decision"
    | decision="reject"``. ``host`` is quoted (``telemetry.quote``) even though the
    settings already restrict it to ``HOST_VALUE_PATTERN``. Raises ``ValueError`` for a
    host that is not valid UTF-8."""
    selector = f"{{{SERVICE_NAME_LABEL}={quote(SERVICE_NAME)}, {HOST_LABEL}={quote(host)}}}"
    return f"{selector} | {EVENT_NAME_KEY}={quote(DECISION_EVENT)} | {DECISION_KEY}={quote(REJECT)}"


def due_blocks(
    blocks: Iterable[OpenBlock], *, now_ms: int, grace_ms: int, horizon_ms: int
) -> list[OpenBlock]:
    """The blocks open for at least ``grace_ms`` and at most ``horizon_ms`` at
    ``now_ms``: younger ones still have a chance to be closed by their hook, and older
    ones are left to the hooks and the stale sweep, which bounds the query's window."""
    return [block for block in blocks if grace_ms <= now_ms - block.opened_at_ms <= horizon_ms]


def query_window_ns(blocks: Sequence[OpenBlock], *, now_ms: int, margin_ms: int) -> tuple[int, int]:
    """The ``(start, end)`` of the query in Unix nanoseconds: from the oldest block's
    opening minus ``margin_ms`` (never before the epoch) to ``now_ms`` plus it. The
    margin covers a clock difference between the app's container and the machine that
    stamps Claude Code's events. Raises ``ValueError`` without a block."""
    if not blocks:
        raise ValueError("the window needs at least one block")
    oldest = min(block.opened_at_ms for block in blocks)
    return max(0, oldest - margin_ms) * _NS_PER_MS, (now_ms + margin_ms) * _NS_PER_MS


def _strings(value: object) -> dict[str, str]:
    """The string items of a mapping; anything else is malformed."""
    if not isinstance(value, Mapping):
        raise _MalformedError
    return {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }


def _entry(labels: Mapping[str, str], value: object) -> Denial | None:
    """The rejection in one entry, ``[ns, line, {"structuredMetadata": {...}}]``; None
    for an entry of another event or decision. Without ``categorize-labels``, Loki
    merges structured metadata into the stream labels, so both are read."""
    if not isinstance(value, list) or len(value) < 2:
        raise _MalformedError
    stamp = value[0]
    if not isinstance(stamp, str) or not _TIMESTAMP.fullmatch(stamp):
        raise _MalformedError
    fields = dict(labels)
    if len(value) > 2:
        categories = value[2]
        if not isinstance(categories, Mapping):
            raise _MalformedError
        fields |= _strings(categories.get(_METADATA) or {})
    event, decision = fields.get(EVENT_NAME_KEY), fields.get(DECISION_KEY)
    session_id, tool_use_id = fields.get(SESSION_ID_KEY), fields.get(TOOL_USE_ID_KEY)
    if not (event and decision and session_id and tool_use_id):
        raise _MalformedError
    if event != DECISION_EVENT or decision != REJECT:
        return None
    return Denial(session_id, tool_use_id, int(stamp) // _NS_PER_MS)


def parse_rejections(document: object) -> ParsedRejections:
    """The rejections in Loki's ``query_range`` answer (the JSON already decoded).

    Never raises: an answer that is not a successful streams result is ``valid=False``;
    a malformed stream or entry is skipped and counted."""
    data = document.get("data") if isinstance(document, Mapping) else None
    result = data.get("result") if isinstance(data, Mapping) else None
    if (
        not isinstance(document, Mapping)
        or document.get("status") != _SUCCESS
        or not isinstance(data, Mapping)
        or data.get("resultType") != _STREAMS
        or not isinstance(result, list)
    ):
        return ParsedRejections(rejections=(), skipped=0, entries=0, valid=False)
    rejections: list[Denial] = []
    skipped = entries = 0
    for stream in result:
        values = stream.get("values") if isinstance(stream, Mapping) else None
        if not isinstance(values, list):
            skipped += 1
            continue
        try:
            labels = _strings(stream.get("stream"))
        except _MalformedError:
            labels = {}
        for value in values:
            entries += 1
            try:
                found = _entry(labels, value)
            except _MalformedError:
                skipped += 1
                continue
            if found is not None:
                rejections.append(found)
    return ParsedRejections(
        rejections=tuple(rejections), skipped=skipped, entries=entries, valid=True
    )


def match_denials(
    rejections: Iterable[Denial], blocks: Iterable[OpenBlock], *, now_ms: int
) -> list[Denial]:
    """The closes to submit: one per open call (``session_id`` and ``tool_use_id``)
    that a rejection names, at the earliest rejection's time clamped to
    ``[opened_at_ms, now_ms]`` (to the opening alone if the clock went back below it).
    In the order of ``blocks``."""
    earliest: dict[tuple[str, str], int] = {}
    for rejection in rejections:
        key = (rejection.session_id, rejection.tool_use_id)
        earliest[key] = min(rejection.at_ms, earliest.get(key, rejection.at_ms))
    closes: list[Denial] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        key = (block.session_id, block.tool_use_id)
        if key in seen or key not in earliest:
            continue
        seen.add(key)
        opened = block.opened_at_ms
        at_ms = min(max(earliest[key], opened), max(now_ms, opened))
        closes.append(Denial(block.session_id, block.tool_use_id, at_ms))
    return closes
