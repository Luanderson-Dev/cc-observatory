#!/usr/bin/env bash
# Run `claude` as a fresh, isolated top-level session with telemetry sent to the capture
# collector.
#
# COST: every run is a real Claude Code session that calls the Anthropic API, so it costs
# a little money (cents with Haiku). Run it by hand only, never in CI.
#
# Usage: run_claude.sh <settings-file> <workspace> [claude args...]
#
# - Checks and prepares <workspace> with `python -m tools.capture.workspace prepare`
#   (needs uv): it refuses a workspace inside a Claude Code scratchpad, under a
#   CLAUDE.md, CLAUDE.local.md or .claude/ (on Windows your home has .claude/, so use a
#   root outside it, such as C:/ccobs-capture/<run>; elsewhere /tmp/ccobs-capture/<run>),
#   holding CLAUDE.local.md or .claude/, or inside a git repository other than its own;
#   then it writes a synthetic CLAUDE.md so InstructionsLoaded still fires, as Project.
#   A refused workspace (or a failure of that step) exits with status 3 before
#   anything runs; other statuses are Claude Code's own.
# - Isolates the session from your own Claude Code setup (CAPTURE_ISOLATION=1, the
#   default): passes --setting-sources project,local (no user settings, hooks, plugins,
#   CLAUDE.md, rules, skills or agents from ~/.claude), --strict-mcp-config (only the
#   servers given with --mcp-config) and --no-chrome (no Claude in Chrome connection,
#   which would add its own MCP server), and sets CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 (auto
#   memory loads whatever --setting-sources says) and ENABLE_CLAUDEAI_MCP_SERVERS=false
#   (no claude.ai connectors). The --settings file below still loads: it is the
#   command-line level of the settings, not one of the setting sources.
#   CAPTURE_ISOLATION=0 loads your own setup and only refuses a scratchpad, for
#   exploratory runs only; never turn such a run into fixtures.
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

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
settings="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
workspace="$(cd "$2" && pwd -P)"
shift 2
native_workspace="$workspace"
if command -v cygpath >/dev/null 2>&1; then
  native_workspace="$(cygpath -w "$workspace")"
fi

# The odd expansions below keep empty arrays safe under `set -u` in bash 3.2 (macOS).
isolation_args=()
isolation_env=()
case "${CAPTURE_ISOLATION:-1}" in
  1)
    prepare_args=(prepare "$native_workspace")
    isolation_args=(--setting-sources project,local --strict-mcp-config --no-chrome)
    isolation_env=(CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 ENABLE_CLAUDEAI_MCP_SERVERS=false)
    ;;
  0)
    prepare_args=(prepare --exploratory "$native_workspace")
    echo "CAPTURE_ISOLATION=0: your own settings, hooks, plugins, memory and MCP servers" \
      "load; do not turn this run into fixtures." >&2
    ;;
  *)
    echo "CAPTURE_ISOLATION must be 1 (the default) or 0" >&2
    exit 2
    ;;
esac

refused=3  # The workspace was refused: nothing ran.
if ! (cd "$repo" && uv run --quiet python -m tools.capture.workspace "${prepare_args[@]}"); then
  exit "$refused"
fi
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
  ${isolation_env[@]+"${isolation_env[@]}"} \
  claude --settings "$settings" ${isolation_args[@]+"${isolation_args[@]}"} "$@"
