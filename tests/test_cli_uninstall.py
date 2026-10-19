"""Tests for ``ccobs uninstall`` (``ccobs.cli.uninstall``): ADR 0037's Uninstall end to end.

Uninstall takes out of the user settings what ``ccobs setup`` put there: every hook
handler it recognizes, and with the setup state the env values setup still owns (the
user's earlier values put back). It keeps the file's format, backs it up, writes
atomically after a hash check, deletes the state, and never shows a value of the
user's: not in the report, not in ``--dry-run``.

No test touches the real ``~/.claude``: the autouse fixture points
``CLAUDE_CONFIG_DIR``, ``HOME`` and ``USERPROFILE`` at temporary directories, and setup's
default probe is replaced so no test reaches the network.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

import pytest

from ccobs.cli import build_parser, install, main, settings_file
from ccobs.cli.install import run_setup
from ccobs.cli.uninstall import run_uninstall
from ccobs.domain.hook_config import hook_settings
from ccobs.domain.settings_merge import SetupState
from ccobs.domain.telemetry_settings import TELEMETRY_VARIABLES, telemetry_env

WINDOWS: Final = sys.platform == "win32"
BOM: Final = b"\xef\xbb\xbf"
HOOK_URL: Final = "http://127.0.0.1:4300/hooks"
ENDPOINT: Final = "http://127.0.0.1:4318"
NOW: Final = datetime(2026, 10, 19, 11, 0, 0, tzinfo=UTC)
LATER: Final = NOW + timedelta(hours=1)
# A value from the user's file, which no output may ever echo.
FILE_VALUE: Final = "value-from-the-users-file"


def _hooks(url: str = HOOK_URL) -> dict[str, object]:
    return cast(dict[str, object], hook_settings(url, windows=WINDOWS)["hooks"])


def _handler_count() -> int:
    groups = cast(dict[str, list[dict[str, list[object]]]], _hooks())
    return sum(len(group["hooks"]) for event in groups.values() for group in event)


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude, nor the network through setup's
    default probe."""
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


