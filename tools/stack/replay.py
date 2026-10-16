"""Replay the sanitized OTLP fixtures to an OpenTelemetry Collector over OTLP/HTTP JSON.

The fixtures in ``tests/fixtures/otlp/`` are export requests Claude Code sent during real
sessions. ``prepare`` turns one scenario into requests that look fresh:

- every ``*UnixNano`` field, and the ``event.timestamp`` attribute, moves by one delta so
  that the scenario's latest timestamp lands on ``now_ns``;
- ``session.id`` becomes a new UUID, and trace and span IDs get fresh random values
  through one mapping per run, so log records still point at their spans;
- optionally, the delta sums become cumulative, and canary values are planted where the
  collector must delete or drop them (identity and content attributes, ADR 0021).

It also computes the total each metric series should reach once stored. ``post_otlp`` and
``send`` deliver the requests.

The hook fixtures in ``tests/fixtures/hooks/`` replay to the app's hook route the same
way: ``prepare_hooks`` gives a scenario's hook payloads the run's session ID (pass the
``session_id`` of ``prepare``'s result, so that hooks and telemetry of one scenario share
it; ``fresh_session_id`` makes one), optionally with canaries in every content field
(ADR 0023), and ``send_hooks`` POSTs them in file order, each answered with an empty 204
or the replay fails. Run it from the repository root::

    uv run python -m tools.stack.replay --endpoint http://127.0.0.1:4318 --dry-run
    uv run python -m tools.stack.replay --hooks-endpoint http://127.0.0.1:4300 --hooks-only

Developer tool, standard library only; not part of the published package.
"""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import random
import re
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Protocol, Self
from urllib.parse import urlsplit

from tools.capture.jsontypes import JSON, JSONObject

type Signal = Literal["metrics", "logs", "traces"]
type Temporality = Literal["delta", "cumulative"]
type Number = int | float

SIGNALS: Final[tuple[Signal, ...]] = ("metrics", "logs", "traces")
TEMPORALITIES: Final[tuple[Temporality, ...]] = ("delta", "cumulative")
DEFAULT_FIXTURES: Final = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
DEFAULT_ENDPOINT: Final = "http://127.0.0.1:4318"
DEFAULT_TIMEOUT: Final = 10.0
# Characters of a refused request's response body kept in the error message.
MAX_ERROR_BODY: Final = 300
# Bytes of a response body read at most.
_MAX_REPLY_BYTES: Final = 64 * 1024

# Attributes the collector always deletes as personal data (ADR 0021): every ``user.*``
# and ``enduser.*`` key, plus ``organization.id``, ``identity.source`` and ``host.name``.
IDENTITY_KEYS: Final = frozenset({"organization.id", "identity.source", "host.name"})
IDENTITY_PREFIXES: Final = ("user.", "enduser.")

# Canaries: values planted where the collector must delete or drop them. Later stages
# search the stored data for the run's marker to prove that nothing got through. The
# lists are the identity and content lists of ADR 0021, including the names that only
# Claude Code's monitoring docs mention (the fixtures never showed them).
CANARY_PREFIX: Final = "ccobs-canary-"
IDENTITY_CANARY_KEYS: Final = (
    "user.email",
    "user.id",
    "user.account_uuid",
    "user.account_id",
    "organization.id",
    "user.groups",
    "identity.source",
    "host.name",
    "enduser.id",
    "enduser.role",
)
LOG_CONTENT_CANARY_KEYS: Final = (
    "prompt",
    "prompt_text",
    "response",
    "tool_parameters",
    "tool_input",
    "error",
    "body",
    "body_ref",
    "hook_definitions",
    "workspace.host_paths",
)
SPAN_CONTENT_CANARY_KEYS: Final = (
    "user_prompt",
    "new_context",
    "system_reminders",
    "system_prompt_preview",
    "user_system_prompt",
    "response.model_output",
    "tool_input",
    "full_command",
    "file_path",
    "hook_definitions",
    "error",
)
# Events that only carry content: whole extra log records are added for them.
CONTENT_EVENT_NAMES: Final = ("api_request_body", "api_response_body", "system_prompt")
# A span event that only carries content, added to every span (not seen in the fixtures;
# its attribute names are a guess, which does not matter because the collector drops the
# whole event).
CONTENT_SPAN_EVENT: Final = "tool.output"
CONTENT_SPAN_EVENT_CANARY_KEYS: Final = ("output",)
# Host paths contain the OS username; they are planted on every signal and level, not
# only on logs (ADR 0021).
HOST_PATHS_CANARY_KEYS: Final = ("workspace.host_paths",)
# Every key planted at each level. Data point values are constant per key for the whole
# run, so they do not split series.
RESOURCE_CANARY_KEYS: Final = (*IDENTITY_CANARY_KEYS, *HOST_PATHS_CANARY_KEYS)
DATA_POINT_CANARY_KEYS: Final = (*IDENTITY_CANARY_KEYS, *HOST_PATHS_CANARY_KEYS)
LOG_RECORD_CANARY_KEYS: Final = (*IDENTITY_CANARY_KEYS, *LOG_CONTENT_CANARY_KEYS)
SPAN_CANARY_KEYS: Final = (
    *IDENTITY_CANARY_KEYS,
    *SPAN_CONTENT_CANARY_KEYS,
    *HOST_PATHS_CANARY_KEYS,
)
# The span events a scenario already has (gen_ai.request.attempt in the fixtures) are
# metadata that the collector keeps, deleting identity and host paths from them.
SPAN_EVENT_CANARY_KEYS: Final = (*IDENTITY_CANARY_KEYS, *HOST_PATHS_CANARY_KEYS)

