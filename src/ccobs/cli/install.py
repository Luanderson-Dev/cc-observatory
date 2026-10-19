"""``ccobs setup``: add cc-observatory's hooks and telemetry to Claude Code's user
settings (ADRs 0002, 0025, 0036 and 0037).

The flow: resolve the target, read the file and the state, merge (pure, in
``ccobs.domain.settings_merge``), stop on conflicts, write nothing when nothing changes
or with ``--dry-run`` (which prints a diff that masks every value setup did not write),
else record the state, back up, write atomically (putting the previous state back when
the settings file is not written) and probe the app.

Not named ``setup.py`` on purpose. Standard library only: the CLI stays light
(tests/test_cli_light.py).
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, TextIO, cast

from ccobs.cli import settings_file
from ccobs.cli.settings_file import (
    ConcurrentChangeError,
    ConfigDirError,
    LoadedSettings,
    SettingsFileError,
)
from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.settings_merge import SettingsShapeError, SetupState, is_ccobs_handler, merge
from ccobs.domain.telemetry_settings import TELEMETRY_VARIABLES, TRACES_VARIABLES, telemetry_env

__all__ = [
    "DEFAULT_APP_PORT",
    "DEFAULT_OTLP_PORT",
    "PROBE_TIMEOUT_S",
    "Probe",
    "configure_parser",
    "masked_diff",
    "probe_app",
    "run_setup",
]

# The stack's port block (ADR 0018); every URL is on IPv4 loopback.
DEFAULT_APP_PORT: Final = 4300
DEFAULT_OTLP_PORT: Final = 4318
PROBE_TIMEOUT_S: Final = 1.0
_HOST: Final = "127.0.0.1"
_MAX_PORT: Final = 65535
_HEALTH_PATH: Final = "/healthz"
_MANAGED_VARIABLES: Final = frozenset((*TELEMETRY_VARIABLES, *TRACES_VARIABLES))
YOUR_GROUP: Final = "<your hook group>"
EARLIER_GROUP: Final = "<earlier cc-observatory group>"
YOUR_VALUE: Final = "<your value>"

type Probe = Callable[[str, float], bool]
type _Hooks = dict[str, list[dict[str, object]]]


def _port(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= _MAX_PORT:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port (1-{_MAX_PORT})")
    return number


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the options of ``ccobs setup`` to its subparser."""
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Claude Code's configuration directory (default: $CLAUDE_CONFIG_DIR, else ~/.claude)",
    )
    parser.add_argument(
        "--app-port",
        type=_port,
        default=DEFAULT_APP_PORT,
        metavar="N",
        help=f"the app's port on 127.0.0.1, which receives the hooks (default {DEFAULT_APP_PORT})",
    )
    parser.add_argument(
        "--otlp-port",
        type=_port,
        default=DEFAULT_OTLP_PORT,
        metavar="N",
        help=f"the collector's OTLP/HTTP port on 127.0.0.1 (default {DEFAULT_OTLP_PORT})",
    )
    parser.add_argument(
        "--traces",
        action="store_true",
        help="also export traces (the stack must run with --profile traces)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace telemetry variables you set to other values (uninstall restores them)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change, with your own values masked, and write nothing",
    )


def probe_app(url: str, timeout: float) -> bool:
    """Whether a GET of ``url`` answers 2xx within ``timeout`` seconds; any error is
    False. Never goes through a proxy: the app is on loopback."""
    # Imported here: only setup needs them, not every ccobs command.
    import http.client
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout):
            return True  # an error status raises HTTPError
    except (OSError, ValueError, http.client.HTTPException):
        return False


# --dry-run.


def _identical(first: object, second: object) -> bool:
    # Equal and in the same key order ({"a": 1} == {"a": True} in Python).
    return json.dumps(first) == json.dumps(second)


def _group_view(group: Mapping[str, object], written: Sequence[object]) -> object:
    """``group`` itself only when it is one this run writes. Any other group is
    masked, even one setup recognizes as its own: a recognized curl handler can carry
    extra arguments (an Authorization header), its URL a token, and the group other
    keys. The shape (a group is an object with a "hooks" list) was checked by merge."""
    if any(_identical(group, ours) for ours in written):
        return group
    handlers = cast(list[object], group["hooks"])
    if handlers and all(is_ccobs_handler(handler) for handler in handlers):
        return EARLIER_GROUP
    # Not "unchanged": the merge removes our handlers from a group that mixes them.
    return YOUR_GROUP


