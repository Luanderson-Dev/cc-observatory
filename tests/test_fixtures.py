"""Contract and privacy guard for the sanitized fixtures in ``tests/fixtures/``.

The fixtures are real Claude Code sessions, captured and sanitized with ``tools/capture``
(see ``tests/fixtures/README.md``). These tests pin what those sessions showed, so a
recapture that drops a signal, changes a payload shape or leaks personal data fails here
before any parser relies on it. Privacy failures name a kind and a location, never the
value itself.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Final, Literal

import pytest

from tools.capture.jsontypes import JSON, JSONObject
from tools.capture.sanitize import (
    BUILTIN_SKILLS,
    CONTENT_ATTRIBUTES,
    IDENTITY_PLACEHOLDERS,
    INTERNAL_SUMMARY,
    KEPT_NAMES,
    KNOWN_AGENT_TYPES,
    NAME_FIELD_KINDS,
    NAME_PLACEHOLDER_TEMPLATES,
    find_content,
    find_isolation_problems,
    find_personal_data,
    internal_summary_keys,
    started_agent_types,
)

FIXTURES: Final = Path(__file__).parent / "fixtures"
SIGNALS: Final = ("metrics", "logs", "traces")


# Loading the fixtures.


@cache
def read_documents(path: Path) -> tuple[JSON, ...]:
    """The JSON documents of a fixture file: one per line in ``.jsonl``, else the file."""
    text = path.read_text(encoding="utf-8")
    documents: tuple[JSON, ...]
    if path.suffix == ".jsonl":
        documents = tuple(json.loads(line) for line in text.splitlines())
    else:
        documents = (json.loads(text),)
    return documents


def manifest() -> JSONObject:
    (document,) = read_documents(FIXTURES / "manifest.json")
    assert isinstance(document, dict), "manifest.json must hold an object"
    return document


def as_text(value: JSON) -> str:
    """``value`` if it is a string, else an empty string."""
    return value if isinstance(value, str) else ""


def objects(value: JSON) -> list[JSONObject]:
    """The objects of a JSON list (anything else: none)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


@dataclass(frozen=True)
class Scenario:
    """A scenario of the manifest: one Claude Code session and its fixture files."""

    name: str
    session_id: str
    files: tuple[str, ...]

    def __str__(self) -> str:
        return self.name

    @property
    def hooks_file(self) -> str:
        return f"hooks/{self.name}.jsonl"

    @property
    def has_hooks(self) -> bool:
        return self.hooks_file in self.files

    def otlp_file(self, signal: str) -> str:
        return f"otlp/{self.name}/{signal}.jsonl"

    @property
    def signals(self) -> tuple[str, ...]:
        return tuple(signal for signal in SIGNALS if self.otlp_file(signal) in self.files)


def load_scenarios() -> list[Scenario]:
    """The manifest's scenarios; malformed entries are left to the manifest tests."""
    entries = manifest().get("scenarios")
    scenarios: list[Scenario] = []
    for entry in objects(entries):
        name, session_id, files = entry.get("name"), entry.get("session_id"), entry.get("files")
        if isinstance(name, str) and isinstance(session_id, str) and isinstance(files, list):
            listed = tuple(file for file in files if isinstance(file, str))
            scenarios.append(Scenario(name, session_id, listed))
    return scenarios


SCENARIOS: Final = load_scenarios()
HOOK_SCENARIOS: Final = [scenario for scenario in SCENARIOS if scenario.has_hooks]
OTLP_SCENARIOS: Final = [scenario for scenario in SCENARIOS if scenario.signals]
METRIC_SCENARIOS: Final = [scenario for scenario in SCENARIOS if "metrics" in scenario.signals]
LOG_SCENARIOS: Final = [scenario for scenario in SCENARIOS if "logs" in scenario.signals]


def scenario_named(name: str) -> Scenario:
    found = [scenario for scenario in SCENARIOS if scenario.name == name]
    assert found, f"no scenario {name!r} in manifest.json"
    return found[0]


# Hook fixtures: hooks/<scenario>.jsonl.


@dataclass(frozen=True)
class HookLine:
    """One captured hook request: when it arrived, through which transport, and the
    payload Claude Code sent."""

    offset_ms: int
    transport: str
    payload: JSONObject

    @property
    def event(self) -> str:
        return as_text(self.payload.get("hook_event_name"))


LINE_KEYS: Final = frozenset({"offset_ms", "transport", "payload"})


@cache
def hook_lines(scenario: Scenario) -> tuple[HookLine, ...]:
    """The scenario's hook lines, each checked against the documented line format."""
    lines: list[HookLine] = []
    for number, line in enumerate(read_documents(FIXTURES / scenario.hooks_file), start=1):
        where = f"{scenario.hooks_file} line {number}"
        assert isinstance(line, dict), f"{where} is not an object"
        assert set(line) == LINE_KEYS, f"{where} has the keys {sorted(line)}"
        offset, transport, payload = line["offset_ms"], line["transport"], line["payload"]
        assert isinstance(offset, int), f"{where}: offset_ms is not an integer"
        assert not isinstance(offset, bool), f"{where}: offset_ms is not an integer"
        assert isinstance(transport, str), f"{where}: transport is not a string"
        assert isinstance(payload, dict), f"{where}: payload is not an object"
        lines.append(HookLine(offset, transport, payload))
    return tuple(lines)


# OTLP fixtures: otlp/<scenario>/<signal>.jsonl, one export request per line.

_LAYOUT: Final = {
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}
_METRIC_KINDS: Final = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")
DELTA: Final = 1  # AGGREGATION_TEMPORALITY_DELTA


def unwrap(value: JSON) -> JSON:
    """The scalar inside an OTLP AnyValue (``intValue`` as an int); anything else as is."""
    if isinstance(value, dict):
        for kind in ("stringValue", "boolValue", "doubleValue"):
            if kind in value:
                return value[kind]
        number = value.get("intValue")
        if isinstance(number, str | int):
            return int(number)
    return value