# Hook canaries: values planted in every hook field the app must never store (ADR 0023),
# on every line. Each key gets a string canary, which replaces any value it had, except
# where a value holds metadata the app keeps next to content:
#   - tool_response keeps its status (async_launched starts a background subagent);
#   - effort, mcp_server and each background_tasks item keep their keys and get one
#     more, with the canary;
#   - error on StopFailure is an error code the app keeps; elsewhere it is free text;
#   - reason is kept on SessionEnd, and free text only on PermissionDenied.
# So the app's state comes out the same as without canaries.
HOOK_CONTENT_CANARY_KEYS: Final = (
    "prompt",
    "tool_input",
    "tool_calls",
    "last_assistant_message",
    "error_details",
    "message",
    "title",
    "requested_schema",
    "permission_suggestions",
    "custom_instructions",
    "compact_summary",
    "task_id",
    "task_subject",
    "task_description",
    "session_title",
    "transcript_path",
    "scratchpad_dir",
    "agent_transcript_path",
    "file_path",
    "url",
    "content",
    "globs",
    "trigger_file_path",
    "parent_file_path",
    "session_crons",
    "description",
    "command",
    # A key Claude Code does not send: unknown keys are dropped too.
    "x_ccobs_unknown",
)
# Objects whose other keys are kept as they are; the canary goes under this key.
HOOK_NESTED_CANARY_KEYS: Final = ("tool_response", "effort", "mcp_server", "background_tasks")
HOOK_NESTED_KEY: Final = "x_ccobs_canary"
# Keys that are free text on some events only (see above): error on every event but
# StopFailure, reason on PermissionDenied.
HOOK_ERROR_CODE_EVENT: Final = "StopFailure"
HOOK_REASON_TEXT_EVENT: Final = "PermissionDenied"
DEFAULT_HOOKS_ENDPOINT: Final = "http://127.0.0.1:4300"
HOOKS_PATH: Final = "/hooks"
# The app's only answer to a hook (ADR 0027).
HOOK_STATUS: Final = 204

# Export request layout per signal: resource list -> scope list -> item list.
_LAYOUT: Final[dict[Signal, tuple[str, str, str]]] = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}
_METRIC_KINDS: Final = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")
_ID_FIELDS: Final = frozenset({"traceId", "spanId", "parentSpanId"})
_DELTA: Final = 1
_CUMULATIVE: Final = 2
_NANOS_PER_SECOND: Final = 1_000_000_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ISO_TIMESTAMP: Final = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)


class RandomBits(Protocol):
    """A source of random bits; ``random.SystemRandom`` unless a caller passes its own."""

    def getrandbits(self, k: int, /) -> int: ...


class ReplayError(Exception):
    """A request the collector refused or could not receive, or an unusable endpoint."""


# Loading the fixtures.


@dataclass(frozen=True)
class Scenario:
    """One fixture scenario: its original session ID, export requests per signal and
    hook lines (``{"offset_ms", "transport", "payload"}`` objects, in file order)."""

    name: str
    session_id: str
    metrics: tuple[JSONObject, ...] = ()
    logs: tuple[JSONObject, ...] = ()
    traces: tuple[JSONObject, ...] = ()
    hooks: tuple[JSONObject, ...] = ()

    def requests(self, signal: Signal) -> tuple[JSONObject, ...]:
        """The export requests of ``signal``, in file order."""
        return {"metrics": self.metrics, "logs": self.logs, "traces": self.traces}[signal]


