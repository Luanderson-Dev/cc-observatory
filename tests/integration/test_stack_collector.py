"""The OpenTelemetry Collector in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. These tests cover the collector
itself (health, OTLP endpoints, accepting every scenario, logs free of canaries); the
modules for each backend read the replayed data back from it.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pytest

from tools.stack.replay import (
    CANARY_PREFIX,
    SIGNALS,
    Prepared,
    ReplayError,
    Signal,
    load_scenario,
    post_otlp,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

COLLECTOR: Final = "otel-collector"


@dataclass(frozen=True)
class Replayed:
    """One scenario sent with canaries: what was prepared, how many requests went out
    and the error that stopped it, if any."""

    prepared: Prepared
    sent: int
    error: str | None


@pytest.fixture(scope="module")
def replayed(otlp_http_url: str) -> list[Replayed]:
    """Every scenario with OTLP data, prepared with canaries and sent to the collector."""
    now_ns = time.time_ns()
    results: list[Replayed] = []
    for name in scenario_names(with_otlp=True):
        prepared = prepare(load_scenario(name), now_ns=now_ns, canaries=True)
        try:
            results.append(Replayed(prepared, send(otlp_http_url, prepared), None))
        except ReplayError as error:
            results.append(Replayed(prepared, 0, str(error)))
    return results


def test_collector_is_healthy(service_health: Callable[[str], str]) -> None:
    assert service_health(COLLECTOR) == "healthy"


@pytest.mark.parametrize("signal", SIGNALS)
def test_empty_otlp_http_export_is_accepted(otlp_http_url: str, signal: Signal) -> None:
    assert post_otlp(otlp_http_url, signal, {}) == 200


def test_otlp_grpc_port_accepts_connections(otlp_grpc_address: tuple[str, int]) -> None:
    with socket.create_connection(otlp_grpc_address, timeout=5):
        pass


def test_every_scenario_replays_with_canaries(replayed: list[Replayed]) -> None:
    assert replayed, "no fixture scenario has OTLP data"
    failed = {item.prepared.scenario: item.error for item in replayed if item.error}
    assert not failed
    for item in replayed:
        total = sum(len(item.prepared.requests(signal)) for signal in SIGNALS)
        assert item.sent == total
        assert item.prepared.canary_values


def test_collector_logs_hold_no_canary(
    service_logs: Callable[[str], str], replayed: list[Replayed]
) -> None:
    # Only this session's lines. They can be none: the collector logs nothing while it
    # works, and with CCOBS_IT_KEEP=1 the session reuses a container that started
    # earlier. A failing `docker compose logs` fails the test.
    logs = service_logs(COLLECTOR)
    markers = {item.prepared.canary_marker for item in replayed}
    assert markers
    assert CANARY_PREFIX not in logs
    assert not [marker for marker in markers if marker and marker in logs]
