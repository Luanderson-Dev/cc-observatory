"""Tests for the settings of the isolated compose project (``tools/stack/isolated.py``).

Pure unit tests: no Docker. The integration tests' conftest builds every ``docker
compose`` call and its environment with these functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from tools.stack.isolated import (
    CLEANUP_COMMAND,
    COMPOSE_FILES,
    DEFAULT_PORTS,
    ENV_FILE,
    PROFILES,
    PROJECT,
    RFC3339,
    compose_command,
    compose_environment,
    ports,
    started_since,
)

ROOT: Final = Path(__file__).resolve().parents[2]
PORT_VARIABLE: Final = re.compile(r"\$\{(?P<name>CCOBS_[A-Z0-9_]+_PORT):-")


def test_default_ports_are_the_24xxx_block() -> None:
    assert DEFAULT_PORTS == {
        "CCOBS_OTLP_GRPC_PORT": 24317,
        "CCOBS_OTLP_HTTP_PORT": 24318,
        "CCOBS_GRAFANA_PORT": 24330,
        "CCOBS_PROMETHEUS_PORT": 24390,
        "CCOBS_LOKI_PORT": 24310,
        "CCOBS_TEMPO_PORT": 24320,
    }


def test_every_port_variable_of_the_compose_files_gets_an_isolated_port() -> None:
    # Otherwise ccobs-it would fall back to the user's default port and collide with
    # the cc-observatory stack.
    used = {
        match["name"]
        for name in COMPOSE_FILES
        for match in PORT_VARIABLE.finditer((ROOT / name).read_text(encoding="utf-8"))
    }
    assert used
    assert used <= set(DEFAULT_PORTS)


def test_ports_default_without_overrides() -> None:
    assert ports({"PATH": "/usr/bin", "CCOBS_OTLP_HTTP_PORT": "4318"}) == dict(DEFAULT_PORTS)


def test_ports_take_ccobs_it_overrides() -> None:
    found = ports({"CCOBS_IT_OTLP_HTTP_PORT": "34318", "CCOBS_IT_GRAFANA_PORT": " 34330 "})

    assert found["CCOBS_OTLP_HTTP_PORT"] == 34318
    assert found["CCOBS_GRAFANA_PORT"] == 34330
    assert found["CCOBS_OTLP_GRPC_PORT"] == DEFAULT_PORTS["CCOBS_OTLP_GRPC_PORT"]


@pytest.mark.parametrize("value", ["", "abc", "0", "65536", "-1", "24318.0"])
def test_ports_reject_an_invalid_override(value: str) -> None:
    with pytest.raises(ValueError, match="CCOBS_IT_LOKI_PORT"):
        ports({"CCOBS_IT_LOKI_PORT": value})


def test_ports_reject_two_variables_on_one_port() -> None:
    with pytest.raises(ValueError, match="24318"):
        ports({"CCOBS_IT_OTLP_GRPC_PORT": "24318"})


def test_compose_environment_drops_ccobs_and_compose_variables() -> None:
    environ = {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "CCOBS_OTLP_HTTP_PORT": "4318",
        "CCOBS_DISCORD_WEBHOOK_URL": "https://example.invalid/hook",
        "CCOBS_IT_KEEP": "1",
        "CCOBS_IT_OTLP_HTTP_PORT": "34318",
        "COMPOSE_PROJECT_NAME": "cc-observatory",
        "COMPOSE_FILE": "compose.yaml",
        "COMPOSE_PROFILES": "traces",
        "compose_env_files": ".env",
    }
    chosen = ports(environ)

    env = compose_environment(environ, chosen)

    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        **{name: str(port) for name, port in chosen.items()},
    }
    assert env["CCOBS_OTLP_HTTP_PORT"] == "34318"
    assert environ["CCOBS_OTLP_HTTP_PORT"] == "4318"


def test_compose_command_names_the_project_env_file_files_and_profiles() -> None:
    root = Path("/repo")

    command = compose_command("/bin/docker", root, "up", "--detach")

    expected = ["/bin/docker", "compose", "--project-name", PROJECT]
    expected += ["--env-file", str(root / ENV_FILE)]
    for name in COMPOSE_FILES:
        expected += ["--file", str(root / name)]
    for profile in PROFILES:
        expected += ["--profile", profile]
    assert command == [*expected, "up", "--detach"]
    assert PROJECT == "ccobs-it"
    assert ENV_FILE == ".env.example"
    assert COMPOSE_FILES == ("compose.yaml", "compose.debug.yaml")


def test_cleanup_command_removes_the_project_with_its_volumes() -> None:
    assert CLEANUP_COMMAND == "docker compose -p ccobs-it down -v --remove-orphans"


# When a container started, against the start of the test session.


@pytest.mark.parametrize(
    ("started_at", "since", "expected"),
    [
        # docker inspect writes nanoseconds in UTC; docker info writes the daemon's zone.
        ("2026-10-14T12:00:01.000000001Z", "2026-10-14T12:00:00.5Z", True),
        ("2026-10-14T12:00:00.5Z", "2026-10-14T12:00:01.000000001Z", False),
        ("2026-10-14T12:00:00Z", "2026-10-14T12:00:00Z", True),
        ("2026-10-14T12:00:00Z", "2026-10-14T09:00:01-03:00", False),
        ("2026-10-14T12:00:01Z", "2026-10-14T09:00:00.999999-03:00", True),
        # A container that never started.
        ("0001-01-01T00:00:00Z", "2026-10-14T12:00:00Z", False),
    ],
)
def test_started_since_compares_daemon_timestamps(
    started_at: str, since: str, expected: bool
) -> None:
    assert started_since(started_at, since) is expected


@pytest.mark.parametrize("value", ["", "2026-10-14", "2026-10-14 12:00:00Z", "12:00:00Z", "now"])
def test_started_since_refuses_what_is_not_rfc_3339(value: str) -> None:
    assert not RFC3339.fullmatch(value)
    with pytest.raises(ValueError, match="RFC 3339"):
        started_since(value, "2026-10-14T12:00:00Z")
    with pytest.raises(ValueError, match="RFC 3339"):
        started_since("2026-10-14T12:00:00Z", value)
