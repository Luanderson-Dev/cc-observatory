"""Static checks of the dev-only egress sink override (``tools/stack/compose.egress-sink.yaml``).

Pure unit tests: no Docker. The override backs the manual check that nothing in the stack
reports usage or checks for updates (ADR 0006): every host name a component calls by
default resolves to a sink container that logs each connection. These tests pin its
structure, so that the procedure in its header keeps working, and that it stays out of
everything users and the integration tests run.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
import yaml

from tools.stack.isolated import COMPOSE_FILES, DEFAULT_PORTS

ROOT: Final = Path(__file__).resolve().parents[2]
OVERRIDE: Final = ROOT / "tools" / "stack" / "compose.egress-sink.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
COMPOSE_DEBUG: Final = ROOT / "compose.debug.yaml"
COLLECTOR_DOCKERFILE: Final = ROOT / "stack" / "otel-collector" / "Dockerfile"
# Where the isolated project's port overrides are documented, with example ports.
ISOLATED_DOCS: Final = (
    ROOT / "tools" / "stack" / "isolated.py",
    ROOT / "tests" / "integration" / "conftest.py",
)
SINK: Final = "sink"
PROJECT: Final = "ccobs-egress"
# Every host name a component of the stack calls by default: Grafana 13.2.4 (its
# conf/defaults.ini and the URLs hard-coded in its server), and the usage reporters of
# Loki 3.7.8 and Tempo 3.1.0. Prometheus and the collector call no host of their own.
OUTBOUND_HOSTS: Final = frozenset(
    {
        "stats.grafana.org",
        "grafana.com",
        "secure.gravatar.com",
        "snapshots.raintank.io",
        "secret-scanning.grafana.net",
    }
)
SINK_PORTS: Final = frozenset({80, 443})
# name:tag@sha256:<index digest> (ADR 0020).
PINNED_IMAGE: Final = re.compile(r"^busybox:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}$")
LISTENER: Final = re.compile(r"\btcpsvd\b[^\n]*?\b0\.0\.0\.0 (?P<port>\d+)\b")
# Compose interpolates ${NAME} and $NAME in the file; the sink's shell variables are
# written $$NAME, which Compose turns into $NAME.
INTERPOLATION: Final = re.compile(r"(?<!\$)\$(?:\{|[A-Za-z_])")
PORT_DEFAULT: Final = re.compile(r"\$\{(?P<name>CCOBS_[A-Z0-9_]+_PORT):-(?P<port>\d+)\}")
PORT_ASSIGNMENT: Final = re.compile(r"\b(?P<name>CCOBS_[A-Z0-9_]+_PORT)=(?P<port>\d+)\b")
FROM_LINE: Final = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)", re.I | re.M)
# The shell function of the procedure, with the port assignments in its command.
EGRESS_FUNCTION: Final = re.compile(r"egress\(\) \{\s*#?\s*CCOBS_[A-Z0-9_]+_PORT=")
# The single setting the negative control turns back on: Grafana's update check, which
# calls https://grafana.com/api/grafana/versions/stable at every start.
CONTROL_SETTING: Final = 'GF_ANALYTICS_CHECK_FOR_UPDATES: "true"'


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _load(path: Path) -> dict[str, object]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


def _services(path: Path) -> dict[str, dict[str, object]]:
    services = _mapping(_load(path).get("services"), f"{path.name} services")
    return {name: _mapping(item or {}, f"{path.name} {name}") for name, item in services.items()}


def _sink() -> dict[str, object]:
    return _services(OVERRIDE)[SINK]


def _networks(path: Path) -> dict[str, object]:
    return _mapping(_load(path).get("networks"), f"{path.name} networks")


def _strings(value: object, where: str) -> list[str]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return [str(item) for item in value]


def _header() -> str:
    lines = OVERRIDE.read_text(encoding="utf-8").splitlines()
    return "\n".join(itertools.takewhile(lambda line: line.startswith("#"), lines))


def _command() -> str:
    return "\n".join(_strings(_sink().get("command"), "sink command"))


def _compose_ports() -> dict[str, int]:
    """The port variables of compose.yaml with their defaults (the user's stack)."""
    text = COMPOSE.read_text(encoding="utf-8")
    return {match["name"]: int(match["port"]) for match in PORT_DEFAULT.finditer(text)}


STACK_SERVICES: Final = sorted(_services(COMPOSE))


# Where the override lives.


def test_override_is_a_dev_tool_that_nothing_loads_by_default() -> None:
    assert OVERRIDE.is_file()
    # Compose loads compose.override.yaml next to compose.yaml without being asked.
    assert not OVERRIDE.name.startswith(("compose.override.", "docker-compose.override."))
    assert OVERRIDE.parent != ROOT
    for path in (COMPOSE, COMPOSE_DEBUG):
        assert OVERRIDE.name not in path.read_text(encoding="utf-8"), path.name
    assert OVERRIDE.name not in COMPOSE_FILES


# The sink.


def test_sink_image_is_pinned() -> None:
    image = _sink().get("image")
    assert isinstance(image, str)
    assert PINNED_IMAGE.fullmatch(image), image


def test_sink_image_follows_the_collector_dockerfile() -> None:
    # Dependabot updates the busybox image of stack/otel-collector/Dockerfile but does not
    # read tools/stack/, so this test fails on its pull request until the sink follows.
    images = FROM_LINE.findall(COLLECTOR_DOCKERFILE.read_text(encoding="utf-8"))
    assert images
    assert _sink().get("image") == images[0]


def test_sink_listens_on_http_and_https() -> None:
    ports = {int(match["port"]) for match in LISTENER.finditer(_command())}
    assert ports == SINK_PORTS


def test_sink_publishes_nothing() -> None:
    assert "ports" not in _sink()


def test_sink_drops_privileges() -> None:
    sink = _sink()
    assert _strings(sink.get("cap_drop"), "cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in _strings(sink.get("security_opt"), "security_opt")


def test_sink_has_a_healthcheck_that_opens_no_connection() -> None:
    # A healthcheck that connected to the sink would show up in its log as a connection.
    test = _strings(_mapping(_sink().get("healthcheck"), "healthcheck").get("test"), "test")
    assert test[0] == "CMD"
    assert "netstat" in " ".join(test)


@pytest.mark.parametrize("network", sorted(_networks(COMPOSE)))
def test_every_outbound_host_resolves_to_the_sink_on_every_network(network: str) -> None:
    networks = _mapping(_sink().get("networks"), "sink networks")
    assert set(networks) == set(_networks(COMPOSE))
    entry = _mapping(networks[network], f"sink networks.{network}")
    assert set(_strings(entry.get("aliases"), f"{network} aliases")) == OUTBOUND_HOSTS


def test_compose_interpolates_nothing() -> None:
    text = OVERRIDE.read_text(encoding="utf-8")
    assert not INTERPOLATION.findall(text)


# What the override changes in the stack.


def test_override_changes_only_the_start_order_of_the_stack_services() -> None:
    services = _services(OVERRIDE)
    assert set(services) == {SINK, *STACK_SERVICES}
    for name in STACK_SERVICES:
        assert set(services[name]) == {"depends_on"}, name


@pytest.mark.parametrize("name", STACK_SERVICES)
def test_service_starts_once_the_sink_listens(name: str) -> None:
    # So the sink is there for the calls a service would make at start.
    depends_on = _mapping(_services(OVERRIDE)[name].get("depends_on"), f"{name} depends_on")
    assert depends_on == {SINK: {"condition": "service_healthy"}}


def test_edge_becomes_internal_so_no_other_host_is_reachable() -> None:
    # Any host the list misses then fails to connect instead of reaching the internet.
    assert set(_networks(COMPOSE)) == {"backend", "edge"}
    assert _networks(OVERRIDE) == {"edge": {"internal": True}}


# The procedure in the header.


def test_header_runs_a_separate_project_with_the_traces_profile() -> None:
    header = _header()
    assert f"-p {PROJECT}" in header
    assert "--profile traces" in header
    assert "tools/stack/compose.egress-sink.yaml" in header
    assert "down -v" in header
    assert CONTROL_SETTING in header


def test_header_moves_every_port_away_from_the_other_projects() -> None:
    moved: Mapping[str, int] = {
        match["name"]: int(match["port"]) for match in PORT_ASSIGNMENT.finditer(_header())
    }
    defaults = _compose_ports()
    assert set(moved) == set(defaults)
    # The user's stack, the isolated project, and the ports its docs give as examples of
    # CCOBS_IT_* overrides.
    examples = {
        int(match["port"])
        for path in ISOLATED_DOCS
        for match in PORT_ASSIGNMENT.finditer(path.read_text(encoding="utf-8"))
    }
    assert examples
    taken = set(defaults.values()) | set(DEFAULT_PORTS.values()) | examples
    assert not taken & set(moved.values())
    assert len(set(moved.values())) == len(moved)


def test_header_sets_the_ports_only_for_its_own_commands() -> None:
    # An export would stay in the shell, and a later `docker compose up -d` of the
    # user's stack in the same terminal would move OTLP and Grafana to these ports.
    header = _header()
    assert "export " not in header
    assert EGRESS_FUNCTION.search(header), header