def attributes(item: JSON) -> dict[str, JSON]:
    """The attributes of an OTLP resource, data point, log record or span, by key."""
    found: dict[str, JSON] = {}
    if isinstance(item, dict):
        for pair in objects(item.get("attributes")):
            key = pair.get("key")
            if isinstance(key, str):
                found[key] = unwrap(pair.get("value"))
    return found


def otlp_documents(scenario: Scenario, signal: str) -> tuple[JSON, ...]:
    if signal not in scenario.signals:
        return ()
    return read_documents(FIXTURES / scenario.otlp_file(signal))


@cache
def otlp_items(scenario: Scenario, signal: str) -> tuple[tuple[JSONObject, JSONObject], ...]:
    """``(resource, item)`` for every metric, log record or span of one signal."""
    resources_key, scopes_key, items_key = _LAYOUT[signal]
    found: list[tuple[JSONObject, JSONObject]] = []
    for document in otlp_documents(scenario, signal):
        assert isinstance(document, dict), f"{scenario.otlp_file(signal)}: not an object"
        for resource in objects(document.get(resources_key)):
            header = resource.get("resource")
            for scope in objects(resource.get(scopes_key)):
                found += [
                    (header if isinstance(header, dict) else {}, item)
                    for item in objects(scope.get(items_key))
                ]
    return tuple(found)


def metric_data(metric: JSONObject) -> JSONObject:
    """The data of a metric: its ``sum``, ``gauge``, ``histogram``, ... object."""
    found = [data for kind in _METRIC_KINDS if isinstance(data := metric.get(kind), dict)]
    assert len(found) == 1, f"metric {metric.get('name')!r} must have exactly one data kind"
    return found[0]


def walk(value: JSON, location: str = "$") -> Iterator[tuple[str, JSON]]:
    """Every node of a JSON value with its location (``$.a[0].b``)."""
    yield location, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{location}[{index}]")


def key_values(document: JSON) -> Iterator[tuple[str, str, JSONObject]]:
    """``(location, key, value)`` of every OTLP attribute (``{"key", "value"}``)."""
    for location, node in walk(document):
        if isinstance(node, dict):
            key, value = node.get("key"), node.get("value")
            if isinstance(key, str) and isinstance(value, dict):
                yield location, key, value


def string_leaves(value: JSON) -> Iterator[str]:
    """Every string value (not key) in a JSON value."""
    for _, node in walk(value):
        if isinstance(node, str):
            yield node


# Manifest.

VERSION: Final = re.compile(r"\d+\.\d+\.\d+")
SCENARIO_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PLATFORMS: Final = frozenset({"windows", "linux", "macos"})
TOP_LEVEL: Final = frozenset({"manifest.json", "README.md", "hooks", "otlp"})


def required_text(document: JSONObject, key: str) -> str:
    value = document.get(key)
    assert isinstance(value, str), f"{key} must be a string"
    return value


def duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def test_manifest_records_the_capture_provenance() -> None:
    document = manifest()

    assert VERSION.fullmatch(required_text(document, "claude_code_version"))
    assert VERSION.fullmatch(required_text(document, "collector_version"))
    date.fromisoformat(required_text(document, "captured_on"))
    assert required_text(document, "platform") in PLATFORMS


def test_manifest_records_an_isolated_capture() -> None:
    # scenarios.json says so and the sanitizer copies it; it refuses a capture that does
    # not, unless --allow-unisolated (and then it records false).
    assert manifest().get("isolated") is True


def test_manifest_scenarios_are_well_formed() -> None:
    entries = manifest().get("scenarios")
    assert isinstance(entries, list)
    assert entries

    for entry in entries:
        assert isinstance(entry, dict)
        name = required_text(entry, "name")
        assert SCENARIO_NAME.fullmatch(name), name
        assert UUID.fullmatch(required_text(entry, "session_id")), name
        assert required_text(entry, "description").strip(), name
        files = entry.get("files")
        assert isinstance(files, list), name
        listed = [file for file in files if isinstance(file, str)]
        own = {f"hooks/{name}.jsonl", *(f"otlp/{name}/{signal}.jsonl" for signal in SIGNALS)}
        assert listed, name
        assert len(listed) == len(files), name
        assert sorted(set(listed) - own) == [], f"{name} lists files of another scenario"
        assert duplicates(listed) == [], name

    assert len(SCENARIOS) == len(entries)
    assert duplicates(scenario.name for scenario in SCENARIOS) == []
    assert duplicates(scenario.session_id for scenario in SCENARIOS) == []


def test_manifest_lists_every_fixture_file_and_nothing_else() -> None:
    listed = {file for scenario in SCENARIOS for file in scenario.files}
    on_disk = {
        path.relative_to(FIXTURES).as_posix()
        for directory in ("hooks", "otlp")
        for path in (FIXTURES / directory).rglob("*")
        if path.is_file()
    }

    assert sorted(listed - on_disk) == [], "listed in manifest.json but missing"
    assert sorted(on_disk - listed) == [], "on disk but not listed in manifest.json"
    assert sorted(path.name for path in FIXTURES.iterdir() if path.name not in TOP_LEVEL) == []


def test_readme_describes_the_current_capture() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    document = manifest()

    for key in ("claude_code_version", "collector_version", "captured_on"):
        assert required_text(document, key) in readme, key
    assert [scenario.name for scenario in SCENARIOS if f"`{scenario.name}`" not in readme] == []


# Hook payloads.

