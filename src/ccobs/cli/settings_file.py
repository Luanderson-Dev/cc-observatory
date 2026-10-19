"""Reading and writing Claude Code's user settings, for ``ccobs setup`` (ADR 0037).

The pure merge lives in ``ccobs.domain.settings_merge``; this module owns the file:

- the target: ``--config-dir``, else ``$CLAUDE_CONFIG_DIR`` (absolute), else
  ``~/.claude``; never a project's ``.claude/`` (ADR 0002);
- strict JSON in (no duplicate key, no number that is not finite, no lone surrogate,
  at most 100 levels deep), and the file's format back out: UTF-8 BOM, CRLF,
  indentation and the final newline are kept;
- an atomic write: a temporary file in the target's directory, then ``os.replace``,
  only while the file still holds what was read (by hash), through a symlink to its
  target, retried while Windows holds the file open;
- backups in ``<config dir>/ccobs/backups/`` (the last 10) and the setup state in
  ``<config dir>/ccobs/state.json``, both private to the user.

Errors never echo the file's content: the user's ``env`` can hold API keys.

Standard library only: the CLI stays light (tests/test_cli_light.py).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn

from ccobs.domain.settings_merge import SetupState, state_from_json, state_to_json

__all__ = [
    "BACKUPS_KEPT",
    "ConcurrentChangeError",
    "ConfigDirError",
    "LoadedSettings",
    "SettingsFileError",
    "backup_settings",
    "ccobs_dir",
    "delete_state",
    "read_settings",
    "read_state",
    "render",
    "resolve_config_dir",
    "restore_state",
    "settings_path",
    "snapshot_state",
    "state_path",
    "write_settings",
    "write_state",
]

BACKUPS_KEPT: Final = 10
CONFIG_DIR_VARIABLE: Final = "CLAUDE_CONFIG_DIR"

_BOM: Final = b"\xef\xbb\xbf"
_PRIVATE_FILE: Final = 0o600
_PRIVATE_DIR: Final = 0o700
_DEFAULT_INDENT: Final = 2
# os.replace fails with PermissionError on Windows while another process (an editor,
# an antivirus, Claude Code itself) holds the file open; it usually lets go quickly.
_REPLACE_ATTEMPTS: Final = 5
_RETRY_DELAY_S = 0.05  # not Final: tests set it to 0
_STATE_FILE: Final = "state.json"
_BACKUPS_DIR: Final = "backups"
_BACKUP_NAME: Final = re.compile(r"settings\.json\.\d{8}T\d{12}Z\.bak")
_FIRST_INDENT: Final = re.compile(r"\n([ \t]+)\S")
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
# Real settings nest a few levels; the merge copies the data recursively, which a
# deeper file could exhaust.
_MAX_DEPTH: Final = 100


class ConfigDirError(ValueError):
    """The configuration directory cannot be resolved."""


class SettingsFileError(ValueError):
    """The settings file cannot be read as a JSON object. The message holds at most a
    line and column, never the file's content."""


class ConcurrentChangeError(RuntimeError):
    """The settings file changed between the read and the write."""


@dataclass(frozen=True)
class LoadedSettings:
    """The parsed settings and how the file was formatted, to write it back alike."""

    data: dict[str, object]
    exists: bool
    digest: str | None  # sha256 of the raw bytes; None when the file is absent
    bom: bool = False
    newline: str = "\n"
    indent: int | str = _DEFAULT_INDENT
    final_newline: bool = True


# Paths.


def resolve_config_dir(arg: str | None, environ: Mapping[str, str]) -> Path:
    """Claude Code's configuration directory: ``arg`` (``--config-dir``, ``~``
    expanded), else ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``. Raises
    ``ConfigDirError`` when ``$CLAUDE_CONFIG_DIR`` is not an absolute path."""
    if arg is not None:
        if not arg.strip():
            # An empty path would be the working directory.
            raise ConfigDirError("--config-dir must not be empty")
        return Path(arg).expanduser().absolute()
    value = environ.get(CONFIG_DIR_VARIABLE, "")
    if value:
        path = Path(value)
        if not path.is_absolute():
            # The value is not echoed: an environment variable can hold anything.
            raise ConfigDirError(
                f"{CONFIG_DIR_VARIABLE} must be an absolute path; "
                "set it to one, or pass --config-dir"
            )
        return path
    return Path.home() / ".claude"


