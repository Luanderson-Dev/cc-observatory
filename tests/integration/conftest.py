"""Fixtures for the integration tests: one isolated compose project for the session.

The project ``ccobs-it`` runs ``compose.yaml`` plus ``compose.debug.yaml`` on ports of
its own (24xxx), so it can run next to the user's ``cc-observatory`` stack. Compose
reads ``.env.example``, never the repository's ``.env``, and inherited ``CCOBS_*`` and
``COMPOSE_*`` variables are dropped (``tools/stack/isolated.py``). A port that is taken
can move with ``CCOBS_IT_<NAME>``, such as ``CCOBS_IT_OTLP_HTTP_PORT=34318``.

The project is removed with its volumes at the end of the session, unless
``CCOBS_IT_KEEP=1``; the next session then reuses its containers, and ``service_logs``
only returns what they logged since that session started (``service_started`` tells
whether a container started in this session, so its start-up lines are among them).
Run with ``uv run pytest -m integration --no-cov``; Docker must be running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from tools.stack.isolated import (
    CLEANUP_COMMAND,
    KEEP_VARIABLE,
    PROJECT,
    RFC3339,
    compose_command,
    compose_environment,
    ports,
    started_since,
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
    """The ``ccobs-it`` project and how to drive it. ``since`` is when the test session
    started (RFC 3339), by the clock that stamps the container logs."""

    docker: str
    env: Mapping[str, str]
    ports: Mapping[str, int]
    since: str

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
        """The container logs of ``service`` since the test session started. With
        ``CCOBS_IT_KEEP=1``, `up` reuses the containers of an earlier session, whose
        lines (errors from stopping a backend, say) would otherwise come back too."""
        result = self.compose("logs", "--no-color", "--since", self.since, service)
        return result.stdout + result.stderr

    def started(self, service: str) -> bool:
        """Whether the container of ``service`` started (or restarted) after the test
        session started, so that ``logs`` holds its start-up lines. False for a
        container that ``CCOBS_IT_KEEP=1`` reused from an earlier session."""
        output = self.compose("ps", "--all", "--quiet", service).stdout
        containers = [line.strip() for line in output.splitlines() if line.strip()]
        assert len(containers) == 1, f"expected one {service} container, got {output!r}"
        # S603: as in compose, docker comes from shutil.which and the arguments are fixed.
        result = subprocess.run(  # noqa: S603
            [self.docker, "inspect", "--format", "{{.State.StartedAt}}", containers[0]],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"`docker inspect` of {service} failed:\n{result.stderr}")
        return started_since(result.stdout.strip(), self.since)

    def network(self, name: str) -> dict[str, object]:
        """``docker network inspect`` of the project's network ``name`` (``backend``,
        ``edge``), which Compose creates as ``<project>_<name>``."""
        # S603: as in compose, docker comes from shutil.which and the arguments are fixed.
        result = subprocess.run(  # noqa: S603
            [self.docker, "network", "inspect", f"{PROJECT}_{name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"`docker network inspect` of {name} failed:\n{result.stderr}")
        # A JSON list with one object per network named.
        entries = _json_objects(result.stdout)
        assert len(entries) == 1, f"expected one {name} network, got {result.stdout[:300]!r}"
        return entries[0]

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


def _daemon_now(docker: str) -> str:
    """Now by the Docker daemon's clock, which stamps the container logs: on Docker
    Desktop it is the VM's clock, which can drift from this machine's (WSL2 clocks
    drift after sleep). This machine's clock if the daemon gives no usable time."""
    # S603: as in Stack.compose, the executable comes from shutil.which.
    info = subprocess.run(  # noqa: S603
        [docker, "info", "--format", "{{.SystemTime}}"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    now = info.stdout.strip()
    if info.returncode == 0 and RFC3339.fullmatch(now):
        return now
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    docker = _docker()
    running = Stack(
        docker=docker,
        env=compose_environment(os.environ, chosen),
        ports=chosen,
        since=_daemon_now(docker),
    )
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
    """The container logs of a service since the test session started."""
    return stack.logs


@pytest.fixture(scope="session")
def service_started(stack: Stack) -> Callable[[str], bool]:
    """Whether a service's container started after the test session started."""
    return stack.started


@pytest.fixture(scope="session")
def network_inspect(stack: Stack) -> Callable[[str], dict[str, object]]:
    """``docker network inspect`` of one of the project's networks (``backend``,
    ``edge``)."""
    return stack.network


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
    """Grafana's root URL, ``http://localhost:<port>``. Grafana redirects any other host
    name there (enforce_domain, ADR 0017), so a client that connects to 127.0.0.1 (ADR
    0018) sends this URL's host and port as its ``Host`` header."""
    return f"http://localhost:{stack.ports['CCOBS_GRAFANA_PORT']}"


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
