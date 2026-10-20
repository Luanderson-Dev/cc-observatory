"""``ccobs status``: a short, fast summary of the stack and the user settings (ADR 0039).

A subset of ``ccobs doctor``: whether the app, the collector's OTLP/HTTP port and
Grafana answer, whether setup's hooks and telemetry ``env`` are in the user settings
file, and the session counts by state, the open blocks and the last hour's hook
failures from the app's ``GET /api/v1/diagnostics`` (ADR 0038), parsed by
``ccobs.domain.diagnostics.parse_diagnostics`` as doctor does. It runs no command and
reads no managed source; the four network probes run at once, so the whole command
waits at most ``DIAGNOSTICS_TIMEOUT_S``.

Output: about eight lines, or ``--json``. Exit 0 unless the app does not answer (1); 2
on a usage error. No output quotes a value read from a settings file or the
environment: settings are summarized as states, and the URLs shown are the stack's own.

Standard library only: the CLI stays light (tests/test_cli_light.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final, Literal, TextIO

import ccobs
from ccobs.cli import settings_file
from ccobs.cli.doctor import (
    CONNECT_TIMEOUT_S,
    HTTP_TIMEOUT_S,
    Connect,
    Http,
    add_target_options,
    http_request,
    tcp_connect,
)
from ccobs.cli.settings_file import ConfigDirError, SettingsFileError
from ccobs.cli.streams import tolerate_unencodable
from ccobs.domain import doctor as rules
from ccobs.domain.diagnostics import (
    CountedHookFailures,
    Diagnostics,
    ReportedSessions,
    UncountedHookFailures,
    parse_diagnostics,
)
from ccobs.domain.doctor import HttpAnswer

__all__ = [
    "DIAGNOSTICS_TIMEOUT_S",
    "Environment",
    "configure_parser",
    "default_environment",
    "run_status",
]

# The diagnostics route waits up to 2 s on Loki before it answers (ADR 0038), so its
# request gets a little longer than the other probes'.
DIAGNOSTICS_TIMEOUT_S: Final = 3.0
_HOST: Final = "127.0.0.1"
_DIAGNOSTICS_PATH: Final = "/api/v1/diagnostics"
_HOUR_S: Final = 3600
_STATE_TEXT: Final = {"waiting_user": "waiting on you"}
_ENABLE: Final = "CLAUDE_CODE_ENABLE_TELEMETRY"
_ENDPOINT: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"

# The states the settings line and ``--json`` report.
type _FileState = Literal["ok", "missing", "unreadable"]
type _HookState = Literal["installed", "outdated", "missing"]
type _TelemetryState = Literal["on", "off", "other_endpoint", "incomplete"]
_HOOK_STATES: Final[Mapping[str, _HookState]] = {
    "ok": "installed",
    "warn": "outdated",
    "fail": "missing",
}

# Why there are no diagnostics to show: the ``--json`` reason, and the text.
type _Unavailable = Literal["app_down", "no_answer", "busy", "no_route", "not_understood"]
_UNAVAILABLE_TEXT: Final[Mapping[_Unavailable, str]] = {
    "app_down": "unknown (app down)",
    # Refused, reset, or no answer within DIAGNOSTICS_TIMEOUT_S.
    "no_answer": "unknown (no answer from the diagnostics route)",
    # The read gate is saturated (ADR 0033): a retry may get through.
    "busy": "unknown (app busy, try again)",
    # An app built before ADR 0038 has no such route.
    "no_route": "unknown (the app predates /api/v1/diagnostics; rebuild it)",
    "not_understood": "unknown (the app's diagnostics were not understood)",
}


@dataclass(frozen=True)
class Environment:
    """Everything status reads from the machine, besides the settings file."""

    http: Http
    connect: Connect
    environ: Mapping[str, str]
    platform: str


def default_environment() -> Environment:
    return Environment(
        http=http_request, connect=tcp_connect, environ=os.environ, platform=sys.platform
    )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the options of ``ccobs status`` to its subparser."""
    add_target_options(parser)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")


# The settings file.


@dataclass(frozen=True)
class _Settings:
    path: str
    file: _FileState
    hooks: _HookState | None  # None when the file is unreadable
    telemetry: _TelemetryState | None  # None when the file is unreadable


