"""``ccobs uninstall``: take cc-observatory's hooks and telemetry out of Claude Code's
user settings (ADRs 0011 and 0037).

The flow: resolve the target, read the file and the state, unmerge (pure, in
``ccobs.domain.settings_merge``), write nothing when nothing changes or with
``--dry-run`` (which prints a diff that masks every value setup did not write), else
back up, write atomically and delete the state. The report names keys, never values:
the user's ``env`` can hold API keys.

Standard library only: the CLI stays light (tests/test_cli_light.py).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, TextIO, cast

from ccobs.cli import settings_file
from ccobs.cli.masking import MANAGED_VARIABLES, masked_view_diff, reason
from ccobs.cli.settings_file import (
    ConcurrentChangeError,
    ConfigDirError,
    LoadedSettings,
    SettingsFileError,
)
from ccobs.domain.settings_merge import (
    SettingsShapeError,
    SetupState,
    UnmergeResult,
    is_ccobs_handler,
    unmerge,
)

__all__ = ["CCOBS_GROUP", "configure_parser", "masked_diff", "run_uninstall"]

CCOBS_GROUP: Final = "<cc-observatory group>"
_PREFIX: Final = "ccobs uninstall"


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the options of ``ccobs uninstall`` to its subparser."""
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Claude Code's configuration directory (default: $CLAUDE_CONFIG_DIR, else ~/.claude)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change, with your own values masked, and write nothing",
    )


# --dry-run.


def masked_diff(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    state: SetupState | None,
) -> list[str]:
    """A unified diff of the keys uninstall touches: the hook events that hold a handler
    setup recognizes (each group as a placeholder, never its content) and the telemetry
    variables, whose value is shown only when it is the one setup wrote (``state``)."""
    block = cast(Mapping[str, list[dict[str, object]]], before.get("hooks", {}))
    events: dict[str, Sequence[object]] = {
        event: ()
        for event, groups in block.items()
        if any(is_ccobs_handler(handler) for group in groups for handler in _handlers(group))
    }
    written = state.env_written if state is not None else {}
    shown = {key: frozenset({written[key]}) for key in MANAGED_VARIABLES if key in written}
    return masked_view_diff(
        before, after, events=events, shown_values=shown, recognized=CCOBS_GROUP
    )


def _handlers(group: Mapping[str, object]) -> list[object]:
    # The shape (a group is an object with a "hooks" list) was checked by unmerge.
    return cast(list[object], group["hooks"])


# The command.


def _names(keys: Sequence[str]) -> str:
    return ", ".join(keys)


def _delete_state(state_dir: Path, *, err: TextIO, out: TextIO) -> None:
    state_file = settings_file.state_path(state_dir)
    try:
        settings_file.delete_state(state_dir)
    except OSError as error:
        print(
            f"warning: could not remove {state_file}: {reason(error)}. It no longer matches "
            "settings.json; ccobs setup recognizes and replaces its entries.",
            file=err,
        )
        return
    print(f"Removed the setup state {state_file}.", file=out)


def _forget_state(
    state_dir: Path, state: SetupState | None, *, dry_run: bool, out: TextIO, err: TextIO
) -> None:
    """Delete the state, if there is one (only say so with ``dry_run``)."""
    if state is None:
        return
    if dry_run:
        print(f"Would remove the setup state {settings_file.state_path(state_dir)}.", file=out)
    else:
        _delete_state(state_dir, err=err, out=out)


def _env_lists(result: UnmergeResult, *, out: TextIO) -> None:
    """Name the telemetry variables left in the env (never their values), so telemetry
    is never left on silently (ADR 0037)."""
    if result.kept_env:
        print(f"Kept {_names(result.kept_env)}: you changed them after setup.", file=out)
    if result.unowned_env:
        print(
            f"Still set, not added by ccobs setup: {_names(result.unowned_env)}. Remove them "
            "from the env block yourself to stop sending telemetry.",
            file=out,
        )


def _env_report(
    before: Mapping[str, object], state: SetupState | None
) -> tuple[list[str], list[str]]:
    """The env keys uninstall removes and those it restores to the user's values."""
    env = before.get("env")
    if state is None or not isinstance(env, dict):
        return [], []
    owned = [key for key, written in state.env_written.items() if env.get(key) == written]
    removed = [key for key in owned if key not in state.env_previous]
    restored = [key for key in owned if key in state.env_previous]
    return removed, restored


