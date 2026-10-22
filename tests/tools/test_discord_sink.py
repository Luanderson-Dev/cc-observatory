"""Static checks of the Discord sink override (``tests/integration/compose.discord-sink.yaml``).

Pure unit tests: no Docker. test_app_alerts.py runs the override in a compose project of
its own to see the app's Discord call land on a sink instead of discord.com. These tests
pin what keeps that call on the machine: the host name resolves to the sink on every
network, edge has no route out, the app takes no proxy from the environment, and the
override stays out of what users and the ccobs-it project run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

from tools.stack.isolated import COMPOSE_FILES

ROOT: Final = Path(__file__).resolve().parents[2]
OVERRIDE: Final = ROOT / "tests" / "integration" / "compose.discord-sink.yaml"
EGRESS_SINK: Final = ROOT / "tools" / "stack" / "compose.egress-sink.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
SINK: Final = "discord-sink"
PROXY_VARIABLES: Final = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
)
LISTENER: Final = re.compile(r"\btcpsvd\b[^\n]*?\b0\.0\.0\.0 (?P<port>\d+)\b")


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _load(path: Path) -> dict[str, object]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


def _service(path: Path, name: str) -> dict[str, object]:
    return _mapping(_mapping(_load(path).get("services"), "services").get(name), name)


def _networks(path: Path) -> dict[str, object]:
    return _mapping(_load(path).get("networks"), f"{path.name} networks")


def test_override_is_loaded_by_nothing_but_its_test() -> None:
    assert OVERRIDE.name not in COMPOSE_FILES
    assert not OVERRIDE.name.startswith(("compose.override.", "docker-compose.override."))
    for path in (COMPOSE, ROOT / "compose.debug.yaml"):
        assert OVERRIDE.name not in path.read_text(encoding="utf-8"), path.name


def test_the_sink_uses_the_egress_sinks_image() -> None:
    # The egress sink's own test keeps its image on the collector Dockerfile's busybox,
    # which Dependabot updates.
    assert _service(OVERRIDE, SINK).get("image") == _service(EGRESS_SINK, "sink").get("image")


def test_the_sink_listens_on_https_only_and_publishes_nothing() -> None:
    sink = _service(OVERRIDE, SINK)
    lines = sink.get("command")
    assert isinstance(lines, list), lines
    command = "\n".join(str(item) for item in lines)
    assert {int(match["port"]) for match in LISTENER.finditer(command)} == {443}
    assert "ports" not in sink


@pytest.mark.parametrize("network", sorted(_networks(COMPOSE)))
def test_discord_com_resolves_to_the_sink_on_every_network(network: str) -> None:
    networks = _mapping(_service(OVERRIDE, SINK).get("networks"), "sink networks")
    assert set(networks) == set(_networks(COMPOSE))
    entry = _mapping(networks[network], network)
    assert entry.get("aliases") == ["discord.com"]


def test_edge_has_no_route_out() -> None:
    assert _networks(OVERRIDE) == {"edge": {"internal": True}}


def test_the_app_takes_no_proxy_and_waits_for_the_sink() -> None:
    app = _service(OVERRIDE, "app")
    environment = _mapping(app.get("environment"), "environment")
    assert set(environment) == PROXY_VARIABLES
    assert all(value == "" for value in environment.values())
    assert _mapping(app.get("depends_on"), "depends_on") == {SINK: {"condition": "service_healthy"}}
    # The webhook URL never appears in the override: the test passes it through the
    # environment of its compose commands only.
    assert "webhooks" not in OVERRIDE.read_text(encoding="utf-8")