def _telemetry(data: Mapping[str, object], *, otlp_port: int) -> _TelemetryState:
    if rules.check_telemetry_env(data, otlp_port=otlp_port).status != "fail":
        return "on"  # a temporality other than cumulative only warns
    env = rules.env_of(data)
    if env.get(_ENABLE) != "1":
        return "off"
    endpoint = env.get(_ENDPOINT)
    if endpoint is not None and endpoint != rules.otlp_endpoint(otlp_port):
        return "other_endpoint"
    return "incomplete"


def _read_settings(path: Path, *, app_port: int, otlp_port: int, windows: bool) -> _Settings:
    try:
        loaded = settings_file.read_settings(path)
    except SettingsFileError:
        # The reader's message never holds content, but status keeps to the state.
        return _Settings(str(path), "unreadable", None, None)
    hooks = rules.check_hooks(loaded.data, app_port=app_port, windows=windows)
    return _Settings(
        str(path),
        "ok" if loaded.exists else "missing",
        _HOOK_STATES[hooks.status],
        _telemetry(loaded.data, otlp_port=otlp_port),
    )


# The diagnostics answer.


def _unavailable(probes: _Probes) -> _Unavailable:
    """Why ``probes`` give no diagnostics to show, when they give none."""
    if not probes.app.ok:
        return "app_down"
    status = probes.diagnostics.status
    if status is None:
        return "no_answer"
    if status == HTTPStatus.SERVICE_UNAVAILABLE:
        return "busy"
    if status == HTTPStatus.NOT_FOUND:
        return "no_route"
    return "not_understood"


# Probes.


@dataclass(frozen=True)
class _Probes:
    app: HttpAnswer
    diagnostics: HttpAnswer
    grafana: HttpAnswer
    otlp: bool


def _probe(env: Environment, *, app_port: int, otlp_port: int, grafana_port: int) -> _Probes:
    """The four probes at once: the whole run waits for the slowest one only."""
    from concurrent.futures import ThreadPoolExecutor  # only status runs threads

    with ThreadPoolExecutor(max_workers=4) as pool:
        app = pool.submit(env.http, "GET", app_port, "/healthz", None, HTTP_TIMEOUT_S)
        diagnostics = pool.submit(
            env.http, "GET", app_port, _DIAGNOSTICS_PATH, None, DIAGNOSTICS_TIMEOUT_S
        )
        grafana = pool.submit(env.http, "GET", grafana_port, "/api/health", None, HTTP_TIMEOUT_S)
        otlp = pool.submit(env.connect, _HOST, otlp_port, CONNECT_TIMEOUT_S)
        return _Probes(app.result(), diagnostics.result(), grafana.result(), otlp.result())


# Output.


@dataclass(frozen=True)
class _Summary:
    app_url: str
    otlp_address: str
    grafana_url: str
    probes: _Probes
    settings: _Settings
    diagnostics: Diagnostics | None
    unavailable: _Unavailable | None  # why ``diagnostics`` is None


def _up(up: bool) -> str:
    return "up  " if up else "down"


def _settings_line(settings: _Settings) -> str:
    if settings.hooks is None or settings.telemetry is None:
        state = "unreadable"
    else:
        state = f"hooks {settings.hooks}, telemetry {settings.telemetry.replace('_', ' ')}"
        if settings.file == "missing":
            state = f"no settings file, {state}"
    return f"Claude Code settings: {state} ({settings.path})"


def _unavailable_text(summary: _Summary) -> str:
    return _UNAVAILABLE_TEXT[summary.unavailable or "not_understood"]


def _sessions_text(sessions: ReportedSessions) -> str:
    states = ", ".join(
        f"{count} {_STATE_TEXT.get(state, state)}" for state, count in sessions.by_state.items()
    )
    blocks = "block" if sessions.open_blocks == 1 else "blocks"
    return f"{states}; {sessions.open_blocks} open {blocks}"


