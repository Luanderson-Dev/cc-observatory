"""Turn raw capture output into sanitized test fixtures.

Reads ``scenarios.json``, the hook captures and the collector's OTLP files from
``--raw``, keeps each scenario's own records, replaces the summaries written by internal
agents, redacts personal data and writes ``hooks/``, ``otlp/`` and ``manifest.json``
under ``--dest``, replacing the files of the previous run. Content-bearing telemetry is
refused (unless ``--allow-content``); after redaction, a structure check makes sure no
key, name or ID changed (other than IDs turned into their fakes) and a guard looks for
personal data left. If any check fails, nothing is written. See
``tools/capture/README.md``.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import getpass
import json
import re
import shutil
import socket
import string
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from tools.capture.jsontypes import JSON, JSONObject

FAKE_USER: Final = "user"
FAKE_HOST: Final = "example-host"
FAKE_EMAIL: Final = "user@example.com"
FAKE_DOMAIN: Final = "example.com"
FAKE_TEXT: Final = "redacted"
FAKE_WINDOWS_HOME: Final = "C:\\Users\\user"
FAKE_MSYS_HOME: Final = "/c/Users/user"
FAKE_POSIX_HOME: Final = "/home/user"
FAKE_WORKSPACE: Final = ("projects", "capture")

IDENTITY_PLACEHOLDERS: Final[dict[str, str]] = {
    "user.email": FAKE_EMAIL,
    "user.account_uuid": "00000000-0000-4000-8000-000000000001",
    "organization.id": "00000000-0000-4000-8000-000000000002",
    "user.account_id": "user_01EXAMPLE",
    "user.id": "0" * 64,
    "user.groups": "example-group",
    "identity.source": "example-idp",
    "host.name": FAKE_HOST,
}

# The capture's own MCP server (elicit_server.py) keeps its name.
CAPTURE_MCP_SERVER: Final = "spike-elicit"
# Skills bundled with Claude Code keep their names; every other skill.name is renamed.
# A bundled skill missing here is only renamed too (the fixture loses a name, nothing leaks).
BUILTIN_SKILLS: Final = frozenset(
    {
        "batch",
        "claude-api",
        "debug",
        "fewer-permission-prompts",
        "init",
        "insights",
        "keybindings-help",
        "loop",
        "review",
        "schedule",
        "security-review",
        "simplify",
        "update-config",
        "verify",
    }
)
# Values Claude Code itself puts in name fields (generic stand-ins for a real name, such as
# mcp_server.name "custom"), and the capture's own MCP server: never collected as names.
KEPT_NAMES: Final = frozenset({"custom", "third-party", "mcp_tool", CAPTURE_MCP_SERVER})
# Fields that hold plugin, marketplace, skill or MCP server names (hook payload keys and
# OTLP attribute keys; mcp_server.name is also the name inside a hook's mcp_server
# object), with the kind of names they hold, and each kind's placeholder.
NAME_FIELD_KINDS: Final[Mapping[str, str]] = {
    "plugin.name": "plugin name",
    "plugin_name": "plugin name",
    "marketplace.name": "marketplace name",
    "marketplace_name": "marketplace name",
    "skill.name": "skill name",
    "skill_name": "skill name",
    "skill": "skill name",
    "mcp_server_name": "MCP server name",
    "mcp_server.name": "MCP server name",
    "server_name": "MCP server name",
}
NAME_PLACEHOLDER_TEMPLATES: Final[Mapping[str, str]] = {
    "plugin name": "plugin-{}",
    "marketplace name": "marketplace-{}",
    "skill name": "skill-{}",
    "MCP server name": "mcp-server-{}",
}

# What replaces a summary written by an internal agent (see internal_summary_keys).
INTERNAL_SUMMARY: Final = "<internal summary removed>"
# Subagent types built into Claude Code, whose last message is kept even without an Agent
# tool call in the session (a subagent the session started keeps it too).
KNOWN_AGENT_TYPES: Final = frozenset({"general-purpose", "Explore", "Plan"})
# The memory types of instructions that come from the capture workspace itself.
WORKSPACE_MEMORY_TYPES: Final = frozenset({"Project", "Local"})

# Fakes for IDs, numbered by first appearance: tool use IDs keep their prefix (toolu_,
# srvtoolu_) and their length, plugin ID hashes are 16 hex digits, client request IDs are
# UUIDs (the a000 group keeps them apart from the identity placeholders).
TOOL_USE_ID_TEMPLATE: Final = "01EXAMPLE{:015d}"
PLUGIN_ID_HASH_TEMPLATE: Final = "{:016x}"
CLIENT_REQUEST_ID_TEMPLATE: Final = "00000000-0000-4000-a000-{:012d}"

# Content-bearing telemetry (ADR 0006). Claude Code only sends it when an OTEL_LOG_* flag
# is on, and the capture settings turn every one of them off.
CONTENT_EVENTS: Final = frozenset({"api_request_body", "api_response_body", "system_prompt"})
CONTENT_ATTRIBUTES: Final = frozenset(
    {"prompt", "prompt_text", "response", "tool_input", "tool_parameters", "user_prompt"}
)
CONTENT_SPAN_EVENTS: Final = frozenset({"tool.output"})
_REDACTED_VALUES: Final = frozenset({"", "<REDACTED>"})

# Username, hostname, --forbid texts and email domains at least this long match inside
# longer words; shorter ones only match as whole words.
MIN_SUBSTRING_LENGTH: Final = 4
GIT_TIMEOUT_SECONDS: Final = 5
_MAX_PATH: Final = 32768

SIGNALS: Final = ("metrics", "logs", "traces")

# Export request layout per signal: resource list -> scope list -> item list.
_OTLP_LAYOUT: Final = {
    "resourceMetrics": ("scopeMetrics", "metrics"),
    "resourceLogs": ("scopeLogs", "logRecords"),
    "resourceSpans": ("scopeSpans", "spans"),
}
_METRIC_DATA: Final = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")

# How a sensitive literal matches, decided by its label (see literal_mode).
type Mode = Literal["anywhere", "word", "name"]
_LOOSE_LABELS: Final = ("username", "hostname", "forbidden text", "email domain")
_NAME_LABELS: Final = tuple(NAME_PLACEHOLDER_TEMPLATES)
_PATH_LABELS: Final = ("home directory", "home alias", "workspace root")
# Values the maintainer controls: a conflict with a placeholder is an error, not a drop.
_CONTROLLED_LABELS: Final = (*_PATH_LABELS, "username", "hostname", "forbidden text")
_PLAIN_WORD: Final = re.compile(r"[A-Za-z]+")

# The placeholders and the words of the allowed paths. A literal that matches inside one
# of them would rewrite the fakes themselves.
_RESERVED_TEXTS: Final = (
    FAKE_EMAIL,
    FAKE_HOST,
    FAKE_TEXT,
    FAKE_WINDOWS_HOME,
    FAKE_MSYS_HOME,
    FAKE_POSIX_HOME,
    "C--Users-user",
    "-home-user",
    "AppData\\Local\\Temp",
    "projects\\capture",
    ".claude.json",
    "req_EXAMPLE0001",
    "toolu_" + TOOL_USE_ID_TEMPLATE.format(1),
    PLUGIN_ID_HASH_TEMPLATE.format(1),
    CLIENT_REQUEST_ID_TEMPLATE.format(1),
    "plugin-1",
    "marketplace-1",
    "skill-1",
    "mcp-server-1",
    *IDENTITY_PLACEHOLDERS.values(),
)
# Values that are already fakes, so they are never treated as personal literals.
_FAKE_VALUES: Final = frozenset(
    value.casefold()
    for value in (
        FAKE_USER,
        FAKE_HOST,
        FAKE_DOMAIN,
        FAKE_TEXT,
        FAKE_WINDOWS_HOME,
        FAKE_MSYS_HOME,
        FAKE_POSIX_HOME,
        *IDENTITY_PLACEHOLDERS.values(),
    )
)

_NAME_FIELDS: Final = frozenset(NAME_FIELD_KINDS)
# The keys of an OTLP AnyValue wrapper: a value inside them belongs to the attribute.
_ANY_VALUE_KEYS: Final = frozenset({"stringValue", "arrayValue", "values"})
# Fields that redaction must never change (besides every dict key).
_PROTECTED_FIELDS: Final = frozenset(
    {
        "hook_event_name",
        "session_id",
        "sessionId",
        "tool_use_id",
        "prompt_id",
        "agent_id",
        "parent_agent_id",
        "traceId",
        "spanId",
        "parentSpanId",
    }
)
_PROTECTED_OTLP_FIELDS: Final = frozenset(
    {
        "name",
        "eventName",
        "key",
        "session.id",
        "prompt.id",
        "event.name",
        "terminal.type",
        "os.type",
        "host.arch",
        "service.name",
        "service.version",
        "model",
        "decision",
        "source",
        "type",
        "language",
        "query_source",
        "start_type",
        "speed",
        "effort",
        "hook_event",
    }
)

_ALNUM: Final = re.compile(r"[A-Za-z0-9]")
# A token: the run of letters, digits, dots, underscores and dashes around a match.
_TOKEN_CHARS: Final = frozenset(string.ascii_letters + string.digits + "._-")
_EDGE_PUNCTUATION: Final = "._-"
# IDs that literals never cut into: UUIDs, 16 to 64 hex digits (trace, span and user
# IDs) and Anthropic object IDs.
_ID_TOKEN: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
    r"|[0-9A-Fa-f]{16,64}"
    r"|(?:(?:srv)?toolu_(?:vrtx_|bdrk_)?|msg_(?:vrtx_|bdrk_)?|req_)[A-Za-z0-9]{16,})"
    r"(?![A-Za-z0-9])"
)
_SCENARIO_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
_FIXTURE_FILE: Final = re.compile(
    r"hooks/[a-z0-9][a-z0-9-]*\.jsonl|otlp/[a-z0-9][a-z0-9-]*/(?:metrics|logs|traces)\.jsonl"
)
_REQUEST_ID: Final = re.compile(
    r"(?<![A-Za-z0-9])req_(?!EXAMPLE\d{4}(?![A-Za-z0-9]))[A-Za-z0-9]{16,}"
)
# Tool use IDs, with the Vertex AI (vrtx) and Bedrock (bdrk) forms; group 1 is the prefix.
_TOOL_USE_ID: Final = re.compile(
    r"(?<![A-Za-z0-9])((?:srv)?toolu_(?:vrtx_|bdrk_)?)"
    r"(?!01EXAMPLE\d{15}(?![A-Za-z0-9]))[A-Za-z0-9]{16,}"
)
_FAKE_PLUGIN_ID_HASH: Final = re.compile(r"0{8}[0-9a-f]{8}")
_FAKE_CLIENT_REQUEST_ID: Final = re.compile(r"00000000-0000-4000-a000-\d{12}")
# The lookbehind makes a match start only where the local part starts: without it, a long
# run of letters with no "@" (a base64 blob) is retried from every position (quadratic).
_EMAIL: Final = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

# Home directories of any user, for the safety net and the guard.
_PATH_NAME: Final = r"[^\\/\s\"'<>|:*?]+"
# A user name: words joined by apostrophes ("O'Brien"); in a run, more name-like words
# after spaces ("John Smith", not "alice && ls").
_NAME_WORD: Final = re.compile(rf"{_PATH_NAME}(?:'{_PATH_NAME})*")
_NAME_RUN: Final = re.compile(rf"{_PATH_NAME}(?:'{_PATH_NAME})*(?: +[\w.()'-]+)*")
_WINDOWS_HOME: Final = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(\\+|/)Users(\\+|/)", re.IGNORECASE)
_MSYS_HOME: Final = re.compile(r"(?<![A-Za-z0-9])(/mnt)?/[A-Za-z]/Users/", re.IGNORECASE)
# Not after a word character, dot, colon or dash, so URL paths (host/users/...) stay.
_POSIX_HOME: Final = re.compile(r"(?<![\w.:-])/(?:Users|home)/", re.IGNORECASE)
_PERCENT_HOME: Final = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]%3A(?:%5C|%2F|[\\/])+Users(?:%5C|%2F|[\\/])+"
    r"|%2F(?:Users|home)%2F",
    re.IGNORECASE,
)
_PERCENT_NAME: Final = re.compile(r"(?:[A-Za-z0-9._~'-]|%(?!5C|2F)[0-9A-F]{2})+", re.IGNORECASE)
# A home directory in Claude Code's projects-dir form: C--Users-<name>-... on Windows,
# -home-<name>-... or -Users-<name>-... on Linux and macOS.
_ENCODED_WINDOWS_HOME: Final = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]--Users-([A-Za-z0-9]+)", re.IGNORECASE
)
_ENCODED_POSIX_HOME: Final = re.compile(
    r"(?<![A-Za-z0-9-])-(?:Users|home)-([A-Za-z0-9]+)", re.IGNORECASE
)
# What may follow the fake home: the capture workspace, Claude Code's own files and temp.
_SEPARATORS: Final = re.compile(r"[\\/]+")
_PATH_REST: Final = re.compile(r"[^\s\"'<>|]*")
_DOT_DOT: Final = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_ALLOWED_UNDER_HOME: Final = re.compile(
    r"(?:projects[\\/]+capture|\.claude(?:\.json[A-Za-z0-9._-]*)?|AppData[\\/]+Local[\\/]+Temp)"
    r"(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)
_ENCODED_COMPONENT: Final = re.compile(r"-+[A-Za-z0-9]")
_ALLOWED_UNDER_ENCODED_HOME: Final = re.compile(
    r"-(?:projects-capture|-claude(?:-json)?|AppData-Local-Temp)(?![A-Za-z0-9])", re.IGNORECASE
)
# Absolute paths, for the guard: drive paths (C:\, C:/); Git Bash and WSL drive paths
# (/c/, /mnt/c/), only with a slash after the letter (so a `cmd /c` switch stays); the
# usual POSIX roots; home-relative paths (~/x, ~name); and a drive path in Claude Code's
# projects-dir form (C--Projects-...), as a path segment or in prose. The POSIX forms do
# not start after a word character, dot, colon, dash, star or hash, so URL paths
# (host/tmp/...), relative paths, globs (**/a/**) and fragments (#/a/b) stay.
_ABSOLUTE_PATH: Final = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?<![\w.:*#(-])(?:/mnt)?/[A-Za-z](?=/)"
    r"|(?<![\w.:*#-])/(?:home|Users|tmp|mnt|opt|var|private|Volumes|media|run/media|root|srv"
    r"|workspaces|cygdrive)(?![A-Za-z0-9._-])"
    r"|(?<![\w~])~(?:[\\/]|[A-Za-z])"
    r"|(?:^|(?<=[\\/]))[A-Za-z]--(?=[A-Za-z0-9])"
    r"|(?<![A-Za-z0-9-])[A-Za-z]--[A-Za-z0-9]+-",
    re.IGNORECASE,
)
# The fake homes an absolute path may start with (the user name ends where a path name
# would, as in _PATH_NAME).
_FAKE_HOME_AT: Final = re.compile(
    r"(?:[A-Za-z]:[\\/]+|(?:/mnt)?/[A-Za-z]/+)Users[\\/]+user(?![^\\/\s\"'<>|:*?])"
    r"|/home/+user(?![^\\/\s\"'<>|:*?])"
    r"|[A-Za-z]--Users-user(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# A file in a capture workspace: under the fake workspace root (the --workspace-root
# after redaction) and inside one of its workspaces, never directly in the root.
_IN_FAKE_WORKSPACE: Final = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+user|(?:/mnt)?/[A-Za-z]/+Users/+user|/home/+user)"
    r"[\\/]+projects[\\/]+capture[\\/]+[^\\/]+[\\/]+[^\\/]",
    re.IGNORECASE,
)
# Literal \uXXXX escapes (JSON inside a string), decoded before the non-ASCII check.
_UNICODE_ESCAPE: Final = re.compile(r"\\u([0-9A-Fa-f]{4})")
# Letters outside ASCII that are units and ordinals rather than words: micro sign and the
# feminine and masculine ordinal indicators.
_ALLOWED_NON_ASCII: Final = frozenset({chr(0x00B5), chr(0x00AA), chr(0x00BA)})

# Plugin and marketplace directories, and MCP tool names (mcp__<server>__<tool>).
_PLUGIN_CACHE_PATH: Final = re.compile(
    rf"\.claude[\\/]+plugins[\\/]+cache[\\/]+({_PATH_NAME})[\\/]+({_PATH_NAME})", re.IGNORECASE
)
_MARKETPLACE_PATH: Final = re.compile(
    rf"\.claude[\\/]+plugins[\\/]+marketplaces[\\/]+({_PATH_NAME})", re.IGNORECASE
)
_PLUGIN_PATH: Final = re.compile(rf"\.claude[\\/]+plugins(?:[\\/]+{_PATH_NAME})+", re.IGNORECASE)
_PATH_SEGMENT: Final = re.compile(r"[^\\/]+")
_MCP_TOOL: Final = re.compile(r"(?<![A-Za-z0-9_])mcp__([A-Za-z0-9_-]+?)__(?=[A-Za-z0-9_-])")


@dataclass(frozen=True)
class LocalContext:
    """Machine-specific values to remove.

    ``home_aliases`` are other spellings of the home directory (Windows 8.3 short names);
    ``forbid`` holds extra texts (a real name, an employer) that must not appear. An empty
    ``user`` or ``host`` is not looked for.
    """

    home: str
    user: str
    host: str
    workspace_root: str | None = None
    home_aliases: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class Names:
    """Personal values found in the capture itself, each replaced by a stable fake."""

    identity: Mapping[str, str] = field(default_factory=dict)
    plugins: tuple[str, ...] = ()
    marketplaces: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    plugin_id_hashes: tuple[str, ...] = ()
    client_request_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """Something found by a check: its kind and location, never the value."""

    kind: str
    location: str


class PersonalDataError(Exception):
    """The guard found personal data left after redaction."""


class ContentError(Exception):
    """The capture holds content-bearing telemetry (prompts, tool content, API bodies)."""


class StructureError(Exception):
    """Redaction would change a key, a name or an ID, or make two keys equal."""


class WorkspaceRootError(Exception):
    """``--workspace-root`` is too close to the top: mapping it to the fake workspace
    would pass far more than the capture workspaces off as workspace."""


class IsolationError(Exception):
    """The capture session loaded instructions from outside its workspace (the user's
    CLAUDE.md, rules or memory), so it was not isolated from the user's own setup."""