def settings_path(config_dir: Path) -> Path:
    return config_dir / "settings.json"


def ccobs_dir(config_dir: Path) -> Path:
    return config_dir / "ccobs"


# Reading.


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_constant(name: str) -> NoReturn:
    # Python reads NaN and Infinity; JSON (and Claude Code) does not. The constant is
    # not echoed, like any other content.
    raise ValueError(name)


def _finite_float(text: str) -> float:
    # 1e400 reads as inf, which would be written back as Infinity.
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


class _DuplicateKeyError(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Never repaired by keeping one of the values: the key is not echoed either, it
    # can be the user's data.
    data = dict(pairs)
    if len(data) != len(pairs):
        raise _DuplicateKeyError
    return data


def _check_values(data: object) -> None:
    """Raise ``SettingsFileError`` when ``data`` nests deeper than ``_MAX_DEPTH`` (the
    merge copies it recursively) or holds a lone surrogate in a key or a string (it
    cannot be written back as UTF-8). Iterative, so any depth is safe to walk."""
    stack: list[tuple[object, int]] = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            if _SURROGATE.search(value):
                raise SettingsFileError(
                    "the settings file is not valid JSON (a lone surrogate escape in a string)"
                )
            continue
        if isinstance(value, dict | list) and depth > _MAX_DEPTH:
            raise SettingsFileError(f"the settings file nests deeper than {_MAX_DEPTH} levels")
        if isinstance(value, dict):
            stack.extend((key, depth) for key in value)
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def read_settings(path: Path) -> LoadedSettings:
    """The settings in ``path``, or an empty object when the file does not exist.

    Raises ``SettingsFileError`` when the file cannot be read, is not UTF-8, is not
    strict JSON (an empty file is not, nor a duplicate key, a number that is not
    finite or a lone surrogate escape), nests too deep or is not a JSON object."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return LoadedSettings(data={}, exists=False, digest=None)
    except OSError as error:
        raise SettingsFileError(f"cannot read the settings file: {error.strerror}") from None
    bom = raw.startswith(_BOM)
    try:
        text = raw.removeprefix(_BOM).decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsFileError("the settings file is not UTF-8") from None
    try:
        data: object = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_keys,
        )
    except json.JSONDecodeError as error:
        raise SettingsFileError(
            f"the settings file is not valid JSON (line {error.lineno}, column {error.colno})"
        ) from None
    except _DuplicateKeyError:
        raise SettingsFileError(
            "the settings file is not valid JSON (an object holds a duplicate key)"
        ) from None
    except ValueError:
        # Python also refuses an integer of more than 4300 digits.
        raise SettingsFileError(
            "the settings file is not valid JSON (a number that is not finite or too long)"
        ) from None
    except RecursionError:
        raise SettingsFileError(
            f"the settings file nests deeper than {_MAX_DEPTH} levels"
        ) from None
    if not isinstance(data, dict):
        raise SettingsFileError("the settings file is not a JSON object")
    _check_values(data)
    match = _FIRST_INDENT.search(text)
    indent: int | str = _DEFAULT_INDENT
    if match is not None:
        whitespace = match[1]
        indent = "\t" if whitespace.startswith("\t") else len(whitespace)
    return LoadedSettings(
        data=data,
        exists=True,
        digest=_digest(raw),
        bom=bom,
        newline="\r\n" if "\r\n" in text else "\n",
        indent=indent,
        final_newline=text.endswith("\n"),
    )


def render(data: Mapping[str, object], fmt: LoadedSettings) -> bytes:
    """``data`` as JSON in the format of the file ``fmt`` was read from."""
    text = json.dumps(data, indent=fmt.indent, ensure_ascii=False, allow_nan=False)
    if fmt.final_newline:
        text += "\n"
    # JSON strings never hold a raw newline, so every "\n" is the layout's.
    encoded = text.replace("\n", fmt.newline).encode("utf-8")
    return _BOM + encoded if fmt.bom else encoded


# Writing.


def _posix() -> bool:
    return os.name == "posix"


def _set_mode(path: Path, mode: int) -> None:
    # On Windows os.chmod only toggles the read-only flag, so setup sets no permission
    # there and changes no ACL: a file gets the ACL it inherits from its directory,
    # which under the user's profile is private to the user, and under a --config-dir
    # elsewhere is whatever that directory grants.
    if _posix():
        os.chmod(path, mode)


def _replace(source: Path, target: Path) -> None:
    attempt = 1
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt >= _REPLACE_ATTEMPTS:
                raise
        attempt += 1
        time.sleep(_RETRY_DELAY_S)


def _atomic_write(target: Path, content: bytes, mode_of: Callable[[], int]) -> None:
    """Write ``content`` to a temporary file next to ``target``, then replace it.
    ``mode_of`` runs right before the replace and returns the mode to give the file;
    when it raises, nothing is written. No temporary file is ever left behind."""
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        _set_mode(temporary, mode_of())
        _replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _current(target: Path) -> tuple[str | None, int]:
    """The digest and permission bits of ``target``; (None, 0o600) when absent."""
    try:
        with target.open("rb") as file:
            raw = file.read()
            mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
    except FileNotFoundError:
        return None, _PRIVATE_FILE
    return _digest(raw), mode


def write_settings(path: Path, content: bytes, *, expected_digest: str | None) -> None:
    """Atomically replace the settings file (its target, when it is a symlink) with
    ``content``, keeping its permission bits (0o600 for a new file on POSIX).

    Raises ``ConcurrentChangeError``, writing nothing, unless the file still has
    ``expected_digest`` (None: the file must still be absent)."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(mode=_PRIVATE_DIR, parents=True, exist_ok=True)

    def unchanged_mode() -> int:
        digest, mode = _current(target)
        if digest != expected_digest:
            raise ConcurrentChangeError("the settings file changed since it was read")
        return mode

    _atomic_write(target, content, unchanged_mode)


def _write_private(directory: Path, name: str, content: bytes) -> Path:
    directory.mkdir(mode=_PRIVATE_DIR, parents=True, exist_ok=True)
    target = directory / name
    _atomic_write(target, content, lambda: _PRIVATE_FILE)
    return target


def backup_settings(path: Path, ccobs_dir: Path, *, now: datetime) -> Path:
    """Copy the settings file to ``<ccobs_dir>/backups/``, named by ``now`` in UTC, and
    keep it plus the ``BACKUPS_KEPT - 1`` newest others. Returns the backup's path.

    The new backup is kept even when it is not the newest by name (a clock set back):
    it is the only copy of the file setup is about to replace."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backups = ccobs_dir / _BACKUPS_DIR
    backup = _write_private(backups, f"settings.json.{stamp}.bak", path.read_bytes())
    # The stamp has a fixed width, so the names sort in time order.
    others = sorted(
        name for name in os.listdir(backups) if _BACKUP_NAME.fullmatch(name) and name != backup.name
    )
    for name in others[: -(BACKUPS_KEPT - 1)]:
        (backups / name).unlink(missing_ok=True)
    return backup


# State.


def read_state(ccobs_dir: Path) -> tuple[SetupState | None, bool]:
    """The state of the previous setup, and whether a state file was rejected. A
    missing file is (None, False); one that cannot be read or is not a valid state is
    (None, True), and is then treated as missing (ADR 0037)."""
    try:
        raw = state_path(ccobs_dir).read_bytes()
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        return state_from_json(json.loads(raw.decode("utf-8"))), False
    except ValueError:  # UnicodeDecodeError and JSONDecodeError included
        return None, True


def state_path(ccobs_dir: Path) -> Path:
    return ccobs_dir / _STATE_FILE


def snapshot_state(ccobs_dir: Path) -> bytes | None:
    """The state file's bytes as they are (valid or not), or None when there is none,
    for ``restore_state``. Raises ``OSError`` when it cannot be read."""
    try:
        return state_path(ccobs_dir).read_bytes()
    except FileNotFoundError:
        return None


def write_state(ccobs_dir: Path, state: SetupState) -> None:
    """Atomically write ``state`` to ``<ccobs_dir>/state.json`` (0o600 on POSIX)."""
    content = json.dumps(state_to_json(state), indent=2) + "\n"
    _write_private(ccobs_dir, _STATE_FILE, content.encode("utf-8"))


def restore_state(ccobs_dir: Path, previous: bytes | None) -> None:
    """Put back the state file that ``snapshot_state`` returned: rewrite its bytes, or
    remove the file when there was none."""
    if previous is None:
        delete_state(ccobs_dir)
    else:
        _write_private(ccobs_dir, _STATE_FILE, previous)


def delete_state(ccobs_dir: Path) -> None:
    """Remove the state file, if there is one."""
    state_path(ccobs_dir).unlink(missing_ok=True)
