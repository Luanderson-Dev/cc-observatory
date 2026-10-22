"""``ccobs doctor``: check Claude Code, its settings and policies, the stack and the clone,
and explain what stops data from arriving (ADR 0039).

The rules are pure (``ccobs.domain.doctor``); this module collects their input. Every
read, command, request and connection goes through an ``Environment``, so tests inject
each one. Doctor writes nothing: ``ccobs setup`` is the only writer (ADR 0037), and
policies belong to the user or their organization.

Output: one line per check grouped by section, or ``--json``. Exit 0 with only ok, warn
and info; 1 when any check fails; 2 on a usage error. No output quotes a value read from
a settings file, the environment or ``.env``, except cc-observatory's own URL and
endpoint, ports and versions.

Standard library only: the CLI stays light (tests/test_cli_light.py). ``winreg`` and
``plistlib`` are imported lazily, on the OS that has them; ``subprocess``, ``socket`` and
the HTTP client only when doctor runs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

import ccobs
from ccobs.cli import settings_file
from ccobs.cli.install import port_type
from ccobs.cli.masking import reason
from ccobs.cli.settings_file import ConfigDirError, SettingsFileError
from ccobs.cli.streams import tolerate_unencodable
from ccobs.domain import doctor as rules
from ccobs.domain.doctor import (
    Check,
    DockerVersions,
    EnvSource,
    HttpAnswer,
    SettingsSource,
    SourceLevel,
    StatEntry,
)
from ccobs.domain.hook_config import HOOK_PATH

__all__ = [
    "COMMAND_TIMEOUT_S",
    "CONNECT_TIMEOUT_S",
    "HTTP_TIMEOUT_S",
    "PLIST_PATH",
    "Connect",
    "Environment",
    "Http",
    "add_target_options",
    "configure_parser",
    "default_environment",
    "http_request",
    "managed_dir",
    "read_registry",
    "run_command",
    "run_doctor",
    "stack_entries",
    "tcp_connect",
]

COMMAND_TIMEOUT_S: Final = 10.0
HTTP_TIMEOUT_S: Final = 2.0
CONNECT_TIMEOUT_S: Final = 1.0
_HOST: Final = "127.0.0.1"
_MAX_BODY: Final = 1024 * 1024
_REGISTRY_KEY: Final = "SOFTWARE\\Policies\\ClaudeCode"
_REGISTRY_VALUE: Final = "Settings"
PLIST_PATH: Final = Path("/Library/Managed Preferences/com.anthropic.claudecode.plist")
_SHELL: Final = "your shell environment"
_HIVES: Final[tuple[tuple[str, SourceLevel], ...]] = (("HKLM", "managed"), ("HKCU", "hkcu"))
_PROJECT_FILES: Final[tuple[tuple[SourceLevel, str], ...]] = (
    ("project", "settings.json"),
    ("local", "settings.local.json"),
)

type Which = Callable[[str], str | None]
# argv and timeout in seconds -> stdout, or None when it did not run, failed or hung.
type Run = Callable[[Sequence[str], float], str | None]
# method, port on 127.0.0.1, path, body (sent as application/json) and timeout.
type Http = Callable[[str, int, str, bytes | None, float], HttpAnswer]
type Connect = Callable[[str, int, float], bool]
# "HKLM" or "HKCU" -> the Settings value, None when absent; raises OSError or ValueError.
type RegistryReader = Callable[[str], str | None]


@dataclass(frozen=True)
class Environment:
    """Everything doctor reads from the machine."""

    which: Which
    run: Run
    http: Http
    connect: Connect
    read_registry: RegistryReader
    platform: str
    managed_dir: Path
    plist_path: Path
    environ: Mapping[str, str]
    cwd: Path


# The default implementations.


def run_command(argv: Sequence[str], timeout: float) -> str | None:
    """``argv``'s standard output when it exits 0 within ``timeout`` seconds, else None.
    No shell, no standard input; a process that hangs is killed.

    After the kill, nothing waits on the pipe again: on Windows a grandchild (node.exe
    behind ``claude.cmd``, a docker CLI plugin) can keep it open, and a second
    ``communicate()`` (what ``subprocess.run`` does) would never return."""
    import subprocess  # only doctor runs commands

    try:
        # S603 cannot see that this is safe: argv[0] comes from shutil.which, every
        # other argument is fixed, and there is no shell.
        process = subprocess.Popen(  # noqa: S603
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return None
    return stdout.decode("utf-8", "replace") if process.returncode == 0 else None


def http_request(
    method: str, port: int, path: str, body: bytes | None, timeout: float
) -> HttpAnswer:
    """The status and body (up to 1 MiB) of one request to ``127.0.0.1:port``; a body
    goes as JSON. Never through a proxy (the stack is on loopback) and never following
    a redirect; any error is no answer."""
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        request = urllib.request.Request(
            f"http://{_HOST}:{port}{path}", data=body, headers=headers, method=method
        )
        with opener.open(request, timeout=timeout) as response:
            return HttpAnswer(response.status, response.read(_MAX_BODY))
    except urllib.error.HTTPError as error:
        error.close()
        return HttpAnswer(error.code)
    except (OSError, ValueError, http.client.HTTPException):
        return HttpAnswer(None)


def tcp_connect(host: str, port: int, timeout: float) -> bool:
    """Whether a TCP connection to ``host:port`` opens within ``timeout`` seconds."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_registry(hive: str) -> str | None:
    """The ``Settings`` value under ``<hive>\\SOFTWARE\\Policies\\ClaudeCode`` (hive
    ``HKLM`` or ``HKCU``), None when it is absent or not on Windows. Raises ``OSError``
    when it cannot be read and ``ValueError`` when it is not a string."""
    if sys.platform != "win32":
        return None
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, _REGISTRY_KEY) as key:
            value, kind = winreg.QueryValueEx(key, _REGISTRY_VALUE)
    except FileNotFoundError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        raise ValueError(f"the {_REGISTRY_VALUE} value is not a string")
    return value


