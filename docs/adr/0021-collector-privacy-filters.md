---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Privacy filters in the collector

## Context and problem statement

[ADR 0006](0006-privacy-metadata-only-by-default.md) has the collector always delete
identity attributes and, while content capture is off, drop content; it lists the
content "for example" and leaves the exact list to milestone 1. The names come from two
sources. The fixtures ([ADR 0012](0012-observed-telemetry-and-hook-contract.md)) show
what Claude Code 2.1.296 sends with the content flags off and detailed beta tracing off,
so most content attributes never appear in them; the others are only in Claude Code's
[monitoring docs](https://code.claude.com/docs/en/monitoring-usage). And users can add
attributes through `OTEL_RESOURCE_ATTRIBUTES`, which Claude Code "attaches ... as
attributes on every metric datapoint and event record, in addition to sending them in
the OTLP resource block"; the docs' own examples include `enduser.id`, to attribute
usage to a person, and `host.name`. What exactly does the collector delete, and where
in its pipelines?

## Decision drivers

- Identity never reaches storage, whatever Claude Code, a gateway or the user adds
  (ADR 0006).
- While content capture is off, content never reaches storage, even with `OTEL_LOG_*`
  flags set outside `ccobs setup` (ADR 0006).
- Exact lists that a test can check, each name traced to the fixtures or the docs.
- Metadata stays: names, sizes, counts, durations, status codes and error types.
- The content opt-in (milestone 4) can turn content filtering off without touching
  identity.
- Nothing is processed, printed or exported before the filters run.

## Considered options

- **Deny lists in `transform` and `filter` processors, with a prefix rule for
  identity.** One processor type reaches every level (resource, data point, log record,
  span, span event) through OTTL; the lists must follow Claude Code.
- **The `attributes` and `resource` processors**, as in the docs' example
  (`attributes/drop-prompt-text`). Delete by key or regex, but the `attributes`
  processor covers span, log and metric attributes only, so resources need a second
  processor and span events are not covered.
- **The `redaction` processor as an extra net.** It can keep only allowed keys, or mask
  values that match regular expressions, such as an e-mail pattern. An allowlist would
  drop every new metadata attribute until it is listed, and an e-mail mask would also
  mask legitimate content once the opt-in exists.
- **Filtering in the app only.** One place for hooks and telemetry, but telemetry never
  passes through the app: it goes from Claude Code to the collector and on to the
  backends ([ADR 0003](0003-observability-backends.md)).

## Decision outcome

Chosen option: "Deny lists in `transform` and `filter` processors, with a prefix rule
for identity", because it reaches every level where Claude Code puts attributes, keeps
identity and content in separate processors, and gives exact lists that a test compares
with the data it plants. This ADR extends ADR 0006, which stays in force.

- **Identity, always deleted**, by `transform/identity`, at every level: resource,
  metric data point, log record, span and span event. It deletes every key that starts
  with `user.` or `enduser.` (OTTL `delete_matching_keys`, with the pattern anchored at
  the start of the key), plus `organization.id`, `identity.source` and `host.name`. The
  prefix rule covers new `user.*` attributes without a change. `host.name` is the
  machine's name, personal data under ADR 0006; the stack's own `host` label
  ([ADR 0001](0001-local-single-machine-scope.md)) comes from configuration, as a later
  ADR of this milestone decides.
    - Seen in the fixtures, on every data point, log record and span: `user.id`,
      `user.email`, `user.account_uuid`, `user.account_id` and `organization.id`.
    - From the docs only: `user.groups` and `identity.source` (sessions signed in to a
      Claude apps gateway), and keys added through `OTEL_RESOURCE_ATTRIBUTES`, such as
      `enduser.id` and `host.name`.
- **Content, deleted while content capture is off**, by `filter/content` (whole records
  and span events) and `transform/content` (attributes). They are separate from
  `transform/identity`, so that the opt-in of milestone 4 can take them out of the
  pipelines:
    - log record attributes: `prompt`, `prompt_text`, `response`, `tool_parameters`,
      `tool_input`, `error`, `body`, `body_ref`, `hook_definitions` and
      `workspace.host_paths`;
    - span attributes: `user_prompt`, `new_context`, `system_reminders`,
      `system_prompt_preview`, `user_system_prompt`, `response.model_output`,
      `tool_input`, `full_command`, `file_path`, `hook_definitions`, `error` and
      `workspace.host_paths`;
    - resource, metric data point and span event attributes: `workspace.host_paths`;
    - whole log records of the events `api_request_body`, `api_response_body` and
      `system_prompt`, matched on the `event.name` attribute, on the record's event
      name field (empty in the fixtures) and on the body `claude_code.<name>`;
    - span events named `tool.output`.
- **Where the content names come from.**
    - Seen in the fixtures: `prompt` and `prompt_text` on `user_prompt` events and
      `response` on `assistant_response`, all `<REDACTED>`; the free-text `error` of
      `api_error` (for example `model: <model name>`); `user_prompt` (`<REDACTED>`) on
      `claude_code.interaction` spans; and `error` on failed `claude_code.llm_request`
      and `claude_code.tool.execution` spans.
    - From the docs only: `tool_parameters` (`tool_result`, `tool_decision`) and
      `tool_input` (`tool_result`), with `OTEL_LOG_TOOL_DETAILS`; `body` and `body_ref`,
      on the body events that only `OTEL_LOG_RAW_API_BODIES` produces; the free-text
      `error` of `compaction` and `api_retries_exhausted`, and, with
      `OTEL_LOG_TOOL_DETAILS`, of `tool_result` and `mcp_server_connection`;
      `hook_definitions`, the JSON hook configuration, on `hook_execution_start` and
      `hook_execution_complete` (detailed beta tracing with `OTEL_LOG_TOOL_DETAILS`);
      on `claude_code.tool` spans, `full_command` (the Bash command string) and
      `file_path` (the target of Read, Edit and Write, which also carries the OS
      username), with `OTEL_LOG_TOOL_DETAILS` alone; on spans, under detailed beta
      tracing, `new_context`, `system_reminders`, `system_prompt_preview`,
      `user_system_prompt` and `response.model_output` (with `OTEL_LOG_USER_PROMPTS`,
      or `OTEL_LOG_TOOL_CONTENT` for `new_context` on `claude_code.tool`), `tool_input`
      on `claude_code.tool` and `hook_definitions` on `claude_code.hook` (with
      `OTEL_LOG_TOOL_DETAILS`); the `system_prompt` event (detailed beta tracing with
      `OTEL_LOG_USER_PROMPTS`); and `tool.output` span events
      (`OTEL_LOG_TOOL_CONTENT`).
    - `workspace.host_paths` (host workspace directories chosen in the desktop app) is
      documented on events only. Its paths contain the OS username, so it is deleted
      on spans, span events, resources and data points too, in case it arrives there,
      for example through `OTEL_RESOURCE_ATTRIBUTES`.
- **Considered and kept as metadata.** The repository attributes
  `vcs.repository.url.full` and `vcs.owner.name` (sent only with
  `OTEL_METRICS_INCLUDE_REPOSITORY`, off by default) can name the user's account, and
  `managed_settings.helper.path` (the policy helper's configured path, sent whenever a
  helper is selected) can contain the OS username. Like `cwd` in ADR 0006, they are
  personal data kept as metadata. `skill_name`, `subagent_type` and `workflow.name` on
  `claude_code.tool` spans are names, metadata too.
- **Structured fields stay**: `status_code`, `error_type`, `error_class`, lengths and
  sizes such as `prompt_length` and `tool_input_size_bytes`, so failures can still be
  counted by kind. Deleting `error` on spans also removes the short category that
  `claude_code.tool.execution` carries without `OTEL_LOG_TOOL_DETAILS` (such as
  `Error:ENOENT`); `error_class` keeps the kind.
- **Order.** In every pipeline, `memory_limiter` comes first, then `transform/identity`,
  then the content processors, before any other processor and any exporter.
- **Fail closed.** The three processors use `error_mode: propagate`: a statement that
  fails refuses the batch instead of passing it on unfiltered.
- **No printing exporters.** The bundled config has no `debug` or `file` exporter (nor
  the older `logging`): they would write telemetry, content included, to the container
  logs or to files outside the stores.

### Consequences

- Good, because identity never reaches Prometheus, Loki or Tempo, whether it comes from
  Claude Code, a gateway or `OTEL_RESOURCE_ATTRIBUTES`.
- Good, because content flags turned on outside `ccobs setup` are neutralised before
  storage, raw API bodies and detailed beta tracing included.
- Good, because the opt-in of milestone 4 removes two processors and leaves identity as
  it is.
- Bad, because the content lists must follow Claude Code as it adds attributes and
  events. The fixture recapture (ADR 0012) and the canary test below catch a change only
  for names they know, so each recapture also rereads the monitoring docs.
- Bad, because the prefix rule also deletes any future `user.*` or `enduser.*` key that
  is not identity; keeping one would need a new ADR.
- Bad, because the repository attributes and the policy helper's path, when Claude Code
  sends them, are stored; like the paths in hook payloads, they are treated as personal
  data in screenshots, exports and bug reports.
- Neutral, because the filters run at every level: Claude Code copies
  `OTEL_RESOURCE_ATTRIBUTES` to data points and log records, so filtering the resource
  alone would leave them as metric labels and event attributes.

### Confirmation

- `tests/test_collector_config.py` (milestone 1) parses
  `stack/otel-collector/config.yaml` and checks: that `memory_limiter` comes first in
  every pipeline, followed by `transform/identity` and the content processors; that
  `transform/identity` deletes every identity canary of `tools/stack/replay.py` at
  every level, and agrees with `is_identity_key` on every key in the fixtures plus near
  misses such as `user_prompt`, `xuser.id` and `host.arch`; that `transform/content`
  deletes exactly the replayer's content canary lists at each level; that
  `filter/content` drops exactly the three content events and `tool.output` span
  events; that the privacy processors fail closed; and that no `debug`, `file` or
  `logging` exporter exists.
- An integration test (`tests/integration/test_stack_privacy.py`) replays the fixtures
  with canaries planted at every level (`tools/stack/replay.py --canaries`) and finds
  none of them in Prometheus, Loki or Tempo.
- The `reviewer` subagent checks every change to the collector config or to the canary
  lists against this ADR.

## More information

- Claude Code [monitoring docs](https://code.claude.com/docs/en/monitoring-usage):
  standard attributes, multi-team support (`OTEL_RESOURCE_ATTRIBUTES`), the audit
  section (`enduser.id`), content attributes under detailed beta tracing, the
  `tool.output` span event, and security and privacy.
- OpenTelemetry Collector contrib:
  [transform processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/transformprocessor/README.md),
  [OTTL functions](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/pkg/ottl/ottlfuncs/README.md)
  (`delete_key`, `delete_matching_keys`),
  [filter processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/filterprocessor/README.md),
  [attributes processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/attributesprocessor/README.md)
  and [redaction processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/redactionprocessor/README.md).
- How the opt-in takes the content processors out of the running collector is decided
  in milestone 4 (ADR 0006), which can reconsider the `redaction` processor as a net
  for content that the opt-in lets through.
- Revisit when Claude Code adds content-bearing attributes or events, or identity
  attributes outside the `user.*` and `enduser.*` prefixes.
