"""Tests for ``ccobs setup`` (``ccobs.cli.install``): ADR 0037's file handling end to end.

Setup writes the hooks of ADR 0025 and the env block of ADR 0036 into the user
settings only (ADR 0002), keeps every user entry, value and format detail, backs the
file up, writes atomically after a hash check, records its state, and never shows a
value it did not write in ``--dry-run``.

No test touches the real ``~/.claude``: the autouse fixture points
``CLAUDE_CONFIG_DIR``, ``HOME`` and ``USERPROFILE`` at temporary directories, and the
default probe is replaced so no test reaches the network by accident. No fixture of a
real ``settings.json`` exists in ``tests/fixtures/`` (it would be the user's file); the
user files here follow Claude Code's settings reference.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, cast

import pytest

from ccobs.cli import build_parser, install, main, settings_file
from ccobs.cli.install import probe_app, run_setup
from ccobs.cli.settings_file import read_settings, read_state
from ccobs.domain.hook_config import hook_settings
from ccobs.domain.settings_merge import SetupState
from ccobs.domain.telemetry_settings import TRACES_VARIABLES, telemetry_env

POSIX: Final = os.name == "posix"
WINDOWS: Final = sys.platform == "win32"
BOM: Final = b"\xef\xbb\xbf"
HOOK_URL: Final = "http://127.0.0.1:4300/hooks"
ENDPOINT: Final = "http://127.0.0.1:4318"
NOW: Final = datetime(2026, 10, 19, 11, 0, 0, tzinfo=UTC)
# A value from the user's file, which no output may ever echo.
FILE_VALUE: Final = "value-from-the-users-file"


def _hooks(url: str = HOOK_URL) -> dict[str, object]:
    return cast(dict[str, object], hook_settings(url, windows=WINDOWS)["hooks"])


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude, nor the network through the default
    probe."""
    isolation = tmp_path_factory.mktemp("isolation")
    home = isolation / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))

    def no_network(url: str, timeout: float) -> bool:
        raise AssertionError("tests inject the probe")

    monkeypatch.setattr(install, "probe_app", no_network)
    return home


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


class Probe:
    def __init__(self, answers: bool = True) -> None:
        self.answers = answers
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bool:
        self.calls.append((url, timeout))
        return self.answers


def _setup(
    config_dir: Path | None,
    *argv: str,
    environ: dict[str, str] | None = None,
    probe: Callable[[str, float], bool] | None = None,
    now: datetime = NOW,
) -> Run:
    target = [] if config_dir is None else ["--config-dir", str(config_dir)]
    args = build_parser().parse_args(["setup", *target, *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_setup(
        args,
        environ={} if environ is None else environ,
        out=out,
        err=err,
        probe=Probe() if probe is None else probe,
        now=now,
    )
    return Run(code, out.getvalue(), err.getvalue())


def _settings(config_dir: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((config_dir / "settings.json").read_bytes())
    return data


def _write(config_dir: Path, data: object, raw: bytes | None = None) -> bytes:
    config_dir.mkdir(parents=True, exist_ok=True)
    content = raw if raw is not None else (json.dumps(data, indent=2) + "\n").encode()
    (config_dir / "settings.json").write_bytes(content)
    return content


def _backups(config_dir: Path) -> list[str]:
    directory = config_dir / "ccobs" / "backups"
    return sorted(os.listdir(directory)) if directory.exists() else []


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# Target resolution.


def test_config_dir_option_wins_over_claude_config_dir(tmp_path: Path) -> None:
    run = _setup(tmp_path / "arg", environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "env")})

    assert run.code == 0, run.err
    assert (tmp_path / "arg" / "settings.json").exists()
    assert not (tmp_path / "env").exists()
    assert f"Claude Code settings: {tmp_path / 'arg' / 'settings.json'}" in run.out


def test_claude_config_dir_is_the_target_when_set(tmp_path: Path) -> None:
    run = _setup(None, environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "env")})

    assert run.code == 0, run.err
    assert (tmp_path / "env" / "settings.json").exists()


