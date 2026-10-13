"""Command-line entry point, installed as ``ccobs`` and ``cc-observatory``."""

import argparse
from collections.abc import Sequence

from ccobs import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` (``sys.argv[1:]`` when ``None``) and return the exit code."""
    parser = argparse.ArgumentParser(
        prog="ccobs",
        description="Local observability for Claude Code.",
    )
    parser.add_argument("--version", action="store_true", help="show the version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"ccobs {__version__}")
        return 0

    parser.print_help()
    return 0
