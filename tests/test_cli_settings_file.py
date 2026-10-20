"""Tests for ``ccobs.cli.settings_file``: reading and writing Claude Code's user settings.

The file handling of ADR 0037: target resolution, strict JSON, the format kept (BOM,
CRLF, indentation, final newline), an atomic write through symlinks after a hash check,
a retry while Windows holds the file, backups (the last 10) and the setup state.

No test touches the real ``~/.claude``: the autouse fixture points
``CLAUDE_CONFIG_DIR``, ``HOME`` and ``USERPROFILE`` at temporary directories. Modes are
checked for real only on POSIX; elsewhere ``_posix`` is patched to cover both branches.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import pytest

from ccobs.cli import settings_file
from ccobs.cli.settings_file import (
    ConcurrentChangeError,
    ConfigDirError,
    LoadedSettings,
    SettingsFileError,
    backup_settings,
    ccobs_dir,
    delete_state,
    read_settings,
    read_state,
    render,
    resolve_config_dir,
    restore_state,
    settings_path,
    snapshot_state,
    state_path,
    write_settings,
    write_state,
)
from ccobs.domain.settings_merge import SetupState, state_to_json

POSIX: Final = os.name == "posix"
BOM: Final = b"\xef\xbb\xbf"
# A value from the user's file, which messages must never echo.
FILE_VALUE: Final = "value-from-the-users-file"
NOW: Final = datetime(2026, 10, 19, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=1)))


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude (outside tmp_path, which tests list)."""
    isolation = tmp_path_factory.mktemp("isolation")
    home = isolation / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))
    return home


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _leftovers(directory: Path, keep: set[str]) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name not in keep)


# Target resolution.


def test_config_dir_argument_wins(tmp_path: Path) -> None:
    environ = {"CLAUDE_CONFIG_DIR": str(tmp_path / "from-env")}

    assert resolve_config_dir(str(tmp_path / "arg"), environ) == tmp_path / "arg"


def test_config_dir_argument_expands_the_home(isolated_home: Path) -> None:
    assert resolve_config_dir("~/custom", {}) == isolated_home / "custom"


def test_relative_config_dir_argument_becomes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert resolve_config_dir("relative", {}) == tmp_path / "relative"


@pytest.mark.parametrize("arg", ["", "   ", "\t"])
def test_an_empty_config_dir_argument_is_refused(
    arg: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty path would resolve to the working directory.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigDirError, match="--config-dir must not be empty"):
        resolve_config_dir(arg, {})


def test_claude_config_dir_is_used_when_set(tmp_path: Path) -> None:
    environ = {"CLAUDE_CONFIG_DIR": str(tmp_path / "from-env")}

    assert resolve_config_dir(None, environ) == tmp_path / "from-env"


def test_relative_claude_config_dir_is_refused() -> None:
    with pytest.raises(ConfigDirError, match="absolute"):
        resolve_config_dir(None, {"CLAUDE_CONFIG_DIR": "relative/dir"})


@pytest.mark.parametrize("environ", [{}, {"CLAUDE_CONFIG_DIR": ""}])
def test_default_config_dir_is_dot_claude_in_the_home(
    environ: dict[str, str], isolated_home: Path
) -> None:
    assert resolve_config_dir(None, environ) == isolated_home / ".claude"


def test_paths_under_the_config_dir(tmp_path: Path) -> None:
    assert settings_path(tmp_path) == tmp_path / "settings.json"
    assert ccobs_dir(tmp_path) == tmp_path / "ccobs"


# Reading.


def test_missing_file_reads_as_empty_with_default_format(tmp_path: Path) -> None:
    loaded = read_settings(tmp_path / "settings.json")

    assert loaded == LoadedSettings(
        data={}, exists=False, digest=None, bom=False, newline="\n", indent=2, final_newline=True
    )


def test_reads_data_digest_and_format(tmp_path: Path) -> None:
    raw = b'{\n  "model": "opus",\n  "env": {\n    "A": "1"\n  }\n}\n'
    path = tmp_path / "settings.json"
    path.write_bytes(raw)

    loaded = read_settings(path)

    assert loaded.data == {"model": "opus", "env": {"A": "1"}}
    assert list(loaded.data) == ["model", "env"]
    assert loaded.exists
    assert loaded.digest == _sha(raw)
    assert (loaded.bom, loaded.newline, loaded.indent, loaded.final_newline) == (
        False,
        "\n",
        2,
        True,
    )


