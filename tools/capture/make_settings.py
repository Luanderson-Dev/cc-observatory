"""Write the hook settings file that capture runs load with ``claude --settings``.

Variants:

- ``http``: an http hook on every event that accepts one, plus an async exec-form curl
  command hook on SessionStart (which does not accept http hooks);
- ``command``: an async exec-form curl command hook on every event;
- ``probe-sessionstart-http``: like ``http``, but SessionStart gets an http hook instead
  of the curl one, to check that Claude Code still ignores it.

Every variant also carries an ``env`` block that sends telemetry to the capture collector
with content logging off. Settings passed with ``--settings`` outrank the user's own
settings, so an ``env`` block there (a future ``ccobs setup``, say) cannot redirect a
capture run or turn content on.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from tools.capture.jsontypes import JSONObject

# Events that accept http hooks and whose registration does not change behaviour.
HTTP_EVENTS: Final = (
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "InstructionsLoaded",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "PreCompact",
    "PostCompact",
    "PostModelSwitch",
    "Elicitation",
    "ElicitationResult",
    "SessionEnd",
)
# Registering these changes Claude Code's behaviour (Setup also rejects http hooks).
NEVER_REGISTERED: Final = frozenset(
    {"WorktreeCreate", "WorktreeRemove", "PreModelSwitch", "MessageDisplay", "FileChanged", "Setup"}
)
VARIANTS: Final = ("http", "command", "probe-sessionstart-http")

DEFAULT_PORT: Final = 8790
COMMAND_TIMEOUT: Final = 5
CURL_MAX_TIME: Final = "2"
MAX_HTTP_TIMEOUT: Final = 5.0
OTLP_ENDPOINT: Final = "http://127.0.0.1:4318"

# Telemetry to the capture collector (compose.yaml), with short export intervals, and
# every content flag explicitly off ("0" turns each flag off, including
# OTEL_LOG_RAW_API_BODIES). build() adds the endpoints (telemetry_endpoints).
TELEMETRY_ENV: Final = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    # The per-signal variables outrank the generic ones. Setting them here keeps a
    # per-signal value in the user's settings from redirecting a capture: settings
    # passed with --settings can add or override keys, but not remove them.
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
    # Blank values, because a key set in the user's settings can only be overridden:
    # no extra resource attributes (they could carry personal data) and no headers.
    "OTEL_EXPORTER_OTLP_HEADERS": "",
    "OTEL_RESOURCE_ATTRIBUTES": "",
    # Claude Code's default, pinned so every capture has the same temporality.
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


def null_device() -> str:
    """Where curl discards the response body on this platform."""
    return "NUL" if sys.platform == "win32" else "/dev/null"


def curl_hook(url: str) -> JSONObject:
    """Async exec-form command hook that posts the payload from stdin with curl."""
    return {
        "type": "command",
        "command": "curl",
        "args": [
            "-s",
            "-m",
            CURL_MAX_TIME,
            "-o",
            null_device(),
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            url,
        ],
        "async": True,
        "timeout": COMMAND_TIMEOUT,
    }


def http_hook(url: str, timeout: float) -> JSONObject:
    """Http hook with an explicit timeout (whole seconds are written as integers)."""
    return {
        "type": "http",
        "url": url,
        "timeout": int(timeout) if timeout.is_integer() else timeout,
    }


def check_timeout(timeout: float) -> float:
    """``timeout`` if it is a valid http hook timeout (more than 0, at most 5 seconds)."""
    if not 0 < timeout <= MAX_HTTP_TIMEOUT:
        raise ValueError(
            f"timeout must be more than 0 and at most {MAX_HTTP_TIMEOUT:g} seconds, got {timeout:g}"
        )
    return timeout


def telemetry_endpoints(otlp_endpoint: str) -> dict[str, str]:
    """The generic OTLP/HTTP endpoint and the per-signal ones derived from it."""
    base = otlp_endpoint.rstrip("/")
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": base,
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": f"{base}/v1/metrics",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"{base}/v1/logs",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{base}/v1/traces",
    }


def build(
    variant: str, port: int, timeout: float, otlp_endpoint: str = OTLP_ENDPOINT
) -> JSONObject:
    """Settings for ``variant``: hooks post to the capture server on ``port`` and
    telemetry goes to ``otlp_endpoint``."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    check_timeout(timeout)
    base = f"http://127.0.0.1:{port}/hooks"

    def hook_for(event: str) -> JSONObject:
        if variant == "command" or (event == "SessionStart" and variant == "http"):
            return curl_hook(f"{base}/command")
        return http_hook(f"{base}/http", timeout)

    env: JSONObject = {**TELEMETRY_ENV, **telemetry_endpoints(otlp_endpoint)}
    hooks: JSONObject = {}
    for event in [*HTTP_EVENTS, "SessionStart"]:
        group: JSONObject = {"hooks": [hook_for(event)]}
        hooks[event] = [group]
    return {"env": env, "hooks": hooks}


def _timeout_argument(text: str) -> float:
    try:
        return check_timeout(float(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: Sequence[str] | None = None) -> None:
    """Write the settings file for the chosen variant."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.capture.make_settings",
        description="Write the hook settings file for a capture run.",
    )
    parser.add_argument("variant", choices=VARIANTS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="capture server port")
    parser.add_argument(
        "--timeout",
        type=_timeout_argument,
        default=2.0,
        help="http hook timeout in seconds, more than 0 and at most 5 (may be fractional)",
    )
    parser.add_argument(
        "--otlp-endpoint",
        default=OTLP_ENDPOINT,
        help=f"OTLP/HTTP endpoint of the capture collector (default: {OTLP_ENDPOINT})",
    )
    parser.add_argument("--out", type=Path, required=True, help="settings file to write")
    args = parser.parse_args(argv)
    settings = build(args.variant, args.port, args.timeout, args.otlp_endpoint)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
