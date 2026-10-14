#!/usr/bin/env bash
# Reproduce the headless capture scenarios (`claude -p`) described in out/scenarios.json.
#
# COST: every scenario calls the Anthropic API through `claude -p` (Haiku by default),
# so a full run costs a little money (cents). Run it by hand only, never in CI.
#
# Usage: tools/capture/run_headless.sh [scenario...]    (default: all scenarios)
#
# Prerequisites: the capture collector is up (docker compose -f tools/capture/compose.yaml
# up -d); claude, curl, git and uv are on PATH; ports 8790, 8791 and 8799 are free. The
# script starts and stops its own capture servers.
#
# Environment:
#   CAPTURE_WORKSPACE_ROOT   where the per-scenario workspaces go (default:
#                            C:/ccobs-capture/run-<UTC time> in Git Bash, outside your
#                            home, which has .claude/; /tmp/ccobs-capture/run-<UTC time>
#                            elsewhere). The root must have no CLAUDE.md,
#                            CLAUDE.local.md or .claude/ in it or above it, and sit
#                            outside any git repository and any Claude Code scratchpad;
#                            the script checks (python -m tools.capture.workspace).
#   CAPTURE_MODEL            model for the scenarios (default: haiku)
#   CAPTURE_ISOLATION        1 (default): every run is isolated from your own Claude Code
#                            setup (run_claude.sh passes --setting-sources project,local
#                            and --strict-mcp-config, turns auto memory and claude.ai
#                            connectors off, and writes a synthetic CLAUDE.md in each
#                            workspace). 0 loads your setup, for exploratory runs only,
#                            never for fixtures.
#
# Output in tools/capture/out/: hooks-<scenario>.jsonl, the settings files, and
# headless-sessions.tsv (scenario, session id, hooks file, isolation: the
# CAPTURE_ISOLATION of the run, 1 or 0) for out/scenarios.json. The
# workspace root printed at the end is the sanitizer's --workspace-root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="$here/out"
model="${CAPTURE_MODEL:-haiku}"
isolation="${CAPTURE_ISOLATION:-1}"
if [[ "$isolation" != 0 && "$isolation" != 1 ]]; then
  echo "CAPTURE_ISOLATION must be 1 (the default) or 0" >&2
  exit 2
fi
port=8790       # capture server, normal mode
slow_port=8791  # capture server, slow mode (endpoint timeout)
dead_port=8799  # nothing listens here (endpoint down)

all_scenarios=(
  headless-tools-subagent
  headless-edit-commit-tasks
  headless-api-error
  headless-sessionstart-http-ignored
  headless-async-command-transport
  hooks-endpoint-down
  hooks-endpoint-timeout
)

prompt_tools_subagent="Do these steps in order using tools, keep every reply very short:
1. Use the Read tool on notes.txt.
2. Use the Read tool on missing.txt (it does not exist, that is expected).
3. Run the Bash command: ls
4. Run the Bash command: echo hello > created.txt
5. Use the Agent tool with subagent_type Explore to list the .txt files in this folder.
6. Reply with the single word done."

prompt_edit_commit_tasks="Do these steps in order using tools, keep replies very short:
1. Use the TaskCreate tool to create a task named \"write hello\" (skip this step if you do not have that tool).
2. Use the Write tool to create hello.py containing a function hello() that returns \"hi\".
3. Use the Edit tool to change the return value in hello.py to \"hello\".
4. Mark the task as completed with the TaskUpdate tool (skip if you do not have it).
5. Run the Bash command: git add hello.py
6. Run the Bash command: git commit -m \"feat: add hello\"
7. Reply with the single word done."

prompt_async_command="Use the Read tool on notes.txt and on a.txt in parallel, then run the \
Bash command: ls. Then use the Agent tool with subagent_type Explore to count the .txt files \
here. Reply with the single word done."

prompt_read_once="Use the Read tool on notes.txt. Then reply with the single word done."
prompt_ok="Reply with the word ok."

cd "$repo"
mkdir -p "$out"
python="$(uv run python -c 'import sys; print(sys.executable)')"
run_name="run-$(date -u +%Y%m%dT%H%M%SZ)"
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) default_root="/c/ccobs-capture/$run_name" ;;
  *) default_root="/tmp/ccobs-capture/$run_name" ;;
esac
workspace_root="${CAPTURE_WORKSPACE_ROOT:-$default_root}"
mkdir -p "$workspace_root"
workspace_root="$(cd "$workspace_root" && pwd -P)"
native_root="$workspace_root"
if command -v cygpath >/dev/null 2>&1; then
  native_root="$(cygpath -w "$workspace_root")"
fi
if ! "$python" -m tools.capture.workspace check-root "$native_root"; then
  rmdir "$workspace_root" 2>/dev/null || true
  exit 1
fi
server_pid=""

stop_server() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    server_pid=""
  fi
}
trap stop_server EXIT

# listening <port>: true when something accepts HTTP connections on 127.0.0.1:<port>.
listening() {
  curl -s -m 1 "http://127.0.0.1:$1/" >/dev/null
}

# start_server <hooks-file> <mode> <port>: a capture server in the background.
start_server() {
  "$python" -m tools.capture.capture_server --out "$1" --mode "$2" --port "$3" --delay 10 &
  server_pid=$!
  for _ in {1..50}; do
    if listening "$3"; then
      return 0
    fi
    sleep 0.2
  done
  echo "the capture server did not start on port $3" >&2
  exit 1
}

# settings <variant> <port> [timeout]: write a hook settings file and print its path.
settings() {
  local file="$out/settings-$1-$2.json"
  "$python" -m tools.capture.make_settings "$1" --port "$2" --timeout "${3:-2}" --out "$file"
  printf '%s\n' "$file"
}

