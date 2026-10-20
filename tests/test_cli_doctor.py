"""Tests for ``ccobs doctor`` (``ccobs.cli.doctor``, ADR 0039): the collectors, the
output and the exit codes.

Every I/O dependency is injected through ``doctor.Environment``: ``which``, the command
runner, the HTTP client, the TCP connect, the registry reader, the platform, the
managed settings directory and plist path, the environment and the working directory.
No test runs a real command, reads the real registry or reaches the network; the
default implementations are tested against fakes (the runner, the registry) and a
loopback server (HTTP, TCP). The autouse fixture points ``CLAUDE_CONFIG_DIR``, ``HOME``
and ``USERPROFILE`` at temporary directories, so no test touches the real ``~/.claude``.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import threading
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, cast

import pytest

import ccobs
from ccobs.cli import build_parser, doctor, main
from ccobs.cli.doctor import Environment, run_doctor
from ccobs.domain.doctor import HttpAnswer, StatEntry
from ccobs.domain.hook_config import hook_settings
from ccobs.domain.telemetry_settings import telemetry_env

ENDPOINT: Final = "http://127.0.0.1:4318"
APP: Final = "http://127.0.0.1:4300"
# A value planted in the user's files and environment, which no output may ever echo.
PLANTED: Final = "planted-value-0001"
SECTIONS: Final = ("Claude Code", "Settings", "Policies", "Stack", "Clone")
ENV_KEYS: Final = (
    "CCOBS_LOKI_RETENTION",
    "CCOBS_HOST_LABEL",
    "CCOBS_APP_PORT",
    "CCOBS_OTLP_HTTP_PORT",
    "CCOBS_GRAFANA_PORT",
)


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude, nor run the default environment."""
    isolation = tmp_path_factory.mktemp("isolation")
    home = isolation / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))

    def no_default() -> Environment:
        raise AssertionError("tests inject the environment")

    monkeypatch.setattr(doctor, "default_environment", no_default)
    return home


def _diagnostics(**change: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": ccobs.__version__,
        "host": "local",
        "started_at_ms": 1,
        "sessions": {"by_state": {"working": 0}, "open_blocks": 0},
        "hooks": {"received": {"Stop": 2}, "dropped": {"invalid": 1, "full": 0}},
        "hook_failures": {
            "available": True,
            "window_s": 3600,
            "executions": 2,
            "non_blocking_error": 0,
            "cancelled": 0,
        },
    }
    document.update(change)
    return document