def _objects(value: JSON) -> list[JSONObject]:
    """The objects of a JSON list (anything else: none)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _manifest_scenarios(fixtures_dir: Path) -> list[JSONObject]:
    document = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))
    scenarios = document.get("scenarios") if isinstance(document, dict) else None
    if not isinstance(scenarios, list):
        raise ValueError(f"{fixtures_dir / 'manifest.json'} has no scenario list")
    return _objects(scenarios)


def _otlp_path(name: str, signal: Signal) -> str:
    return f"otlp/{name}/{signal}.jsonl"


def _files(entry: JSONObject) -> set[str]:
    files = entry.get("files")
    return {path for path in files if isinstance(path, str)} if isinstance(files, list) else set()


def _hooks_path(name: str) -> str:
    return f"hooks/{name}.jsonl"


def scenario_names(
    fixtures_dir: Path = DEFAULT_FIXTURES, *, with_otlp: bool = False, with_hooks: bool = False
) -> list[str]:
    """Scenario names in manifest order. With ``with_otlp`` or ``with_hooks``, only those
    that have OTLP data or hook lines; with both, those that have either."""
    names: list[str] = []
    for entry in _manifest_scenarios(fixtures_dir):
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        files = _files(entry)
        has_otlp = any(_otlp_path(name, signal) in files for signal in SIGNALS)
        has_hooks = _hooks_path(name) in files
        if (with_otlp or with_hooks) and not (
            (with_otlp and has_otlp) or (with_hooks and has_hooks)
        ):
            continue
        names.append(name)
    return names


def _read_objects(path: Path, what: str) -> tuple[JSONObject, ...]:
    objects: list[JSONObject] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{number}: expected {what} object")
        objects.append(item)
    return tuple(objects)


def _read_requests(path: Path) -> tuple[JSONObject, ...]:
    return _read_objects(path, "an OTLP export request")


def load_scenario(name: str, fixtures_dir: Path = DEFAULT_FIXTURES) -> Scenario:
    """The scenario ``name`` of the manifest; a hooks-only scenario has no requests, and
    a scenario without a hook fixture no hook lines."""
    entries = {entry.get("name"): entry for entry in _manifest_scenarios(fixtures_dir)}
    entry = entries.get(name)
    if entry is None:
        raise ValueError(f"unknown scenario {name!r} (not in {fixtures_dir / 'manifest.json'})")
    session_id = entry.get("session_id")
    if not isinstance(session_id, str):
        raise ValueError(f"scenario {name!r} has no session_id in the manifest")
    files = _files(entry)

    def read(signal: Signal) -> tuple[JSONObject, ...]:
        path = _otlp_path(name, signal)
        return _read_requests(fixtures_dir / path) if path in files else ()

    hooks_path = _hooks_path(name)
    hooks = _read_objects(fixtures_dir / hooks_path, "a hook line") if hooks_path in files else ()
    return Scenario(name, session_id, read("metrics"), read("logs"), read("traces"), hooks)


# Attributes and series.


def is_identity_key(key: str) -> bool:
    """Whether the collector deletes the attribute ``key`` as personal data (ADR 0021;
    see ``IDENTITY_KEYS``)."""
    return key in IDENTITY_KEYS or key.startswith(IDENTITY_PREFIXES)


def label_value(value: JSON) -> str:
    """An OTLP JSON AnyValue as text (every data point attribute in the fixtures is a
    string)."""
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("stringValue"), str):
        return str(value["stringValue"])
    if "boolValue" in value:
        return "true" if value["boolValue"] is True else "false"
    if "intValue" in value:
        return str(int(str(value["intValue"])))
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _labels(owner: JSONObject) -> dict[str, str]:
    """The attributes of ``owner`` as text, without identity attributes and data point
    canary keys (the collector always drops identity, and drops the content keys while
    content capture is off)."""
    labels: dict[str, str] = {}
    for attribute in _objects(owner.get("attributes")):
        key = attribute.get("key")
        if isinstance(key, str) and not is_identity_key(key) and key not in DATA_POINT_CANARY_KEYS:
            labels[key] = label_value(attribute.get("value"))
    return labels


@dataclass(frozen=True, order=True)
class SeriesKey:
    """A metric series as stored once the collector deletes identity attributes (and,
    with content capture off, ``workspace.host_paths``): the metric name and the data
    point attributes (OTel names), sorted by key."""

    metric: str
    labels: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, metric: str, labels: Mapping[str, str]) -> Self:
        """The key of ``metric`` with ``labels`` in any order."""
        return cls(metric, tuple(sorted(labels.items())))

    def label(self, key: str) -> str | None:
        """The value of the label ``key``, if the series has it."""
        return self.label_dict().get(key)

    def label_dict(self) -> dict[str, str]:
        """The labels as a dict."""
        return dict(self.labels)

    def matches(self, metric: str, labels: Mapping[str, str] | None = None) -> bool:
        """Whether this is a series of ``metric`` that has every label in ``labels``."""
        own = self.label_dict()
        return self.metric == metric and all(
            own.get(key) == value for key, value in (labels or {}).items()
        )


@dataclass(frozen=True)
class Prepared:
    """A scenario ready to send, with the totals its metric series should reach.

    ``expected`` maps each series (identity attributes left out) to the sum of its delta
    values, whatever the temporality sent. ``canary_values`` holds every planted value;
    each one starts with ``canary_marker`` and names where it was planted."""

    scenario: str
    session_id: str
    temporality: Temporality
    metrics: tuple[JSONObject, ...]
    logs: tuple[JSONObject, ...]
    traces: tuple[JSONObject, ...]
    expected: Mapping[SeriesKey, Number]
    canary_marker: str | None = None
    canary_values: frozenset[str] = frozenset()

    def requests(self, signal: Signal) -> tuple[JSONObject, ...]:
        """The export requests of ``signal``, in file order."""
        return {"metrics": self.metrics, "logs": self.logs, "traces": self.traces}[signal]

    def expected_series(
        self, metric: str, labels: Mapping[str, str] | None = None
    ) -> dict[SeriesKey, Number]:
        """The expected totals of the series of ``metric`` that have every label in
        ``labels``."""
        return {key: total for key, total in self.expected.items() if key.matches(metric, labels)}

    def expected_total(self, metric: str, labels: Mapping[str, str] | None = None) -> Number:
        """The summed expected totals of the matching series; ``KeyError`` if none match."""
        series = self.expected_series(metric, labels)
        if not series:
            raise KeyError(f"no expected series of {metric} with labels {dict(labels or {})}")
        return sum(series.values())

    def summary(self) -> JSONObject:
        """A JSON summary: session ID, request counts, canary marker and expected totals."""
        totals: list[JSON] = []
        for key, total in sorted(self.expected.items()):
            labels: JSONObject = dict(key.labels)
            totals.append({"metric": key.metric, "labels": labels, "total": total})
        requests: JSONObject = {signal: len(self.requests(signal)) for signal in SIGNALS}
        return {
            "scenario": self.scenario,
            "session_id": self.session_id,
            "temporality": self.temporality,
            "requests": requests,
            "canary_marker": self.canary_marker,
            "canary_value_count": len(self.canary_values),
            "expected_totals": totals,
        }


# Preparing a run.


def _nanos(value: JSON) -> int:
    """A ``*UnixNano`` value (a string in OTLP JSON, or a number) as an integer."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError(f"not a Unix nanosecond timestamp: {value!r}")
    return int(value)


def _unix_nanos(node: JSON) -> Iterator[int]:
    """Every ``*UnixNano`` value under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("UnixNano"):
                yield _nanos(value)
            else:
                yield from _unix_nanos(value)
    elif isinstance(node, list):
        for value in node:
            yield from _unix_nanos(value)


def _id_values(node: JSON) -> Iterator[str]:
    """Every trace and span ID under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _ID_FIELDS and isinstance(value, str):
                yield value
            else:
                yield from _id_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _id_values(value)


def shift_iso_timestamp(text: str, delta_ns: int) -> str:
    """``text`` (ISO 8601 with ``Z`` or an offset) moved by ``delta_ns``, in the same
    format: as many fraction digits (truncated) and the same suffix."""
    match = _ISO_TIMESTAMP.fullmatch(text)
    if match is None:
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}")
    whole, fraction, suffix = match.groups()
    moment = datetime.fromisoformat(whole + suffix)
    digits = len(fraction) if fraction else 0
    total = (moment - _EPOCH) // timedelta(seconds=1) * _NANOS_PER_SECOND
    total += int(fraction.ljust(9, "0")) if fraction else 0
    seconds, nanos = divmod(total + delta_ns, _NANOS_PER_SECOND)
    shifted = (_EPOCH + timedelta(seconds=seconds)).astimezone(moment.tzinfo or UTC)
    head = shifted.strftime("%Y-%m-%dT%H:%M:%S")
    if digits:
        head += "." + f"{nanos:09d}"[:digits]
    return head + suffix