class LiteralConflictError(Exception):
    """A value to remove would match inside the placeholders or the allowed path words."""


class DestinationError(Exception):
    """``--dest`` holds files this tool did not write, so it refuses to touch them."""


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    """Non-blank ``values`` in order, without case-insensitive repeats."""
    seen: dict[str, str] = {}
    for value in values:
        if value.strip():
            seen.setdefault(value.casefold(), value)
    return tuple(seen.values())


# Paths of the local machine.


def encode_project_dir(path: str) -> str:
    """Claude Code's projects-dir name for a path: every non-alphanumeric becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _is_windows_path(path: str) -> bool:
    return re.match(r"[A-Za-z]:(?:[\\/]|$)", path) is not None


def _normalize_path(path: str) -> str:
    if _is_windows_path(path):
        return path.replace("/", "\\").rstrip("\\")
    return path.rstrip("/")


def _path_variants(path: str) -> list[str]:
    """Spellings of ``path`` found in payloads (same order for every path of a style)."""
    native = _normalize_path(path)
    if _is_windows_path(path):
        forward = native.replace("\\", "/")
        return [
            native,
            forward,
            native.replace("\\", "\\\\"),
            "/" + native[0].lower() + forward[2:],
            encode_project_dir(native),
        ]
    return [native, encode_project_dir(native)]


def _fake_home(path: str) -> str:
    return FAKE_WINDOWS_HOME if _is_windows_path(path) else FAKE_POSIX_HOME


def _fake_workspace(path: str) -> str:
    separator = "\\" if _is_windows_path(path) else "/"
    return separator.join([_fake_home(path), *FAKE_WORKSPACE])


def _path_replacements(real: str, fake: str) -> list[tuple[str, str]]:
    return list(zip(_path_variants(real), _path_variants(fake), strict=True))


def _home_spellings(ctx: LocalContext) -> list[str]:
    return [_normalize_path(home) for home in _unique([ctx.home, *ctx.home_aliases])]


def _workspace_spellings(ctx: LocalContext) -> list[str]:
    """The workspace root, also through each home alias when it lies under the home."""
    if not ctx.workspace_root:
        return []
    root = _normalize_path(ctx.workspace_root)
    homes = _home_spellings(ctx)
    spellings = [root]
    for home in homes:
        separator = "\\" if _is_windows_path(home) else "/"
        if root.casefold().startswith(home.casefold() + separator):
            spellings += [other + root[len(home) :] for other in homes]
    return list(_unique(spellings))


def path_replacements(ctx: LocalContext) -> list[tuple[str, str]]:
    """Replacements for the home directory, its aliases and the workspace root."""
    pairs: list[tuple[str, str]] = []
    for home in _home_spellings(ctx):
        pairs += _path_replacements(home, _fake_home(home))
    for root in _workspace_spellings(ctx):
        pairs += _path_replacements(root, _fake_workspace(root))
    return pairs


# Lookups on the local machine (replaced in tests).


def git_identity() -> list[str]:
    """``git config user.name`` and ``user.email``; unset values and a missing git are skipped."""
    git = shutil.which("git")
    if git is None:
        return []
    values: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            # S603 cannot see that this is safe: git comes from shutil.which and every
            # argument is fixed (ruff only trusts literals and sys.executable here).
            result = subprocess.run(  # noqa: S603
                [git, "config", "--get", key],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            values.append(value)
    return values


def _short_path_name(path: str) -> str:
    """Windows GetShortPathNameW; empty if the path does not exist or the call fails."""
    if sys.platform != "win32":
        return ""
    buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    length: int = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else ""


def short_path(path: str) -> str | None:
    """The 8.3 short form of ``path`` on Windows, if it differs; None elsewhere."""
    if sys.platform != "win32":
        return None
    short = _short_path_name(path)
    return short if short and short.casefold() != path.casefold() else None


# Matching modes, IDs and replacers.


def literal_mode(label: str, value: str) -> Mode:
    """How the literal ``value`` with guard label ``label`` matches.

    - ``anywhere``: the username, hostname, ``--forbid`` texts and email domains (and the
      ``host.name`` identity value) of MIN_SUBSTRING_LENGTH characters or more match
      inside longer words, and the whole token around them is replaced;
    - ``name``: plugin, marketplace, skill and MCP server names that are plain words
      (``git``, ``memory``) only match where they are used as names: in name fields, in
      ``mcp__<server>__`` tool names and in ``.claude/plugins/...`` paths;
    - ``word``: everything else matches as a whole word.
    """
    if label.startswith(_NAME_LABELS) and _PLAIN_WORD.fullmatch(value):
        return "name"
    if _is_loose(label) and len(value) >= MIN_SUBSTRING_LENGTH:
        return "anywhere"
    return "word"


def _is_loose(label: str) -> bool:
    return label.startswith(_LOOSE_LABELS) or label.endswith("(host.name)")


def _literal_pattern(literal: str) -> str:
    """Regex for ``literal`` that does not match inside a longer alphanumeric word."""
    pattern = re.escape(literal)
    if _ALNUM.fullmatch(literal[0]):
        pattern = r"(?<![A-Za-z0-9])" + pattern
    if _ALNUM.fullmatch(literal[-1]):
        pattern += r"(?![A-Za-z0-9])"
    return pattern


def _pattern_for(value: str, mode: Mode) -> str:
    return re.escape(value) if mode == "anywhere" else _literal_pattern(value)


def _alternation(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """One capturing group per pattern, case-insensitive (None for no patterns)."""
    groups = [f"({pattern})" for pattern in patterns]
    return re.compile("|".join(groups), re.IGNORECASE) if groups else None


def _conflicts(value: str, mode: Mode) -> bool:
    if mode == "name":
        return False
    pattern = re.compile(_pattern_for(value, mode), re.IGNORECASE)
    return any(pattern.search(text) for text in _RESERVED_TEXTS)


def reserved_conflict(text: str) -> bool:
    """Whether ``text``, matched like a ``--forbid`` text, would hit a placeholder or an
    allowed path word (and so corrupt the fixtures)."""
    return _conflicts(text, literal_mode("forbidden text", text))


class _IdSpans:
    """The ID-shaped tokens of a text, which literals never cut into."""

    def __init__(self, text: str) -> None:
        self._spans = [match.span() for match in _ID_TOKEN.finditer(text)]
        self._starts = [start for start, _ in self._spans]
        self._ends = [end for _, end in self._spans]
        self._length = len(text)

    def cuts(self, start: int, end: int) -> bool:
        """Whether ``text[start:end]`` overlaps an ID without containing it whole."""
        index = max(bisect.bisect_right(self._starts, start) - 1, 0)
        while index < len(self._spans) and self._spans[index][0] < end:
            id_start, id_end = self._spans[index]
            if id_end > start and not (start <= id_start and id_end <= end):
                return True
            index += 1
        return False

    def left_limit(self, position: int) -> int:
        """End of the last ID that ends at or before ``position`` (0 if none)."""
        index = bisect.bisect_right(self._ends, position) - 1
        return self._ends[index] if index >= 0 else 0

    def right_limit(self, position: int) -> int:
        """Start of the first ID that starts at or after ``position`` (the end if none)."""
        index = bisect.bisect_left(self._starts, position)
        return self._starts[index] if index < len(self._starts) else self._length


def _token_span(text: str, start: int, end: int, left: int, right: int) -> tuple[int, int]:
    """The token around ``text[start:end]`` within ``left:right``, edge punctuation off."""
    first, last = start, end
    while first > left and text[first - 1] in _TOKEN_CHARS:
        first -= 1
    while first < start and text[first] in _EDGE_PUNCTUATION:
        first += 1
    while last < right and text[last] in _TOKEN_CHARS:
        last += 1
    while last > end and text[last - 1] in _EDGE_PUNCTUATION:
        last -= 1
    return first, last


class _Replacer:
    """Replace literals case-insensitively, longest first, never cutting into an ID.

    A literal marked ``anywhere`` matches inside longer words and the whole token around
    it is replaced (dots, dashes and underscores at the token's edges stay), by the fake
    of the longest such literal in the token. With ``tokens=False`` (dict keys) it only
    matches as a whole word, like the other literals.
    """

    def __init__(self, replacements: Iterable[tuple[str, str, bool]]) -> None:
        unique: dict[str, tuple[str, str, bool]] = {}
        for real, fake, anywhere in replacements:
            if real:
                unique.setdefault(real.casefold(), (real, fake, anywhere))
        self._ordered = sorted(unique.values(), key=lambda entry: len(entry[0]), reverse=True)
        self._anywhere = [(real.casefold(), fake) for real, fake, loose in self._ordered if loose]
        self._patterns = {
            True: _alternation(
                _pattern_for(real, "anywhere" if loose else "word")
                for real, _, loose in self._ordered
            ),
            False: _alternation(_literal_pattern(real) for real, _, _ in self._ordered),
        }

    def _fake_for(self, token: str, default: str) -> str:
        folded = token.casefold()
        return next((fake for real, fake in self._anywhere if real in folded), default)

    def __call__(self, text: str, *, tokens: bool = True) -> str:
        pattern = self._patterns[tokens]
        if pattern is None:
            return text
        ids: _IdSpans | None = None
        spans: list[tuple[int, int, str]] = []
        for match in pattern.finditer(text):
            previous_end = spans[-1][1] if spans else 0
            if match.end() <= previous_end:
                continue
            if ids is None:
                ids = _IdSpans(text)
            if ids.cuts(match.start(), match.end()):
                continue
            _, fake, loose = self._ordered[(match.lastindex or 1) - 1]
            start, end = match.span()
            if tokens and loose:
                left = max(previous_end, ids.left_limit(start))
                start, end = _token_span(text, start, end, left, ids.right_limit(end))
                fake = self._fake_for(text[start:end], fake)
            if spans and start < previous_end:
                start, _, fake = spans.pop()
                end = max(end, previous_end)
                fake = self._fake_for(text[start:end], fake)
            spans.append((start, end, fake))
        parts: list[str] = []
        done = 0
        for start, end, fake in spans:
            parts += [text[done:start], fake]
            done = end
        parts.append(text[done:])
        return "".join(parts)


class _PlainNames:
    """Collected names that are plain words (``git``, ``memory``, ``commit``).

    They are too common to replace everywhere, so they are only matched where they are
    used as names: as the whole value of a name field, as the server in
    ``mcp__<server>__<tool>`` and as a segment of a ``.claude/plugins/...`` path.
    """

    def __init__(self, names: Mapping[str, tuple[str, str]]) -> None:
        self._names = dict(names)

    def entry(self, value: str) -> tuple[str, str] | None:
        """``(label, fake)`` of a name field value that is one of the names."""
        return self._names.get(value.casefold())

    def field(self, value: str) -> str:
        """A name field value, renamed if it is one of the names."""
        found = self.entry(value)
        return found[1] if found else value

    def uses(self, text: str) -> list[tuple[int, int, str, str]]:
        """``(start, end, label, fake)`` of each name in tool names and plugin paths."""
        if not self._names:
            return []
        found: list[tuple[int, int, str, str]] = []
        for match in _MCP_TOOL.finditer(text):
            if (entry := self.entry(match.group(1))) is not None:
                found.append((match.start(1), match.end(1), *entry))
        for match in _PLUGIN_PATH.finditer(text):
            segments = list(_PATH_SEGMENT.finditer(text, match.start(), match.end()))
            for segment in segments[3:]:  # after .claude, plugins and cache/marketplaces
                if (entry := self.entry(segment.group())) is not None:
                    found.append((segment.start(), segment.end(), *entry))
        return sorted(found)

    def replace(self, text: str) -> str:
        """``text`` with the names in tool names and plugin paths renamed."""
        parts: list[str] = []
        done = 0
        for start, end, _, fake in self.uses(text):
            if start >= done:
                parts += [text[done:start], fake]
                done = end
        parts.append(text[done:])
        return "".join(parts)


def _attribute_key(node: JSONObject) -> str | None:
    key, value = node.get("key"), node.get("value")
    return key if isinstance(key, str) and isinstance(value, dict) else None


def _child_field(node: JSONObject, key: str, parent: str | None) -> str | None:
    """The field a child value belongs to: its key, or for the value of an OTLP KeyValue
    the attribute key (carried through the AnyValue wrapper), or ``mcp_server.name`` for
    the name in a hook payload's ``mcp_server`` object."""
    attribute = _attribute_key(node)
    if attribute is not None and key == "value":
        return attribute
    if key in _ANY_VALUE_KEYS:
        return parent
    if parent == "mcp_server" and key == "name":
        return "mcp_server.name"
    return key


# Identity attributes, request IDs, plugins, marketplaces, skills and MCP servers.


class StableNames:
    """Give each distinct value a fake name, numbered by first appearance."""

    def __init__(self, template: str) -> None:
        self._template = template
        self._names: dict[str, str] = {}

    def __call__(self, value: str) -> str:
        if value not in self._names:
            self._names[value] = self._template.format(len(self._names) + 1)
        return self._names[value]


def replace_request_ids(text: str, names: StableNames) -> str:
    """Replace Anthropic request IDs (``req_...``) with stable fakes."""
    return _REQUEST_ID.sub(lambda match: names(match.group()), text)


def replace_tool_use_ids(text: str, names: StableNames) -> str:
    """Replace tool use IDs (``toolu_...``, ``srvtoolu_...``, ``toolu_vrtx_...``,
    ``toolu_bdrk_...``) with stable fakes that keep the prefix; ``names`` numbers the IDs
    (TOOL_USE_ID_TEMPLATE gives the same length)."""
    return _TOOL_USE_ID.sub(lambda match: f"{match[1]}{names(match.group())}", text)


def _walk(value: JSON, location: str = "$") -> Iterator[tuple[str, JSON]]:
    """Every node of a JSON value with its location (``$.a[0].b``)."""
    yield location, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{location}[{index}]")


def _field_strings(value: JSON, parent: str | None = None) -> Iterator[tuple[str | None, str]]:
    """Every string value (not key) of a JSON value, with the field it belongs to (see
    _child_field; None at the top)."""
    if isinstance(value, str):
        yield parent, value
    elif isinstance(value, list):
        for item in value:
            yield from _field_strings(item, parent)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _field_strings(item, _child_field(value, key, parent))


def _strings(value: JSON) -> Iterator[str]:
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def _map_string_values(value: JSON, rename: Callable[[str], str]) -> JSON:
    """Apply ``rename`` to every ``stringValue`` inside an OTLP AnyValue."""
    if isinstance(value, list):
        return [_map_string_values(item, rename) for item in value]
    if isinstance(value, dict):
        return {
            key: rename(item)
            if key == "stringValue" and isinstance(item, str)
            else _map_string_values(item, rename)
            for key, item in value.items()
        }
    return value


def _string_values(value: JSON) -> list[str]:
    """Every ``stringValue`` inside an OTLP AnyValue."""
    found: list[str] = []

    def collect(text: str) -> str:
        found.append(text)
        return text

    _map_string_values(value, collect)
    return found


def _key_values(value: JSON) -> Iterator[tuple[str, str, JSONObject]]:
    """Location, key and AnyValue of every OTLP KeyValue (``{"key", "value"}``)."""
    for location, node in _walk(value):
        if isinstance(node, dict):
            key, inner = node.get("key"), node.get("value")
            if isinstance(key, str) and isinstance(inner, dict):
                yield location, key, inner


def redact_identity_attributes(value: JSON) -> JSON:
    """Put the fixed placeholders in every identity attribute, by key."""
    if isinstance(value, list):
        return [redact_identity_attributes(item) for item in value]
    if not isinstance(value, dict):
        return value
    key, inner = value.get("key"), value.get("value")
    if isinstance(key, str) and isinstance(inner, dict) and key in IDENTITY_PLACEHOLDERS:
        placeholder = IDENTITY_PLACEHOLDERS[key]
        return {**value, "value": _map_string_values(inner, lambda _: placeholder)}
    return {k: redact_identity_attributes(item) for k, item in value.items()}


def collect_identity_values(docs: Iterable[JSON]) -> dict[str, str]:
    """Map every real identity attribute value found in ``docs`` to its placeholder."""
    found: dict[str, str] = {}
    for doc in docs:
        for _, key, inner in _key_values(doc):
            placeholder = IDENTITY_PLACEHOLDERS.get(key)
            if placeholder is None:
                continue
            for real in _string_values(inner):
                if real and real != placeholder:
                    found.setdefault(real, placeholder)
    return found


def _is_builtin_skill(name: str) -> bool:
    return name.removeprefix("/") in BUILTIN_SKILLS


def _is_kept_name(name: str) -> bool:
    return name in KEPT_NAMES


def _is_kept_skill(name: str) -> bool:
    return _is_kept_name(name) or _is_builtin_skill(name)


def _is_fake_plugin_id_hash(value: str) -> bool:
    return _FAKE_PLUGIN_ID_HASH.fullmatch(value) is not None


def _is_fake_client_request_id(value: str) -> bool:
    return _FAKE_CLIENT_REQUEST_ID.fullmatch(value) is not None


def _names(found: Iterable[str], keep: Callable[[str], bool]) -> tuple[str, ...]:
    return tuple(name for name in _unique(found) if not keep(name))


def collect_names(otlp_docs: Iterable[JSON], hook_payloads: Iterable[JSON]) -> Names:
    """Identity values, plugins, marketplaces, skills, MCP servers, plugin ID hashes and
    client request IDs, by first appearance.

    Names come from every field of NAME_FIELD_KINDS (``plugin.name``, ``plugin_name``,
    ``skill``, ``mcp_server.name``, ...), from ``.claude/plugins/...`` paths (plugins and
    marketplaces) and from ``mcp__<server>__<tool>`` names (MCP servers); built-in skills
    are kept. Plugin ID hashes and client request IDs come from ``plugin_id_hash`` and
    ``client_request_id``. KEPT_NAMES and values that are already fakes are not collected.
    """
    docs, payloads = list(otlp_docs), list(hook_payloads)
    found: dict[str, list[str]] = {kind: [] for kind in NAME_PLACEHOLDER_TEMPLATES}
    plugin_id_hashes: list[str] = []
    client_request_ids: list[str] = []
    ids_by_field = {"plugin_id_hash": plugin_id_hashes, "client_request_id": client_request_ids}
    for value in [*docs, *payloads]:
        for name, text in _field_strings(value):
            kind = NAME_FIELD_KINDS.get(name or "")
            if kind is not None:
                found[kind].append(text)
            elif name in ids_by_field:
                ids_by_field[name].append(text)
    plugins, marketplaces = found["plugin name"], found["marketplace name"]
    servers = found["MCP server name"]
    for text in (text for value in [*docs, *payloads] for text in _strings(value)):
        for match in _PLUGIN_CACHE_PATH.finditer(text):
            marketplaces.append(match[1])
            plugins.append(match[2])
        marketplaces += [match[1] for match in _MARKETPLACE_PATH.finditer(text)]
        servers += [match[1] for match in _MCP_TOOL.finditer(text)]
    return Names(
        identity=collect_identity_values(docs),
        plugins=_names(plugins, keep=_is_kept_name),
        marketplaces=_names(marketplaces, keep=_is_kept_name),
        skills=_names(found["skill name"], keep=_is_kept_skill),
        mcp_servers=_names(servers, keep=_is_kept_name),
        plugin_id_hashes=_names(plugin_id_hashes, keep=_is_fake_plugin_id_hash),
        client_request_ids=_names(client_request_ids, keep=_is_fake_client_request_id),
    )


def email_domains(names: Names) -> list[str]:
    """Domains of the collected ``user.email`` values."""
    return list(
        _unique(
            real.rpartition("@")[2]
            for real, placeholder in names.identity.items()
            if placeholder == FAKE_EMAIL and "@" in real
        )
    )


def _name_groups(names: Names) -> list[tuple[str, tuple[str, ...], str]]:
    values = {
        "plugin name": names.plugins,
        "marketplace name": names.marketplaces,
        "skill name": names.skills,
        "MCP server name": names.mcp_servers,
    }
    return [(kind, values[kind], template) for kind, template in NAME_PLACEHOLDER_TEMPLATES.items()]


def _id_groups(names: Names) -> list[tuple[str, tuple[str, ...], str]]:
    return [
        ("plugin id hash", names.plugin_id_hashes, PLUGIN_ID_HASH_TEMPLATE),
        ("client request id", names.client_request_ids, CLIENT_REQUEST_ID_TEMPLATE),
    ]


# Sensitive literals: what the guard looks for, how they match and their fakes.


@dataclass(frozen=True)
class _Literal:
    label: str
    value: str
    fake: str
    mode: Mode


def _forbidden_fake(text: str) -> str:
    return FAKE_EMAIL if _EMAIL.fullmatch(text) else FAKE_TEXT


def _candidates(ctx: LocalContext, names: Names) -> list[tuple[str, str, str]]:
    """``(label, value, fake)`` of every value to remove."""
    candidates = [("home directory", _normalize_path(ctx.home), _fake_home(ctx.home))]
    candidates += [
        (f"home alias {number}", _normalize_path(alias), _fake_home(alias))
        for number, alias in enumerate(ctx.home_aliases, start=1)
    ]
    if ctx.workspace_root:
        root = ctx.workspace_root
        candidates.append(("workspace root", _normalize_path(root), _fake_workspace(root)))
    candidates += [("username", ctx.user, FAKE_USER), ("hostname", ctx.host, FAKE_HOST)]
    candidates += [
        (f"forbidden text {number}", text, _forbidden_fake(text))
        for number, text in enumerate(ctx.forbid, start=1)
    ]
    keys = {placeholder: key for key, placeholder in IDENTITY_PLACEHOLDERS.items()}
    candidates += [
        (f"identity value {number} ({keys.get(fake, 'identity')})", real, fake)
        for number, (real, fake) in enumerate(names.identity.items(), start=1)
    ]
    candidates += [
        (f"email domain {number}", domain, FAKE_DOMAIN)
        for number, domain in enumerate(email_domains(names), start=1)
    ]
    candidates += [
        (f"{label} {number}", value, template.format(number))
        for label, values, template in [*_name_groups(names), *_id_groups(names)]
        for number, value in enumerate(values, start=1)
    ]
    return candidates


def _literals(ctx: LocalContext, names: Names) -> list[_Literal]:
    """The sensitive literals, without fakes and blanks.

    A literal that would match inside a placeholder or an allowed path word is dropped if
    it was found in the capture (it is a generic word then), and refused with
    LiteralConflictError if it comes from the maintainer (username, hostname, paths,
    ``--forbid``).
    """
    literals: list[_Literal] = []
    refused: list[str] = []
    for label, value, fake in _candidates(ctx, names):
        if not value.strip() or value.casefold() in _FAKE_VALUES:
            continue
        mode = literal_mode(label, value)
        if _conflicts(value, mode):
            if label.startswith(_CONTROLLED_LABELS):
                refused.append(label)
            continue
        literals.append(_Literal(label, value, fake, mode))
    if refused:
        raise LiteralConflictError(
            f"{', '.join(refused)} would match inside the placeholders or the allowed path"
            " words (user, example-host, example.com, AppData\\Local\\Temp,"
            " projects\\capture, .claude, ...), so replacing it would corrupt the fixtures."
            " For the username or hostname, pass --user '' or --host '' (the home"
            " directory is still replaced as a path) and add distinctive forms of your"
            " name with --forbid; leave out a conflicting --forbid text."
        )
    return literals


def guard_literals(ctx: LocalContext, names: Names) -> dict[str, str]:
    """What the guard looks for, by label: local values, forbidden texts and ``names``."""
    return {literal.label: literal.value for literal in _literals(ctx, names)}


# Safety net and guard.


def _allowed_email(email: str) -> bool:
    return email.lower().endswith("@example.com")


def _is_fake_name(name: str) -> bool:
    return name.casefold() == FAKE_USER


def _user_name_end(text: str, start: int, extend_fake: bool) -> int:
    """End of the user name that starts at ``start``, after a home-directory prefix.

    A Windows name may hold spaces, so name-like words after the first one belong to it
    ("John Smith"); if a colon follows them (``C:\\Users\\a and D:\\...``), the name is
    only the first word. The fake name is only extended when ``extend_fake`` is set (the
    guard), so ``C:\\Users\\user alice\\x`` counts as someone else's home.
    """
    word = _NAME_WORD.match(text, start)
    if word is None:
        return start
    if _is_fake_name(word.group()) and not extend_fake:
        return word.end()
    run = _NAME_RUN.match(text, start)
    if run is None or text[run.end() : run.end() + 1] in (":", "*", "?"):
        return word.end()
    return run.end()


def _percent_name_end(text: str, start: int, extend_fake: bool) -> int:
    name = _PERCENT_NAME.match(text, start)
    return name.end() if name else start


def _windows_fake(match: re.Match[str]) -> str:
    return f"C:{match.group(1)}Users{match.group(2)}{FAKE_USER}"


def _msys_fake(match: re.Match[str]) -> str:
    return (match.group(1) or "") + FAKE_MSYS_HOME


def _posix_fake(match: re.Match[str]) -> str:
    return FAKE_POSIX_HOME


def _percent_fake(match: re.Match[str]) -> str:
    return match.group() + FAKE_USER


@dataclass(frozen=True)
class _HomeStyle:
    """One way to write a home directory: its prefix, the fake that replaces prefix and
    name, how the name ends, and whether the workspace allowlist applies after it."""

    prefix: re.Pattern[str]
    fake: Callable[[re.Match[str]], str]
    name_end: Callable[[str, int, bool], int]
    allowlist: bool


_HOME_STYLES: Final = (
    _HomeStyle(_WINDOWS_HOME, _windows_fake, _user_name_end, allowlist=True),
    _HomeStyle(_MSYS_HOME, _msys_fake, _user_name_end, allowlist=True),
    _HomeStyle(_POSIX_HOME, _posix_fake, _user_name_end, allowlist=True),
    _HomeStyle(_PERCENT_HOME, _percent_fake, _percent_name_end, allowlist=False),
)
# The fake homes in projects-dir form (encode_project_dir of the fake homes).
_ENCODED_HOMES: Final = (
    (_ENCODED_WINDOWS_HOME, "C--Users-user"),
    (_ENCODED_POSIX_HOME, "-home-user"),
)


def _home_matches(
    text: str, style: _HomeStyle, extend_fake: bool
) -> Iterator[tuple[re.Match[str], int]]:
    """Each home-directory prefix in ``text`` with the end of the user name after it."""
    for match in style.prefix.finditer(text):
        end = style.name_end(text, match.end(), extend_fake)
        if end > match.end():
            yield match, end


def _replace_homes(text: str, style: _HomeStyle) -> str:
    parts: list[str] = []
    done = 0
    for match, end in _home_matches(text, style, extend_fake=False):
        if match.start() < done or _is_fake_name(text[match.end() : end]):
            continue
        parts += [text[done : match.start()], style.fake(match)]
        done = end
    parts.append(text[done:])
    return "".join(parts)


def _replace_encoded_homes(text: str, pattern: re.Pattern[str], fake_home: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group() if _is_fake_name(match.group(1)) else fake_home

    return pattern.sub(replace, text)


def scrub_safety_net(text: str) -> str:
    """Last pass over every string: other emails and other people's home directories."""
    text = _EMAIL.sub(lambda m: m.group() if _allowed_email(m.group()) else FAKE_EMAIL, text)
    for style in _HOME_STYLES:
        text = _replace_homes(text, style)
    for pattern, fake_home in _ENCODED_HOMES:
        text = _replace_encoded_homes(text, pattern, fake_home)
    return text


def _has_user_path(text: str) -> bool:
    for style in _HOME_STYLES:
        for match, end in _home_matches(text, style, extend_fake=True):
            if not _is_fake_name(text[match.end() : end]):
                return True
    return any(
        not _is_fake_name(match.group(1))
        for pattern, _ in _ENCODED_HOMES
        for match in pattern.finditer(text)
    )


def _allowed_under_home(text: str, position: int) -> bool:
    separators = _SEPARATORS.match(text, position)
    if separators is None:
        return True
    rest = _PATH_REST.match(text, separators.end())
    if rest is not None and _DOT_DOT.search(rest.group()):
        return False
    position = separators.end()
    return (
        _NAME_WORD.match(text, position) is None
        or _ALLOWED_UNDER_HOME.match(text, position) is not None
    )


def _allowed_under_encoded_home(text: str, position: int) -> bool:
    if _ENCODED_COMPONENT.match(text, position) is None:
        return True
    return _ALLOWED_UNDER_ENCODED_HOME.match(text, position) is not None


def _has_path_outside_workspace(text: str) -> bool:
    """A path under the fake home outside the workspace, ``.claude`` and the temp dir."""
    for style in _HOME_STYLES:
        if not style.allowlist:
            continue
        for match, end in _home_matches(text, style, extend_fake=False):
            if _is_fake_name(text[match.end() : end]) and not _allowed_under_home(text, end):
                return True
    return any(
        _is_fake_name(match.group(1)) and not _allowed_under_encoded_home(text, match.end())
        for pattern, _ in _ENCODED_HOMES
        for match in pattern.finditer(text)
    )


def _has_path_outside_fake_home(text: str) -> bool:
    """An absolute path that does not start with a fake home (another drive, the root of
    a drive, /tmp, /opt, ..., or someone else's home)."""
    return any(
        _FAKE_HOME_AT.match(text, match.start()) is None for match in _ABSOLUTE_PATH.finditer(text)
    )


def _has_non_ascii_letter(text: str) -> bool:
    """A letter or combining mark outside ASCII, after decoding literal ``\\uXXXX`` escapes
    and NFC normalization. Punctuation, symbols and digits are fine, and so are the micro
    sign and the ordinal indicators."""
    if text.isascii() and _UNICODE_ESCAPE.search(text) is None:
        return False
    decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match[1], 16)), text)
    return any(
        not char.isascii()
        and char not in _ALLOWED_NON_ASCII
        and unicodedata.category(char)[0] in ("L", "M")
        for char in unicodedata.normalize("NFC", decoded)
    )