@pytest.mark.parametrize(
    ("raw", "bom", "newline", "indent", "final_newline"),
    [
        pytest.param(BOM + b'{\n  "a": 1\n}\n', True, "\n", 2, True, id="bom"),
        pytest.param(b'{\r\n  "a": [\r\n    1\r\n  ]\r\n}\r\n', False, "\r\n", 2, True, id="crlf"),
        pytest.param(b'{\n    "a": {\n        "b": 1\n    }\n}\n', False, "\n", 4, True, id="four"),
        pytest.param(b'{\n\t"a": {\n\t\t"b": 1\n\t}\n}\n', False, "\n", "\t", True, id="tab"),
        pytest.param(b'{\n  "a": 1\n}', False, "\n", 2, False, id="no-final-newline"),
        pytest.param(b'{"a": 1}\n', False, "\n", 2, True, id="one-line"),
    ],
)
def test_detects_the_format_and_renders_it_back(
    tmp_path: Path,
    raw: bytes,
    bom: bool,
    newline: str,
    indent: int | str,
    final_newline: bool,
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(raw)

    loaded = read_settings(path)

    assert (loaded.bom, loaded.newline, loaded.indent, loaded.final_newline) == (
        bom,
        newline,
        indent,
        final_newline,
    )
    if b"\n " in raw or b"\n\t" in raw:
        assert render(loaded.data, loaded) == raw


def test_render_keeps_non_ascii_and_the_format(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(BOM + b'{\r\n    "a": 1\r\n}')
    loaded = read_settings(path)

    rendered = render({"a": 1, "name": "café"}, loaded)

    assert rendered == BOM + '{\r\n    "a": 1,\r\n    "name": "café"\r\n}'.encode()


def test_render_of_a_new_file_uses_two_spaces_lf_and_a_final_newline(tmp_path: Path) -> None:
    loaded = read_settings(tmp_path / "settings.json")

    assert render({"a": {"b": "c"}}, loaded) == b'{\n  "a": {\n    "b": "c"\n  }\n}\n'


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param(b'{"key": "' + FILE_VALUE.encode() + b'",}', "line 1, column", id="trailing"),
        pytest.param(b'{\n  // "' + FILE_VALUE.encode() + b'"\n}', "line 2, column", id="comment"),
        pytest.param(b"", "line 1, column 1", id="empty"),
        pytest.param(b"  \n\t\n", "line", id="whitespace"),
        pytest.param(BOM, "line 1, column 1", id="only-bom"),
        pytest.param(b'["' + FILE_VALUE.encode() + b'"]', "not a JSON object", id="array"),
        pytest.param(b'"' + FILE_VALUE.encode() + b'"', "not a JSON object", id="string"),
        pytest.param(b'{"a": "\xff' + FILE_VALUE.encode() + b'"}', "UTF-8", id="not-utf8"),
        pytest.param(b'{"a": NaN}', "not valid JSON", id="nan"),
        pytest.param(b'{"a": -Infinity}', "not valid JSON", id="infinity"),
        # Python reads these as inf and would write them back as Infinity.
        pytest.param(b'{"a": 1e400}', "not valid JSON", id="huge-float"),
        pytest.param(b'{"a": -1e400}', "not valid JSON", id="huge-negative-float"),
        pytest.param(b'{"a": ' + b"1" * 5000 + b"}", "not valid JSON", id="huge-int"),
        pytest.param(b'{"a": 1, "a": 2}', "duplicate key", id="duplicate-key"),
        pytest.param(
            b'{"env": {"' + FILE_VALUE.encode() + b'": "x", "' + FILE_VALUE.encode() + b'": "y"}}',
            "duplicate key",
            id="nested-duplicate-key",
        ),
        pytest.param(b'{"a": "\\ud83d' + FILE_VALUE.encode() + b'"}', "surrogate", id="surrogate"),
        pytest.param(b'{"\\udc00": 1}', "surrogate", id="surrogate-key"),
        pytest.param(b'{"a": [{"b": ["\\udfff"]}]}', "surrogate", id="nested-surrogate"),
        pytest.param(
            b'{"a": ' + b"[" * 100 + b"]" * 100 + b"}", "deeper than 100 levels", id="too-deep"
        ),
        pytest.param(
            b'{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
            "deeper than 100 levels",
            id="recursion",
        ),
    ],
)
def test_invalid_files_raise_without_their_content(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(raw)

    with pytest.raises(SettingsFileError, match=message) as excinfo:
        read_settings(path)

    assert FILE_VALUE not in str(excinfo.value)
    assert "NaN" not in str(excinfo.value)
    assert "Infinity" not in str(excinfo.value)
    assert "inf" not in str(excinfo.value)
    assert path.read_bytes() == raw


def test_nesting_up_to_the_limit_is_read(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"a": ' + b"[" * 99 + b"]" * 99 + b"}")  # 100 levels with the object

    loaded = read_settings(path)

    assert loaded.exists


def test_a_valid_surrogate_pair_is_read(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"a": "\\ud83d\\ude00"}')

    loaded = read_settings(path)

    assert loaded.data == {"a": "\U0001f600"}
    assert render(loaded.data, loaded) == '{\n  "a": "\U0001f600"\n}'.encode()


def test_finite_floats_are_read(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"a": 1.5, "b": -2e3, "c": 1e308}')

    assert read_settings(path).data == {"a": 1.5, "b": -2000.0, "c": 1e308}


def test_render_refuses_a_number_that_is_not_finite(tmp_path: Path) -> None:
    loaded = read_settings(tmp_path / "settings.json")

    with pytest.raises(ValueError, match="not JSON compliant"):
        render({"a": float("inf")}, loaded)


def test_unreadable_settings_raise_a_settings_file_error(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.mkdir()  # a directory cannot be read as a file on any OS

    with pytest.raises(SettingsFileError, match="cannot read"):
        read_settings(path)


# Writing.


def test_write_creates_the_directory_and_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "dir" / "settings.json"

    write_settings(path, b"{}\n", expected_digest=None)

    assert path.read_bytes() == b"{}\n"
    assert os.listdir(path.parent) == ["settings.json"]
    if POSIX:
        assert _mode(path) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_write_keeps_the_permission_bits_of_the_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")
    path.chmod(0o640)

    write_settings(path, b'{"a": 1}\n', expected_digest=_sha(b"{}\n"))

    assert path.read_bytes() == b'{"a": 1}\n'
    assert _mode(path) == 0o640


def _record_chmod(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    original = os.chmod

    def chmod(path: str | os.PathLike[str], mode: int) -> None:
        calls.append((os.fspath(path), mode))
        original(path, mode)

    monkeypatch.setattr(os, "chmod", chmod)
    return calls


def test_on_posix_the_temp_file_gets_the_mode_before_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_file, "_posix", lambda: True)
    calls = _record_chmod(monkeypatch)
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")
    existing = _mode(path)

    write_settings(path, b"[]\n", expected_digest=_sha(b"{}\n"))

    assert len(calls) == 1
    temp, mode = calls[0]
    assert Path(temp).parent == tmp_path
    assert Path(temp).name != "settings.json"
    assert mode == existing


def test_a_new_file_gets_mode_0600_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_file, "_posix", lambda: True)
    calls = _record_chmod(monkeypatch)

    write_settings(tmp_path / "settings.json", b"{}\n", expected_digest=None)

    assert [mode for _, mode in calls] == [0o600]


def test_elsewhere_no_mode_is_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_file, "_posix", lambda: False)
    calls = _record_chmod(monkeypatch)
    path = tmp_path / "settings.json"

    write_settings(path, b"{}\n", expected_digest=None)
    write_state(tmp_path / "ccobs", SetupState({}, {}, False, False, ()))
    path.write_bytes(b"{}\n")
    backup_settings(path, tmp_path / "ccobs", now=NOW)

    assert calls == []


def test_posix_is_keyed_on_os_name() -> None:
    assert settings_file._posix() is (os.name == "posix")


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        pytest.param(b"{}\n", _sha(b"{ }\n"), id="changed"),
        pytest.param(b"{}\n", None, id="created"),
        pytest.param(None, _sha(b"{}\n"), id="deleted"),
    ],
)
def test_a_concurrent_change_aborts_the_write(
    tmp_path: Path, existing: bytes | None, expected: str | None
) -> None:
    path = tmp_path / "settings.json"
    if existing is not None:
        path.write_bytes(existing)

    with pytest.raises(ConcurrentChangeError):
        write_settings(path, b'{"ours": 1}\n', expected_digest=expected)

    if existing is None:
        assert not path.exists()
        assert os.listdir(tmp_path) == []
    else:
        assert path.read_bytes() == existing
        assert os.listdir(tmp_path) == ["settings.json"]


