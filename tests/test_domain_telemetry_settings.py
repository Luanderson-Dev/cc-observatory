"""Unit tests of ``ccobs.domain.telemetry_settings``, the telemetry ``env`` block.

The block points Claude Code at the stack's OTLP/HTTP receiver on IPv4 loopback (ADR
0018) with cumulative temporality (ADR 0013), adds the beta tracing variables only on
request (README.md, "For traces"), and never writes a content variable, the session ID
variable or a per-signal OTLP variable (ADRs 0006, 0016 and 0036).
"""

from __future__ import annotations

import re
from typing import Final

import pytest

from ccobs.domain.telemetry_settings import (
    CONTENT_VARIABLES,
    DEFAULT_OTLP_ENDPOINT,
    PER_SIGNAL_ENDPOINT_VARIABLES,
    SESSION_ID_VARIABLE,
    TELEMETRY_VARIABLES,
    TRACES_VARIABLES,
    telemetry_env,
)

EXPECTED_DEFAULT: Final = [
    ("CLAUDE_CODE_ENABLE_TELEMETRY", "1"),
    ("OTEL_METRICS_EXPORTER", "otlp"),
    ("OTEL_LOGS_EXPORTER", "otlp"),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"),
    ("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "cumulative"),
]
EXPECTED_TRACES: Final = [
    ("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "1"),
    ("OTEL_TRACES_EXPORTER", "otlp"),
]
INVALID_ENDPOINTS: Final = (
    "http://localhost:4318",
    "https://127.0.0.1:4318",
    "http://[::1]:4318",
    "http://0.0.0.0:4318",
    "http://127.0.0.1:4318/v1/metrics",
    "http://127.0.0.1:0",
    "http://127.0.0.1:65536",
    "http://127.0.0.1:04318",
    "http://user@127.0.0.1:4318",
    "http://127.0.0.1:4318/",
    "http://127.0.0.1:4318?x=1",
    "http://127.0.0.1:4318#x",
    "http://127.0.0.1:",
    "127.0.0.1:4318",
    "",
)
_ERROR: Final = re.escape("must be http to 127.0.0.1 (not localhost,")


def test_the_default_endpoint_is_otlp_http_on_ipv4_loopback() -> None:
    assert DEFAULT_OTLP_ENDPOINT == "http://127.0.0.1:4318"


def test_the_default_block_in_its_fixed_order() -> None:
    env = telemetry_env()

    assert list(env.items()) == EXPECTED_DEFAULT
    assert telemetry_env(DEFAULT_OTLP_ENDPOINT) == env


def test_traces_append_the_beta_variables() -> None:
    env = telemetry_env(traces=True)

    assert list(env.items()) == EXPECTED_DEFAULT + EXPECTED_TRACES


def test_the_variable_name_constants_match_the_block() -> None:
    assert list(TELEMETRY_VARIABLES) == [name for name, _ in EXPECTED_DEFAULT]
    assert list(TRACES_VARIABLES) == [name for name, _ in EXPECTED_TRACES]
    assert tuple(telemetry_env(traces=True)) == TELEMETRY_VARIABLES + TRACES_VARIABLES


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:1", "http://127.0.0.1:65535"])
def test_a_custom_port(endpoint: str) -> None:
    env = telemetry_env(endpoint)

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == endpoint
    assert list(env) == list(TELEMETRY_VARIABLES)


def test_an_endpoint_without_a_port() -> None:
    env = telemetry_env("http://127.0.0.1")

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1"


@pytest.mark.parametrize("traces", [False, True])
@pytest.mark.parametrize("endpoint", INVALID_ENDPOINTS)
def test_an_invalid_endpoint_is_rejected_without_echoing_it(endpoint: str, traces: bool) -> None:
    with pytest.raises(ValueError, match=_ERROR) as error:
        telemetry_env(endpoint, traces=traces)

    if endpoint:
        assert endpoint not in str(error.value)


def test_the_content_variables() -> None:
    assert CONTENT_VARIABLES == (
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_ASSISTANT_RESPONSES",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "OTEL_LOG_RAW_API_BODIES",
        "ENABLE_BETA_TRACING_DETAILED",
        "BETA_TRACING_ENDPOINT",
    )


def test_the_session_id_variable() -> None:
    assert SESSION_ID_VARIABLE == "OTEL_METRICS_INCLUDE_SESSION_ID"


def test_the_per_signal_endpoint_variables() -> None:
    assert PER_SIGNAL_ENDPOINT_VARIABLES == (
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    )


@pytest.mark.parametrize("traces", [False, True])
def test_no_content_variable_is_produced(traces: bool) -> None:
    env = telemetry_env(traces=traces)

    assert not set(env) & set(CONTENT_VARIABLES)
    assert not [name for name in env if name.startswith("OTEL_LOG_")]


@pytest.mark.parametrize("traces", [False, True])
def test_the_session_id_variable_is_not_produced(traces: bool) -> None:
    assert SESSION_ID_VARIABLE not in telemetry_env(traces=traces)


@pytest.mark.parametrize("traces", [False, True])
def test_no_per_signal_endpoint_variable_is_produced(traces: bool) -> None:
    assert not set(telemetry_env(traces=traces)) & set(PER_SIGNAL_ENDPOINT_VARIABLES)


@pytest.mark.parametrize("traces", [False, True])
def test_every_value_is_a_string(traces: bool) -> None:
    # Claude Code only accepts string values in the env block of settings.json.
    env = telemetry_env(traces=traces)

    assert all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())


def test_each_call_returns_a_new_dict() -> None:
    first = telemetry_env()
    first["OTEL_METRICS_EXPORTER"] = "none"
    first["EXTRA"] = "1"

    second = telemetry_env()

    assert second is not first
    assert list(second.items()) == EXPECTED_DEFAULT