def test_the_home_is_the_default_target(isolated_home: Path) -> None:
    run = _setup(None)

    assert run.code == 0, run.err
    assert (isolated_home / ".claude" / "settings.json").exists()


def test_config_dir_expands_the_home(isolated_home: Path) -> None:
    run = _setup(Path("~") / "custom")

    assert run.code == 0, run.err
    assert (isolated_home / "custom" / "settings.json").exists()


def test_a_relative_claude_config_dir_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    run = _setup(None, environ={"CLAUDE_CONFIG_DIR": "relative"})

    assert run.code == 2
    assert "CLAUDE_CONFIG_DIR must be an absolute path" in run.err
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("arg", ["", " "])
def test_an_empty_config_dir_is_a_usage_error(
    arg: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    run = _setup(None, "--config-dir", arg)

    assert run.code == 2
    assert "--config-dir must not be empty" in run.err
    assert os.listdir(tmp_path) == []


# A new file.


def test_a_new_file_holds_exactly_the_hooks_and_the_env(tmp_path: Path) -> None:
    probe = Probe()

    run = _setup(tmp_path / "config", probe=probe)

    assert run.code == 0, run.err
    path = tmp_path / "config" / "settings.json"
    expected = {"hooks": _hooks(), "env": telemetry_env(ENDPOINT)}
    assert path.read_bytes() == (json.dumps(expected, indent=2) + "\n").encode()
    assert _backups(tmp_path / "config") == []
    state, rejected = read_state(tmp_path / "config" / "ccobs")
    assert not rejected
    assert state == SetupState(
        env_written=telemetry_env(ENDPOINT),
        env_previous={},
        created_env=True,
        created_hooks=True,
        created_events=tuple(_hooks()),
    )
    assert probe.calls == [("http://127.0.0.1:4300/healthz", 1.0)]
    assert "16 hook events" in run.out
    assert "6 added, 0 updated, 0 removed" in run.out
    assert "Hooks apply to running Claude Code sessions now" in run.out
    assert "restart the ones already open" in run.out
    assert "http://127.0.0.1:4300" in run.out
    assert run.err == ""


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_the_new_file_and_the_state_are_private(tmp_path: Path) -> None:
    _setup(tmp_path / "config")

    assert _mode(tmp_path / "config" / "settings.json") == 0o600
    assert _mode(tmp_path / "config" / "ccobs" / "state.json") == 0o600


# An existing file.

FOREIGN_PRE_TOOL: Final[dict[str, object]] = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "~/bin/audit.sh"}],
}
FOREIGN_STOP: Final[dict[str, object]] = {
    "hooks": [
        {
            "type": "http",
            "url": "http://127.0.0.1:9000/hooks",
            "headers": {"Authorization": f"Bearer {FILE_VALUE}"},
        }
    ]
}
USER_FILE: Final[dict[str, object]] = {
    "model": "opus",
    "hooks": {"PreToolUse": [FOREIGN_PRE_TOOL], "Stop": [FOREIGN_STOP]},
    "permissions": {"allow": [f"Bash(echo {FILE_VALUE})"]},
    "env": {"ANTHROPIC_API_KEY": FILE_VALUE, "FOO": "bar"},
}


def test_user_entries_are_kept_in_order(tmp_path: Path) -> None:
    config = tmp_path / "config"
    original = _write(config, USER_FILE)

    run = _setup(config)

    assert run.code == 0, run.err
    data = _settings(config)
    assert list(data) == ["model", "hooks", "permissions", "env"]
    assert data["model"] == "opus"
    assert data["permissions"] == USER_FILE["permissions"]
    hooks = cast(dict[str, list[object]], data["hooks"])
    ours = _hooks()
    assert list(hooks) == [
        "PreToolUse",
        "Stop",
        *(e for e in ours if e not in ("PreToolUse", "Stop")),
    ]
    assert hooks["PreToolUse"] == [FOREIGN_PRE_TOOL, *cast(list[object], ours["PreToolUse"])]
    assert hooks["Stop"] == [FOREIGN_STOP, *cast(list[object], ours["Stop"])]
    for event, groups in ours.items():
        assert hooks[event][-1] == cast(list[object], groups)[0]
    assert data["env"] == {"ANTHROPIC_API_KEY": FILE_VALUE, "FOO": "bar", **telemetry_env(ENDPOINT)}
    backups = _backups(config)
    assert backups == ["settings.json.20261019T110000000000Z.bak"]
    assert (config / "ccobs" / "backups" / backups[0]).read_bytes() == original
    assert f"Backup: {config / 'ccobs' / 'backups' / backups[0]}" in run.out
    assert FILE_VALUE not in run.out + run.err


