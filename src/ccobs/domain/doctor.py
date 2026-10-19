"""The rules of ``ccobs doctor`` (ADR 0039): pure checks over data the CLI collected.

Each rule takes parsed data (settings objects, command output, HTTP answers, file
modes) and returns a ``Check``. The CLI (``ccobs.cli.doctor``) does every read, run and
request, then prints the checks grouped by section.

Privacy: a check never quotes a value read from a settings file, the environment or
``.env``. It names keys, files and sources; the only values it shows are the ones
cc-observatory itself writes (the hook URL, the OTLP endpoint), ports, and versions.

Claude Code facts used here come from its settings reference
(https://code.claude.com/docs/en/settings-reference), managed settings
(https://code.claude.com/docs/en/managed-settings) and hooks
(https://code.claude.com/docs/en/hooks) pages:

- a settings ``env`` value overwrites the same variable exported in the shell, except
  when the desktop app (or a self-hosted runner) builds the launch environment;
- project and local settings cannot set the variables that turn telemetry on, choose
  where it goes or export content (Claude Code 2.1.282 and later drops them); only
  ``none`` for an exporter selector (and an off value for some content flags) still
  applies from them;
- ``allowedHttpHookUrls`` arrays merge across every settings file, ``*`` is a
  wildcard, and an empty array blocks every HTTP hook;
- the HKCU registry value is user-writable and applies only when no admin source is
  present, so it is warn-level evidence here.

A JSON ``null`` counts as "not set" everywhere, like an absent key.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal, cast

from ccobs.domain.diagnostics import UncountedHookFailures, parse_diagnostics
from ccobs.domain.durations import parse_duration
from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.settings_merge import is_ccobs_handler
from ccobs.domain.telemetry import HOST_VALUE_PATTERN
from ccobs.domain.telemetry_settings import (
    CONTENT_VARIABLES,
    PER_SIGNAL_ENDPOINT_VARIABLES,
    SESSION_ID_VARIABLE,
    TRACES_VARIABLES,
    telemetry_env,
)

__all__ = [
    "DEFAULT_APP_PORT",
    "DEFAULT_GRAFANA_PORT",
    "DEFAULT_OTLP_PORT",
    "ENV_FILE_KEYS",
    "MIN_BUILDX_VERSION",
    "MIN_CLAUDE_VERSION",
    "MIN_COMPOSE_VERSION",
    "MIN_ENGINE_VERSION",
    "SAFE_LINUX_ENGINE_VERSION",
    "STATUSES",
    "Check",
    "DockerVersions",
    "EnvSource",
    "HttpAnswer",
    "SettingsSource",
    "SourceLevel",
    "StatEntry",
    "Status",
    "check_app",
    "check_claude_version",
    "check_content_variables",
    "check_curl",
    "check_diagnostics",
    "check_disable_all_hooks",
    "check_docker",
    "check_env_file",
    "check_grafana",
    "check_hook_endpoint",
    "check_hooks",
    "check_http_hook_allowlist",
    "check_managed_env",
    "check_managed_hooks_only",
    "check_otlp",
    "check_per_signal_endpoints",
    "check_project_telemetry_off",
    "check_session_id",
    "check_settings_file",
    "check_settings_sources",
    "check_shell_overrides",
    "check_stack_readable",
    "check_telemetry_env",
    "env_of",
    "exit_code",
    "hook_url",
    "is_ccobs_compose",
    "otlp_endpoint",
    "parse_env_file",
    "summary",
]

type Status = Literal["ok", "warn", "fail", "info"]
STATUSES: Final[tuple[Status, ...]] = ("ok", "warn", "fail", "info")

# The stack's port block (ADR 0018); every URL is on IPv4 loopback.
DEFAULT_APP_PORT: Final = 4300
DEFAULT_OTLP_PORT: Final = 4318
DEFAULT_GRAFANA_PORT: Final = 4330
_HOST: Final = "127.0.0.1"

# ADR 0012 (the contract was observed on 2.1.296) and ADR 0020 (minimum Docker).
MIN_CLAUDE_VERSION: Final = (2, 1, 296)
MIN_ENGINE_VERSION: Final = (25, 0, 0)
MIN_COMPOSE_VERSION: Final = (2, 20, 2)
MIN_BUILDX_VERSION: Final = (0, 17, 0)
# Engines before 28.0 let hosts on the same L2 segment reach ports published to
# localhost on Linux (ADR 0018).
SAFE_LINUX_ENGINE_VERSION: Final = (28, 0, 0)

_TEMPORALITY: Final = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
_ENABLE: Final = "CLAUDE_CODE_ENABLE_TELEMETRY"
_ENDPOINT: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
_PROTOCOL: Final = "OTEL_EXPORTER_OTLP_PROTOCOL"
_HTTP_PROTOBUF: Final = "http/protobuf"
_EXPORTERS: Final = ("OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER")
_OTLP: Final = "otlp"
_BETA_ENDPOINT: Final = "BETA_TRACING_ENDPOINT"
_OFF_VALUES: Final = frozenset({"", "0", "false", "no", "off"})
_DISABLE_ALL_HOOKS: Final = "disableAllHooks"
_MANAGED_HOOKS_ONLY: Final = "allowManagedHooksOnly"
_ALLOWLIST: Final = "allowedHttpHookUrls"

_VERSION: Final = re.compile(r"(?<![0-9.])v?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")
_HANDLER_PORT: Final = re.compile(r"http://127\.0\.0\.1(?::([0-9]+))?/")
_COMPOSE_NAME: Final = re.compile(r"""name:[ \t]*(["']?)cc-observatory\1[ \t]*(?:#.*)?""")
# An allowlist pattern's scheme and host, which match case-insensitively.
_PATTERN_HOST: Final = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<host>[^/:?#]*)")
# An inline comment after an unquoted .env value: a blank, then '#'.
_ENV_COMMENT: Final = re.compile(r"\s#")
_DAY: Final = timedelta(days=1)

_RETENTION_KEY: Final = "CCOBS_LOKI_RETENTION"
_HOST_LABEL_KEY: Final = "CCOBS_HOST_LABEL"
_PORT_KEYS: Final = (
    ("CCOBS_APP_PORT", "--app-port", DEFAULT_APP_PORT),
    ("CCOBS_OTLP_HTTP_PORT", "--otlp-port", DEFAULT_OTLP_PORT),
    ("CCOBS_GRAFANA_PORT", "--grafana-port", DEFAULT_GRAFANA_PORT),
)
# The only keys doctor ever reads from a clone's .env.
ENV_FILE_KEYS: Final = (_RETENTION_KEY, _HOST_LABEL_KEY, *(key for key, _, _ in _PORT_KEYS))
_MAX_PORT: Final = 65535
# stat.S_IROTH and stat.S_IXOTH, without importing a module the domain does not need.
_OTHERS_READ: Final = 0o004
_OTHERS_SEARCH: Final = 0o001
_MAX_LISTED: Final = 10

_SERVER_MANAGED_NOTE: Final = (
    "Server-managed settings (from claude.ai or a gateway) are not visible here; "
    "/status in Claude Code shows the sources in effect."
)
_SETUP: Final = "ccobs setup"


@dataclass(frozen=True)
class Check:
    """One result: ``id`` is stable (docs and tests use it); ``detail`` may hold several
    lines; ``fix`` is what to do, when there is something to do."""

    id: str
    status: Status
    title: str
    detail: str
    fix: str | None = None


def summary(checks: Iterable[Check]) -> dict[str, int]:
    """How many checks have each status, every status listed."""
    counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
    for check in checks:
        counts[check.status] += 1
    return counts


def exit_code(checks: Iterable[Check]) -> int:
    """1 when any check failed, else 0 (warnings and notes do not fail doctor)."""
    return 1 if any(check.status == "fail" for check in checks) else 0


def hook_url(app_port: int) -> str:
    return f"http://{_HOST}:{app_port}{HOOK_PATH}"


def otlp_endpoint(otlp_port: int) -> str:
    return f"http://{_HOST}:{otlp_port}"


def _setup_command(*, app_port: int = DEFAULT_APP_PORT, otlp_port: int = DEFAULT_OTLP_PORT) -> str:
    flags = ""
    if app_port != DEFAULT_APP_PORT:
        flags += f" --app-port {app_port}"
    if otlp_port != DEFAULT_OTLP_PORT:
        flags += f" --otlp-port {otlp_port}"
    return _SETUP + flags


def _worst(statuses: Iterable[Status], default: Status = "ok") -> Status:
    found = set(statuses)
    for status in ("fail", "warn", "info"):
        if status in found:
            return cast(Status, status)
    return default


def _listed(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_LISTED])
    more = len(names) - _MAX_LISTED
    return shown + (f" and {more} more" if more > 0 else "")


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _parse_version(text: str, *, patch_required: bool) -> tuple[int, int, int] | None:
    match = _VERSION.search(text)
    if match is None or (patch_required and match[3] is None):
        return None
    return int(match[1]), int(match[2]), int(match[3] or 0)


# Claude Code and the host.


def check_curl(path: str | None) -> Check:
    """``path`` is where ``curl`` is on PATH, or None."""
    if path is None:
        return Check(
            "curl",
            "warn",
            "curl is not on PATH",
            "Only the async SessionStart hook runs curl; without it the app learns about a "
            "session from its next event.",
            "install curl (Windows 10 and 11 and macOS ship it)",
        )
    return Check("curl", "ok", "curl is on PATH", path)


def check_claude_version(output: str | None) -> Check:
    """``output`` is what ``claude --version`` printed, or None when it did not run."""
    if output is None:
        return Check(
            "claude-version",
            "warn",
            "Claude Code was not found",
            "`claude --version` did not run (not on PATH, or it failed).",
            "install Claude Code, or put `claude` on PATH",
        )
    version = _parse_version(output, patch_required=True)
    if version is None:
        return Check(
            "claude-version",
            "warn",
            "could not read Claude Code's version",
            "`claude --version` printed no X.Y.Z version.",
        )
    text = _version_text(version)
    if version < MIN_CLAUDE_VERSION:
        minimum = _version_text(MIN_CLAUDE_VERSION)
        return Check(
            "claude-version",
            "warn",
            f"Claude Code {text} is older than {minimum}",
            f"cc-observatory's hook and telemetry contract was recorded on {minimum}.",
            "update Claude Code (`claude update`)",
        )
    return Check("claude-version", "ok", f"Claude Code {text}", "")


@dataclass(frozen=True)
class DockerVersions:
    """What the Docker commands printed; None when a command did not run or failed."""

    found: bool  # docker is on PATH
    engine: str | None  # `docker version --format {{.Server.Version}}`: None, no daemon
    compose: str | None  # `docker compose version --short`
    buildx: str | None  # `docker buildx version`


def _docker_part(
    name: str,
    output: str | None,
    minimum: tuple[int, int, int],
    *,
    missing: tuple[Status, str],
) -> tuple[Status, str, tuple[int, int, int] | None]:
    if output is None:
        return missing[0], missing[1], None
    version = _parse_version(output, patch_required=False)
    if version is None:
        return "warn", f"could not read the {name} version", None
    text = _version_text(version)
    if version < minimum:
        return "fail", f"{name} {text} is older than {_version_text(minimum)}", version
    return "ok", f"{name} {text}", version


def check_docker(versions: DockerVersions, *, linux: bool) -> Check:
    """ADR 0020's minimum versions (fail), and ADR 0018's Linux warning for engines
    older than 28.0. Docker missing or its daemon down is a warning: the stack checks
    tell whether the stack itself is up."""
    if not versions.found:
        return Check(
            "docker",
            "warn",
            "Docker was not found",
            "`docker` is not on PATH; the stack checks below tell whether the stack is up.",
            "install Docker (Docker Desktop, or Docker Engine with the Compose plugin)",
        )
    parts = [
        _docker_part(
            "Docker Engine",
            versions.engine,
            MIN_ENGINE_VERSION,
            missing=("warn", "the Docker daemon did not answer"),
        ),
        _docker_part(
            "Docker Compose",
            versions.compose,
            MIN_COMPOSE_VERSION,
            missing=("fail", "the Docker Compose plugin (`docker compose`) was not found"),
        ),
        _docker_part(
            "Buildx",
            versions.buildx,
            MIN_BUILDX_VERSION,
            missing=(
                "warn",
                "the Buildx plugin was not found (Compose falls back to the deprecated "
                "classic builder)",
            ),
        ),
    ]
    statuses: list[Status] = [status for status, _, _ in parts]
    lines = [text for _, text, _ in parts]
    engine = parts[0][2]
    if linux and engine is not None and MIN_ENGINE_VERSION <= engine < SAFE_LINUX_ENGINE_VERSION:
        statuses.append("warn")
        lines.append(
            "engines older than 28.0 let hosts on the same network segment reach ports "
            "published to localhost on Linux"
        )
    status = _worst(statuses)
    title = {
        "ok": "Docker meets the minimum versions",
        "warn": "Docker needs attention",
        "fail": "Docker is older than the stack needs",
    }[status]
    fix = None
    if status != "ok":
        fix = (
            "Docker Engine 25.0 or later (28.0 or later on Linux), Compose 2.20.2 or later "
            "and Buildx 0.17.0 or later"
        )
    return Check("docker", status, title, "\n".join(lines), fix)


# Settings.


def env_of(settings: Mapping[str, object] | None) -> Mapping[str, object]:
    """The ``env`` object of ``settings``; empty when there is none or it is not one."""
    if settings is None:
        return {}
    env = settings.get("env")
    return cast(Mapping[str, object], env) if isinstance(env, dict) else {}


def _skipped(check_id: str, title: str) -> Check:
    return Check(
        check_id, "info", title, "Skipped: the user settings file is missing or cannot be read."
    )


def check_settings_file(label: str, *, exists: bool, error: str | None) -> Check:
    """``label`` is the file's path; ``error`` the reader's message (never content)."""
    if error is not None:
        return Check(
            "settings-file",
            "fail",
            "Claude Code's user settings cannot be read",
            f"{label}: {error}",
            "fix the file, then run ccobs setup",
        )
    if not exists:
        return Check(
            "settings-file",
            "fail",
            "Claude Code's user settings do not exist",
            label,
            f"run {_SETUP}",
        )
    return Check("settings-file", "ok", "Claude Code's user settings", label)


def _identical(first: object, second: object) -> bool:
    # Equal and in the same key order ({"a": 1} == {"a": True} in Python).
    return json.dumps(first, default=str) == json.dumps(second, default=str)


def _handler_port(handler: Mapping[str, object]) -> int:
    url = handler.get("url")
    if not isinstance(url, str):
        url = cast(list[str], handler["args"])[-1]  # the curl form, checked by the caller
    match = _HANDLER_PORT.match(url)
    return int(match[1]) if match is not None and match[1] is not None else 80


def check_hooks(settings: Mapping[str, object] | None, *, app_port: int, windows: bool) -> Check:
    """Whether every registered event holds the group ``hook_settings`` writes for
    ``app_port``. ``settings`` is None when the user file could not be read."""
    if settings is None:
        return _skipped("hooks", "cc-observatory's hooks")
    expected = cast(
        dict[str, list[dict[str, object]]],
        hook_settings(hook_url(app_port), windows=windows)["hooks"],
    )
    raw = settings.get("hooks")
    block = cast(Mapping[str, object], raw) if isinstance(raw, dict) else {}
    present: set[str] = set()
    stale: set[str] = set()
    ports: set[int] = set()
    for event, groups in block.items():
        if not isinstance(groups, list):
            continue
        ours = expected.get(event, [])
        for group in groups:
            if any(_identical(group, written) for written in ours):
                present.add(event)
                continue
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for handler in handlers if isinstance(handlers, list) else []:
                if is_ccobs_handler(handler):
                    stale.add(event)
                    ports.add(_handler_port(handler))
    total = len(expected)
    absent = [event for event in expected if event not in present and event not in stale]
    other_ports = sorted(port for port in ports if port != app_port)
    notes: list[str] = []
    if stale:
        notes.append(
            "Handlers that cc-observatory wrote but that differ from this version's "
            f"(an older version or another port) in: {_listed(sorted(stale))}."
        )
    if other_ports:
        notes.append(
            f"Some handlers post to port {', '.join(map(str, other_ports))}, not --app-port "
            f"{app_port}."
        )
    if absent:
        return Check(
            "hooks",
            "fail",
            f"cc-observatory's hooks are missing for {len(absent)} of {total} events",
            "\n".join([f"Missing: {_listed(absent)}.", *notes]),
            f"run {_setup_command(app_port=app_port)}",
        )
    if notes:
        fix = f"re-run {_setup_command(app_port=app_port)}"
        if other_ports:
            fix += ", or pass the matching --app-port to doctor"
        return Check(
            "hooks", "warn", "some of cc-observatory's hooks are out of date", "\n".join(notes), fix
        )
    return Check(
        "hooks",
        "ok",
        f"cc-observatory's hooks are in place for all {total} events",
        hook_url(app_port),
    )


def check_telemetry_env(settings: Mapping[str, object] | None, *, otlp_port: int) -> Check:
    """Whether the user ``env`` holds each value ``telemetry_env`` writes. Values are
    never quoted: the env can hold API keys."""
    if settings is None:
        return _skipped("telemetry-env", "telemetry variables")
    endpoint = otlp_endpoint(otlp_port)
    expected = telemetry_env(endpoint)
    env = env_of(settings)
    lines: list[str] = []
    statuses: list[Status] = []
    replaced = False
    for key, value in expected.items():
        if env.get(key) is None:
            problem = "is missing"
        elif env[key] != value:
            problem = "holds another value"
            replaced = True
        else:
            continue
        if key == _TEMPORALITY:
            statuses.append("warn")
            lines.append(
                f"{key} {problem}: the stack expects cumulative (docs/stack.md, the delta limit)."
            )
        else:
            statuses.append("fail")
            lines.append(
                f"{key} {problem}" + (f" (expected {endpoint})" if key == _ENDPOINT else "")
            )
    status = _worst(statuses)
    if status == "ok":
        return Check("telemetry-env", "ok", "the telemetry variables point at the stack", endpoint)
    fix = f"run {_setup_command(otlp_port=otlp_port)}"
    if replaced:
        fix += " --force (ccobs uninstall puts your values back)"
    title = (
        "the telemetry variables do not point at the stack"
        if status == "fail"
        else "the metrics temporality is not cumulative"
    )
    return Check("telemetry-env", status, title, "\n".join(lines), fix)


@dataclass(frozen=True)
class EnvSource:
    """Environment variables from one place: ``label`` names it (a path, "your user
    settings", "your shell environment")."""

    label: str
    env: Mapping[str, object]


def _text(value: object) -> str:
    # A plist can hold dates and bytes, which JSON cannot.
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _set(env: Mapping[str, object], name: str) -> bool:
    """Whether ``env`` sets ``name``: present and not JSON null."""
    return env.get(name) is not None


def _value(env: Mapping[str, object], name: str) -> str:
    """``name``'s value as stripped text; call only when ``_set``."""
    return _text(env[name]).strip()


def _content_enabled(name: str, value: object) -> bool:
    text = _text(value).strip()
    if name == _BETA_ENDPOINT:
        return text != ""
    return text.lower() not in _OFF_VALUES


def check_content_variables(sources: Sequence[EnvSource]) -> Check:
    """A content capture flag set to an enabling value (ADR 0006): content capture is
    not supported yet, and the collector drops content-bearing telemetry anyway (ADR
    0021)."""
    found = [
        f"{name} in {source.label}"
        for source in sources
        for name in CONTENT_VARIABLES
        if _set(source.env, name) and _content_enabled(name, source.env[name])
    ]
    if not found:
        return Check("content-variables", "ok", "no content capture variable is enabled", "")
    return Check(
        "content-variables",
        "warn",
        "content capture variables are enabled",
        "\n".join(
            [
                *found,
                "cc-observatory does not support content capture yet; its collector drops "
                "content-bearing telemetry anyway.",
            ]
        ),
        "remove them, or set them to 0",
    )


def check_session_id(sources: Sequence[EnvSource]) -> Check:
    """``OTEL_METRICS_INCLUDE_SESSION_ID=false`` merges concurrent sessions' series
    (ADR 0016)."""
    found = [
        source.label
        for source in sources
        if _set(source.env, SESSION_ID_VARIABLE)
        and _value(source.env, SESSION_ID_VARIABLE).lower() == "false"
    ]
    if not found:
        return Check("session-id", "ok", f"{SESSION_ID_VARIABLE} is not turned off", "")
    return Check(
        "session-id",
        "warn",
        f"{SESSION_ID_VARIABLE} is false",
        f"Set in {_listed(found)}: the metrics of concurrent sessions cannot be told apart.",
        f"remove {SESSION_ID_VARIABLE}",
    )


def check_per_signal_endpoints(sources: Sequence[EnvSource]) -> Check:
    """Per-signal OTLP variables override the generic endpoint and protocol the block
    writes (ADR 0036)."""
    found = [
        f"{name} in {source.label}"
        for source in sources
        for name in PER_SIGNAL_ENDPOINT_VARIABLES
        if _set(source.env, name) and _value(source.env, name) != ""
    ]
    if not found:
        return Check("per-signal-endpoints", "ok", "no per-signal OTLP variable is set", "")
    return Check(
        "per-signal-endpoints",
        "warn",
        "per-signal OTLP variables are set",
        "\n".join(
            [*found, "They take precedence over the endpoint and protocol ccobs setup writes."]
        ),
        "remove them, unless they point at the stack on purpose",
    )


def check_shell_overrides(environ: Mapping[str, str], *, otlp_port: int) -> Check:
    """Telemetry variables exported in the shell: the settings ``env`` overwrites them,
    except when the desktop app builds the launch environment."""
    names = [
        name
        for name in (*telemetry_env(otlp_endpoint(otlp_port)), *TRACES_VARIABLES)
        if name in environ
    ]
    if not names:
        return Check("shell-overrides", "ok", "no telemetry variable is exported in the shell", "")
    return Check(
        "shell-overrides",
        "info",
        "telemetry variables are exported in your shell",
        "\n".join(
            [
                _listed(names),
                "Claude Code's settings env overwrites them, except when the Claude Desktop "
                "app (or a self-hosted runner) starts the session: its launch environment wins.",
            ]
        ),
    )


# Policies.

type SourceLevel = Literal["managed", "hkcu", "user", "project", "local"]


@dataclass(frozen=True)
class SettingsSource:
    """One settings document. ``level``: ``managed`` is an admin source (a managed
    settings file or drop-in, the HKLM registry value, the macOS plist); ``hkcu`` the
    user-writable registry value. ``data`` is None when it could not be read
    (``error`` says why, never quoting content)."""

    level: SourceLevel
    label: str
    data: Mapping[str, object] | None
    error: str | None = None


# Admin sources count fully; the user-writable HKCU value only as a warning.
_POLICY_LEVELS: Final[tuple[tuple[SourceLevel, Status], ...]] = (
    ("managed", "fail"),
    ("hkcu", "warn"),
)


def _with(sources: Sequence[SettingsSource], level: SourceLevel) -> list[SettingsSource]:
    return [source for source in sources if source.level == level and source.data is not None]


def _data(source: SettingsSource) -> Mapping[str, object]:
    return source.data if source.data is not None else {}


def check_settings_sources(sources: Sequence[SettingsSource]) -> Check:
    """Which managed documents doctor read and which it could not; always a note."""
    managed = [source for source in sources if source.level in ("managed", "hkcu")]
    read = [source.label for source in managed if source.data is not None]
    unreadable = [
        f"could not read {source.label}: {source.error}"
        for source in sources
        if source.data is None
    ]
    lines = [
        f"Managed settings read: {', '.join(read)}."
        if read
        else "No managed settings document was found on this machine.",
        *unreadable,
        _SERVER_MANAGED_NOTE,
    ]
    return Check("settings-sources", "info", "settings sources", "\n".join(lines))


def _defines(source: SettingsSource, key: str) -> bool:
    return _data(source).get(key) is not None


_FILE_LEVELS: Final[tuple[SourceLevel, ...]] = ("local", "project", "user")


def _file_disable_all_hooks(sources: Sequence[SettingsSource]) -> Check:
    """The highest of local > project > user that sets ``disableAllHooks`` decides."""
    defining = [
        source
        for level in _FILE_LEVELS
        for source in _with(sources, level)
        if _defines(source, _DISABLE_ALL_HOOKS)
    ]
    off = "hooks are not turned off"
    if not defining:
        return Check("disable-all-hooks", "ok", off, "")
    winner = defining[0]
    if _data(winner)[_DISABLE_ALL_HOOKS] is True:
        if winner.level == "user":
            return Check(
                "disable-all-hooks",
                "fail",
                "all hooks are off",
                f"disableAllHooks is true in {winner.label}.",
                f"remove disableAllHooks from {winner.label}",
            )
        return Check(
            "disable-all-hooks",
            "warn",
            "hooks are off in this project",
            f"disableAllHooks is true in {winner.label}.",
            f"remove disableAllHooks from {winner.label} to see this project's sessions",
        )
    user = [
        source
        for source in defining
        if source.level == "user" and _data(source)[_DISABLE_ALL_HOOKS] is True
    ]
    if not user:
        return Check("disable-all-hooks", "ok", off, "")
    return Check(
        "disable-all-hooks",
        "info",
        "hooks are off in your other projects",
        f"disableAllHooks is true in {user[0].label}; {winner.label} sets it to false, so "
        "hooks run in this project only.",
        f"remove disableAllHooks from {user[0].label}",
    )


def check_disable_all_hooks(sources: Sequence[SettingsSource]) -> Check:
    """``disableAllHooks``: an admin source that sets it decides (true fails, false
    wins over every other file); else the highest of local > project > user that sets
    it. The HKCU value counts, as a warning, only when no admin source sets it and no
    user file turns hooks off."""
    admin = [s for s in _with(sources, "managed") if _defines(s, _DISABLE_ALL_HOOKS)]
    if admin:
        on = [s.label for s in admin if _data(s)[_DISABLE_ALL_HOOKS] is True]
        if on:
            return Check(
                "disable-all-hooks",
                "fail",
                "your organization's managed settings turn all hooks off",
                f"disableAllHooks is true in {', '.join(on)}.",
                "ask your administrator",
            )
        return Check(
            "disable-all-hooks",
            "ok",
            "hooks are not turned off",
            f"Your organization's managed settings set disableAllHooks to false in "
            f"{', '.join(s.label for s in admin)}.",
        )
    files = _file_disable_all_hooks(sources)
    if files.status == "fail":
        return files
    hkcu = [s.label for s in _with(sources, "hkcu") if _data(s).get(_DISABLE_ALL_HOOKS) is True]
    if hkcu:
        return Check(
            "disable-all-hooks",
            "warn",
            "the user policy registry value turns all hooks off",
            f"disableAllHooks is true in {', '.join(hkcu)} (applies when no admin source is "
            "present).",
            f"remove disableAllHooks from {', '.join(hkcu)}",
        )
    return files


def check_managed_hooks_only(sources: Sequence[SettingsSource]) -> Check:
    """``allowManagedHooksOnly: true`` stops every hook ccobs setup writes."""
    for level, status in _POLICY_LEVELS:
        found = [
            s.label for s in _with(sources, level) if _data(s).get(_MANAGED_HOOKS_ONLY) is True
        ]
        if found:
            return Check(
                "managed-hooks-only",
                status,
                "only managed hooks may run",
                f"allowManagedHooksOnly is true in {', '.join(found)}.",
                "ask your administrator to allow user hooks",
            )
    return Check("managed-hooks-only", "ok", "user hooks may run", "")


def _normalized_pattern(pattern: str) -> str:
    """``pattern`` with its scheme and host in lower case and without a trailing dot on
    the host: Claude Code matches host names case-insensitively and treats
    ``example.com.`` as ``example.com`` (settings reference, allowedHttpHookUrls)."""
    match = _PATTERN_HOST.match(pattern)
    if match is None:
        return pattern
    host = match["host"].lower()
    host = host.removesuffix(".")
    return match["scheme"].lower() + host + pattern[match.end() :]


def _pattern_matches(pattern: str, url: str) -> bool:
    """``url`` is ours: lower-case scheme and IPv4 host, so only the pattern is
    normalized."""
    parts = _normalized_pattern(pattern).split("*")
    regex = ".*".join(re.escape(part) for part in parts)
    return re.fullmatch(regex, url) is not None


def _allowlists(sources: Iterable[SettingsSource]) -> tuple[list[str], list[str]]:
    """The labels of the documents that define the allowlist, and every pattern."""
    labels: list[str] = []
    patterns: list[str] = []
    for source in sources:
        value = _data(source).get(_ALLOWLIST)
        if not isinstance(value, list):
            continue  # absent, null or not an array: not defined
        labels.append(source.label)
        patterns.extend(item for item in value if isinstance(item, str))
    return labels, patterns


def check_http_hook_allowlist(
    sources: Sequence[SettingsSource], *, app_port: int, user_file: str
) -> Check:
    """When ``allowedHttpHookUrls`` is defined anywhere, the merged patterns must match
    the hook URL. Reported, never fixed: setup does not edit it. The fix always names
    ``user_file`` (the user settings): the arrays merge across files, so a pattern
    there is enough, even when a managed file defines the list."""
    url = hook_url(app_port)
    readable = [source for source in sources if source.data is not None]
    admin_labels, admin_patterns = _allowlists(s for s in readable if s.level != "hkcu")
    hkcu_labels, hkcu_patterns = _allowlists(s for s in readable if s.level == "hkcu")
    if admin_labels and not any(_pattern_matches(p, url) for p in admin_patterns):
        status: Status = "fail"
        labels = admin_labels
    elif hkcu_labels and not any(
        _pattern_matches(p, url) for p in (*admin_patterns, *hkcu_patterns)
    ):
        status, labels = "warn", hkcu_labels
    elif admin_labels or hkcu_labels:
        return Check(
            "http-hook-allowlist",
            "ok",
            "allowedHttpHookUrls allows the hook URL",
            url,
        )
    else:
        return Check(
            "http-hook-allowlist", "ok", "no allowedHttpHookUrls: every hook URL is allowed", ""
        )
    return Check(
        "http-hook-allowlist",
        status,
        "allowedHttpHookUrls blocks cc-observatory's hooks",
        f"Defined in {', '.join(labels)}; no pattern matches {url}.",
        f'add "{url}" to allowedHttpHookUrls in {user_file}: the lists of every settings '
        "file merge, so one matching pattern is enough (ccobs setup never edits it)",
    )


def _exporter_issue(env: Mapping[str, object], name: str) -> str | None:
    """An exporter selector is a comma-separated list; the stack needs ``otlp`` in it."""
    if not _set(env, name):
        return None
    chosen = [part.strip() for part in _value(env, name).lower().split(",")]
    if _OTLP in chosen:
        return None
    if chosen == ["none"]:
        return f"turns {name} off"
    return f"sets {name} to an exporter other than {_OTLP}"


def _managed_env_issues(env: Mapping[str, object], endpoint: str) -> list[str]:
    issues: list[str] = []
    if _set(env, _ENDPOINT) and _value(env, _ENDPOINT).rstrip("/") != endpoint:
        issues.append(f"sets {_ENDPOINT} to another endpoint")
    if _set(env, _PROTOCOL) and _value(env, _PROTOCOL).lower() != _HTTP_PROTOBUF:
        issues.append(f"sets {_PROTOCOL} to another protocol (the stack takes {_HTTP_PROTOBUF})")
    issues.extend(
        f"sets {name}"
        for name in PER_SIGNAL_ENDPOINT_VARIABLES
        if _set(env, name) and _value(env, name) != ""
    )
    if _set(env, _ENABLE) and _value(env, _ENABLE).lower() in _OFF_VALUES:
        issues.append(f"turns {_ENABLE} off")
    issues.extend(issue for name in _EXPORTERS if (issue := _exporter_issue(env, name)) is not None)
    return issues


def check_managed_env(sources: Sequence[SettingsSource], *, otlp_port: int) -> Check:
    """A managed ``env`` that sends telemetry elsewhere or turns it off overrides the
    user settings."""
    endpoint = otlp_endpoint(otlp_port)
    for level, status in _POLICY_LEVELS:
        lines = [
            f"{source.label} {issue}"
            for source in _with(sources, level)
            for issue in _managed_env_issues(env_of(source.data), endpoint)
        ]
        if lines:
            return Check(
                "managed-env",
                status,
                "your organization's managed settings override the telemetry variables",
                "\n".join(lines),
                "ask your administrator",
            )
    return Check("managed-env", "ok", "no managed setting overrides the telemetry variables", "")


_PROJECT_LEVELS: Final[tuple[SourceLevel, ...]] = ("project", "local")


def check_project_telemetry_off(sources: Sequence[SettingsSource]) -> Check:
    """Project and local files can still turn an exporter off (``none``)."""
    lines = [
        f"{name} is none in {source.label}"
        for level in _PROJECT_LEVELS
        for source in _with(sources, level)
        for name in _EXPORTERS
        if name in env_of(source.data)
        and _text(env_of(source.data)[name]).strip().lower() == "none"
    ]
    if not lines:
        return Check("project-telemetry-off", "ok", "this project leaves telemetry on", "")
    return Check(
        "project-telemetry-off",
        "warn",
        "this project turns telemetry off",
        "\n".join(lines),
        "remove the exporter setting from the project's settings to see its telemetry",
    )


# Stack.


@dataclass(frozen=True)
class HttpAnswer:
    """``status`` is None when nothing answered."""

    status: int | None
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _answer_text(answer: HttpAnswer) -> str:
    return "no answer" if answer.status is None else f"status {answer.status}"


def check_app(answer: HttpAnswer, *, app_port: int) -> Check:
    url = f"http://{_HOST}:{app_port}"
    if answer.ok:
        return Check("app", "ok", "the app answers", url)
    return Check(
        "app",
        "fail",
        "the app does not answer",
        f"GET {url}/healthz: {_answer_text(answer)}.",
        "start the stack: `docker compose up -d` in your cc-observatory clone",
    )


def check_hook_endpoint(answer: HttpAnswer, *, app_port: int) -> Check:
    """The direct probe of ADR 0011: ``POST {}`` to the hook URL must answer 2xx."""
    url = hook_url(app_port)
    note = "The probe posts {}, which the app drops as one invalid hook event."
    if answer.ok:
        return Check("hook-endpoint", "ok", "the hook endpoint answers", f"{url}\n{note}")
    return Check(
        "hook-endpoint",
        "fail",
        "the hook endpoint does not answer 2xx",
        f"POST {url}: {_answer_text(answer)}.\n{note}",
        "start the stack, or check that --app-port is the app's port",
    )


def check_diagnostics(answer: HttpAnswer | None, *, cli_version: str) -> Check:
    """The app's ``GET /api/v1/diagnostics`` (ADR 0038). ``answer`` is None when the app
    is down (the request was not sent)."""
    if answer is None:
        return Check("diagnostics", "info", "the app's diagnostics", "Skipped: the app is down.")
    parsed = parse_diagnostics(answer.body) if answer.ok else None
    if parsed is None:
        return Check(
            "diagnostics",
            "warn",
            "the app's diagnostics were not understood",
            f"GET /api/v1/diagnostics: {_answer_text(answer)}; an app older than this CLI "
            "has no such route.",
            "rebuild the app: `docker compose build app && docker compose up -d`",
        )
    lines: list[str] = []
    statuses: list[Status] = []
    if parsed.version != cli_version:
        statuses.append("warn")
        lines.append(
            f"CLI {cli_version}, app {parsed.version}: rebuild the app or upgrade the CLI."
        )
    failures = parsed.hook_failures
    if isinstance(failures, UncountedHookFailures):
        statuses.append("warn")
        lines.append(f"The app cannot read Loki: {failures.reason}.")
    else:
        errors, cancelled = failures.counts.non_blocking_error, failures.counts.cancelled
        if errors or cancelled:
            statuses.append("warn")
            lines.append(
                f"Hook failures in the last hour: {errors} non-blocking errors, {cancelled} "
                "cancelled. They include Claude Code's own and your other hooks."
            )
    serious = {
        reason: parsed.hooks.dropped.get(reason, 0) for reason in ("full", "too_large", "internal")
    }
    if any(serious.values()):
        statuses.append("warn")
        counts = ", ".join(f"{reason} {count}" for reason, count in serious.items())
        lines.append(f"The app dropped hook events since it started: {counts}.")
    if not any(parsed.hooks.received.values()):
        statuses.append("info")
        lines.append("No hook event since the app started: open a Claude Code session.")
    status = _worst(statuses)
    if status == "ok":
        received = sum(parsed.hooks.received.values())
        return Check(
            "diagnostics",
            "ok",
            "the app's diagnostics",
            f"App {parsed.version}; {received} hook events received since it started.",
        )
    title = "the app's diagnostics need attention" if status == "warn" else "the app's diagnostics"
    return Check("diagnostics", status, title, "\n".join(lines))


def check_otlp(connected: bool, *, otlp_port: int) -> Check:
    address = f"{_HOST}:{otlp_port}"
    if connected:
        return Check("otlp", "ok", "the collector's OTLP/HTTP port accepts connections", address)
    return Check(
        "otlp",
        "fail",
        "the collector's OTLP/HTTP port refuses connections",
        address,
        "start the stack, or check that --otlp-port is the collector's port",
    )


def check_grafana(answer: HttpAnswer, *, grafana_port: int) -> Check:
    url = f"http://{_HOST}:{grafana_port}"
    if answer.ok:
        return Check("grafana", "ok", "Grafana answers", url)
    return Check(
        "grafana",
        "warn",
        "Grafana does not answer",
        f"GET {url}/api/health: {_answer_text(answer)}.",
        "start the stack, or check that --grafana-port is Grafana's port",
    )


# Clone.


def is_ccobs_compose(text: str) -> bool:
    """Whether a compose file's text has the top-level line ``name: cc-observatory``."""
    return any(_COMPOSE_NAME.fullmatch(line.rstrip("\r")) for line in text.split("\n"))


@dataclass(frozen=True)
class StatEntry:
    """A path under ``stack/`` (relative), whether it is a directory, and its mode.
    ``error`` says why it could not be checked (then ``is_dir`` and ``mode`` mean
    nothing)."""

    path: str
    is_dir: bool
    mode: int
    error: str | None = None


def check_stack_readable(entries: Sequence[StatEntry]) -> Check:
    """The containers' non-root users read ``stack/`` as "others" (ADR 0020)."""
    blocked = [
        entry.path
        for entry in entries
        if entry.error is None
        and (not entry.mode & _OTHERS_READ or (entry.is_dir and not entry.mode & _OTHERS_SEARCH))
    ]
    unchecked = [f"{entry.path} ({entry.error})" for entry in entries if entry.error is not None]
    if not blocked and not unchecked:
        return Check("stack-readable", "ok", "stack/ is readable by the containers", "")
    lines: list[str] = []
    if blocked:
        lines.append(f"Not readable (or searchable) by others: {_listed(blocked)}.")
    if unchecked:
        lines.append(f"Could not check: {_listed(unchecked)}.")
    if blocked:
        return Check(
            "stack-readable",
            "fail",
            "stack/ is not readable by the containers",
            "\n".join(lines),
            "chmod -R o+rX stack/",
        )
    return Check(
        "stack-readable",
        "warn",
        "some of stack/ could not be checked",
        "\n".join(lines),
        "make sure the containers can read stack/: chmod -R o+rX stack/",
    )


def _env_value(raw: str) -> str:
    """A ``.env`` value: inside matching quotes (which may hold ``#``, and anything
    after the closing quote is a comment), else up to an inline comment (a blank, then
    ``#``)."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing > 0:
            return value[1:closing]
    # Searched in ``raw``: in ``KEY= # note`` the blank before '#' is after the '='.
    found = _ENV_COMMENT.search(raw)
    return (raw[: found.start()] if found is not None else raw).strip()


def parse_env_file(text: str, keys: Iterable[str] = ENV_FILE_KEYS) -> dict[str, str]:
    """The values of ``keys`` in a ``.env`` text: ``KEY=VALUE`` lines, blanks and
    comments skipped, optional matching quotes, inline comments after an unquoted value.
    An empty value is absent: compose's ``${VAR:-default}`` uses the default then.
    Every other key is ignored."""
    wanted = frozenset(keys)
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        # compose's dotenv also accepts a shell-style "export " prefix.
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not separator or key not in wanted:
            continue
        value = _env_value(value)
        if value:
            values[key] = value
        else:
            values.pop(key, None)  # a later empty line wins, like compose
    return values


def _port_of(text: str) -> int | None:
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 1 <= number <= _MAX_PORT else None


def check_env_file(
    values: Mapping[str, str] | None, *, app_port: int, otlp_port: int, grafana_port: int
) -> Check:
    """The clone's ``.env`` (``values`` from ``parse_env_file``; None when absent).
    Only ports are quoted; the retention and host label are judged, never shown."""
    found = values if values is not None else {}
    lines: list[str] = []
    statuses: list[Status] = []
    retention = found.get(_RETENTION_KEY)
    if retention is not None:
        try:
            invalid = parse_duration(retention) % _DAY != timedelta(0)
            problem = "is not a multiple of 24h, which Loki's retention requires"
        except ValueError:
            invalid, problem = True, "is not a duration"
        if invalid:
            statuses.append("warn")
            lines.append(f"{_RETENTION_KEY} {problem}.")
    label = found.get(_HOST_LABEL_KEY)
    if label is not None and not HOST_VALUE_PATTERN.fullmatch(label):
        statuses.append("fail")
        lines.append(f"{_HOST_LABEL_KEY} must be letters, digits, '.', '_' or '-' (and not empty).")
    used = {"--app-port": app_port, "--otlp-port": otlp_port, "--grafana-port": grafana_port}
    for key, flag, default in _PORT_KEYS:
        text = found.get(key)
        port = default if text is None else _port_of(text)
        if port is None:
            statuses.append("warn")
            lines.append(f"{key} is not a port.")
        elif port != used[flag]:
            statuses.append("warn")
            where = (
                f"{key} is {port} in .env" if text is not None else f"the stack's default is {port}"
            )
            lines.append(f"{where}, but doctor used {flag} {used[flag]}: pass {flag} {port}.")
    status = _worst(statuses)
    absent = "No .env: the stack runs with compose.yaml's defaults."
    if status == "ok":
        return Check(
            "env-file",
            "ok",
            "the clone's .env matches",
            absent if values is None else "The .env values doctor checks are valid.",
        )
    if values is None:
        lines.insert(0, absent)
    return Check(
        "env-file",
        status,
        "the clone's .env needs attention",
        "\n".join(lines),
        "edit .env, then `docker compose up -d`" if values is not None else None,
    )
