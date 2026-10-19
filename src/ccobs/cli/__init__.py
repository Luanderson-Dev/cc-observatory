"""Command-line entry point, installed as ``ccobs`` and ``cc-observatory``."""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from ccobs import __version__
from ccobs.cli import install


def build_parser() -> argparse.ArgumentParser:
    """The ``ccobs`` parser with its subcommands."""
    parser = argparse.ArgumentParser(
        prog="ccobs",
        description="Local observability for Claude Code.",
    )
    parser.add_argument("--version", action="store_true", help="show the version and exit")
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    install.configure_parser(
        commands.add_parser(
            "setup",
            help="add cc-observatory's hooks and telemetry to Claude Code's user settings",
            description=(
                "Add cc-observatory's hooks and telemetry variables to Claude Code's user "
                "settings, keeping everything else in the file. Never touches a project's "
                ".claude/ settings."
            ),
        )
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` (``sys.argv[1:]`` when ``None``) and return the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"ccobs {__version__}")
        return 0

    if args.command == "setup":
        return install.run_setup(
            args,
            environ=os.environ,
            out=sys.stdout,
            err=sys.stderr,
            probe=install.probe_app,
            now=datetime.now(UTC),
        )

    parser.print_help()
    return 0