# workspace <scenario> [file...]: a new workspace holding small text files.
workspace() {
  local dir="$workspace_root/$1"
  shift
  if [[ -e "$dir" ]]; then
    echo "$dir already exists; remove it or set another CAPTURE_WORKSPACE_ROOT" >&2
    exit 1
  fi
  mkdir -p "$dir"
  local name
  for name in "$@"; do
    printf 'capture fixture file %s\n' "$name" >"$dir/$name"
  done
  printf '%s\n' "$dir"
}

# run <scenario> <settings> <workspace> <hooks file or -> [claude args...]: one session,
# isolated from the user's own setup by run_claude.sh (see CAPTURE_ISOLATION). A
# refused workspace (run_claude.sh exit status 3) stops the whole capture, and its
# session is not recorded.
run() {
  local name="$1" settings_file="$2" dir="$3" hooks_file="$4"
  shift 4
  local session_id status=0
  session_id="$("$python" -c 'import uuid; print(uuid.uuid4())')"
  echo "== $name (session $session_id)"
  bash "$here/run_claude.sh" "$settings_file" "$dir" --session-id "$session_id" "$@" ||
    status=$?
  if [[ $status -eq 3 ]]; then
    echo "the workspace of $name was refused; stopping the capture" >&2
    exit 3
  fi
  if [[ $status -ne 0 ]]; then
    echo "   claude exited with status $status (expected for headless-api-error)"
  fi
  printf '%s\t%s\t%s\t%s\n' "$name" "$session_id" "$hooks_file" "$isolation" >>"$out/headless-sessions.tsv"
  sleep 6  # Let the last metric export (5 s interval) reach the collector.
}

run_scenario() {
  local name="$1"
  local hooks="hooks-$name.jsonl"
  local dir settings_file
  case "$name" in
    headless-tools-subagent)
      # Read fails once (PostToolUseFailure), `echo >` is not allowed (PermissionRequest,
      # denied in -p), and an Explore subagent runs (SubagentStart/SubagentStop).
      dir="$(workspace "$name" notes.txt)"
      settings_file="$(settings http "$port")"
      start_server "$out/$hooks" ok "$port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_tools_subagent" \
        --model "$model" --allowedTools "Read,Glob,Grep,Agent,Bash(ls)"
      ;;
    headless-edit-commit-tasks)
      # TaskCreated/TaskCompleted, Write and Edit, then git add and git commit.
      dir="$(workspace "$name")"
      git -C "$dir" init -q
      git -C "$dir" config user.name "Capture Bot"
      git -C "$dir" config user.email "capture@example.com"
      git -C "$dir" config commit.gpgsign false
      settings_file="$(settings http "$port")"
      start_server "$out/$hooks" ok "$port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_edit_commit_tasks" \
        --model "$model" --permission-mode acceptEdits \
        --allowedTools "Write,Edit,TaskCreate,TaskUpdate,Bash(git add:*),Bash(git commit:*)"
      ;;
    headless-api-error)
      # An unknown model: StopFailure (model_not_found) and the api_error event.
      dir="$(workspace "$name")"
      settings_file="$(settings http "$port")"
      start_server "$out/$hooks" ok "$port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_ok" \
        --model claude-capture-unknown-model
      ;;
    headless-sessionstart-http-ignored)
      # SessionStart registered as an http hook, which Claude Code does not support.
      dir="$(workspace "$name")"
      settings_file="$(settings probe-sessionstart-http "$port")"
      start_server "$out/$hooks" ok "$port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_ok" --model "$model"
      ;;
    headless-async-command-transport)
      # Every hook as an async exec-form curl command hook; a background Explore subagent.
      dir="$(workspace "$name" notes.txt a.txt)"
      settings_file="$(settings command "$port")"
      start_server "$out/$hooks" ok "$port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_async_command" \
        --model "$model" --allowedTools "Read,Glob,Grep,Agent,Bash(ls)"
      ;;
    hooks-endpoint-down)
      # http hooks pointing at a port where nothing listens: the session must still work.
      if listening "$dead_port"; then
        echo "port $dead_port must have nothing listening" >&2
        exit 1
      fi
      dir="$(workspace "$name" notes.txt)"
      settings_file="$(settings http "$dead_port")"
      run "$name" "$settings_file" "$dir" - -p "$prompt_read_once" --model "$model"
      ;;
    hooks-endpoint-timeout)
      # A capture server that waits 10 s and a 1 s hook timeout: every http hook is cancelled.
      dir="$(workspace "$name" notes.txt)"
      settings_file="$(settings http "$slow_port" 1)"
      start_server "$out/$hooks" slow "$slow_port"
      run "$name" "$settings_file" "$dir" "$hooks" -p "$prompt_read_once" --model "$model"
      ;;
    *)
      echo "unknown scenario: $name (known: ${all_scenarios[*]})" >&2
      exit 2
      ;;
  esac
  stop_server
}

if ! listening 4318; then
  echo "the capture collector is not listening on 127.0.0.1:4318; start it first:" >&2
  echo "  docker compose -f tools/capture/compose.yaml up -d" >&2
  exit 1
fi

scenarios=("$@")
if [[ ${#scenarios[@]} -eq 0 ]]; then
  scenarios=("${all_scenarios[@]}")
fi
for scenario in "${scenarios[@]}"; do
  run_scenario "$scenario"
done

echo "sessions: $out/headless-sessions.tsv"
echo "workspace root (sanitizer --workspace-root): $native_root"