def _in_fake_workspace(path: str) -> bool:
    return _IN_FAKE_WORKSPACE.match(path) is not None and _DOT_DOT.search(path) is None


def find_isolation_problems(payload: Mapping[str, JSON]) -> list[Finding]:
    """Signs that a capture session was not isolated from the user's own setup, in one
    redacted hook payload: an InstructionsLoaded whose memory type is not Project or
    Local, or whose file is not in the capture workspace (``<fake home>/projects/capture``
    once ``--workspace-root`` is replaced). Findings never hold values."""
    if payload.get("hook_event_name") != "InstructionsLoaded":
        return []
    findings: list[Finding] = []
    memory_type, file_path = payload.get("memory_type"), payload.get("file_path")
    if not (isinstance(memory_type, str) and memory_type in WORKSPACE_MEMORY_TYPES):
        findings.append(Finding("instructions not from the capture workspace", "$.memory_type"))
    if not (isinstance(file_path, str) and _in_fake_workspace(file_path)):
        findings.append(Finding("instructions file outside the capture workspace", "$.file_path"))
    return findings


def _is_foreign_id(field: str | None, text: str) -> str | None:
    """The kind of a ``plugin_id_hash`` or ``client_request_id`` value that is not a fake."""
    if not text:
        return None
    if field == "plugin_id_hash" and not _is_fake_plugin_id_hash(text):
        return "plugin id hash"
    if field == "client_request_id" and not _is_fake_client_request_id(text):
        return "client request id"
    return None