def test_project_settings_are_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_bytes(b'{"model": "sonnet"}\n')
    (project / ".claude" / "settings.local.json").write_bytes(b"{}\n")
    monkeypatch.chdir(project)
    user = tmp_path / "user-config"

    run = _setup(None, environ={"CLAUDE_CONFIG_DIR": str(user)})

    assert run.code == 0, run.err
    assert (project / ".claude" / "settings.json").read_bytes() == b'{"model": "sonnet"}\n'
    assert (project / ".claude" / "settings.local.json").read_bytes() == b"{}\n"
    assert sorted(os.listdir(project / ".claude")) == ["settings.json", "settings.local.json"]
    assert (user / "settings.json").exists()


# Invalid files.


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"env": {"KEY": "' + FILE_VALUE.encode() + b'"},}', id="invalid-json"),
        pytest.param(b"", id="empty"),
        pytest.param(b'["' + FILE_VALUE.encode() + b'"]', id="top-level-array"),
        pytest.param(b'{"k": "\xff' + FILE_VALUE.encode() + b'"}', id="not-utf8"),
        pytest.param(b'{"hooks": ["' + FILE_VALUE.encode() + b'"]}', id="hooks-not-an-object"),
        pytest.param(b'{"model": 1e400}', id="huge-float"),
        pytest.param(b'{"env": {"K": "\\ud83d' + FILE_VALUE.encode() + b'"}}', id="surrogate"),
        pytest.param(b'{"model": "a", "model": "' + FILE_VALUE.encode() + b'"}', id="duplicate"),
        pytest.param(b'{"a": ' + b"[" * 500 + b"]" * 500 + b"}", id="too-deep"),
    ],
)
def test_an_invalid_file_is_left_alone(tmp_path: Path, raw: bytes) -> None:
    config = tmp_path / "config"
    _write(config, None, raw)

    run = _setup(config)

    assert run.code == 2
    assert run.err.startswith("ccobs setup: ")
    assert "nothing was changed" in run.err
    assert FILE_VALUE not in run.out + run.err
    assert (config / "settings.json").read_bytes() == raw
    assert os.listdir(config) == ["settings.json"]


# Env conflicts.

CONFLICTING: Final[dict[str, object]] = {
    "env": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://user:{FILE_VALUE}@collector.example:4318",
    }
}


def test_a_conflict_without_force_writes_nothing(tmp_path: Path) -> None:
    config = tmp_path / "config"
    original = _write(config, CONFLICTING)

    run = _setup(config)

    assert run.code == 1
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" in run.err
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in run.err
    assert "--force" in run.err
    assert FILE_VALUE not in run.out + run.err
    assert '"0"' not in run.err
    assert (config / "settings.json").read_bytes() == original
    assert os.listdir(config) == ["settings.json"]


def test_force_replaces_and_records_the_previous_values(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, CONFLICTING)

    run = _setup(config, "--force")

    assert run.code == 0, run.err
    assert _settings(config)["env"] == telemetry_env(ENDPOINT)
    state, _ = read_state(config / "ccobs")
    assert state is not None
    assert state.env_previous == CONFLICTING["env"]
    assert "4 added, 2 updated, 0 removed" in run.out
    assert FILE_VALUE not in run.out + run.err


# Idempotence.


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    assert _setup(config).code == 0
    path = config / "settings.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    state_before = (config / "ccobs" / "state.json").read_bytes()
    backups = _backups(config)

    probe = Probe()
    run = _setup(config, probe=probe, now=NOW + timedelta(hours=1))

    assert run.code == 0, run.err
    assert "already up to date" in run.out
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert (config / "ccobs" / "state.json").read_bytes() == state_before
    assert _backups(config) == backups
    assert probe.calls == []