def _iso_from_nanos(nanos: int) -> str:
    """Unix nanoseconds as Claude Code writes ``event.timestamp``: UTC, milliseconds."""
    seconds, rest = divmod(nanos, _NANOS_PER_SECOND)
    moment = _EPOCH + timedelta(seconds=seconds)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{rest // 1_000_000:03d}Z"


class _IdMap:
    """Fresh random hex IDs of the same length, one per original ID for the whole run."""

    def __init__(self, rng: RandomBits, originals: Iterable[str]) -> None:
        self._rng = rng
        self._new: dict[str, str] = {}
        self._taken = set(originals)

    def __call__(self, original: str) -> str:
        if not original:
            return original
        if original not in self._new:
            self._new[original] = self._fresh(len(original))
        return self._new[original]

    def _fresh(self, length: int) -> str:
        while True:
            candidate = f"{self._rng.getrandbits(4 * length):0{length}x}"
            # All zeros is an invalid ID; a taken value would merge two IDs or keep one.
            if candidate.strip("0") and candidate not in self._taken:
                self._taken.add(candidate)
                return candidate


@dataclass(frozen=True)
class _Rewrite:
    """Shifts timestamps, replaces the session ID and maps trace and span IDs in place."""

    delta_ns: int
    session_id: str
    ids: _IdMap

    def __call__(self, node: JSON) -> None:
        if isinstance(node, list):
            for value in node:
                self(value)
            return
        if not isinstance(node, dict):
            return
        self._attribute(node)
        for key, value in list(node.items()):
            if key.endswith("UnixNano"):
                node[key] = self._shift(value)
            elif key in _ID_FIELDS and isinstance(value, str):
                node[key] = self.ids(value)
            else:
                self(value)

    def _shift(self, value: JSON) -> JSON:
        nanos = _nanos(value)
        if nanos == 0:
            return value
        return str(nanos + self.delta_ns) if isinstance(value, str) else nanos + self.delta_ns

    def _attribute(self, node: JSONObject) -> None:
        key, value = node.get("key"), node.get("value")
        if not isinstance(value, dict):
            return
        if key == "session.id":
            node["value"] = {"stringValue": self.session_id}
        elif key == "event.timestamp" and isinstance(value.get("stringValue"), str):
            value["stringValue"] = shift_iso_timestamp(str(value["stringValue"]), self.delta_ns)


def _entries(request: JSONObject, signal: Signal) -> Iterator[tuple[JSONObject, JSONObject]]:
    """(resource entry, item) for every item of an export request, in order."""
    resources, scopes, items = _LAYOUT[signal]
    for entry in _objects(request.get(resources)):
        for scope in _objects(entry.get(scopes)):
            for item in _objects(scope.get(items)):
                yield entry, item


def _point_value(point: JSONObject) -> Number | None:
    as_int, as_double = point.get("asInt"), point.get("asDouble")
    if isinstance(as_int, str | int) and not isinstance(as_int, bool):
        return int(as_int)
    if isinstance(as_double, int | float) and not isinstance(as_double, bool):
        return as_double
    if isinstance(as_double, str):
        return float(as_double)
    return None


@dataclass(frozen=True)
class _DeltaPoint:
    resource: JSONObject
    scope: JSONObject
    metric: str
    data: JSONObject
    point: JSONObject


def _delta_sum_points(requests: Iterable[JSONObject]) -> Iterator[_DeltaPoint]:
    """The data points of every delta sum, in file order."""
    resources, scopes, metrics = _LAYOUT["metrics"]
    for request in requests:
        for entry in _objects(request.get(resources)):
            resource = entry.get("resource")
            for scope in _objects(entry.get(scopes)):
                for metric in _objects(scope.get(metrics)):
                    name, data = metric.get("name"), metric.get("sum")
                    if not isinstance(name, str) or not isinstance(data, dict):
                        continue
                    if data.get("aggregationTemporality") != _DELTA:
                        continue
                    for point in _objects(data.get("dataPoints")):
                        own = resource if isinstance(resource, dict) else {}
                        yield _DeltaPoint(own, scope, name, data, point)


def expected_totals(requests: Iterable[JSONObject]) -> Mapping[SeriesKey, Number]:
    """The sum of the delta values of each series in ``requests``.

    ``requests`` can be any subset of a run's metric requests, such as the halves sent
    around a collector restart; the totals of the parts add up to the run's. Keys leave
    out identity attributes and canary keys, as the collector stores them. Metrics that
    are not sums are skipped.

    Raises ``ValueError`` on a sum that is not delta: for cumulative input, the total of a
    subset is the last value of each series, not a sum of its points."""
    requests = list(requests)
    _require_delta(
        requests,
        "expected totals",
        "for cumulative sums, the total of a subset is the last value of each series",
    )
    totals: dict[SeriesKey, Number] = {}
    for found in _delta_sum_points(requests):
        value = _point_value(found.point)
        if value is not None:
            key = SeriesKey.of(found.metric, _labels(found.point))
            totals[key] = totals.get(key, 0) + value
    return MappingProxyType(totals)


def contiguous_series(
    first: Iterable[JSONObject], rest: Iterable[JSONObject]
) -> tuple[SeriesKey, ...]:
    """The series of ``first`` that go on in ``rest`` with no gap, sorted: their earliest
    point in ``rest`` starts at the time of their latest point in ``first``.

    ``first`` and ``rest`` are the parts of a run's metric requests sent around a
    collector restart. ``delta_to_cumulative`` then starts each continuing series again
    at its next point's start time; for these series that is the time of the last sample
    Prometheus stored, so Prometheus drops the new stream's zero sample as a duplicate,
    and if the new running total is not lower than the old one, ``increase(...
    anchored)`` loses the old total (ADR 0013). Keys are those of ``expected_totals``.

    Raises ``ValueError`` on a sum that is not delta: a cumulative stream keeps one start
    time for its whole life."""
    first, rest = list(first), list(rest)
    _require_delta(
        [*first, *rest],
        "contiguous series",
        "a cumulative stream keeps one start time for its whole life",
    )
    ends: dict[SeriesKey, int] = {}
    for found in _delta_sum_points(first):
        key = SeriesKey.of(found.metric, _labels(found.point))
        ends[key] = max(ends.get(key, 0), _nanos(found.point.get("timeUnixNano", 0)))
    # Per series of rest: (time, start time) of its earliest point.
    nexts: dict[SeriesKey, tuple[int, int]] = {}
    for found in _delta_sum_points(rest):
        key = SeriesKey.of(found.metric, _labels(found.point))
        point = (
            _nanos(found.point.get("timeUnixNano", 0)),
            _nanos(found.point.get("startTimeUnixNano", 0)),
        )
        if key not in nexts or point[0] < nexts[key][0]:
            nexts[key] = point
    return tuple(
        sorted(key for key, (_, start) in nexts.items() if key in ends and start == ends[key])
    )


def _require_delta(requests: Iterable[JSONObject], purpose: str, reason: str) -> None:
    """Raise ``ValueError`` if a sum in ``requests`` is not delta, naming ``purpose``
    and why it needs delta."""
    for request in requests:
        for _, metric in _entries(request, "metrics"):
            data = metric.get("sum")
            temporality = data.get("aggregationTemporality") if isinstance(data, dict) else _DELTA
            if temporality != _DELTA:
                raise ValueError(
                    f"{purpose} need delta sums, but {metric.get('name')} has "
                    f"aggregationTemporality {temporality} (2 is cumulative): {reason}"
                )


def _attribute_identity(owner: JSON) -> list[str]:
    attributes = owner.get("attributes") if isinstance(owner, dict) else None
    return sorted(json.dumps(item, sort_keys=True) for item in _objects(attributes))


def _to_cumulative(requests: Iterable[JSONObject]) -> None:
    """Turn every delta sum into running totals per series, in time order, in place.

    A series is a resource, a scope, a metric name and data point attributes. Each point
    keeps its end time and gets the series' first start time; gaps keep accumulating."""
    series: dict[str, list[tuple[int, int, JSONObject]]] = defaultdict(list)
    sums: list[JSONObject] = []
    for position, found in enumerate(_delta_sum_points(requests)):
        identity = json.dumps(
            [
                _attribute_identity(found.resource),
                json.dumps(found.scope.get("scope"), sort_keys=True),
                found.metric,
                _attribute_identity(found.point),
            ]
        )
        end = _nanos(found.point.get("timeUnixNano", 0))
        series[identity].append((end, position, found.point))
        sums.append(found.data)
    for points in series.values():
        points.sort(key=lambda item: (item[0], item[1]))
        start = points[0][2].get("startTimeUnixNano")
        running: Number = 0
        for _, _, point in points:
            value = _point_value(point)
            if value is None:
                continue
            running += value
            if "asInt" in point:
                point["asInt"] = str(int(running))
            else:
                point["asDouble"] = running
            if start is not None:
                point["startTimeUnixNano"] = start
    for data in sums:
        data["aggregationTemporality"] = _CUMULATIVE


def _set_attribute(owner: JSONObject, key: str, value: str) -> None:
    """Set the string attribute ``key`` of ``owner``, replacing any value it had."""
    attributes = owner.get("attributes")
    if not isinstance(attributes, list):
        attributes = []
        owner["attributes"] = attributes
    for attribute in _objects(attributes):
        if attribute.get("key") == key:
            attribute["value"] = {"stringValue": value}
            return
    attributes.append({"key": key, "value": {"stringValue": value}})


def _record_time(record: JSONObject) -> int:
    for field in ("timeUnixNano", "observedTimeUnixNano"):
        nanos = _nanos(record.get(field, 0))
        if nanos:
            return nanos
    return 0


def _add_content_events(logs: Iterable[JSONObject], session_id: str) -> None:
    """Append one record per content event to the first scope that has log records, at
    the time of its last record."""
    resources, scopes, items = _LAYOUT["logs"]
    for request in logs:
        for entry in _objects(request.get(resources)):
            for scope in _objects(entry.get(scopes)):
                records = scope.get(items)
                existing = _objects(records)
                if not isinstance(records, list) or not existing:
                    continue
                nanos = _record_time(existing[-1])
                for name in CONTENT_EVENT_NAMES:
                    record: JSONObject = {
                        "timeUnixNano": str(nanos),
                        "observedTimeUnixNano": str(nanos),
                        "body": {"stringValue": f"claude_code.{name}"},
                        "attributes": [],
                    }
                    _set_attribute(record, "session.id", session_id)
                    _set_attribute(record, "event.name", name)
                    _set_attribute(record, "event.timestamp", _iso_from_nanos(nanos))
                    records.append(record)
                return


def _plant_canaries(requests: Mapping[Signal, list[JSONObject]], marker: str) -> frozenset[str]:
    """Plant canary values in place; return every value planted."""
    planted: set[str] = set()

    def plant(owner: JSONObject, place: str, keys: Iterable[str]) -> None:
        for key in keys:
            value = f"{marker}:{place}:{key}"
            planted.add(value)
            _set_attribute(owner, key, value)

    for signal in SIGNALS:
        for request in requests[signal]:
            for entry in _objects(request.get(_LAYOUT[signal][0])):
                resource = entry.get("resource")
                if not isinstance(resource, dict):
                    resource = {}
                    entry["resource"] = resource
                plant(resource, f"{signal}.resource", RESOURCE_CANARY_KEYS)
    for request in requests["metrics"]:
        for _, metric in _entries(request, "metrics"):
            for kind in _METRIC_KINDS:
                data = metric.get(kind)
                for point in _objects(data.get("dataPoints") if isinstance(data, dict) else None):
                    # One value per key for every point, so series do not split.
                    plant(point, "metrics.datapoint", DATA_POINT_CANARY_KEYS)
    records = [record for request in requests["logs"] for _, record in _entries(request, "logs")]
    for number, record in enumerate(records):
        plant(record, f"logs.record{number}", LOG_RECORD_CANARY_KEYS)
    spans = [span for request in requests["traces"] for _, span in _entries(request, "traces")]
    for number, span in enumerate(spans):
        plant(span, f"traces.span{number}", SPAN_CANARY_KEYS)
        for index, sent in enumerate(_objects(span.get("events"))):
            plant(sent, f"traces.span{number}.event{index}", SPAN_EVENT_CANARY_KEYS)
        event: JSONObject = {
            "timeUnixNano": span.get("startTimeUnixNano", "0"),
            "name": CONTENT_SPAN_EVENT,
            "attributes": [],
        }
        plant(event, f"traces.span{number}.{CONTENT_SPAN_EVENT}", CONTENT_SPAN_EVENT_CANARY_KEYS)
        events = span.get("events")
        if isinstance(events, list):
            events.append(event)
        else:
            span["events"] = [event]
    return frozenset(planted)


def fresh_session_id(rng: RandomBits | None = None) -> str:
    """A new UUID4 session ID, from ``rng`` (``random.SystemRandom`` by default). Pass
    one value to ``prepare`` and ``prepare_hooks`` so that a scenario's telemetry and
    hooks share it."""
    source: RandomBits = rng if rng is not None else random.SystemRandom()
    return str(uuid.UUID(int=source.getrandbits(128), version=4))


def _new_marker(rng: RandomBits) -> str:
    return f"{CANARY_PREFIX}{rng.getrandbits(64):016x}"


def prepare(
    scenario: Scenario,
    *,
    now_ns: int,
    session_id: str | None = None,
    temporality: Temporality = "delta",
    canaries: bool = False,
    rng: RandomBits | None = None,
) -> Prepared:
    """Fresh requests for ``scenario``, which is left untouched.

    The latest timestamp of all three signals moves to ``now_ns``, the session ID becomes
    ``session_id`` (a new UUID4 by default) and trace and span IDs are replaced. With
    ``canaries``, identity and content canaries are planted (see the ``*_CANARY_KEYS``
    constants and ``CONTENT_EVENT_NAMES``)."""
    source: RandomBits = rng if rng is not None else random.SystemRandom()
    if session_id is None:
        session_id = fresh_session_id(source)
    requests: dict[Signal, list[JSONObject]] = {
        signal: copy.deepcopy(list(scenario.requests(signal))) for signal in SIGNALS
    }
    everything = [request for signal in SIGNALS for request in requests[signal]]
    times = [nanos for request in everything for nanos in _unix_nanos(request) if nanos]
    originals = {value for request in everything for value in _id_values(request)}
    rewrite = _Rewrite(
        delta_ns=now_ns - max(times) if times else 0,
        session_id=session_id,
        ids=_IdMap(source, originals),
    )
    for request in everything:
        rewrite(request)
    expected = expected_totals(requests["metrics"])
    marker: str | None = None
    planted: frozenset[str] = frozenset()
    if canaries:
        marker = _new_marker(source)
        _add_content_events(requests["logs"], session_id)
        planted = _plant_canaries(requests, marker)
    if temporality == "cumulative":
        _to_cumulative(requests["metrics"])
    return Prepared(
        scenario=scenario.name,
        session_id=session_id,
        temporality=temporality,
        metrics=tuple(requests["metrics"]),
        logs=tuple(requests["logs"]),
        traces=tuple(requests["traces"]),
        expected=expected,
        canary_marker=marker,
        canary_values=planted,
    )


# Preparing the hooks.


@dataclass(frozen=True)
class HookLine:
    """One hook of a scenario: how long after the scenario's first hook Claude Code sent
    it (``offset_ms``), through which transport (``http``, or ``command`` for the async
    SessionStart hook, which posts the same JSON to the same route; ADR 0011), and the
    payload to POST."""

    offset_ms: int
    transport: str
    payload: JSONObject


@dataclass(frozen=True)
class PreparedHooks:
    """A scenario's hook lines ready to POST, in file order. ``canary_values`` holds
    every planted value; each one starts with ``canary_marker`` and names its place."""

    scenario: str
    session_id: str
    lines: tuple[HookLine, ...]
    canary_marker: str | None = None
    canary_values: frozenset[str] = frozenset()

    def summary(self) -> JSONObject:
        """A JSON summary: session ID, line count and canary marker."""
        return {
            "scenario": self.scenario,
            "session_id": self.session_id,
            "hook_lines": len(self.lines),
            "canary_marker": self.canary_marker,
            "canary_value_count": len(self.canary_values),
        }


def _hook_line(item: JSONObject, where: str) -> tuple[int, str, JSONObject]:
    offset, transport, payload = item.get("offset_ms"), item.get("transport"), item.get("payload")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not isinstance(transport, str)
        or not isinstance(payload, dict)
    ):
        raise ValueError(f"{where}: expected an integer offset_ms, a transport and a payload")
    return offset, transport, payload