class _Detector:
    """Finds personal data in one string: the guard's checks, by kind or label."""

    def __init__(self, literals: Mapping[str, str]) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        plain: dict[str, tuple[str, str]] = {}
        for label, value in literals.items():
            if not value:
                continue
            mode = literal_mode(label, value)
            if mode == "name":
                plain.setdefault(value.casefold(), (label, ""))
            else:
                self._patterns.append((label, re.compile(_pattern_for(value, mode), re.I)))
        self._plain = _PlainNames(plain)

    def _literal_found(self, pattern: re.Pattern[str], text: str, ids: list[_IdSpans]) -> bool:
        for match in pattern.finditer(text):
            if not ids:
                ids.append(_IdSpans(text))
            if not ids[0].cuts(match.start(), match.end()):
                return True
        return False

    def kinds(self, text: str, field: str | None = None) -> list[str]:
        """Kinds of personal data in ``text``, a value of ``field`` (None for keys)."""
        if text == INTERNAL_SUMMARY:
            return []
        kinds = []
        if any(not _allowed_email(m.group()) for m in _EMAIL.finditer(text)):
            kinds.append("email")
        if _has_user_path(text):
            kinds.append("user path")
        if _has_path_outside_workspace(text):
            kinds.append("path outside workspace")
        if _has_path_outside_fake_home(text):
            kinds.append("absolute path outside fake home")
        if _REQUEST_ID.search(text):
            kinds.append("request id")
        if _TOOL_USE_ID.search(text):
            kinds.append("tool use id")
        ids: list[_IdSpans] = []
        kinds += [
            label for label, pattern in self._patterns if self._literal_found(pattern, text, ids)
        ]
        kinds += [label for _, _, label, _ in self._plain.uses(text)]
        if field in _NAME_FIELDS and (entry := self._plain.entry(text)) is not None:
            kinds.append(entry[0])
        if (foreign := _is_foreign_id(field, text)) is not None:
            kinds.append(foreign)
        if _has_non_ascii_letter(text):
            kinds.append("non-ascii letter")
        return list(dict.fromkeys(kinds))