def test_up_to_date_without_a_state_writes_no_state(tmp_path: Path) -> None:
    config = tmp_path / "config"
    original = _write(config, {"hooks": _hooks(), "env": telemetry_env(ENDPOINT)})

    run = _setup(config)

    assert run.code == 0, run.err
    assert "already up to date" in run.out
    assert (config / "settings.json").read_bytes() == original
    assert os.listdir(config) == ["settings.json"]


# --dry-run.


def test_dry_run_on_a_new_file_writes_nothing(tmp_path: Path) -> None:
    config = tmp_path / "config"

    run = _setup(config, "--dry-run")

    assert run.code == 0, run.err
    assert not config.exists()
    assert "--- settings.json (before)" in run.out
    assert "+++ settings.json (after)" in run.out
    assert HOOK_URL in run.out
    assert ENDPOINT in run.out
    assert "nothing was written" in run.out


def test_dry_run_masks_every_value_setup_did_not_write(tmp_path: Path) -> None:
    config = tmp_path / "config"
    user = {
        **USER_FILE,
        "env": {
            "ANTHROPIC_API_KEY": FILE_VALUE,
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://{FILE_VALUE}@collector.example",
            "OTEL_METRICS_EXPORTER": "otlp",
        },
    }
    original = _write(config, user)

    run = _setup(config, "--dry-run", "--force")

    assert run.code == 0, run.err
    assert FILE_VALUE not in run.out + run.err
    assert "Bearer" not in run.out
    assert "permissions" not in run.out
    assert "ANTHROPIC_API_KEY" not in run.out
    assert "model" not in run.out
    assert '-    "OTEL_EXPORTER_OTLP_ENDPOINT": "<your value>"' in run.out
    assert f'+    "OTEL_EXPORTER_OTLP_ENDPOINT": "{ENDPOINT}"' in run.out
    assert '"OTEL_METRICS_EXPORTER": "otlp"' in run.out
    assert '"<your hook group>"' in run.out
    assert HOOK_URL in run.out
    assert (config / "settings.json").read_bytes() == original
    assert os.listdir(config) == ["settings.json"]


def test_dry_run_leaves_out_hooks_and_env_setup_does_not_touch(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(
        config,
        {
            "hooks": {"ConfigChange": [{"hooks": [{"type": "command", "command": FILE_VALUE}]}]},
            "env": {"FOO": FILE_VALUE},
        },
    )

    run = _setup(config, "--dry-run")

    assert run.code == 0, run.err
    assert FILE_VALUE not in run.out
    assert "ConfigChange" not in run.out
    assert "FOO" not in run.out
    assert "-{}" in run.out


def test_dry_run_masks_recognized_groups_setup_does_not_write(tmp_path: Path) -> None:
    config = tmp_path / "config"
    token_url = f"http://127.0.0.1:4300/{FILE_VALUE}/hooks"
    curl = {
        "type": "command",
        "command": "curl",
        "args": ["-H", f"Authorization: Bearer {FILE_VALUE}", "--data-binary", "@-", token_url],
        "async": True,
    }
    http = {"type": "http", "url": "http://127.0.0.1:5300/hooks", "timeout": 3}
    foreign = {"type": "command", "command": "~/bin/audit.sh"}
    _write(
        config,
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": FILE_VALUE, "hooks": [curl]},
                    {"hooks": [http]},
                    {"hooks": [http, foreign]},
                ]
            }
        },
    )

    run = _setup(config, "--dry-run")

    assert run.code == 0, run.err
    assert FILE_VALUE not in run.out + run.err
    assert "Bearer" not in run.out
    assert "5300" not in run.out
    assert "audit.sh" not in run.out
    assert run.out.count('"<earlier cc-observatory group>"') == 2  # before only
    assert run.out.count('"<your hook group>"') == 2  # before and after
    assert HOOK_URL in run.out


