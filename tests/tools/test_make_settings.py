"""Tests for the capture tool's hook settings generator."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.capture import make_settings
from tools.capture.make_settings import HTTP_EVENTS, NEVER_REGISTERED, VARIANTS, main

FORBIDDEN = {
    "WorktreeCreate",
    "WorktreeRemove",
    "PreModelSwitch",
    "MessageDisplay",
    "FileChanged",
    "Setup",
}


def build(variant: str, port: int, timeout: float, **options: str) -> dict[str, Any]:
    """make_settings.build, typed loosely so the tests can index into the settings."""
    return make_settings.build(variant, port, timeout, **options)


def hooks_of(settings: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for event, groups in settings["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, hook


def dicts_in(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dicts_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from dicts_in(child)


def test_variants_are_http_command_and_the_sessionstart_probe() -> None:
    assert VARIANTS == ("http", "command", "probe-sessionstart-http")
    assert FORBIDDEN <= NEVER_REGISTERED


@pytest.mark.parametrize("variant", VARIANTS)
def test_forbidden_events_are_never_registered(variant: str) -> None:
    settings = build(variant, port=8790, timeout=2)

    assert FORBIDDEN.isdisjoint(settings["hooks"])
    assert set(settings["hooks"]) == {*HTTP_EVENTS, "SessionStart"}


@pytest.mark.parametrize("variant", VARIANTS)
def test_no_status_message_or_on_failure_anywhere(variant: str) -> None:
    settings = build(variant, port=8790, timeout=2)

    for item in dicts_in(settings):
        assert "statusMessage" not in item
        assert "onFailure" not in item


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_hook_has_an_explicit_timeout_and_a_loopback_url(variant: str) -> None:
    settings = build(variant, port=8790, timeout=2)

    for _, hook in hooks_of(settings):
        assert isinstance(hook["timeout"], int | float)
        assert 0 < hook["timeout"] <= 5
        target = hook["url"] if hook["type"] == "http" else hook["args"][-1]
        assert target.startswith("http://127.0.0.1:8790/hooks/")


def test_http_variant_uses_http_hooks_and_an_async_command_hook_for_sessionstart() -> None:
    settings = build("http", port=8790, timeout=2)

    for event, hook in hooks_of(settings):
        if event == "SessionStart":
            assert hook["type"] == "command"
            assert hook["async"] is True
            assert hook["command"] == "curl"
            assert hook["args"][-1] == "http://127.0.0.1:8790/hooks/command"
        else:
            assert hook == {
                "type": "http",
                "url": "http://127.0.0.1:8790/hooks/http",
                "timeout": 2,
            }


def test_command_variant_uses_async_curl_everywhere() -> None:
    settings = build("command", port=8790, timeout=2)

    for _, hook in hooks_of(settings):
        assert hook["type"] == "command"
        assert hook["async"] is True
        assert hook["args"][-1] == "http://127.0.0.1:8790/hooks/command"


@pytest.mark.parametrize("variant", ["http", "command"])
def test_sessionstart_is_never_http_outside_the_probe(variant: str) -> None:
    settings = build(variant, port=8790, timeout=2)

    assert [hook["type"] for event, hook in hooks_of(settings) if event == "SessionStart"] == [
        "command"
    ]


def test_probe_variant_registers_sessionstart_and_every_other_event_as_http() -> None:
    settings = build("probe-sessionstart-http", port=8790, timeout=2)

    assert {hook["type"] for _, hook in hooks_of(settings)} == {"http"}
    assert settings["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "http", "url": "http://127.0.0.1:8790/hooks/http", "timeout": 2}]}
    ]


@pytest.mark.parametrize(("platform", "null"), [("win32", "NUL"), ("linux", "/dev/null")])
def test_curl_discards_output_to_the_platform_null_device(
    monkeypatch: pytest.MonkeyPatch, platform: str, null: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    (hook,) = build("http", port=8790, timeout=2)["hooks"]["SessionStart"][0]["hooks"]

    output = hook["args"][hook["args"].index("-o") + 1]
    assert output == null


@pytest.mark.parametrize("variant", VARIANTS)
def test_env_block_points_telemetry_at_the_capture_collector_with_content_off(
    variant: str,
) -> None:
    env = build(variant, port=8790, timeout=2)["env"]

    assert env == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:4318/v1/metrics",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:4318/v1/logs",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:4318/v1/traces",
        "OTEL_EXPORTER_OTLP_HEADERS": "",
        "OTEL_RESOURCE_ATTRIBUTES": "",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
        "OTEL_METRIC_EXPORT_INTERVAL": "5000",
        "OTEL_LOGS_EXPORT_INTERVAL": "1000",
        "OTEL_TRACES_EXPORT_INTERVAL": "1000",
        "OTEL_METRICS_INCLUDE_VERSION": "true",
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_ASSISTANT_RESPONSES": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
        "OTEL_LOG_TOOL_CONTENT": "0",
        "OTEL_LOG_RAW_API_BODIES": "0",
    }


def test_env_block_uses_the_given_otlp_endpoint() -> None:
    env = build("http", port=8790, timeout=2, otlp_endpoint="http://127.0.0.1:14318")["env"]

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:14318"
    assert env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == "http://127.0.0.1:14318/v1/metrics"


def test_env_block_strips_a_trailing_slash_from_the_otlp_endpoint() -> None:
    env = build("http", port=8790, timeout=2, otlp_endpoint="http://127.0.0.1:14318/")["env"]

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:14318"
    assert env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == "http://127.0.0.1:14318/v1/metrics"
    assert env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "http://127.0.0.1:14318/v1/logs"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://127.0.0.1:14318/v1/traces"


def test_main_passes_the_otlp_endpoint_option(tmp_path: Path) -> None:
    out = tmp_path / "settings.json"

    main(["command", "--otlp-endpoint", "http://localhost:24318", "--out", str(out)])

    env = json.loads(out.read_text(encoding="utf-8"))["env"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:24318"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://localhost:24318/v1/traces"


def test_main_defaults_the_otlp_endpoint_to_the_capture_collector(tmp_path: Path) -> None:
    out = tmp_path / "settings.json"

    main(["http", "--out", str(out)])

    env = json.loads(out.read_text(encoding="utf-8"))["env"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"


@pytest.mark.parametrize("timeout", [0, -1, 5.5, float("nan")])
def test_build_rejects_a_timeout_outside_zero_to_five_seconds(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        build("http", port=8790, timeout=timeout)


@pytest.mark.parametrize("timeout", [0.1, 5])
def test_build_accepts_a_timeout_up_to_five_seconds(timeout: float) -> None:
    (hook,) = build("http", port=8790, timeout=timeout)["hooks"]["Stop"][0]["hooks"]

    assert hook["timeout"] == timeout


@pytest.mark.parametrize("timeout", ["0", "6", "abc"])
def test_main_rejects_a_bad_timeout_with_a_usage_error(tmp_path: Path, timeout: str) -> None:
    out = tmp_path / "settings.json"

    with pytest.raises(SystemExit) as excinfo:
        main(["http", "--timeout", timeout, "--out", str(out)])

    assert excinfo.value.code == 2
    assert not out.exists()


def test_fractional_timeout_is_kept() -> None:
    settings = build("http", port=8790, timeout=0.5)

    assert {hook["timeout"] for event, hook in hooks_of(settings) if hook["type"] == "http"} == {
        0.5
    }


def test_main_writes_the_settings_file(tmp_path: Path) -> None:
    out = tmp_path / "settings-http.json"

    main(["http", "--port", "8791", "--timeout", "1.5", "--out", str(out)])

    raw = out.read_bytes().decode("utf-8")
    assert raw.endswith("}\n")
    assert "\r\n" not in raw
    settings = json.loads(raw)
    assert settings["hooks"]["Stop"] == [
        {"hooks": [{"type": "http", "url": "http://127.0.0.1:8791/hooks/http", "timeout": 1.5}]}
    ]
