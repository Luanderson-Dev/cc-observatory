---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Observed telemetry and hook contract of Claude Code 2.1.296

## Context and problem statement

Claude Code documents its [telemetry](https://code.claude.com/docs/en/monitoring-usage)
and its [hooks](https://code.claude.com/docs/en/hooks), but the docs leave out what
ingestion code needs most: which attributes arrive on which signal and in what shape, the
order and delays of hook events, and where the behaviour differs from the text. The
collector filters (milestone 1, [ADR 0006](0006-privacy-metadata-only-by-default.md)),
the hook parsers and the session state machine (milestone 2) need one reference. The
milestone 0 spike captured real sessions with Claude Code 2.1.296 on Windows, through an
OpenTelemetry Collector (contrib 0.162.0), with Haiku, and sanitized them into
`tests/fixtures/`. The fixtures come from a recapture on 2026-10-13, isolated from the
maintainer's own setup, because the first capture let personal context reach the
compaction summaries. What contract does cc-observatory build on, and which Claude Code
versions does it support?

## Decision drivers

- Code uses observed names and fields, never guesses (CLAUDE.md).
- Every difference between the docs and the observed behaviour is written down.
- A Claude Code upgrade that changes the contract is caught by tests or a recapture, not
  by users.
- The record can be checked.

## Considered options

- **Record the observed contract in an ADR, backed by the fixtures.** One reference with
  the exact names, whose executable form is the fixtures and their tests. It has to be
  redone when Claude Code changes.
- **Rely on the official docs.** No upkeep, but they leave out shapes, order and delays,
  and the spike found differences.
- **Schema-less ingestion.** Store whatever arrives and decide later. Tolerant of
  changes, but nothing says when a field the product needs is gone, and the privacy
  filters need known names.

## Decision outcome

Chosen option: "Record the observed contract in an ADR, backed by the fixtures", because
it is the only option that gives code exact names and makes contract changes visible.

- **Minimum supported version.** Claude Code 2.1.296, the version tested. Older versions
  are unsupported until tested, and `ccobs doctor` warns about them (milestone 4).
- **Executable form.** The fixtures in `tests/fixtures/` (scenarios and telemetry
  settings in `manifest.json`) are the executable form of this ADR, checked by
  `tests/test_fixtures.py`.
- **OpenTelemetry names.** The names below are the ones Claude Code sends. The names
  they get in Prometheus and Loki are decided in milestone 1, in a new ADR, never
  guessed.
- **Changes.** When Claude Code changes, recapture with `tools/capture/`. A changed
  contract means a new ADR that supersedes this one.

### Capture conditions

- **Isolated** (2026-10-13; details in `tools/capture/README.md`):
  `--setting-sources project,local`, so nothing loads from `~/.claude` (user settings,
  hooks, plugins outside the built-in bundle, `CLAUDE.md`); `--strict-mcp-config`; auto
  memory and claude.ai connectors off; a synthetic project `CLAUDE.md`; and a workspace
  root outside the home directory and outside any git repository. The capture's hooks
  were the only configured hooks that ran (Claude Code adds some of its own, see
  Events).
- **Telemetry** over OTLP `http/protobuf` with `CLAUDE_CODE_ENABLE_TELEMETRY=1`, tracing
  on (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, `OTEL_TRACES_EXPORTER=otlp`), every
  `OTEL_LOG_*` content flag set to `0`, `OTEL_METRICS_INCLUDE_VERSION=true`, and
  `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta` (the documented default,
  pinned).
- **Scenarios** (`manifest.json`): headless `claude -p` runs, with tools and a subagent
  (`headless-tools-subagent`); edits, a commit and tasks in `acceptEdits` mode
  (`headless-edit-commit-tasks`); an unknown model (`headless-api-error`); an `http` hook
  on `SessionStart`, which never fired (`headless-sessionstart-http-ignored`); a
  background subagent with every hook as an async command
  (`headless-async-command-transport`); the hook endpoint down (`hooks-endpoint-down`)
  and hung (`hooks-endpoint-timeout`). And two interactive sessions in `default` mode,
  driven through a pseudo-terminal: permission dialogs and `/compact`
  (`interactive-permission-compact`); an MCP elicitation and an idle prompt
  (`interactive-elicitation-idle`).

### Telemetry: common shape

- **Resource**, on every signal: `service.name` (`claude-code`), `service.version`,
  `os.type`, `os.version` and `host.arch`. No `host.name`.
- **Scopes:** `com.anthropic.claude_code` (metrics), `com.anthropic.claude_code.events`
  (logs) and `com.anthropic.claude_code.tracing` (traces).
- **On every metric data point, log record and span**, not on the resource:
  `session.id`, `terminal.type`, `app.version` (there because the capture set
  `OTEL_METRICS_INCLUDE_VERSION=true`) and the identity attributes `user.id`,
  `user.email`, `user.account_uuid`, `user.account_id` and `organization.id`. The
  collector must delete them there, not only on the resource (ADR 0006).
- **Metrics** are monotonic sums with delta temporality (`aggregationTemporality` 1) in
  every fixture. They become cumulative only when configured; ADR 0003 leaves the bridge
  to Prometheus's cumulative series to milestone 1.

### Metrics

| Metric                                | Unit     | Attributes (observed values)                                                                                                                                                                                                 |
| ------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude_code.session.count`           | none     | `start_type` (`fresh`)                                                                                                                                                                                                       |
| `claude_code.active_time.total`       | `s`      | `type` (`user`, `cli`)                                                                                                                                                                                                       |
| `claude_code.token.usage`             | `tokens` | `type` (`input`, `output`, `cacheRead`, `cacheCreation`), `model`, `query_source` (`main`, `subagent`, `auxiliary`), `effort`, `agent.name` (subagent requests: `Explore`), `mcp_server.name` and `mcp_tool.name` (`custom`) |
| `claude_code.cost.usage`              | `USD`    | the `token.usage` attributes except `type`                                                                                                                                                                                   |
| `claude_code.lines_of_code.count`     | none     | `type` (`added`, `removed`), `model`                                                                                                                                                                                         |
| `claude_code.code_edit_tool.decision` | none     | `tool_name` (`Edit`, `Write`), `decision` (`accept`, `reject`), `source` (`config`), `language` (`Python`, `Plain text`)                                                                                                     |
| `claude_code.commit.count`            | none     | none                                                                                                                                                                                                                         |

Not observed: `claude_code.pull_request.count` (no pull request was created), and the
documented `start_type` values `resume`, `continue` and `agents_view`.

### Events

- The log record body is the string `claude_code.<name>`, the `event.name` attribute
  holds `<name>` without the prefix, and there is no `eventName` field. Every record also
  has `event.timestamp` (ISO 8601) and `event.sequence`.
- `prompt.id`, equal to the hooks' `prompt_id`, is on every record emitted after the
  first prompt.
- With tracing on, records emitted during an interaction carry `traceId` and `spanId`.
  Startup records (`hook_registered`, `plugin_loaded`, `managed_settings_resolved`,
  `mcp_server_connection` and the hook events of the startup `SessionStart`) carry none;
  the hook events of the `SessionStart` after `/compact` carry a `traceId` and a
  `prompt.id`.
- With the content flags off, `prompt` and `prompt_text` (`user_prompt`) and `response`
  (`assistant_response`) arrive as `<REDACTED>`. Free-text `error` still arrives on
  `api_error`, for example `model: <model name>`.

| `event.name`                | Attributes besides the common ones and `prompt.id` (observed values)                                                                                                                                                                                                    |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `user_prompt`               | `prompt` and `prompt_text` (`<REDACTED>`), `prompt_length`, `message.uuid`, `command_name` (`compact`, `exit`), `command_source` (`builtin`)                                                                                                                            |
| `api_request`               | `model`, `query_source`, `effort`, `speed`, `agent.name`, `mcp_server.name`, `mcp_tool.name`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `cost_usd_micros`, `duration_ms`, `ttft_ms`, `request_id`, `client_request_id` |
| `api_error`                 | `model`, `query_source`, `effort`, `speed`, `error`, `status_code` (`404`), `duration_ms`, `attempt`, `request_id`                                                                                                                                                      |
| `assistant_response`        | `model`, `query_source`, `request_id`, `message.uuid`, `response` (`<REDACTED>`), `response_length`                                                                                                                                                                     |
| `tool_decision`             | `tool_name`, `tool_use_id`, `tool_source` (`builtin`, `mcp`), `decision` (`accept`, `reject`), `source` (`config`, `user_temporary`, `user_reject`)                                                                                                                     |
| `tool_result`               | `tool_name`, `tool_use_id`, `success`, `duration_ms`, `error_type` (`ShellError`, `TelemetrySafeError`), `decision_type`, `decision_source`, `tool_input_size_bytes`, `tool_result_size_bytes`, `mcp_server_scope`                                                      |
| `subagent_completed`        | `agent_type`, `agent.source`, `is_built_in`, `is_async`, `model`, `final_model`, `model_swapped`, `total_tokens`, `total_tool_uses`, `duration_ms`                                                                                                                      |
| `compaction`                | `trigger` (`manual`), `success`, `duration_ms`, `pre_tokens`, `post_tokens`, `precompute_reuse`                                                                                                                                                                         |
| `hook_registered`           | `hook_event`, `hook_type` (`command`, `http`), `hook_source` (`userSettings`, `flagSettings`), `safe_mode`                                                                                                                                                              |
| `hook_execution_start`      | `hook_event`, `hook_name` (such as `PreToolUse:Read`), `num_hooks`, `hook_source` (`merged`), `managed_only`, `safe_mode`                                                                                                                                               |
| `hook_execution_complete`   | the `hook_execution_start` attributes, plus `num_success`, `num_blocking`, `num_non_blocking_error`, `num_cancelled`, `total_duration_ms`, `stdout_chars`, `additional_context_chars`, `system_message_chars`, `initial_user_message_chars`, `num_outputs_persisted`    |
| `mcp_server_connection`     | `status` (`connected`), `transport_type` (`stdio`), `server_scope` (`dynamic`), `duration_ms`, `is_plugin`                                                                                                                                                              |
| `plugin_loaded`             | `plugin.name`, `plugin.scope` (`default-bundle`), `marketplace.name`, `plugin_id_hash`, `enabled_via`, `has_hooks`, `has_mcp`, `host_owned_mcp`, `agent_path_count`, `command_path_count`, `skill_path_count`, `safe_mode`                                              |
| `managed_settings_resolved` | `managed_settings.trigger`, `managed_settings.sources`, `managed_settings.source_behavior`, `managed_settings.helper.state`, `managed_settings.helper.applied`                                                                                                          |

- On events, `query_source` holds subsystem names: `sdk` (the main thread in `-p`),
  `repl_main_thread` (the main thread in an interactive session),
  `agent:builtin:Explore`, `compact`, `generate_session_title` and `prompt_suggestion`.
  Metrics use the three categories in the table above, and spans use
  `query_source_safe` with dots (`agent.builtin.Explore`).
- `hook_registered` listed the hooks in the user settings although
  `--setting-sources project,local` kept them from running: it inventories configured
  hooks, it does not prove that a hook runs.
- `num_hooks` and `num_success` also count hooks that Claude Code adds itself: with only
  the capture's hook configured, `PostToolUse` counted 2 hooks for Read, Write, Edit and
  Glob, and 3 for Bash. Failures show in `num_non_blocking_error` and `num_cancelled`.
- `hook_execution_start` and `hook_execution_complete` appeared for `SessionStart`,
  `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`,
  `PostToolUseFailure`, `PostToolBatch`, `SubagentStart`, `SubagentStop`, `TaskCreated`,
  `TaskCompleted` and `Stop`. `InstructionsLoaded`, `Notification`, `Elicitation`,
  `ElicitationResult`, `PreCompact`, `PostCompact`, `StopFailure` and `SessionEnd` fired
  their hooks but never produced them.

### Traces

| Span                               | Parent                                                                      | Attributes besides the common ones (observed values)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ---------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude_code.interaction`          | none (one root per prompt)                                                  | `span.type`, `interaction.sequence`, `interaction.duration_ms`, `user_prompt` (`<REDACTED>`), `user_prompt_length`, `parent.source` (`none`), `queued_sends`                                                                                                                                                                                                                                                                                                                                                                                |
| `claude_code.llm_request`          | `claude_code.interaction`, or the Agent tool's `claude_code.tool.execution` | `span.type`, `model`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, `llm_request.context` (`interaction`, `tool`), `query_source_safe`, `effort`, `speed`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `duration_ms`, `ttft_ms`, `first_content_ms`, `stop_reason`, `success`, `attempt`, `request_id`, `client_request_id`, `agent_id`; on failure `status_code`, `error` and `error_class` (`model_not_found`); span event `gen_ai.request.attempt` |
| `claude_code.tool`                 | `claude_code.interaction`, or the Agent tool's `claude_code.tool.execution` | `span.type`, `tool_name`, `tool_name_safe`, `tool_use_id`, `gen_ai.tool.call.id`, `duration_ms`, `agent_id`; for Bash, `bash_argv0` and `bash_command_class`                                                                                                                                                                                                                                                                                                                                                                                |
| `claude_code.tool.blocked_on_user` | `claude_code.tool`                                                          | `span.type`, `decision`, `source`, `duration_ms`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `claude_code.tool.execution`       | `claude_code.tool`                                                          | `span.type`, `tool_use_id`, `gen_ai.tool.call.id`, `success`, `duration_ms`; on failure `error` and `error_class` (`ShellError`, `TelemetrySafeError`)                                                                                                                                                                                                                                                                                                                                                                                      |

- Subagent spans (`llm_request` and `tool`) nest under the Agent tool's
  `claude_code.tool.execution` span, one level below the `claude_code.tool` span the docs
  name.
- Spans of a first-level subagent have `agent_id` but no `parent_agent_id`, as the docs
  describe for agents started from the main session. Deeper nesting was not exercised.
- Every tool call gets a `blocked_on_user` span, even when settings decide: 1 to 30 ms
  with `decision=accept` and `source=config`; 24 to 94 ms for the three calls denied in
  `-p` (`decision=reject`, `source=config`); 9.4 s when a person approved
  (`source=user_temporary`); 2.3 s when a person pressed Esc (`decision=reject` and
  `source=user_reject`, not the `user_abort` the docs give for a dismissed prompt).
- Not observed: `claude_code.hook` spans (detailed beta tracing was off) and `tool.output`
  span events (content off).

### Hooks

Every payload has `session_id`, `transcript_path`, `cwd` and `hook_event_name`, and none
has a timestamp. Beyond those:

- `prompt_id` is absent before the first prompt (`InstructionsLoaded`, `SessionStart`
  with `source=startup`) and present on every event after it. `/compact` and `/exit`
  change it without a `UserPromptSubmit`: `PreCompact`, the compaction's `SubagentStop`,
  `PostCompact` and `SessionStart` (`source=compact`) carry a new one, and `SessionEnd`
  after `/exit` another, each matching a telemetry `user_prompt` event with
  `command_name` `compact` or `exit`.
- `permission_mode` (`default`, `acceptEdits`) is only on `UserPromptSubmit`,
  `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`,
  `PostToolBatch`, `Stop` and `SubagentStop`.
- Inside a subagent, the tool events, `PermissionRequest` and `PostToolBatch` carry
  `agent_id` and `agent_type`.
- `scratchpad_dir` is on every event of the interactive sessions and never in `-p`. It is
  a path with the OS username, like `cwd` and `transcript_path`: personal data under
  ADR 0006. Being optional, the hook parsers and the ADR 0006 path filters (milestone 2)
  must not require it.
- `effort` is an object (`{"level": "medium"}`) on the tool events, `PermissionRequest`,
  `Stop`, `StopFailure` and `SubagentStop`, where telemetry has a string.
- Tool names observed: `Read`, `Write`, `Edit`, `Glob`, `Bash`, `PowerShell` (tried by
  the subagent on Windows), `Agent`, `TaskCreate`, `TaskUpdate`, `ToolSearch` and an MCP
  tool (`mcp__<server>__<tool>`).

| Event                          | Fields besides the common ones (observed values)                                                                                                                                                                        |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `InstructionsLoaded`           | `file_path`, `memory_type` (`Project`, the synthetic `CLAUDE.md`), `load_reason` (`session_start`)                                                                                                                      |
| `SessionStart`                 | `source` (`startup`, `compact`), `model` (interactive sessions only; absent in `-p`)                                                                                                                                    |
| `UserPromptSubmit`             | `prompt`                                                                                                                                                                                                                |
| `PreToolUse`                   | `tool_name`, `tool_input`, `tool_use_id`, `mcp_server` (MCP tools)                                                                                                                                                      |
| `PermissionRequest`            | `tool_name`, `tool_input`, `permission_suggestions` (optional: absent on the subagent's request); no `tool_use_id`                                                                                                      |
| `PostToolUse`                  | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms`, `mcp_server` (MCP tools); for Agent, `tool_response.status` is `completed` or, for a background subagent, `async_launched`                    |
| `PostToolUseFailure`           | `tool_name` (`Read`, `Bash`), `tool_input`, `tool_use_id`, `error`, `is_interrupt`, `duration_ms`                                                                                                                       |
| `PostToolBatch`                | `tool_calls` (each with `tool_name`, `tool_input`, `tool_response`, `tool_use_id`)                                                                                                                                      |
| `Notification`                 | `notification_type` (`permission_prompt`, `idle_prompt`, `elicitation_dialog`, `elicitation_response`), `message`                                                                                                       |
| `Elicitation`                  | `mcp_server_name`, `mode` (`form`), `message`, `requested_schema`                                                                                                                                                       |
| `ElicitationResult`            | `mcp_server_name`, `mode`, `action` (`cancel`)                                                                                                                                                                          |
| `SubagentStart`                | `agent_id`, `agent_type` (`Explore`)                                                                                                                                                                                    |
| `SubagentStop`                 | `agent_id`, `agent_type` (`Explore`, or `""` for internal agents), `agent_transcript_path`, `last_assistant_message` (optional: missing on one internal agent), `stop_hook_active`, `background_tasks`, `session_crons` |
| `TaskCreated`, `TaskCompleted` | `task_id`, `task_subject`, `task_description`                                                                                                                                                                           |
| `Stop`                         | `last_assistant_message`, `stop_hook_active`, `background_tasks`, `session_crons`                                                                                                                                       |
| `StopFailure`                  | `error` (`model_not_found`), `last_assistant_message`                                                                                                                                                                   |
| `PreCompact`                   | `trigger` (`manual`), `custom_instructions`                                                                                                                                                                             |
| `PostCompact`                  | `trigger` (`manual`), `compact_summary`                                                                                                                                                                                 |
| `SessionEnd`                   | `reason` (`other` at the end of a `-p` run, `prompt_input_exit` after `/exit`)                                                                                                                                          |

Registered during the capture but never fired: `UserPromptExpansion`, `PermissionDenied`,
`TeammateIdle`, `ConfigChange`, `CwdChanged`, `DirectoryAdded` and `PostModelSwitch`.

Order and timing:

- **Permission requests.** `PermissionRequest` fires in `-p` too (Bash, Write, and
  PowerShell inside the subagent). With nothing to decide it, the call is denied: no
  `PostToolUse`, `PostToolUseFailure` or `PermissionDenied` follows, and telemetry
  records `tool_decision` with `decision=reject` and `source=config`.
- **A dialog denied with Esc** has no closing hook event: no `PostToolUse` and no `Stop`.
  Telemetry records `tool_decision` with `decision=reject` and `source=user_reject`, and
  a `blocked_on_user` span.
- **Notification delays.** `permission_prompt` and `elicitation_dialog` arrive about 6 s
  after the dialog opens, and only if it is still open then (a dialog dismissed after
  2.3 s produced none). `idle_prompt` arrives 60 s after `Stop`.
- **Elicitation.** `Elicitation`, then `Notification` `elicitation_dialog`,
  `ElicitationResult`, `Notification` `elicitation_response` and the tool's
  `PostToolUse`.
- **`/compact`.** `PreCompact`, an internal `SubagentStop` (`agent_type` `""`, with no
  `SubagentStart`), `PostCompact`, and `SessionStart` with `source=compact`, whose async
  delivery ([ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)) arrived after
  `PostCompact`.
- **Internal agents** fire `SubagentStop` without `SubagentStart` at other times too,
  such as right after `Stop` in interactive sessions.
- **Foreground subagent.** `PreToolUse` (Agent), `SubagentStart`, the subagent's tool
  events, `SubagentStop`, then `PostToolUse` (Agent).
- **Background subagent.** `PostToolUse` (Agent, `async_launched`) fires as soon as the
  subagent is launched; with every hook async it even arrived before the `PreToolUse`.
  The main `Stop` arrives before the subagent ends and lists it in `background_tasks`
  with `status: running`. After `SubagentStop`, the subagent's report arrives as a
  `UserPromptSubmit`, followed by another `Stop`.
- **Teardown in `-p`.** With every hook async, no `SessionEnd` arrived. The
  [hooks reference](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
  says that in `-p` Claude Code kills async hooks still running at teardown.
- **Parallel tools.** The two-call batches in the fixtures ran one tool at a time: each
  call's `PreToolUse` was followed by its outcome (`PostToolUse`, `PostToolUseFailure`,
  or a `PermissionRequest` denied in `-p` with no closing event) before the next call's
  `PreToolUse`, and a subagent's tool events nested between the Agent call's
  `PreToolUse` and `PostToolUse`. The docs say parallel tool calls fire `PostToolUse`
  concurrently; that was not observed.
- **API failure.** An unknown model gave `StopFailure` with `error=model_not_found` and
  no `Stop`, alongside the `api_error` event with `status_code` 404.
- **Permission mode.** In exploratory runs outside the fixtures, the maintainer's
  interactive sessions started in `auto` mode, where no `PermissionRequest` fired; the
  starting mode depends on the user's settings.
- **Join keys.** The identifiers of ADR 0002 match across sources: `session_id` and
  `session.id`, `prompt_id` and `prompt.id`, `tool_use_id` (`gen_ai.tool.call.id` on
  spans), and a subagent's `agent_id` in hooks and spans. Internal agents' requests
  (`query_source_safe` `compact`, `prompt_suggestion`, `generate_session_title`) have
  `claude_code.llm_request` spans without `agent_id`, so the `agent_id` of their
  `SubagentStop` matches no span.

### Consequences

- Good, because the collector filters, the hook parsers and the state machine build on
  exact names that tests check.
- Good, because the differences from the docs (subagent span nesting, `user_reject` for
  Esc, identity attributes on data points instead of the resource, notification delays,
  hooks counted but not configured) are known before code relies on them.
- Good, because the isolated capture shows Claude Code with only cc-observatory's hooks,
  as a user would run it after `ccobs setup`.
- Bad, because the record comes from one version on one OS: Linux and macOS were not
  captured, and Claude Code releases often, so the contract has to be rechecked on
  upgrades.
- Bad, because several behaviours were seen once (one compaction, one elicitation, one
  API error), and others were not exercised at all (listed below).
- Neutral, because the names after translation to Prometheus and Loki are a separate
  decision (milestone 1).

### Confirmation

- `tests/test_fixtures.py` (milestone 0) runs over the fixtures, the executable form of
  this ADR. A recapture that changes a name or a field recorded here changes those tests
  and needs a new ADR.
- The collector config tests (milestone 1) and the hook parser tests (milestone 2) take
  these fixtures as input.
- `ccobs doctor` (milestone 4) warns when Claude Code is older than 2.1.296.
- The `reviewer` subagent checks that code uses telemetry and hook names recorded here or
  in a later ADR.

## More information

- Claude Code docs: [monitoring](https://code.claude.com/docs/en/monitoring-usage) and
  [hooks](https://code.claude.com/docs/en/hooks). How the capture is isolated and how to
  rerun it: `tools/capture/README.md`.
- Hook delivery (the transport, timeouts and the stack-down behaviour) is
  [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md).
- Not exercised: resumed, continued or cleared sessions; automatic compaction; pull
  requests; nested subagents; Linux and macOS; detailed beta tracing; the content opt-in
  (`OTEL_LOG_*` on); user plugins and project or user hooks besides cc-observatory's;
  sessions from the Claude Desktop app, whose `service.name` the docs give as
  `claude-code-desktop`.
- The hook fields kept or dropped (ADR 0006) and the signals that open and close a block
  are decided in milestone 2 from these fixtures.