def test_dry_run_shows_the_values_of_the_previous_run(tmp_path: Path) -> None:
    config = tmp_path / "config"
    assert _setup(config, "--traces").code == 0
    path = config / "settings.json"
    before = path.read_bytes()

    run = _setup(config, "--dry-run")

    assert run.code == 0, run.err
    for key in TRACES_VARIABLES:
        assert f'-    "{key}": ' in run.out
    assert "<your value>" not in run.out
    assert path.read_bytes() == before


# Format.

FORMATS: Final = [
    pytest.param(BOM + b'{\n  "model": "opus"\n}\n', id="bom"),
    pytest.param(b'{\r\n  "model": "opus"\r\n}\r\n', id="crlf"),
    pytest.param(b'{\n    "model": "opus"\n}\n', id="four-spaces"),
    pytest.param(b'{\n\t"model": "opus"\n}\n', id="tab"),
    pytest.param(b'{\n  "model": "opus"\n}', id="no-final-newline"),
]


@pytest.mark.parametrize("raw", FORMATS)
def test_the_format_is_kept(tmp_path: Path, raw: bytes) -> None:
    config = tmp_path / "config"
    _write(config, None, raw)
    before = read_settings(config / "settings.json")

    run = _setup(config)

    assert run.code == 0, run.err
    after = read_settings(config / "settings.json")
    assert (after.bom, after.newline, after.indent, after.final_newline) == (
        before.bom,
        before.newline,
        before.indent,
        before.final_newline,
    )
    assert list(after.data) == ["model", "hooks", "env"]


def test_a_symlinked_settings_file_stays_a_link(tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "claude-settings.json"
    target.write_bytes(b'{"model": "opus"}\n')
    config = tmp_path / "config"
    config.mkdir()
    link = config / "settings.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this OS refuses to create symlinks here")

    run = _setup(config)

    assert run.code == 0, run.err
    assert link.is_symlink()
    assert json.loads(target.read_bytes())["env"] == telemetry_env(ENDPOINT)


# Concurrent change and write errors.


def test_a_change_while_setup_runs_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    original = settings_file.read_settings
    other = b'{"model": "sonnet"}\n'

    def read_then_change(path: Path) -> settings_file.LoadedSettings:
        loaded = original(path)
        path.write_bytes(other)  # Claude Code saving /config meanwhile
        return loaded

    monkeypatch.setattr(settings_file, "read_settings", read_then_change)

    run = _setup(config)

    assert run.code == 1
    assert "changed while setup ran; run it again" in run.err
    assert "settings.json was not changed" in run.err
    assert (config / "settings.json").read_bytes() == other
    assert not (config / "ccobs" / "state.json").exists()


def _previous_state(config: Path) -> bytes:
    """Run setup once on another port; returns the state it recorded."""
    assert _setup(config, "--app-port", "5300", "--force").code == 0
    return (config / "ccobs" / "state.json").read_bytes()


def test_a_change_while_setup_runs_puts_the_previous_state_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _write(config, {"env": {"OTEL_METRICS_EXPORTER": "prometheus"}})
    state = _previous_state(config)
    original = settings_file.read_settings
    other = b'{"model": "sonnet"}\n'

    def read_then_change(path: Path) -> settings_file.LoadedSettings:
        loaded = original(path)
        path.write_bytes(other)
        return loaded

    monkeypatch.setattr(settings_file, "read_settings", read_then_change)

    run = _setup(config, now=NOW + timedelta(hours=1))

    assert run.code == 1
    assert (config / "settings.json").read_bytes() == other
    # env_previous (the user's "prometheus") is still there for uninstall.
    assert (config / "ccobs" / "state.json").read_bytes() == state


def test_a_state_that_cannot_be_written_stops_setup(tmp_path: Path) -> None:
    config = tmp_path / "config"
    original = _write(config, {"model": "opus"})
    (config / "ccobs" / "state.json").mkdir(parents=True)

    run = _setup(config)

    assert run.code == 2
    assert f"cannot write {config / 'ccobs' / 'state.json'}" in run.err
    assert "settings.json was not changed" in run.err
    assert ".tmp" not in run.err
    assert (config / "settings.json").read_bytes() == original
    assert _backups(config) == []
    assert "Backup:" not in run.out