def _failures_line(summary: _Summary) -> str:
    if summary.diagnostics is None:
        return f"Hook failures (last hour): {_unavailable_text(summary)}"
    failures = summary.diagnostics.hook_failures
    window_s = failures.window_s
    window = "last hour" if window_s == _HOUR_S else f"last {window_s} s"
    if isinstance(failures, UncountedHookFailures):
        return f"Hook failures ({window}): unknown (Loki unavailable: {failures.reason})"
    counts = failures.counts
    runs = "run" if counts.executions == 1 else "runs"
    return (
        f"Hook failures ({window}): {counts.non_blocking_error} non-blocking errors, "
        f"{counts.cancelled} cancelled ({counts.executions} {runs})"
    )


def _text_lines(summary: _Summary) -> Sequence[str]:
    version = f"cc-observatory {ccobs.__version__}"
    diagnostics = summary.diagnostics
    if diagnostics is not None and diagnostics.version != ccobs.__version__:
        version += f" (app {diagnostics.version})"
    if diagnostics is not None:
        sessions = _sessions_text(diagnostics.sessions)
    else:
        sessions = _unavailable_text(summary)
    probes = summary.probes
    return [
        version,
        f"App       {_up(probes.app.ok)}  {summary.app_url}",
        f"Collector {_up(probes.otlp)}  {summary.otlp_address} (OTLP/HTTP)",
        f"Grafana   {_up(probes.grafana.ok)}  {summary.grafana_url}",
        _settings_line(summary.settings),
        f"Sessions: {sessions}",
        _failures_line(summary),
        "Run `ccobs doctor` for details.",
    ]


def _failures_json(failures: CountedHookFailures | UncountedHookFailures) -> dict[str, object]:
    if isinstance(failures, UncountedHookFailures):
        return {"available": False, "reason": failures.reason, "window_s": failures.window_s}
    return {"available": True, "window_s": failures.window_s, **asdict(failures.counts)}


def _json_document(summary: _Summary) -> dict[str, object]:
    diagnostics, probes = summary.diagnostics, summary.probes
    return {
        "version": ccobs.__version__,
        "app": {
            "up": probes.app.ok,
            "url": summary.app_url,
            "version": diagnostics.version if diagnostics is not None else None,
        },
        "collector": {"up": probes.otlp, "address": summary.otlp_address},
        "grafana": {"up": probes.grafana.ok, "url": summary.grafana_url},
        "settings": asdict(summary.settings),
        "diagnostics": {"understood": diagnostics is not None, "reason": summary.unavailable},
        "sessions": asdict(diagnostics.sessions) if diagnostics is not None else None,
        "hook_failures": (
            _failures_json(diagnostics.hook_failures) if diagnostics is not None else None
        ),
    }


# The command.


def run_status(args: argparse.Namespace, *, env: Environment, out: TextIO, err: TextIO) -> int:
    """Run ``ccobs status``. Returns 0 when the app answers, 1 when it does not, 2 on a
    usage error."""
    tolerate_unencodable(out, err)
    try:
        config_dir = settings_file.resolve_config_dir(args.config_dir, env.environ)
    except ConfigDirError as error:
        print(f"ccobs status: {error}", file=err)
        return 2
    app_port: int = args.app_port
    otlp_port: int = args.otlp_port
    grafana_port: int = args.grafana_port
    probes = _probe(env, app_port=app_port, otlp_port=otlp_port, grafana_port=grafana_port)
    settings = _read_settings(
        settings_file.settings_path(config_dir),
        app_port=app_port,
        otlp_port=otlp_port,
        windows=env.platform == "win32",
    )
    # A diagnostics answer from a port whose /healthz fails is not trusted.
    trusted = probes.app.ok and probes.diagnostics.ok
    diagnostics = parse_diagnostics(probes.diagnostics.body) if trusted else None
    summary = _Summary(
        app_url=f"http://{_HOST}:{app_port}",
        otlp_address=f"{_HOST}:{otlp_port}",
        grafana_url=f"http://{_HOST}:{grafana_port}",
        probes=probes,
        settings=settings,
        diagnostics=diagnostics,
        unavailable=None if diagnostics is not None else _unavailable(probes),
    )
    if args.json:
        # ASCII: any console can show it, and a JSON reader gets the same text back.
        print(json.dumps(_json_document(summary), indent=2, ensure_ascii=True), file=out)
    else:
        for line in _text_lines(summary):
            print(line, file=out)
    return 0 if probes.app.ok else 1