TRANSPORTS: Final = frozenset({"http", "command"})
COMMON_FIELDS: Final = frozenset({"session_id", "hook_event_name", "cwd", "transcript_path"})
# Fields every payload of the event carried in the capture, besides the common ones and
# prompt_id and permission_mode, which have their own tests. Fields that depend on the
# model or the environment (effort, session_crons, scratchpad_dir) are left out, and so
# are optional ones: PermissionRequest's permission_suggestions (the docs call it
# optional, and a subagent's PermissionRequest came without it).
EVENT_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "InstructionsLoaded": frozenset({"file_path", "memory_type", "load_reason"}),
    "SessionStart": frozenset({"source"}),
    "UserPromptSubmit": frozenset({"prompt"}),
    "PreToolUse": frozenset({"tool_name", "tool_input", "tool_use_id"}),
    "PermissionRequest": frozenset({"tool_name", "tool_input"}),
    "PostToolUse": frozenset(
        {"tool_name", "tool_input", "tool_response", "tool_use_id", "duration_ms"}
    ),
    "PostToolUseFailure": frozenset(
        {"tool_name", "tool_input", "tool_use_id", "error", "is_interrupt", "duration_ms"}
    ),
    "PostToolBatch": frozenset({"tool_calls"}),
    "Notification": frozenset({"notification_type", "message"}),
    "Elicitation": frozenset({"mcp_server_name", "message", "mode", "requested_schema"}),
    "ElicitationResult": frozenset({"mcp_server_name", "action", "mode"}),
    "SubagentStart": frozenset({"agent_id", "agent_type"}),
    "SubagentStop": frozenset(
        {"agent_id", "agent_type", "agent_transcript_path", "stop_hook_active", "background_tasks"}
    ),
    "TaskCreated": frozenset({"task_id", "task_subject", "task_description"}),
    "TaskCompleted": frozenset({"task_id", "task_subject", "task_description"}),
    "Stop": frozenset({"stop_hook_active", "last_assistant_message", "background_tasks"}),
    "StopFailure": frozenset({"error", "last_assistant_message"}),
    "PreCompact": frozenset({"trigger", "custom_instructions"}),
    "PostCompact": frozenset({"trigger", "compact_summary"}),
    "SessionEnd": frozenset({"reason"}),
}
# PermissionRequest has no tool_use_id: a block is matched to its PreToolUse another way.
ABSENT_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "PermissionRequest": frozenset({"tool_use_id"}),
}
PERMISSION_MODE_EVENTS: Final = frozenset(
    {
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "SubagentStop",
        "Stop",
    }
)
# Registering these changes Claude Code's behaviour. (Setup is not here: the rule for it
# is only that it takes no http hooks.)
FORBIDDEN_EVENTS: Final = frozenset(
    {"WorktreeCreate", "WorktreeRemove", "PreModelSwitch", "MessageDisplay", "FileChanged"}
)


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_hook_lines_have_the_documented_format(scenario: Scenario) -> None:
    assert hook_lines(scenario)  # hook_lines checks every line


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_hook_offsets_start_at_zero_and_never_decrease(scenario: Scenario) -> None:
    offsets = [line.offset_ms for line in hook_lines(scenario)]

    assert offsets[0] == 0
    assert offsets == sorted(offsets)


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_hook_transports_are_http_or_command(scenario: Scenario) -> None:
    assert sorted({line.transport for line in hook_lines(scenario)} - TRANSPORTS) == []


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_hook_payloads_belong_to_the_scenario_session(scenario: Scenario) -> None:
    session_ids = {as_text(line.payload.get("session_id")) for line in hook_lines(scenario)}

    assert session_ids == {scenario.session_id}


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_hook_payloads_carry_the_fields_of_their_event(scenario: Scenario) -> None:
    problems: list[str] = []
    for number, line in enumerate(hook_lines(scenario), start=1):
        where = f"line {number} ({line.event or 'no hook_event_name'})"
        if line.event not in EVENT_FIELDS:
            problems.append(f"{where}: not in EVENT_FIELDS; review the event and add it")
            continue
        missing = (COMMON_FIELDS | EVENT_FIELDS[line.event]) - line.payload.keys()
        unexpected = ABSENT_FIELDS.get(line.event, frozenset()) & line.payload.keys()
        if missing:
            problems.append(f"{where}: missing {sorted(missing)}")
        if unexpected:
            problems.append(f"{where}: unexpected {sorted(unexpected)}")

    assert problems == []


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_permission_mode_only_comes_with_prompt_tool_and_stop_events(scenario: Scenario) -> None:
    wrong = [
        f"line {number} ({line.event})"
        for number, line in enumerate(hook_lines(scenario), start=1)
        if ("permission_mode" in line.payload) != (line.event in PERMISSION_MODE_EVENTS)
    ]

    assert wrong == []


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_prompt_id_appears_with_the_first_prompt(scenario: Scenario) -> None:
    lines = hook_lines(scenario)
    events = [line.event for line in lines]
    assert "UserPromptSubmit" in events
    first_prompt = events.index("UserPromptSubmit")

    before = [line.event for line in lines[:first_prompt] if "prompt_id" in line.payload]
    after = [line.event for line in lines[first_prompt:] if "prompt_id" not in line.payload]
    assert before == [], "prompt_id before the first UserPromptSubmit"
    assert after == [], "no prompt_id from the first UserPromptSubmit on"


# Neither takes http hooks.
COMMAND_ONLY_EVENTS: Final = frozenset({"SessionStart", "Setup"})


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_session_start_and_setup_only_arrive_through_command_hooks(scenario: Scenario) -> None:
    transports = {
        (line.event, line.transport)
        for line in hook_lines(scenario)
        if line.event in COMMAND_ONLY_EVENTS
    }

    assert {transport for _, transport in transports} <= {"command"}