def test_a_write_that_keeps_failing_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    original = _write(config, {"model": "opus"})

    def held(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise PermissionError(13, "Access is denied", os.fspath(source))

    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(settings_file, "_RETRY_DELAY_S", 0)

    run = _setup(config)

    assert run.code == 2
    assert f"cannot write {config / 'ccobs' / 'state.json'}: Access is denied" in run.err
    assert ".tmp" not in run.err
    assert (config / "settings.json").read_bytes() == original
    assert sorted(os.listdir(config)) == ["ccobs", "settings.json"]
    assert os.listdir(config / "ccobs") == []


def _fail_settings_write(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    def fail(target: Path, content: bytes, *, expected_digest: str | None) -> None:
        raise PermissionError(13, "Access is denied", str(path.parent / ".settings.json.x.tmp"))

    monkeypatch.setattr(settings_file, "write_settings", fail)


@pytest.mark.parametrize("had_state", [True, False])
def test_a_failed_settings_write_puts_the_previous_state_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, had_state: bool
) -> None:
    config = tmp_path / "config"
    _write(config, {"env": {"OTEL_METRICS_EXPORTER": "prometheus"}})
    state = _previous_state(config) if had_state else None
    original = (config / "settings.json").read_bytes()
    _fail_settings_write(monkeypatch, config / "settings.json")

    run = _setup(config, "--force", now=NOW + timedelta(hours=1))

    assert run.code == 2
    assert f"cannot write {config / 'settings.json'}: Access is denied" in run.err
    assert "settings.json was not changed" in run.err
    assert ".tmp" not in run.err
    assert (config / "settings.json").read_bytes() == original
    state_file = config / "ccobs" / "state.json"
    assert (state_file.read_bytes() if state_file.exists() else None) == state


def test_a_failed_backup_puts_the_previous_state_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    original = _write(config, {"model": "opus"})

    def fail(path: Path, ccobs_dir: Path, *, now: datetime) -> Path:
        raise OSError(28, "No space left on device", str(ccobs_dir / "backups" / "x.tmp"))

    monkeypatch.setattr(settings_file, "backup_settings", fail)

    run = _setup(config)

    assert run.code == 2
    assert "cannot back up the settings file: No space left on device" in run.err
    assert "settings.json was not changed" in run.err
    assert ".tmp" not in run.err
    assert (config / "settings.json").read_bytes() == original
    assert not (config / "ccobs" / "state.json").exists()


def test_a_state_that_cannot_be_put_back_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _fail_settings_write(monkeypatch, config / "settings.json")

    def fail(ccobs_dir: Path, previous: bytes | None) -> None:
        raise PermissionError(13, "Access is denied", str(ccobs_dir / ".state.json.x.tmp"))

    monkeypatch.setattr(settings_file, "restore_state", fail)

    run = _setup(config)

    assert run.code == 2
    assert f"cannot write {config / 'settings.json'}" in run.err
    assert f"could not put back {config / 'ccobs' / 'state.json'}" in run.err
    assert "ccobs uninstall" not in run.err
    assert ".tmp" not in run.err


# Backups and state.


def test_one_backup_per_write_and_at_most_ten(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})

    for run_index in range(12):
        port = "4300" if run_index % 2 else "5300"
        run = _setup(config, "--app-port", port, now=NOW + timedelta(minutes=run_index))
        assert run.code == 0, run.err

    expected = [
        f"settings.json.{(NOW + timedelta(minutes=index)):%Y%m%dT%H%M%S}000000Z.bak"
        for index in range(2, 12)
    ]
    assert _backups(config) == expected


def test_a_rejected_state_is_ignored_with_a_warning(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "ccobs").mkdir(parents=True)
    (config / "ccobs" / "state.json").write_bytes(b'{"version": 99}')

    run = _setup(config)

    assert run.code == 0, run.err
    assert "warning" in run.err
    assert "state" in run.err
    assert read_state(config / "ccobs")[1] is False