@dataclass
class Fakes:
    """The injected dependencies, with a healthy machine by default."""

    tmp: Path
    platform: str = "linux"
    which: dict[str, str] = field(
        default_factory=lambda: {
            "curl": "/usr/bin/curl",
            "claude": "/usr/bin/claude",
            "docker": "/usr/bin/docker",
        }
    )
    outputs: dict[tuple[str, ...], str] = field(
        default_factory=lambda: {
            ("/usr/bin/claude", "--version"): "2.1.300 (Claude Code)\n",
            ("/usr/bin/docker", "version", "--format", "{{.Server.Version}}"): "28.3.2\n",
            ("/usr/bin/docker", "compose", "version", "--short"): "2.39.1\n",
            ("/usr/bin/docker", "buildx", "version"): "github.com/docker/buildx v0.26.1 x\n",
        }
    )
    answers: dict[tuple[str, str], HttpAnswer] = field(
        default_factory=lambda: {
            ("GET", f"{APP}/healthz"): HttpAnswer(200),
            ("POST", f"{APP}/hooks"): HttpAnswer(204),
            ("GET", f"{APP}/api/v1/diagnostics"): HttpAnswer(
                200, json.dumps(_diagnostics()).encode()
            ),
            ("GET", "http://127.0.0.1:4330/api/health"): HttpAnswer(200),
        }
    )
    open_ports: set[int] = field(default_factory=lambda: {4318})
    registry: dict[str, str | Exception] = field(default_factory=dict)
    environ: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    runs: list[tuple[tuple[str, ...], float]] = field(default_factory=list)
    requests: list[tuple[str, str, bytes | None, float]] = field(default_factory=list)
    connects: list[tuple[str, int, float]] = field(default_factory=list)

    def environment(self) -> Environment:
        def run(argv: Sequence[str], timeout: float) -> str | None:
            self.runs.append((tuple(argv), timeout))
            return self.outputs.get(tuple(argv))

        def http(
            method: str, port: int, path: str, body: bytes | None, timeout: float
        ) -> HttpAnswer:
            url = f"http://127.0.0.1:{port}{path}"
            self.requests.append((method, url, body, timeout))
            return self.answers.get((method, url), HttpAnswer(None))

        def connect(host: str, port: int, timeout: float) -> bool:
            self.connects.append((host, port, timeout))
            return port in self.open_ports

        def read_registry(hive: str) -> str | None:
            value = self.registry.get(hive)
            if isinstance(value, Exception):
                raise value
            return value

        cwd = self.cwd if self.cwd is not None else self.tmp / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        return Environment(
            which=self.which.get,
            run=run,
            http=http,
            connect=connect,
            read_registry=read_registry,
            platform=self.platform,
            managed_dir=self.tmp / "managed",
            plist_path=self.tmp / "managed.plist",
            environ=self.environ,
            cwd=cwd,
        )


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def _doctor(fakes: Fakes, config_dir: Path | None, *argv: str) -> Run:
    target = [] if config_dir is None else ["--config-dir", str(config_dir)]
    args = build_parser().parse_args(["doctor", *target, *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_doctor(args, env=fakes.environment(), out=out, err=err)
    return Run(code, out.getvalue(), err.getvalue())


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _complete(windows: bool = False, **extra: object) -> dict[str, object]:
    hooks = hook_settings(f"{APP}/hooks", windows=windows)["hooks"]
    return {"hooks": hooks, "env": telemetry_env(ENDPOINT), **extra}


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    _write_json(directory / "settings.json", _complete())
    return directory


def _checks(run: Run) -> dict[str, dict[str, object]]:
    document = json.loads(run.out)
    return {check["id"]: check for check in document["checks"]}


def _status(run: Run, check_id: str) -> object:
    return _checks(run)[check_id]["status"]


# The whole run.


def test_a_healthy_machine_exits_0_with_every_section(fakes: Fakes, config: Path) -> None:
    run = _doctor(fakes, config)

    assert run.code == 0, run.out
    assert run.err == ""
    assert "[FAIL]" not in run.out
    positions = [run.out.index(f"\n{section}\n") for section in SECTIONS[:4]]
    assert positions == sorted(positions)
    assert "\nClone\n" not in run.out
    assert "  [ OK ] curl is on PATH\n         /usr/bin/curl\n" in run.out
    assert "  [INFO] settings sources\n" in run.out
    assert run.out.rstrip().splitlines()[-1].startswith("Summary: ")
    assert " 0 fail," in run.out


def test_the_probes_go_to_loopback_with_short_timeouts(fakes: Fakes, config: Path) -> None:
    _doctor(fakes, config)

    assert ("POST", f"{APP}/hooks", b"{}", 2.0) in fakes.requests
    assert all(timeout == 2.0 for _, _, _, timeout in fakes.requests)
    assert fakes.connects == [("127.0.0.1", 4318, 1.0)]
    assert all(timeout == 10.0 for _, timeout in fakes.runs)


def test_json_output(fakes: Fakes, config: Path) -> None:
    run = _doctor(fakes, config, "--json")

    document = json.loads(run.out)
    assert run.code == 0
    assert set(document) == {"version", "checks", "summary"}
    assert document["version"] == ccobs.__version__
    first = document["checks"][0]
    assert set(first) == {"section", "id", "status", "title", "detail", "fix"}
    assert first["section"] == "Claude Code"
    assert sum(document["summary"].values()) == len(document["checks"])
    assert document["summary"]["fail"] == 0


def test_a_failure_exits_1(fakes: Fakes, tmp_path: Path) -> None:
    run = _doctor(fakes, tmp_path / "empty-config")

    assert run.code == 1
    assert "  [FAIL] Claude Code's user settings do not exist\n" in run.out
    assert "         fix: run ccobs setup\n" in run.out
    assert "[INFO] cc-observatory's hooks" in run.out


def test_a_warning_alone_exits_0(fakes: Fakes, config: Path) -> None:
    del fakes.which["curl"]

    run = _doctor(fakes, config)

    assert run.code == 0
    assert "  [WARN] curl is not on PATH\n" in run.out


def test_unreadable_settings_fail_without_content(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "settings.json").write_text('{"env": {"K": "' + PLANTED + '"}', encoding="utf-8")

    run = _doctor(fakes, directory, "--json")

    assert run.code == 1
    assert _status(run, "settings-file") == "fail"
    assert "not valid JSON" in str(_checks(run)["settings-file"]["detail"])
    assert PLANTED not in run.out


def test_the_config_dir_comes_from_the_environment(
    fakes: Fakes, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.environ = {"CLAUDE_CONFIG_DIR": str(config)}

    run = _doctor(fakes, None, "--json")

    assert _status(run, "settings-file") == "ok"


def test_a_relative_config_dir_variable_is_a_usage_error(fakes: Fakes) -> None:
    fakes.environ = {"CLAUDE_CONFIG_DIR": "relative"}

    run = _doctor(fakes, None)

    assert run.code == 2
    assert run.out == ""
    assert run.err.startswith("ccobs doctor: CLAUDE_CONFIG_DIR must be an absolute path")


def test_a_bad_port_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["doctor", "--grafana-port", "0"])

    assert raised.value.code == 2


def test_a_stack_dir_that_is_not_a_directory_is_a_usage_error(
    fakes: Fakes, config: Path, tmp_path: Path
) -> None:
    run = _doctor(fakes, config, "--stack-dir", str(tmp_path / "missing"))

    assert run.code == 2
    assert "--stack-dir" in run.err


def test_ports_flags_reach_every_probe(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / "config"
    _write_json(directory / "settings.json", {})

    _doctor(fakes, directory, "--app-port", "4400", "--otlp-port", "4319", "--grafana-port", "4331")

    urls = {url for _, url, _, _ in fakes.requests}
    assert urls == {
        "http://127.0.0.1:4400/healthz",
        "http://127.0.0.1:4400/hooks",
        "http://127.0.0.1:4331/api/health",
    }
    assert fakes.connects == [("127.0.0.1", 4319, 1.0)]


# Claude Code and the host.


def test_nothing_installed(fakes: Fakes, config: Path) -> None:
    fakes.which = {}

    run = _doctor(fakes, config, "--json")

    assert fakes.runs == []
    assert _status(run, "curl") == "warn"
    assert _status(run, "claude-version") == "warn"
    assert _checks(run)["docker"]["title"] == "Docker was not found"


def test_docker_daemon_down(fakes: Fakes, config: Path) -> None:
    del fakes.outputs[("/usr/bin/docker", "version", "--format", "{{.Server.Version}}")]

    run = _doctor(fakes, config, "--json")

    assert _status(run, "docker") == "warn"
    assert "daemon did not answer" in str(_checks(run)["docker"]["detail"])


@pytest.mark.parametrize(("platform", "status"), [("linux", "warn"), ("darwin", "ok")])
def test_old_engine_warns_on_linux_only(
    fakes: Fakes, config: Path, platform: str, status: str
) -> None:
    fakes.platform = platform
    fakes.outputs[("/usr/bin/docker", "version", "--format", "{{.Server.Version}}")] = "27.1.0"

    assert _status(_doctor(fakes, config, "--json"), "docker") == status


def test_windows_expects_the_windows_curl_hook(fakes: Fakes, tmp_path: Path) -> None:
    fakes.platform = "win32"
    directory = tmp_path / "config"
    _write_json(directory / "settings.json", _complete(windows=True))

    assert _status(_doctor(fakes, directory, "--json"), "hooks") == "ok"


# Settings sources.


def test_environment_rules_read_the_shell(fakes: Fakes, config: Path) -> None:
    fakes.environ = {
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_METRICS_INCLUDE_SESSION_ID": "false",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": PLANTED,
        "OTEL_METRICS_EXPORTER": PLANTED,
    }

    run = _doctor(fakes, config, "--json")

    for check_id in ("content-variables", "session-id", "per-signal-endpoints"):
        assert _status(run, check_id) == "warn"
        assert "your shell environment" in str(_checks(run)[check_id]["detail"])
    assert _status(run, "shell-overrides") == "info"
    assert PLANTED not in run.out


def test_project_files_count(fakes: Fakes, config: Path) -> None:
    cwd = fakes.tmp / "project"
    fakes.cwd = cwd
    _write_json(
        cwd / ".claude" / "settings.json",
        {"disableAllHooks": True, "env": {"OTEL_METRICS_INCLUDE_SESSION_ID": "false"}},
    )
    (cwd / ".claude" / "settings.local.json").write_text("{", encoding="utf-8")

    run = _doctor(fakes, config, "--json")

    assert _status(run, "disable-all-hooks") == "warn"
    assert _status(run, "session-id") == "warn"
    sources = str(_checks(run)["settings-sources"]["detail"])
    assert f"could not read {cwd / '.claude' / 'settings.local.json'}" in sources


def test_managed_content_variables_warn(fakes: Fakes, config: Path) -> None:
    fakes.platform = "win32"
    managed = fakes.tmp / "managed" / "managed-settings.json"
    _write_json(managed, {"env": {"OTEL_LOG_TOOL_DETAILS": "1"}})
    fakes.registry = {"HKCU": json.dumps({"env": {"OTEL_LOG_USER_PROMPTS": PLANTED}})}

    run = _doctor(fakes, config, "--json")

    check = _checks(run)["content-variables"]
    assert check["status"] == "warn"
    assert f"OTEL_LOG_TOOL_DETAILS in {managed}" in str(check["detail"])
    assert "OTEL_LOG_USER_PROMPTS in HKCU\\SOFTWARE\\Policies\\ClaudeCode" in str(check["detail"])
    assert PLANTED not in run.out


def test_managed_session_id_false_warns(fakes: Fakes, config: Path) -> None:
    # Managed settings win over the user's, so a managed false drops session IDs for
    # every session (ADR 0016).
    fakes.platform = "win32"
    managed = fakes.tmp / "managed" / "managed-settings.json"
    _write_json(managed, {"env": {"OTEL_METRICS_INCLUDE_SESSION_ID": "false"}})

    run = _doctor(fakes, config, "--json")

    check = _checks(run)["session-id"]
    assert check["status"] == "warn"
    assert str(managed) in str(check["detail"])


def test_project_content_variables_are_ignored_like_claude_code_does(
    fakes: Fakes, config: Path
) -> None:
    cwd = fakes.tmp / "project"
    fakes.cwd = cwd
    _write_json(cwd / ".claude" / "settings.json", {"env": {"OTEL_LOG_USER_PROMPTS": "1"}})

    assert _status(_doctor(fakes, config, "--json"), "content-variables") == "ok"


def test_the_user_file_is_not_read_twice_as_a_project_file(
    fakes: Fakes, isolated_home: Path
) -> None:
    config = isolated_home / ".claude"
    _write_json(config / "settings.json", _complete(disableAllHooks=True))
    fakes.cwd = isolated_home

    run = _doctor(fakes, config, "--json")

    check = _checks(run)["disable-all-hooks"]
    assert (check["status"], check["title"]) == ("fail", "all hooks are off")


def test_the_allowlist_fix_names_the_user_file(fakes: Fakes, config: Path) -> None:
    managed = fakes.tmp / "managed" / "managed-settings.json"
    _write_json(managed, {"allowedHttpHookUrls": ["https://hooks.example.com/*"]})

    run = _doctor(fakes, config, "--json")

    check = _checks(run)["http-hook-allowlist"]
    assert check["status"] == "fail"
    assert str(config / "settings.json") in str(check["fix"])
    assert str(managed) not in str(check["fix"])


# Managed sources on each OS.


def test_managed_files_on_linux(fakes: Fakes, config: Path) -> None:
    managed = fakes.tmp / "managed"
    _write_json(managed / "managed-settings.json", {"allowManagedHooksOnly": True})
    _write_json(
        managed / "managed-settings.d" / "10-env.json", {"env": {"OTEL_LOGS_EXPORTER": "none"}}
    )
    (managed / "managed-settings.d" / "20-bad.json").write_text("[", encoding="utf-8")
    (managed / "managed-settings.d" / ".hidden.json").write_text("[", encoding="utf-8")
    (managed / "managed-settings.d" / "notes.txt").write_text("x", encoding="utf-8")

    run = _doctor(fakes, config, "--json")

    assert run.code == 1
    assert _status(run, "managed-hooks-only") == "fail"
    assert _status(run, "managed-env") == "fail"
    sources = str(_checks(run)["settings-sources"]["detail"])
    assert str(managed / "managed-settings.json") in sources
    assert f"could not read {managed / 'managed-settings.d' / '20-bad.json'}" in sources
    assert ".hidden" not in sources
    assert "notes.txt" not in sources


def test_an_unreadable_drop_in_directory_is_noted(fakes: Fakes, config: Path) -> None:
    managed = fakes.tmp / "managed"
    managed.mkdir()
    (managed / "managed-settings.d").write_text("not a directory", encoding="utf-8")

    run = _doctor(fakes, config, "--json")

    assert f"could not read {managed / 'managed-settings.d'}" in str(
        _checks(run)["settings-sources"]["detail"]
    )


def test_a_drop_in_removed_before_it_is_read_is_skipped(
    fakes: Fakes, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drop_ins = fakes.tmp / "managed" / "managed-settings.d"
    real_scandir = os.scandir

    def scandir(path: Path) -> list[object]:
        if Path(path) == drop_ins:
            return [types.SimpleNamespace(name="gone.json")]
        return list(real_scandir(path))

    monkeypatch.setattr(os, "scandir", scandir)

    run = _doctor(fakes, config, "--json")

    assert "gone.json" not in str(_checks(run)["settings-sources"]["detail"])


def test_the_registry_on_windows(fakes: Fakes, tmp_path: Path) -> None:
    fakes.platform = "win32"
    fakes.registry = {
        "HKLM": json.dumps({"disableAllHooks": True}),
        "HKCU": json.dumps({"allowManagedHooksOnly": True}),
    }
    directory = tmp_path / "config"
    _write_json(directory / "settings.json", _complete(windows=True))

    run = _doctor(fakes, directory, "--json")

    assert _status(run, "disable-all-hooks") == "fail"
    assert _status(run, "managed-hooks-only") == "warn"
    sources = str(_checks(run)["settings-sources"]["detail"])
    assert "HKLM\\SOFTWARE\\Policies\\ClaudeCode" in sources
    assert "HKCU\\SOFTWARE\\Policies\\ClaudeCode" in sources


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (OSError(5, "Access is denied"), "Access is denied"),
        (ValueError("the Settings value is not a string"), "the Settings value is not a string"),
        ("{", "not valid JSON"),
        ("[1]", "not a JSON object"),
    ],
)
def test_unreadable_registry_values_are_noted(
    fakes: Fakes, config: Path, value: str | Exception, reason: str
) -> None:
    fakes.platform = "win32"
    fakes.registry = {"HKLM": value}

    run = _doctor(fakes, config, "--json")

    detail = str(_checks(run)["settings-sources"]["detail"])
    assert "could not read HKLM\\SOFTWARE\\Policies\\ClaudeCode: " + reason in detail


def test_the_registry_is_not_read_elsewhere(fakes: Fakes, config: Path) -> None:
    fakes.registry = {"HKLM": json.dumps({"disableAllHooks": True})}

    assert _status(_doctor(fakes, config, "--json"), "disable-all-hooks") == "ok"


def test_the_plist_on_macos(fakes: Fakes, config: Path) -> None:
    fakes.platform = "darwin"
    (fakes.tmp / "managed.plist").write_bytes(
        plistlib.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}})
    )

    run = _doctor(fakes, config, "--json")

    assert _status(run, "managed-env") == "fail"
    assert str(fakes.tmp / "managed.plist") in str(_checks(run)["managed-env"]["detail"])