def test_the_write_goes_through_a_symlink_to_its_target(tmp_path: Path) -> None:
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    target = real_dir / "claude-settings.json"
    target.write_bytes(b"{}\n")
    config = tmp_path / "config"
    config.mkdir()
    link = config / "settings.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this OS refuses to create symlinks here")

    write_settings(link, b'{"a": 1}\n', expected_digest=_sha(b"{}\n"))

    assert link.is_symlink()
    assert target.read_bytes() == b'{"a": 1}\n'
    assert os.listdir(config) == ["settings.json"]
    assert os.listdir(real_dir) == ["claude-settings.json"]


def _failing_replace(monkeypatch: pytest.MonkeyPatch, failures: int) -> Callable[[], int]:
    """Make the first ``failures`` calls of os.replace raise PermissionError."""
    original = os.replace
    attempts = 0

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise PermissionError(13, "The process cannot access the file")
        original(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(settings_file, "_RETRY_DELAY_S", 0)
    return lambda: attempts


def test_the_replace_is_retried_while_the_file_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _failing_replace(monkeypatch, failures=2)
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")

    write_settings(path, b'{"a": 1}\n', expected_digest=_sha(b"{}\n"))

    assert attempts() == 3
    assert path.read_bytes() == b'{"a": 1}\n'
    assert os.listdir(tmp_path) == ["settings.json"]


def test_five_failed_replaces_raise_and_leave_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _failing_replace(monkeypatch, failures=99)
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")

    with pytest.raises(PermissionError):
        write_settings(path, b'{"a": 1}\n', expected_digest=_sha(b"{}\n"))

    assert attempts() == 5
    assert path.read_bytes() == b"{}\n"
    assert os.listdir(tmp_path) == ["settings.json"]


def test_the_retry_waits_between_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _failing_replace(monkeypatch, failures=1)
    monkeypatch.setattr(settings_file, "_RETRY_DELAY_S", 0.01)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    write_settings(tmp_path / "settings.json", b"{}\n", expected_digest=None)

    assert sleeps == [0.01]


# Backups.


def test_backup_copies_the_file_under_a_utc_name(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"a": 1}\n')

    backup = backup_settings(path, tmp_path / "ccobs", now=NOW)

    assert backup == tmp_path / "ccobs" / "backups" / "settings.json.20261019T110000123456Z.bak"
    assert backup.read_bytes() == b'{"a": 1}\n'
    assert sorted(os.listdir(backup.parent)) == [backup.name]
    if POSIX:
        assert _mode(backup) == 0o600


def test_backup_and_state_get_mode_0600_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_file, "_posix", lambda: True)
    calls = _record_chmod(monkeypatch)
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")

    backup_settings(path, tmp_path / "ccobs", now=NOW)
    write_state(tmp_path / "ccobs", SetupState({}, {}, False, False, ()))

    assert [mode for _, mode in calls] == [0o600, 0o600]