def _replace_text(node: JSON, old: str, new: str) -> JSON:
    """``node`` with ``old`` replaced by ``new`` in every string value (keys stay)."""
    if isinstance(node, str):
        return node.replace(old, new)
    if isinstance(node, list):
        return [_replace_text(value, old, new) for value in node]
    if isinstance(node, dict):
        return _replace_in_object(node, old, new)
    return node


def _replace_in_object(node: JSONObject, old: str, new: str) -> JSONObject:
    return {key: _replace_text(value, old, new) for key, value in node.items()}


def _plant_hook_canaries(payload: JSONObject, marker: str, place: str, planted: set[str]) -> None:
    """Plant canaries in ``payload`` in place (see ``HOOK_CONTENT_CANARY_KEYS``)."""

    def canary(key: str) -> str:
        value = f"{marker}:{place}:{key}"
        planted.add(value)
        return value

    event = payload.get("hook_event_name")
    for key in HOOK_CONTENT_CANARY_KEYS:
        payload[key] = canary(key)
    if event != HOOK_ERROR_CODE_EVENT:
        payload["error"] = canary("error")
    if event == HOOK_REASON_TEXT_EVENT:
        payload["reason"] = canary("reason")
    for key in HOOK_NESTED_CANARY_KEYS:
        current = payload.get(key)
        if key == "background_tasks":
            # Only the tasks that are there: a new task would change the app's state.
            if isinstance(current, list):
                payload[key] = [
                    {**item, HOOK_NESTED_KEY: canary(f"{key}.{index}")}
                    if isinstance(item, dict)
                    else item
                    for index, item in enumerate(current)
                ]
        elif isinstance(current, dict):
            payload[key] = {**current, HOOK_NESTED_KEY: canary(key)}
        else:
            payload[key] = {HOOK_NESTED_KEY: canary(key)}


