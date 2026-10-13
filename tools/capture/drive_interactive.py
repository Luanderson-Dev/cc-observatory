"""Drive an interactive Claude Code session in a pseudo-terminal. Windows only (pywinpty).

Some hook events only fire in interactive sessions: permission dialogs, notifications,
``/compact``, ``/exit`` and MCP elicitation. This script starts the given command in a
pywinpty pseudo-terminal, accepts the workspace trust dialog, types prompts and keys, and
waits on the hook events that the capture server appends to ``--hooks``. Scenarios:

- ``permission-compact``: approve one permission dialog, deny a second one with Esc,
  wait for ``idle_prompt``, then ``/compact`` and ``/exit``;
- ``elicitation-idle``: decline an MCP elicitation (``elicit_server.py``) with Esc, wait
  for ``idle_prompt``, then ``/exit``.

Start Claude Code through ``run_claude.sh``, which checks the workspace and writes its
synthetic ``CLAUDE.md`` (``tools/capture/workspace.py``) and isolates the session from
your own setup by default (``--setting-sources project,local``, ``--strict-mcp-config``,
``--no-chrome``, no auto memory, no claude.ai connectors; see ``CAPTURE_ISOLATION``
there).

pywinpty is not a dev dependency; run this with ``uv run --with pywinpty`` (see
``tools/capture/README.md``). Each run calls the Anthropic API and costs a little money.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    # Only for annotations: the script also runs as a plain file, where the tools package
    # is not importable.
    from tools.capture.jsontypes import JSON, JSONObject

try:
    from winpty import PtyProcess
except ImportError as exc:  # Not Windows, or started without --with pywinpty.
    raise SystemExit(
        "drive_interactive.py needs Windows and pywinpty:"
        " uv run --with pywinpty python tools/capture/drive_interactive.py ..."
    ) from exc

ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]")
ENTER: Final = "\r"
ESC: Final = "\x1b"
ARROW_DOWN: Final = "\x1b[B"
# Seconds to wait for the prompt after startup, and before answering a dialog so the
# notification that Claude Code sends about 6 s after it opens gets captured.
SETTLE: Final = 6.0
DIALOG_WAIT: Final = 9.0


def log(message: str) -> None:
    """Print a timestamped progress line."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Session:
    """A Claude Code process in a pseudo-terminal and the hook events it produced."""

    def __init__(self, command: Sequence[str], hooks: Path, screen_log: Path) -> None:
        self._hooks = hooks
        self._screen_log = screen_log
        self._screen: list[str] = []
        self._proc = PtyProcess.spawn(list(command), dimensions=(40, 140))
        threading.Thread(target=self._read_screen, daemon=True).start()

    def _read_screen(self) -> None:
        with self._screen_log.open("w", encoding="utf-8", newline="\n") as fh:
            while self._proc.isalive():
                try:
                    data = str(self._proc.read(4096))
                except EOFError:
                    return
                text = ANSI.sub("", data)
                self._screen.append(text)
                fh.write(text)
                fh.flush()

    def screen(self, chars: int = 3000) -> str:
        """The last ``chars`` characters of terminal text, without escape codes."""
        return "".join(self._screen)[-chars:]

    def events(self) -> list[JSONObject]:
        """Hook payloads captured so far, in arrival order."""
        if not self._hooks.exists():
            return []
        events: list[JSONObject] = []
        for line in self._hooks.read_text(encoding="utf-8").splitlines():
            record: JSON = json.loads(line) if line.strip() else None
            body = record.get("body") if isinstance(record, dict) else None
            if isinstance(body, dict):
                events.append(body)
        return events

    def wait_event(self, name: str, timeout: float, after: int = 0, **match: str) -> int:
        """Wait for hook event ``name`` past index ``after``; return the index after it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            for index, event in enumerate(events[after:], start=after):
                if event.get("hook_event_name") == name and all(
                    event.get(key) == value for key, value in match.items()
                ):
                    return index + 1
            time.sleep(0.5)
        raise TimeoutError(f"no {name} {match} within {timeout} s")

    def try_wait_event(self, name: str, timeout: float, after: int = 0, **match: str) -> int:
        """Like ``wait_event``, but log a timeout and return ``after`` instead of raising."""
        try:
            index = self.wait_event(name, timeout, after, **match)
        except TimeoutError as exc:
            log(str(exc))
            return after
        log(f"{name} seen")
        return index

    def press(self, keys: str) -> None:
        """Send raw keys."""
        self._proc.write(keys)

    def type_text(self, text: str) -> None:
        """Type ``text`` like a person would, then press Enter."""
        for char in text:
            self._proc.write(char)
            time.sleep(0.02)
        time.sleep(0.4)
        self._proc.write(ENTER)

    def accept_trust_dialog(self, timeout: float = 40.0) -> None:
        """Accept the workspace trust dialog if it shows up (arrow down, then Enter)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            screen = self.screen()
            if "trust" in screen.lower() and "Yes" in screen:
                log("trust dialog: move to 'Yes' and accept")
                time.sleep(1)
                self.press(ARROW_DOWN)
                time.sleep(0.5)
                self.press(ENTER)
                time.sleep(3)
                return
            started = any(e.get("hook_event_name") == "SessionStart" for e in self.events())
            if started and ">" in screen:
                return
            time.sleep(1)

    def exit(self, after: int) -> None:
        """Type ``/exit`` and wait for SessionEnd."""
        log("/exit")
        self.type_text("/exit")
        self.try_wait_event("SessionEnd", 30, after=after)

    def close(self) -> None:
        """Stop the process if it is still running."""
        time.sleep(2)
        if self._proc.isalive():
            self._proc.terminate(force=True)