def find_personal_data(value: JSON, literals: Mapping[str, str]) -> list[Finding]:
    """Find emails outside example.com, user paths, paths under the fake home outside the
    workspace, absolute paths outside the fake home, real request and tool use IDs,
    ``plugin_id_hash`` and ``client_request_id`` values that are not fakes, letters outside
    ASCII, and the given literals.

    ``literals`` maps a label (used as the finding's kind) to a value; each matches as
    literal_mode says, and never inside an ID. Keys are checked too. Findings never hold
    values.
    """
    detector = _Detector(literals)
    findings: list[Finding] = []

    def visit(node: JSON, location: str, parent: str | None) -> None:
        if isinstance(node, str):
            findings.extend(Finding(kind, location) for kind in detector.kinds(node, parent))
        elif isinstance(node, dict):
            for key, child in node.items():
                key_kinds = detector.kinds(key)
                findings.extend(Finding(kind, f"{location} (key)") for kind in key_kinds)
                where = f"{location}.<key>" if key_kinds else f"{location}.{key}"
                visit(child, where, _child_field(node, key, parent))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{location}[{index}]", parent)

    visit(value, "$", None)
    return findings


# Content-bearing telemetry.


def _content_event(name: JSON) -> str | None:
    if isinstance(name, str):
        event = name.removeprefix("claude_code.")
        if event in CONTENT_EVENTS:
            return event
    return None


