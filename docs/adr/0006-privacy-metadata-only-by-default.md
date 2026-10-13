---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Metadata only by default, with content capture as an opt-in

## Context and problem statement

Claude Code's telemetry and hook payloads can carry the user's prompts, tool inputs and
outputs (source code, file contents, commands and any secret in them), assistant messages
and error text, plus identity attributes such as the email address. cc-observatory keeps
its data on the user's machine ([ADR 0001](0001-local-single-machine-scope.md)), but
stored data ends up in screenshots, bug reports and backups. What should be stored by
default?

## Decision drivers

- Most views need metadata only: state, timings, tool names, sizes, counts, tokens, cost.
- Content is the most sensitive and the bulkiest part of the data.
- Storing content is the user's explicit, informed choice.
- The default holds even if content flags were turned on outside cc-observatory.
- Fixtures and bug reports must not leak anyone's data.

## Considered options

- **Capture everything locally.** It is the user's machine, and content makes debugging
  richer, but every screenshot, export and volume backup then holds code and secrets.
- **Metadata only, with an explicit opt-in for content.** A safe default; the opt-in has
  to be wired through Claude Code, the collector and the app.
- **Never capture content.** Simplest and safest, but no way to read prompts in the UI.

## Decision outcome

Chosen option: "Metadata only, with an explicit opt-in for content", because the default
stays safe and a user who wants content can still turn it on knowingly.

- **App.** Before storing anything, the app drops content from hook payloads: prompts,
  tool inputs and outputs (`tool_input`, `tool_response`), assistant messages
  (`last_assistant_message`) and error text (such as `error_details`). It keeps tool
  names, sizes and counts.
- **Claude Code.** `ccobs setup` does not set `OTEL_LOG_USER_PROMPTS`,
  `OTEL_LOG_TOOL_DETAILS` or `OTEL_LOG_TOOL_CONTENT`, so Claude Code keeps redacting.
- **Collector, content.** The stack enforces the default as well, because those flags
  can be turned on elsewhere (the shell, managed settings, a `--settings` file). While
  content capture is off, the collector drops content-bearing attributes and events
  before storage, for example: the `prompt`, `prompt_text`, `response`,
  `tool_parameters`, `tool_input` and free-text `error` attributes, and their
  counterparts on spans; the `api_request_body`, `api_response_body` and `system_prompt`
  events; `tool.output` span events; and `workspace.host_paths`, whose paths contain the
  OS username.
- **Collector, identity.** It always deletes `user.email`, `user.account_uuid`,
  `user.account_id`, `organization.id` and `user.id` (an installation ID, or the identity
  provider subject on gateway sessions), plus `user.groups` and `identity.source` on
  gateway sessions.
- **Opt-in.** `ccobs setup --capture-content` shows a warning, then turns content on in
  Claude Code's settings, the collector and the app. When `OTEL_LOG_ASSISTANT_RESPONSES`
  is unset it follows `OTEL_LOG_USER_PROMPTS`, and `OTEL_LOG_TOOL_CONTENT` only has an
  effect with tracing on. `ccobs doctor` warns when Claude Code's content flags are on
  without the opt-in.
- **Paths.** `cwd`, `transcript_path` and the other paths in hook payloads contain the
  OS username. They are kept as metadata (projects come from `cwd`) and treated as
  personal data.
- **No outbound traffic.** The bundled stack turns off upstream usage reporting and
  update checks in Grafana, Loki and Tempo.
- **Fixtures.** Test fixtures come from real sessions, sanitized, and a guard test fails
  if personal data shows up in them.

### Consequences

- Good, because a default install stores no code, prompts or secrets, even when content
  flags were turned on outside cc-observatory.
- Good, because the user's email address and account IDs never reach storage.
- Bad, because the UI shows what a session did, not what was said.
- Bad, because the filters in the app and the collector must follow Claude Code as it
  adds payload fields, attributes and events.
- Bad, because the opt-in spans three places: Claude Code's settings on the host, and
  the collector and the app in containers.
- Neutral, because `user.id` is removed although it is anonymous outside gateway
  sessions; on a single machine it adds nothing.

### Confirmation

- A guard test over `tests/fixtures/` (milestone 0) fails on emails, account IDs,
  usernames in paths and hostnames.
- Collector config tests (milestone 1) check that every pipeline deletes each identity
  attribute, on resources and on data points, log records and spans; that with content
  capture off, input carrying the content attributes and events above is stored without
  them; and that usage reporting and update checks stay off.
- App tests (milestone 2) feed payloads full of content and check that none is stored.
- CLI tests (milestone 4) check that the default `env` block sets no `OTEL_LOG_*` content
  variable, that `--capture-content` sets them after the warning, and that `doctor` warns
  when they are on without the opt-in.
- The `reviewer` subagent checks every diff for content stored without the opt-in.

## More information

- Claude Code's [monitoring docs](https://code.claude.com/docs/en/monitoring-usage)
  list what each `OTEL_LOG_*` variable reveals.
- Milestone 1 implements and tests the collector's processors and the exact list of
  dropped attributes and events. Milestone 2 decides the exact hook fields kept or
  dropped, and whether the app keeps an allowlist or a denylist, from the spike's
  fixtures.
- Milestone 4 decides how `--capture-content` switches the collector and the app on,
  given that the CLI runs on the host and they run in containers, and whether it should
  ever set `OTEL_LOG_RAW_API_BODIES`, which `ccobs setup` does not set.