def permission_compact(session: Session) -> None:
    """Approve a permission dialog, deny another with Esc, idle, ``/compact``, ``/exit``."""
    log("prompt 1: permission dialog, approve")
    session.type_text("Run the Bash command: echo hello > created.txt")
    after = session.wait_event("PermissionRequest", 60)
    log(f"PermissionRequest seen; waiting {DIALOG_WAIT} s for the permission_prompt notification")
    time.sleep(DIALOG_WAIT)
    session.press(ENTER)  # First option: Yes.
    after = session.wait_event("Stop", 90, after=after)
    log("Stop after approval")
    time.sleep(3)
    log("prompt 2: permission dialog, deny with Esc")
    session.type_text("Run the Bash command: echo again > second.txt")
    after = session.wait_event("PermissionRequest", 60, after=after)
    time.sleep(2)
    session.press(ESC)
    log("denied; waiting for the idle_prompt notification (about 60 s)")
    after = session.try_wait_event("Notification", 100, after, notification_type="idle_prompt")
    log("/compact")
    session.type_text("/compact")
    after = session.try_wait_event("PostCompact", 120, after=after)
    time.sleep(3)
    session.exit(after)


def elicitation_idle(session: Session) -> None:
    """Decline an MCP elicitation with Esc, stay idle until ``idle_prompt``, ``/exit``."""
    log("prompt: call the elicitation tool")
    session.type_text(
        "Call the pick_color tool from the spike-elicit MCP server once, then reply with one word."
    )
    after = session.wait_event("Elicitation", 90)
    log(f"Elicitation seen; waiting {DIALOG_WAIT} s for its notification, then Esc")
    time.sleep(DIALOG_WAIT)
    session.press(ESC)
    after = session.try_wait_event("ElicitationResult", 30, after=after)
    after = session.wait_event("Stop", 90, after=after)
    log("Stop seen; idle, waiting up to 100 s for idle_prompt")
    after = session.try_wait_event("Notification", 100, after, notification_type="idle_prompt")
    session.exit(after)


SCENARIOS: Final[dict[str, Callable[[Session], None]]] = {
    "permission-compact": permission_compact,
    "elicitation-idle": elicitation_idle,
}


def main(argv: Sequence[str] | None = None) -> None:
    """Run one interactive scenario against the command given after ``--``."""
    parser = argparse.ArgumentParser(
        prog="drive_interactive.py",
        description="Drive an interactive Claude Code session (Windows only).",
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument(
        "--hooks", type=Path, required=True, help="JSONL file the capture server appends to"
    )
    parser.add_argument(
        "--screen-log", type=Path, help="terminal text log (default: next to --hooks)"
    )
    parser.add_argument("command", nargs="+", help="command that starts Claude Code, after --")
    args = parser.parse_args(argv)
    screen_log = args.screen_log or args.hooks.with_name(f"screen-{args.scenario}.log")
    session = Session(args.command, args.hooks, screen_log)
    try:
        session.accept_trust_dialog()
        time.sleep(SETTLE)
        SCENARIOS[args.scenario](session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