def prepare_hooks(
    scenario: Scenario,
    *,
    session_id: str | None = None,
    canaries: bool = False,
    canary_marker: str | None = None,
    rng: RandomBits | None = None,
) -> PreparedHooks:
    """Fresh hook payloads for ``scenario``, which is left untouched.

    The scenario's session ID becomes ``session_id`` (a new UUID4 by default) in every
    string of every payload, so that ``transcript_path`` and the like follow it; pass the
    ``session_id`` of ``prepare``'s result to share it with the telemetry. With
    ``canaries``, a canary goes in every content field (``HOOK_CONTENT_CANARY_KEYS``),
    under ``canary_marker`` (a new one by default; pass ``prepare``'s to search both with
    one marker). Offsets and transports stay as they were. Raises ``ValueError`` on a
    malformed line, or on a payload whose ``session_id`` is not the scenario's (it would
    keep its own ID, which the replacement above misses)."""
    source: RandomBits = rng if rng is not None else random.SystemRandom()
    if session_id is None:
        session_id = fresh_session_id(source)
    marker = (canary_marker or _new_marker(source)) if canaries else None
    planted: set[str] = set()
    lines: list[HookLine] = []
    for number, item in enumerate(scenario.hooks):
        where = f"{scenario.name} hook line {number + 1}"
        offset, transport, payload = _hook_line(item, where)
        if "session_id" in payload and payload["session_id"] != scenario.session_id:
            raise ValueError(f"{where}: the payload's session_id is not the scenario's")
        fresh = (
            _replace_in_object(payload, scenario.session_id, session_id)
            if scenario.session_id
            else copy.deepcopy(payload)
        )
        fresh["session_id"] = session_id
        if marker is not None:
            _plant_hook_canaries(fresh, marker, f"hooks.line{number}", planted)
        lines.append(HookLine(offset, transport, fresh))
    return PreparedHooks(
        scenario=scenario.name,
        session_id=session_id,
        lines=tuple(lines),
        canary_marker=marker,
        canary_values=frozenset(planted),
    )


