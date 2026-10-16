"""Presentation rules for the web UI: paths, durations, board bays and short descriptions.

Paths. Hook payloads carry ``cwd``, which holds the OS username (ADR 0006). By product
decision P2 the full ``cwd`` never reaches the HTML: the session page shows
``display_path`` (the home directory as ``~``) and the overview shows ``project_name``.

- A home directory is ``/home/<user>``, ``/var/home/<user>``, ``/root`` and
  ``Users/<user>`` anywhere in the path (``/Users/<user>``, ``<drive>:\\Users\\<user>``,
  WSL's ``/mnt/<drive>/Users/<user>``, ``/Volumes/<disk>/Users/<user>``,
  ``D:\\Data\\Users\\<user>``), with either separator and in any letter case: hiding
  too much is the safe direction. The shared folders under ``Users`` (``Public``,
  ``Default``, ``Default User``, ``All Users``, ``Shared``) are not homes. Windows'
  ``\\\\?\\`` and ``\\\\.\\`` prefixes are dropped first.
- A UNC path (``\\\\server\\share\\...``) loses its server, a hostname, and its share,
  which is often the username: what is below the share is shown after ``…``, or after
  ``~`` when it is a home directory (such as ``\\\\wsl.localhost\\Ubuntu\\home\\<user>``).
- Any other path is kept as it is. A missing or empty ``cwd`` gives ``None``, and the
  template picks its own placeholder.

Known limitations of the path rules, which recognise a home by its shape only:

- a ``/home/<group>/<user>`` layout hides the group, not the user:
  ``/home/staff/<user>/x`` shows as ``~/<user>/x`` (``/home/users/<user>`` is caught
  by the ``Users`` rule);
- homes elsewhere are not recognised and stay as they are, such as ``/home2/<user>``,
  ``/export/home/<user>`` and FreeBSD's ``/usr/home/<user>``; MSYS and Cygwin forms
  are caught only when they spell ``/home/<user>`` or ``.../Users/<user>`` (such as
  ``/c/Users/<user>`` or ``/cygdrive/c/Users/<user>``), not a home they mount elsewhere;
- so ``project_name`` of an unrecognised home directory itself is the username.

Durations. ``format_duration`` shows elapsed time with its two largest units, the second
zero-padded so that a column of timers lines up: ``42s``, ``4m 05s``, ``1h 03m``,
``2d 04h``. It rounds down, so a ticking timer never runs ahead, and clock skew (a
negative time) shows as ``0s``.

Bays. The overview stacks sessions in bays by status, in ``BAYS`` order, sorted inside
each bay by ``board_sort_key``. Ended sessions stay on the overview for
``RECENT_END_WINDOW_MS`` (product decision P4); older ones are only on the page of all
sessions.

Descriptions use stored metadata only (ADR 0023), never content.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ccobs.domain.sessions import BlockKind, SessionStatus

__all__ = [
    "BAYS",
    "RECENT_END_WINDOW_MS",
    "Bay",
    "StripFacts",
    "bay_of",
    "board_sort_key",
    "describe_activity",
    "describe_block",
    "display_path",
    "format_duration",
    "is_recent_end",
    "project_name",
]

# Paths.

_SEPARATORS: Final = "\\/"
_SEP: Final = r"[\\/]"
_NAME: Final = r"[^\\/]+"
# Windows' extended-length and device prefixes; \\?\UNC\server\share is a UNC path.
_DEVICE_UNC: Final = re.compile(r"[\\/]{2}[?.][\\/]UNC[\\/]", re.IGNORECASE)
_DEVICE: Final = re.compile(r"[\\/]{2}[?.][\\/]")
_UNC: Final = re.compile(
    rf"[\\/]{{2}}(?P<server>{_NAME})(?:{_SEP}(?P<share>{_NAME}))?(?P<rest>.*)", re.DOTALL
)
# [ ] and not a space: the pattern below is verbose, which ignores spaces.
_NOT_A_USER: Final = r"(?:Public|Default|Default[ ]User|All[ ]Users|Shared)"
_HOME: Final = re.compile(
    rf"""
    (?:
        (?:.*?{_SEP})?Users{_SEP}(?!{_NOT_A_USER}(?:{_SEP}|$)){_NAME}
      | (?:{_SEP}var)?{_SEP}home{_SEP}{_NAME}
      | {_SEP}root
    )
    (?={_SEP}|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_HIDDEN: Final = "…"


def display_path(cwd: str | None) -> str | None:
    """``cwd`` with its home directory as ``~`` and a UNC server and share as ``…``."""
    if not cwd:
        return None
    path = _DEVICE_UNC.sub("\\\\\\\\", cwd, count=1)
    path = _DEVICE.sub("", path, count=1)
    unc = _UNC.fullmatch(path)
    if unc is not None:
        rest = unc["rest"]
        return _home_to_tilde(rest) or _HIDDEN + rest.rstrip(_SEPARATORS)
    return _home_to_tilde(path) or cwd


def _home_to_tilde(path: str) -> str | None:
    home = _HOME.match(path)
    if home is None:
        return None
    return "~" + path[home.end() :].rstrip(_SEPARATORS)


def project_name(cwd: str | None) -> str | None:
    """The last component of ``display_path(cwd)``: ``~`` for the home directory itself,
    ``C:`` for a drive root, ``/`` for the root."""
    shown = display_path(cwd)
    if shown is None:
        return None
    trimmed = shown.rstrip(_SEPARATORS)
    if not trimmed:
        return shown[0]
    return re.split(_SEP, trimmed)[-1]


# Durations.

_SECOND: Final = 1000
_UNITS: Final = (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1))


