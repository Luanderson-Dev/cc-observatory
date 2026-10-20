"""The hook events cc-observatory registers, and the settings block for them (ADR 0025).

Every event in Claude Code's hooks reference (https://code.claude.com/docs/en/hooks)
falls in exactly one category:

- ``REGISTERED_HTTP_EVENTS``: an ``http`` hook with ``timeout: 1`` (ADR 0011);
- ``ASYNC_COMMAND_EVENTS``: ``SessionStart``, which does not accept ``http`` hooks, gets
  an async exec-form ``curl`` that posts the same JSON body (ADR 0011);
- ``FORBIDDEN_EVENTS``: registering them changes Claude Code's behaviour (CLAUDE.md);
- ``NOT_REGISTERED``: events the state machine (ADR 0024) does not need, each with its
  reason.

``hook_settings`` builds the ``{"hooks": {...}}`` block of Claude Code's settings.json
that ``ccobs setup`` (milestone 4) writes. It never adds ``onFailure``,
``statusMessage`` or a matcher, and every handler has a short explicit ``timeout``.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "ASYNC_COMMAND_EVENTS",
    "ASYNC_COMMAND_TIMEOUT_S",
    "CURL_MAX_TIME_S",
    "DEFAULT_APP_PORT",
    "DEFAULT_HOOK_URL",
    "FORBIDDEN_EVENTS",
    "HOOK_PATH",
    "HTTP_TIMEOUT_S",
    "NOT_REGISTERED",
    "REGISTERED_HTTP_EVENTS",
    "hook_settings",
    "is_hook_url",
]

HOOK_PATH: Final = "/hooks"
# The app's port in the stack's port block (ADR 0018): the one place the CLI, the
# domain and the UI take it from.
DEFAULT_APP_PORT: Final = 4300
DEFAULT_HOOK_URL: Final = f"http://127.0.0.1:{DEFAULT_APP_PORT}{HOOK_PATH}"

# Seconds. An http hook is cancelled after HTTP_TIMEOUT_S, which bounds what a hung app
# costs each event. Claude Code does not enforce the timeout of an async command hook,
# so curl's own --max-time bounds the process; the hook still gets an explicit timeout
# above it, like every hook (ADR 0011).
HTTP_TIMEOUT_S: Final = 1
CURL_MAX_TIME_S: Final = 2
ASYNC_COMMAND_TIMEOUT_S: Final = 5

# In the order Claude Code fires them in a session, so the settings read top to bottom.
REGISTERED_HTTP_EVENTS: Final = (
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
ASYNC_COMMAND_EVENTS: Final = ("SessionStart",)
FORBIDDEN_EVENTS: Final = (
    "WorktreeCreate",
    "WorktreeRemove",
    "PreModelSwitch",
    "MessageDisplay",
    "FileChanged",
)
NOT_REGISTERED: Final[tuple[tuple[str, str], ...]] = (
    (
        "Setup",
        "runs only with --init-only, or --init/--maintenance in -p mode, and accepts no http hook",
    ),
    ("UserPromptExpansion", "its payload is the prompt and command arguments (content)"),
    ("PermissionDenied", "fires only in auto mode; progress events close its blocks"),
    (
        "PostToolBatch",
        "not needed: tool outcomes, progress events and Stop already settle pending calls "
        "(maintainer decision for milestone 2)",
    ),
    ("TaskCreated", "task lists are not tracked, and subjects are content"),
    ("TaskCompleted", "task lists are not tracked, and subjects are content"),
    ("TeammateIdle", "agent teams are not tracked; subagents come from SubagentStart"),
    (
        "InstructionsLoaded",
        "carries instruction-file paths and their scope; instruction loading is not "
        "tracked, and it fires many times per session",
    ),
    ("ConfigChange", "configuration changes are not tracked"),
    ("CwdChanged", "carries only paths; cwd already arrives on every event"),
    ("DirectoryAdded", "carries only a path; directories are not tracked"),
    ("PostModelSwitch", "the model comes from SessionStart and the telemetry"),
)

_NULL_DEVICE_WINDOWS: Final = "NUL"
_NULL_DEVICE_POSIX: Final = "/dev/null"
_MAX_PORT: Final = 65535
# http to 127.0.0.1, an optional port without leading zeros, and a path of plain segments
# (none starting with a dot, so no "." or "..") that ends in HOOK_PATH. No user info,
# query or fragment. Only 127.0.0.1: the stack publishes its ports there alone (ADR
# 0018), localhost may resolve to ::1, and [::1] can never connect.
_HOOK_URL: Final = re.compile(
    r"http://127\.0\.0\.1"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
    r"(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*" + re.escape(HOOK_PATH)
)


def is_hook_url(url: object) -> bool:
    """Whether ``url`` is a string that ``hook_settings`` accepts: http to 127.0.0.1, an
    optional port up to 65535, and a path ending in ``HOOK_PATH``. Never raises."""
    if not isinstance(url, str):
        return False
    match = _HOOK_URL.fullmatch(url)
    port = match["port"] if match else None
    return match is not None and (port is None or int(port) <= _MAX_PORT)


def _check_url(url: str) -> str:
    if not is_hook_url(url):
        # The URL is not echoed: it came from the user's input.
        raise ValueError(
            f"the hook URL must be http to 127.0.0.1 (not localhost, which may resolve to "
            f"::1), with a path ending in {HOOK_PATH}"
        )
    return url


def _http_hook(url: str) -> dict[str, object]:
    return {"type": "http", "url": url, "timeout": HTTP_TIMEOUT_S}


def _curl_hook(url: str, *, windows: bool) -> dict[str, object]:
    """An async exec-form command hook: Claude Code spawns curl with ``args`` and no
    shell, so nothing needs quoting on any OS, and the response goes to the null device
    through ``-o`` instead of a shell redirect (ADR 0011)."""
    null_device = _NULL_DEVICE_WINDOWS if windows else _NULL_DEVICE_POSIX
    return {
        "type": "command",
        "command": "curl",
        "args": [
            "-s",
            "-m",
            str(CURL_MAX_TIME_S),
            "-o",
            null_device,
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            url,
        ],
        "async": True,
        "timeout": ASYNC_COMMAND_TIMEOUT_S,
    }


def hook_settings(url: str, *, windows: bool) -> dict[str, object]:
    """The ``hooks`` block of Claude Code's settings.json that posts to ``url``.

    ``windows`` picks the null device of the async ``curl`` (``NUL`` or ``/dev/null``).
    Raises ``ValueError`` unless ``url`` is http to 127.0.0.1 with a path ending in
    ``HOOK_PATH``. Each call returns new objects, in a fixed order.
    """
    _check_url(url)
    hooks: dict[str, object] = {}
    for event in REGISTERED_HTTP_EVENTS:
        hooks[event] = [{"hooks": [_http_hook(url)]}]
    for event in ASYNC_COMMAND_EVENTS:
        hooks[event] = [{"hooks": [_curl_hook(url, windows=windows)]}]
    return {"hooks": hooks}