# Sending.

# The partialSuccess fields of the OTLP export responses, per signal.
_REJECTED: Final = (
    ("rejectedDataPoints", "data points"),
    ("rejectedLogRecords", "log records"),
    ("rejectedSpans", "spans"),
)


def _bounded(text: str) -> str:
    return text if len(text) <= MAX_ERROR_BODY else text[:MAX_ERROR_BODY] + "..."


def _count(value: JSON) -> int:
    """An OTLP JSON int64 (a string, or a number) as an integer; anything else: 0."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _rejections(reply: bytes) -> str | None:
    """What a 2xx reply's ``partialSuccess`` says the collector rejected, if anything.

    An empty or non-JSON body, ``{}`` and a ``partialSuccess`` without rejected items
    (even with a warning in ``errorMessage``) mean full success."""
    try:
        document = json.loads(reply) if reply.strip() else None
    except ValueError:
        return None
    partial = document.get("partialSuccess") if isinstance(document, dict) else None
    if not isinstance(partial, dict):
        return None
    counts = [
        f"{count} {noun}" for field, noun in _REJECTED if (count := _count(partial.get(field)))
    ]
    if not counts:
        return None
    message = partial.get("errorMessage")
    detail = f": {_bounded(message)}" if isinstance(message, str) and message else ""
    return f"rejected {', '.join(counts)}{detail}"


def _post_json(
    endpoint: str, suffix: str, document: JSONObject, *, timeout: float
) -> tuple[str, int, bytes]:
    """POST ``document`` as JSON to ``{endpoint}{suffix}`` on a connection of its own;
    return the URL, the status and the start of the body. Raises ``ReplayError`` when
    the server cannot be reached or does not answer within ``timeout``, or when
    ``endpoint`` is not http(s)."""
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError as error:
        raise ReplayError(f"invalid endpoint {endpoint!r}: {error}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReplayError(f"the endpoint must be an http:// or https:// URL, got {endpoint!r}")
    path = f"{parts.path.rstrip('/')}{suffix}"
    url = f"{parts.scheme}://{parts.netloc}{path}"
    connection_type = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parts.hostname, port, timeout=timeout)
    body = json.dumps(document, separators=(",", ":")).encode()
    try:
        try:
            connection.connect()
        except OSError as error:
            raise ReplayError(f"could not reach {url}: {error}") from error
        try:
            headers = {"Content-Type": "application/json"}
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            status, reply = response.status, response.read(_MAX_REPLY_BYTES)
        except (OSError, http.client.HTTPException) as error:
            raise ReplayError(f"POST {url} got no answer: {error}") from error
    finally:
        connection.close()
    return url, status, reply


def post_otlp(
    endpoint: str, signal: Signal, request: JSONObject, *, timeout: float = DEFAULT_TIMEOUT
) -> int:
    """POST one export request as JSON to ``{endpoint}/v1/{signal}``; return the status.

    Raises ``ReplayError`` on a non-2xx answer (with the status and the start of the
    body), on a 2xx answer whose ``partialSuccess`` reports rejected data points, log
    records or spans, when the collector cannot be reached or does not answer within
    ``timeout``, or when ``endpoint`` is not http(s)."""
    url, status, reply = _post_json(endpoint, f"/v1/{signal}", request, timeout=timeout)
    if not 200 <= status < 300:
        text = _bounded(reply.decode("utf-8", errors="replace"))
        raise ReplayError(f"POST {url} answered {status}: {text}")
    rejected = _rejections(reply)
    if rejected is not None:
        raise ReplayError(f"POST {url} answered {status} but the collector {rejected}")
    return status


def send(
    endpoint: str,
    prepared: Prepared,
    *,
    signals: Iterable[Signal] = SIGNALS,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """POST every request of ``signals``, signal by signal, in file order; return how
    many were sent. Stops at the first ``ReplayError``."""
    sent = 0
    for signal in signals:
        for request in prepared.requests(signal):
            post_otlp(endpoint, signal, request, timeout=timeout)
            sent += 1
    return sent


def post_hook(endpoint: str, payload: JSONObject, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """POST one hook payload as JSON to ``{endpoint}/hooks``, the app's hook route.

    Raises ``ReplayError`` unless the answer is the app's neutral one, a 204 with an empty
    body (ADR 0027), and as ``post_otlp`` does for an unreachable or silent server or an
    endpoint that is not http(s)."""
    url, status, reply = _post_json(endpoint, HOOKS_PATH, payload, timeout=timeout)
    if status != HOOK_STATUS or reply:
        text = _bounded(reply.decode("utf-8", errors="replace"))
        raise ReplayError(
            f"POST {url} answered {status} with a {len(reply)}-byte body, not an empty "
            f"{HOOK_STATUS}: {text}"
        )


def send_hooks(
    endpoint: str,
    lines: Iterable[HookLine],
    *,
    speed: float | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], object] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> list[float]:
    """POST ``lines`` (a ``PreparedHooks``'s, or a part of them) in order, each on a
    connection of its own; return how long each POST took, in seconds, connecting
    included. Stops at the first ``ReplayError``.

    By default the lines go back to back: the app stamps each hook with its own receive
    time and its state machine depends only on the order (ADRs 0024 and 0027), so the
    real gaps (the longest scenario spans more than two minutes) would add only wall
    time. With ``speed``, each line waits until ``offset_ms / speed`` after the first one
    went out (``1.0`` is real time, ``10.0`` ten times faster), for what does depend on
    time, such as the reconciler's grace period or a live view. ``sleep`` defaults to
    ``time.sleep`` and ``monotonic`` to ``time.perf_counter``, a monotonic clock with
    sub-millisecond resolution (``time.monotonic`` ticks every 15.6 ms on Windows)."""
    if speed is not None and not speed > 0:
        raise ValueError(f"speed must be positive, got {speed}")
    sleep = sleep or time.sleep
    monotonic = monotonic or time.perf_counter
    durations: list[float] = []
    started: float | None = None
    first_ms = 0
    for line in lines:
        if started is None:
            started, first_ms = monotonic(), line.offset_ms
        elif speed is not None:
            delay = started + (line.offset_ms - first_ms) / 1000 / speed - monotonic()
            if delay > 0:
                sleep(delay)
        before = monotonic()
        post_hook(endpoint, line.payload, timeout=timeout)
        durations.append(monotonic() - before)
    return durations


# Command line.


@dataclass(frozen=True)
class _Run:
    """One scenario of the command line: its telemetry and, if asked, its hooks."""

    telemetry: Prepared
    hooks: PreparedHooks | None

    def summary(self, *, otlp: bool) -> JSONObject:
        summary = self.telemetry.summary()
        if not otlp:
            summary["requests"] = dict.fromkeys(SIGNALS, 0)
        if self.hooks is not None:
            # The hooks' own marker when no telemetry canary was planted (--hooks-only).
            summary["canary_marker"] = self.telemetry.canary_marker or self.hooks.canary_marker
            summary["hook_lines"] = len(self.hooks.lines)
            summary["hook_canary_value_count"] = len(self.hooks.canary_values)
        return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare and send scenarios, then print a JSON summary of the scenarios sent (all
    of them with ``--dry-run``). With ``--hooks-endpoint``, each scenario's hooks go to
    the app first, with the session ID of its telemetry. On a send error, stop: the
    summary also names the failed scenario (possibly sent in part), one line goes to
    stderr, and the result is 1."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.stack.replay",
        description=(
            "Replay the OTLP fixtures to an OpenTelemetry Collector (OTLP/HTTP JSON), and "
            "optionally the hook fixtures to the app."
        ),
    )
    parser.add_argument(
        "--endpoint",
        help=f"OTLP/HTTP base URL ({DEFAULT_ENDPOINT}); not with --hooks-only",
    )
    parser.add_argument(
        "--hooks-endpoint",
        metavar="URL",
        help=f"also POST the hooks to the app at this base URL (such as {DEFAULT_HOOKS_ENDPOINT})",
    )
    parser.add_argument(
        "--hooks-only", action="store_true", help="send the hooks only, no telemetry"
    )
    parser.add_argument(
        "--speed",
        type=float,
        metavar="FACTOR",
        help="pace the hooks at offset_ms / FACTOR (1 is real time; default: back to back)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=scenario_names(),
        metavar="NAME",
        help=(
            "scenario to replay, repeatable (default: every scenario with data to send: "
            "OTLP data, hooks, or either)"
        ),
    )
    parser.add_argument("--temporality", choices=TEMPORALITIES, default="delta")
    parser.add_argument("--canaries", action="store_true", help="plant privacy canaries")
    parser.add_argument("--dry-run", action="store_true", help="prepare and print, send nothing")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per POST")
    args = parser.parse_args(argv)
    hooks_endpoint: str | None = args.hooks_endpoint
    if hooks_endpoint is None:
        for flag, given in (("--hooks-only", args.hooks_only), ("--speed", args.speed is not None)):
            if given:
                parser.error(f"{flag} needs --hooks-endpoint")
    if args.speed is not None and not args.speed > 0:
        parser.error(f"--speed must be positive, got {args.speed}")
    if args.hooks_only and args.endpoint is not None:
        parser.error("--endpoint does not go with --hooks-only, which sends no telemetry")
    otlp = not args.hooks_only
    endpoint: str = args.endpoint or DEFAULT_ENDPOINT

    names: list[str] = args.scenario or scenario_names(
        with_otlp=otlp, with_hooks=hooks_endpoint is not None
    )
    temporality: Temporality = "cumulative" if args.temporality == "cumulative" else "delta"
    now_ns = time.time_ns()
    runs: list[_Run] = []
    for name in names:
        scenario = load_scenario(name)
        telemetry = prepare(
            scenario, now_ns=now_ns, temporality=temporality, canaries=args.canaries and otlp
        )
        hooks = (
            prepare_hooks(
                scenario,
                session_id=telemetry.session_id,
                canaries=args.canaries,
                canary_marker=telemetry.canary_marker,
            )
            if hooks_endpoint is not None
            else None
        )
        runs.append(_Run(telemetry, hooks))
    done = list(runs) if args.dry_run else []
    failed: JSONObject | None = None
    if not args.dry_run:
        for run in runs:
            try:
                if run.hooks is not None and hooks_endpoint is not None:
                    send_hooks(
                        hooks_endpoint, run.hooks.lines, speed=args.speed, timeout=args.timeout
                    )
                if otlp:
                    send(endpoint, run.telemetry, timeout=args.timeout)
            except ReplayError as error:
                scenario_name = run.telemetry.scenario
                print(f"replay failed: {scenario_name}: {error}", file=sys.stderr)
                failed = {"scenario": scenario_name, "session_id": run.telemetry.session_id}
                failed["error"] = str(error)
                break
            done.append(run)
    output: JSONObject = {
        "endpoint": endpoint if otlp else None,
        "hooks_endpoint": hooks_endpoint,
        "sent": not args.dry_run,
        "now_ns": now_ns,
        "scenarios": [run.summary(otlp=otlp) for run in done],
        "failed": failed,
    }
    print(json.dumps(output, indent=2))
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
