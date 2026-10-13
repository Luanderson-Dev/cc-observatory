"""Tests for the ``ccobs`` command-line entry point."""

import subprocess
import sys
from importlib.metadata import version

import pytest

import ccobs
from ccobs.cli import main

DIST_VERSION = version("cc-observatory")


def test_package_exposes_distribution_version() -> None:
    assert ccobs.__version__ == DIST_VERSION


def test_version_flag_prints_prog_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"ccobs {DIST_VERSION}\n"
    assert captured.err == ""


def test_no_arguments_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith("usage: ccobs")
    assert "--version" in captured.out
    assert captured.err == ""


def test_python_dash_m_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ccobs", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ccobs {DIST_VERSION}"
