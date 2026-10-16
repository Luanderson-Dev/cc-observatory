"""Unit tests of ``ccobs.domain.hooks``, the per-event allowlist of hook metadata (ADR 0023).

Every captured hook payload (ADR 0012) must parse to its event class, and no content or
path besides ``cwd`` may survive parsing: a canary placed in every content and path
field, in unknown keys and in nested values must not show up in the event's repr or in
its ``dataclasses.asdict`` form. Synthetic payloads cover the fields and events that only
the official hooks reference documents.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from ccobs.domain import hooks
from ccobs.domain.hooks import (
    BackgroundTask,
    HookEvent,
    HookPayloadError,
    UnknownHookEvent,
    parse_hook,
)

HOOK_FIXTURES: Final = Path(__file__).parent / "fixtures" / "hooks"

SESSION: Final = "00000000-0000-4000-8000-000000000000"

# The class each event name parses to. Events that are not here parse to UnknownHookEvent.
EXPECTED_CLASSES: Final[dict[str, type[HookEvent]]] = {
    "InstructionsLoaded": hooks.InstructionsLoaded,
    "SessionStart": hooks.SessionStart,
    "UserPromptSubmit": hooks.UserPromptSubmit,
    "PreToolUse": hooks.PreToolUse,
    "PermissionRequest": hooks.PermissionRequest,
    "PostToolUse": hooks.PostToolUse,
    "PostToolUseFailure": hooks.PostToolUseFailure,
    "PermissionDenied": hooks.PermissionDenied,
    "PostToolBatch": hooks.PostToolBatch,
    "Notification": hooks.Notification,
    "Elicitation": hooks.Elicitation,
    "ElicitationResult": hooks.ElicitationResult,
    "SubagentStart": hooks.SubagentStart,
    "SubagentStop": hooks.SubagentStop,
    "TaskCreated": hooks.TaskCreated,
    "TaskCompleted": hooks.TaskCompleted,
    "Stop": hooks.Stop,
    "StopFailure": hooks.StopFailure,
    "PreCompact": hooks.PreCompact,
    "PostCompact": hooks.PostCompact,
    "SessionEnd": hooks.SessionEnd,
}

# Fields that must never be stored without the content opt-in (ADR 0023): content, paths
# other than cwd, and free text. Some are only in the hooks reference (url, content,
# globs, trigger_file_path, parent_file_path, session_title, title, error_details), and
# session_crons carries cron prompts. PermissionDenied's reason is free text, but reason
# is kept on SessionEnd, so the canary test plants it on PermissionDenied only.
NEVER_KEPT: Final = frozenset(
    {
        "prompt",
        "tool_input",
        "tool_response",
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
    }
)

CANARY: Final = "CANARY-7f3c"
CWD: Final = "CWD-KEPT-91ab"


def fixture_payloads() -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(HOOK_FIXTURES.glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            payload = json.loads(line)["payload"]
            yield f"{path.stem}:{number}:{payload['hook_event_name']}", payload


FIXTURE_PAYLOADS: Final = list(fixture_payloads())
FIXTURE_IDS: Final = [name for name, _ in FIXTURE_PAYLOADS]


def fixture_payload(
    event: str, predicate: Callable[[dict[str, Any]], object] | None = None
) -> dict[str, Any]:
    """The first fixture payload of ``event`` that ``predicate`` accepts."""
    for _, payload in FIXTURE_PAYLOADS:
        if payload["hook_event_name"] == event and (predicate is None or predicate(payload)):
            return copy.deepcopy(payload)
    raise AssertionError(f"no fixture payload for {event}")


def payload(event: str, **fields: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "hook_event_name": event, **fields}


# Synthetic payloads for what only the hooks reference documents: events never fired in
# the capture, and documented fields absent from the fixtures.
DOCUMENTED_ONLY: Final = [
    payload("Setup", trigger="init"),
    payload("UserPromptExpansion", prompt="/x", expanded_prompt="secret"),
    payload(
        "PermissionDenied",
        tool_name="mcp__srv__tool",
        tool_input={"command": "rm"},
        tool_use_id="toolu_1",
        reason="[Irreversible Local Destruction]",
    ),
    payload("TeammateIdle", teammate_name="t"),
    payload("ConfigChange", source="user_settings", file_path="/home/u/.claude/s.json"),
    payload("CwdChanged", old_cwd="/home/u/a", new_cwd="/home/u/b"),
    payload("DirectoryAdded", directory="/home/u/b"),
    payload("PostModelSwitch", from_model="a", to_model="b"),
    payload("SessionStart", source="resume", model="m", session_title="t", context_tokens=1),
    payload("UserPromptSubmit", prompt="hello", session_title="t"),
    payload("Notification", notification_type="auth_success", message="m", title="t"),
    payload("Elicitation", mcp_server_name="s", mode="url", url="https://x", elicitation_id="e"),
    payload("ElicitationResult", mcp_server_name="s", action="accept", content={"u": "x"}),
    payload("StopFailure", error="rate_limit", error_details="429 Too Many Requests"),
    payload("InstructionsLoaded", file_path="/h/u/C.md", globs=["*"], trigger_file_path="/a"),
    payload(
        "Stop",
        stop_hook_active=True,
        background_tasks=[
            {"id": "t1", "type": "shell", "status": "running", "description": "d", "command": "c"},
            {"id": "t2", "type": "MCP task", "status": "running", "server": "s", "tool": "t"},
            {"id": "t3", "type": "workflow", "status": "running", "name": "n"},
        ],
        session_crons=[{"id": "c", "schedule": "0 9 * * *", "recurring": True, "prompt": "p"}],
    ),
    payload("NoSuchEventYet", anything="x"),
]


def with_canaries(original: dict[str, Any]) -> dict[str, Any]:
    """``original`` with a canary in every never-kept field, nested values and unknown keys."""
    poisoned = copy.deepcopy(original)
    nested = {"text": CANARY, "items": [CANARY, {"deep": CANARY}], "path": f"/home/{CANARY}"}
    for key in NEVER_KEPT:
        poisoned[key] = f"{CANARY}-{key}"
    poisoned |= {
        "tool_input": nested,
        "tool_response": {**nested, "stdout": CANARY},
        "tool_calls": [{"tool_name": "Read", "tool_input": nested, "tool_response": CANARY}],
        "requested_schema": {"properties": {CANARY: {"title": CANARY}}},
        "permission_suggestions": [{"rules": [{"ruleContent": CANARY}]}],
        "session_crons": [{"id": "c", "prompt": CANARY}],
        "content": nested,
        "x_unknown_field": CANARY,
        "x_unknown_nested": {"deep": [CANARY, {CANARY: CANARY}]},
        CANARY: CANARY,
        "cwd": CWD,
        "effort": {**original.get("effort", {"level": "medium"}), "x_extra": CANARY},
        "mcp_server": {"name": "server", "source": CANARY, "x_extra": CANARY},
    }
    if original["hook_event_name"] == "PermissionDenied":
        poisoned["reason"] = f"[{CANARY}] Auto mode could not evaluate this action"
    if original["hook_event_name"] in {"PostToolUseFailure", "StopFailure"}:
        poisoned["error"] = f"Exit code 2 {CANARY}"
    else:
        poisoned["error"] = CANARY
    tasks = original.get("background_tasks")
    items = tasks if isinstance(tasks, list) and tasks else [{"id": "t", "type": "shell"}]
    poisoned["background_tasks"] = [
        {
            **item,
            "description": CANARY,
            "command": CANARY,
            "server": CANARY,
            "tool": CANARY,
            "name": CANARY,
            "x_unknown": {"deep": CANARY},
        }
        for item in items
    ]
    return poisoned


def all_field_names(cls: type[Any]) -> set[str]:
    names = {f.name for f in dataclasses.fields(cls)}
    if cls is not BackgroundTask:
        names |= all_field_names(BackgroundTask)
    return names


# Every fixture line parses to its class.


def test_fixtures_hold_hook_payloads() -> None:
    assert len(FIXTURE_PAYLOADS) > 100


def test_every_fixture_event_has_a_class() -> None:
    events = {p["hook_event_name"] for _, p in FIXTURE_PAYLOADS}

    assert events <= set(EXPECTED_CLASSES)


@pytest.mark.parametrize(("name", "original"), FIXTURE_PAYLOADS, ids=FIXTURE_IDS)
def test_fixture_payload_parses_to_its_class(name: str, original: dict[str, Any]) -> None:
    event = parse_hook(copy.deepcopy(original))

    assert type(event) is EXPECTED_CLASSES[original["hook_event_name"]], name
    assert event.session_id == original["session_id"]
    assert event.hook_event_name == original["hook_event_name"]
    assert event.prompt_id == original.get("prompt_id")
    assert event.permission_mode == original.get("permission_mode")
    assert event.agent_id == original.get("agent_id")
    assert event.agent_type == original.get("agent_type")
    assert event.cwd == original["cwd"]


def test_event_classes_are_frozen() -> None:
    event = parse_hook(fixture_payload("Stop"))
    attribute = "session_id"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, attribute, "other")


# Nothing but the allowlist survives.


@pytest.mark.parametrize("cls", [*EXPECTED_CLASSES.values(), UnknownHookEvent, BackgroundTask])
def test_no_class_has_a_never_kept_field(cls: type[Any]) -> None:
    assert all_field_names(cls).isdisjoint(NEVER_KEPT)


def assert_no_canary(event: HookEvent) -> None:
    stored = json.dumps(dataclasses.asdict(event), ensure_ascii=False)

    assert CANARY not in repr(event)
    assert CANARY not in stored
    assert CWD in stored
    assert CWD not in repr(event)


@pytest.mark.parametrize(("name", "original"), FIXTURE_PAYLOADS, ids=FIXTURE_IDS)
def test_canaries_in_fixture_payloads_are_dropped(name: str, original: dict[str, Any]) -> None:
    event = parse_hook(with_canaries(original))

    assert type(event) is EXPECTED_CLASSES[original["hook_event_name"]], name
    assert_no_canary(event)


@pytest.mark.parametrize(
    "original", DOCUMENTED_ONLY, ids=[p["hook_event_name"] for p in DOCUMENTED_ONLY]
)
def test_canaries_in_documented_only_payloads_are_dropped(original: dict[str, Any]) -> None:
    assert_no_canary(parse_hook(with_canaries(original)))


@pytest.mark.parametrize(
    "original", DOCUMENTED_ONLY, ids=[p["hook_event_name"] for p in DOCUMENTED_ONLY]
)
def test_documented_only_payloads_parse(original: dict[str, Any]) -> None:
    event = parse_hook(copy.deepcopy(original))

    expected = EXPECTED_CLASSES.get(original["hook_event_name"], UnknownHookEvent)
    assert type(event) is expected


def test_cwd_is_kept_but_left_out_of_repr() -> None:
    original = fixture_payload("SessionStart")

    event = parse_hook(original)

    assert event.cwd == original["cwd"]
    assert "cwd" not in repr(event)
    assert original["cwd"] not in repr(event)
    assert dataclasses.asdict(event)["cwd"] == original["cwd"]


# Per-event fields, from the fixtures.


def test_tool_events_keep_tool_metadata() -> None:
    original = fixture_payload("PostToolUse", lambda p: p["tool_name"] == "Bash")

    event = parse_hook(original)

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_name == "Bash"
    assert event.tool_use_id == original["tool_use_id"]
    assert event.duration_ms == original["duration_ms"]
    assert event.effort_level == "medium"
    assert event.permission_mode == original["permission_mode"]
    assert event.mcp_server_name is None


def test_mcp_tool_events_name_the_server_from_the_mcp_server_field() -> None:
    original = fixture_payload("PreToolUse", lambda p: "mcp_server" in p)

    event = parse_hook(original)

    assert isinstance(event, hooks.PreToolUse)
    assert event.mcp_server_name == original["mcp_server"]["name"] == "spike-elicit"


@pytest.mark.parametrize(
    ("tool_name", "server"),
    [
        ("mcp__github__create_issue", "github"),
        ("mcp__spike-elicit__pick_color", "spike-elicit"),
        ("mcp__server__tool__with__underscores", "server"),
        ("mcp__onlyserver", None),
        ("mcp____tool", None),
        ("mcp__server__", None),
        ("Bash", None),
    ],
)
def test_mcp_server_falls_back_to_the_tool_name(tool_name: str, server: str | None) -> None:
    event = parse_hook(payload("PermissionRequest", tool_name=tool_name))

    assert isinstance(event, hooks.PermissionRequest)
    assert event.mcp_server_name == server


def test_mcp_server_field_wins_over_the_tool_name() -> None:
    event = parse_hook(
        payload("PreToolUse", tool_name="mcp__a__tool", mcp_server={"name": "b", "source": "x"})
    )

    assert isinstance(event, hooks.PreToolUse)
    assert event.mcp_server_name == "b"


def test_post_tool_use_failure_keeps_is_interrupt_and_drops_the_error_text() -> None:
    original = fixture_payload("PostToolUseFailure")

    event = parse_hook(original)

    assert isinstance(event, hooks.PostToolUseFailure)
    assert event.is_interrupt is False
    assert event.duration_ms == original["duration_ms"]
    assert original["error"] not in repr(event)
    assert "error" not in {f.name for f in dataclasses.fields(event)}


def test_permission_request_has_no_tool_use_id() -> None:
    event = parse_hook(fixture_payload("PermissionRequest"))

    assert isinstance(event, hooks.PermissionRequest)
    assert event.tool_use_id is None
    assert event.tool_name in {"Bash", "PowerShell", "Write"}


def test_subagent_tool_events_carry_the_agent() -> None:
    original = fixture_payload("PreToolUse", lambda p: "agent_id" in p)

    event = parse_hook(original)

    assert event.agent_id == original["agent_id"]
    assert event.agent_type == "Explore"


def test_internal_subagent_stop_keeps_the_empty_agent_type() -> None:
    event = parse_hook(fixture_payload("SubagentStop", lambda p: p["agent_type"] == ""))

    assert isinstance(event, hooks.SubagentStop)
    assert event.agent_type == ""
    assert event.stop_hook_active is False


def test_notification_keeps_its_type() -> None:
    event = parse_hook(
        fixture_payload("Notification", lambda p: p["notification_type"] == "idle_prompt")
    )

    assert isinstance(event, hooks.Notification)
    assert event.notification_type == "idle_prompt"


def test_elicitation_keeps_server_and_mode() -> None:
    event = parse_hook(fixture_payload("Elicitation"))

    assert isinstance(event, hooks.Elicitation)
    assert (event.mcp_server_name, event.mode) == ("spike-elicit", "form")


def test_elicitation_result_keeps_server_mode_and_action() -> None:
    event = parse_hook(fixture_payload("ElicitationResult"))

    assert isinstance(event, hooks.ElicitationResult)
    assert (event.mcp_server_name, event.mode, event.action) == ("spike-elicit", "form", "cancel")


def test_session_start_keeps_source_and_model() -> None:
    event = parse_hook(fixture_payload("SessionStart", lambda p: "model" in p))

    assert isinstance(event, hooks.SessionStart)
    assert event.source == "startup"
    assert event.model == "claude-haiku-5-5"


def test_session_start_without_model() -> None:
    event = parse_hook(fixture_payload("SessionStart", lambda p: "model" not in p))

    assert isinstance(event, hooks.SessionStart)
    assert event.model is None


def test_session_end_keeps_the_reason() -> None:
    event = parse_hook(fixture_payload("SessionEnd", lambda p: p["reason"] == "prompt_input_exit"))

    assert isinstance(event, hooks.SessionEnd)
    assert event.reason == "prompt_input_exit"


@pytest.mark.parametrize("name", ["PreCompact", "PostCompact"])
def test_compaction_events_keep_the_trigger(name: str) -> None:
    event = parse_hook(fixture_payload(name))

    assert isinstance(event, hooks.PreCompact | hooks.PostCompact)
    assert event.trigger == "manual"


# StopFailure's error code.


def test_stop_failure_keeps_the_fixture_error_code() -> None:
    event = parse_hook(fixture_payload("StopFailure"))

    assert isinstance(event, hooks.StopFailure)
    assert event.error == "model_not_found"
    assert event.effort_level == "high"


@pytest.mark.parametrize("code", ["model_not_found", "rate_limit", "unknown", "a" + "b" * 63])
def test_stop_failure_keeps_codes(code: str) -> None:
    event = parse_hook(payload("StopFailure", error=code))

    assert isinstance(event, hooks.StopFailure)
    assert event.error == code


@pytest.mark.parametrize(
    "error",
    [
        "API Error: Rate limit reached",
        "model: claude-x",
        "a" * 65,
        "model_not_found\n",
        "Model_not_found",
        "1_error",
        "_error",
        "",
        "rate-limit",
        "réseau",
        42,
        None,
        ["model_not_found"],
    ],
)
def test_stop_failure_drops_anything_but_a_code(error: object) -> None:
    event = parse_hook(payload("StopFailure", error=error))

    assert isinstance(event, hooks.StopFailure)
    assert event.error is None


# Sizes.


def test_prompt_length_counts_characters() -> None:
    event = parse_hook(payload("UserPromptSubmit", prompt="olá 🌍"))

    assert isinstance(event, hooks.UserPromptSubmit)
    assert event.prompt_length == 5


def test_prompt_length_of_a_fixture_prompt() -> None:
    original = fixture_payload("UserPromptSubmit")

    event = parse_hook(original)

    assert isinstance(event, hooks.UserPromptSubmit)
    assert event.prompt_length == len(original["prompt"])


def test_tool_sizes_are_utf8_bytes_of_compact_json() -> None:
    event = parse_hook(
        payload("PostToolUse", tool_name="Write", tool_input={"text": "é"}, tool_response="ok")
    )

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_input_bytes == 13  # {"text":"é"}: 12 characters, é takes 2 bytes
    assert event.tool_response_bytes == 4  # "ok" with its quotes


def test_tool_sizes_of_a_fixture_payload() -> None:
    original = fixture_payload("PostToolUse")

    event = parse_hook(original)

    def size(value: object) -> int:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_input_bytes == size(original["tool_input"])
    assert event.tool_response_bytes == size(original["tool_response"])


def test_pre_tool_use_has_an_input_size() -> None:
    event = parse_hook(payload("PreToolUse", tool_name="Read", tool_input={}))

    assert isinstance(event, hooks.PreToolUse)
    assert event.tool_input_bytes == 2


def test_absent_or_null_sizes_are_none() -> None:
    event = parse_hook(payload("PostToolUse", tool_name="Read", tool_input=None))

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_input_bytes is None
    assert event.tool_response_bytes is None


def test_unserializable_tool_input_has_no_size() -> None:
    event = parse_hook(payload("PreToolUse", tool_name="Read", tool_input={1, 2}))

    assert isinstance(event, hooks.PreToolUse)
    assert event.tool_input_bytes is None


# background_tasks.


def test_background_tasks_keep_only_the_allowed_keys() -> None:
    original = fixture_payload("Stop", lambda p: p.get("background_tasks"))

    event = parse_hook(original)

    (task,) = original["background_tasks"]
    assert isinstance(event, hooks.Stop)
    assert event.background_tasks == (
        BackgroundTask(id=task["id"], type="subagent", status="running", agent_type="Explore"),
    )
    assert task["description"] not in repr(event)


def test_empty_and_absent_background_tasks() -> None:
    empty = parse_hook(fixture_payload("SubagentStop", lambda p: p.get("background_tasks") == []))
    absent = parse_hook(payload("Stop"))

    assert isinstance(empty, hooks.SubagentStop)
    assert empty.background_tasks == ()
    assert isinstance(absent, hooks.Stop)
    assert absent.background_tasks is None
    assert absent.stop_hook_active is None


def test_background_tasks_skip_non_objects_and_drop_wrong_types() -> None:
    event = parse_hook(
        payload("Stop", background_tasks=[1, "x", None, {"id": 2, "type": "shell", "status": []}])
    )

    assert isinstance(event, hooks.Stop)
    assert event.background_tasks == (
        BackgroundTask(id=None, type="shell", status=None, agent_type=None),
    )


# Unknown events and bad input.


@pytest.mark.parametrize("name", ["Setup", "UserPromptExpansion", "SomethingNew", "stop"])
def test_unknown_event_names_map_to_unknown_hook_event(name: str) -> None:
    event = parse_hook(
        payload(name, prompt_id="p", cwd="/x", tool_name="Bash", source="s", reason="r")
    )

    assert type(event) is UnknownHookEvent
    assert event.hook_event_name == name
    assert event.prompt_id == "p"
    assert {f.name for f in dataclasses.fields(event)} == {
        f.name for f in dataclasses.fields(HookEvent)
    }


def test_hook_payload_error_is_a_value_error() -> None:
    assert issubclass(HookPayloadError, ValueError)


@pytest.mark.parametrize("obj", [None, [], "text", 1, ["session_id"]])
def test_non_objects_are_rejected(obj: object) -> None:
    with pytest.raises(HookPayloadError):
        parse_hook(obj)


@pytest.mark.parametrize(
    "obj",
    [
        {"hook_event_name": "Stop"},
        {"session_id": "", "hook_event_name": "Stop"},
        {"session_id": None, "hook_event_name": "Stop"},
        {"session_id": 7, "hook_event_name": "Stop"},
        {"session_id": SESSION},
        {"session_id": SESSION, "hook_event_name": ""},
        {"session_id": SESSION, "hook_event_name": 3},
        {"session_id": SESSION, "hook_event_name": ["Stop"]},
    ],
)
def test_missing_or_invalid_required_fields_are_rejected(obj: dict[str, object]) -> None:
    with pytest.raises(HookPayloadError):
        parse_hook(obj)


def test_errors_do_not_echo_the_payload() -> None:
    with pytest.raises(HookPayloadError) as raised:
        parse_hook({"session_id": [CANARY], "hook_event_name": "Stop", "prompt": CANARY})

    assert CANARY not in str(raised.value)


# Wrong types in optional fields.


def test_wrong_typed_optional_fields_become_none() -> None:
    event = parse_hook(
        payload(
            "PostToolUseFailure",
            prompt_id=1,
            permission_mode=["default"],
            effort="high",
            agent_id=True,
            agent_type=3.5,
            cwd=5,
            tool_name=5,
            tool_use_id={"id": "x"},
            mcp_server="server",
            duration_ms="5",
            is_interrupt="false",
        )
    )

    assert isinstance(event, hooks.PostToolUseFailure)
    assert (event.prompt_id, event.permission_mode, event.effort_level) == (None, None, None)
    assert (event.agent_id, event.agent_type, event.cwd) == (None, None, None)
    assert (event.tool_name, event.tool_use_id, event.mcp_server_name) == (None, None, None)
    assert (event.duration_ms, event.is_interrupt) == (None, None)


@pytest.mark.parametrize("effort", [{"level": 3}, {}, ["medium"], None])
def test_wrong_typed_effort_level_becomes_none(effort: object) -> None:
    assert parse_hook(payload("Stop", effort=effort)).effort_level is None


@pytest.mark.parametrize("duration", [True, 1.5, "12", None])
def test_duration_must_be_an_integer(duration: object) -> None:
    event = parse_hook(payload("PostToolUse", duration_ms=duration))

    assert isinstance(event, hooks.PostToolUse)
    assert event.duration_ms is None


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("SessionStart", {"source": 1, "model": None}),
        ("UserPromptSubmit", {"prompt": ["x"]}),
        ("Notification", {"notification_type": 1}),
        ("Elicitation", {"mcp_server_name": 1, "mode": {}}),
        ("ElicitationResult", {"mcp_server_name": [], "mode": 2, "action": False}),
        ("SessionEnd", {"reason": 0}),
        ("PreCompact", {"trigger": None}),
        ("PostCompact", {"trigger": 1}),
        ("Stop", {"stop_hook_active": 0, "background_tasks": "none"}),
        ("SubagentStop", {"stop_hook_active": "true", "background_tasks": {"id": "x"}}),
        ("PreToolUse", {"tool_name": "Read", "mcp_server": {"name": 1}}),
    ],
)
def test_wrong_typed_event_fields_become_none(name: str, fields: dict[str, object]) -> None:
    event = parse_hook(payload(name, **fields))
    common = {f.name for f in dataclasses.fields(HookEvent)}

    extra = {
        f.name: getattr(event, f.name)
        for f in dataclasses.fields(event)
        if f.name not in common and f.name != "tool_name"
    }
    assert extra
    assert set(extra.values()) == {None}


# Fields added for the state machine: tool_response.status, elicitation_id, PermissionDenied.


def agent_status(status: str) -> Callable[[dict[str, Any]], object]:
    def matches(p: dict[str, Any]) -> object:
        response = p["tool_response"]
        return isinstance(response, dict) and response.get("status") == status

    return matches


@pytest.mark.parametrize("status", ["completed", "async_launched"])
def test_post_tool_use_keeps_the_fixture_agent_status(status: str) -> None:
    original = fixture_payload("PostToolUse", agent_status(status))

    event = parse_hook(original)

    assert isinstance(event, hooks.PostToolUse)
    assert original["tool_name"] == "Agent"
    assert event.tool_response_status == status


@pytest.mark.parametrize(
    "tool_response",
    [
        "completed",
        {"status": "Completed"},
        {"status": "done: 3 files"},
        {"status": "a" * 65},
        {"status": 1},
        {"stdout": "x"},
        ["completed"],
        None,
    ],
)
def test_tool_response_status_must_be_a_code(tool_response: object) -> None:
    event = parse_hook(payload("PostToolUse", tool_name="Bash", tool_response=tool_response))

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_response_status is None


def test_tool_response_keeps_only_its_status() -> None:
    original = fixture_payload("PostToolUse", lambda p: p["tool_name"] == "Agent")
    original["tool_response"] = {
        **original["tool_response"],
        "content": [{"type": "text", "text": CANARY}],
        "prompt": CANARY,
        "x_unknown": {"deep": CANARY},
    }

    event = parse_hook(original)

    assert isinstance(event, hooks.PostToolUse)
    assert event.tool_response_status in {"completed", "async_launched"}
    assert CANARY not in repr(event)
    assert CANARY not in json.dumps(dataclasses.asdict(event))


@pytest.mark.parametrize("name", ["Elicitation", "ElicitationResult"])
def test_elicitations_keep_the_elicitation_id(name: str) -> None:
    event = parse_hook(payload(name, mcp_server_name="s", elicitation_id="elicit-123"))

    assert isinstance(event, hooks.Elicitation | hooks.ElicitationResult)
    assert event.elicitation_id == "elicit-123"


def test_permission_denied_is_a_tool_event_without_its_reason() -> None:
    event = parse_hook(
        payload(
            "PermissionDenied",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/build"},
            tool_use_id="toolu_1",
            mcp_server={"name": "srv", "source": "user"},
            reason="[Irreversible Local Destruction]",
        )
    )

    assert isinstance(event, hooks.PermissionDenied)
    assert isinstance(event, hooks.ToolEvent)
    assert (event.tool_name, event.tool_use_id, event.mcp_server_name) == ("Bash", "toolu_1", "srv")
    assert event.tool_input_bytes is not None
    assert "reason" not in {f.name for f in dataclasses.fields(event)}
    assert "Irreversible" not in repr(event)


# Bounds: any local process can post to the hook endpoint, and kept values become SQLite
# rows and possibly Prometheus labels.

LIMIT: Final = 256
CWD_LIMIT: Final = 4096
BAD_TEXT: Final = ["a\ud800b", "\udfff", "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\x85b", "\x1b[31m"]


def test_strings_at_the_limit_are_kept() -> None:
    event = parse_hook(payload("SessionStart", source="s" * LIMIT, model="m" * LIMIT))

    assert isinstance(event, hooks.SessionStart)
    assert event.source == "s" * LIMIT
    assert event.model == "m" * LIMIT


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Stop", "prompt_id"),
        ("Stop", "permission_mode"),
        ("Stop", "agent_id"),
        ("Stop", "agent_type"),
        ("SessionStart", "source"),
        ("SessionStart", "model"),
        ("SessionEnd", "reason"),
        ("PreToolUse", "tool_name"),
        ("PreToolUse", "tool_use_id"),
        ("Notification", "notification_type"),
        ("ElicitationResult", "mcp_server_name"),
        ("ElicitationResult", "mode"),
        ("ElicitationResult", "action"),
        ("Elicitation", "elicitation_id"),
        ("PreCompact", "trigger"),
    ],
)
@pytest.mark.parametrize("value", ["x" * (LIMIT + 1), *BAD_TEXT])
def test_long_or_unsafe_strings_are_dropped(name: str, key: str, value: str) -> None:
    event = parse_hook(payload(name, **{key: value}))

    assert getattr(event, key) is None


@pytest.mark.parametrize("value", ["x" * (LIMIT + 1), *BAD_TEXT])
def test_unsafe_effort_level_and_mcp_server_are_dropped(value: str) -> None:
    event = parse_hook(payload("PreToolUse", effort={"level": value}, mcp_server={"name": value}))

    assert isinstance(event, hooks.PreToolUse)
    assert (event.effort_level, event.mcp_server_name) == (None, None)


@pytest.mark.parametrize("value", ["x" * (LIMIT + 1), *BAD_TEXT])
def test_unsafe_background_task_fields_are_dropped(value: str) -> None:
    task = {"id": value, "type": value, "status": value, "agent_type": value}

    event = parse_hook(payload("Stop", background_tasks=[task]))

    assert isinstance(event, hooks.Stop)
    assert event.background_tasks == (BackgroundTask(),)


def test_cwd_has_a_path_sized_limit() -> None:
    kept = parse_hook(payload("Stop", cwd="/p" * (CWD_LIMIT // 2)))
    dropped = parse_hook(payload("Stop", cwd="/p" * (CWD_LIMIT // 2) + "x"))

    assert kept.cwd is not None
    assert len(kept.cwd) == CWD_LIMIT
    assert dropped.cwd is None


@pytest.mark.parametrize("value", BAD_TEXT)
def test_unsafe_cwd_is_dropped(value: str) -> None:
    assert parse_hook(payload("Stop", cwd=f"/home/u/{value}")).cwd is None


def test_non_ascii_strings_are_kept() -> None:
    event = parse_hook(payload("SessionStart", source="démarrage", cwd="C:\\Users\\usuário\\x"))

    assert isinstance(event, hooks.SessionStart)
    assert event.source == "démarrage"
    assert event.cwd == "C:\\Users\\usuário\\x"


@pytest.mark.parametrize("key", ["session_id", "hook_event_name"])
@pytest.mark.parametrize("value", ["x" * (LIMIT + 1), *BAD_TEXT])
def test_long_or_unsafe_required_fields_are_rejected(key: str, value: str) -> None:
    obj = payload("Stop") | {key: value}

    with pytest.raises(HookPayloadError) as raised:
        parse_hook(obj)

    assert value not in str(raised.value)


def test_required_fields_at_the_limit_are_accepted() -> None:
    event = parse_hook(payload("E" * LIMIT) | {"session_id": "s" * LIMIT})

    assert type(event) is UnknownHookEvent
    assert (len(event.session_id), len(event.hook_event_name)) == (LIMIT, LIMIT)


@pytest.mark.parametrize("value", ["", "x" * (LIMIT + 1), *BAD_TEXT])
def test_session_ids_outside_the_bounds_are_not_valid(value: str) -> None:
    assert not hooks.is_valid_session_id(value)


@pytest.mark.parametrize("value", [SESSION, "s", "s" * LIMIT, "../etc", "é"])
def test_session_ids_within_the_bounds_are_valid(value: str) -> None:
    # The same rule parse_hook applies, so every stored session_id passes it.
    assert hooks.is_valid_session_id(value)
    assert parse_hook(payload("Stop") | {"session_id": value}).session_id == value


def test_prompt_length_is_not_bounded_by_the_string_limit() -> None:
    event = parse_hook(payload("UserPromptSubmit", prompt="line\n" * 1000 + "\ud800"))

    assert isinstance(event, hooks.UserPromptSubmit)
    assert event.prompt_length == 5001


@pytest.mark.parametrize("duration", [0, 1, 2**63 - 1])
def test_durations_in_range_are_kept(duration: int) -> None:
    event = parse_hook(payload("PostToolUse", duration_ms=duration))

    assert isinstance(event, hooks.PostToolUse)
    assert event.duration_ms == duration


@pytest.mark.parametrize("duration", [-1, 2**63, 10**100, True, False])
def test_durations_out_of_range_are_dropped(duration: int) -> None:
    event = parse_hook(payload("PostToolUseFailure", duration_ms=duration))

    assert isinstance(event, hooks.PostToolUseFailure)
    assert event.duration_ms is None


def response_or_input_size(event: HookEvent, key: str) -> int | None:
    assert isinstance(event, hooks.PostToolUse)
    return event.tool_input_bytes if key == "tool_input" else event.tool_response_bytes


@pytest.mark.parametrize("key", ["tool_input", "tool_response"])
def test_lone_surrogates_do_not_break_sizes(key: str) -> None:
    decoded = json.loads('{"value": "\\ud800", "nested": ["a\\udfffb"]}')
    text = json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)

    event = parse_hook(payload("PostToolUse", tool_name="Read", **{key: decoded}))

    assert response_or_input_size(event, key) == len(text.encode("utf-8", "surrogatepass"))


@pytest.mark.parametrize("key", ["tool_input", "tool_response"])
def test_a_lone_surrogate_string_has_a_size(key: str) -> None:
    event = parse_hook(payload("PostToolUse", **{key: json.loads('"\\ud800"')}))

    assert response_or_input_size(event, key) == 5  # the quotes and the surrogate's 3 bytes
