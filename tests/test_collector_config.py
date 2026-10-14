"""Static checks of the OpenTelemetry Collector config (ADR 0003, ADR 0006, ADR 0021).

Unit tests: they parse ``stack/otel-collector/config.yaml`` and the OTTL statements of
its privacy processors, and compare the keys and events they delete with the canaries
that ``tools.stack.replay`` plants, so that every planted canary is covered by a filter.
The integration tests send the canaries through a running collector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
import yaml

from tools.stack.replay import (
    CONTENT_EVENT_NAMES,
    CONTENT_SPAN_EVENT,
    DATA_POINT_CANARY_KEYS,
    DEFAULT_FIXTURES,
    IDENTITY_CANARY_KEYS,
    LOG_CONTENT_CANARY_KEYS,
    LOG_RECORD_CANARY_KEYS,
    RESOURCE_CANARY_KEYS,
    SPAN_CANARY_KEYS,
    SPAN_CONTENT_CANARY_KEYS,
    is_identity_key,
)

ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG: Final = ROOT / "stack" / "otel-collector" / "config.yaml"
COMPOSE: Final = ROOT / "compose.yaml"

IDENTITY: Final = "transform/identity"
CONTENT_FILTER: Final = "filter/content"
CONTENT_TRANSFORM: Final = "transform/content"
# The OTTL statement lists of the transform processor, per signal.
STATEMENTS: Final = {
    "metrics": "metric_statements",
    "logs": "log_statements",
    "traces": "trace_statements",
}
# Where the collector must delete identity attributes, per signal (ADR 0021).
IDENTITY_CONTEXTS: Final = {
    "metrics": ("resource", "datapoint"),
    "logs": ("resource", "log"),
    "traces": ("resource", "span", "spanevent"),
}
# The keys replay plants at each level, per signal: every one must be deleted.
PLANTED: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "metrics": {"resource": RESOURCE_CANARY_KEYS, "datapoint": DATA_POINT_CANARY_KEYS},
    "logs": {"resource": RESOURCE_CANARY_KEYS, "log": LOG_RECORD_CANARY_KEYS},
    "traces": {"resource": RESOURCE_CANARY_KEYS, "span": SPAN_CANARY_KEYS},
}
HOST_PATHS: Final = "workspace.host_paths"
# Exporters that would print telemetry, content included, into the container logs.
FORBIDDEN_EXPORTERS: Final = frozenset({"debug", "file", "logging"})
# Keys near the identity rule that must survive it.
KEPT_KEYS: Final = (
    "user",
    "user_prompt",
    "user_prompt_length",
    "user_system_prompt",
    "username",
    "enduser",
    "enduser_id",
    "xuser.id",
    "superuser.id",
    "host.arch",
    "host.id",
    "organization",
    "organization.name",
    "identity",
    "session.id",
    "prompt.id",
    "service.name",
)

# Metadata near the content names that must survive every privacy processor (ADR 0021).
METADATA_KEYS: Final = (
    "skill_name",
    "subagent_type",
    "workflow.name",
    "vcs.repository.name",
    "managed_settings.helper.path",
    "prompt_length",
    "tool_input_size_bytes",
    "error_type",
    "error_class",
    "status_code",
    "hook_name",
    "tool_name",
)

# OTTL paths carry their context prefix (``log.attributes``), as 0.162.0 expects.
DELETE_KEY: Final = re.compile(r'^delete_key\((?P<context>\w+)\.attributes, "(?P<key>[^"\\]+)"\)$')
DELETE_MATCHING: Final = re.compile(
    r'^delete_matching_keys\((?P<context>\w+)\.attributes, "(?P<pattern>(?:[^"\\]|\\.)+)"\)$'
)
EVENT_NAME_CONDITION: Final = re.compile(r'^log\.attributes\["event\.name"\] == "(?P<name>[^"]+)"$')
EVENT_FIELD_CONDITION: Final = re.compile(r'^log\.event_name == "(?P<name>[^"]+)"$')
BODY_CONDITION: Final = re.compile(r'^log\.body == "claude_code\.(?P<name>[^"]+)"$')
SPAN_EVENT_CONDITION: Final = re.compile(r'^spanevent\.name == "(?P<name>[^"]+)"$')


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _strings(value: object, where: str) -> list[str]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    assert all(isinstance(item, str) for item in value), f"{where} must hold strings"
    return [str(item) for item in value]


def _config() -> dict[str, object]:
    return _mapping(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), CONFIG.name)


def _section(name: str) -> dict[str, dict[str, object]]:
    return {
        key: _mapping(value or {}, f"{name}.{key}")
        for key, value in _mapping(_config().get(name), name).items()
    }


def _pipelines() -> dict[str, dict[str, list[str]]]:
    service = _mapping(_config().get("service"), "service")
    pipelines = _mapping(service.get("pipelines"), "service.pipelines")
    return {
        name: {
            part: _strings(value, f"{name}.{part}")
            for part, value in _mapping(pipeline, name).items()
        }
        for name, pipeline in pipelines.items()
    }


def _pipeline_names() -> list[str]:
    """Pipeline IDs for parametrize, read at collection time (none if the config is
    missing, so that ``test_every_signal_has_a_pipeline`` reports it)."""
    return sorted(_pipelines()) if CONFIG.is_file() else []


def _signal(pipeline: str) -> str:
    """The signal of a pipeline ID such as ``logs`` or ``logs/loki``."""
    return pipeline.partition("/")[0]


def _component_type(component: str) -> str:
    """The type of a component ID such as ``filter/content``."""
    return component.partition("/")[0]


def _unquote(ottl: str) -> str:
    """The value of an OTTL string literal's content (``\\\\`` is one backslash)."""
    return re.sub(r"\\(.)", r"\1", ottl)


