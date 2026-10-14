# Test fixtures

Sanitized data from real Claude Code sessions: the hook requests Claude Code sent and
the metrics, events and traces it exported over OpenTelemetry. Parser tests run on these
real shapes instead of guessed ones. `tests/test_fixtures.py` pins what they show (the
contract every recapture must keep) and guards them against personal data.

## Provenance

- Claude Code 2.1.296, with Haiku.
- OpenTelemetry Collector (contrib) 0.162.0, writing OTLP JSON with its file exporter.
- Captured on 2026-10-13, on Windows, as an isolated capture under a workspace root
  outside the home.

They were captured with the developer tooling in
[`tools/capture/`](../../tools/capture/README.md): headless runs (`claude -p`) and
interactive sessions driven through a pseudo-terminal, each a fresh session with a known
session ID. Each session ran in its own workspace under a root outside the home, with no
`CLAUDE.md`, `CLAUDE.local.md` or `.claude/` above it, outside any git repository but
its own and outside any Claude Code scratchpad; the workspace held a synthetic
`CLAUDE.md`, so InstructionsLoaded fired as Project memory. Sessions ran isolated from
the maintainer's own Claude Code setup: `--setting-sources project,local` (no user
memory, settings, plugins, skills or agents; the user's hooks are covered below),
`--strict-mcp-config` (only the capture's own MCP server), auto memory and claude.ai
connectors off; the hooks and telemetry settings came from the `--settings` file. Claude
Code still listed the hooks of the maintainer's user settings in its `hook_registered`
events (event and type only, as an inventory), but did not run them: the hook executions
of Stop, UserPromptSubmit, PreToolUse and SessionStart count only the capture's hook
(`num_hooks` 1), which `tests/test_fixtures.py` checks. The interactive fixtures also
show one built-in MCP connection (`mcp_server_connection`, scope `dynamic`, stdio): the
Claude in Chrome integration, captured before `run_claude.sh` passed `--no-chrome`; no
server name reaches the fixtures. Hooks went to a local capture server (http hooks, and
command hooks that post their stdin with `curl`); metrics, events and traces went to a
local collector, with every `OTEL_LOG_*` content flag off.

`tools/capture/sanitize.py` then kept each scenario's own session, refused
content-bearing telemetry and any instructions loaded from outside the workspace,
replaced the summaries written by internal agents (PostCompact `compact_summary`, and the
`last_assistant_message` of a SubagentStop the session did not start, such as compaction
and prompt suggestion) with `<internal summary removed>`, replaced personal data with
fixed placeholders (the home directory and the workspace root become a fake `user` home,
identity attributes become fakes such as `user@example.com`, request IDs become
`req_EXAMPLE0001`, tool use IDs `toolu_01EXAMPLE000000000000001`, client request IDs and
plugin ID hashes numbered fakes, plugin, marketplace, skill and MCP server names
`plugin-1`, `marketplace-1`, ...) and checked the result for personal data before writing
anything. Other hook content (prompts, tool input and output, the main agent's messages)
stays on purpose: the capture prompts are synthetic and the sessions saw nothing of the
maintainer's own setup.

## Scenarios

One line each; `manifest.json` has the full description and the session ID.

- `headless-tools-subagent`: `claude -p` with a Read of an existing and of a missing file
  (PostToolUseFailure), Bash `ls` allowed, a file write that is not allowed
  (PermissionRequest, denied in `-p`) and an Explore subagent; http hooks plus an async
  command hook for SessionStart.
- `headless-edit-commit-tasks`: tasks (TaskCreated, TaskCompleted), Write, Edit, and
  `git add` and `git commit` in the workspace's own repository (lines of code, edit
  decisions, commit count).
- `headless-api-error`: an unknown model, so StopFailure `model_not_found` and the
  `api_error` event.
- `headless-sessionstart-http-ignored`: SessionStart configured as an http hook, which
  Claude Code ignores (no SessionStart arrives); the other events use http hooks; hooks
  only, no telemetry.
- `headless-async-command-transport`: every hook as an async command hook; the Explore
  subagent ran in the background (PostToolUse of the Agent tool with status
  `async_launched`), the main Stop came before the subagent finished, and its report
  arrived as a UserPromptSubmit. Async delivery reorders events (here the PostToolUse of
  the Agent tool arrived before its PreToolUse), and no SessionEnd arrived.
- `interactive-permission-compact`: default permission mode; one permission dialog
  approved (PermissionRequest, then the `permission_prompt` Notification about 6 s later)
  and one denied with Esc (no closing hook event; telemetry `tool_decision`
  reject/user_reject), then `/compact` (PreCompact, an internal SubagentStop and
  PostCompact, plus a SessionStart `source=compact` delivered asynchronously, so its
  position varies) and `/exit` (SessionEnd `prompt_input_exit`).
- `interactive-elicitation-idle`: an MCP elicitation declined with Esc (Elicitation, the
  `elicitation_dialog` Notification, ElicitationResult, the `elicitation_response`
  Notification), then idle (`idle_prompt` 60 s after Stop), then `/exit`.
- `hooks-endpoint-down`: http hooks pointing at a closed port; the session completes
  normally; telemetry only, with `hook_execution_complete` counting the errors.
- `hooks-endpoint-timeout`: a hook server slower than the 1 s hook timeout, so every http
  hook is cancelled after the timeout (`hook_execution_complete` `num_cancelled`).

## Files

~~~text
manifest.json                    versions, date, platform, whether the capture was
                                 isolated, the telemetry env, and each scenario's name,
                                 session ID, description and files
hooks/<scenario>.jsonl           one line per hook request, in arrival order
otlp/<scenario>/metrics.jsonl    OTLP JSON export requests, one per line
otlp/<scenario>/logs.jsonl
otlp/<scenario>/traces.jsonl
README.md                        this file, written by hand
~~~

Each hook line is an object with three keys:

- `offset_ms`: milliseconds since the scenario's first hook request (an integer that
  never decreases);
- `transport`: `http` for an http hook, `command` for a command hook;
- `payload`: the JSON body Claude Code sent, sanitized.

Each OTLP line is an export request as the collector's file exporter wrote it
(`resourceMetrics`, `resourceLogs` or `resourceSpans`), keeping only the data points, log
records and spans whose `session.id` is the scenario's. As in any OTLP JSON, 64-bit
integers are strings (`"intValue": "1"`); some numeric event attributes are strings too,
because Claude Code sends them that way.

## Regenerating

Fixtures are regenerated, never edited by hand. When Claude Code changes (a new version,
new hook events, new telemetry attributes), follow `tools/capture/README.md`: capture
again, run the sanitizer with `--dest tests/fixtures`, update this README (versions,
date, scenarios), review the diff, and run:

~~~bash
uv run pytest tests/test_fixtures.py --no-cov
~~~

The sanitizer only replaces the files listed in the previous `manifest.json`, so this
README stays. If a contract test fails after a recapture, find out what changed in Claude
Code before changing the test.
