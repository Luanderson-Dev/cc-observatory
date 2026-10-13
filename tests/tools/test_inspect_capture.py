"""Tests for the capture tool's inspection commands, on synthetic OTLP output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.capture.inspect_capture import any_value, main


def kv(key: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "value": value}


def s(value: str) -> dict[str, Any]:
    return {"stringValue": value}


def write_raw(raw: Path) -> None:
    metrics = {
        "resourceMetrics": [
            {
                "resource": {"attributes": [kv("service.name", s("claude-code"))]},
                "scopeMetrics": [
                    {
                        "scope": {"name": "com.anthropic.claude_code"},
                        "metrics": [
                            {
                                "name": "claude_code.token.usage",
                                "unit": "tokens",
                                "sum": {
                                    "aggregationTemporality": 1,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "asDouble": 12,
                                            "attributes": [
                                                kv("session.id", s("s1")),
                                                kv("type", s("input")),
                                                kv("model", s("claude-haiku-5-5")),
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }

    def record(session: str, event: str, **extra: dict[str, Any]) -> dict[str, Any]:
        attributes = [kv("session.id", s(session)), kv("event.name", s(event))]
        attributes += [kv(key, value) for key, value in extra.items()]
        return {"traceId": "abc", "attributes": attributes}

    hook_complete = {
        "hook_event": s("PreToolUse"),
        "total_duration_ms": {"intValue": "1003"},
        "num_hooks": {"intValue": "1"},
        "num_success": {"intValue": "0"},
        "num_non_blocking_error": {"intValue": "0"},
        "num_cancelled": {"intValue": "1"},
    }
    logs = {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "scope": {"name": "com.anthropic.claude_code.events"},
                        "logRecords": [
                            record("s1", "api_request", model=s("claude-haiku-5-5")),
                            record("s1", "hook_execution_complete", **hook_complete),
                            record("s2", "hook_execution_complete", **hook_complete),
                        ],
                    }
                ],
            }
        ]
    }
    traces = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "com.anthropic.claude_code"},
                        "spans": [
                            {
                                "name": "claude_code.interaction",
                                "attributes": [
                                    kv("session.id", s("s1")),
                                    kv("agent_id", s("a1")),
                                    kv("parent_agent_id", s("a0")),
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    for name, doc in (("metrics", metrics), ("logs", logs), ("traces", traces)):
        (raw / f"otlp-{name}.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")


def test_any_value_decodes_otlp_values() -> None:
    assert any_value({"stringValue": "x"}) == "x"
    assert any_value({"intValue": "42"}) == 42
    assert any_value({"boolValue": True}) is True
    assert any_value({"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}}) == [
        "a",
        1,
    ]
    assert any_value({}) is None


def test_summary_prints_metric_event_and_span_shapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_raw(tmp_path)

    assert main(["summary", "--raw", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "claude_code.token.usage unit=tokens kind=sum temporality=[1] monotonic=True" in out
    assert "keys: model, session.id, type" in out
    assert "type = input" in out
    assert "api_request n=1 trace=True" in out
    assert "hook_execution_complete n=2" in out
    assert "claude_code.interaction n=1" in out
    assert "agent ids: a1 <- a0" in out
    assert "resource keys: service.name" in out


def test_hook_timings_lists_hook_execution_complete_per_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_raw(tmp_path)

    assert main(["hook-timings", "--raw", str(tmp_path), "timeout=s1"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines() == [
        "== timeout: 1 hook executions",
        "    PreToolUse total_ms=1003 hooks=1 success=0 non_blocking_error=0 cancelled=1",
    ]


def test_hook_timings_rejects_arguments_without_a_label(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["hook-timings", "--raw", str(tmp_path), "s1"])

    assert excinfo.value.code == 2