@dataclass
class Deletions:
    """What the statements of one OTTL context delete: exact keys and key patterns."""

    keys: set[str] = field(default_factory=set)
    patterns: list[re.Pattern[str]] = field(default_factory=list)

    def deletes(self, key: str) -> bool:
        """Whether ``key`` is deleted (Go's ``MatchString`` finds a match anywhere)."""
        return key in self.keys or any(pattern.search(key) for pattern in self.patterns)


def _deletions(processor: str, signal: str) -> dict[str, Deletions]:
    """The deletions of a transform processor for ``signal``, per OTTL context. Any
    statement that is not a plain ``delete_key`` or ``delete_matching_keys`` on the
    attributes of its group's context fails the test, so the parser stays in step with
    the config."""
    groups = _section("processors")[processor].get(STATEMENTS[signal]) or []
    assert isinstance(groups, list), f"{processor} {STATEMENTS[signal]}"
    found: dict[str, Deletions] = {}
    for group in groups:
        mapping = _mapping(group, f"{processor} {STATEMENTS[signal]} entry")
        context = str(mapping.get("context"))
        deletions = found.setdefault(context, Deletions())
        for statement in _strings(mapping.get("statements"), f"{processor} {context}"):
            if (match := DELETE_KEY.fullmatch(statement)) and match["context"] == context:
                deletions.keys.add(match["key"])
            elif (match := DELETE_MATCHING.fullmatch(statement)) and match["context"] == context:
                deletions.patterns.append(re.compile(_unquote(match["pattern"])))
            else:
                pytest.fail(f"{processor} {context}: unexpected statement {statement!r}")
    return found


