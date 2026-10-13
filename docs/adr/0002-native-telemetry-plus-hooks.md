---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Collect from Claude Code's native telemetry and from hooks

## Context and problem statement

cc-observatory needs two kinds of data: what a session consumed (tokens, cost, API
requests, tool results) and what it is doing right now (working, waiting on a person,
finished). Claude Code offers native OpenTelemetry export, lifecycle hooks and the
transcript files it writes. Which sources should cc-observatory read?

## Decision drivers

- Live state within seconds, above all when a session is waiting on a person.
- Tokens and cost as Claude Code reports them, not recomputed by the project.
- Documented interfaces only, so Claude Code upgrades do not silently break the project.
- No interference with Claude Code, and no content stored by default
  ([ADR 0006](0006-privacy-metadata-only-by-default.md)).

## Considered options

- **Telemetry only.** Metrics (`claude_code.token.usage`, `claude_code.cost.usage`, ...),
  events (`claude_code.api_request`, `claude_code.api_error`, `claude_code.tool_result`,
  ...) and, in beta, traces whose spans carry `agent_id` and `parent_agent_id`. Rich and
  documented, but exported in batches (by default every 60 s for metrics and every 5 s
  for events), and no signal says that a session is waiting on a person right now: the
  span for a permission wait (`claude_code.tool.blocked_on_user`) arrives after it ends.
- **Hooks only.** Real-time lifecycle events: a prompt submitted (`UserPromptSubmit`),
  tool use (`PreToolUse`, `PostToolUse`), permission requests (`PermissionRequest`),
  notifications such as permission and idle prompts (`Notification`), subagents
  (`SubagentStart`, `SubagentStop`), compaction (`PreCompact`, `PostCompact`), the end
  of a turn (`Stop`, `StopFailure`) and of a session (`SessionEnd`). No tokens or cost.
- **Transcript parsing.** Read the JSONL transcripts under `~/.claude/projects/`. They
  hold everything, but the format is internal to Claude Code and changes between
  versions, and the files are mostly conversation content.
- **Telemetry and hooks.** Each source for what it does well.

## Decision outcome

Chosen option: "Telemetry and hooks", because neither documented source covers both
needs alone, and the transcript format is not a supported interface.

- Telemetry (OTLP to the collector, [ADR 0003](0003-observability-backends.md)) brings
  tokens, cost (an estimate computed by Claude Code), API requests and errors, tool
  results and, when tracing is on, the agent hierarchy.
- Hooks (to the app) bring live session state, blocks, subagent start and stop,
  compaction and session end.
- The two sources are joined on the identifiers they share: `session_id` (`session.id`
  in telemetry), `prompt_id` (`prompt.id`) and `tool_use_id`.
- Claude Code honours its telemetry variables from the environment it starts in, a
  `--settings` file, the user settings and managed settings, but ignores them in project
  settings (`.claude/settings.json` and `.claude/settings.local.json`) except to turn a
  signal off. So `ccobs setup` writes the telemetry `env` block, and the hooks, to the
  user settings (`~/.claude/settings.json`).

### Consequences

- Good, because live state and consumption both come from documented interfaces.
- Good, because the shared identifiers link a block to the tokens and cost of the same
  session and prompt.
- Bad, because there are two ingestion paths to build, test and keep running.
- Bad, because telemetry lags the hooks by up to an export interval, so views must
  tolerate one source being ahead of the other.
- Bad, because hooks run inside Claude Code's loop, so the endpoint must never slow it
  down or change its decisions (the hook rules in CLAUDE.md).
- Neutral, because traces stay optional (beta, off by default), so the agent tree also
  relies on `SubagentStart` and `SubagentStop`.

### Confirmation

- The telemetry and hooks spike (milestone 0) records real payloads as sanitized
  fixtures in `tests/fixtures/`. The OTLP pipeline is tested against them in milestone 1,
  and the hook parsers in milestone 2.
- `ccobs setup` tests (milestone 4) check that it writes to the user settings and never
  to a project's `.claude/settings.json` or `.claude/settings.local.json`.
- No code reads transcript files; the `reviewer` subagent checks diffs against the ADRs.

## More information

- Claude Code docs: [monitoring](https://code.claude.com/docs/en/monitoring-usage),
  [hooks](https://code.claude.com/docs/en/hooks) and
  [settings](https://code.claude.com/docs/en/settings).
- The hook transport (hook type, the events registered, timeouts, how a new session is
  detected given that `SessionStart` does not accept `http` hooks, and the behaviour with
  the stack down) is decided in ADR 0011, after the milestone 0 spike.
- The session state machine and the signals that open and close a block are decided in
  milestone 2.