@pytest.mark.parametrize("content", [b"not a plist", plistlib.dumps([1, 2])])
def test_an_unreadable_plist_is_noted(fakes: Fakes, config: Path, content: bytes) -> None:
    fakes.platform = "darwin"
    (fakes.tmp / "managed.plist").write_bytes(content)

    run = _doctor(fakes, config, "--json")

    assert f"could not read {fakes.tmp / 'managed.plist'}" in str(
        _checks(run)["settings-sources"]["detail"]
    )


def test_an_unreadable_plist_file_is_noted(fakes: Fakes, config: Path) -> None:
    fakes.platform = "darwin"
    (fakes.tmp / "managed.plist").mkdir()

    run = _doctor(fakes, config, "--json")

    assert f"could not read {fakes.tmp / 'managed.plist'}" in str(
        _checks(run)["settings-sources"]["detail"]
    )


def test_the_plist_is_not_read_elsewhere(fakes: Fakes, config: Path) -> None:
    (fakes.tmp / "managed.plist").write_bytes(plistlib.dumps({"disableAllHooks": True}))

    assert _status(_doctor(fakes, config, "--json"), "disable-all-hooks") == "ok"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", Path("/Library/Application Support/ClaudeCode")),
        ("win32", Path("C:\\Program Files\\ClaudeCode")),
        ("linux", Path("/etc/claude-code")),
        ("freebsd14", Path("/etc/claude-code")),
    ],
)
def test_managed_dir(platform: str, expected: Path) -> None:
    assert doctor.managed_dir(platform) == expected


