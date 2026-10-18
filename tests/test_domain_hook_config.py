"""Unit tests of ``ccobs.domain.hook_config``, the registered hook events (ADR 0025).

The documented events come from the Claude Code hooks reference
(https://code.claude.com/docs/en/hooks, "Hook lifecycle" table, read on 2026-10-15). Each
one must land in exactly one category, every registered one must be parsed by
``ccobs.domain.hooks`` and handled by ``ccobs.domain.sessions``, and the settings block
must follow ADR 0011: an http hook with ``timeout: 1``, an async exec-form curl on
``SessionStart``, and never ``onFailure``, ``statusMessage`` or a forbidden event.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Final

import pytest

from ccobs.domain import hook_config, hooks, sessions
from ccobs.domain.hook_config import (
    ASYNC_COMMAND_EVENTS,
    ASYNC_COMMAND_TIMEOUT_S,
    CURL_MAX_TIME_S,
    DEFAULT_HOOK_URL,
    FORBIDDEN_EVENTS,
    HOOK_PATH,
    HTTP_TIMEOUT_S,
    NOT_REGISTERED,
    REGISTERED_HTTP_EVENTS,
    hook_settings,
    is_hook_url,
)
from ccobs.domain.hooks import UnknownHookEvent, parse_hook
from ccobs.domain.sessions import SessionChanged, apply

# Every event in the "Hook lifecycle" table of https://code.claude.com/docs/en/hooks.
DOCUMENTED_EVENTS: Final = frozenset(
    {
        "SessionStart",
        "Setup",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "DirectoryAdded",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "Elicitation",
        "ElicitationResult",
        "SessionEnd",
    }
)
# The events with a parser class in ccobs.domain.hooks.
PARSED_EVENTS: Final = frozenset(hooks._PARSERS)
# CLAUDE.md: registering these changes Claude Code's behaviour.
CLAUDE_MD_FORBIDDEN: Final = frozenset(
    {"WorktreeCreate", "WorktreeRemove", "PreModelSwitch", "MessageDisplay", "FileChanged"}
)
# CLAUDE.md: these do not accept http hooks.
NO_HTTP: Final = frozenset({"SessionStart", "Setup"})
# Keys that must never appear in the settings (CLAUDE.md, ADR 0011).
FORBIDDEN_KEYS: Final = frozenset({"onFailure", "statusMessage"})
SESSION: Final = "00000000-0000-4000-8000-000000000000"

REGISTERED: Final = (*REGISTERED_HTTP_EVENTS, *ASYNC_COMMAND_EVENTS)
NOT_REGISTERED_EVENTS: Final = tuple(event for event, _ in NOT_REGISTERED)


def _hooks(*, windows: bool = False) -> dict[str, list[dict[str, object]]]:
    settings = hook_settings(DEFAULT_HOOK_URL, windows=windows)
    assert list(settings) == ["hooks"]
    block = settings["hooks"]
    assert isinstance(block, dict)
    return block


def _entries(*, windows: bool = False) -> Iterator[tuple[str, dict[str, object]]]:
    for event, groups in _hooks(windows=windows).items():
        for group in groups:
            handlers = group["hooks"]
            assert isinstance(handlers, list)
            for handler in handlers:
                assert isinstance(handler, dict)
                yield event, handler


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _curl(*, windows: bool) -> dict[str, object]:
    [handler] = [handler for event, handler in _entries(windows=windows) if event == "SessionStart"]
    return handler


# Categories.


def test_the_documented_list_covers_the_parser() -> None:
    assert PARSED_EVENTS <= DOCUMENTED_EVENTS
    for name in PARSED_EVENTS:
        assert parse_hook({"session_id": SESSION, "hook_event_name": name}).__class__.__name__ == (
            name
        )


def test_every_documented_event_is_in_exactly_one_category() -> None:
    categories = (
        REGISTERED_HTTP_EVENTS,
        ASYNC_COMMAND_EVENTS,
        FORBIDDEN_EVENTS,
        NOT_REGISTERED_EVENTS,
    )
    every = [event for category in categories for event in category]

    assert sorted(every) == sorted(DOCUMENTED_EVENTS)
    assert len(every) == len(set(every))


def test_the_categories_are_the_decided_ones() -> None:
    assert REGISTERED_HTTP_EVENTS == (
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "Elicitation",
        "ElicitationResult",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "StopFailure",
        "PreCompact",
        "PostCompact",
        "SessionEnd",
    )
    assert ASYNC_COMMAND_EVENTS == ("SessionStart",)
    assert frozenset(FORBIDDEN_EVENTS) == CLAUDE_MD_FORBIDDEN


def test_every_unregistered_event_has_a_one_line_reason() -> None:
    for event, reason in NOT_REGISTERED:
        assert reason.strip(), event
        assert "\n" not in reason, event


def test_the_reasons_state_the_decisions() -> None:
    reasons = dict(NOT_REGISTERED)
    assert reasons["Setup"] == (
        "runs only with --init-only, or --init/--maintenance in -p mode, and accepts no http hook"
    )
    assert reasons["InstructionsLoaded"] == (
        "carries instruction-file paths and their scope; instruction loading is not "
        "tracked, and it fires many times per session"
    )
    assert reasons["PostToolBatch"] == (
        "not needed: tool outcomes, progress events and Stop already settle pending calls "
        "(maintainer decision for milestone 2)"
    )


def test_no_forbidden_event_is_registered() -> None:
    assert not CLAUDE_MD_FORBIDDEN & set(REGISTERED)
    assert not CLAUDE_MD_FORBIDDEN & set(_hooks(windows=False))
    assert not CLAUDE_MD_FORBIDDEN & set(_hooks(windows=True))


def test_only_registered_events_reach_the_settings() -> None:
    assert list(_hooks()) == list(REGISTERED)


def test_events_without_http_never_get_an_http_hook() -> None:
    assert not NO_HTTP & set(REGISTERED_HTTP_EVENTS)
    for windows in (False, True):
        for event, handler in _entries(windows=windows):
            if event in NO_HTTP:
                assert handler["type"] == "command"


@pytest.mark.parametrize("name", REGISTERED)
def test_every_registered_event_is_parsed_and_handled(name: str) -> None:
    cls = getattr(hooks, name)
    assert issubclass(cls, hooks.HookEvent)
    assert cls is not UnknownHookEvent

    parsed = parse_hook({"session_id": SESSION, "hook_event_name": name})
    assert type(parsed) is cls

    # The reducer has a rule for it (tests/test_domain_sessions.py checks each rule has
    # an effect of its own), and applying it works.
    assert name in sessions.HANDLED_EVENTS
    state, effects = apply(None, parsed, 1_800_000_000_000, 1)
    assert state.session.session_id == SESSION
    assert isinstance(effects[-1], SessionChanged)


def test_the_registered_events_are_handled_by_the_reducer() -> None:
    assert set(REGISTERED) <= sessions.HANDLED_EVENTS


# Handlers.


@pytest.mark.parametrize("windows", [False, True])
def test_every_handler_has_an_explicit_timeout(windows: bool) -> None:
    for event, handler in _entries(windows=windows):
        assert isinstance(handler.get("timeout"), int), event
        if handler["type"] == "http":
            assert handler["timeout"] == HTTP_TIMEOUT_S == 1
        else:
            assert handler["timeout"] == ASYNC_COMMAND_TIMEOUT_S


@pytest.mark.parametrize("windows", [False, True])
def test_http_handlers_have_the_exact_shape(windows: bool) -> None:
    for event, handler in _entries(windows=windows):
        if event in REGISTERED_HTTP_EVENTS:
            assert handler == {"type": "http", "url": DEFAULT_HOOK_URL, "timeout": 1}


@pytest.mark.parametrize("windows", [False, True])
def test_groups_have_one_handler_and_no_matcher(windows: bool) -> None:
    for groups in _hooks(windows=windows).values():
        assert len(groups) == 1
        assert list(groups[0]) == ["hooks"]
        assert isinstance(groups[0]["hooks"], list)
        assert len(groups[0]["hooks"]) == 1


@pytest.mark.parametrize("windows", [False, True])
def test_no_forbidden_key_anywhere(windows: bool) -> None:
    settings = hook_settings(DEFAULT_HOOK_URL, windows=windows)
    assert not FORBIDDEN_KEYS & set(_walk_keys(settings))
    for _, handler in _entries(windows=windows):
        if handler["type"] == "http":
            assert "async" not in handler


@pytest.mark.parametrize(("windows", "null"), [(False, "/dev/null"), (True, "NUL")])
def test_the_async_curl_posts_json_quietly_with_a_max_time(windows: bool, null: str) -> None:
    handler = _curl(windows=windows)

    assert handler == {
        "type": "command",
        "command": "curl",
        "args": [
            "-s",
            "-m",
            str(CURL_MAX_TIME_S),
            "-o",
            null,
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            DEFAULT_HOOK_URL,
        ],
        "async": True,
        "timeout": ASYNC_COMMAND_TIMEOUT_S,
    }
    args = handler["args"]
    assert isinstance(args, list)
    # Exec form: no shell, so no redirect and nothing to quote.
    assert not any(">" in arg or "|" in arg for arg in args)
    assert CURL_MAX_TIME_S < ASYNC_COMMAND_TIMEOUT_S


def test_the_timeouts_follow_adr_0011() -> None:
    assert (HTTP_TIMEOUT_S, CURL_MAX_TIME_S, ASYNC_COMMAND_TIMEOUT_S) == (1, 2, 5)


# URL.


def test_the_default_url_is_the_app_port() -> None:
    assert HOOK_PATH == "/hooks"
    assert DEFAULT_HOOK_URL == "http://127.0.0.1:4300/hooks"


ACCEPTED_URLS: Final = (
    "http://127.0.0.1:4300/hooks",
    "http://127.0.0.1/hooks",
    "http://127.0.0.1:1/hooks",
    "http://127.0.0.1:65535/hooks",
    "http://127.0.0.1:24300/ccobs/hooks",
    "http://127.0.0.1:24300/a/b_c-d.e~/hooks",
)
REJECTED_URLS: Final = (
    "",
    "127.0.0.1:4300/hooks",
    "https://127.0.0.1:4300/hooks",
    "ftp://127.0.0.1:4300/hooks",
    "http://example.com:4300/hooks",
    "http://192.168.0.10:4300/hooks",
    "http://0.0.0.0:4300/hooks",
    "http://127.0.0.2:4300/hooks",
    "http://::1:4300/hooks",
    # ADR 0018 publishes ports on 127.0.0.1 only: localhost may resolve to ::1, and
    # [::1] never connects (and risks curl's URL globbing).
    "http://localhost:4300/hooks",
    "http://localhost:24300/ccobs/hooks",
    "http://[::1]:4300/hooks",
    "http://127.0.0.1.example.com/hooks",
    "http://127.0.0.1@example.com/hooks",
    "http://user@127.0.0.1:4300/hooks",
    "http://LOCALHOST:4300/hooks",
    "http://127.0.0.1:4300",
    "http://127.0.0.1:4300/",
    "http://127.0.0.1:4300/hook",
    "http://127.0.0.1:4300/hooks/",
    "http://127.0.0.1:4300/myhooks",
    "http://127.0.0.1:4300/hooks?x=1",
    "http://127.0.0.1:4300/hooks#x",
    "http://127.0.0.1:4300/../hooks",
    "http://127.0.0.1:4300/./hooks",
    "http://127.0.0.1:4300//hooks",
    "http://127.0.0.1:0/hooks",
    "http://127.0.0.1:65536/hooks",
    "http://127.0.0.1:04300/hooks",
    "http://127.0.0.1:/hooks",
    "http://127.0.0.1:4300/ho oks/hooks",
    " http://127.0.0.1:4300/hooks",
    "http://127.0.0.1:4300/hooks\n",
)


@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_accepted_urls(url: str) -> None:
    settings = hook_settings(url, windows=False)
    assert url in json.dumps(settings)
    assert is_hook_url(url)


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_rejected_urls(url: str) -> None:
    with pytest.raises(ValueError, match="hook URL"):
        hook_settings(url, windows=False)
    assert not is_hook_url(url)


@pytest.mark.parametrize("value", [None, 4300, b"http://127.0.0.1:4300/hooks", [DEFAULT_HOOK_URL]])
def test_is_hook_url_is_false_for_a_non_string(value: object) -> None:
    assert is_hook_url(value) is False


def test_a_rejected_url_is_not_echoed() -> None:
    with pytest.raises(ValueError, match="hook URL") as caught:
        hook_settings("http://secret.example.com/hooks", windows=True)
    assert "secret" not in str(caught.value)


# Output.


@pytest.mark.parametrize("windows", [False, True])
def test_the_output_is_json_and_deterministic(windows: bool) -> None:
    first = hook_settings(DEFAULT_HOOK_URL, windows=windows)
    second = hook_settings(DEFAULT_HOOK_URL, windows=windows)

    assert json.loads(json.dumps(first)) == first
    assert json.dumps(first) == json.dumps(second)
    assert first is not second


def test_calls_do_not_share_mutable_state() -> None:
    first = _hooks()
    first["Stop"][0]["hooks"] = []
    assert _hooks()["Stop"][0]["hooks"]


def test_the_module_documents_its_rule() -> None:
    assert "no I/O" in str(hook_config.__doc__)