def _is_blank(value: JSONObject) -> bool:
    text = value.get("stringValue")
    return not value or (len(value) == 1 and isinstance(text, str) and text in _REDACTED_VALUES)


def find_content(doc: JSON) -> list[Finding]:
    """Content-bearing telemetry in an OTLP export request: API body and system prompt
    events, content attributes that are not empty or ``<REDACTED>``, ``tool.output`` span
    events. Findings never hold values."""
    findings: list[Finding] = []
    for location, node in _walk(doc):
        if not isinstance(node, dict):
            continue
        key, inner = node.get("key"), node.get("value")
        if isinstance(key, str) and isinstance(inner, dict):
            if key in CONTENT_ATTRIBUTES and not _is_blank(inner):
                findings.append(Finding(f"content attribute {key}", location))
            if key == "event.name":
                events = (_content_event(name) for name in _string_values(inner))
                findings += [Finding(f"content event {e}", location) for e in events if e]
        if (event := _content_event(node.get("eventName"))) is not None:
            findings.append(Finding(f"content event {event}", f"{location}.eventName"))
        body = node.get("body")
        if isinstance(body, dict) and (event := _content_event(body.get("stringValue"))):
            findings.append(Finding(f"content event {event}", f"{location}.body"))
        span_events = node.get("events")
        for index, item in enumerate(span_events if isinstance(span_events, list) else []):
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and name in CONTENT_SPAN_EVENTS:
                where = f"{location}.events[{index}]"
                findings.append(Finding(f"content span event {name}", where))
    return findings


# Redaction of whole payloads.


class Redactor:
    """Redact strings, hook payloads and OTLP export requests with stable fakes.

    Each string goes through, in order: the local paths, identity values, distinctive
    names, plugin ID hashes and client request IDs as whole words; plain-word names where
    used as names; request and tool use IDs; the username, hostname, ``--forbid`` texts and
    email domains (the whole token around them, except in dict keys); and the safety net.
    Nothing cuts into an ID. A name field (``plugin.name``, ``marketplace.name``, ...)
    holding a collected name of its own kind gets that kind's placeholder, so a
    marketplace named like a plugin still becomes ``marketplace-N``. Raises StructureError
    if two keys of an object would become equal, and LiteralConflictError for a value that
    would match inside the placeholders.
    """

    def __init__(self, ctx: LocalContext, names: Names) -> None:
        literals = _literals(ctx, names)
        self._field_names = {
            (literal.label.rsplit(" ", 1)[0], literal.value.casefold()): literal.fake
            for literal in literals
            if literal.label.startswith(_NAME_LABELS)
        }
        structured = [(real, fake, False) for real, fake in path_replacements(ctx)]
        structured += [
            (literal.value, literal.fake, False)
            for literal in literals
            if literal.mode == "word"
            and not _is_loose(literal.label)
            and not literal.label.startswith(_PATH_LABELS)
        ]
        self._structured = _Replacer(structured)
        self._loose = _Replacer(
            (literal.value, literal.fake, literal.mode == "anywhere")
            for literal in literals
            if _is_loose(literal.label)
        )
        self._plain = _PlainNames(
            {
                literal.value.casefold(): (literal.label, literal.fake)
                for literal in literals
                if literal.mode == "name"
            }
        )
        self._request_ids = StableNames("req_EXAMPLE{:04d}")
        self._tool_use_ids = StableNames(TOOL_USE_ID_TEMPLATE)

    def ids(self, value: str) -> str:
        """``value`` with only its request and tool use IDs turned into their fakes."""
        value = replace_request_ids(value, self._request_ids)
        return replace_tool_use_ids(value, self._tool_use_ids)

    def text(self, value: str, *, key: bool = False) -> str:
        """Redact one string (``key``: a dict key, never matched inside longer words).
        INTERNAL_SUMMARY stays as it is."""
        if value == INTERNAL_SUMMARY:
            return value
        value = self._structured(value)
        value = self._plain.replace(value)
        value = self.ids(value)
        value = self._loose(value, tokens=not key)
        return scrub_safety_net(value)

    def value(self, value: JSON) -> JSON:
        """Redact every string (keys included) in a JSON value."""
        return self._value(value, "$", None)

    def _value(self, value: JSON, location: str, parent: str | None) -> JSON:
        if isinstance(value, str):
            kind = NAME_FIELD_KINDS.get(parent or "")
            if kind is not None:
                fake = self._field_names.get((kind, value.casefold()))
                if fake is not None:
                    return fake
                value = self._plain.field(value)
            return self.text(value)
        if isinstance(value, list):
            return [
                self._value(item, f"{location}[{index}]", parent)
                for index, item in enumerate(value)
            ]
        if isinstance(value, dict):
            result: JSONObject = {}
            for key, item in value.items():
                new_key = self.text(key, key=True)
                if new_key in result:
                    raise StructureError(
                        f"redaction would make two keys of the object at {location} equal"
                    )
                child = _child_field(value, key, parent)
                result[new_key] = self._value(item, f"{location}.{new_key}", child)
            return result
        return value

    def otlp(self, doc: JSON) -> JSON:
        """Redact an OTLP export request, identity attributes first."""
        return self.value(redact_identity_attributes(doc))


def _structure_changes(
    original: JSON,
    redacted: JSON,
    protected: frozenset[str],
    id_fakes: Callable[[str], str],
    location: str = "$",
    parent: str | None = None,
) -> Iterator[tuple[str, str, str]]:
    """``(what, where, original text)`` for every key or protected field that changed into
    anything but ``id_fakes`` of it (the original with its IDs turned into their fakes)."""
    if isinstance(original, str):
        changed = original != redacted and redacted != id_fakes(original)
        if changed and parent is not None and parent in protected:
            yield parent, location, original
    elif isinstance(original, list) and isinstance(redacted, list):
        for index, (old, new) in enumerate(zip(original, redacted, strict=True)):
            where = f"{location}[{index}]"
            yield from _structure_changes(old, new, protected, id_fakes, where, parent)
    elif isinstance(original, dict) and isinstance(redacted, dict):
        for (key, old), (new_key, new) in zip(original.items(), redacted.items(), strict=True):
            if key != new_key and new_key != id_fakes(key):
                yield "a key", f"{location} (key)", key
            child = _child_field(original, key, parent)
            where = f"{location}.{new_key}"
            yield from _structure_changes(old, new, protected, id_fakes, where, child)


# Summaries written by internal agents.


def started_agent_types(payloads: Iterable[JSON]) -> frozenset[str]:
    """The subagent types whose last message a session's SubagentStop may keep: each
    ``subagent_type`` the session asked for in a PreToolUse of the Agent tool, and
    KNOWN_AGENT_TYPES."""
    started = set(KNOWN_AGENT_TYPES)
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
            continue
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") == "Agent" and isinstance(tool_input, dict):
            subagent_type = tool_input.get("subagent_type")
            if isinstance(subagent_type, str) and subagent_type:
                started.add(subagent_type)
    return frozenset(started)


