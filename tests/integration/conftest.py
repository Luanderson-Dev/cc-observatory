"""Fixtures for the integration tests: one isolated compose project for the session.

The project ``ccobs-it`` runs ``compose.yaml`` plus ``compose.debug.yaml`` on ports of
its own (24xxx), so it can run next to the user's ``cc-observatory`` stack. Compose
reads ``.env.example``, never the repository's ``.env``, and inherited ``CCOBS_*`` and
``COMPOSE_*`` variables are dropped (``tools/stack/isolated.py``). A port that is taken
can move with ``CCOBS_IT_<NAME>``, such as ``CCOBS_IT_OTLP_HTTP_PORT=34318``.

The project is removed with its volumes at the end of the session, unless
``CCOBS_IT_KEEP=1``. Run with ``uv run pytest -m integration --no-cov``; Docker must be
running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from tools.stack.isolated import (
    CLEANUP_COMMAND,
    KEEP_VARIABLE,
    PROJECT,
    compose_command,
    compose_environment,
    ports,
)

ROOT: Final = Path(__file__).resolve().parents[2]
LOOPBACK: Final = "127.0.0.1"
# Seconds compose waits for every service to be healthy.
WAIT_TIMEOUT: Final = 180
# Seconds for a whole `up` (image builds included), for `down` and for any other command.
UP_TIMEOUT: Final = 900
DOWN_TIMEOUT: Final = 300
COMMAND_TIMEOUT: Final = 120


@dataclass(frozen=True)
class Stack:
    """The ``ccobs-it`` project and how to drive it."""

    docker: str
    env: Mapping[str, str]
    ports: Mapping[str, int]

    def compose(
        self, *args: str, check: bool = True, timeout: float = COMMAND_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Run ``docker compose`` for the project; with ``check``, fail the test on a
        non-zero exit, showing the output."""
        # S603 cannot see that this is safe: docker comes from shutil.which and every
        # argument is fixed by this module or its callers (ruff only trusts literals).
        result = subprocess.run(  # noqa: S603
            compose_command(self.docker, ROOT, *args),
            cwd=ROOT,
            env=dict(self.env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            pytest.fail(
                f"`docker compose {' '.join(args)}` exited with {result.returncode}:\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def logs(self, service: str) -> str:
        """The container logs of ``service``."""
        result = self.compose("logs", "--no-color", service)
        return result.stdout + result.stderr

    def health(self, service: str) -> str:
        """The health status of ``service`` (``healthy``, ``starting``, ...)."""
        output = self.compose("ps", "--all", "--format", "json", service).stdout
        lines = [line for line in output.splitlines() if line.strip()]
        # Compose prints one JSON object per line (older versions: one JSON list).
        entries = [item for line in lines for item in _json_objects(line)]
        assert len(entries) == 1, f"expected one {service} container, got {output!r}"
        return str(entries[0].get("Health", ""))

    def url(self, variable: str) -> str:
        """The base URL of the port published through ``variable``."""
        return f"http://{LOOPBACK}:{self.ports[variable]}"


def _json_objects(line: str) -> list[dict[str, object]]:
    document = json.loads(line)
    items = document if isinstance(document, list) else [document]
    return [item for item in items if isinstance(item, dict)]


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail(
            "the integration tests need Docker: install Docker (Docker Desktop on Windows "
            "and macOS) and make sure `docker` is on PATH",
            pytrace=False,
        )
    # S603: as in Stack.compose, the executable comes from shutil.which.
    info = subprocess.run(  # noqa: S603
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    if info.returncode != 0:
        pytest.fail(
            f"Docker is installed but the daemon does not answer; start Docker and retry:\n"
            f"{info.stderr.strip()}",
            pytrace=False,
        )
    return docker


def _diagnostics(running: Stack) -> str:
    """``ps`` and ``logs`` of the project, for a failed start; never raises."""
    parts: list[str] = []
    for args in (("ps", "--all"), ("logs", "--no-color")):
        try:
            result = running.compose(*args, check=False)
            output = result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError) as error:
            output = f"(failed: {error})"
        parts.append(f"--- docker compose {' '.join(args)} ---\n{output}")
    return "\n".join(parts)


def _tear_down(running: Stack) -> None:
    """Remove the project with its volumes. A failure only warns, with the command to
    clean up by hand, so that it never hides the failure of a test."""
    try:
        result = running.compose(
            "down", "--volumes", "--remove-orphans", check=False, timeout=DOWN_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as error:
        problem = f"failed: {error}"
    else:
        if result.returncode == 0:
            return
        problem = f"exited with {result.returncode}: {result.stderr.strip()}"
    warnings.warn(
        f"removing the {PROJECT} project {problem}; its containers restart with Docker "
        f"until you run: {CLEANUP_COMMAND}",
        stacklevel=1,
    )


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    """Start the ``ccobs-it`` project (building local images) and wait until every
    service is healthy; remove it with its volumes at the end."""
    try:
        chosen = ports(os.environ)
    except ValueError as error:
        pytest.fail(f"invalid port override: {error}", pytrace=False)
    running = Stack(docker=_docker(), env=compose_environment(os.environ, chosen), ports=chosen)
    keep = os.environ.get(KEEP_VARIABLE) == "1"
    # Everything from `up` on is inside the try: a timeout or Ctrl+C during `up` still
    # removes the containers, which `restart: unless-stopped` would bring back forever.
    try:
        up = running.compose(
            "up",
            "--detach",
            "--build",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            str(WAIT_TIMEOUT),
            check=False,
            timeout=UP_TIMEOUT,
        )
        if up.returncode != 0:
            pytest.fail(
                f"the {PROJECT} stack did not start (exit {up.returncode}):\n"
                f"{up.stdout}\n{up.stderr}\n{_diagnostics(running)}",
                pytrace=False,
            )
        yield running
    finally:
        if not keep:
            _tear_down(running)


@pytest.fixture(scope="session")
def service_health(stack: Stack) -> Callable[[str], str]:
    """The health status of a service (``healthy``, ``starting``, ...)."""
    return stack.health


@pytest.fixture(scope="session")
def service_logs(stack: Stack) -> Callable[[str], str]:
    """The container logs of a service."""
    return stack.logs


@pytest.fixture(scope="session")
def compose(stack: Stack) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run ``docker compose`` for the project, such as ``compose("stop", "loki")``; a
    test that stops a service must start it again and wait until it is healthy."""
    return stack.compose


@pytest.fixture(scope="session")
def otlp_http_url(stack: Stack) -> str:
    """Base URL of the collector's OTLP/HTTP receiver."""
    return stack.url("CCOBS_OTLP_HTTP_PORT")


@pytest.fixture(scope="session")
def otlp_grpc_address(stack: Stack) -> tuple[str, int]:
    """Host and port of the collector's OTLP/gRPC receiver."""
    return LOOPBACK, stack.ports["CCOBS_OTLP_GRPC_PORT"]


@pytest.fixture(scope="session")
def grafana_url(stack: Stack) -> str:
    """Base URL of Grafana (from the stage that adds it)."""
    return stack.url("CCOBS_GRAFANA_PORT")


@pytest.fixture(scope="session")
def prometheus_url(stack: Stack) -> str:
    """Base URL of Prometheus, published by the debug override."""
    return stack.url("CCOBS_PROMETHEUS_PORT")


@pytest.fixture(scope="session")
def loki_url(stack: Stack) -> str:
    """Base URL of Loki, published by the debug override."""
    return stack.url("CCOBS_LOKI_PORT")


@pytest.fixture(scope="session")
def tempo_url(stack: Stack) -> str:
    """Base URL of Tempo's HTTP API, published by the debug override."""
    return stack.url("CCOBS_TEMPO_PORT")
