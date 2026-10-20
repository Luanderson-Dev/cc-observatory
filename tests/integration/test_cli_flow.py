"""``ccobs setup``, ``doctor``, ``status`` and ``uninstall`` against the isolated
``ccobs-it`` compose project (ADRs 0037 and 0039).

Needs Docker: ``uv run pytest -m integration --no-cov``. The CLI runs in process through
``ccobs.cli.main``, on a settings file in a temporary ``--config-dir`` and with the
project's ports (24xxx), never the user's stack or ``~/.claude``: the autouse fixture
points ``CLAUDE_CONFIG_DIR``, ``HOME`` and ``USERPROFILE`` at a temporary directory, as
the CLI's unit tests do, and runs in a temporary working directory, so doctor reads no
project ``.claude/`` and no clone ``.env``. Each test runs its own setup, so none
depends on the order of the tests or of the other integration test files.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import pytest
from app_support import detail, items, mapping, poll

from ccobs.cli import main
from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.telemetry_settings import telemetry_env
from tools.stack.replay import load_scenario, prepare_hooks, send_hooks

pytestmark = pytest.mark.integration

LOOPBACK: Final = "127.0.0.1"
# A value the user keeps in their own env block: no command may ever print it.
USER_VALUE: Final = "ccobs-it-placeholder-secret-7f3a"
FOREIGN_HOOK: Final[dict[str, object]] = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "~/bin/audit.sh", "timeout": 10}],
}
# A user settings file as one looks before setup: a model, permissions, a hook of the
# user's own with a matcher, and a variable of their own holding a secret.
ORIGINAL: Final[dict[str, object]] = {
    "model": "sonnet",
    "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(./.env)"]},
    "hooks": {"PreToolUse": [FOREIGN_HOOK]},
    "env": {"EXAMPLE_SERVICE_TOKEN": USER_VALUE},
    "includeCoAuthoredBy": False,
}
# Doctor's checks that must be ok against a running stack after setup.
OK_CHECKS: Final = (
    "settings-file",
    "hooks",
    "telemetry-env",
    "app",
    "hook-endpoint",
    "otlp",
    "grafana",
)
NO_FAILURE_SECTIONS: Final = ("Settings", "Stack")
# One recorded hook line, from a sanitized fixture, so that a session exists.
SCENARIO: Final = "headless-edit-commit-tasks"
# Seconds for the app to store a posted hook and for status to report it.
STORED_DEADLINE: Final = 15.0
UNREACHABLE: Final = "did not answer"


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude, a project's .claude/ or a clone's .env,
    nor read telemetry variables exported in the developer's shell."""
    isolation = tmp_path_factory.mktemp("isolation")
    home = isolation / "home"
    home.mkdir()
    workdir = isolation / "work"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))
    for name in list(os.environ):
        if name.upper().startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(workdir)
    return home


def _port(url: str) -> int:
    port = urlsplit(url).port
    assert port is not None, url
    return port


