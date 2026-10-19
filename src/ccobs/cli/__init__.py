"""Command-line entry point, installed as ``ccobs`` and ``cc-observatory``."""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from ccobs import __version__
from ccobs.cli import doctor, install, uninstall


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
    uninstall.configure_parser(
        commands.add_parser(
            "uninstall",
            help="take cc-observatory's hooks and telemetry out of Claude Code's user settings",
            description=(
                "Remove the hooks ccobs setup recognizes as its own and the telemetry "
                "variables it still owns from Claude Code's user settings, putting back the "
                "values it replaced. Keeps everything else in the file."
            ),
        )
    )
    doctor.configure_parser(
        commands.add_parser(
            "doctor",
            help="check Claude Code, its settings and policies, and the stack",
            description=(
                "Check what cc-observatory needs: Claude Code and Docker versions, the hooks "
                "and telemetry variables in your user settings, policies that block them, "
                "and the stack. Changes nothing. Exits 1 when a check fails."
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

    if args.command == "uninstall":
        return uninstall.run_uninstall(
            args, environ=os.environ, out=sys.stdout, err=sys.stderr, now=datetime.now(UTC)
        )

    if args.command == "doctor":
        return doctor.run_doctor(
            args, env=doctor.default_environment(), out=sys.stdout, err=sys.stderr
        )

    parser.print_help()
    return 0