# Stack.


def test_the_app_down_skips_the_diagnostics(fakes: Fakes, config: Path) -> None:
    fakes.answers = {}
    fakes.open_ports = set()

    run = _doctor(fakes, config, "--json")

    assert run.code == 1
    assert _status(run, "app") == "fail"
    assert _status(run, "hook-endpoint") == "fail"
    assert _status(run, "diagnostics") == "info"
    assert _status(run, "otlp") == "fail"
    assert _status(run, "grafana") == "warn"
    assert all("diagnostics" not in url for _, url, _, _ in fakes.requests)


def test_diagnostics_warnings_reach_the_output(fakes: Fakes, config: Path) -> None:
    document = _diagnostics(version="0.0.1")
    fakes.answers[("GET", f"{APP}/api/v1/diagnostics")] = HttpAnswer(
        200, json.dumps(document).encode()
    )

    run = _doctor(fakes, config)

    assert f"CLI {ccobs.__version__}, app 0.0.1" in run.out


# Clone.


def _clone(root: Path, env: str | None = None) -> Path:
    (root / "stack" / "collector").mkdir(parents=True)
    (root / "stack" / "collector" / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    (root / "compose.yaml").write_text("name: cc-observatory\nservices: {}\n", encoding="utf-8")
    if env is not None:
        (root / ".env").write_text(env, encoding="utf-8")
    return root


def test_a_clone_in_the_cwd_adds_the_clone_section(fakes: Fakes, config: Path) -> None:
    fakes.cwd = _clone(fakes.tmp / "clone", env=f"CCOBS_APP_PORT=4400\nOTHER={PLANTED}\n")

    run = _doctor(fakes, config)

    assert "\nClone\n" in run.out
    assert "  [ OK ] stack/ is readable by the containers\n" in run.out
    assert "CCOBS_APP_PORT is 4400 in .env" in run.out
    assert PLANTED not in run.out


def test_the_stack_dir_flag(fakes: Fakes, config: Path) -> None:
    root = _clone(fakes.tmp / "elsewhere")

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    assert _status(run, "stack-readable") == "ok"
    assert _checks(run)["env-file"]["detail"] == (
        "No .env: the stack runs with compose.yaml's defaults."
    )


def test_stack_readability_is_not_checked_on_windows(fakes: Fakes, config: Path) -> None:
    fakes.platform = "win32"
    root = _clone(fakes.tmp / "clone")

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    assert "stack-readable" not in _checks(run)
    assert "env-file" in _checks(run)


def test_a_stack_dir_without_stack_warns(fakes: Fakes, config: Path) -> None:
    root = fakes.tmp / "bare"
    root.mkdir()

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    assert _status(run, "stack-readable") == "warn"


def test_stack_entries_are_relative_to_the_clone(fakes: Fakes) -> None:
    root = _clone(fakes.tmp / "clone")

    entries = doctor.stack_entries(root / "stack")

    assert [(entry.path, entry.is_dir) for entry in entries] == [
        ("stack", True),
        ("stack/collector", True),
        ("stack/collector/config.yaml", False),
    ]


def test_stack_entries_that_cannot_be_read_are_reported(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _clone(fakes.tmp / "clone")
    (root / "stack" / "locked").mkdir()
    real_lstat = Path.lstat
    real_scandir = os.scandir

    def lstat(self: Path) -> os.stat_result:
        if self.name == "config.yaml":
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    def scandir(path: str) -> object:
        if Path(path).name == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(os, "scandir", scandir)

    entries = doctor.stack_entries(root / "stack")

    errors = {entry.path: entry.error for entry in entries if entry.error is not None}
    assert errors == {
        "stack/collector/config.yaml": "Permission denied",
        "stack/locked": "cannot list it: Permission denied",
    }


def test_a_stack_directory_that_cannot_be_read_is_reported(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _clone(fakes.tmp / "clone")
    real_stat = Path.stat

    def stat(self: Path, **kwargs: bool) -> os.stat_result:
        if self.name == "stack":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    [entry, *_] = doctor.stack_entries(root / "stack")

    assert entry == StatEntry("stack", False, 0, "Permission denied")


def test_unreadable_stack_entries_reach_the_output(
    fakes: Fakes, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _clone(fakes.tmp / "clone")
    real_lstat = Path.lstat

    def lstat(self: Path) -> os.stat_result:
        if self.name == "config.yaml":
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    check = _checks(run)["stack-readable"]
    assert check["status"] in ("warn", "fail")
    assert "Could not check: stack/collector/config.yaml (Permission denied)" in str(
        check["detail"]
    )


def test_an_unreadable_env_file_warns(fakes: Fakes, config: Path) -> None:
    root = _clone(fakes.tmp / "clone")
    (root / ".env").mkdir()

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    check = _checks(run)["env-file"]
    assert check["status"] == "warn"
    assert check["title"] == "the clone's .env cannot be read"


def test_an_env_file_that_is_not_utf8_is_read_leniently(fakes: Fakes, config: Path) -> None:
    root = _clone(fakes.tmp / "clone")
    (root / ".env").write_bytes(b"\xff\xfe\nCCOBS_HOST_LABEL=bad label\n")

    run = _doctor(fakes, config, "--stack-dir", str(root), "--json")

    assert _status(run, "env-file") == "fail"


@pytest.mark.parametrize(
    "compose", ["name: something-else\n", None, "directory"], ids=["other", "none", "unreadable"]
)
def test_other_directories_are_not_clones(fakes: Fakes, config: Path, compose: str | None) -> None:
    cwd = fakes.tmp / "work"
    cwd.mkdir()
    if compose == "directory":
        (cwd / "compose.yaml").mkdir()
    elif compose is not None:
        (cwd / "compose.yaml").write_text(compose, encoding="utf-8")
    fakes.cwd = cwd

    run = _doctor(fakes, config, "--json")

    assert "env-file" not in _checks(run)


# Privacy.


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_no_output_quotes_a_planted_value(fakes: Fakes, tmp_path: Path, platform: str) -> None:
    fakes.platform = platform
    directory = tmp_path / "config"
    planted_env = dict.fromkeys(
        (
            *telemetry_env(ENDPOINT),
            "OTEL_LOG_USER_PROMPTS",
            "BETA_TRACING_ENDPOINT",
            "OTEL_METRICS_INCLUDE_SESSION_ID",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
            "ANTHROPIC_API_KEY",
        ),
        PLANTED,
    )
    planted_settings = {
        "env": planted_env,
        "allowedHttpHookUrls": [PLANTED],
        "apiKeyHelper": PLANTED,
        "disableAllHooks": PLANTED,
        "allowManagedHooksOnly": PLANTED,
        "hooks": {"Stop": [{"hooks": [{"type": "http", "url": f"http://{PLANTED}/hooks"}]}]},
    }
    invalid = '{"env": {"ANTHROPIC_API_KEY": "' + PLANTED + '"'
    _write_json(directory / "settings.json", planted_settings)
    fakes.environ = dict(planted_env)
    managed = fakes.tmp / "managed"
    _write_json(managed / "managed-settings.json", planted_settings)
    _write_json(managed / "managed-settings.d" / "10-planted.json", planted_settings)
    (managed / "managed-settings.d" / "20-invalid.json").write_text(invalid, encoding="utf-8")
    fakes.registry = {"HKLM": json.dumps(planted_settings), "HKCU": invalid}
    (fakes.tmp / "managed.plist").write_bytes(plistlib.dumps(planted_settings))
    env_lines = [
        *(f"{key}={PLANTED}" for key in (*planted_env, *ENV_KEYS)),
        *(f'{key}="{PLANTED} # x" # {PLANTED}' for key in ENV_KEYS),
        *(f"{key}={PLANTED}\t# {PLANTED}" for key in ENV_KEYS),
    ]
    root = _clone(fakes.tmp / "clone", env="\n".join(env_lines))
    _write_json(root / ".claude" / "settings.json", planted_settings)
    (root / ".claude" / "settings.local.json").write_text(invalid, encoding="utf-8")
    fakes.cwd = root

    text = _doctor(fakes, directory)
    as_json = _doctor(fakes, directory, "--json")

    assert text.code == as_json.code == 1
    assert PLANTED not in text.out + text.err
    assert PLANTED not in as_json.out + as_json.err


# Encoding.

CJK: Final = "\u8a2d\u5b9a"  # not in cp1252


def _cp1252() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252"), raw


def test_text_output_survives_a_console_that_cannot_encode_the_path(
    fakes: Fakes, tmp_path: Path
) -> None:
    directory = tmp_path / f"config-{CJK}"
    _write_json(directory / "settings.json", _complete())
    args = build_parser().parse_args(["doctor", "--config-dir", str(directory)])
    out, raw = _cp1252()
    err, _ = _cp1252()

    code = run_doctor(args, env=fakes.environment(), out=out, err=err)

    out.flush()
    text = raw.getvalue().decode("cp1252")
    assert code == 0
    assert "config-\\u8a2d\\u5b9a" in text
    assert "Summary: " in text


def test_json_output_is_ascii(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / f"config-{CJK}"
    _write_json(directory / "settings.json", _complete())
    args = build_parser().parse_args(["doctor", "--config-dir", str(directory), "--json"])
    out, raw = _cp1252()

    code = run_doctor(args, env=fakes.environment(), out=out, err=io.StringIO())

    out.flush()
    assert code == 0
    assert raw.getvalue().isascii()
    checks = {check["id"]: check for check in json.loads(raw.getvalue())["checks"]}
    assert checks["settings-file"]["detail"] == str(directory / "settings.json")


def test_usage_errors_survive_a_console_that_cannot_encode_the_path(
    fakes: Fakes, config: Path, tmp_path: Path
) -> None:
    missing = tmp_path / f"missing-{CJK}"
    args = build_parser().parse_args(
        ["doctor", "--config-dir", str(config), "--stack-dir", str(missing)]
    )
    err, raw = _cp1252()

    code = run_doctor(args, env=fakes.environment(), out=io.StringIO(), err=err)

    err.flush()
    assert code == 2
    assert "missing-\\u8a2d\\u5b9a" in raw.getvalue().decode("cp1252")


# The default implementations.


def test_default_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.undo()  # the autouse fixture's guard; HOME stays out of this test
    monkeypatch.chdir(tmp_path)

    env = doctor.default_environment()

    assert env.platform == sys.platform
    assert env.managed_dir == doctor.managed_dir(sys.platform)
    assert env.plist_path == doctor.PLIST_PATH
    assert env.cwd == tmp_path
    assert env.run is doctor.run_command
    assert env.http is doctor.http_request
    assert env.connect is doctor.tcp_connect
    assert env.read_registry is doctor.read_registry


class _FakePopen:
    """A stand-in for ``subprocess.Popen``; ``hang`` makes ``communicate`` time out."""

    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.killed = False
        self.communicate_calls: list[float | None] = []

    stdout_bytes: bytes = b""
    final: int = 0
    hang: bool = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, None]:
        self.communicate_calls.append(timeout)
        if self.hang:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0.0)
        self.returncode = self.final
        return self.stdout_bytes, None

    def kill(self) -> None:
        self.killed = True


def _fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    returncode: int = 0,
    hang: bool = False,
    error: OSError | None = None,
) -> list[_FakePopen]:
    created: list[_FakePopen] = []

    class Popen(_FakePopen):
        stdout_bytes = stdout
        final = returncode

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            if error is not None:
                raise error
            super().__init__(argv, **kwargs)
            self.hang = hang
            created.append(self)

    monkeypatch.setattr(subprocess, "Popen", Popen)
    return created


def test_run_command_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _fake_popen(monkeypatch, stdout="2.1.300 \u00e9\n".encode())

    assert doctor.run_command(["/bin/claude", "--version"], 1.0) == "2.1.300 \u00e9\n"
    [process] = created
    assert process.argv == ["/bin/claude", "--version"]
    assert process.kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
    }
    assert process.communicate_calls == [1.0]


def test_run_command_is_none_on_a_failure_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_popen(monkeypatch, stdout=b"client only", returncode=1)

    assert doctor.run_command(["docker", "version"], 1.0) is None


def test_run_command_is_none_when_it_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_popen(monkeypatch, error=FileNotFoundError(2, "not found"))

    assert doctor.run_command(["missing"], 1.0) is None


def test_run_command_kills_a_hung_process_without_waiting_on_its_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On Windows a grandchild (node.exe behind claude.cmd, a docker CLI plugin) can
    # keep the pipe open after the kill: a second communicate() would never return.
    created = _fake_popen(monkeypatch, hang=True)

    assert doctor.run_command(["hang"], 0.01) is None
    [process] = created
    assert process.killed
    assert process.communicate_calls == [0.01]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200 if self.path == "/ok" else 500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        received = self.rfile.read(length)
        typed = self.headers["Content-Type"] == "application/json"
        self.send_response(204 if typed and received == b"{}" else 400)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server_port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_http_request_get(server_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy from the environment is never used for loopback.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")

    assert doctor.http_request("GET", server_port, "/ok", None, 2.0) == HttpAnswer(
        200, b'{"ok": true}'
    )


def test_http_request_post_json(server_port: int) -> None:
    assert doctor.http_request("POST", server_port, "/hooks", b"{}", 2.0).status == 204


def test_http_request_does_not_follow_redirects(server_port: int) -> None:
    assert doctor.http_request("GET", server_port, "/redirect", None, 2.0).status == 302


def test_http_request_error_status(server_port: int) -> None:
    assert doctor.http_request("GET", server_port, "/missing", None, 2.0).status == 500


def test_http_request_no_answer() -> None:
    assert doctor.http_request("GET", 1, "not a path", None, 2.0) == HttpAnswer(None)
    assert doctor.http_request("GET", _closed_port(), "/", None, 2.0) == HttpAnswer(None)


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return cast(int, probe.getsockname()[1])


def test_tcp_connect() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        assert doctor.tcp_connect("127.0.0.1", port, 1.0) is True
    assert doctor.tcp_connect("127.0.0.1", _closed_port(), 1.0) is False


# The registry reader, against a fake winreg module.


@dataclass
class _Key:
    root: int

    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *args: object) -> None:
        pass


HKLM_ROOT: Final = 1
HKCU_ROOT: Final = 2


def _fake_winreg(
    monkeypatch: pytest.MonkeyPatch, values: Mapping[int, tuple[object, int] | None]
) -> list[tuple[int, str]]:
    opened: list[tuple[int, str]] = []

    def open_key(root: int, path: str) -> _Key:
        opened.append((root, path))
        if values.get(root) is None:
            raise FileNotFoundError(2, "missing")
        return _Key(root)

    def query_value_ex(key: _Key, name: str) -> tuple[object, int]:
        if name != "Settings":
            raise FileNotFoundError(2, "missing")
        return cast(tuple[object, int], values[key.root])

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=HKLM_ROOT,
        HKEY_CURRENT_USER=HKCU_ROOT,
        REG_SZ=10,
        REG_EXPAND_SZ=11,
        OpenKey=open_key,
        QueryValueEx=query_value_ex,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return opened


def test_read_registry_reads_the_settings_value(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _fake_winreg(monkeypatch, {HKLM_ROOT: ("{}", 10), HKCU_ROOT: ('{"a": 1}', 11)})

    assert doctor.read_registry("HKLM") == "{}"
    assert doctor.read_registry("HKCU") == '{"a": 1}'
    assert opened == [
        (HKLM_ROOT, "SOFTWARE\\Policies\\ClaudeCode"),
        (HKCU_ROOT, "SOFTWARE\\Policies\\ClaudeCode"),
    ]


def test_read_registry_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_winreg(monkeypatch, {})

    assert doctor.read_registry("HKLM") is None


@pytest.mark.parametrize("value", [(b"{}", 3), (7, 10)])
def test_read_registry_refuses_a_value_that_is_not_a_string(
    monkeypatch: pytest.MonkeyPatch, value: tuple[object, int]
) -> None:
    _fake_winreg(monkeypatch, {HKLM_ROOT: value})

    with pytest.raises(ValueError, match="not a string"):
        doctor.read_registry("HKLM")


def test_read_registry_elsewhere_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert doctor.read_registry("HKLM") is None


# Wiring.


def test_main_runs_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fakes: Fakes,
    config: Path,
) -> None:
    monkeypatch.setattr(doctor, "default_environment", fakes.environment)

    code = main(["doctor", "--config-dir", str(config), "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["summary"]["fail"] == 0


def test_help_lists_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "doctor" in capsys.readouterr().out


def test_doctor_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["doctor", "--help"])

    out = capsys.readouterr().out
    for option in (
        "--config-dir",
        "--app-port",
        "--otlp-port",
        "--grafana-port",
        "--stack-dir",
        "--json",
    ):
        assert option in out


def test_fakes_are_replaceable(fakes: Fakes) -> None:
    # Environment is a frozen dataclass: tests can swap one dependency.
    env = replace(fakes.environment(), platform="darwin")

    assert env.platform == "darwin"