def _view(
    settings: Mapping[str, object],
    hooks: Mapping[str, Sequence[object]],
    shown_values: Mapping[str, frozenset[str]],
) -> dict[str, object]:
    """The part of ``settings`` setup touches, with every value it did not write
    masked: the user's file can hold API keys and credentials."""
    view: dict[str, object] = {}
    block = settings.get("hooks")
    if isinstance(block, dict):
        events = {
            event: [_group_view(group, written) for group in block[event]]
            for event, written in hooks.items()
            if event in block
        }
        if events:
            view["hooks"] = events
    env = settings.get("env")
    if isinstance(env, dict):
        variables = {
            key: value
            if isinstance(value, str) and value in shown_values.get(key, frozenset())
            else YOUR_VALUE
            for key, value in env.items()
            if key in _MANAGED_VARIABLES
        }
        if variables:
            view["env"] = variables
    return view


def masked_diff(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    hooks: Mapping[str, Sequence[object]],
    env: Mapping[str, str],
    state: SetupState | None,
) -> list[str]:
    """A unified diff of the keys setup touches. A hook group is shown only when it is
    one this run writes, an env value only when it is what this run writes or what the
    previous run wrote (``state``); nothing else from the file ever appears."""
    written = state.env_written if state is not None else {}
    shown = {
        key: frozenset(value for value in (env.get(key), written.get(key)) if value is not None)
        for key in _MANAGED_VARIABLES
    }

    def lines(settings: Mapping[str, object]) -> list[str]:
        view = _view(settings, hooks, shown)
        return json.dumps(view, indent=2, ensure_ascii=False).splitlines()

    return list(
        difflib.unified_diff(
            lines(before),
            lines(after),
            fromfile="settings.json (before)",
            tofile="settings.json (after)",
            lineterm="",
        )
    )


# The command.


def _env_of(settings: Mapping[str, object]) -> Mapping[str, object]:
    env = settings.get("env")
    return env if isinstance(env, dict) else {}


def _env_changes(before: Mapping[str, object], after: Mapping[str, object]) -> str:
    old, new = _env_of(before), _env_of(after)
    managed = (*TELEMETRY_VARIABLES, *TRACES_VARIABLES)
    added = sum(1 for key in managed if key in new and key not in old)
    updated = sum(1 for key in managed if key in new and key in old and new[key] != old[key])
    removed = sum(1 for key in managed if key in old and key not in new)
    return f"{added} added, {updated} updated, {removed} removed"


def _conflict_message(conflicts: Sequence[str]) -> str:
    # Key names only: the values are the user's and can hold credentials.
    return "\n".join(
        [
            "ccobs setup: these telemetry variables in your settings hold other values:",
            *(f"  {key}" for key in conflicts),
            "Nothing was written. Re-run with --force to replace them; ccobs uninstall "
            "puts your values back.",
        ]
    )


def _unreachable_message(app_port: int) -> str:
    return (
        f"warning: the app at {_HOST}:{app_port} did not answer. Until the stack is up "
        "(`docker compose up -d` in your cc-observatory clone), every hook event shows a "
        "hook-error notice in Claude Code's transcript (Claude Code keeps working). "
        "Run `ccobs uninstall` to undo this setup."
    )


def _reason(error: OSError) -> str:
    # strerror only: str(error) and error.filename can name a temporary file.
    return error.strerror or type(error).__name__


def _back_up_and_write(
    path: Path,
    state_dir: Path,
    loaded: LoadedSettings,
    settings: Mapping[str, object],
    *,
    now: datetime,
    out: TextIO,
) -> tuple[str, int] | None:
    """Back the settings file up (when it exists), then write ``settings`` to it.
    Returns None when done, else the failure's message and exit code."""
    if loaded.exists:
        try:
            backup = settings_file.backup_settings(path, state_dir, now=now)
        except OSError as error:
            return f"cannot back up the settings file: {_reason(error)}", 2
        print(f"Backup: {backup}", file=out)
    try:
        settings_file.write_settings(
            path, settings_file.render(settings, loaded), expected_digest=loaded.digest
        )
    except ConcurrentChangeError:
        return "the file changed while setup ran; run it again", 1
    except OSError as error:
        return f"cannot write {path}: {_reason(error)}", 2
    return None