# Each capture workspace holds a synthetic CLAUDE.md (tools/capture/workspace.py), so
# every session with hooks loads it as Project memory. This fails on fixtures captured
# before that, until the recapture.
@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_every_hook_scenario_loads_the_synthetic_project_instructions(
    scenario: Scenario,
) -> None:
    claim = Claim("hooks", "InstructionsLoaded", (("memory_type", "Project"),))

    found = claim.holds(records(scenario, "hooks"))

    assert found, f"{scenario}: expected {claim}"


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_no_forbidden_hook_event_appears(scenario: Scenario) -> None:
    events = {line.event for line in hook_lines(scenario)}

    assert sorted(events & FORBIDDEN_EVENTS) == []


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=str)
def test_every_subagent_start_has_a_later_stop(scenario: Scenario) -> None:
    # Async command hooks can arrive in any order, so with them the stop only has to
    # exist; with http hooks it must come later.
    lines = hook_lines(scenario)
    any_order = {line.transport for line in lines} == {"command"}
    unmatched = [
        f"line {number}"
        for number, line in enumerate(lines, start=1)
        if line.event == "SubagentStart"
        and not any(
            other.event == "SubagentStop"
            and other.payload.get("agent_id") == line.payload.get("agent_id")
            for other in (lines if any_order else lines[number:])
        )
    ]

    assert unmatched == []


def tool_status(payload: JSONObject) -> JSON:
    """The ``status`` of a PostToolUse ``tool_response`` (None without one)."""
    response = payload.get("tool_response")
    return response.get("status") if isinstance(response, dict) else None


def runs_in_background(payload: JSONObject, agent_id: str) -> bool:
    """Whether a Stop payload lists ``agent_id`` as a running background subagent."""
    return any(
        task.get("id") == agent_id
        and task.get("type") == "subagent"
        and task.get("status") == "running"
        for task in objects(payload.get("background_tasks"))
    )


def notifies_task(payload: JSONObject, agent_id: str) -> bool:
    """Whether a UserPromptSubmit prompt is the task notification of ``agent_id``."""
    prompt = as_text(payload.get("prompt"))
    return "<task-notification>" in prompt and f"<task-id>{agent_id}</task-id>" in prompt


def test_background_claims_need_the_running_subagent_and_its_notification() -> None:
    running: JSONObject = {
        "background_tasks": [{"id": "a1", "type": "subagent", "status": "running"}]
    }
    done: JSONObject = {
        "background_tasks": [{"id": "a1", "type": "subagent", "status": "completed"}]
    }
    note = "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>"

    assert runs_in_background(running, "a1")
    assert not runs_in_background(running, "a2")
    assert not runs_in_background(done, "a1")
    assert not runs_in_background({}, "a1")
    assert notifies_task({"prompt": note}, "a1")
    assert not notifies_task({"prompt": note}, "a2")
    assert not notifies_task({"prompt": "<task-id>a1</task-id>"}, "a1")


def test_background_subagent_outlives_the_main_stop() -> None:
    # Async command hooks arrive in any order (the PostToolUse of the Agent tool even
    # before its PreToolUse, events 1 ms apart swapped), so the claims rest on content,
    # never on arrival order. No SessionEnd either: in -p, async hooks still running at
    # teardown can be killed.
    lines = hook_lines(scenario_named("headless-async-command-transport"))
    starts = [
        as_text(line.payload.get("agent_id")) for line in lines if line.event == "SubagentStart"
    ]
    launched = [
        line
        for line in lines
        if line.event == "PostToolUse"
        and line.payload.get("tool_name") == "Agent"
        and tool_status(line.payload) == "async_launched"
    ]

    assert launched
    assert len(starts) == 1
    agent_id = starts[0]
    # The main Stop happened while the subagent was still running.
    assert any(
        line.event == "Stop" and runs_in_background(line.payload, agent_id) for line in lines
    )
    # The subagent's report came back to the main agent as a task notification.
    assert any(
        line.event == "UserPromptSubmit" and notifies_task(line.payload, agent_id) for line in lines
    )


# OTLP telemetry.

REDACTED: Final = "<REDACTED>"


def content_problems(document: JSON) -> list[str]:
    """Content-bearing telemetry in an export request, as kind and location."""
    problems = [f"{finding.kind} at {finding.location}" for finding in find_content(document)]
    problems += [
        f"{key} is not {REDACTED} at {location}"
        for location, key, value in key_values(document)
        if key in CONTENT_ATTRIBUTES and value != {"stringValue": REDACTED}
    ]
    return problems


@pytest.mark.parametrize("scenario", OTLP_SCENARIOS, ids=str)
def test_every_otlp_item_carries_the_session_id(scenario: Scenario) -> None:
    for signal in scenario.signals:
        items = [item for _, item in otlp_items(scenario, signal)]
        if signal == "metrics":
            items = [point for item in items for point in objects(metric_data(item)["dataPoints"])]
        session_ids = {as_text(attributes(item).get("session.id")) for item in items}

        assert items, signal
        assert session_ids == {scenario.session_id}, signal


@pytest.mark.parametrize("scenario", OTLP_SCENARIOS, ids=str)
def test_otlp_resources_name_the_captured_claude_code(scenario: Scenario) -> None:
    version = required_text(manifest(), "claude_code_version")
    services = {
        (as_text(resource.get("service.name")), as_text(resource.get("service.version")))
        for signal in scenario.signals
        for header, _ in otlp_items(scenario, signal)
        for resource in [attributes(header)]
    }

    assert services == {("claude-code", version)}


@pytest.mark.parametrize("scenario", METRIC_SCENARIOS, ids=str)
def test_metrics_use_delta_temporality(scenario: Scenario) -> None:
    # Gauges have no temporality; every other kind must be delta.
    temporalities = {
        as_text(metric.get("name")): metric_data(metric).get("aggregationTemporality")
        for _, metric in otlp_items(scenario, "metrics")
        if "gauge" not in metric
    }

    assert temporalities
    assert {name: value for name, value in temporalities.items() if value != DELTA} == {}


@pytest.mark.parametrize("scenario", LOG_SCENARIOS, ids=str)
def test_log_records_are_named_events(scenario: Scenario) -> None:
    problems: list[str] = []
    for number, (_, record) in enumerate(otlp_items(scenario, "logs"), start=1):
        name = attributes(record).get("event.name")
        if not isinstance(name, str) or not name:
            problems.append(f"log record {number}: no event.name")
        elif unwrap(record.get("body")) != f"claude_code.{name}":
            problems.append(f"log record {number} ({name}): body is not claude_code.{name}")

    assert problems == []


