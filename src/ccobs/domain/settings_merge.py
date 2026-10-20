"""Adding cc-observatory's entries to Claude Code's user settings, and taking them out.

``ccobs setup`` writes two things into the user ``settings.json`` (ADR 0002): the hook
handlers of ``hook_config.hook_settings`` (ADR 0025) and the ``env`` block of
``telemetry_settings.telemetry_env`` (ADR 0036). The file is the user's, and Claude Code
writes it too, so nothing marks our entries (ADR 0037):

- a handler is ours when ``is_ccobs_handler`` recognizes it: an http hook to
  ``127.0.0.1/.../hooks`` with only type, url and timeout, or the exec-form async curl
  that posts stdin to such a URL. Anything else is the user's and is never touched;
- ``SetupState`` records the env values setup wrote, the user values ``--force``
  replaced and which containers setup created, so ``unmerge`` restores the file.

``merge`` and ``unmerge`` take the parsed JSON object of the file and return new
objects: they never mutate their inputs, keep the order of existing keys and append new
ones, never "repair" a file of the wrong shape (``SettingsShapeError``) and never create
``allowedHttpHookUrls`` or ``disableAllHooks``.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from ccobs.domain.hook_config import is_hook_url
from ccobs.domain.telemetry_settings import TELEMETRY_VARIABLES, TRACES_VARIABLES

__all__ = [
    "STATE_VERSION",
    "MergeResult",
    "SettingsShapeError",
    "SetupState",
    "UnmergeResult",
    "is_ccobs_handler",
    "merge",
    "state_from_json",
    "state_to_json",
    "unmerge",
]

STATE_VERSION: Final = 1

_HTTP_KEYS: Final = frozenset({"type", "url", "timeout"})
_CURL_KEYS: Final = frozenset({"type", "command", "args", "async", "timeout"})
_STATE_KEYS: Final = (
    "version",
    "env_written",
    "env_previous",
    "created_env",
    "created_hooks",
    "created_events",
)
_MANAGED_VARIABLES: Final = (*TELEMETRY_VARIABLES, *TRACES_VARIABLES)

_Groups = list[dict[str, object]]
_HooksBlock = dict[str, _Groups]


class SettingsShapeError(ValueError):
    """The settings are not shaped as Claude Code documents them. The message never
    holds a value from the file: it can hold API keys."""


@dataclass(frozen=True)
class SetupState:
    """What a run of setup wrote, so that the next run and uninstall know what is ours."""

    env_written: dict[str, str]  # key -> the value setup wrote and owns
    env_previous: dict[str, str]  # key -> the user's value that --force overwrote
    created_env: bool  # setup created the "env" object
    created_hooks: bool  # setup created the "hooks" object
    created_events: tuple[str, ...]  # hook event keys setup created


@dataclass(frozen=True)
class MergeResult:
    settings: dict[str, object]
    state: SetupState
    conflicts: tuple[str, ...]  # env keys whose user value differs, sorted
    changed: bool


@dataclass(frozen=True)
class UnmergeResult:
    settings: dict[str, object]
    removed_handlers: int
    kept_env: tuple[str, ...]  # owned keys the user changed, left for them to remove
    unowned_env: tuple[str, ...]  # telemetry variables left that the state does not own
    changed: bool


def _empty_state() -> SetupState:
    return SetupState({}, {}, created_env=False, created_hooks=False, created_events=())


# Recognition.


def _posts_stdin_to_a_hook_url(args: object) -> bool:
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False
    posts_stdin = any(
        args[index] == "--data-binary" and args[index + 1] == "@-" for index in range(len(args) - 1)
    )
    return posts_stdin and is_hook_url(args[-1])


def is_ccobs_handler(handler: object) -> bool:
    """Whether ``handler`` is one that ``hook_settings`` writes, at any port, path or
    timeout (older and later versions stay recognized). A handler with any other key,
    URL or form is the user's."""
    if not isinstance(handler, dict):
        return False
    keys = handler.keys()
    if handler.get("type") == "http":
        return keys <= _HTTP_KEYS and is_hook_url(handler.get("url"))
    return (
        keys <= _CURL_KEYS
        and handler.get("type") == "command"
        and handler.get("command") == "curl"
        and handler.get("async") is True
        and _posts_stdin_to_a_hook_url(handler.get("args"))
    )


# Shape.


