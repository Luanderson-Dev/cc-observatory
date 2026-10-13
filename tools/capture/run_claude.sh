#!/usr/bin/env bash
# Run `claude` as a fresh top-level session with telemetry sent to the capture collector.
#
# COST: every run is a real Claude Code session that calls the Anthropic API, so it costs
# a little money (cents with Haiku). Run it by hand only, never in CI.
#
# Usage: run_claude.sh <settings-file> <workspace> [claude args...]
#
# - Unsets the CLAUDE* variables that a parent Claude Code session exports, so a run
#   started from inside Claude Code is not treated as its child.
# - Unsets the OTEL_LOG_* content flags, OTEL_RESOURCE_ATTRIBUTES (which could add
#   personal attributes) and the per-signal OTLP endpoints and headers (which would
#   override the endpoint below), in case the shell exports them.
# - Turns on metrics, events and traces over OTLP/HTTP to 127.0.0.1:4318 with short
#   export intervals (the collector in compose.yaml). The settings file carries the same
#   values in its env block, which outranks the user's own settings.
# - Loads the hook settings written by make_settings.py with --settings and runs in
#   <workspace>.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <settings-file> <workspace> [claude args...]" >&2
  exit 2
fi

settings="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
workspace="$2"
shift 2
cd "$workspace"

exec env \
  -u CLAUDECODE \
  -u CLAUDE_CODE_CHILD_SESSION \
  -u CLAUDE_CODE_SESSION_ID \
  -u CLAUDE_PID \
  -u CLAUDE_EFFORT \
  -u CLAUDE_CODE_MESSAGING_SOCKET \
  -u CLAUDE_CODE_MESSAGING_TOKEN \
  -u CLAUDE_CODE_SESSION_ATTENDED \
  -u CLAUDE_CODE_ENTRYPOINT \
  -u CLAUDE_CODE_EXECPATH \
  -u OTEL_LOG_USER_PROMPTS \
  -u OTEL_LOG_ASSISTANT_RESPONSES \
  -u OTEL_LOG_TOOL_DETAILS \
  -u OTEL_LOG_TOOL_CONTENT \
  -u OTEL_LOG_RAW_API_BODIES \
  -u OTEL_RESOURCE_ATTRIBUTES \
  -u OTEL_EXPORTER_OTLP_HEADERS \
  -u OTEL_EXPORTER_OTLP_METRICS_ENDPOINT \
  -u OTEL_EXPORTER_OTLP_LOGS_ENDPOINT \
  -u OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
  -u OTEL_EXPORTER_OTLP_METRICS_PROTOCOL \
  -u OTEL_EXPORTER_OTLP_LOGS_PROTOCOL \
  -u OTEL_EXPORTER_OTLP_TRACES_PROTOCOL \
  CLAUDE_CODE_ENABLE_TELEMETRY=1 \
  CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
  OTEL_METRICS_EXPORTER=otlp \
  OTEL_LOGS_EXPORTER=otlp \
  OTEL_TRACES_EXPORTER=otlp \
  OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
  OTEL_METRIC_EXPORT_INTERVAL=5000 \
  OTEL_LOGS_EXPORT_INTERVAL=1000 \
  OTEL_TRACES_EXPORT_INTERVAL=1000 \
  OTEL_METRICS_INCLUDE_VERSION=true \
  CLAUDE_CODE_OTEL_DIAG_STDERR=1 \
  claude --settings "$settings" "$@"