@pytest.mark.parametrize("scenario", OTLP_SCENARIOS, ids=str)
def test_otlp_content_attributes_are_redacted(scenario: Scenario) -> None:
    problems = [
        f"{scenario.otlp_file(signal)} record {number}: {problem}"
        for signal in scenario.signals
        for number, document in enumerate(otlp_documents(scenario, signal), start=1)
        for problem in content_problems(document)
    ]

    assert problems == []


# Events that run none of Claude Code's own hooks: a hook execution for them counts the
# capture's single hook only, so any other number means hooks from elsewhere (the user's
# settings, which hook_registered still lists) ran.
CAPTURE_ONLY_HOOK_EVENTS: Final = frozenset(
    {"Stop", "UserPromptSubmit", "PreToolUse", "SessionStart"}
)
HOOK_EXECUTION_EVENTS: Final = frozenset({"hook_execution_start", "hook_execution_complete"})


def test_only_the_capture_hooks_run() -> None:
    checked: list[str] = []
    wrong: list[str] = []
    for scenario in LOG_SCENARIOS:
        for number, record in enumerate(records(scenario, "logs"), start=1):
            fields = record.attributes
            if (
                record.name in HOOK_EXECUTION_EVENTS
                and fields.get("hook_event") in CAPTURE_ONLY_HOOK_EVENTS
            ):
                where = (
                    f"{scenario} log record {number} ({record.name}, {fields.get('hook_event')})"
                )
                checked.append(where)
                if as_number(fields.get("num_hooks", "")) != 1:
                    wrong.append(where)

    assert checked
    assert wrong == []


# What each scenario showed. A recapture that loses one of these signals fails here.

type Source = Literal["hooks", "metrics", "logs", "traces"]


class Is(Enum):
    """A condition on a field, instead of an exact value."""

    PRESENT = "present"
    ABSENT = "absent"
    POSITIVE = "> 0"


type Expected = JSON | Is


@dataclass(frozen=True)
class Record:
    """A hook payload, metric data point, event or span: its name and attributes."""

    name: str
    attributes: Mapping[str, JSON]


def as_number(value: JSON) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value) if isinstance(value, str) else None
    except ValueError:
        return None


def meets(fields: Mapping[str, JSON], key: str, expected: Expected) -> bool:
    if expected is Is.ABSENT:
        return key not in fields
    if key not in fields:
        return False
    if expected is Is.PRESENT:
        return True
    if expected is Is.POSITIVE:
        number = as_number(fields[key])
        return number is not None and number > 0
    return fields[key] == expected


@dataclass(frozen=True)
class Claim:
    """Some record of ``source`` named ``name`` meets every condition (with
    ``present=False``: none does)."""

    source: Source
    name: str
    conditions: tuple[tuple[str, Expected], ...] = ()
    present: bool = True

    def __str__(self) -> str:
        conditions = " ".join(
            f"{key} {expected.value}" if isinstance(expected, Is) else f"{key}={expected}"
            for key, expected in self.conditions
        )
        return f"{'' if self.present else 'no '}{self.source} {self.name} {conditions}".rstrip()

    def matches(self, record: Record) -> bool:
        return record.name == self.name and all(
            meets(record.attributes, key, expected) for key, expected in self.conditions
        )

    def holds(self, records: Iterable[Record]) -> bool:
        return any(self.matches(record) for record in records) == self.present


def hook(name: str, **conditions: Expected) -> Claim:
    return Claim("hooks", name, tuple(conditions.items()))


def no_hook(name: str) -> Claim:
    return Claim("hooks", name, present=False)


def metric(name: str, **conditions: Expected) -> Claim:
    return Claim("metrics", name, tuple(conditions.items()))


def event(name: str, **conditions: Expected) -> Claim:
    return Claim("logs", name, tuple(conditions.items()))


def span(name: str, **conditions: Expected) -> Claim:
    return Claim("traces", name, tuple(conditions.items()))


@cache
def records(scenario: Scenario, source: Source) -> tuple[Record, ...]:
    """The scenario's hook payloads, metric data points, events or spans."""
    if source == "hooks":
        lines = hook_lines(scenario) if scenario.has_hooks else ()
        return tuple(Record(line.event, line.payload) for line in lines)
    found: list[Record] = []
    for _, item in otlp_items(scenario, source):
        if source == "metrics":
            points = objects(metric_data(item).get("dataPoints"))
            found += [Record(as_text(item.get("name")), attributes(point)) for point in points]
        elif source == "logs":
            fields = attributes(item)
            found.append(Record(as_text(fields.get("event.name")), fields))
        else:
            found.append(Record(as_text(item.get("name")), attributes(item)))
    return tuple(found)


