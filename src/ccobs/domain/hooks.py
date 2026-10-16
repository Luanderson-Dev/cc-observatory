"""Hook payloads reduced to metadata: the per-event allowlist of ADR 0023.

Claude Code's hook payloads carry content (prompts, tool inputs and outputs, assistant
messages, error text) and paths that contain the OS username (ADRs 0006 and 0012).
``parse_hook`` turns a decoded payload into a frozen event that holds only the fields
allowed for its event, plus sizes computed from content that is then discarded. Every
other key, known or unknown, is dropped, so a field Claude Code adds later is never
stored by accident. The content opt-in (milestone 4) extends this allowlist; it does not
bypass it.

Field names follow the payloads of Claude Code 2.1.296 in ``tests/fixtures/hooks/``
(ADR 0012). Kept values are bounded: optional fields of the wrong type, out of range or
with unsafe text become ``None``; only a payload that is not an object, or lacks a valid
``session_id`` or ``hook_event_name``, is rejected.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, TypedDict

__all__ = [
    "BackgroundTask",
    "Elicitation",
    "ElicitationResult",
    "HookEvent",
    "HookPayloadError",
    "InstructionsLoaded",
    "Notification",
    "PermissionDenied",
    "PermissionRequest",
    "PostCompact",
    "PostToolBatch",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PreToolUse",
    "SessionEnd",
    "SessionStart",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "TaskCompleted",
    "TaskCreated",
    "ToolEvent",
    "UnknownHookEvent",
    "UserPromptSubmit",
    "is_valid_session_id",
    "parse_hook",
]

# Codes: StopFailure's error (documented codes such as model_not_found) and
# tool_response.status (completed, async_launched). Anything else may be free text.
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Bounds on every kept value (ADR 0023): any local process can post to the hook endpoint,
# and kept values become SQLite rows and possibly Prometheus labels. A string longer than
# its limit, or holding a control character (Unicode category Cc: U+0000 to U+001F and
# U+007F to U+009F) or a lone surrogate (U+D800 to U+DFFF), is dropped. cwd gets a
# path-sized limit, since PATH_MAX is 4096 on Linux and Windows long paths pass 260
# characters. Integers must fit a signed 64-bit SQLite column and be non-negative.
_MAX_TEXT: Final = 256
_MAX_PATH: Final = 4096
_UNSAFE_CHARACTER: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_MAX_INT: Final = 2**63

# MCP tools are named mcp__<server>__<tool>.
_MCP_PREFIX: Final = "mcp__"
_MCP_SEPARATOR: Final = "__"


class HookPayloadError(ValueError):
    """The payload is not a hook payload. The message never echoes payload values."""


# Events.


@dataclass(frozen=True, kw_only=True)
class HookEvent:
    """The fields every event keeps. ``effort_level`` is the payload's ``effort.level``."""

    session_id: str
    hook_event_name: str
    prompt_id: str | None = None
    permission_mode: str | None = None
    effort_level: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    # Personal data (ADR 0006): the working directory contains the OS username. Kept,
    # because projects come from it, but left out of repr so that logging an event never
    # writes it; logging code must not log it either.
    cwd: str | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class UnknownHookEvent(HookEvent):
    """An event name this module does not know: only the common fields are kept."""


@dataclass(frozen=True, kw_only=True)
class InstructionsLoaded(HookEvent):
    """An instruction file was loaded. Its path is dropped."""


@dataclass(frozen=True, kw_only=True)
class SessionStart(HookEvent):
    """A session started. On Bedrock, ``model`` can be an inference-profile ARN that holds
    the AWS account ID: treat it like ``cwd`` in logs and the UI (ADR 0023)."""

    source: str | None = None
    model: str | None = None


@dataclass(frozen=True, kw_only=True)
class SessionEnd(HookEvent):
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class UserPromptSubmit(HookEvent):
    """A prompt was submitted; only its length in characters is kept."""

    prompt_length: int | None = None


@dataclass(frozen=True, kw_only=True)
class ToolEvent(HookEvent):
    """The fields of every tool event. ``tool_input_bytes`` is the size of the input."""

    tool_name: str | None = None
    tool_use_id: str | None = None
    mcp_server_name: str | None = None
    tool_input_bytes: int | None = None