def _setup(config_dir: Path, *argv: str) -> None:
    args = build_parser().parse_args(["setup", "--config-dir", str(config_dir), *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_setup(args, environ={}, out=out, err=err, probe=lambda url, timeout: True, now=NOW)
    assert code == 0, err.getvalue()


def _uninstall(
    config_dir: Path | None,
    *argv: str,
    environ: dict[str, str] | None = None,
    now: datetime = LATER,
) -> Run:
    target = [] if config_dir is None else ["--config-dir", str(config_dir)]
    args = build_parser().parse_args(["uninstall", *target, *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_uninstall(
        args, environ={} if environ is None else environ, out=out, err=err, now=now
    )
    return Run(code, out.getvalue(), err.getvalue())


def _raw(data: object, *, indent: int | str = 2, newline: str = "\n", bom: bool = False) -> bytes:
    text = (json.dumps(data, indent=indent, ensure_ascii=False) + "\n").replace("\n", newline)
    return (BOM if bom else b"") + text.encode()


def _write(config_dir: Path, data: object, raw: bytes | None = None) -> bytes:
    config_dir.mkdir(parents=True, exist_ok=True)
    content = raw if raw is not None else _raw(data)
    (config_dir / "settings.json").write_bytes(content)
    return content


def _settings(config_dir: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((config_dir / "settings.json").read_bytes())
    return data


def _env(config_dir: Path) -> dict[str, object]:
    return cast(dict[str, object], _settings(config_dir)["env"])


def _edit(config_dir: Path, change: dict[str, object]) -> None:
    """Change the settings by hand after setup, as the user would."""
    data = _settings(config_dir)
    for key, value in change.items():
        data[key] = value
    _write(config_dir, data)


def _backups(config_dir: Path) -> list[str]:
    directory = config_dir / "ccobs" / "backups"
    return sorted(os.listdir(directory)) if directory.exists() else []


def _state_file(config_dir: Path) -> Path:
    return config_dir / "ccobs" / "state.json"


def _snapshot(config_dir: Path) -> dict[str, bytes]:
    """Every file under ``config_dir`` and its bytes."""
    return {
        str(path.relative_to(config_dir)): path.read_bytes()
        for path in sorted(config_dir.rglob("*"))
        if path.is_file()
    }


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


# Setup, then uninstall.

FORMATS: Final = [
    pytest.param(2, "\n", False, id="lf-2-spaces"),
    pytest.param(4, "\r\n", False, id="crlf-4-spaces"),
    pytest.param(2, "\n", True, id="bom"),
    pytest.param("\t", "\r\n", True, id="bom-crlf-tab"),
]


@pytest.mark.parametrize(("indent", "newline", "bom"), FORMATS)
def test_setup_then_uninstall_gives_the_file_back(
    tmp_path: Path, indent: int | str, newline: str, bom: bool
) -> None:
    config = tmp_path / "config"
    original = _write(config, None, _raw(USER_FILE, indent=indent, newline=newline, bom=bom))
    _setup(config)

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert (config / "settings.json").read_bytes() == original
    assert not _state_file(config).exists()
    assert len(_backups(config)) == 2  # one per write: setup's and uninstall's
    newest = config / "ccobs" / "backups" / _backups(config)[-1]
    assert f"Backup: {newest}" in run.out
    assert f"Removed {_handler_count()} cc-observatory hook handlers" in run.out
    assert f"Telemetry variables removed: {', '.join(TELEMETRY_VARIABLES)}." in run.out
    assert "Hooks stop in running Claude Code sessions now" in run.out
    assert "telemetry continues in sessions already open until you restart them" in run.out
    assert "docker compose down" in run.out
    assert "docker compose down -v" in run.out
    assert FILE_VALUE not in run.out + run.err
    assert run.err == ""


def test_uninstall_after_setup_on_an_empty_dir_leaves_an_empty_object(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _setup(config)

    run = _uninstall(config)

    assert run.code == 0, run.err
    # setup created both containers, so both go; the file itself stays.
    assert (config / "settings.json").read_bytes() == b"{}\n"
    assert not _state_file(config).exists()
    assert len(_backups(config)) == 1


def test_uninstall_puts_back_the_values_force_replaced(tmp_path: Path) -> None:
    config = tmp_path / "config"
    user_endpoint = f"https://user:{FILE_VALUE}@collector.example:4318"
    original = _write(
        config,
        {
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
                "OTEL_EXPORTER_OTLP_ENDPOINT": user_endpoint,
            }
        },
    )
    _setup(config, "--force")

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert (config / "settings.json").read_bytes() == original
    assert (
        "Telemetry variables restored to your earlier values: "
        "CLAUDE_CODE_ENABLE_TELEMETRY, OTEL_EXPORTER_OTLP_ENDPOINT." in run.out
    )
    assert FILE_VALUE not in run.out + run.err


def test_a_value_the_user_changed_after_setup_is_kept(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    env = {**telemetry_env(ENDPOINT), "OTEL_METRICS_EXPORTER": f"prometheus-{FILE_VALUE}"}
    _edit(config, {"env": env})

    run = _uninstall(config)

    assert run.code == 0, run.err
    data = _settings(config)
    assert data["env"] == {"OTEL_METRICS_EXPORTER": f"prometheus-{FILE_VALUE}"}
    assert "hooks" not in data
    assert "Kept OTEL_METRICS_EXPORTER: you changed them after setup." in run.out
    assert "not added by ccobs setup" not in run.out
    assert FILE_VALUE not in run.out + run.err
    assert not _state_file(config).exists()


def test_variables_pasted_by_hand_are_reported_not_removed(tmp_path: Path) -> None:
    config = tmp_path / "config"
    env = {"ANTHROPIC_API_KEY": FILE_VALUE, **telemetry_env(ENDPOINT)}
    _write(config, {"hooks": _hooks(), "env": env})

    run = _uninstall(config)

    assert run.code == 0, run.err
    # Without a state, every event the removal emptied goes, but no container.
    assert _settings(config) == {"hooks": {}, "env": env}
    assert f"Removed {_handler_count()} cc-observatory hook handlers" in run.out
    assert (
        f"Still set, not added by ccobs setup: {', '.join(TELEMETRY_VARIABLES)}. "
        "Remove them from the env block yourself to stop sending telemetry." in run.out
    )
    assert "Telemetry variables removed" not in run.out
    assert ENDPOINT not in run.out + run.err
    assert FILE_VALUE not in run.out + run.err


def test_a_lost_state_removes_the_hooks_and_reports_the_env(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, USER_FILE)
    _setup(config)
    _state_file(config).unlink()

    run = _uninstall(config)

    assert run.code == 0, run.err
    data = _settings(config)
    assert data["hooks"] == USER_FILE["hooks"]
    assert data["env"] == {**cast(dict[str, str], USER_FILE["env"]), **telemetry_env(ENDPOINT)}
    assert "not added by ccobs setup: " in run.out
    assert FILE_VALUE not in run.out + run.err


def test_a_rejected_state_is_ignored_with_a_warning(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, USER_FILE)
    _setup(config)
    _state_file(config).write_bytes(b'{"version": 99}')

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "warning: ignored the setup state" in run.err
    data = _settings(config)
    assert data["hooks"] == USER_FILE["hooks"]
    assert data["env"] == {**cast(dict[str, str], USER_FILE["env"]), **telemetry_env(ENDPOINT)}
    assert "not added by ccobs setup: " in run.out
    # A state uninstall cannot read is not its to delete (a later version may own it).
    assert _state_file(config).read_bytes() == b'{"version": 99}'


# A missing settings file.

STATE: Final = SetupState(
    env_written=telemetry_env(ENDPOINT),
    env_previous={},
    created_env=True,
    created_hooks=True,
    created_events=(),
)


def test_a_missing_file_with_a_state_deletes_the_state(tmp_path: Path) -> None:
    config = tmp_path / "config"
    settings_file.write_state(config / "ccobs", STATE)

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "settings.json does not exist; nothing to remove from it." in run.out
    assert f"Removed the setup state {_state_file(config)}." in run.out
    assert not _state_file(config).exists()
    assert not (config / "settings.json").exists()


def test_a_missing_file_with_a_state_in_a_dry_run_keeps_the_state(tmp_path: Path) -> None:
    config = tmp_path / "config"
    settings_file.write_state(config / "ccobs", STATE)
    before = _snapshot(config)

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    assert f"Would remove the setup state {_state_file(config)}." in run.out
    assert "Dry run: nothing was written." in run.out
    assert _snapshot(config) == before


def test_a_missing_file_without_a_state_changes_nothing(tmp_path: Path) -> None:
    config = tmp_path / "config"

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "settings.json does not exist; nothing to remove from it." in run.out
    assert "setup state" not in run.out
    assert not config.exists()


# Nothing to remove.


@pytest.mark.parametrize("with_state", [False, True])
def test_nothing_to_remove_writes_nothing(tmp_path: Path, with_state: bool) -> None:
    config = tmp_path / "config"
    original = _write(config, {"model": "opus", "hooks": {"Stop": [FOREIGN_STOP]}})
    if with_state:
        settings_file.write_state(config / "ccobs", STATE)
    path = config / "settings.json"
    mtime = path.stat().st_mtime_ns

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "nothing of cc-observatory's to remove from settings.json" in run.out
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (original, mtime)
    assert _backups(config) == []
    assert not _state_file(config).exists()
    assert ("Removed the setup state" in run.out) is with_state
    assert "Backup:" not in run.out
    assert FILE_VALUE not in run.out + run.err


def test_nothing_to_remove_in_a_dry_run_keeps_the_state(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    settings_file.write_state(config / "ccobs", STATE)
    before = _snapshot(config)

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    assert "nothing of cc-observatory's to remove from settings.json" in run.out
    assert f"Would remove the setup state {_state_file(config)}." in run.out
    assert "Dry run: nothing was written." in run.out
    assert _snapshot(config) == before


def test_nothing_to_remove_still_reports_variables_setup_did_not_add(tmp_path: Path) -> None:
    config = tmp_path / "config"
    original = _write(config, {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": FILE_VALUE}})

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "nothing of cc-observatory's to remove" in run.out
    assert "Still set, not added by ccobs setup: CLAUDE_CODE_ENABLE_TELEMETRY." in run.out
    assert (config / "settings.json").read_bytes() == original
    assert FILE_VALUE not in run.out + run.err


KEPT: Final = ", ".join(telemetry_env(ENDPOINT))
TRACES_BY_HAND: Final = f"otlp-{FILE_VALUE}"


def _take_back_by_hand(config: Path) -> None:
    """After setup, remove the hooks and change every value setup wrote, by hand; then
    add a telemetry variable setup did not write."""
    data = _settings(config)
    del data["hooks"]
    env = {key: f"{FILE_VALUE}-{key}" for key in telemetry_env(ENDPOINT)}
    data["env"] = {**env, "OTEL_TRACES_EXPORTER": TRACES_BY_HAND}
    _write(config, data)


def test_nothing_to_remove_still_reports_the_values_the_user_changed(tmp_path: Path) -> None:
    # ADR 0037: telemetry is never left on silently, even when nothing is written.
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    _take_back_by_hand(config)
    original = (config / "settings.json").read_bytes()

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "nothing of cc-observatory's to remove" in run.out
    assert f"Kept {KEPT}: you changed them after setup." in run.out
    assert "Still set, not added by ccobs setup: OTEL_TRACES_EXPORTER." in run.out
    assert (config / "settings.json").read_bytes() == original
    assert not _state_file(config).exists()
    assert FILE_VALUE not in run.out + run.err
    assert ENDPOINT not in run.out + run.err


def test_nothing_to_remove_in_a_dry_run_reports_both_lists(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    _take_back_by_hand(config)
    before = _snapshot(config)

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    kept = run.out.index(f"Kept {KEPT}: you changed them after setup.")
    unowned = run.out.index("Still set, not added by ccobs setup: OTEL_TRACES_EXPORTER.")
    assert kept < unowned < run.out.index("Dry run: nothing was written.")
    assert _snapshot(config) == before
    assert FILE_VALUE not in run.out + run.err
    assert ENDPOINT not in run.out + run.err


# --dry-run.


def test_dry_run_reports_kept_and_unowned_variables(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    env = {
        **_env(config),
        "OTEL_METRICS_EXPORTER": f"prometheus-{FILE_VALUE}",
        "OTEL_TRACES_EXPORTER": TRACES_BY_HAND,
    }
    _edit(config, {"env": env})
    before = _snapshot(config)

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    assert "+++ settings.json (after)" in run.out
    kept = run.out.index("Kept OTEL_METRICS_EXPORTER: you changed them after setup.")
    unowned = run.out.index("Still set, not added by ccobs setup: OTEL_TRACES_EXPORTER.")
    assert kept < unowned < run.out.index("Dry run: nothing was written.")
    assert _snapshot(config) == before
    assert FILE_VALUE not in run.out + run.err


def test_dry_run_writes_nothing_and_masks_every_user_value(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(
        config,
        {
            **USER_FILE,
            "env": {
                "ANTHROPIC_API_KEY": FILE_VALUE,
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://user:{FILE_VALUE}@collector.example",
            },
        },
    )
    _setup(config, "--force")
    # An older cc-observatory group, with a header and a token in its URL path.
    older = {
        "matcher": FILE_VALUE,
        "hooks": [
            {
                "type": "command",
                "command": "curl",
                "args": [
                    "-H",
                    f"Authorization: Bearer {FILE_VALUE}",
                    "--data-binary",
                    "@-",
                    f"http://127.0.0.1:4300/{FILE_VALUE}/hooks",
                ],
                "async": True,
            }
        ],
    }
    hooks = cast(dict[str, list[object]], _settings(config)["hooks"])
    hooks["PreToolUse"].append(older)
    _edit(config, {"hooks": hooks})
    before = _snapshot(config)

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    assert _snapshot(config) == before
    assert FILE_VALUE not in run.out + run.err
    assert "Bearer" not in run.out
    assert "audit.sh" not in run.out
    assert "9000" not in run.out
    assert "ANTHROPIC_API_KEY" not in run.out
    assert "model" not in run.out
    assert "permissions" not in run.out
    assert "--- settings.json (before)" in run.out
    assert "+++ settings.json (after)" in run.out
    assert '"<cc-observatory group>"' in run.out
    assert '"<your hook group>"' in run.out
    assert f'-    "OTEL_EXPORTER_OTLP_ENDPOINT": "{ENDPOINT}"' in run.out
    assert '+    "OTEL_EXPORTER_OTLP_ENDPOINT": "<your value>"' in run.out
    assert '-    "CLAUDE_CODE_ENABLE_TELEMETRY": "1"' in run.out
    assert "Dry run: nothing was written." in run.out
    assert "Removed" not in run.out


def test_dry_run_leaves_out_events_without_a_cc_observatory_handler(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(
        config,
        {
            "hooks": {
                "ConfigChange": [{"hooks": [{"type": "command", "command": FILE_VALUE}]}],
                "Stop": [{"hooks": [{"type": "http", "url": HOOK_URL, "timeout": 3}]}],
            },
            "env": {"FOO": FILE_VALUE},
        },
    )

    run = _uninstall(config, "--dry-run")

    assert run.code == 0, run.err
    assert FILE_VALUE not in run.out
    assert "ConfigChange" not in run.out
    assert "FOO" not in run.out
    assert '"Stop"' in run.out
    assert HOOK_URL not in run.out


# Errors.


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"env": {"KEY": "' + FILE_VALUE.encode() + b'"},}', id="invalid-json"),
        pytest.param(b'{"hooks": ["' + FILE_VALUE.encode() + b'"]}', id="hooks-not-an-object"),
    ],
)
def test_an_invalid_file_is_left_alone(tmp_path: Path, raw: bytes) -> None:
    config = tmp_path / "config"
    _write(config, None, raw)
    settings_file.write_state(config / "ccobs", STATE)
    before = _snapshot(config)

    run = _uninstall(config)

    assert run.code == 2
    assert run.err.startswith("ccobs uninstall: ")
    assert "nothing was changed" in run.err
    assert FILE_VALUE not in run.out + run.err
    assert _snapshot(config) == before


@pytest.mark.parametrize("arg", ["", " "])
def test_an_empty_config_dir_is_a_usage_error(
    arg: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    run = _uninstall(None, "--config-dir", arg)

    assert run.code == 2
    assert "--config-dir must not be empty" in run.err
    assert os.listdir(tmp_path) == []


def test_a_change_while_uninstall_runs_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    state = _state_file(config).read_bytes()
    original = settings_file.read_settings
    other = b'{"model": "sonnet"}\n'

    def read_then_change(path: Path) -> settings_file.LoadedSettings:
        loaded = original(path)
        path.write_bytes(other)  # Claude Code saving /config meanwhile
        return loaded

    monkeypatch.setattr(settings_file, "read_settings", read_then_change)

    run = _uninstall(config)

    assert run.code == 1
    assert "settings.json changed while uninstall ran; run it again" in run.err
    assert (config / "settings.json").read_bytes() == other
    assert _state_file(config).read_bytes() == state


def test_a_write_that_keeps_failing_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    before = _snapshot(config)

    def held(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise PermissionError(13, "Access is denied", os.fspath(source))

    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(settings_file, "_RETRY_DELAY_S", 0)

    run = _uninstall(config)

    assert run.code == 2
    assert "Access is denied" in run.err
    assert "settings.json was not changed" in run.err
    assert ".tmp" not in run.err
    assert _snapshot(config) == before  # no temporary file left either
    assert not any(name.endswith(".tmp") for name in os.listdir(config))


def test_a_failed_settings_write_leaves_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    original = (config / "settings.json").read_bytes()
    state = _state_file(config).read_bytes()

    def fail(target: Path, content: bytes, *, expected_digest: str | None) -> None:
        raise PermissionError(13, "Access is denied", str(config / ".settings.json.x.tmp"))

    monkeypatch.setattr(settings_file, "write_settings", fail)

    run = _uninstall(config)

    assert run.code == 2
    assert f"cannot write {config / 'settings.json'}: Access is denied" in run.err
    assert "settings.json was not changed" in run.err
    assert ".tmp" not in run.err
    assert (config / "settings.json").read_bytes() == original
    assert _state_file(config).read_bytes() == state


def test_a_failed_backup_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    _write(config, {"model": "opus"})
    _setup(config)
    before = _snapshot(config)

    def fail(path: Path, ccobs_dir: Path, *, now: datetime) -> Path:
        raise OSError(28, "No space left on device", str(ccobs_dir / "backups" / "x.tmp"))

    monkeypatch.setattr(settings_file, "backup_settings", fail)

    run = _uninstall(config)

    assert run.code == 2
    assert "cannot back up the settings file: No space left on device" in run.err
    assert ".tmp" not in run.err
    assert _snapshot(config) == before


def _fail_state_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(ccobs_dir: Path) -> None:
        raise PermissionError(13, "Access is denied", str(ccobs_dir / "state.json"))

    monkeypatch.setattr(settings_file, "delete_state", fail)


def test_a_state_that_cannot_be_deleted_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    _setup(config)
    _fail_state_deletion(monkeypatch)

    run = _uninstall(config)

    assert run.code == 0
    assert (config / "settings.json").read_bytes() == b"{}\n"
    assert f"warning: could not remove {_state_file(config)}: Access is denied" in run.err
    assert "ccobs setup recognizes and replaces its entries" in run.err
    assert "Removed the setup state" not in run.out


@pytest.mark.parametrize("settings", [None, {"model": "opus"}], ids=["missing", "unchanged"])
def test_a_state_that_cannot_be_deleted_without_a_write_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: dict[str, object] | None
) -> None:
    config = tmp_path / "config"
    if settings is not None:
        _write(config, settings)
    settings_file.write_state(config / "ccobs", STATE)
    _fail_state_deletion(monkeypatch)

    run = _uninstall(config)

    assert run.code == 0
    assert f"warning: could not remove {_state_file(config)}" in run.err
    assert _state_file(config).exists()


def test_the_report_never_shows_an_env_value(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _write(
        config,
        {
            "env": {
                "ANTHROPIC_API_KEY": FILE_VALUE,
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://{FILE_VALUE}@collector.example",
            }
        },
    )
    _setup(config, "--force", "--traces")
    env = _env(config)
    env["OTEL_METRICS_EXPORTER"] = f"{FILE_VALUE}-changed"  # kept
    env["OTEL_TRACES_EXPORTER"] = f"{FILE_VALUE}-traces"  # kept
    _edit(config, {"env": env})

    run = _uninstall(config)

    assert run.code == 0, run.err
    assert "Kept OTEL_METRICS_EXPORTER, OTEL_TRACES_EXPORTER" in run.out
    assert FILE_VALUE not in run.out + run.err
    assert ENDPOINT not in run.out + run.err


# A console that cannot encode the path.


def test_output_survives_a_console_that_cannot_encode_the_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "config-\u8a2d\u5b9a"  # not in cp1252
    _setup(config_dir)
    args = build_parser().parse_args(["uninstall", "--config-dir", str(config_dir)])
    out_raw = io.BytesIO()
    out = io.TextIOWrapper(out_raw, encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    code = run_uninstall(args, environ={}, out=out, err=err, now=LATER)

    out.flush()
    assert code == 0
    assert "config-\\u8a2d\\u5b9a" in out_raw.getvalue().decode("cp1252")


# The command line.


def test_main_dispatches_to_uninstall(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config"
    _setup(config)

    exit_code = main(["uninstall", "--config-dir", str(config)])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert (config / "settings.json").read_bytes() == b"{}\n"
    assert f"Claude Code settings: {config / 'settings.json'}" in captured.out


def test_main_reads_claude_config_dir_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "env-config"))

    exit_code = main(["uninstall", "--dry-run"])

    assert exit_code == 0
    assert str(tmp_path / "env-config" / "settings.json") in capsys.readouterr().out
    assert not (tmp_path / "env-config").exists()


def test_help_lists_the_uninstall_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "uninstall" in capsys.readouterr().out


def test_python_dash_m_uninstall_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ccobs", "uninstall", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    for option in ("--config-dir", "--dry-run"):
        assert option in result.stdout