def _write(
    path: Path,
    state_dir: Path,
    loaded: LoadedSettings,
    settings: Mapping[str, object],
    *,
    now: datetime,
    out: TextIO,
) -> tuple[str, int] | None:
    """Back the settings file up, then write ``settings`` to it. Returns None when
    done, else the failure's message and exit code."""
    try:
        backup = settings_file.backup_settings(path, state_dir, now=now)
    except OSError as error:
        return f"cannot back up the settings file: {reason(error)}", 2
    print(f"Backup: {backup}", file=out)
    try:
        settings_file.write_settings(
            path, settings_file.render(settings, loaded), expected_digest=loaded.digest
        )
    except ConcurrentChangeError:
        return "settings.json changed while uninstall ran; run it again", 1
    except OSError as error:
        return f"cannot write {path}: {reason(error)}", 2
    return None


def _report(
    result: UnmergeResult, before: Mapping[str, object], state: SetupState | None, *, out: TextIO
) -> None:
    print(f"Removed {result.removed_handlers} cc-observatory hook handlers.", file=out)
    removed, restored = _env_report(before, state)
    if removed:
        print(f"Telemetry variables removed: {_names(removed)}.", file=out)
    if restored:
        print(f"Telemetry variables restored to your earlier values: {_names(restored)}.", file=out)
    _env_lists(result, out=out)
    print(
        "Hooks stop in running Claude Code sessions now; telemetry continues in sessions "
        "already open until you restart them.",
        file=out,
    )
    print(
        "The stack keeps running: `docker compose down` in your cc-observatory clone stops "
        "it (`docker compose down -v` also deletes its data).",
        file=out,
    )


def run_uninstall(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    now: datetime,
) -> int:
    """Run ``ccobs uninstall`` with the parsed ``args``. Returns the exit code: 0 done
    (or nothing to do), 1 a concurrent change, 2 a usage or file error. The state is
    deleted only once settings.json no longer holds what it records."""
    config_arg: str | None = args.config_dir
    dry_run: bool = args.dry_run

    try:
        config_dir = settings_file.resolve_config_dir(config_arg, environ)
    except ConfigDirError as error:
        print(f"{_PREFIX}: {error}", file=err)
        return 2
    path = settings_file.settings_path(config_dir)
    state_dir = settings_file.ccobs_dir(config_dir)
    print(f"Claude Code settings: {path}", file=out)

    try:
        loaded = settings_file.read_settings(path)
    except SettingsFileError as error:
        print(f"{_PREFIX}: {error}; nothing was changed.", file=err)
        return 2
    state, rejected = settings_file.read_state(state_dir)
    if rejected:
        print(
            f"warning: ignored the setup state in {state_dir}, which is not valid; uninstall "
            "removes the hooks it recognizes and leaves the env and that file alone.",
            file=err,
        )

    if not loaded.exists:
        print("settings.json does not exist; nothing to remove from it.", file=out)
        _forget_state(state_dir, state, dry_run=dry_run, out=out, err=err)
        if dry_run:
            print("Dry run: nothing was written.", file=out)
        return 0

    try:
        result = unmerge(loaded.data, state=state)
    except SettingsShapeError as error:
        print(f"{_PREFIX}: {error}; nothing was changed.", file=err)
        return 2
    if not result.changed:
        print("There is nothing of cc-observatory's to remove from settings.json.", file=out)
        _forget_state(state_dir, state, dry_run=dry_run, out=out, err=err)
        _env_lists(result, out=out)
        if dry_run:
            print("Dry run: nothing was written.", file=out)
        return 0
    if dry_run:
        for line in masked_diff(loaded.data, result.settings, state=state):
            print(line, file=out)
        _env_lists(result, out=out)
        print("Dry run: nothing was written.", file=out)
        return 0

    failure = _write(path, state_dir, loaded, result.settings, now=now, out=out)
    if failure is not None:
        message, code = failure
        print(f"{_PREFIX}: {message}; settings.json was not changed.", file=err)
        return code
    _forget_state(state_dir, state, dry_run=False, out=out, err=err)
    _report(result, loaded.data, state, out=out)
    return 0