CONTRACT: Final[Mapping[str, tuple[Claim, ...]]] = {
    "headless-tools-subagent": (
        hook("PermissionRequest", tool_use_id=Is.ABSENT),
        hook("PostToolUseFailure"),
        hook("SubagentStart", agent_id=Is.PRESENT),
        hook("SubagentStop", agent_id=Is.PRESENT),
        metric("claude_code.token.usage", query_source="main"),
        metric("claude_code.token.usage", query_source="subagent"),
        span("claude_code.interaction"),
        span("claude_code.llm_request"),
        span("claude_code.tool"),
        span("claude_code.tool.blocked_on_user"),
        span("claude_code.tool.execution"),
    ),
    "headless-edit-commit-tasks": (
        hook("TaskCreated"),
        hook("TaskCompleted"),
        metric("claude_code.lines_of_code.count"),
        metric("claude_code.code_edit_tool.decision"),
        metric("claude_code.commit.count"),
    ),
    "headless-api-error": (
        hook("StopFailure", error="model_not_found"),
        event("api_error"),
    ),
    "headless-sessionstart-http-ignored": (
        no_hook("SessionStart"),
        hook("UserPromptSubmit"),
        hook("SessionEnd"),
    ),
    "headless-async-command-transport": (
        hook("SubagentStart", agent_id=Is.PRESENT),
        hook("SubagentStop", agent_id=Is.PRESENT),
        metric("claude_code.token.usage", query_source="subagent"),
        event("subagent_completed"),
    ),
    "interactive-permission-compact": (
        hook("PermissionRequest"),
        hook("Notification", notification_type="permission_prompt"),
        hook("PreCompact"),
        hook("SessionStart", source="compact"),
        hook("PostCompact"),
        hook("SessionEnd", reason="prompt_input_exit"),
        event("tool_decision", decision="reject", source="user_reject"),
        event("compaction"),
    ),
    "interactive-elicitation-idle": (
        hook("Elicitation"),
        hook("ElicitationResult", action="cancel"),
        hook("Notification", notification_type="elicitation_dialog"),
        hook("Notification", notification_type="elicitation_response"),
        hook("Notification", notification_type="idle_prompt"),
    ),
    "hooks-endpoint-down": (event("hook_execution_complete", num_non_blocking_error=Is.POSITIVE),),
    "hooks-endpoint-timeout": (event("hook_execution_complete", num_cancelled=Is.POSITIVE),),
}
CONTRACT_CASES: Final = [
    pytest.param(name, claim, id=f"{name}: {claim}")
    for name, claims in CONTRACT.items()
    for claim in claims
]


def test_contract_covers_every_scenario() -> None:
    assert sorted(CONTRACT) == sorted(scenario.name for scenario in SCENARIOS)


@pytest.mark.parametrize(("name", "claim"), CONTRACT_CASES)
def test_scenario_shows_its_observed_signal(name: str, claim: Claim) -> None:
    scenario = scenario_named(name)

    # A plain boolean, so a failure prints the claim and never the payloads.
    found = claim.holds(records(scenario, claim.source))

    assert found, f"{name}: expected {claim}"


def test_a_claim_fails_when_the_signal_is_missing() -> None:
    found = [Record("Stop", {}), Record("StopFailure", {"error": "rate_limit", "count": "0"})]

    assert hook("StopFailure", error=Is.PRESENT).holds(found)
    assert not hook("StopFailure", error="model_not_found").holds(found)
    assert not hook("StopFailure", count=Is.POSITIVE).holds(found)
    assert not hook("Stop", error=Is.PRESENT).holds(found)
    assert hook("Stop", error=Is.ABSENT).holds(found)
    assert no_hook("SessionStart").holds(found)
    assert not no_hook("Stop").holds(found)
    assert str(event("x", n=Is.POSITIVE, kind="a")) == "logs x n > 0 kind=a"


# Privacy guard: no personal data in any fixture file, this README included.

REQUEST_ID: Final = re.compile(r"(?<![A-Za-z0-9_])req_[A-Za-z0-9]+")
REQUEST_ID_PLACEHOLDER: Final = re.compile(r"req_EXAMPLE\d{4}")
# Each name field holds a placeholder of its own kind (from the sanitizer's own mapping),
# or a name the sanitizer keeps.
NAME_PLACEHOLDERS: Final[Mapping[str, re.Pattern[str]]] = {
    field: re.compile(re.escape(NAME_PLACEHOLDER_TEMPLATES[kind]).replace(re.escape("{}"), r"\d+"))
    for field, kind in NAME_FIELD_KINDS.items()
}
MCP_TOOL: Final = re.compile(r"(?<![A-Za-z0-9_])mcp__([A-Za-z0-9_-]+?)__(?=[A-Za-z0-9_-])")
FIXTURE_FILES: Final = sorted(
    path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*") if path.is_file()
)


def fixture_documents(name: str) -> tuple[JSON, ...]:
    """The JSON documents of a fixture file; any other file is one string."""
    path = FIXTURES / name
    if path.suffix in (".json", ".jsonl"):
        return read_documents(path)
    return (path.read_text(encoding="utf-8"),)


def field_values(
    value: JSON, location: str = "$", field: str | None = None
) -> Iterator[tuple[str, str | None, str]]:
    """``(location, field, text)`` of every string value. The field is its key, the key of
    the OTLP attribute it is the value of, or ``mcp_server.name`` for the name in a hook
    payload's ``mcp_server`` object."""
    if isinstance(value, str):
        yield location, field, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from field_values(item, f"{location}[{index}]", field)
    elif isinstance(value, dict):
        attribute = value.get("key") if isinstance(value.get("value"), dict) else None
        for key, item in value.items():
            child: str | None = key
            if key == "value" and isinstance(attribute, str):
                child = attribute
            elif key in ("stringValue", "arrayValue", "values"):
                child = field
            elif field == "mcp_server" and key == "name":
                child = "mcp_server.name"
            yield from field_values(item, f"{location}.{key}", child)


def is_kept_name(name: str) -> bool:
    """A name the sanitizer keeps: Claude Code's generic stand-ins, the capture's own MCP
    server and the skills bundled with Claude Code."""
    return name in KEPT_NAMES or name.removeprefix("/") in BUILTIN_SKILLS


def name_problems(document: JSON) -> list[str]:
    """Plugin, marketplace, skill and MCP server names that are not placeholders of their
    own kind or kept names, including the server in ``mcp__<server>__<tool>``."""
    server = NAME_PLACEHOLDERS["mcp_server_name"]
    problems: list[str] = []
    for location, field, text in field_values(document):
        placeholder = NAME_PLACEHOLDERS.get(field or "")
        if placeholder is not None and not (placeholder.fullmatch(text) or is_kept_name(text)):
            problems.append(f"{field} is not a placeholder at {location}")
        problems += [
            f"MCP server in a tool name is not a placeholder at {location}"
            for match in MCP_TOOL.finditer(text)
            if not (server.fullmatch(match[1]) or is_kept_name(match[1]))
        ]
    return problems


def payload_of(document: JSON) -> JSONObject | None:
    """The payload of a hook line; None for any other document."""
    payload = document.get("payload") if isinstance(document, dict) else None
    return payload if isinstance(payload, dict) else None