def _unused_port() -> int:
    """A loopback port nothing listens on (the OS picks it; it is closed again)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def ports(app_url: str, otlp_http_url: str, grafana_url: str) -> dict[str, int]:
    return {
        "app": _port(app_url),
        "otlp": _port(otlp_http_url),
        "grafana": _port(grafana_url),
    }


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A Claude Code configuration directory holding ``ORIGINAL``."""
    directory = tmp_path / "claude-config"
    directory.mkdir()
    _settings_path(directory).write_text(
        json.dumps(ORIGINAL, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return directory


def _settings_path(directory: Path) -> Path:
    return directory / "settings.json"


def _state_path(directory: Path) -> Path:
    return directory / "ccobs" / "state.json"


def _read(directory: Path) -> dict[str, object]:
    return mapping(json.loads(_settings_path(directory).read_bytes()), "settings")


def _ordered_keys(value: object) -> object:
    """``value`` with every object as a list of (key, value) pairs, so that a comparison
    also checks the order of the keys."""
    if isinstance(value, dict):
        return [(key, _ordered_keys(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [_ordered_keys(item) for item in value]
    return value


type Cli = Callable[..., tuple[int, str, str]]


@pytest.fixture
def ccobs(capsys: pytest.CaptureFixture[str]) -> Cli:
    """Run ``ccobs`` in process; returns the exit code, stdout and stderr."""

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def _target(config: Path, ports: dict[str, int], *, grafana: bool = True) -> list[str]:
    argv = [
        "--config-dir",
        str(config),
        "--app-port",
        str(ports["app"]),
        "--otlp-port",
        str(ports["otlp"]),
    ]
    return [*argv, "--grafana-port", str(ports["grafana"])] if grafana else argv


def _set_up(ccobs: Cli, config: Path, ports: dict[str, int]) -> tuple[str, str]:
    code, out, err = ccobs("setup", *_target(config, ports, grafana=False))
    assert code == 0, err
    return out, err


def test_setup_writes_the_hooks_and_telemetry_and_keeps_the_users_entries(
    ccobs: Cli, config_dir: Path, ports: dict[str, int]
) -> None:
    out, err = _set_up(ccobs, config_dir, ports)

    # The app is up, so setup does not warn that it did not answer.
    assert UNREACHABLE not in err, err
    settings = _read(config_dir)
    expected_hooks = mapping(
        hook_settings(
            f"http://{LOOPBACK}:{ports['app']}{HOOK_PATH}", windows=sys.platform == "win32"
        )["hooks"],
        "hooks",
    )
    hooks = {
        event: items(groups, event) for event, groups in mapping(settings["hooks"], "hooks").items()
    }
    # Setup appends its group to each event, after the user's own.
    assert items(hooks["PreToolUse"], "PreToolUse")[0] == FOREIGN_HOOK
    ours = {
        event: [group for group in groups if group != FOREIGN_HOOK]
        for event, groups in hooks.items()
    }
    assert ours == expected_hooks
    env = mapping(settings["env"], "env")
    assert env == {
        "EXAMPLE_SERVICE_TOKEN": USER_VALUE,
        **telemetry_env(f"http://{LOOPBACK}:{ports['otlp']}"),
    }
    for key in ("model", "permissions", "includeCoAuthoredBy"):
        assert settings[key] == ORIGINAL[key], key
    assert _state_path(config_dir).is_file()
    assert USER_VALUE not in out + err


def _doctor(ccobs: Cli, config: Path, ports: dict[str, int]) -> tuple[int, dict[str, object], str]:
    code, out, err = ccobs("doctor", *_target(config, ports), "--json")
    return code, mapping(json.loads(out), "doctor"), out + err


def test_doctor_finds_nothing_failing_after_setup(
    ccobs: Cli, config_dir: Path, ports: dict[str, int]
) -> None:
    _set_up(ccobs, config_dir, ports)

    code, document, text = _doctor(ccobs, config_dir, ports)

    checks = [mapping(check, "check") for check in items(document["checks"], "checks")]
    by_id = {str(check["id"]): check for check in checks}
    for check_id in OK_CHECKS:
        assert by_id[check_id]["status"] == "ok", by_id[check_id]
    # The project's image is built from this checkout, so the versions match; it can
    # still warn about hook failures or drops other tests caused, or be info when no
    # hook has reached a freshly started app yet.
    assert by_id["diagnostics"]["status"] != "fail", by_id["diagnostics"]
    assert "upgrade the CLI" not in str(by_id["diagnostics"]["detail"]), by_id["diagnostics"]
    failing = [
        check
        for check in checks
        if check["section"] in NO_FAILURE_SECTIONS and check["status"] == "fail"
    ]
    assert failing == []
    # Claude Code and Docker checks may warn where `claude` is absent (CI), but Docker
    # runs the stack, so its check never fails.
    assert by_id["docker"]["status"] != "fail", by_id["docker"]
    assert code == (1 if any(check["status"] == "fail" for check in checks) else 0)
    assert USER_VALUE not in text


def _status(ccobs: Cli, config: Path, ports: dict[str, int]) -> tuple[int, dict[str, object]]:
    code, out, _ = ccobs("status", *_target(config, ports), "--json")
    return code, mapping(json.loads(out), "status")


def _session_count(document: dict[str, object]) -> int:
    sessions = document["sessions"]
    if sessions is None:
        return 0
    by_state = mapping(mapping(sessions, "sessions")["by_state"], "by_state")
    return sum(int(str(count)) for count in by_state.values())


def test_status_reports_the_app_up_and_a_posted_session(
    ccobs: Cli, config_dir: Path, ports: dict[str, int], app_url: str
) -> None:
    _set_up(ccobs, config_dir, ports)
    run = prepare_hooks(load_scenario(SCENARIO))
    send_hooks(app_url, run.lines[:1])

    stored = poll(
        lambda: detail(app_url, run.session_id), lambda found: found is not None, STORED_DEADLINE
    )
    assert stored is not None, "the app did not store the posted hook"
    code, document = poll(
        lambda: _status(ccobs, config_dir, ports),
        lambda result: _session_count(result[1]) >= 1,
        STORED_DEADLINE,
    )

    assert code == 0, document
    assert mapping(document["app"], "app")["up"] is True
    assert mapping(document["collector"], "collector")["up"] is True
    assert mapping(document["grafana"], "grafana")["up"] is True
    assert mapping(document["diagnostics"], "diagnostics")["understood"] is True
    settings = mapping(document["settings"], "settings")
    assert (settings["file"], settings["hooks"], settings["telemetry"]) == ("ok", "installed", "on")
    assert _session_count(document) >= 1
    assert USER_VALUE not in json.dumps(document)


def test_uninstall_gives_back_the_original_file(
    ccobs: Cli, config_dir: Path, ports: dict[str, int]
) -> None:
    _set_up(ccobs, config_dir, ports)
    assert _read(config_dir) != ORIGINAL

    code, out, err = ccobs("uninstall", "--config-dir", str(config_dir))

    assert code == 0, err
    assert _ordered_keys(_read(config_dir)) == _ordered_keys(ORIGINAL)
    assert not _state_path(config_dir).exists()
    assert USER_VALUE not in out + err


def test_status_exits_1_when_the_app_does_not_answer(
    ccobs: Cli, config_dir: Path, ports: dict[str, int]
) -> None:
    _set_up(ccobs, config_dir, ports)

    code, document = _status(ccobs, config_dir, {**ports, "app": _unused_port()})

    assert code == 1
    assert mapping(document["app"], "app")["up"] is False
    assert mapping(document["diagnostics"], "diagnostics")["reason"] == "app_down"