def _settings_of(settings: Mapping[str, object]) -> dict[str, object]:
    """A deep copy of ``settings``, after checking the containers merge and unmerge
    walk."""
    if not isinstance(settings, dict):
        raise SettingsShapeError("the settings are not a JSON object")
    if "hooks" in settings:
        block = settings["hooks"]
        if not isinstance(block, dict):
            raise SettingsShapeError('"hooks" in the settings is not an object')
        for groups in block.values():
            if not isinstance(groups, list):
                raise SettingsShapeError('an event in "hooks" is not a list')
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise SettingsShapeError(
                        'a matcher group in "hooks" is not an object with a "hooks" list'
                    )
    if "env" in settings and not isinstance(settings["env"], dict):
        raise SettingsShapeError('"env" in the settings is not an object')
    return copy.deepcopy(settings)


def _hooks_of(settings: dict[str, object]) -> _HooksBlock | None:
    # Shape checked by _settings_of.
    return cast(_HooksBlock | None, settings.get("hooks"))


def _env_of(settings: dict[str, object]) -> dict[str, object] | None:
    # Shape checked by _settings_of.
    return cast(dict[str, object] | None, settings.get("env"))


def _identical(first: object, second: object) -> bool:
    """Equal and in the same key order (``{"a": 1} == {"a": True}`` in Python)."""
    return json.dumps(first) == json.dumps(second)


# Hooks.


def _strip(block: _HooksBlock) -> tuple[int, set[str]]:
    """Remove our handlers from every group of ``block``, in place, and the groups that
    removal emptied. Returns the number removed and the events whose list it emptied
    (left in place as an empty list)."""
    removed = 0
    emptied: set[str] = set()
    for event, groups in block.items():
        kept: _Groups = []
        for group in groups:
            handlers = cast(list[object], group["hooks"])
            rest = [handler for handler in handlers if not is_ccobs_handler(handler)]
            removed += len(handlers) - len(rest)
            if rest or len(rest) == len(handlers):
                group["hooks"] = rest
                kept.append(group)
        if groups and not kept:
            emptied.add(event)
        block[event] = kept
    return removed, emptied