@dataclass(frozen=True, kw_only=True)
class PreToolUse(ToolEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class PermissionRequest(ToolEvent):
    """A permission request. Claude Code sends no ``tool_use_id`` with it (ADR 0012)."""


@dataclass(frozen=True, kw_only=True)
class PermissionDenied(ToolEvent):
    """Auto mode denied a tool call. Its ``reason`` is free text and is dropped."""


@dataclass(frozen=True, kw_only=True)
class PostToolUse(ToolEvent):
    """A tool call succeeded. ``tool_response_status`` is ``tool_response.status`` when it
    is a code: for Agent, ``async_launched`` is the only hook signal that a background
    subagent started (ADR 0012)."""

    duration_ms: int | None = None
    tool_response_bytes: int | None = None
    tool_response_status: str | None = None


@dataclass(frozen=True, kw_only=True)
class PostToolUseFailure(ToolEvent):
    """A failed tool call. Its ``error`` is free text (output, paths) and is dropped."""

    duration_ms: int | None = None
    is_interrupt: bool | None = None


@dataclass(frozen=True, kw_only=True)
class PostToolBatch(HookEvent):
    """A batch of tool calls ended. The calls themselves are content and are dropped."""


@dataclass(frozen=True, kw_only=True)
class Notification(HookEvent):
    notification_type: str | None = None


@dataclass(frozen=True, kw_only=True)
class Elicitation(HookEvent):
    mcp_server_name: str | None = None
    mode: str | None = None
    elicitation_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class ElicitationResult(HookEvent):
    mcp_server_name: str | None = None
    mode: str | None = None
    action: str | None = None
    elicitation_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class SubagentStart(HookEvent):
    """A subagent started; ``agent_id`` and ``agent_type`` are common fields."""


@dataclass(frozen=True, kw_only=True)
class BackgroundTask:
    """An in-flight task listed by Stop and SubagentStop, without its free text."""

    id: str | None = None
    type: str | None = None
    status: str | None = None
    agent_type: str | None = None


@dataclass(frozen=True, kw_only=True)
class Stop(HookEvent):
    """The main agent stopped. ``background_tasks`` is ``None`` when the payload has none."""

    stop_hook_active: bool | None = None
    background_tasks: tuple[BackgroundTask, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class SubagentStop(HookEvent):
    stop_hook_active: bool | None = None
    background_tasks: tuple[BackgroundTask, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class StopFailure(HookEvent):
    """A turn ended on an API error. ``error`` is kept only when it is a code."""

    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskCreated(HookEvent):
    """A task was created. Its id, subject and description are dropped."""


@dataclass(frozen=True, kw_only=True)
class TaskCompleted(HookEvent):
    """A task was completed. Its id, subject and description are dropped."""


@dataclass(frozen=True, kw_only=True)
class PreCompact(HookEvent):
    trigger: str | None = None


@dataclass(frozen=True, kw_only=True)
class PostCompact(HookEvent):
    trigger: str | None = None


# Field readers: each returns None for an absent field, one of the wrong type, or one
# outside the bounds.

type _Payload = Mapping[str, object]


def _safe_text(value: object, limit: int = _MAX_TEXT) -> str | None:
    """``value`` if it is a string of at most ``limit`` characters with no control
    character and no lone surrogate; else ``None``."""
    if not isinstance(value, str) or len(value) > limit or _UNSAFE_CHARACTER.search(value):
        return None
    return value


def is_valid_session_id(value: str) -> bool:
    """Whether ``parse_hook`` would accept ``value`` as a ``session_id``: non-empty, at most
    256 characters, no control character and no lone surrogate. Every stored session ID
    passes it, so a reader can refuse any other value without a lookup."""
    return bool(_safe_text(value))


def _str(payload: _Payload, key: str, limit: int = _MAX_TEXT) -> str | None:
    return _safe_text(payload.get(key), limit)


def _int(payload: _Payload, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < _MAX_INT else None


def _bool(payload: _Payload, key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _object(payload: _Payload, key: str) -> _Payload:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _code(payload: _Payload, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _json_bytes(payload: _Payload, key: str) -> int | None:
    """UTF-8 size of the field as compact JSON; ``None`` when absent, null or unserializable."""
    value = payload.get(key)
    if value is None:
        return None
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return None
    # JSON allows a lone surrogate (an escaped U+D800), which has no UTF-8 form;
    # surrogatepass counts the 3 bytes it would take instead of failing.
    return len(text.encode("utf-8", "surrogatepass"))


def _length(payload: _Payload, key: str) -> int | None:
    """The character count of a content field. The content is never stored, so the
    string bounds do not apply."""
    value = payload.get(key)
    return len(value) if isinstance(value, str) else None


def _mcp_server_name(payload: _Payload) -> str | None:
    """The MCP server of a tool event: ``mcp_server.name``, else the tool name's prefix."""
    name = _str(_object(payload, "mcp_server"), "name")
    if name is not None:
        return name
    tool_name = _str(payload, "tool_name")
    if tool_name is None or not tool_name.startswith(_MCP_PREFIX):
        return None
    server, separator, tool = tool_name.removeprefix(_MCP_PREFIX).partition(_MCP_SEPARATOR)
    return server if separator and server and tool else None


def _background_tasks(payload: _Payload) -> tuple[BackgroundTask, ...] | None:
    items = payload.get("background_tasks")
    if not isinstance(items, list):
        return None
    return tuple(
        BackgroundTask(
            id=_str(item, "id"),
            type=_str(item, "type"),
            status=_str(item, "status"),
            agent_type=_str(item, "agent_type"),
        )
        for item in items
        if isinstance(item, dict)
    )


# Parsing.


class _Common(TypedDict):
    session_id: str
    hook_event_name: str
    prompt_id: str | None
    permission_mode: str | None
    effort_level: str | None
    agent_id: str | None
    agent_type: str | None
    cwd: str | None


class _Tool(TypedDict):
    tool_name: str | None
    tool_use_id: str | None
    mcp_server_name: str | None
    tool_input_bytes: int | None


def _required(payload: _Payload, key: str) -> str:
    value = _safe_text(payload.get(key))
    if not value:
        raise HookPayloadError(f"hook payload has no valid {key!r}")
    return value


def _common(payload: _Payload) -> _Common:
    return _Common(
        session_id=_required(payload, "session_id"),
        hook_event_name=_required(payload, "hook_event_name"),
        prompt_id=_str(payload, "prompt_id"),
        permission_mode=_str(payload, "permission_mode"),
        effort_level=_str(_object(payload, "effort"), "level"),
        agent_id=_str(payload, "agent_id"),
        agent_type=_str(payload, "agent_type"),
        cwd=_str(payload, "cwd", _MAX_PATH),
    )


def _tool(payload: _Payload) -> _Tool:
    return _Tool(
        tool_name=_str(payload, "tool_name"),
        tool_use_id=_str(payload, "tool_use_id"),
        mcp_server_name=_mcp_server_name(payload),
        tool_input_bytes=_json_bytes(payload, "tool_input"),
    )


type _Parser = Callable[[_Payload, _Common], HookEvent]

_PARSERS: Final[Mapping[str, _Parser]] = {
    "InstructionsLoaded": lambda p, c: InstructionsLoaded(**c),
    "SessionStart": lambda p, c: SessionStart(
        **c, source=_str(p, "source"), model=_str(p, "model")
    ),
    "SessionEnd": lambda p, c: SessionEnd(**c, reason=_str(p, "reason")),
    "UserPromptSubmit": lambda p, c: UserPromptSubmit(**c, prompt_length=_length(p, "prompt")),
    "PreToolUse": lambda p, c: PreToolUse(**c, **_tool(p)),
    "PermissionRequest": lambda p, c: PermissionRequest(**c, **_tool(p)),
    "PermissionDenied": lambda p, c: PermissionDenied(**c, **_tool(p)),
    "PostToolUse": lambda p, c: PostToolUse(
        **c,
        **_tool(p),
        duration_ms=_int(p, "duration_ms"),
        tool_response_bytes=_json_bytes(p, "tool_response"),
        tool_response_status=_code(_object(p, "tool_response"), "status"),
    ),
    "PostToolUseFailure": lambda p, c: PostToolUseFailure(
        **c,
        **_tool(p),
        duration_ms=_int(p, "duration_ms"),
        is_interrupt=_bool(p, "is_interrupt"),
    ),
    "PostToolBatch": lambda p, c: PostToolBatch(**c),
    "Notification": lambda p, c: Notification(**c, notification_type=_str(p, "notification_type")),
    "Elicitation": lambda p, c: Elicitation(
        **c,
        mcp_server_name=_str(p, "mcp_server_name"),
        mode=_str(p, "mode"),
        elicitation_id=_str(p, "elicitation_id"),
    ),
    "ElicitationResult": lambda p, c: ElicitationResult(
        **c,
        mcp_server_name=_str(p, "mcp_server_name"),
        mode=_str(p, "mode"),
        action=_str(p, "action"),
        elicitation_id=_str(p, "elicitation_id"),
    ),
    "SubagentStart": lambda p, c: SubagentStart(**c),
    "SubagentStop": lambda p, c: SubagentStop(
        **c,
        stop_hook_active=_bool(p, "stop_hook_active"),
        background_tasks=_background_tasks(p),
    ),
    "Stop": lambda p, c: Stop(
        **c,
        stop_hook_active=_bool(p, "stop_hook_active"),
        background_tasks=_background_tasks(p),
    ),
    "StopFailure": lambda p, c: StopFailure(**c, error=_code(p, "error")),
    "TaskCreated": lambda p, c: TaskCreated(**c),
    "TaskCompleted": lambda p, c: TaskCompleted(**c),
    "PreCompact": lambda p, c: PreCompact(**c, trigger=_str(p, "trigger")),
    "PostCompact": lambda p, c: PostCompact(**c, trigger=_str(p, "trigger")),
}


def parse_hook(obj: object) -> HookEvent:
    """Reduce a decoded hook payload to the metadata its event may keep (ADR 0023).

    Raises ``HookPayloadError`` when ``obj`` is not an object or lacks a valid
    ``session_id`` or ``hook_event_name``: a non-empty string of at most 256 characters
    with no control character or lone surrogate. An unknown event name gives an
    ``UnknownHookEvent`` with the common fields only.
    """
    if not isinstance(obj, dict):
        raise HookPayloadError("hook payload is not a JSON object")
    payload: _Payload = obj
    common = _common(payload)
    parser = _PARSERS.get(common["hook_event_name"])
    if parser is None:
        return UnknownHookEvent(**common)
    return parser(payload, common)