def internal_summary_keys(
    payload: Mapping[str, JSON], agent_types: Collection[str] = KNOWN_AGENT_TYPES
) -> list[str]:
    """The keys of a hook payload that hold a summary written by an internal agent.

    PostCompact's ``compact_summary``, and the ``last_assistant_message`` of a SubagentStop
    whose ``agent_type`` is not in ``agent_types`` (started_agent_types of the session):
    internal agents such as compaction and prompt suggestion, which run with an empty or
    unknown type, summarize the whole context, the user's own instructions (CLAUDE.md,
    memory) included, so no redaction of names and paths can make them safe. The rule
    fails closed: a missing or unknown type counts as internal.
    """
    event, agent_type = payload.get("hook_event_name"), payload.get("agent_type")
    keys: list[str] = []
    if event == "PostCompact":
        keys.append("compact_summary")
    if event == "SubagentStop" and not (isinstance(agent_type, str) and agent_type in agent_types):
        keys.append("last_assistant_message")
    return [key for key in keys if key in payload]


def drop_internal_summaries(
    payload: JSONObject, agent_types: Collection[str] = KNOWN_AGENT_TYPES
) -> JSONObject:
    """``payload`` with each internal summary (internal_summary_keys) replaced by
    INTERNAL_SUMMARY. The keys and their order stay; a null summary stays null."""
    keys = internal_summary_keys(payload, agent_types)
    return {
        key: INTERNAL_SUMMARY if key in keys and value is not None else value
        for key, value in payload.items()
    }


def _drop_line_summaries(line: JSONObject, agent_types: Collection[str]) -> JSONObject:
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return line
    return {**line, "payload": drop_internal_summaries(payload, agent_types)}


# Selection.


def _objects(value: JSON) -> list[JSONObject]:
    """The objects of a JSON list (anything else: none)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_attribute(attributes: JSON, key: str) -> str | None:
    for item in _objects(attributes):
        if item.get("key") == key:
            value = item.get("value")
            text = value.get("stringValue") if isinstance(value, dict) else None
            return text if isinstance(text, str) else None
    return None


def _session_of(item: JSONObject, fallback: str | None) -> str | None:
    return _string_attribute(item.get("attributes"), "session.id") or fallback


def _keep(
    item: JSONObject, item_key: str, session_id: str, fallback: str | None
) -> JSONObject | None:
    """``item`` restricted to ``session_id``, or None if nothing of it is left."""
    if item_key != "metrics":
        return item if _session_of(item, fallback) == session_id else None
    for kind in _METRIC_DATA:
        data = item.get(kind)
        if isinstance(data, dict):
            points: list[JSON] = [
                point
                for point in _objects(data.get("dataPoints"))
                if _session_of(point, fallback) == session_id
            ]
            return {**item, kind: {**data, "dataPoints": points}} if points else None
    return None


def filter_otlp(doc: JSON, session_id: str) -> JSONObject | None:
    """Keep one session's data points, log records and spans; drop emptied containers."""
    layout = None
    if isinstance(doc, dict):
        layout = next((entry for entry in _OTLP_LAYOUT.items() if entry[0] in doc), None)
    if not isinstance(doc, dict) or layout is None:
        raise ValueError("not an OTLP export request (no resourceMetrics/Logs/Spans)")
    resource_key, (scope_key, item_key) = layout
    resources: list[JSON] = []
    for resource in _objects(doc[resource_key]):
        header = resource.get("resource")
        attributes = header.get("attributes") if isinstance(header, dict) else None
        fallback = _string_attribute(attributes, "session.id")
        scopes: list[JSON] = []
        for scope in _objects(resource.get(scope_key)):
            items: list[JSON] = [
                kept
                for item in _objects(scope.get(item_key))
                if (kept := _keep(item, item_key, session_id, fallback)) is not None
            ]
            if items:
                scopes.append({**scope, item_key: items})
        if scopes:
            resources.append({**resource, scope_key: scopes})
    return {**doc, resource_key: resources} if resources else None


def _transport(path: str) -> str:
    transport = path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if transport not in ("http", "command"):
        raise ValueError(f"unknown hook transport for request path {path!r}")
    return transport


def hook_records(records: Iterable[JSON], session_id: str) -> list[JSONObject]:
    """One session's hook requests in arrival order, as fixture lines (not yet redacted)."""
    timed: list[tuple[datetime, str, JSONObject]] = []
    for record in _objects(list(records)):
        body = record.get("body")
        if not isinstance(body, dict) or body.get("session_id") != session_id:
            continue
        received_at, path = record.get("received_at"), record.get("path")
        if not isinstance(received_at, str) or not isinstance(path, str):
            raise ValueError("hook record without received_at or path")
        timed.append((datetime.fromisoformat(received_at), path, body))
    timed.sort(key=lambda entry: entry[0])
    if not timed:
        return []
    start = timed[0][0]
    return [
        {
            "offset_ms": ((at - start) // timedelta(microseconds=1) + 500) // 1000,
            "transport": _transport(path),
            "payload": body,
        }
        for at, path, body in timed
    ]


# Pipeline and CLI.


@dataclass
class _Scenario:
    name: str
    session_id: str
    description: str
    hooks: list[JSONObject] = field(default_factory=list)
    otlp: dict[str, list[JSON]] = field(default_factory=dict)


def _read_json(path: Path) -> JSON:
    value: JSON = json.loads(path.read_text(encoding="utf-8"))
    return value


def _read_jsonl(path: Path) -> list[JSON]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _select(raw: Path, entries: JSON, otlp: Mapping[str, list[JSON]]) -> list[_Scenario]:
    """Each scenario of the manifest with its session's hook lines and OTLP documents."""
    if not isinstance(entries, list):
        raise ValueError("scenarios.json: 'scenarios' must be a list")
    hook_files: dict[str, list[JSON]] = {}
    selected: list[_Scenario] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("scenarios.json: every scenario must be an object")
        name, session_id = entry.get("name"), entry.get("session_id")
        description, hooks = entry.get("description", ""), entry.get("hooks")
        if not isinstance(name, str) or not _SCENARIO_NAME.fullmatch(name):
            raise ValueError(f"invalid scenario name {name!r}: use a-z, 0-9 and dashes")
        if any(scenario.name == name for scenario in selected):
            raise ValueError(f"duplicate scenario name {name!r}")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"scenario {name}: session_id must be a non-empty string")
        if not isinstance(description, str) or not isinstance(hooks, str | None):
            raise ValueError(f"scenario {name}: description and hooks must be strings")
        scenario = _Scenario(name, session_id, description)
        if hooks:
            if hooks not in hook_files:
                hook_files[hooks] = _read_jsonl(raw / hooks)
            scenario.hooks = hook_records(hook_files[hooks], session_id)
            if not scenario.hooks:
                raise ValueError(f"scenario {name}: no hook records for its session")
        if entry.get("otlp"):
            for signal in SIGNALS:
                docs: list[JSON] = [
                    kept
                    for doc in otlp[signal]
                    if (kept := filter_otlp(doc, session_id)) is not None
                ]
                if docs:
                    scenario.otlp[signal] = docs
            if not scenario.otlp:
                raise ValueError(f"scenario {name}: no telemetry for its session")
        selected.append(scenario)
    return selected


def _telemetry_env(raw: Path) -> JSON:
    """The ``env`` blocks of the settings files in ``raw`` (make_settings.py output): one
    object if they agree, a list if not, None without settings files."""
    envs: dict[str, JSON] = {}
    for path in sorted(raw.glob("settings-*.json")):
        settings = _read_json(path)
        env = settings.get("env") if isinstance(settings, dict) else None
        if isinstance(env, dict):
            envs.setdefault(json.dumps(env, sort_keys=True), env)
    values = list(envs.values())
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _report(problems: list[str]) -> str:
    shown = "\n  ".join(problems[:20])
    more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
    return f"\n  {shown}{more}"


def _problems(
    outputs: Mapping[str, list[JSON]], find: Callable[[JSON], list[Finding]]
) -> list[str]:
    return [
        f"{name} record {number}: {finding.kind} at {finding.location}"
        for name, records in outputs.items()
        for number, record in enumerate(records, start=1)
        for finding in find(record)
    ]


def _check_content(scenarios: Iterable[_Scenario]) -> None:
    outputs = {
        f"otlp/{scenario.name}/{signal}.jsonl": docs
        for scenario in scenarios
        for signal, docs in scenario.otlp.items()
    }
    problems = _problems(outputs, find_content)
    if problems:
        raise ContentError(
            "the capture holds content-bearing telemetry, nothing was written. Capture again"
            " with the OTEL_LOG_* flags off (make_settings.py and run_claude.sh do that), or"
            f" pass --allow-content if you really mean to keep it:{_report(problems)}"
        )


def _check_structure(
    pairs: Mapping[str, list[tuple[JSON, JSON, bool]]],
    literals: Mapping[str, str],
    id_fakes: Callable[[str], str],
) -> None:
    detector = _Detector(literals)
    problems: list[str] = []
    for name, records in pairs.items():
        for number, (original, redacted, otlp) in enumerate(records, start=1):
            protected = _PROTECTED_FIELDS | _PROTECTED_OTLP_FIELDS if otlp else _PROTECTED_FIELDS
            changes = _structure_changes(original, redacted, protected, id_fakes)
            for what, where, text in changes:
                labels = ", ".join(detector.kinds(text)) or "redaction"
                problems.append(f"{name} record {number}: changed {what} at {where} ({labels})")
    if problems:
        raise StructureError(
            "redaction would change keys, names or IDs, nothing was written. A value to"
            " remove is part of Claude Code's own names: pass --user '' or --host '' for the"
            " username or hostname, or leave out that --forbid text, and add distinctive"
            f" forms of your name with --forbid:{_report(problems)}"
        )


def _line_isolation_problems(line: JSON) -> list[Finding]:
    payload = line.get("payload") if isinstance(line, dict) else None
    if not isinstance(payload, dict):
        return []
    return [
        Finding(found.kind, "$.payload" + found.location.removeprefix("$"))
        for found in find_isolation_problems(payload)
    ]


def _check_isolation(outputs: Mapping[str, list[JSON]]) -> None:
    hooks = {name: records for name, records in outputs.items() if name.startswith("hooks/")}
    problems = _problems(hooks, _line_isolation_problems)
    if problems:
        raise IsolationError(
            "the capture was not isolated from your own Claude Code setup, nothing was"
            " written: Claude Code loaded instructions from outside the capture workspace"
            " (a CLAUDE.md, rules or memory of yours). Capture again through run_claude.sh"
            " with its default isolation (CAPTURE_ISOLATION=1), in a workspace root with no"
            " CLAUDE.md, CLAUDE.local.md or .claude/ in it or above it, and pass that root"
            f" as --workspace-root:{_report(problems)}"
        )