def run_setup(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    probe: Probe,
    now: datetime,
) -> int:
    """Run ``ccobs setup`` with the parsed ``args``. Returns the exit code: 0 done (or
    nothing to do), 1 an actionable refusal (conflicts, a concurrent change), 2 a
    usage or file error. Nothing is written unless every check passes."""
    config_arg: str | None = args.config_dir
    app_port: int = args.app_port
    otlp_port: int = args.otlp_port
    traces: bool = args.traces
    force: bool = args.force
    dry_run: bool = args.dry_run

    try:
        config_dir = settings_file.resolve_config_dir(config_arg, environ)
    except ConfigDirError as error:
        print(f"ccobs setup: {error}", file=err)
        return 2
    path = settings_file.settings_path(config_dir)
    state_dir = settings_file.ccobs_dir(config_dir)
    print(f"Claude Code settings: {path}", file=out)

    try:
        loaded = settings_file.read_settings(path)
    except SettingsFileError as error:
        print(f"ccobs setup: {error}; nothing was changed.", file=err)
        return 2
    state, rejected = settings_file.read_state(state_dir)
    if rejected:
        print(
            f"warning: ignored the setup state in {state_dir}, which is not valid; "
            "setup continues as if it had never run.",
            file=err,
        )

    hook_url = f"http://{_HOST}:{app_port}{HOOK_PATH}"
    hooks = cast(_Hooks, hook_settings(hook_url, windows=sys.platform == "win32")["hooks"])
    env = telemetry_env(f"http://{_HOST}:{otlp_port}", traces=traces)
    try:
        result = merge(loaded.data, hooks=hooks, env=env, state=state, force=force)
    except SettingsShapeError as error:
        print(f"ccobs setup: {error}; nothing was changed.", file=err)
        return 2
    if result.conflicts:
        print(_conflict_message(result.conflicts), file=err)
        return 1
    if not result.changed:
        print("The settings are already up to date; nothing was written.", file=out)
        return 0
    if dry_run:
        for line in masked_diff(loaded.data, result.settings, hooks=hooks, env=env, state=state):
            print(line, file=out)
        print("Dry run: nothing was written.", file=out)
        return 0

    state_file = settings_file.state_path(state_dir)
    try:
        previous_state = settings_file.snapshot_state(state_dir)
        settings_file.write_state(state_dir, result.state)
    except OSError as error:
        print(
            f"ccobs setup: cannot write {state_file}: {_reason(error)}; "
            "settings.json was not changed.",
            file=err,
        )
        return 2
    # The state goes first: uninstall needs env_previous for every settings file setup
    # wrote. If settings.json is not written after all, the previous state goes back.
    failure = _back_up_and_write(path, state_dir, loaded, result.settings, now=now, out=out)
    if failure is not None:
        message, code = failure
        print(f"ccobs setup: {message}; settings.json was not changed.", file=err)
        try:
            settings_file.restore_state(state_dir, previous_state)
        except OSError as error:
            print(
                f"ccobs setup: could not put back {state_file} either ({_reason(error)}); "
                "run ccobs setup again.",
                file=err,
            )
        return code

    print(
        f"Wrote the hooks for {len(hooks)} hook events; telemetry variables: "
        f"{_env_changes(loaded.data, result.settings)}.",
        file=out,
    )
    print(
        "Hooks apply to running Claude Code sessions now; telemetry starts in sessions you "
        "open from now on (restart the ones already open).",
        file=out,
    )
    if traces:
        print(
            "Traces: run the stack with `docker compose --profile traces up -d` (Tempo).",
            file=out,
        )
    if not probe(f"http://{_HOST}:{app_port}{_HEALTH_PATH}", PROBE_TIMEOUT_S):
        print(_unreachable_message(app_port), file=err)
    print(f"App: http://{_HOST}:{app_port}", file=out)
    return 0