def summary_problems(document: JSON, agent_types: Collection[str]) -> list[str]:
    """Summaries written by internal agents (compaction, prompt suggestion, any subagent
    the session did not start) in a hook line that are not the sanitizer's placeholder:
    they summarize the whole context, the user's own instructions included."""
    payload = payload_of(document)
    if payload is None:
        return []
    return [
        f"internal summary {key} is not the placeholder at $.payload.{key}"
        for key in internal_summary_keys(payload, agent_types)
        if payload[key] not in (None, INTERNAL_SUMMARY)
    ]


def isolation_problems(document: JSON) -> list[str]:
    """Instructions a hook line shows loaded from outside the capture workspace."""
    payload = payload_of(document)
    if payload is None:
        return []
    return [
        f"{found.kind} at $.payload{found.location.removeprefix('$')}"
        for found in find_isolation_problems(payload)
    ]


def privacy_problems(document: JSON, agent_types: Collection[str] = KNOWN_AGENT_TYPES) -> list[str]:
    """Personal data in a fixture document, as kind and location, never the value.

    The sanitizer's guard runs without machine-specific literals (so it is the same on
    every machine): emails outside example.com, home directories other than the fake
    user's, paths outside the capture workspace, absolute paths outside the fake home,
    real request and tool use IDs, plugin ID hashes and client request IDs that are not
    fakes, letters outside ASCII. On top of it, identity attributes must hold the
    sanitizer's placeholders, every request ID must be a ``req_EXAMPLE`` placeholder,
    name fields must hold placeholders of their own kind (or kept names), summaries
    written by internal agents (``agent_types``: the subagents the session started)
    must be the placeholder, and InstructionsLoaded must come from the capture
    workspace.
    """
    problems = [f"{found.kind} at {found.location}" for found in find_personal_data(document, {})]
    for location, key, value in key_values(document):
        placeholder = IDENTITY_PLACEHOLDERS.get(key)
        if placeholder is not None and any(text != placeholder for text in string_leaves(value)):
            problems.append(f"{key} other than its placeholder at {location}")
        if key == "request_id" and not all(
            REQUEST_ID_PLACEHOLDER.fullmatch(text) for text in string_leaves(value)
        ):
            problems.append(f"request_id other than a req_EXAMPLE placeholder at {location}")
    foreign = [
        token
        for text in string_leaves(document)
        for token in REQUEST_ID.findall(text)
        if not REQUEST_ID_PLACEHOLDER.fullmatch(token)
    ]
    if foreign:
        problems.append(f"{len(foreign)} request IDs other than req_EXAMPLE placeholders")
    problems += name_problems(document) + summary_problems(document, agent_types)
    return problems + isolation_problems(document)


def privacy_report(documents: Sequence[JSON]) -> list[str]:
    """privacy_problems of every document of a file, with the subagents its session
    started (hook files) taken into account."""
    agent_types = started_agent_types(payload_of(document) for document in documents)
    return [
        f"record {number}: {problem}"
        for number, document in enumerate(documents, start=1)
        for problem in privacy_problems(document, agent_types)
    ]


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_file_holds_no_personal_data(name: str) -> None:
    assert privacy_report(fixture_documents(name)) == []


def test_privacy_guard_reports_planted_data_without_echoing_it() -> None:
    planted: JSON = {
        "cwd": "C:\\Users\\jdoe\\work",
        "note": "ask jdoe@corp.invalid",
        "file_path": "D:\\work\\acme\\notes.txt",
        "summary": "Resumo: ação",
        "attributes": [
            {"key": "user.email", "value": {"stringValue": "jdoe@corp.invalid"}},
            {"key": "host.name", "value": {"stringValue": "JDOE-LAPTOP"}},
            {"key": "request_id", "value": {"stringValue": "req_011CAbCdEfGhIjKlMnOpQrSt"}},
        ],
    }

    report = "\n".join(privacy_problems(planted))

    for kind in (
        "user path at $.cwd",
        "email at $.note",
        "absolute path outside fake home at $.file_path",
        "non-ascii letter at $.summary",
        "user.email other than its placeholder",
        "host.name other than its placeholder",
        "request_id other than a req_EXAMPLE placeholder",
        "request IDs other than req_EXAMPLE placeholders",
    ):
        assert kind in report
    for value in ("jdoe", "corp.invalid", "JDOE", "req_011C", "acme", "notes.txt", "Resumo", "ç"):
        assert value not in report


