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
``send`` deliver the requests. Run it from the repository root::

    uv run python -m tools.stack.replay --endpoint http://127.0.0.1:4318 --dry-run

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
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
# A span event that only carries content (not seen in the fixtures; its attribute names
# are a guess, which does not matter because the collector drops the whole event).
CONTENT_SPAN_EVENT: Final = "tool.output"
SPAN_EVENT_CANARY_KEYS: Final = ("output",)
# Host paths contain the OS username; they are planted on every signal, not only on logs.
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
    """One fixture scenario: its original session ID and export requests per signal."""

    name: str
    session_id: str
    metrics: tuple[JSONObject, ...] = ()
    logs: tuple[JSONObject, ...] = ()
    traces: tuple[JSONObject, ...] = ()

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


def scenario_names(fixtures_dir: Path = DEFAULT_FIXTURES, *, with_otlp: bool = False) -> list[str]:
    """Scenario names in manifest order; with ``with_otlp``, only those with OTLP data."""
    names: list[str] = []
    for entry in _manifest_scenarios(fixtures_dir):
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if with_otlp and not any(_otlp_path(name, signal) in _files(entry) for signal in SIGNALS):
            continue
        names.append(name)
    return names


def _read_requests(path: Path) -> tuple[JSONObject, ...]:
    requests: list[JSONObject] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError(f"{path}:{number}: expected an OTLP export request object")
        requests.append(request)
    return tuple(requests)


def load_scenario(name: str, fixtures_dir: Path = DEFAULT_FIXTURES) -> Scenario:
    """The scenario ``name`` of the manifest; a hooks-only scenario has no requests."""
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

    return Scenario(name, session_id, read("metrics"), read("logs"), read("traces"))


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
        event: JSONObject = {
            "timeUnixNano": span.get("startTimeUnixNano", "0"),
            "name": CONTENT_SPAN_EVENT,
            "attributes": [],
        }
        plant(event, f"traces.span{number}.event", SPAN_EVENT_CANARY_KEYS)
        events = span.get("events")
        if isinstance(events, list):
            events.append(event)
        else:
            span["events"] = [event]
    return frozenset(planted)


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
        session_id = str(uuid.UUID(int=source.getrandbits(128), version=4))
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
        marker = f"{CANARY_PREFIX}{source.getrandbits(64):016x}"
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


def post_otlp(
    endpoint: str, signal: Signal, request: JSONObject, *, timeout: float = DEFAULT_TIMEOUT
) -> int:
    """POST one export request as JSON to ``{endpoint}/v1/{signal}``; return the status.

    Raises ``ReplayError`` on a non-2xx answer (with the status and the start of the
    body), on a 2xx answer whose ``partialSuccess`` reports rejected data points, log
    records or spans, when the collector cannot be reached or does not answer within
    ``timeout``, or when ``endpoint`` is not http(s)."""
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError as error:
        raise ReplayError(f"invalid endpoint {endpoint!r}: {error}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReplayError(f"the endpoint must be an http:// or https:// URL, got {endpoint!r}")
    path = f"{parts.path.rstrip('/')}/v1/{signal}"
    url = f"{parts.scheme}://{parts.netloc}{path}"
    connection_type = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parts.hostname, port, timeout=timeout)
    body = json.dumps(request, separators=(",", ":")).encode()
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


# Command line.


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare and send scenarios, then print a JSON summary of the scenarios sent (all
    of them with ``--dry-run``). On a send error, stop: the summary also names the failed
    scenario (possibly sent in part), one line goes to stderr, and the result is 1."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.stack.replay",
        description="Replay the OTLP fixtures to an OpenTelemetry Collector (OTLP/HTTP JSON).",
    )
    parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help=f"OTLP/HTTP base URL ({DEFAULT_ENDPOINT})"
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=scenario_names(),
        metavar="NAME",
        help="scenario to replay, repeatable (default: every scenario with OTLP data)",
    )
    parser.add_argument("--temporality", choices=TEMPORALITIES, default="delta")
    parser.add_argument("--canaries", action="store_true", help="plant privacy canaries")
    parser.add_argument("--dry-run", action="store_true", help="prepare and print, send nothing")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per POST")
    args = parser.parse_args(argv)

    names: list[str] = args.scenario or scenario_names(with_otlp=True)
    temporality: Temporality = "cumulative" if args.temporality == "cumulative" else "delta"
    now_ns = time.time_ns()
    runs = [
        prepare(load_scenario(name), now_ns=now_ns, temporality=temporality, canaries=args.canaries)
        for name in names
    ]
    done = list(runs) if args.dry_run else []
    failed: JSONObject | None = None
    if not args.dry_run:
        for run in runs:
            try:
                send(args.endpoint, run, timeout=args.timeout)
            except ReplayError as error:
                print(f"replay failed: {run.scenario}: {error}", file=sys.stderr)
                failed = {"scenario": run.scenario, "session_id": run.session_id}
                failed["error"] = str(error)
                break
            done.append(run)
    output: JSONObject = {
        "endpoint": args.endpoint,
        "sent": not args.dry_run,
        "now_ns": now_ns,
        "scenarios": [run.summary() for run in done],
        "failed": failed,
    }
    print(json.dumps(output, indent=2))
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