def test_only_the_ten_newest_backups_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    backups = tmp_path / "ccobs" / "backups"
    backups.mkdir(parents=True)
    (backups / "notes.txt").write_text("the user's own file\n")
    names: list[str] = []
    for index in range(12):
        path.write_bytes(f'{{"run": {index}}}\n'.encode())
        names.append(
            backup_settings(path, tmp_path / "ccobs", now=NOW + timedelta(hours=index)).name
        )

    assert sorted(os.listdir(backups)) == sorted([*names[2:], "notes.txt"])
    assert (backups / names[-1]).read_bytes() == b'{"run": 11}\n'


def test_the_backup_just_made_is_kept_even_when_the_clock_went_back(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    backups = tmp_path / "ccobs" / "backups"
    names: list[str] = []
    for index in range(12):
        path.write_bytes(f'{{"run": {index}}}\n'.encode())
        names.append(
            backup_settings(path, tmp_path / "ccobs", now=NOW + timedelta(hours=index)).name
        )
    path.write_bytes(b'{"run": "skewed"}\n')

    backup = backup_settings(path, tmp_path / "ccobs", now=NOW - timedelta(days=1))

    assert backup.read_bytes() == b'{"run": "skewed"}\n'
    assert sorted(os.listdir(backups)) == sorted([backup.name, *names[3:]])


# State.

STATE: Final = SetupState(
    env_written={"CLAUDE_CODE_ENABLE_TELEMETRY": "1"},
    env_previous={},
    created_env=True,
    created_hooks=False,
    created_events=("Stop",),
)


def test_missing_state_is_none_and_not_rejected(tmp_path: Path) -> None:
    assert read_state(tmp_path / "ccobs") == (None, False)


def test_state_round_trip(tmp_path: Path) -> None:
    directory = tmp_path / "ccobs"

    write_state(directory, STATE)

    raw = (directory / "state.json").read_bytes()
    assert raw == (json.dumps(state_to_json(STATE), indent=2) + "\n").encode()
    assert b"\r\n" not in raw
    assert read_state(directory) == (STATE, False)
    assert os.listdir(directory) == ["state.json"]
    if POSIX:
        assert _mode(directory / "state.json") == 0o600


def test_write_state_replaces_the_previous_one(tmp_path: Path) -> None:
    directory = tmp_path / "ccobs"
    write_state(directory, SetupState({}, {}, False, False, ()))

    write_state(directory, STATE)

    assert read_state(directory) == (STATE, False)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{not json", id="invalid-json"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b'{"version": 1}', id="bad-shape"),
        pytest.param(
            json.dumps({**state_to_json(STATE), "env_written": {"PATH": "x"}}).encode(),
            id="foreign-env-key",
        ),
    ],
)
def test_an_invalid_state_is_rejected(tmp_path: Path, raw: bytes) -> None:
    directory = tmp_path / "ccobs"
    directory.mkdir()
    (directory / "state.json").write_bytes(raw)

    assert read_state(directory) == (None, True)


