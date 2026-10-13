"""Inspect raw capture output: telemetry shapes and hook timings.

- ``summary``: metric names, units, kinds, temporality, attribute keys and a few
  low-cardinality values; event names and attribute keys; span names, attribute keys
  and agent ids.
- ``hook-timings label=session_id ...``: the ``hook_execution_complete`` events of each
  labelled session.

Run it from the repository root with ``uv run python -m tools.capture.inspect_capture``.
Its output can include values from the raw capture; do not paste it anywhere public.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tools.capture.jsontypes import JSON, JSONObject

DEFAULT_RAW: Final = Path(__file__).resolve().parent / "out"
# Metric attributes whose values are listed: low cardinality and no personal data.
LISTED_VALUES: Final = (
    "type",
    "query_source",
    "model",
    "agent.name",
    "decision",
    "source",
    "tool_name",
    "start_type",
    "language",
    "speed",
    "effort",
    "skill.name",
)
_METRIC_KINDS: Final = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


def _objects(value: JSON) -> list[JSONObject]:
    """The objects of a JSON list (anything else: none)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def any_value(value: JSON) -> object:
    """Python value of an OTLP JSON AnyValue."""
    if not isinstance(value, dict):
        return None
    array, kvlist = value.get("arrayValue"), value.get("kvlistValue")
    if isinstance(array, dict):
        values = array.get("values")
        return [any_value(item) for item in values] if isinstance(values, list) else []
    if isinstance(kvlist, dict):
        return attributes(kvlist.get("values"))
    if "intValue" in value:
        number = value["intValue"]
        return int(number) if isinstance(number, str | int) else None
    for key in ("stringValue", "boolValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    return None


def attributes(items: JSON) -> dict[str, object]:
    """OTLP attribute list as a dict."""
    return {
        key: any_value(item.get("value"))
        for item in _objects(items)
        if isinstance(key := item.get("key"), str)
    }


def read_jsonl(path: Path) -> list[JSON]:
    """Export requests from a collector file exporter output (missing file: none)."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _items(
    docs: Iterable[JSON], resource_key: str, scope_key: str, item_key: str
) -> Iterator[tuple[JSONObject, JSONObject, JSONObject]]:
    """Every (resource, scope, item) of one signal."""
    for doc in docs:
        resources = doc.get(resource_key) if isinstance(doc, dict) else None
        for resource in _objects(resources):
            for scope in _objects(resource.get(scope_key)):
                for item in _objects(scope.get(item_key)):
                    yield resource, scope, item


@dataclass
class MetricShape:
    """What one metric looked like across the capture."""

    unit: str | None = None
    kind: str | None = None
    temporality: set[int] = field(default_factory=set)
    monotonic: bool | None = None
    keys: set[str] = field(default_factory=set)
    values: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


@dataclass
class EventShape:
    """What one event (log record) looked like across the capture."""

    count: int = 0
    keys: set[str] = field(default_factory=set)
    traced: bool = False


@dataclass
class SpanShape:
    """What one span looked like across the capture."""

    count: int = 0
    keys: set[str] = field(default_factory=set)
    agents: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class HookTiming:
    """One ``hook_execution_complete`` event."""

    hook_event: str
    total_ms: int
    hooks: int
    success: int
    non_blocking_error: int
    cancelled: int


def _resource_keys(docs: Iterable[JSON], resource_key: str) -> set[str]:
    keys: set[str] = set()
    for doc in docs:
        resources = doc.get(resource_key) if isinstance(doc, dict) else None
        for resource in _objects(resources):
            header = resource.get("resource")
            keys |= attributes(
                header.get("attributes") if isinstance(header, dict) else None
            ).keys()
    return keys


def metric_shapes(docs: Iterable[JSON]) -> dict[str, MetricShape]:
    """Shape of every metric, by name."""
    shapes: defaultdict[str, MetricShape] = defaultdict(MetricShape)
    for _, _, metric in _items(docs, "resourceMetrics", "scopeMetrics", "metrics"):
        shape = shapes[str(metric.get("name"))]
        unit = metric.get("unit")
        shape.unit = unit if isinstance(unit, str) else None
        for kind in _METRIC_KINDS:
            data = metric.get(kind)
            if not isinstance(data, dict):
                continue
            shape.kind = kind
            temporality, monotonic = data.get("aggregationTemporality"), data.get("isMonotonic")
            if isinstance(temporality, int):
                shape.temporality.add(temporality)
            if isinstance(monotonic, bool):
                shape.monotonic = monotonic
            for point in _objects(data.get("dataPoints")):
                values = attributes(point.get("attributes"))
                shape.keys |= values.keys()
                for key in LISTED_VALUES:
                    if key in values:
                        shape.values[key].add(str(values[key]))
    return dict(shapes)


def event_shapes(docs: Iterable[JSON]) -> dict[str, EventShape]:
    """Shape of every event, by ``event.name``."""
    shapes: defaultdict[str, EventShape] = defaultdict(EventShape)
    for _, _, record in _items(docs, "resourceLogs", "scopeLogs", "logRecords"):
        values = attributes(record.get("attributes"))
        shape = shapes[str(values.get("event.name", record.get("eventName")))]
        shape.count += 1
        shape.keys |= values.keys()
        shape.traced = shape.traced or bool(record.get("traceId"))
    return dict(shapes)


def span_shapes(docs: Iterable[JSON]) -> dict[str, SpanShape]:
    """Shape of every span, by name, with the agent ids seen on it."""
    shapes: defaultdict[str, SpanShape] = defaultdict(SpanShape)
    for _, _, span in _items(docs, "resourceSpans", "scopeSpans", "spans"):
        values = attributes(span.get("attributes"))
        shape = shapes[str(span.get("name"))]
        shape.count += 1
        shape.keys |= values.keys()
        if "agent_id" in values or "parent_agent_id" in values:
            shape.agents.add((str(values.get("agent_id")), str(values.get("parent_agent_id"))))
    return dict(shapes)


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, int | float | str) else 0


def hook_timings(docs: Iterable[JSON], sessions: Mapping[str, str]) -> dict[str, list[HookTiming]]:
    """``hook_execution_complete`` events per label, for ``{label: session_id}``."""
    labels = {session: label for label, session in sessions.items()}
    rows: dict[str, list[HookTiming]] = {label: [] for label in sessions}
    for _, _, record in _items(docs, "resourceLogs", "scopeLogs", "logRecords"):
        values = attributes(record.get("attributes"))
        label = labels.get(str(values.get("session.id")))
        if values.get("event.name") != "hook_execution_complete" or label is None:
            continue
        rows[label].append(
            HookTiming(
                hook_event=str(values.get("hook_event")),
                total_ms=_as_int(values.get("total_duration_ms")),
                hooks=_as_int(values.get("num_hooks")),
                success=_as_int(values.get("num_success")),
                non_blocking_error=_as_int(values.get("num_non_blocking_error")),
                cancelled=_as_int(values.get("num_cancelled")),
            )
        )
    return rows


def _joined(values: Iterable[object]) -> str:
    return ", ".join(sorted(str(value) for value in values))


def print_summary(raw: Path) -> None:
    """Print the shape of the metrics, events and spans in ``raw``."""
    metrics = read_jsonl(raw / "otlp-metrics.jsonl")
    logs = read_jsonl(raw / "otlp-logs.jsonl")
    traces = read_jsonl(raw / "otlp-traces.jsonl")

    print("== metrics")
    for name, metric in sorted(metric_shapes(metrics).items()):
        print(
            f"{name} unit={metric.unit} kind={metric.kind}"
            f" temporality={sorted(metric.temporality)} monotonic={metric.monotonic}"
        )
        print(f"    keys: {_joined(metric.keys)}")
        for key, values in sorted(metric.values.items()):
            print(f"    {key} = {_joined(values)}")
    print(f"resource keys: {_joined(_resource_keys(metrics, 'resourceMetrics'))}")

    print("== events")
    for name, event in sorted(event_shapes(logs).items()):
        print(f"{name} n={event.count} trace={event.traced}")
        print(f"    keys: {_joined(event.keys)}")
    scopes = {
        header.get("name") if isinstance(header := scope.get("scope"), dict) else None
        for _, scope, _ in _items(logs, "resourceLogs", "scopeLogs", "logRecords")
    }
    print(f"scopes: {_joined(scopes)}")
    print(f"resource keys: {_joined(_resource_keys(logs, 'resourceLogs'))}")

    print("== spans")
    for name, span in sorted(span_shapes(traces).items()):
        print(f"{name} n={span.count}")
        print(f"    keys: {_joined(span.keys)}")
        if span.agents:
            print(
                f"    agent ids: {_joined(f'{agent} <- {parent}' for agent, parent in span.agents)}"
            )
    print(f"resource keys: {_joined(_resource_keys(traces, 'resourceSpans'))}")


def print_hook_timings(raw: Path, sessions: Mapping[str, str]) -> None:
    """Print the hook timings of each labelled session."""
    for label, rows in hook_timings(read_jsonl(raw / "otlp-logs.jsonl"), sessions).items():
        print(f"== {label}: {len(rows)} hook executions")
        for row in rows:
            print(
                f"    {row.hook_event} total_ms={row.total_ms} hooks={row.hooks}"
                f" success={row.success} non_blocking_error={row.non_blocking_error}"
                f" cancelled={row.cancelled}"
            )


def _label_session(text: str) -> tuple[str, str]:
    label, separator, session = text.partition("=")
    if not (label and separator and session):
        raise argparse.ArgumentTypeError(f"expected label=session_id, got {text!r}")
    return label, session


def main(argv: Sequence[str] | None = None) -> int:
    """Run one inspection command; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.capture.inspect_capture",
        description="Inspect raw capture output.",
    )
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW, help="raw capture dir")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="telemetry shapes")
    summary.add_argument("--raw", type=Path, default=argparse.SUPPRESS, help="raw capture dir")
    timings = commands.add_parser("hook-timings", help="hook_execution_complete per session")
    timings.add_argument("--raw", type=Path, default=argparse.SUPPRESS, help="raw capture dir")
    timings.add_argument("sessions", nargs="+", type=_label_session, metavar="label=session_id")
    args = parser.parse_args(argv)
    if args.command == "summary":
        print_summary(args.raw)
    else:
        print_hook_timings(args.raw, dict(args.sessions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
