"""The fixture sanitizer's privacy lists against the domain's (ADR 0006, ADR 0021).

``tools/capture/sanitize.py`` replaces identity attributes by key and refuses captures
that hold content; ``ccobs.domain.telemetry`` lists what the collector deletes, and
``tests/test_domain_telemetry.py`` keeps those lists equal to the collector config and
to the replayer's canaries. These tests close the loop: every key or event the sanitizer
knows as identity or content is in the domain's lists, so the collector deletes it too
and the canary replay plants it. Each failure names the key and where to add it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

import pytest

from ccobs.domain.telemetry import (
    CONTENT_EVENTS,
    CONTENT_SPAN_EVENT,
    DATA_POINT_CONTENT_KEYS,
    IDENTITY_KEYS,
    IDENTITY_PREFIXES,
    LOG_CONTENT_KEYS,
    RESOURCE_CONTENT_KEYS,
    SPAN_CONTENT_KEYS,
    SPAN_EVENT_CONTENT_KEYS,
    is_identity_key,
)
from tools.capture import sanitize
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack import replay

COLLECTOR_CONFIG: Final = "stack/otel-collector/config.yaml"
# The domain's content keys per level (ADR 0021), by constant name.
CONTENT_LEVELS: Final[Mapping[str, tuple[str, frozenset[str]]]] = {
    "resource": ("RESOURCE_CONTENT_KEYS", RESOURCE_CONTENT_KEYS),
    "datapoint": ("DATA_POINT_CONTENT_KEYS", DATA_POINT_CONTENT_KEYS),
    "log": ("LOG_CONTENT_KEYS", LOG_CONTENT_KEYS),
    "span": ("SPAN_CONTENT_KEYS", SPAN_CONTENT_KEYS),
    "spanevent": ("SPAN_EVENT_CONTENT_KEYS", SPAN_EVENT_CONTENT_KEYS),
}
# Export request layout per signal: resource list -> scope list -> item list.
LAYOUT: Final[Mapping[replay.Signal, tuple[str, str, str]]] = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _keys(owner: JSONObject) -> Iterator[str]:
    for attribute in _objects(owner.get("attributes")):
        key = attribute.get("key")
        if isinstance(key, str):
            yield key


def _owners(request: JSONObject, signal: replay.Signal) -> Iterator[tuple[str, JSONObject]]:
    """(level, owner) for every resource, data point, log record, span and span event of
    an OTLP export request."""
    resources, scopes, items = LAYOUT[signal]
    for entry in _objects(request.get(resources)):
        resource = entry.get("resource")
        if isinstance(resource, dict):
            yield "resource", resource
        for scope in _objects(entry.get(scopes)):
            for item in _objects(scope.get(items)):
                if signal == "metrics":
                    for data in item.values():
                        if isinstance(data, dict):
                            for point in _objects(data.get("dataPoints")):
                                yield "datapoint", point
                elif signal == "logs":
                    yield "log", item
                else:
                    yield "span", item
                    for event in _objects(item.get("events")):
                        yield "spanevent", event


def _fixture_keys_by_level() -> dict[str, set[str]]:
    """Every attribute key of the OTLP fixtures, by level."""
    found: dict[str, set[str]] = {level: set() for level in CONTENT_LEVELS}
    for name in replay.scenario_names(with_otlp=True):
        scenario = replay.load_scenario(name)
        for signal in replay.SIGNALS:
            for request in scenario.requests(signal):
                for level, owner in _owners(request, signal):
                    found[level].update(_keys(owner))
    return found


FIXTURE_KEYS: Final = _fixture_keys_by_level()


def test_the_fixture_walk_sees_every_level() -> None:
    # The data-driven check below means nothing for a level it never reaches.
    assert all(FIXTURE_KEYS.values()), {level: len(keys) for level, keys in FIXTURE_KEYS.items()}


@pytest.mark.parametrize("key", sorted(sanitize.IDENTITY_PLACEHOLDERS))
def test_every_identity_key_of_the_sanitizer_is_domain_identity(key: str) -> None:
    assert is_identity_key(key), (
        f"tools/capture/sanitize.py IDENTITY_PLACEHOLDERS has {key!r}, which "
        "ccobs.domain.telemetry does not treat as identity: add it to IDENTITY_KEYS there "
        f"(or extend IDENTITY_PREFIXES {IDENTITY_PREFIXES}), to transform/identity at every "
        f"level in {COLLECTOR_CONFIG} and to tools/stack/replay.py (ADR 0021)"
    )
    assert key in replay.IDENTITY_CANARY_KEYS, (
        f"tools/capture/sanitize.py IDENTITY_PLACEHOLDERS has {key!r}, which the canary "
        "replay never plants: add it to IDENTITY_CANARY_KEYS in tools/stack/replay.py, so "
        "that the integration tests prove the collector deletes it"
    )


def test_the_domain_identity_keys_are_known_to_the_sanitizer() -> None:
    # The other way round, for the exact keys (the prefixes cover any user.* key): a key
    # the collector deletes as identity must also get a placeholder in the fixtures.
    missing = sorted(IDENTITY_KEYS - set(sanitize.IDENTITY_PLACEHOLDERS))
    assert not missing, (
        f"ccobs.domain.telemetry IDENTITY_KEYS has {missing}, which "
        "tools/capture/sanitize.py IDENTITY_PLACEHOLDERS does not replace: add a fake "
        "value for each there"
    )


@pytest.mark.parametrize("key", sorted(sanitize.CONTENT_ATTRIBUTES))
def test_every_content_attribute_of_the_sanitizer_is_domain_content(key: str) -> None:
    levels = [name for name, keys in CONTENT_LEVELS.values() if key in keys]
    assert levels, (
        f"tools/capture/sanitize.py CONTENT_ATTRIBUTES has {key!r}, which no content list "
        "of ccobs.domain.telemetry has: add it to the list of each level where Claude Code "
        "sends it (LOG_CONTENT_KEYS for events, SPAN_CONTENT_KEYS for spans, ...), to "
        f"transform/content in {COLLECTOR_CONFIG} and to the content canaries in "
        "tools/stack/replay.py (ADR 0021)"
    )


@pytest.mark.parametrize("level", sorted(CONTENT_LEVELS))
def test_content_attributes_of_the_sanitizer_in_the_fixtures_are_deleted_at_their_level(
    level: str,
) -> None:
    # Where a recapture shows a content attribute (even "<REDACTED>"), the collector must
    # delete it at that level, not only at another one.
    name, deleted = CONTENT_LEVELS[level]
    kept = sorted((FIXTURE_KEYS[level] & sanitize.CONTENT_ATTRIBUTES) - deleted)
    assert not kept, (
        f"the fixtures carry the content attributes {kept} on {level} level, where "
        f"ccobs.domain.telemetry {name} does not delete them: add them there, to "
        f"transform/content ({level} context) in {COLLECTOR_CONFIG} and to the canaries "
        "of that level in tools/stack/replay.py (ADR 0021)"
    )


@pytest.mark.parametrize("event", sorted(sanitize.CONTENT_EVENTS))
def test_every_content_event_of_the_sanitizer_is_a_domain_content_event(event: str) -> None:
    assert event in CONTENT_EVENTS, (
        f"tools/capture/sanitize.py CONTENT_EVENTS has {event!r}, which "
        "ccobs.domain.telemetry CONTENT_EVENTS does not have: add it there, to the "
        f"log_conditions of filter/content in {COLLECTOR_CONFIG} (event.name, event_name "
        "and body) and to CONTENT_EVENT_NAMES in tools/stack/replay.py (ADR 0021)"
    )


@pytest.mark.parametrize("event", sorted(sanitize.CONTENT_SPAN_EVENTS))
def test_every_content_span_event_of_the_sanitizer_is_the_domains(event: str) -> None:
    assert event == CONTENT_SPAN_EVENT, (
        f"tools/capture/sanitize.py CONTENT_SPAN_EVENTS has {event!r}, but "
        f"ccobs.domain.telemetry drops only the span event {CONTENT_SPAN_EVENT!r}: make "
        "CONTENT_SPAN_EVENT a set holding it, add it to the trace_conditions of "
        f"filter/content in {COLLECTOR_CONFIG} and plant it in tools/stack/replay.py "
        "(ADR 0021)"
    )