def test_an_unreadable_state_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "ccobs" / "state.json").mkdir(parents=True)

    assert read_state(tmp_path / "ccobs") == (None, True)


def test_delete_state(tmp_path: Path) -> None:
    directory = tmp_path / "ccobs"
    write_state(directory, STATE)

    delete_state(directory)
    delete_state(directory)  # already gone: no error

    assert read_state(directory) == (None, False)


def test_state_path(tmp_path: Path) -> None:
    assert state_path(tmp_path / "ccobs") == tmp_path / "ccobs" / "state.json"


def test_snapshot_and_restore_put_the_previous_state_back(tmp_path: Path) -> None:
    directory = tmp_path / "ccobs"
    directory.mkdir()
    previous = b'{"version": 99, "not": "a valid state"}'
    (directory / "state.json").write_bytes(previous)

    snapshot = snapshot_state(directory)
    write_state(directory, STATE)
    restore_state(directory, snapshot)

    assert snapshot == previous
    assert (directory / "state.json").read_bytes() == previous
    assert os.listdir(directory) == ["state.json"]
    if POSIX:
        assert _mode(directory / "state.json") == 0o600


def test_restoring_a_missing_state_deletes_the_new_one(tmp_path: Path) -> None:
    directory = tmp_path / "ccobs"

    snapshot = snapshot_state(directory)
    write_state(directory, STATE)
    restore_state(directory, snapshot)

    assert snapshot is None
    assert os.listdir(directory) == []


def test_an_unreadable_state_cannot_be_snapshot(tmp_path: Path) -> None:
    (tmp_path / "ccobs" / "state.json").mkdir(parents=True)

    with pytest.raises((IsADirectoryError, PermissionError)):
        snapshot_state(tmp_path / "ccobs")


def test_the_backup_name_is_in_utc(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")

    backup = backup_settings(
        path, tmp_path / "ccobs", now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    )

    assert backup.name == "settings.json.20260102T030405000000Z.bak"