# App probe and messages.


def test_an_unreachable_app_warns_and_still_writes(tmp_path: Path) -> None:
    config = tmp_path / "config"

    run = _setup(config, probe=Probe(answers=False))

    assert run.code == 0
    assert "127.0.0.1:4300" in run.err
    assert "docker compose up -d" in run.err
    assert "hook-error" in run.err
    assert "ccobs uninstall" in run.err
    assert (config / "settings.json").exists()


def test_traces_add_their_keys_and_the_profile_reminder(tmp_path: Path) -> None:
    config = tmp_path / "config"

    run = _setup(config, "--traces")

    assert run.code == 0, run.err
    assert _settings(config)["env"] == telemetry_env(ENDPOINT, traces=True)
    assert "--profile traces" in run.out


def test_without_traces_there_is_no_profile_reminder(tmp_path: Path) -> None:
    run = _setup(tmp_path / "config")

    assert "--profile traces" not in run.out


def test_ports_reach_every_url(tmp_path: Path) -> None:
    config = tmp_path / "config"
    probe = Probe()

    run = _setup(config, "--app-port", "5300", "--otlp-port", "5318", probe=probe)

    assert run.code == 0, run.err
    data = _settings(config)
    assert data["hooks"] == _hooks("http://127.0.0.1:5300/hooks")
    assert data["env"] == telemetry_env("http://127.0.0.1:5318")
    assert probe.calls == [("http://127.0.0.1:5300/healthz", 1.0)]
    assert "http://127.0.0.1:5300" in run.out


@pytest.mark.parametrize("option", ["--app-port", "--otlp-port"])
@pytest.mark.parametrize("value", ["0", "65536", "http", "-1"])
def test_an_invalid_port_is_a_usage_error(
    option: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["setup", option, value])

    assert excinfo.value.code == 2
    assert "not a port" in capsys.readouterr().err


# A console that cannot encode the path.


def test_output_survives_a_console_that_cannot_encode_the_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "config-\u8a2d\u5b9a"  # not in cp1252
    args = build_parser().parse_args(["setup", "--config-dir", str(config_dir)])
    out_raw, err_raw = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(out_raw, encoding="cp1252")
    err = io.TextIOWrapper(err_raw, encoding="cp1252")

    code = run_setup(args, environ={}, out=out, err=err, probe=Probe(answers=False), now=NOW)

    out.flush()
    err.flush()
    assert code == 0
    assert "config-\\u8a2d\\u5b9a" in out_raw.getvalue().decode("cp1252")
    assert "127.0.0.1:4300" in err_raw.getvalue().decode("cp1252")  # the probe's notice
    assert (config_dir / "settings.json").exists()


# The command line.


def test_main_dispatches_to_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = Probe()
    monkeypatch.setattr(install, "probe_app", probe)

    exit_code = main(["setup", "--config-dir", str(tmp_path / "config")])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert (tmp_path / "config" / "settings.json").exists()
    assert probe.calls == [("http://127.0.0.1:4300/healthz", 1.0)]


def test_main_reads_claude_config_dir_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "env-config"))

    exit_code = main(["setup", "--dry-run"])

    assert exit_code == 0
    assert str(tmp_path / "env-config" / "settings.json") in capsys.readouterr().out
    assert not (tmp_path / "env-config").exists()


def test_help_lists_the_setup_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "setup" in capsys.readouterr().out


def test_python_dash_m_setup_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ccobs", "setup", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    for option in ("--config-dir", "--app-port", "--otlp-port", "--traces", "--force", "--dry-run"):
        assert option in result.stdout


# The default probe, against a loopback server.


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 500)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def app_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_is_true_when_the_app_answers(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A proxy from the environment is never used for the local app.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")

    assert probe_app(f"{app_url}/healthz", 1.0) is True


def test_the_probe_is_false_on_an_error_status(app_url: str) -> None:
    assert probe_app(f"{app_url}/missing", 1.0) is False


def test_the_probe_is_false_on_a_bad_url() -> None:
    assert probe_app("not a url", 1.0) is False