def format_duration(ms: int) -> str:
    """Elapsed time in its two largest units, such as ``4m 05s``; ``0s`` when negative."""
    seconds = max(ms, 0) // _SECOND
    for index, (unit, size) in enumerate(_UNITS[:-1]):
        if seconds >= size:
            minor_unit, minor_size = _UNITS[index + 1]
            minor = seconds % size // minor_size
            return f"{seconds // size}{unit} {minor:02d}{minor_unit}"
    return f"{seconds}s"


# Bays.


class Bay(StrEnum):
    """A group on the overview: one per session status, in board order."""

    WAITING_USER = "waiting_user"
    WORKING = "working"
    IDLE = "idle"
    ENDED = "ended"
    STALE = "stale"

    @property
    def heading(self) -> str:
        return _HEADINGS[self]

    @property
    def rank(self) -> int:
        return BAYS.index(self)


_HEADINGS: Final = {
    Bay.WAITING_USER: "Waiting on you",
    Bay.WORKING: "Working",
    Bay.IDLE: "Idle",
    Bay.ENDED: "Ended in the last 24 hours",
    Bay.STALE: "Stale",
}
BAYS: Final = tuple(Bay)
RECENT_END_WINDOW_MS: Final = 24 * 3_600 * _SECOND


def bay_of(status: str) -> Bay:
    try:
        return Bay(status)
    except ValueError:
        raise ValueError(f"{status!r} is not a session status") from None


@dataclass(frozen=True, kw_only=True)
class StripFacts:
    """What orders a session on the board. ``oldest_open_block_at_ms`` is the opening of
    its oldest open block, for a session waiting on you."""

    session_id: str
    status: SessionStatus
    last_event_at_ms: int
    ended_at_ms: int | None = None
    oldest_open_block_at_ms: int | None = None


def board_sort_key(facts: StripFacts) -> tuple[int, int, str]:
    """Bay order first. Waiting: the longest wait first (from the oldest open block, or
    the last event when no block is known). Ended: the latest end first. Working, idle
    and stale: the latest activity first. Ties go by session ID."""
    bay = bay_of(facts.status)
    if bay is Bay.WAITING_USER:
        opened = facts.oldest_open_block_at_ms
        moment = -(opened if opened is not None else facts.last_event_at_ms)
    elif bay is Bay.ENDED:
        ended = facts.ended_at_ms
        moment = ended if ended is not None else facts.last_event_at_ms
    else:
        moment = facts.last_event_at_ms
    return (bay.rank, -moment, facts.session_id)


def is_recent_end(ended_at_ms: int | None, now_ms: int) -> bool:
    """Whether a session that ended at ``ended_at_ms`` still shows on the overview."""
    return ended_at_ms is not None and now_ms - ended_at_ms <= RECENT_END_WINDOW_MS


# Descriptions.

# Built-in tools whose permission dialog is a question or a plan, not a tool approval
# (from the tools reference; no fixture has them).
_PERMISSION_WORDING: Final = {
    "AskUserQuestion": "Question from Claude",
    "ExitPlanMode": "Plan approval",
}


def describe_block(kind: BlockKind, tool_name: str | None, mcp_server_name: str | None) -> str:
    """What a block waits on, such as ``Permission: Bash`` or ``Question from <server>``."""
    if kind == "elicitation":
        return f"Question from {mcp_server_name or 'an MCP server'}"
    if not tool_name:
        return "Permission"
    return _PERMISSION_WORDING.get(tool_name, f"Permission: {tool_name}")


# SessionEnd's documented reasons (hooks reference; the fixtures show other and
# prompt_input_exit). other, a missing reason and any unknown one read as "Ended".
_END_WORDING: Final = {
    "prompt_input_exit": "Exited",
    "clear": "Cleared",
    "resume": "Resumed",
    "logout": "Logged out",
    "bypass_permissions_disabled": "Bypass permissions disabled",
}


def describe_activity(
    status: SessionStatus,
    *,
    compacting: bool = False,
    running_subagents: int = 0,
    idle_notified: bool = False,
    prompted: bool = True,
    end_reason: str | None = None,
) -> str:
    """What a session is doing, for its strip.

    ``running_subagents`` counts only the running agents that are not ``internal``
    (internal agents never make the session work, ADR 0024). ``prompted`` is whether the
    session ever had a prompt (``last_prompt_id is not None``): an idle session without
    one is ready, not finished. A waiting session is described by its block
    (``describe_block``); ``last_error_code`` is never cleared, so it is not an
    activity."""
    match status:
        case "waiting_user":
            return "Waiting on you"
        case "ended":
            return _END_WORDING.get(end_reason or "", "Ended")
        case "stale":
            return "No recent events"
    if compacting:
        return "Compacting"
    if running_subagents:
        noun = "subagent" if running_subagents == 1 else "subagents"
        return f"{running_subagents} {noun} running"
    if status == "working":
        return "In a turn"
    if not prompted:
        return "Ready"
    return "Waiting for your next prompt" if idle_notified else "Turn finished"
