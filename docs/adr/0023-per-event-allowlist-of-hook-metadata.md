---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# The app keeps a per-event allowlist of hook metadata

## Context and problem statement

[ADR 0006](0006-privacy-metadata-only-by-default.md) has the app drop content from hook
payloads before storing anything, and leaves to milestone 2 the exact fields kept or
dropped, and whether the app keeps an allowlist or a denylist. The payloads of Claude
Code 2.1.296 ([ADR 0012](0012-observed-telemetry-and-hook-contract.md),
`tests/fixtures/hooks/`) mix metadata with content: prompts, tool inputs and outputs,
the calls of a tool batch, assistant messages, free-text errors (the fixtures'
`PostToolUseFailure` errors quote command output and a path with the OS username),
elicitation messages and schemas, task subjects, compaction summaries, and paths
(`transcript_path`, `scratchpad_dir`, `agent_transcript_path`, `file_path`) that contain
the OS username. The [hooks reference](https://code.claude.com/docs/en/hooks) documents
more of them, and Claude Code adds fields between releases. Which hook fields does the
app keep, and how does it decide?

## Decision drivers

- Without the content opt-in, no content reaches storage, including content in fields
  that Claude Code adds after the capture (ADR 0006).
- The app needs metadata only: identifiers, states, tool names, durations, sizes and
  enumerated values such as `notification_type` or `reason`.
- Exact rules that a test can check, each field traced to the fixtures or the docs.
- Parsing never fails on a field it does not need: a hook must still be accepted (and
  answered with an empty 2xx, [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md))
  when an optional field changes type.
- The parser lives in `ccobs.domain`: standard library only, no I/O.

## Considered options

- **A denylist.** Store the payload minus known content fields. New metadata arrives
  without code changes, but every content field Claude Code adds is stored until
  someone lists it, which breaks the first driver.
- **A per-event allowlist.** Parse each known event into a typed record of the fields
  allowed for it, compute sizes from content and discard the content. A new field is
  dropped until it is listed, so new metadata needs a code change.
- **Raw JSON with content stripped.** Keep the payload's structure and blank every
  string value outside a few known keys. The structure still leaks key names, list
  lengths and nested identifiers, and the rules become as long as an allowlist without
  giving typed fields.

## Decision outcome

Chosen option: "A per-event allowlist", because it is the only option where a field
nobody reviewed cannot reach storage, and it gives the state machine typed fields. The
cost, listing new metadata by hand, is the right way round for privacy. The collector
made the opposite choice for telemetry ([ADR 0021](0021-collector-privacy-filters.md)):
there, every new metadata attribute matters to dashboards and content arrives redacted
by default, while the app parses a small set of hook events into records it designs.
This ADR extends ADR 0006, which stays in force, as ADR 0006 leaves the exact hook
fields to milestone 2.

`ccobs.domain.hooks.parse_hook` turns a decoded payload into a frozen dataclass, one
per known event, and keeps only these fields:

- **Common, on every event:** `session_id` and `hook_event_name` (required, non-empty
  strings within the bounds below; anything else rejects the payload), `prompt_id`,
  `permission_mode`, `effort.level` (an object `{"level": ...}` in the fixtures; stored
  as `effort_level`), `agent_id`, `agent_type` and `cwd`.
- **`cwd` is the one path kept**, because projects come from it. It is personal data
  (ADR 0006): it is left out of the dataclass's repr, and no log line may contain it.
  Every other path is dropped, which narrows ADR 0006's "Paths" rule for hooks.
- **`model` on Bedrock** can be an inference-profile ARN that contains the AWS account
  ID. It is kept as metadata, the UI and logs treat it like `cwd`, and the storage and
  UI stages decide whether to shorten it.
- **Tool events** (`PreToolUse`, `PermissionRequest`, `PermissionDenied`,
  `PostToolUse`, `PostToolUseFailure`): `tool_name`, `tool_use_id`, the MCP server name
  and `tool_input_bytes`; `PostToolUse` adds `duration_ms`, `tool_response_bytes` and
  `tool_response_status`; `PostToolUseFailure` adds `duration_ms` and `is_interrupt`.
  `PermissionDenied`'s `reason` is free text (a classifier rule or a sentence) and is
  dropped. The MCP server name comes from the payload's `mcp_server.name`, or, when
  that is missing, from a tool name of the form `mcp__<server>__<tool>`.
- **`tool_response_status`** is `tool_response.status`, kept for any tool but only when
  it fully matches `^[a-z][a-z0-9_]{0,63}$`; every other key of `tool_response` is
  dropped. For `Agent`, `async_launched` is the only hook signal that a background
  subagent started, which the state machine needs.
- **`UserPromptSubmit`:** `prompt_length`, the prompt's length in characters.
- **`SessionStart`:** `source` and `model`. **`SessionEnd`:** `reason`.
- **`Notification`:** `notification_type`.
- **`Elicitation`:** `mcp_server_name`, `mode` and `elicitation_id`.
  **`ElicitationResult`:** `mcp_server_name`, `mode`, `action` and `elicitation_id`, an
  opaque ID that pairs a result with its request.
- **`PreCompact` and `PostCompact`:** `trigger`.
- **`Stop` and `SubagentStop`:** `stop_hook_active` and `background_tasks`, reduced to
  `id`, `type`, `status` and `agent_type` per item.
- **`StopFailure`:** `error`, only when it fully matches `^[a-z][a-z0-9_]{0,63}$`, the
  shape of the documented codes such as `model_not_found`; anything else may be API
  text and becomes `None`.
- **`SubagentStart`, `InstructionsLoaded`, `PostToolBatch`, `TaskCreated` and
  `TaskCompleted`:** the common fields only.
- **Unknown event names** give an `UnknownHookEvent` with the common fields only.
- **Sizes are computed, never copied:** `tool_input_bytes` and `tool_response_bytes`
  are the UTF-8 length of `json.dumps(value, separators=(",", ":"),
  ensure_ascii=False)`, and `None` when the field is absent or null.
- **Sizes count lone surrogates:** JSON allows an escaped lone surrogate, which has no
  UTF-8 form; it counts as the 3 bytes it would take (`surrogatepass`).
- **Bounds on every kept value**, because any local process can post to the hook
  endpoint, and kept values become SQLite rows and possibly Prometheus labels. A kept
  string (the IDs, `hook_event_name`, `tool_name`, `model`, the enumerated fields such
  as `source`, `reason` and `notification_type`, `elicitation_id` and the
  `background_tasks` fields) is dropped when it is longer than 256 characters or holds
  a control character (Unicode category Cc, which includes `\n`, `\t` and NUL) or a
  lone surrogate. `cwd` has the same rules with a 4096-character limit, because real
  working directories can pass 256 characters (`PATH_MAX` is 4096 on Linux, and Windows
  long paths pass the old 260-character limit). For `session_id` and `hook_event_name`
  a value outside the bounds rejects the payload. Integers (`duration_ms`) are kept
  only from 0 to 2^63 - 1, the range of a SQLite integer column; booleans are not
  integers. The prompt is never stored, so its length is counted at any size.
- **Wrong types never fail a payload:** an optional field of the wrong type becomes
  `None`, and items of `background_tasks` that are not objects are skipped. Error
  messages never echo payload values.
- **Never kept**, on any event: `prompt`, `tool_input`, `tool_response`, `tool_calls`,
  `last_assistant_message`, the `PostToolUseFailure` `error` text, `error_details`,
  `message`, `title`, `requested_schema`, `permission_suggestions`,
  `custom_instructions`, `compact_summary`, the `task_*` fields, `session_title`,
  `session_crons`, `PermissionDenied`'s `reason`, every key of `tool_response` but
  `status`, the `description`, `command` and other keys of `background_tasks` items,
  `transcript_path`, `scratchpad_dir`, `agent_transcript_path`, `file_path`, the
  rest of `mcp_server` and `effort`, and every key not listed above.
- **Content opt-in.** The opt-in of milestone 4 extends this allowlist with content
  fields; it does not replace the parser with raw storage.

Fields by source. Confirmed by the fixtures: every kept field above, with the values
listed in ADR 0012, `mcp_server` as an object `{"name", "source"}` on the MCP tool's
`PreToolUse` and `PostToolUse`, and `tool_response.status` (`completed` and
`async_launched`, both on `Agent`). Unverified, from the hooks reference only:

- `mcp_server` on `PermissionRequest` and `PostToolUseFailure` (no MCP tool needed a
  permission or failed in the capture), and `is_interrupt` being `true`;
- the `StopFailure` codes other than `model_not_found`, and `error_details`;
- `title` on `Notification`, and the notification types not captured;
- `elicitation_id` on `Elicitation` and `ElicitationResult` (kept), `url` on
  `Elicitation`, and `content` on `ElicitationResult`;
- `PermissionDenied` and its fields (kept as a tool event): it fires in auto mode only,
  was registered during the capture but never fired, and is in no committed fixture;
- the `command`, `server`, `tool` and `name` keys of `background_tasks` items, and the
  `prompt` of `session_crons` items (always empty lists in the fixtures);
- `session_title` on `SessionStart` and `UserPromptSubmit`, `agent_type` on
  `SessionStart`, and the resume fields of `SessionStart` (`context_tokens` and others);
- the `globs`, `trigger_file_path` and `parent_file_path` paths of `InstructionsLoaded`;
- the other events that never fired in the capture (`Setup`, `UserPromptExpansion`,
  `TeammateIdle`, `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `PostModelSwitch`),
  which are parsed as unknown events.

The fixtures and the hooks reference agree on every field this ADR keeps; no difference
between them changes the allowlist.

### Consequences

- Good, because a field that Claude Code adds is dropped until someone reviews it and
  lists it here, so new content never reaches storage by accident.
- Good, because the app and the state machine work with typed, frozen records instead
  of dictionaries, and a payload with an odd optional field is still accepted.
- Good, because sizes and the prompt length keep the views informative without content.
- Bad, because new metadata, such as the resume fields of `SessionStart`,
  `memory_type` and `load_reason` of `InstructionsLoaded`, `mcp_server.source` or the
  number of calls in a `PostToolBatch`, needs a code change and a new ADR that
  supersedes this one before it is stored. A recapture on a Claude Code upgrade (ADR 0012) is the
  moment to review them.
- Bad, because enumerated values (`source`, `reason`, `notification_type` and others)
  are bounded but not checked against the documented values, so a new value is kept
  without a code change; only `StopFailure`'s `error` and `tool_response.status` must
  have the shape of a code, because the fields around them carry free text.
- Bad, because a value outside the bounds is lost rather than truncated, for example a
  `cwd` longer than 4096 characters; truncating would store a wrong value.
- Neutral, because `cwd` stays personal data that the logging and the UI must handle
  with care; this ADR only keeps it out of the dataclass's repr.

### Confirmation

- `tests/test_domain_hooks.py` parses every line of every hook fixture to its expected
  class; plants a canary in every never-kept field, in nested values and in unknown
  keys, in every fixture payload and in synthetic payloads for the fields and events
  that only the docs describe, and checks that it is in neither the event's repr nor
  `dataclasses.asdict`, while `cwd` keeps its value and stays out of repr; checks that
  no event class has a never-kept field name; and covers the `StopFailure` code rule,
  the sizes (including non-ASCII text and lone surrogates), the string and integer
  bounds at their limits, `tool_response_status`, `elicitation_id`, `PermissionDenied`,
  `background_tasks`, unknown events, rejected payloads and wrong-typed optional
  fields.
- `tests/test_cli_light.py` and `tests/test_domain_telemetry.py` keep the parser on the
  standard library and free of I/O.
- The `reviewer` subagent checks that any change to the allowlist comes with a new ADR
  that supersedes this one.

## More information

- Claude Code [hooks reference](https://code.claude.com/docs/en/hooks): the common input
  fields and each event's input section.
- The capture behind the fixtures: ADR 0012 and `tools/capture/README.md`.
- Milestone 4 decides how the content opt-in reaches the app and which content fields it
  adds to the allowlist (ADR 0006).
- Revisit when a recapture shows new fields or events, or when the state machine needs
  a field that is not listed here.