def _merge_hooks(
    settings: dict[str, object],
    hooks: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[bool, tuple[str, ...]]:
    """Returns whether the "hooks" object was created and the event keys created."""
    block = _hooks_of(settings)
    created_hooks = block is None and bool(hooks)
    if block is None:
        block = {}
        if hooks:
            settings["hooks"] = block
    _, emptied = _strip(block)
    for event in emptied - set(hooks):
        del block[event]
    created_events: list[str] = []
    for event, groups in hooks.items():
        # An event key is the user's when it existed with entries of theirs, or empty.
        if event not in block or event in emptied:
            created_events.append(event)
        block.setdefault(event, []).extend(copy.deepcopy(dict(group)) for group in groups)
    return created_hooks, tuple(created_events)


# Env.


def _owned(current: Mapping[str, object] | None, state: SetupState) -> dict[str, str]:
    """The keys of ``state.env_written`` that still hold the value setup wrote. A key
    the user changed or deleted is no longer ours."""
    if current is None:
        return {}
    return {
        key: written
        for key, written in state.env_written.items()
        if key in current and current[key] == written
    }


def _conflicts(
    current: Mapping[str, object] | None, env: Mapping[str, str], owned: Mapping[str, str]
) -> tuple[str, ...]:
    if current is None:
        return ()
    return tuple(
        sorted(
            key
            for key, value in env.items()
            if key in current and key not in owned and current[key] != value
        )
    )


def _merge_env(
    settings: dict[str, object],
    env: Mapping[str, str],
    state: SetupState,
) -> tuple[bool, dict[str, str], dict[str, str]]:
    """Returns whether the "env" object was created, and the new owned and previous
    values."""
    current = _env_of(settings)
    created_env = current is None and bool(env)
    if current is None:
        current = {}
        if env:
            settings["env"] = current
    owned, previous = _owned(current, state), state.env_previous
    for key in [key for key in owned if key not in env]:
        if key in previous:
            current[key] = previous[key]
        else:
            del current[key]
    env_written: dict[str, str] = {}
    env_previous: dict[str, str] = {}
    for key, value in env.items():
        if key in owned:
            if key in previous:
                env_previous[key] = previous[key]
        elif key in current:
            existing = current[key]
            if existing == value:
                continue  # pasted by hand: the user's, kept by uninstall
            if not isinstance(existing, str):
                # The value is not echoed: the user's env can hold API keys.
                raise SettingsShapeError(f'"env" key {key} holds a value that is not a string')
            env_previous[key] = existing
        current[key] = value
        env_written[key] = value
    return created_env, env_written, env_previous


def merge(
    settings: Mapping[str, object],
    *,
    hooks: Mapping[str, Sequence[Mapping[str, object]]],
    env: Mapping[str, str],
    state: SetupState | None,
    force: bool = False,
) -> MergeResult:
    """``settings`` with exactly one copy of each group of ``hooks`` (the ``"hooks"`` of
    ``hook_settings``) appended to its event, and the ``env`` of ``telemetry_env``.

    ``state`` is the previous run's, or None. An env key is ours while it holds the
    value setup wrote; a key the user changed is theirs again. A key of the user's
    with another value is a conflict: without ``force``, nothing changes and
    ``conflicts`` lists them all; with ``force``, the value is replaced and recorded in
    the state. An event key setup created stays recorded across runs while it is
    registered. Raises ``SettingsShapeError`` when the settings are not shaped as
    Claude Code documents.
    """
    result = _settings_of(settings)
    previous = state if state is not None else _empty_state()
    conflicts = _conflicts(_env_of(result), env, _owned(_env_of(result), previous))
    if conflicts and not force:
        return MergeResult(result, previous, conflicts, changed=False)

    created_hooks, created_events = _merge_hooks(result, hooks)
    created_env, env_written, env_previous = _merge_env(result, env, previous)
    ours = set(created_events) | set(previous.created_events)
    new_state = SetupState(
        env_written=env_written,
        env_previous=env_previous,
        created_env=created_env or ("env" in settings and previous.created_env),
        created_hooks=created_hooks or ("hooks" in settings and previous.created_hooks),
        created_events=tuple(event for event in hooks if event in ours),
    )
    return MergeResult(result, new_state, (), changed=not _identical(result, settings))


def unmerge(settings: Mapping[str, object], *, state: SetupState | None) -> UnmergeResult:
    """``settings`` without our handlers, and with ``state``, without the env values
    setup wrote (the user's earlier values restored).

    Without a state the env is left alone. ``kept_env`` lists the keys of the state
    the user changed after setup (empty without a state); ``unowned_env`` lists the
    telemetry variables left in the env that the state does not own (all of them
    without a state), so the user can remove them by hand. Raises
    ``SettingsShapeError`` like ``merge``.
    """
    result = _settings_of(settings)
    removed = 0
    block = _hooks_of(result)
    if block is not None:
        removed, emptied = _strip(block)
        for event in emptied:
            if state is None or event in state.created_events:
                del block[event]
        if not block and state is not None and state.created_hooks:
            del result["hooks"]

    current = _env_of(result)
    kept: list[str] = []
    if current is not None and state is not None:
        for key, written in state.env_written.items():
            if key not in current:
                continue
            if current[key] != written:
                kept.append(key)
            elif key in state.env_previous:
                current[key] = state.env_previous[key]
            else:
                del current[key]
        if not current and state.created_env:
            del result["env"]
    owned = state.env_written if state is not None else {}
    present = current if current is not None else {}
    unowned = tuple(key for key in _MANAGED_VARIABLES if key in present and key not in owned)
    return UnmergeResult(
        result, removed, tuple(kept), unowned, changed=not _identical(result, settings)
    )


# State.


def state_to_json(state: SetupState) -> dict[str, object]:
    """The JSON object of ``state``, with ``"version"``."""
    return {
        "version": STATE_VERSION,
        "env_written": dict(state.env_written),
        "env_previous": dict(state.env_previous),
        "created_env": state.created_env,
        "created_hooks": state.created_hooks,
        "created_events": list(state.created_events),
    }


def _invalid(what: str) -> ValueError:
    return ValueError(f"the setup state is not valid: {what}")


def _str_dict(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise _invalid(f"{name} is not an object of strings")
    return dict(value)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{name} is not a boolean")
    return value


def state_from_json(data: object) -> SetupState:
    """The ``SetupState`` in ``data``, the parsed JSON of a state file. Raises
    ``ValueError`` on any shape or type error (the CLI then treats it as missing)."""
    if not isinstance(data, dict) or set(data) != set(_STATE_KEYS):
        raise _invalid("not an object with the expected keys")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != STATE_VERSION:
        raise _invalid("unknown version")
    env_written = _str_dict(data["env_written"], "env_written")
    env_previous = _str_dict(data["env_previous"], "env_previous")
    if not (env_written.keys() | env_previous.keys()) <= set(_MANAGED_VARIABLES):
        raise _invalid("an env key is a variable setup does not manage")
    if not env_previous.keys() <= env_written.keys():
        raise _invalid("env_previous has a key setup did not write")
    events = data["created_events"]
    if (
        not isinstance(events, list)
        or not all(isinstance(event, str) for event in events)
        or len(set(events)) != len(events)
    ):
        raise _invalid("created_events is not a list of distinct strings")
    return SetupState(
        env_written=env_written,
        env_previous=env_previous,
        created_env=_bool(data["created_env"], "created_env"),
        created_hooks=_bool(data["created_hooks"], "created_hooks"),
        created_events=tuple(events),
    )