def _fixture_keys() -> set[str]:
    """Every attribute key in the OTLP fixtures."""

    def walk(node: object) -> Iterator[str]:
        if isinstance(node, dict):
            if isinstance(node.get("key"), str) and "value" in node:
                yield str(node["key"])
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    keys: set[str] = set()
    for path in sorted((DEFAULT_FIXTURES / "otlp").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                keys.update(walk(json.loads(line)))
    return keys


def _conditions(signal: str) -> list[str]:
    """The ``log_conditions`` or ``trace_conditions`` of ``filter/content``: the filter
    processor's current layout (0.146.0 and later), with context-prefixed paths."""
    key = f"{signal}_conditions"
    return _strings(_section("processors")[CONTENT_FILTER].get(key), f"{CONTENT_FILTER} {key}")


PIPELINES: Final = _pipeline_names()


# Pipelines.


def test_every_signal_has_a_pipeline() -> None:
    assert {_signal(name) for name in _pipelines()} == {"metrics", "logs", "traces"}


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_memory_limiter_is_first_in_every_pipeline(pipeline: str) -> None:
    assert _pipelines()[pipeline]["processors"][0] == "memory_limiter"


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_identity_is_deleted_in_every_pipeline(pipeline: str) -> None:
    assert IDENTITY in _pipelines()[pipeline]["processors"]


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_content_processors_are_in_the_pipelines_that_carry_content(pipeline: str) -> None:
    processors = _pipelines()[pipeline]["processors"]
    assert CONTENT_TRANSFORM in processors
    if _signal(pipeline) in ("logs", "traces"):
        assert CONTENT_FILTER in processors
    else:
        assert CONTENT_FILTER not in processors


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_privacy_processors_run_in_order_before_any_other_processor(pipeline: str) -> None:
    # memory_limiter, transform/identity, then the content processors where present
    # (filter first, so the transform skips dropped records), then anything else.
    processors = _pipelines()[pipeline]["processors"]
    content = [name for name in (CONTENT_FILTER, CONTENT_TRANSFORM) if name in processors]
    expected = ["memory_limiter", IDENTITY, *content]
    assert processors[: len(expected)] == expected


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_pipelines_only_use_defined_components(pipeline: str) -> None:
    parts = _pipelines()[pipeline]
    for part in ("receivers", "processors", "exporters"):
        assert set(parts[part]) <= set(_section(part)), f"{pipeline}.{part}"


def test_memory_limiter_is_percentage_based() -> None:
    limiter = _section("processors")["memory_limiter"]
    assert isinstance(limiter.get("limit_percentage"), int)
    assert isinstance(limiter.get("spike_limit_percentage"), int)
    assert "limit_mib" not in limiter
    assert "spike_limit_mib" not in limiter
    assert limiter.get("check_interval")


def test_no_exporter_prints_telemetry() -> None:
    types = {_component_type(name) for name in _section("exporters")}
    assert not types & FORBIDDEN_EXPORTERS


@pytest.mark.parametrize("processor", [IDENTITY, CONTENT_FILTER, CONTENT_TRANSFORM])
def test_privacy_processors_fail_closed(processor: str) -> None:
    # On a statement error the batch is refused, never passed on unfiltered.
    assert _section("processors")[processor].get("error_mode") == "propagate"


# Receivers and health.


def test_receivers_bind_the_otlp_container_ports() -> None:
    protocols = _mapping(_section("receivers")["otlp"].get("protocols"), "otlp.protocols")
    grpc = _mapping(protocols.get("grpc"), "grpc")
    http = _mapping(protocols.get("http"), "http")
    assert grpc.get("endpoint") == "0.0.0.0:4317"
    assert http.get("endpoint") == "0.0.0.0:4318"


def test_health_check_extension_answers_the_compose_healthcheck() -> None:
    extensions = _section("extensions")
    endpoint = str(extensions["health_check"].get("endpoint"))
    service = _mapping(_config().get("service"), "service")
    assert "health_check" in _strings(service.get("extensions"), "service.extensions")
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    collector = _mapping(_mapping(compose["services"], "services")["otel-collector"], "collector")
    test = _strings(_mapping(collector["healthcheck"], "healthcheck")["test"], "test")
    assert test[-1] == f"http://{endpoint}/"


# Identity (always deleted).


@pytest.mark.parametrize(
    ("signal", "context"),
    [(signal, context) for signal, contexts in IDENTITY_CONTEXTS.items() for context in contexts],
)
def test_identity_canaries_are_deleted_at_every_level(signal: str, context: str) -> None:
    deletions = _deletions(IDENTITY, signal).get(context, Deletions())
    kept = [key for key in IDENTITY_CANARY_KEYS if not deletions.deletes(key)]
    assert not kept, f"{signal} {context} keeps {kept}"


@pytest.mark.parametrize(
    ("signal", "context"),
    [(signal, context) for signal, contexts in IDENTITY_CONTEXTS.items() for context in contexts],
)
def test_identity_rule_deletes_exactly_the_identity_keys(signal: str, context: str) -> None:
    deletions = _deletions(IDENTITY, signal)[context]
    corpus = _fixture_keys() | set(IDENTITY_CANARY_KEYS) | set(KEPT_KEYS)
    wrong = sorted(key for key in corpus if deletions.deletes(key) != is_identity_key(key))
    assert not wrong, f"{signal} {context}: the identity rule disagrees on {wrong}"


@pytest.mark.parametrize("signal", sorted(STATEMENTS))
def test_identity_rule_has_no_other_contexts(signal: str) -> None:
    assert set(_deletions(IDENTITY, signal)) == set(IDENTITY_CONTEXTS[signal])


# Content (deleted while content capture is off).


def test_content_attributes_are_deleted_exactly() -> None:
    expected = {
        "metrics": {"resource": {HOST_PATHS}, "datapoint": {HOST_PATHS}},
        "logs": {"resource": {HOST_PATHS}, "log": set(LOG_CONTENT_CANARY_KEYS)},
        "traces": {"resource": {HOST_PATHS}, "span": {*SPAN_CONTENT_CANARY_KEYS, HOST_PATHS}},
    }
    for signal, contexts in expected.items():
        found = _deletions(CONTENT_TRANSFORM, signal)
        assert {context: deletions.keys for context, deletions in found.items()} == contexts
        assert not any(deletions.patterns for deletions in found.values()), signal


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_every_planted_canary_is_deleted_by_its_pipeline(pipeline: str) -> None:
    signal = _signal(pipeline)
    transforms = [
        name
        for name in _pipelines()[pipeline]["processors"]
        if _component_type(name) == "transform"
    ]
    for context, keys in PLANTED[signal].items():
        levels = [_deletions(name, signal).get(context, Deletions()) for name in transforms]
        kept = [key for key in keys if not any(level.deletes(key) for level in levels)]
        assert not kept, f"{pipeline} {context} keeps {kept}"


def test_content_events_are_dropped_by_event_name_and_by_body() -> None:
    conditions = _conditions("log")
    for name in CONTENT_EVENT_NAMES:
        assert f'log.attributes["event.name"] == "{name}"' in conditions
        assert f'log.event_name == "{name}"' in conditions
        assert f'log.body == "claude_code.{name}"' in conditions


def test_content_filter_drops_nothing_else() -> None:
    for condition in _conditions("log"):
        match = (
            EVENT_NAME_CONDITION.fullmatch(condition)
            or EVENT_FIELD_CONDITION.fullmatch(condition)
            or BODY_CONDITION.fullmatch(condition)
        )
        assert match, f"unexpected log condition {condition!r}"
        assert match["name"] in CONTENT_EVENT_NAMES
    # Only the current layout: no deprecated logs.log_record or traces.spanevent, and
    # nothing that drops metrics, whole spans or other records.
    content = _section("processors")[CONTENT_FILTER]
    assert set(content) == {"error_mode", "log_conditions", "trace_conditions"}


def test_content_span_events_are_dropped() -> None:
    conditions = _conditions("trace")
    assert conditions == [f'spanevent.name == "{CONTENT_SPAN_EVENT}"']
    assert SPAN_EVENT_CONDITION.fullmatch(conditions[0])


@pytest.mark.parametrize("processor", [IDENTITY, CONTENT_TRANSFORM])
def test_metadata_attributes_survive(processor: str) -> None:
    for signal in STATEMENTS:
        for context, deletions in _deletions(processor, signal).items():
            deleted = [key for key in METADATA_KEYS if deletions.deletes(key)]
            assert not deleted, f"{processor} {signal} {context} deletes {deleted}"
