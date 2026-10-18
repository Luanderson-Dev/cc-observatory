"""Unit tests of ``ccobs.domain.settings_merge``: setup's entries in the user settings.

``merge`` adds cc-observatory's hook handlers (ADR 0025) and telemetry env block (ADR
0036) to Claude Code's user ``settings.json``, and ``unmerge`` takes them out again,
both by recognizing our entries plus a state of what setup wrote (ADR 0037). The user's
own entries, values and key order are never touched, a round trip restores the file,
and neither ever creates ``allowedHttpHookUrls`` or ``disableAllHooks``.

No fixture of a real ``settings.json`` exists in ``tests/fixtures/`` (it would be the
user's file); the user files here follow Claude Code's settings reference.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from typing import Final, cast

import pytest

from ccobs.domain import settings_merge
from ccobs.domain.hook_config import (
    ASYNC_COMMAND_EVENTS,
    DEFAULT_HOOK_URL,
    REGISTERED_HTTP_EVENTS,
    hook_settings,
)
from ccobs.domain.settings_merge import (
    STATE_VERSION,
    MergeResult,
    SettingsShapeError,
    SetupState,
    is_ccobs_handler,
    merge,
    state_from_json,
    state_to_json,
    unmerge,
)
from ccobs.domain.telemetry_settings import (
    DEFAULT_OTLP_ENDPOINT,
    TELEMETRY_VARIABLES,
    TRACES_VARIABLES,
    telemetry_env,
)

OTHER_HOOK_URL: Final = "http://127.0.0.1:5300/hooks"
OTHER_ENDPOINT: Final = "http://127.0.0.1:5318"
EVENTS: Final = (*REGISTERED_HTTP_EVENTS, *ASYNC_COMMAND_EVENTS)
# A value from the user's file, which messages must never echo.
FILE_VALUE: Final = "value-from-the-users-file"

HTTP: Final[dict[str, object]] = {"type": "http", "url": DEFAULT_HOOK_URL, "timeout": 1}
CURL_ARGS: Final = ("-s", "-m", "2", "-o", "NUL", "--data-binary", "@-", DEFAULT_HOOK_URL)
CURL: Final[dict[str, object]] = {
    "type": "command",
    "command": "curl",
    "args": list(CURL_ARGS),
    "async": True,
    "timeout": 5,
}


def _hooks(url: str = DEFAULT_HOOK_URL, *, windows: bool = False) -> dict[str, list[object]]:
    block = hook_settings(url, windows=windows)["hooks"]
    assert isinstance(block, dict)
    return block


def _desired(url: str = DEFAULT_HOOK_URL) -> dict[str, list[dict[str, object]]]:
    return {
        event: [cast(dict[str, object], group) for group in groups]
        for event, groups in _hooks(url).items()
    }


def _merge(
    settings: Mapping[str, object],
    *,
    url: str = DEFAULT_HOOK_URL,
    endpoint: str = DEFAULT_OTLP_ENDPOINT,
    traces: bool = False,
    state: SetupState | None = None,
    force: bool = False,
) -> MergeResult:
    return merge(
        settings,
        hooks=_desired(url),
        env=telemetry_env(endpoint, traces=traces),
        state=state,
        force=force,
    )


def _ordered_keys(value: object) -> object:
    """The value with every object turned into its list of (key, value) pairs, so that
    a comparison also checks the order in which the keys are written."""
    if isinstance(value, dict):
        return [(key, _ordered_keys(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [_ordered_keys(item) for item in value]
    return value


def _handlers(settings: Mapping[str, object], event: str) -> list[object]:
    block = settings["hooks"]
    assert isinstance(block, dict)
    groups = block[event]
    assert isinstance(groups, list)
    found: list[object] = []
    for group in groups:
        assert isinstance(group, dict)
        found.extend(group["hooks"])
    return found


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _user_settings() -> dict[str, object]:
    """A realistic user file: settings Claude Code writes itself, the user's own hooks
    and env, and keys this version does not know."""
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "model": "opus",
        "permissions": {
            "allow": ["Bash(git status)", "Read(./src/**)"],
            "deny": ["Read(./.env)"],
            "defaultMode": "acceptEdits",
        },
        "env": {
            "ANTHROPIC_MODEL": "claude-opus",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku",
            "BASH_DEFAULT_TIMEOUT_MS": "60000",
        },
        "statusLine": {"type": "command", "command": "~/.claude/statusline.sh"},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "~/.claude/guard.sh", "timeout": 10}],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        },
        "futureSetting": {"nested": [1, 2, {"a": True}]},
        "includeCoAuthoredBy": False,
    }


# Recognition.

FOREIGN_HANDLERS: Final[tuple[object, ...]] = (
    {"type": "http", "url": "http://example.com/hooks", "timeout": 1},
    {"type": "http", "url": DEFAULT_HOOK_URL, "timeout": 1, "headers": {"X-Token": "t"}},
    {"type": "http", "url": "http://localhost:4300/hooks", "timeout": 1},
    {"type": "http", "url": "http://127.0.0.1:4300/other", "timeout": 1},
    {"type": "http", "timeout": 1},
    {"type": "command", "url": DEFAULT_HOOK_URL},
    {"type": "command", "command": f"curl -s --data-binary @- {DEFAULT_HOOK_URL}"},
    {**CURL, "args": [*CURL_ARGS[:-1], "http://127.0.0.1:4300/elsewhere"]},
    {**CURL, "args": [*CURL_ARGS[:-1], "http://example.com/hooks"]},
    {**CURL, "async": False},
    {**CURL, "async": 1},
    {key: value for key, value in CURL.items() if key != "async"},
    {**CURL, "command": "wget"},
    {**CURL, "type": "http"},
    {**CURL, "args": "--data-binary @- " + DEFAULT_HOOK_URL},
    {**CURL, "args": ["--data-binary", "@-", DEFAULT_HOOK_URL, 1]},
    {**CURL, "args": [*CURL_ARGS[:5], "--data-binary", "@body.json", DEFAULT_HOOK_URL]},
    {**CURL, "args": ["-s", "--data-binary"]},
    {**CURL, "args": ["@-", "--data-binary", DEFAULT_HOOK_URL]},
    {**CURL, "args": []},
    {**CURL, "statusMessage": "posting"},
    {**CURL, "shell": "bash"},
    "http",
    None,
    [HTTP],
)
OWN_HANDLERS: Final[tuple[dict[str, object], ...]] = (
    HTTP,
    {"type": "http", "url": DEFAULT_HOOK_URL},
    {"type": "http", "url": DEFAULT_HOOK_URL, "timeout": 5},
    {"type": "http", "url": "http://127.0.0.1:24300/ccobs/hooks", "timeout": 1},
    CURL,
    {key: value for key, value in CURL.items() if key != "timeout"},
    {**CURL, "args": ["--data-binary", "@-", OTHER_HOOK_URL]},
)


@pytest.mark.parametrize("handler", OWN_HANDLERS)
def test_our_handlers_are_recognized(handler: dict[str, object]) -> None:
    assert is_ccobs_handler(handler) is True


@pytest.mark.parametrize("handler", FOREIGN_HANDLERS)
def test_other_handlers_are_not_ours(handler: object) -> None:
    assert is_ccobs_handler(handler) is False


@pytest.mark.parametrize("windows", [False, True])
def test_every_handler_hook_settings_writes_is_recognized(windows: bool) -> None:
    for groups in _hooks(windows=windows).values():
        for group in groups:
            assert isinstance(group, dict)
            for handler in group["hooks"]:
                assert is_ccobs_handler(handler)


# Merge and unmerge.


def test_an_empty_file_gets_the_hooks_and_the_env_and_unmerge_empties_it() -> None:
    result = _merge({})

    assert result.settings == {"hooks": _hooks(), "env": telemetry_env()}
    assert result.changed is True
    assert result.conflicts == ()
    assert result.state == SetupState(
        env_written=telemetry_env(),
        env_previous={},
        created_env=True,
        created_hooks=True,
        created_events=EVENTS,
    )

    undone = unmerge(result.settings, state=result.state)

    assert undone.settings == {}
    assert undone.removed_handlers == len(EVENTS)
    assert undone.kept_env == ()
    assert undone.changed is True


def test_a_round_trip_restores_the_user_file_and_its_key_order() -> None:
    original = _user_settings()

    merged = _merge(original, traces=True)
    undone = unmerge(merged.settings, state=merged.state)

    assert undone.settings == original
    assert _ordered_keys(undone.settings) == _ordered_keys(original)
    assert undone.kept_env == ()
    assert undone.unowned_env == ()


def test_merge_appends_after_the_users_entries_and_keeps_their_order() -> None:
    original = _user_settings()
    result = _merge(original)
    settings = result.settings

    assert list(settings) == list(original)
    hooks = settings["hooks"]
    assert isinstance(hooks, dict)
    assert list(hooks) == [
        "PreToolUse",
        "Stop",
        *(e for e in EVENTS if e not in {"PreToolUse", "Stop"}),
    ]
    pre_tool_use = hooks["PreToolUse"]
    assert isinstance(pre_tool_use, list)
    original_hooks = original["hooks"]
    assert isinstance(original_hooks, dict)
    assert pre_tool_use == [*original_hooks["PreToolUse"], {"hooks": [HTTP]}]

    env = settings["env"]
    original_env = original["env"]
    assert isinstance(env, dict)
    assert isinstance(original_env, dict)
    assert list(env) == [*original_env, *TELEMETRY_VARIABLES]

    # The events that existed with the user's entries were not created by setup.
    assert result.state.created_events == tuple(
        e for e in EVENTS if e not in {"PreToolUse", "Stop"}
    )
    assert result.state.created_hooks is False
    assert result.state.created_env is False


@pytest.mark.parametrize("traces", [False, True])
def test_merge_is_idempotent(traces: bool) -> None:
    first = _merge(_user_settings(), traces=traces)
    second = _merge(first.settings, traces=traces, state=first.state)

    assert second.changed is False
    assert _ordered_keys(second.settings) == _ordered_keys(first.settings)
    assert second.state == first.state
    assert second.conflicts == ()


def test_a_port_change_replaces_the_handlers_and_the_endpoint() -> None:
    first = _merge(_user_settings())
    second = _merge(first.settings, url=OTHER_HOOK_URL, endpoint=OTHER_ENDPOINT, state=first.state)

    assert second.conflicts == ()
    assert second.changed is True
    for event in EVENTS:
        handlers = [h for h in _handlers(second.settings, event) if is_ccobs_handler(h)]
        assert len(handlers) == 1, event
        assert OTHER_HOOK_URL in json.dumps(handlers)
        assert DEFAULT_HOOK_URL not in json.dumps(handlers)
    env = second.settings["env"]
    assert isinstance(env, dict)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == OTHER_ENDPOINT
    assert second.state.env_written == telemetry_env(OTHER_ENDPOINT)

    # The port change is a fixed point too, and unmerge still restores the user file.
    third = _merge(second.settings, url=OTHER_HOOK_URL, endpoint=OTHER_ENDPOINT, state=second.state)
    assert third.changed is False
    assert unmerge(third.settings, state=third.state).settings == _user_settings()


def test_an_older_handler_shape_is_replaced_not_duplicated() -> None:
    old = {"type": "http", "url": DEFAULT_HOOK_URL, "timeout": 5}
    mine = {"hooks": [{"type": "command", "command": "mine.sh"}, old]}
    settings: dict[str, object] = {"hooks": {"Stop": [{"hooks": [old]}, mine]}}

    result = _merge(settings, state=None)

    assert _handlers(result.settings, "Stop") == [{"type": "command", "command": "mine.sh"}, HTTP]
    # The event existed with the user's entries: setup did not create it.
    assert "Stop" not in result.state.created_events


def test_a_hand_pasted_block_is_taken_over_and_removed() -> None:
    # docs/app.md's snippet, pasted by hand before setup existed.
    settings: dict[str, object] = {"hooks": _hooks()}

    result = _merge(settings)

    assert result.settings["hooks"] == _hooks()
    assert result.state.created_events == EVENTS
    assert result.state.created_hooks is False
    assert unmerge(result.settings, state=result.state).settings == {"hooks": {}}


def test_foreign_handlers_are_never_touched() -> None:
    foreign = [copy.deepcopy(handler) for handler in FOREIGN_HANDLERS]
    settings: dict[str, object] = {
        "hooks": {"Stop": [{"hooks": foreign}], "SessionStart": [{"hooks": [foreign[6]]}]}
    }

    merged = _merge(settings)
    assert _handlers(merged.settings, "Stop") == [*FOREIGN_HANDLERS, HTTP]
    undone = unmerge(merged.settings, state=merged.state)
    assert undone.settings == settings
    without_state = unmerge(merged.settings, state=None)
    assert without_state.settings["hooks"] == settings["hooks"]
    assert without_state.removed_handlers == len(EVENTS)


def test_events_that_are_no_longer_registered_lose_our_handlers() -> None:
    # An older version registered an event this one does not: its group goes away, and
    # so does the key when nothing of the user's is left in it.
    settings: dict[str, object] = {
        "hooks": {
            "TaskCreated": [{"hooks": [HTTP]}],
            "TeammateIdle": [{"hooks": [HTTP]}, {"matcher": "x", "hooks": [{"type": "mine"}]}],
        }
    }

    result = _merge(settings)
    hooks = result.settings["hooks"]

    assert isinstance(hooks, dict)
    assert "TaskCreated" not in hooks
    assert hooks["TeammateIdle"] == [{"matcher": "x", "hooks": [{"type": "mine"}]}]
    assert list(hooks) == ["TeammateIdle", *EVENTS]


def test_empty_containers_of_the_user_are_kept() -> None:
    settings: dict[str, object] = {
        "hooks": {"Stop": [], "Notification": [{"matcher": "", "hooks": []}]},
        "env": {},
    }

    merged = _merge(settings)
    assert "Stop" not in merged.state.created_events
    assert "Notification" not in merged.state.created_events
    assert merged.state.created_env is False
    undone = unmerge(merged.settings, state=merged.state)

    assert undone.settings == settings
    assert _ordered_keys(undone.settings) == _ordered_keys(settings)


def test_nothing_desired_changes_nothing() -> None:
    result = merge({}, hooks={}, env={}, state=None)

    assert result.settings == {}
    assert result.changed is False
    assert result.state == SetupState({}, {}, False, False, ())


# Env.


def test_an_env_conflict_stops_without_force() -> None:
    settings: dict[str, object] = {
        "env": {"OTEL_METRICS_EXPORTER": "prometheus", "OTEL_EXPORTER_OTLP_ENDPOINT": FILE_VALUE}
    }

    result = _merge(settings)

    assert result.changed is False
    assert result.settings == settings
    assert result.conflicts == ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_METRICS_EXPORTER")
    assert result.state == SetupState({}, {}, False, False, ())


def test_a_conflict_returns_the_given_state() -> None:
    first = _merge({})
    env = first.settings["env"]
    assert isinstance(env, dict)
    settings = {**first.settings, "env": {**env, "OTEL_TRACES_EXPORTER": "zipkin"}}

    result = _merge(settings, traces=True, state=first.state)

    assert result.conflicts == ("OTEL_TRACES_EXPORTER",)
    assert result.state is first.state
    assert result.settings == settings


def test_force_overwrites_records_and_unmerge_restores() -> None:
    settings: dict[str, object] = {"env": {"A": "1", "OTEL_METRICS_EXPORTER": "prometheus"}}

    merged = _merge(settings, force=True)

    assert merged.conflicts == ()
    env = merged.settings["env"]
    assert isinstance(env, dict)
    assert list(env)[:2] == ["A", "OTEL_METRICS_EXPORTER"]
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert merged.state.env_previous == {"OTEL_METRICS_EXPORTER": "prometheus"}
    assert merged.state.env_written == telemetry_env()

    # A re-run keeps the recorded value, and a port change too.
    again = _merge(merged.settings, endpoint=OTHER_ENDPOINT, state=merged.state)
    assert again.state.env_previous == {"OTEL_METRICS_EXPORTER": "prometheus"}

    undone = unmerge(again.settings, state=again.state)
    assert undone.settings == settings
    assert _ordered_keys(undone.settings) == _ordered_keys(settings)


def test_force_refuses_a_non_string_value_without_echoing_it() -> None:
    settings: dict[str, object] = {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": 12345}}

    assert _merge(settings).conflicts == ("CLAUDE_CODE_ENABLE_TELEMETRY",)
    with pytest.raises(SettingsShapeError, match="CLAUDE_CODE_ENABLE_TELEMETRY") as caught:
        _merge(settings, force=True)
    assert "12345" not in str(caught.value)


def test_hand_pasted_env_is_not_owned_and_unmerge_keeps_it() -> None:
    settings: dict[str, object] = {"env": telemetry_env()}

    merged = _merge(settings, traces=True)

    assert merged.state.env_written == dict(
        telemetry_env(traces=True).items() - telemetry_env().items()
    )
    undone = unmerge(merged.settings, state=merged.state)
    assert undone.settings == {"env": telemetry_env()}
    assert undone.kept_env == ()
    assert undone.unowned_env == TELEMETRY_VARIABLES


def test_a_value_the_user_changed_after_setup_is_kept_and_reported() -> None:
    merged = _merge({"env": {"A": "1"}})
    env = merged.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_LOGS_EXPORTER"] = "none"
    del env["OTEL_METRICS_EXPORTER"]

    undone = unmerge(merged.settings, state=merged.state)

    assert undone.settings["env"] == {"A": "1", "OTEL_LOGS_EXPORTER": "none"}
    assert undone.kept_env == ("OTEL_LOGS_EXPORTER",)
    assert undone.unowned_env == ()


def test_turning_traces_off_removes_the_traces_variables() -> None:
    on = _merge(_user_settings(), traces=True)
    off = _merge(on.settings, traces=False, state=on.state)

    env = off.settings["env"]
    assert isinstance(env, dict)
    assert not set(TRACES_VARIABLES) & set(env)
    assert off.state.env_written == telemetry_env()
    assert off.settings == _merge(_user_settings()).settings


def test_turning_traces_off_restores_a_forced_value() -> None:
    settings: dict[str, object] = {"env": {"OTEL_TRACES_EXPORTER": "zipkin"}}
    on = _merge(settings, traces=True, force=True)
    off = _merge(on.settings, traces=False, state=on.state)

    env = off.settings["env"]
    assert isinstance(env, dict)
    assert env["OTEL_TRACES_EXPORTER"] == "zipkin"
    assert "OTEL_TRACES_EXPORTER" not in off.state.env_written
    assert off.state.env_previous == {}


def test_turning_traces_off_leaves_a_value_the_user_changed() -> None:
    on = _merge({}, traces=True)
    env = on.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_TRACES_EXPORTER"] = "console"
    del env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"]

    off = _merge(on.settings, traces=False, state=on.state)

    env = off.settings["env"]
    assert isinstance(env, dict)
    assert env["OTEL_TRACES_EXPORTER"] == "console"
    assert "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA" not in env
    assert off.state.env_written == telemetry_env()
    # No longer owned: uninstall keeps it and does not report it.
    assert unmerge(off.settings, state=off.state).settings == {
        "env": {"OTEL_TRACES_EXPORTER": "console"}
    }


def test_a_deleted_owned_key_is_written_again_and_its_record_dropped() -> None:
    forced = _merge({"env": {"OTEL_LOGS_EXPORTER": "none"}}, force=True)
    env = forced.settings["env"]
    assert isinstance(env, dict)
    del env["OTEL_LOGS_EXPORTER"]

    again = _merge(forced.settings, state=forced.state)

    assert again.state.env_previous == {}
    assert again.state.env_written == telemetry_env()


def test_an_owned_value_the_user_changed_is_no_longer_ours_and_conflicts() -> None:
    first = _merge({})
    env = first.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_LOGS_EXPORTER"] = "none"

    again = _merge(first.settings, state=first.state)

    assert again.conflicts == ("OTEL_LOGS_EXPORTER",)
    assert again.changed is False
    assert again.settings == first.settings


def test_a_port_change_conflicts_on_an_owned_value_the_user_changed() -> None:
    first = _merge({})
    env = first.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = FILE_VALUE

    again = _merge(first.settings, endpoint=OTHER_ENDPOINT, state=first.state)

    assert again.conflicts == ("OTEL_EXPORTER_OTLP_ENDPOINT",)
    assert again.settings == first.settings


def test_force_on_a_user_changed_owned_value_records_the_users_value() -> None:
    forced = _merge({"env": {"OTEL_LOGS_EXPORTER": "console"}}, force=True)
    env = forced.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_LOGS_EXPORTER"] = "none"

    again = _merge(forced.settings, state=forced.state, force=True)

    again_env = again.settings["env"]
    assert isinstance(again_env, dict)
    assert again_env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert again.state.env_written == telemetry_env()
    # The user's current value, not the one the first --force replaced.
    assert again.state.env_previous == {"OTEL_LOGS_EXPORTER": "none"}
    undone = unmerge(again.settings, state=again.state)
    assert undone.settings == {"env": {"OTEL_LOGS_EXPORTER": "none"}}


def test_an_owned_value_the_user_set_to_the_desired_one_is_no_longer_ours() -> None:
    first = _merge({})
    env = first.settings["env"]
    assert isinstance(env, dict)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = OTHER_ENDPOINT

    again = _merge(first.settings, endpoint=OTHER_ENDPOINT, state=first.state)

    assert again.conflicts == ()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in again.state.env_written
    undone = unmerge(again.settings, state=again.state)
    assert undone.settings == {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": OTHER_ENDPOINT}}
    assert undone.kept_env == ()
    assert undone.unowned_env == ("OTEL_EXPORTER_OTLP_ENDPOINT",)


def test_a_lost_state_and_a_re_run_reports_the_whole_block_as_unowned() -> None:
    first = _merge(_user_settings(), traces=True)

    again = _merge(first.settings, traces=True, state=None)
    undone = unmerge(again.settings, state=again.state)

    assert again.state.env_written == {}
    assert undone.unowned_env == (*TELEMETRY_VARIABLES, *TRACES_VARIABLES)
    assert undone.kept_env == ()
    assert undone.settings["hooks"] == _user_settings()["hooks"]


# Created event keys.


def test_a_created_event_stays_ours_across_runs() -> None:
    first = _merge({})
    assert "Stop" in first.state.created_events
    mine: dict[str, object] = {"hooks": [{"type": "command", "command": "mine.sh"}]}
    hooks = first.settings["hooks"]
    assert isinstance(hooks, dict)
    hooks["Stop"].append(mine)

    again = _merge(first.settings, state=first.state)
    assert "Stop" in again.state.created_events
    hooks = again.settings["hooks"]
    assert isinstance(hooks, dict)
    hooks["Stop"].remove(mine)

    undone = unmerge(again.settings, state=again.state)

    assert undone.settings == {}


def test_created_events_keep_only_registered_events() -> None:
    first = _merge({})
    state = SetupState(
        env_written=first.state.env_written,
        env_previous={},
        created_env=True,
        created_hooks=True,
        created_events=("TaskCreated", *first.state.created_events),
    )

    again = _merge(first.settings, state=state)

    assert again.state.created_events == EVENTS


# Unmerge without a state.


def test_unmerge_without_a_state_removes_only_our_handlers() -> None:
    merged = _merge(_user_settings(), traces=True)

    undone = unmerge(merged.settings, state=None)

    original = _user_settings()
    env = undone.settings["env"]
    assert isinstance(env, dict)
    assert env == {**cast(dict[str, str], original["env"]), **telemetry_env(traces=True)}
    assert undone.settings["hooks"] == original["hooks"]
    assert undone.unowned_env == (*TELEMETRY_VARIABLES, *TRACES_VARIABLES)
    assert undone.kept_env == ()
    assert undone.removed_handlers == len(EVENTS)


def test_unmerge_without_a_state_keeps_an_emptied_hooks_object() -> None:
    undone = unmerge({"hooks": _hooks(), "model": "opus"}, state=None)

    assert undone.settings == {"hooks": {}, "model": "opus"}
    assert undone.kept_env == ()


def test_unmerge_of_a_file_without_our_entries_changes_nothing() -> None:
    original = _user_settings()

    undone = unmerge(original, state=_merge({}).state)

    assert undone.changed is False
    assert undone.removed_handlers == 0
    assert undone.settings == original


def test_unmerge_with_a_state_and_no_env_object() -> None:
    merged = _merge({})
    settings = {key: value for key, value in merged.settings.items() if key != "env"}

    undone = unmerge(settings, state=merged.state)

    assert undone.settings == {}
    assert undone.kept_env == ()


def test_unmerge_with_a_state_and_no_hooks_object() -> None:
    merged = _merge({"model": "opus"})
    settings = {key: value for key, value in merged.settings.items() if key != "hooks"}

    undone = unmerge(settings, state=merged.state)

    assert undone.settings == {"model": "opus"}
    assert undone.removed_handlers == 0


# Shape errors.

BAD_SHAPES: Final[tuple[object, ...]] = (
    [FILE_VALUE],
    FILE_VALUE,
    {"hooks": [FILE_VALUE]},
    {"hooks": {"Stop": FILE_VALUE}},
    {"hooks": {"Stop": [FILE_VALUE]}},
    {"hooks": {"Stop": [{"matcher": FILE_VALUE}]}},
    {"hooks": {"Stop": [{"hooks": FILE_VALUE}]}},
    {"env": [FILE_VALUE]},
    {"env": FILE_VALUE},
)


@pytest.mark.parametrize("settings", BAD_SHAPES)
def test_a_bad_shape_raises_without_echoing_the_file(settings: object) -> None:
    for call in (
        lambda: _merge(cast(Mapping[str, object], settings)),
        lambda: unmerge(cast(Mapping[str, object], settings), state=None),
    ):
        with pytest.raises(SettingsShapeError) as caught:
            call()
        assert FILE_VALUE not in str(caught.value)
        assert isinstance(caught.value, ValueError)


# Purity and invariants.


def test_inputs_are_never_mutated() -> None:
    settings = _merge(_user_settings(), traces=True, force=True).settings
    env = settings["env"]
    assert isinstance(env, dict)
    env["OTEL_LOGS_EXPORTER"] = "none"
    hooks = _desired(OTHER_HOOK_URL)
    desired_env = telemetry_env(OTHER_ENDPOINT)
    state = _merge(_user_settings(), traces=True).state
    before = copy.deepcopy((settings, hooks, desired_env, state))

    merged = merge(settings, hooks=hooks, env=desired_env, state=state, force=True)
    unmerge(settings, state=state)
    unmerge(settings, state=None)

    assert (settings, hooks, desired_env, state) == before
    # The output shares no object with the inputs.
    merged_hooks = merged.settings["hooks"]
    assert isinstance(merged_hooks, dict)
    merged_hooks["Stop"][-1]["hooks"].clear()
    assert hooks == before[1]


@pytest.mark.parametrize("traces", [False, True])
def test_never_creates_the_allowlist_or_the_kill_switch(traces: bool) -> None:
    for start in ({}, _user_settings()):
        merged = _merge(start, traces=traces)
        keys = set(_walk_keys(merged.settings))
        assert "allowedHttpHookUrls" not in keys
        assert "disableAllHooks" not in keys
        assert not {"onFailure", "statusMessage"} & keys


def test_existing_allowlist_and_kill_switch_are_left_alone() -> None:
    settings: dict[str, object] = {
        "allowedHttpHookUrls": ["http://example.com/*"],
        "disableAllHooks": False,
        "model": "opus",
    }

    merged = _merge(settings)

    assert list(merged.settings) == [*settings, "hooks", "env"]
    assert merged.settings["allowedHttpHookUrls"] == ["http://example.com/*"]
    assert merged.settings["disableAllHooks"] is False
    assert unmerge(merged.settings, state=merged.state).settings == settings


# State.


def test_state_round_trips_through_json() -> None:
    state = _merge({"env": {"OTEL_LOGS_EXPORTER": "none"}}, traces=True, force=True).state

    data = state_to_json(state)

    assert data["version"] == STATE_VERSION == 1
    assert json.loads(json.dumps(data)) == data
    assert state_from_json(json.loads(json.dumps(data))) == state
    assert list(data) == [
        "version",
        "env_written",
        "env_previous",
        "created_env",
        "created_hooks",
        "created_events",
    ]


def _state_json(**changes: object) -> dict[str, object]:
    data = state_to_json(
        SetupState(
            env_written={"OTEL_LOGS_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp"},
            env_previous={"OTEL_LOGS_EXPORTER": "none"},
            created_env=True,
            created_hooks=False,
            created_events=("Stop",),
        )
    )
    data.update(changes)
    return data


MALFORMED_STATES: Final[tuple[object, ...]] = (
    None,
    [],
    "state",
    {},
    {key: value for key, value in _state_json().items() if key != "created_events"},
    _state_json(extra=1),
    _state_json(version=2),
    _state_json(version="1"),
    _state_json(version=True),
    _state_json(env_written=[]),
    _state_json(env_written={"OTEL_LOGS_EXPORTER": 1}),
    _state_json(env_written={"OTEL_LOGS_EXPORTER": None}),
    _state_json(env_previous={"OTEL_LOGS_EXPORTER": 0}),
    _state_json(env_previous="OTEL_LOGS_EXPORTER"),
    _state_json(env_previous={"OTEL_TRACES_EXPORTER": "none"}),
    _state_json(created_env=1),
    _state_json(created_env="true"),
    _state_json(created_hooks=None),
    _state_json(created_events="Stop"),
    _state_json(created_events=["Stop", 1]),
    _state_json(created_events=["Stop", "Stop"]),
)


def test_the_state_fixture_is_valid() -> None:
    assert state_from_json(_state_json()).env_previous == {"OTEL_LOGS_EXPORTER": "none"}


@pytest.mark.parametrize("data", MALFORMED_STATES)
def test_a_malformed_state_raises_value_error(data: object) -> None:
    with pytest.raises(ValueError, match="state"):
        state_from_json(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"env_written": {"ANTHROPIC_MODEL": "claude-opus"}, "env_previous": {}},
        {
            "env_written": {"ANTHROPIC_MODEL": "claude-opus"},
            "env_previous": {"ANTHROPIC_MODEL": "claude-haiku"},
        },
        {"env_previous": {"ANTHROPIC_MODEL": "claude-haiku"}},
    ],
)
def test_a_state_with_a_variable_setup_does_not_manage_is_refused(
    changes: dict[str, object],
) -> None:
    # A corrupt state must not make unmerge delete or rewrite the user's own variables.
    with pytest.raises(ValueError, match="not manage") as caught:
        state_from_json(_state_json(**changes))
    assert "claude-" not in str(caught.value)


def test_the_module_documents_its_rule() -> None:
    assert "no I/O" in str(settings_merge.__doc__)