def test_privacy_guard_reports_real_names_ids_and_summaries_without_echoing_them() -> None:
    compact: JSON = {
        "offset_ms": 0,
        "transport": "http",
        "payload": {
            "hook_event_name": "PostCompact",
            "compact_summary": "The user keeps acme-secret notes",
            "tool_name": "mcp__acme-db__query",
            "mcp_server": {"name": "acme-db", "source": "dynamic"},
        },
    }
    internal_stop: JSON = {
        "offset_ms": 1,
        "transport": "http",
        "payload": {
            "hook_event_name": "SubagentStop",
            "agent_type": "",
            "last_assistant_message": "acme-secret next steps",
        },
    }
    log_record: JSON = {
        "attributes": [
            {"key": "plugin.name", "value": {"stringValue": "acme-tools"}},
            {"key": "marketplace.name", "value": {"stringValue": "plugin-2"}},
            {"key": "skill.name", "value": {"stringValue": "simplify"}},
            {"key": "mcp_server.name", "value": {"stringValue": "custom"}},
            {"key": "mcp_server_name", "value": {"stringValue": "spike-elicit"}},
            {"key": "plugin_id_hash", "value": {"stringValue": "abcdef0123456789"}},
            {
                "key": "client_request_id",
                "value": {"stringValue": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"},
            },
            {"key": "tool_use_id", "value": {"stringValue": "toolu_01AbCdEfGhIjKlMnOpQrStUv"}},
            {"key": "tool_name", "value": {"stringValue": "mcp__spike-elicit__pick_color"}},
        ]
    }

    report = "\n".join(
        problem
        for document in (compact, internal_stop, log_record)
        for problem in privacy_problems(document)
    )

    for kind in (
        "internal summary compact_summary is not the placeholder at $.payload.compact_summary",
        "internal summary last_assistant_message is not the placeholder",
        "MCP server in a tool name is not a placeholder at $.payload.tool_name",
        "mcp_server.name is not a placeholder at $.payload.mcp_server.name",
        "plugin.name is not a placeholder at $.attributes[0]",
        "marketplace.name is not a placeholder at $.attributes[1]",
        "plugin id hash at $.attributes[5]",
        "client request id at $.attributes[6]",
        "tool use id at $.attributes[7]",
    ):
        assert kind in report
    for kept in ("attributes[2]", "attributes[3]", "attributes[4]", "attributes[8]"):
        assert kept not in report
    for value in ("acme", "abcdef01", "aaaaaaaa", "toolu_01Ab", "plugin-2"):
        assert value not in report
    placeholders: JSON = {
        "offset_ms": 0,
        "transport": "http",
        "payload": {"hook_event_name": "PostCompact", "compact_summary": INTERNAL_SUMMARY},
    }
    assert privacy_problems(placeholders) == []


def hook_line(payload: JSONObject) -> JSON:
    return {"offset_ms": 0, "transport": "http", "payload": payload}


def test_privacy_report_keeps_only_subagents_the_session_started() -> None:
    agent_call = hook_line(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"description": "d", "prompt": "p", "subagent_type": "acme-reviewer"},
        }
    )
    started = hook_line(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "acme-reviewer",
            "last_assistant_message": "ok",
        }
    )
    explore = hook_line(
        {"hook_event_name": "SubagentStop", "agent_type": "Explore", "last_assistant_message": "2"}
    )
    unknown = hook_line(
        {"hook_event_name": "SubagentStop", "agent_type": "other", "last_assistant_message": "x"}
    )

    assert privacy_report([agent_call, started, explore, unknown]) == [
        "record 4: internal summary last_assistant_message is not the placeholder"
        " at $.payload.last_assistant_message"
    ]
    assert len(privacy_report([started, explore])) == 1


def test_privacy_guard_reports_instructions_from_outside_the_workspace() -> None:
    user_memory = hook_line(
        {
            "hook_event_name": "InstructionsLoaded",
            "memory_type": "User",
            "file_path": "C:\\Users\\user\\.claude\\CLAUDE.md",
            "load_reason": "session_start",
        }
    )
    synthetic = hook_line(
        {
            "hook_event_name": "InstructionsLoaded",
            "memory_type": "Project",
            "file_path": "C:\\Users\\user\\projects\\capture\\headless-x\\CLAUDE.md",
            "load_reason": "session_start",
        }
    )

    report = "\n".join(privacy_problems(user_memory))

    assert "instructions not from the capture workspace at $.payload.memory_type" in report
    assert "instructions file outside the capture workspace at $.payload.file_path" in report
    assert "User" not in report
    assert privacy_problems(synthetic) == []


# Values copied from a real capture belong in the fixture files only. Tests that need one
# read it from the fixtures at run time.

REPOSITORY: Final = FIXTURES.parent.parent
SOURCE_DIRS: Final = ("src", "tests", "tools")
SOURCE_SUFFIXES: Final = frozenset({".py", ".sh", ".md", ".json", ".yaml", ".toml"})
ID_SHAPES: Final = (
    re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"),
    re.compile(r"(?:srvtoolu|toolu|msg|req|user)_((?=[A-Za-z_]*[0-9])[A-Za-z0-9_]{8,})"),
    re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{8,}(?![A-Za-z0-9])"),
    re.compile(r"(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{10,}"),
)
# The sanitizer's fakes, which the fixtures and the tests share on purpose.
FAKE_ID: Final = re.compile(r"EXAMPLE|^(?:[0-]|4000|8000|a000)*[0-9]{0,4}$")


def source_files() -> list[Path]:
    """The repository's source, test and tool files, without the fixtures and raw output."""
    return [
        path
        for directory in SOURCE_DIRS
        for path in sorted((REPOSITORY / directory).rglob("*"))
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and FIXTURES not in path.parents
        and "out" not in path.relative_to(REPOSITORY).parts
    ]


def id_tokens(text: str) -> set[str]:
    """ID-shaped tokens of ``text`` (and the body of prefixed IDs such as ``toolu_...``),
    without the sanitizer's fakes."""
    found = {
        token
        for shape in ID_SHAPES
        for match in shape.finditer(text)
        for token in (match.group(), *match.groups())
        if token and len(token) >= 8
    }
    return {token for token in found if not FAKE_ID.search(token)}


def test_tests_and_tools_hard_code_no_fixture_values() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.suffix in (".json", ".jsonl")
    )

    shared = sorted(
        f"{path.relative_to(REPOSITORY).as_posix()}: {token[:4]}... ({len(token)} chars)"
        for path in source_files()
        for token in id_tokens(path.read_text(encoding="utf-8"))
        if token in fixture_text
    )

    assert shared == []


def test_fixture_value_check_spots_copied_ids_and_skips_fakes() -> None:
    text = (
        "toolu_01AbCdEfGhIjKlMnOpQrStUv aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1 abcdef0123456789"
        " req_EXAMPLE0001 toolu_01EXAMPLE000000000000001 0000000000000001"
        " 00000000-0000-4000-a000-000000000002 00000000-0000-4000-8000-000000000001"
        " user_prompt_length tool_result_size_bytes msg_stop_reason_unknown"
    )

    assert id_tokens(text) == {
        "toolu_01AbCdEfGhIjKlMnOpQrStUv",
        "01AbCdEfGhIjKlMnOpQrStUv",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
        "aaaaaaaa",
        "aaaaaaaaaaa1",
        "abcdef0123456789",
    }
