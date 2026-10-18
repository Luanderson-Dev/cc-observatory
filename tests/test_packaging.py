"""Tests for packaging metadata that the CLI and ``uvx`` depend on."""

import importlib
import importlib.metadata
import subprocess
import sys

import pytest

import ccobs
from ccobs.cli import main


@pytest.mark.parametrize("script", ["ccobs", "cc-observatory"])
def test_console_script_points_to_cli_main(script: str) -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    (entry_point,) = (ep for ep in scripts if ep.name == script)

    assert entry_point.value == "ccobs.cli:main"
    assert entry_point.load() is main


def test_version_falls_back_when_package_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    try:
        assert importlib.reload(ccobs).__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(ccobs)


def test_testclient_imports_without_a_warning() -> None:
    """Starlette's TestClient runs on httpx2; on plain httpx it warns at import time.

    A fresh interpreter with every warning turned into an error, so the check does not
    depend on what this process imported first or on pytest's warning filters.
    """
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import fastapi.testclient"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