def _check(outputs: Mapping[str, list[JSON]], literals: Mapping[str, str]) -> None:
    problems = _problems(outputs, lambda record: find_personal_data(record, literals))
    if problems:
        raise PersonalDataError(
            f"personal data left after redaction, nothing was written:{_report(problems)}"
        )


def _serialize(name: str, records: list[JSON]) -> str:
    if name.endswith(".jsonl"):
        return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    return json.dumps(records[0], ensure_ascii=False, indent=2) + "\n"


def _manifest_files(manifest: JSON) -> list[str] | None:
    """The fixture files listed in a manifest this tool wrote; None for anything else."""
    scenarios = manifest.get("scenarios") if isinstance(manifest, dict) else None
    if not isinstance(scenarios, list):
        return None
    files: list[str] = []
    for scenario in scenarios:
        listed = scenario.get("files") if isinstance(scenario, dict) else None
        if not isinstance(listed, list):
            return None
        for name in listed:
            if not isinstance(name, str) or not _FIXTURE_FILE.fullmatch(name):
                return None
            files.append(name)
    return files


def _previous_fixtures(dest: Path) -> list[str]:
    """Files the previous run wrote under ``dest``, from its manifest.

    Raises DestinationError if ``dest`` has ``hooks/`` or ``otlp/`` but no manifest from
    this tool, so a wrong ``--dest`` (``.git``, ``~/.claude``) is never touched.
    """
    manifest_path = dest / "manifest.json"
    if not manifest_path.exists():
        if (dest / "hooks").exists() or (dest / "otlp").exists():
            raise DestinationError(
                f"{dest} has hooks/ or otlp/ but no manifest.json written by this tool;"
                " refusing to write there (check --dest)"
            )
        return []
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError):
        manifest = None
    files = _manifest_files(manifest)
    if files is None:
        raise DestinationError(
            f"{manifest_path} is not a fixtures manifest written by this tool;"
            " refusing to write there (check --dest)"
        )
    return files


def _remove_previous(dest: Path, files: Iterable[str]) -> None:
    """Delete the previous run's files, then the directories they leave empty."""
    for name in files:
        path = dest / name
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != dest and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    (dest / "manifest.json").unlink(missing_ok=True)


_MIN_WORKSPACE_ROOT_DEPTH: Final = 2


def workspace_root_problem(ctx: LocalContext) -> str | None:
    """Why ``ctx.workspace_root`` cannot be mapped to the fake workspace (None if it can).

    A relative path, a drive or filesystem root, a single top-level directory, or the
    home directory or one above it would pass everything below it off as the capture
    workspace, and with it the isolation check."""
    raw = ctx.workspace_root
    if not raw:
        return None
    if not (_is_windows_path(raw) or raw.startswith("/")):
        return "--workspace-root must be an absolute path"
    root = _normalize_path(raw)
    if len([part for part in _SEPARATORS.split(root)[1:] if part]) < _MIN_WORKSPACE_ROOT_DEPTH:
        return (
            "--workspace-root must be at least two directories below a drive or the"
            " filesystem root, such as C:\\ccobs-capture\\<run> or /tmp/ccobs-capture/<run>"
        )
    folded = root.casefold()
    for home in _home_spellings(ctx):
        separator = "\\" if _is_windows_path(home) else "/"
        if home.casefold() == folded or home.casefold().startswith(folded + separator):
            return "--workspace-root must not be the home directory or a directory above it"
    return None


def sanitize(
    raw: Path,
    dest: Path,
    ctx: LocalContext,
    *,
    allow_content: bool = False,
    allow_unisolated: bool = False,
) -> list[str]:
    """Write sanitized fixtures for every scenario in ``raw/scenarios.json`` under ``dest``.

    Replaces the files listed in ``dest/manifest.json`` (and the manifest) and returns the
    written paths, relative to ``dest``. Internal summaries are replaced before anything
    else (drop_internal_summaries). ``scenarios.json`` must say ``"isolated": true``,
    which goes into the manifest, and every InstructionsLoaded must come from the
    capture workspace, unless ``allow_unisolated`` (the manifest then records false).
    Raises WorkspaceRootError, IsolationError, ContentError (unless ``allow_content``),
    StructureError, PersonalDataError, LiteralConflictError or DestinationError, and
    writes nothing, if a check fails.
    """
    if (problem := workspace_root_problem(ctx)) is not None:
        raise WorkspaceRootError(problem)
    manifest = _read_json(raw / "scenarios.json")
    if not isinstance(manifest, dict):
        raise ValueError("scenarios.json must hold an object")
    isolated = manifest.get("isolated") is True
    if not isolated and not allow_unisolated:
        raise IsolationError(
            'scenarios.json does not say the capture was isolated ("isolated": true),'
            " nothing was written. Capture through run_headless.sh and run_claude.sh with"
            " their default isolation (CAPTURE_ISOLATION=1, the isolation column of"
            ' out/headless-sessions.tsv), then set "isolated": true in scenarios.json; for'
            " an exploratory capture, pass --allow-unisolated."
        )
    otlp = {signal: _read_jsonl(raw / f"otlp-{signal}.jsonl") for signal in SIGNALS}
    scenarios = _select(raw, manifest.get("scenarios"), otlp)
    for scenario in scenarios:
        agent_types = started_agent_types(line.get("payload") for line in scenario.hooks)
        scenario.hooks = [_drop_line_summaries(line, agent_types) for line in scenario.hooks]
    if not allow_content:
        _check_content(scenarios)
    names = collect_names(
        (doc for docs in otlp.values() for doc in docs),
        (line["payload"] for scenario in scenarios for line in scenario.hooks),
    )
    redactor = Redactor(ctx, names)
    outputs: dict[str, list[JSON]] = {}
    pairs: dict[str, list[tuple[JSON, JSON, bool]]] = {}
    entries: list[JSON] = []
    for scenario in scenarios:
        files: list[JSON] = []
        if scenario.hooks:
            name = f"hooks/{scenario.name}.jsonl"
            files.append(name)
            redacted = [
                {**line, "payload": redactor.value(line["payload"])} for line in scenario.hooks
            ]
            outputs[name] = list(redacted)
            pairs[name] = [
                (line["payload"], new["payload"], False)
                for line, new in zip(scenario.hooks, redacted, strict=True)
            ]
        for signal, docs in scenario.otlp.items():
            name = f"otlp/{scenario.name}/{signal}.jsonl"
            files.append(name)
            outputs[name] = [redactor.otlp(doc) for doc in docs]
            pairs[name] = [(doc, new, True) for doc, new in zip(docs, outputs[name], strict=True)]
        entries.append(
            {
                "name": scenario.name,
                "session_id": scenario.session_id,
                "description": redactor.text(scenario.description),
                "files": files,
            }
        )
    header = {key: redactor.value(value) for key, value in manifest.items() if key != "scenarios"}
    header["isolated"] = isolated and not allow_unisolated
    if "telemetry_env" not in header and (env := _telemetry_env(raw)) is not None:
        header["telemetry_env"] = redactor.value(env)
    outputs["manifest.json"] = [{**header, "scenarios": entries}]

    literals = guard_literals(ctx, names)
    _check_structure(pairs, literals, redactor.ids)
    if not allow_unisolated:
        _check_isolation(outputs)
    _check(outputs, literals)
    _remove_previous(dest, _previous_fixtures(dest))
    for name, records in outputs.items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize(name, records), encoding="utf-8", newline="\n")
    return list(outputs)


def build_parser() -> argparse.ArgumentParser:
    """Command-line parser for ``python -m tools.capture.sanitize``."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.capture.sanitize",
        description="Turn raw capture output into sanitized test fixtures.",
    )
    parser.add_argument("--raw", type=Path, required=True, help="raw capture dir")
    parser.add_argument("--dest", type=Path, required=True, help="fixtures dir to write")
    parser.add_argument("--workspace-root", help="scratch directory the capture sessions ran in")
    parser.add_argument("--home", help="home directory to remove (default: this machine's)")
    parser.add_argument(
        "--user", help="username to remove (default: this machine's; '' to not look for it)"
    )
    parser.add_argument(
        "--host", help="hostname to remove (default: this machine's; '' to not look for it)"
    )
    parser.add_argument(
        "--home-alias",
        action="append",
        default=[],
        metavar="PATH",
        help="another spelling of the home directory, such as a Windows 8.3 short name"
        " (repeatable; on Windows the short name is added automatically)",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="TEXT",
        help="text to redact and guard against, such as your name or employer (repeatable;"
        " git user.name and user.email are added automatically)",
    )
    parser.add_argument(
        "--allow-content",
        action="store_true",
        help="keep content-bearing telemetry (prompts, tool content, API bodies) instead of"
        " refusing it",
    )
    parser.add_argument(
        "--allow-unisolated",
        action="store_true",
        help="accept a capture that scenarios.json does not mark as isolated, and instructions"
        " loaded from outside the workspace (exploratory use; the manifest records"
        ' "isolated": false)',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sanitizer; returns the process exit code."""
    args = build_parser().parse_args(argv)
    home = args.home or str(Path.home())
    short = short_path(home)
    identity = git_identity()
    kept = [value for value in identity if not reserved_conflict(value)]
    if len(kept) < len(identity):
        print(
            "note: a git user.name or user.email value would match inside the placeholders,"
            " so it is not looked for; add distinctive forms with --forbid",
            file=sys.stderr,
        )
    ctx = LocalContext(
        home=home,
        user=args.user if args.user is not None else getpass.getuser(),
        host=args.host if args.host is not None else socket.gethostname(),
        workspace_root=args.workspace_root,
        home_aliases=_unique([*args.home_alias, *([short] if short else [])]),
        forbid=_unique([*args.forbid, *kept]),
    )
    try:
        written = sanitize(
            args.raw,
            args.dest,
            ctx,
            allow_content=args.allow_content,
            allow_unisolated=args.allow_unisolated,
        )
    except (
        ContentError,
        StructureError,
        PersonalDataError,
        LiteralConflictError,
        DestinationError,
        IsolationError,
        WorkspaceRootError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(written)} files under {args.dest}")
    for name in written:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