def managed_dir(platform: str) -> Path:
    """Claude Code's system directory for managed settings files."""
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if platform == "win32":
        return Path("C:\\Program Files\\ClaudeCode")
    return Path("/etc/claude-code")


def default_environment() -> Environment:
    return Environment(
        which=shutil.which,
        run=run_command,
        http=http_request,
        connect=tcp_connect,
        read_registry=read_registry,
        platform=sys.platform,
        managed_dir=managed_dir(sys.platform),
        plist_path=PLIST_PATH,
        environ=os.environ,
        cwd=Path.cwd(),
    )


# The parser.


def add_target_options(parser: argparse.ArgumentParser) -> None:
    """Add ``--config-dir`` and the stack's port options, shared with ``ccobs status``."""
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Claude Code's configuration directory (default: $CLAUDE_CONFIG_DIR, else ~/.claude)",
    )
    for flag, default, what in (
        ("--app-port", rules.DEFAULT_APP_PORT, "the app's port, which receives the hooks"),
        ("--otlp-port", rules.DEFAULT_OTLP_PORT, "the collector's OTLP/HTTP port"),
        ("--grafana-port", rules.DEFAULT_GRAFANA_PORT, "Grafana's port"),
    ):
        parser.add_argument(
            flag,
            type=port_type,
            default=default,
            metavar="N",
            help=f"{what} on 127.0.0.1 (default {default})",
        )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the options of ``ccobs doctor`` to its subparser."""
    add_target_options(parser)
    parser.add_argument(
        "--stack-dir",
        metavar="PATH",
        help="your cc-observatory clone (default: the working directory, when it is one)",
    )
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")


# Collectors.


def _claude_code(env: Environment) -> list[Check]:
    claude = env.which("claude")
    docker = env.which("docker")
    versions = DockerVersions(found=False, engine=None, compose=None, buildx=None)
    if docker is not None:
        versions = DockerVersions(
            found=True,
            engine=env.run(
                [docker, "version", "--format", "{{.Server.Version}}"], COMMAND_TIMEOUT_S
            ),
            compose=env.run([docker, "compose", "version", "--short"], COMMAND_TIMEOUT_S),
            buildx=env.run([docker, "buildx", "version"], COMMAND_TIMEOUT_S),
        )
    return [
        rules.check_curl(env.which("curl")),
        rules.check_claude_version(
            env.run([claude, "--version"], COMMAND_TIMEOUT_S) if claude is not None else None
        ),
        rules.check_docker(versions, linux=env.platform.startswith("linux")),
    ]


def _read_file_source(level: SourceLevel, path: Path) -> SettingsSource | None:
    """The settings document at ``path``; None when there is no such file."""
    try:
        loaded = settings_file.read_settings(path)
    except SettingsFileError as error:
        return SettingsSource(level, str(path), None, str(error))
    return SettingsSource(level, str(path), loaded.data) if loaded.exists else None


def _parsed_source(level: SourceLevel, label: str, text: str) -> SettingsSource:
    try:
        data: object = json.loads(text)
    except (ValueError, RecursionError):
        return SettingsSource(level, label, None, "not valid JSON")
    if not isinstance(data, dict):
        return SettingsSource(level, label, None, "not a JSON object")
    return SettingsSource(level, label, data)


def _managed_sources(env: Environment) -> list[SettingsSource]:
    """Every managed document on this machine: the files (all OSes), the registry
    (Windows) and the plist (macOS). Doctor does not rank them like Claude Code does:
    an admin source that sets a key counts; HKCU is warn-level evidence."""
    found: list[SettingsSource] = []
    main = _read_file_source("managed", env.managed_dir / "managed-settings.json")
    if main is not None:
        found.append(main)
    drop_ins = env.managed_dir / "managed-settings.d"
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(drop_ins)
            if entry.name.endswith(".json") and not entry.name.startswith(".")
        )
    except FileNotFoundError:
        names = []
    except OSError as error:
        found.append(SettingsSource("managed", str(drop_ins), None, reason(error)))
        names = []
    for name in names:
        source = _read_file_source("managed", drop_ins / name)
        if source is not None:
            found.append(source)
    if env.platform == "win32":
        for hive, level in _HIVES:
            label = f"{hive}\\{_REGISTRY_KEY}"
            try:
                text = env.read_registry(hive)
            except OSError as error:
                found.append(SettingsSource(level, label, None, reason(error)))
                continue
            except ValueError as error:
                found.append(SettingsSource(level, label, None, str(error)))
                continue
            if text is not None:
                found.append(_parsed_source(level, label, text))
    if env.platform == "darwin":
        found.extend(_plist_source(env.plist_path))
    return found


def _plist_source(path: Path) -> list[SettingsSource]:
    import plistlib  # macOS only

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        return [SettingsSource("managed", str(path), None, reason(error))]
    try:
        data: object = plistlib.loads(raw)
    except Exception:
        # plistlib raises InvalidFileException, ExpatError and others on a bad file.
        return [SettingsSource("managed", str(path), None, "not a valid property list")]
    if not isinstance(data, dict):
        return [SettingsSource("managed", str(path), None, "not a dictionary")]
    return [SettingsSource("managed", str(path), data)]


def _project_sources(env: Environment, user_path: Path) -> list[SettingsSource]:
    found: list[SettingsSource] = []
    for level, name in _PROJECT_FILES:
        path = env.cwd / ".claude" / name
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(
            os.path.realpath(user_path)
        ):
            continue  # the working directory is the home directory
        source = _read_file_source(level, path)
        if source is not None:
            found.append(source)
    return found


@dataclass(frozen=True)
class _Settings:
    checks: list[Check]
    policies: list[Check]


def _settings_and_policies(env: Environment, path: Path, args: argparse.Namespace) -> _Settings:
    app_port: int = args.app_port
    otlp_port: int = args.otlp_port
    error: str | None = None
    data: Mapping[str, object] | None = None
    exists = True
    try:
        loaded = settings_file.read_settings(path)
    except SettingsFileError as caught:
        error = str(caught)
    else:
        exists = loaded.exists
        data = loaded.data if loaded.exists else None

    managed = _managed_sources(env)
    project = _project_sources(env, path)
    user_label = str(path)
    user_env = EnvSource(user_label, rules.env_of(data))
    shell = EnvSource(_SHELL, env.environ)
    project_envs = [EnvSource(s.label, rules.env_of(s.data)) for s in project if s.data is not None]
    managed_envs = [EnvSource(s.label, rules.env_of(s.data)) for s in managed if s.data is not None]
    # Claude Code drops project and local values of the content and per-signal
    # variables (settings reference, "Variables Claude Code ignores in env"), so only the
    # user file, the shell and managed settings (admin or HKCU) can set them.
    settings = [
        rules.check_settings_file(user_label, exists=exists, error=error),
        rules.check_hooks(data, app_port=app_port, windows=env.platform == "win32"),
        rules.check_telemetry_env(data, otlp_port=otlp_port),
        rules.check_content_variables([user_env, shell, *managed_envs]),
        rules.check_session_id([user_env, shell, *project_envs, *managed_envs]),
        rules.check_per_signal_endpoints([user_env, shell, *managed_envs]),
        rules.check_shell_overrides(env.environ, otlp_port=otlp_port),
    ]
    user = [SettingsSource("user", user_label, data)] if data is not None else []
    sources = [*managed, *user, *project]
    policies = [
        rules.check_settings_sources(sources),
        rules.check_disable_all_hooks(sources),
        rules.check_managed_hooks_only(sources),
        rules.check_http_hook_allowlist(sources, app_port=app_port, user_file=user_label),
        rules.check_managed_env(sources, otlp_port=otlp_port),
        rules.check_project_telemetry_off(sources),
    ]
    return _Settings(settings, policies)


def _stack(env: Environment, args: argparse.Namespace) -> list[Check]:
    app_port: int = args.app_port
    otlp_port: int = args.otlp_port
    grafana_port: int = args.grafana_port
    app = env.http("GET", app_port, "/healthz", None, HTTP_TIMEOUT_S)
    hook = env.http("POST", app_port, HOOK_PATH, b"{}", HTTP_TIMEOUT_S)
    diagnostics = (
        env.http("GET", app_port, "/api/v1/diagnostics", None, HTTP_TIMEOUT_S) if app.ok else None
    )
    grafana = env.http("GET", grafana_port, "/api/health", None, HTTP_TIMEOUT_S)
    return [
        rules.check_app(app, app_port=app_port),
        rules.check_hook_endpoint(hook, app_port=app_port),
        rules.check_diagnostics(diagnostics, cli_version=ccobs.__version__),
        rules.check_alerts(diagnostics),
        rules.check_otlp(env.connect(_HOST, otlp_port, CONNECT_TIMEOUT_S), otlp_port=otlp_port),
        rules.check_grafana(grafana, grafana_port=grafana_port),
    ]


def _is_clone(directory: Path) -> bool:
    try:
        text = (directory / "compose.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return rules.is_ccobs_compose(text)


def _stat_entry(path: Path, stack: Path, *, follow: bool) -> StatEntry:
    name = path.relative_to(stack.parent).as_posix()
    try:
        # lstat: a symbolic link is judged by its own mode (always rwx on Linux).
        status = path.stat() if follow else path.lstat()
    except OSError as error:
        return StatEntry(name, False, 0, reason(error))
    return StatEntry(name, stat.S_ISDIR(status.st_mode), stat.S_IMODE(status.st_mode))


def stack_entries(stack: Path) -> list[StatEntry]:
    """``stack`` and everything under it, with paths relative to its parent. A path
    that cannot be read (or a directory that cannot be listed) is an entry with an
    ``error``, never an exception."""
    entries = [_stat_entry(stack, stack, follow=True)]

    def unlisted(error: OSError) -> None:
        directory = Path(error.filename or stack)
        entries.append(
            StatEntry(
                directory.relative_to(stack.parent).as_posix(),
                True,
                0,
                f"cannot list it: {reason(error)}",
            )
        )

    for root, directories, files in os.walk(stack, onerror=unlisted):
        for name in sorted(directories) + sorted(files):
            entries.append(_stat_entry(Path(root) / name, stack, follow=False))
    return entries


def _clone(env: Environment, root: Path, args: argparse.Namespace) -> list[Check]:
    checks: list[Check] = []
    if env.platform != "win32":
        stack = root / "stack"
        if stack.is_dir():
            checks.append(rules.check_stack_readable(stack_entries(stack)))
        else:
            checks.append(
                Check(
                    "stack-readable",
                    "warn",
                    "no stack/ directory",
                    f"{root} has no stack/ directory; is it your cc-observatory clone?",
                )
            )
    env_path = root / ".env"
    try:
        values: dict[str, str] | None = rules.parse_env_file(
            env_path.read_bytes().decode("utf-8", "replace")
        )
    except FileNotFoundError:
        values = None
    except OSError as error:
        checks.append(
            Check(
                "env-file",
                "warn",
                "the clone's .env cannot be read",
                f"{env_path}: {reason(error)}",
            )
        )
        return checks
    checks.append(
        rules.check_env_file(
            values,
            app_port=args.app_port,
            otlp_port=args.otlp_port,
            grafana_port=args.grafana_port,
        )
    )
    return checks


# Output.

_LABELS: Final = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[INFO]"}
_INDENT: Final = " " * 9


def _print_text(sections: Sequence[tuple[str, Sequence[Check]]], out: TextIO) -> None:
    print(f"ccobs doctor {ccobs.__version__}", file=out)
    for section, checks in sections:
        print(f"\n{section}", file=out)
        for check in checks:
            print(f"  {_LABELS[check.status]} {check.title}", file=out)
            for line in check.detail.splitlines():
                print(f"{_INDENT}{line}", file=out)
            if check.fix is not None:
                print(f"{_INDENT}fix: {check.fix}", file=out)
    counts = rules.summary(check for _, checks in sections for check in checks)
    print(
        "\nSummary: " + ", ".join(f"{count} {status}" for status, count in counts.items()),
        file=out,
    )


def _print_json(sections: Sequence[tuple[str, Sequence[Check]]], out: TextIO) -> None:
    checks = [
        {
            "section": section,
            "id": check.id,
            "status": check.status,
            "title": check.title,
            "detail": check.detail,
            "fix": check.fix,
        }
        for section, items in sections
        for check in items
    ]
    document = {
        "version": ccobs.__version__,
        "checks": checks,
        "summary": rules.summary(check for _, items in sections for check in items),
    }
    # ASCII: any console can show it, and a JSON reader gets the same text back.
    print(json.dumps(document, indent=2, ensure_ascii=True), file=out)


# The command.


def run_doctor(args: argparse.Namespace, *, env: Environment, out: TextIO, err: TextIO) -> int:
    """Run ``ccobs doctor``. Returns 0 when no check fails, 1 when one does, 2 on a
    usage error."""
    tolerate_unencodable(out, err)
    try:
        config_dir = settings_file.resolve_config_dir(args.config_dir, env.environ)
    except ConfigDirError as error:
        print(f"ccobs doctor: {error}", file=err)
        return 2
    stack_arg: str | None = args.stack_dir
    clone: Path | None = None
    if stack_arg is not None:
        clone = Path(stack_arg).expanduser().absolute()
        if not clone.is_dir():
            print(f"ccobs doctor: --stack-dir {clone} is not a directory", file=err)
            return 2
    elif _is_clone(env.cwd):
        clone = env.cwd

    settings = _settings_and_policies(env, settings_file.settings_path(config_dir), args)
    sections: list[tuple[str, Sequence[Check]]] = [
        ("Claude Code", _claude_code(env)),
        ("Settings", settings.checks),
        ("Policies", settings.policies),
        ("Stack", _stack(env, args)),
    ]
    if clone is not None:
        sections.append(("Clone", _clone(env, clone, args)))
    if args.json:
        _print_json(sections, out)
    else:
        _print_text(sections, out)
    return rules.exit_code(check for _, checks in sections for check in checks)
