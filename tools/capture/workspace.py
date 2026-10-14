"""Check and prepare capture workspaces, so a capture session sees none of your own setup.

Claude Code loads ``CLAUDE.md`` and ``CLAUDE.local.md`` from its working directory and
every directory above it, and rules, skills and agents from the ``.claude/`` directories
above it; a git repository around the workspace brings its branch and commits into the
context. ``--setting-sources project,local`` does not stop any of that, because those
files count as project or local sources. So a capture workspace must sit under a root
with none of them in it or above it (on Windows, outside your home, which has
``.claude/``), outside any git repository but its own, and outside any Claude Code
scratchpad. Each workspace gets a synthetic ``CLAUDE.md`` so InstructionsLoaded still
fires, as Project memory.

Commands (``python -m tools.capture.workspace``):

- ``check-root ROOT``: refuse a workspace root that breaks those rules;
- ``prepare WORKSPACE``: refuse a workspace that breaks them, else write the synthetic
  ``CLAUDE.md`` (``--exploratory`` only refuses a scratchpad and writes nothing).

Problems go to stderr with the paths involved (they stay on your terminal); the exit code
is 1 when there is any.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

SYNTHETIC_INSTRUCTIONS: Final = "Synthetic project instructions for the capture workspace.\n"
MEMORY_ENTRIES: Final = ("CLAUDE.md", "CLAUDE.local.md", ".claude")
GIT_TIMEOUT_SECONDS: Final = 10


def ancestors(path: Path) -> list[Path]:
    """The directories above ``path``, nearest first (replaced in tests)."""
    return list(path.parents)


def _memory_problems(directories: Iterable[Path]) -> list[str]:
    return [
        f"{directory / name} would load into the session"
        " (no CLAUDE.md, CLAUDE.local.md or .claude/ may sit above a capture workspace)"
        for directory in directories
        for name in MEMORY_ENTRIES
        if (directory / name).exists()
    ]


def _scratchpad_problems(path: Path) -> list[str]:
    if any(part.casefold() == "scratchpad" for part in path.parts):
        return [
            f"{path} is inside a Claude Code scratchpad; use a short neutral directory"
            " such as C:/ccobs-capture/<run> or /tmp/ccobs-capture/<run>"
        ]
    return []


def git_toplevel(path: Path) -> Path | None:
    """The top of the git work tree around ``path``; None outside any repository.

    Raises FileNotFoundError without git and RuntimeError when git fails otherwise.
    """
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git is not on PATH")
    # S603 cannot see that this is safe: git comes from shutil.which and the only
    # argument that is not fixed is the directory to look at.
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    if "not a git repository" in result.stderr:
        return None
    raise RuntimeError(result.stderr.strip() or f"git exited with status {result.returncode}")


def _git_problems(path: Path, allowed: Path | None) -> list[str]:
    """A repository around ``path`` other than ``allowed`` (a workspace's own)."""
    try:
        toplevel = git_toplevel(path)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return [f"cannot check {path} for a git repository around it ({exc})"]
    if toplevel is None or (allowed is not None and toplevel.samefile(allowed)):
        return []
    return [
        f"{path} is inside the git repository at {toplevel}, whose branch and commits would"
        " reach the session"
    ]


def root_problems(root: Path) -> list[str]:
    """Why ``root`` cannot hold capture workspaces (empty when it can)."""
    root = root.resolve()
    return [
        *_scratchpad_problems(root),
        *_memory_problems([root, *ancestors(root)]),
        *_git_problems(root, allowed=None),
    ]


def workspace_problems(workspace: Path) -> list[str]:
    """Why ``workspace`` cannot host a capture session (empty when it can).

    Its own git repository is fine (edit-commit-tasks makes one), and so is a
    ``CLAUDE.md`` holding exactly SYNTHETIC_INSTRUCTIONS.
    """
    workspace = workspace.resolve()
    problems = [*_scratchpad_problems(workspace), *_memory_problems(ancestors(workspace))]
    problems += [
        f"{workspace / name} would load into the session"
        for name in ("CLAUDE.local.md", ".claude")
        if (workspace / name).exists()
    ]
    instructions = workspace / "CLAUDE.md"
    if instructions.exists() and not (
        instructions.is_file()
        and instructions.read_text(encoding="utf-8") == SYNTHETIC_INSTRUCTIONS
    ):
        problems.append(f"{instructions} is not the synthetic CLAUDE.md of a capture workspace")
    return problems + _git_problems(workspace, allowed=workspace)


def prepare_workspace(workspace: Path) -> list[str]:
    """workspace_problems, and when there are none, the synthetic ``CLAUDE.md``."""
    problems = workspace_problems(workspace)
    instructions = workspace / "CLAUDE.md"
    if not problems and not instructions.exists():
        instructions.write_text(SYNTHETIC_INSTRUCTIONS, encoding="utf-8", newline="\n")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``check-root`` or ``prepare``; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.capture.workspace",
        description="Check and prepare capture workspaces.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_root = commands.add_parser("check-root", help="check a workspace root")
    check_root.add_argument("root", type=Path)
    prepare = commands.add_parser("prepare", help="check a workspace and write its CLAUDE.md")
    prepare.add_argument("workspace", type=Path)
    prepare.add_argument(
        "--exploratory",
        action="store_true",
        help="only refuse a scratchpad, write nothing (CAPTURE_ISOLATION=0)",
    )
    args = parser.parse_args(argv)
    if args.command == "check-root":
        problems = root_problems(args.root)
    elif args.exploratory:
        problems = _scratchpad_problems(args.workspace.resolve())
    else:
        problems = prepare_workspace(args.workspace)
    for problem in problems:
        print(f"refusing: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
