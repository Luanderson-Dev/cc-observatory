---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# Register only the hook events the state machine needs

## Context and problem statement

[ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md) delivers hooks as `http`
hooks with `timeout: 1`, plus an async exec-form `curl` on `SessionStart`, and leaves
open which events are registered: "only those the product uses", decided with the
session state machine in milestone 2 and written by `ccobs setup` in milestone 4. Every
registered `http` event costs a request, and up to 1 s of Claude Code's time if the app
hangs; while the stack is down, every one of them writes a hook-error notice in the
user's transcript (ADR 0011). The [hooks reference](https://code.claude.com/docs/en/hooks)
documents 33 events (its "Hook lifecycle" table, read on 2026-10-15), five of which
CLAUDE.md forbids, and the state machine of
[ADR 0024](0024-session-agent-and-block-state-machine.md) reacts to a subset of the rest.
Which events does cc-observatory register, and with which settings?

## Decision drivers

- Every registered `http` event must earn its request and its notice when the stack is
  down (ADR 0011).
- The session, agent and block rules of ADR 0024 must receive every event they rely on.
- Never register an event whose registration changes Claude Code's behaviour, and never
  an `http` hook on `SessionStart` or `Setup` (CLAUDE.md).
- Prefer events that carry metadata over events whose payload is mostly content or
  paths (ADRs 0006 and 0023).
- One source for the hook settings: `ccobs setup`, `ccobs doctor` and the docs must
  agree.

## Considered options

- **Register everything allowed.** Every documented event except the forbidden ones,
  as the milestone 0 captures did (`tools/capture/make_settings.py`). Nothing is
  missed, but events the app ignores still cost a request each, add a notice each
  while the stack is down, and several of them (`InstructionsLoaded`, `CwdChanged`,
  `DirectoryAdded`, `TaskCreated`, `UserPromptExpansion`) carry mostly paths or content.
- **Register what the state machine needs.** The events ADR 0024 reacts to, and nothing
  else; an event joins the list with the rule that uses it.
- **Register a minimal set.** Only the events that open and close blocks and end
  sessions (`PermissionRequest`, `Elicitation`, `ElicitationResult`, `PostToolUse`,
  `PostToolUseFailure`, `Stop`, `SessionEnd`). Fewer notices, but no turns, no
  subagents, no compaction and no tool activity, so the `working` state and the agent
  tree of ADR 0024 cannot be derived.

## Decision outcome

Chosen option: "Register what the state machine needs", because it is the smallest set
that feeds every rule of ADR 0024 except the `PermissionDenied` and `PostToolBatch`
rules, which stay as fallbacks for hooks registered by hand; "everything" pays requests
and notices for events nobody reads, and the minimal set cannot tell a working session
from an idle one.

`ccobs.domain.hook_config` holds the lists, and every documented event is in exactly
one of them:

- **`http` hooks** (`REGISTERED_HTTP_EVENTS`), in this order: `UserPromptSubmit`,
  `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`,
  `Notification`, `Elicitation`, `ElicitationResult`, `SubagentStart`, `SubagentStop`,
  `Stop`, `StopFailure`, `PreCompact`, `PostCompact`, `SessionEnd`.
- **Async command hook** (`ASYNC_COMMAND_EVENTS`): `SessionStart`, which accepts no
  `http` hook.
- **Forbidden** (`FORBIDDEN_EVENTS`, CLAUDE.md): `WorktreeCreate`, `WorktreeRemove`,
  `PreModelSwitch`, `MessageDisplay`, `FileChanged`.
- **Not registered** (`NOT_REGISTERED`), each with its reason:
    - `Setup`: runs only with `--init-only`, or `--init`/`--maintenance` in `-p` mode,
      and accepts no `http` hook;
    - `UserPromptExpansion`: its payload is the prompt and command arguments, content;
      `UserPromptSubmit` already marks the turn;
    - `PermissionDenied`: fires only in auto mode, and never fired in the milestone 0
      captures; the next progress event in the scope closes its block (ADR 0024);
    - `PostToolBatch`: not needed: tool outcomes, progress events and `Stop` already
      settle pending calls (maintainer decision for milestone 2);
    - `TaskCreated` and `TaskCompleted`: task lists are not tracked, and their subjects
      are content;
    - `TeammateIdle`: agent teams are not tracked; subagents come from `SubagentStart`;
    - `InstructionsLoaded`: carries instruction-file paths and their scope; instruction
      loading is not tracked, and it fires many times per session;
    - `ConfigChange`: configuration changes are not tracked;
    - `CwdChanged`: carries only paths, and `cwd` already arrives on every event;
    - `DirectoryAdded`: carries only a path, and directories are not tracked;
    - `PostModelSwitch`: the model comes from `SessionStart` and the telemetry.
- **The parser and the reducer stay wider than the list.** `ccobs.domain.hooks` parses
  `PermissionDenied`, `PostToolBatch`, `InstructionsLoaded`, `TaskCreated` and
  `TaskCompleted`, and ADR 0024's rules for `PermissionDenied` and `PostToolBatch` stay,
  so a user who registers them by hand, or a later ADR that adds them, gets correct
  state without a parser change.
- **URL.** `http://127.0.0.1:4300/hooks` by default: port 4300 is the app's in
  [ADR 0018](0018-ports-networks-and-exposure.md), and `/hooks` is the hook path.
  `hook_settings` accepts only `http` to `127.0.0.1`, with an optional port and a path
  of plain segments ending in `/hooks`, without user info, query or fragment; anything
  else raises `ValueError`, without echoing the URL. `localhost` and `[::1]` are
  rejected: ADR 0018 publishes every port on `127.0.0.1` only and tells clients to use
  it rather than `localhost`, which may resolve to `::1`; `[::1]` can never connect,
  and inside `curl` arguments its brackets risk URL globbing.
- **Handlers.** Each event gets one group without a `matcher` (every occurrence
  matches), with one handler:
    - `http`: `{"type": "http", "url": <url>, "timeout": 1}`;
    - `SessionStart`: `{"type": "command", "command": "curl", "args": ["-s", "-m", "2",
      "-o", <null device>, "-H", "Content-Type: application/json", "--data-binary",
      "@-", <url>], "async": true, "timeout": 5}`, with `NUL` as the null device on
      Windows and `/dev/null` elsewhere. Exec form (`args`) spawns `curl` without a
      shell, so nothing is quoted or redirected on any OS, as in the milestone 0
      captures. `-m 2` bounds the process, since Claude Code does not enforce `timeout`
      on a running async hook; `timeout: 5`, the value the captures used, stays above it.
      This also settles ADR 0011's other open item, the exact timeout of the async hook:
      it is 5, while `curl`'s `-m 2` is the real bound, because Claude Code does not
      enforce the timeout on a running async hook.
  No handler has `onFailure`, `statusMessage` or, on `http` hooks, `async`.
- **One source.** `hook_settings(url, *, windows)` returns the `{"hooks": {...}}` block.
  `ccobs setup` (milestone 4) writes it, and the hook settings snippet in the docs
  (stage 15 of milestone 2) is generated from it.

### Consequences

- Good, because 15 `http` events cover every rule of ADR 0024 except the
  `PermissionDenied` and `PostToolBatch` rules, which stay as fallbacks for hooks
  registered by hand, and the 12 unregistered events cost nothing, not even a notice
  while the stack is down.
- Good, because events that carry mostly paths or content never leave Claude Code, on
  top of the allowlist of [ADR 0023](0023-per-event-allowlist-of-hook-metadata.md).
- Good, because the settings come from one pure function, so setup, doctor and the docs
  cannot drift, and the unit tests check them against the documented events.
- Bad, because each tool call still costs two requests (`PreToolUse` and
  `PostToolUse`), and with the stack down each registered event writes a notice;
  mitigated as in ADR 0011.
- Bad, because a request denied in auto mode stays open until the next progress event
  of its scope, or until the telemetry stage closes it, since `PermissionDenied` is not
  registered.
- Bad, because a new event in the hooks reference is not covered until it is
  classified here; the documented list in the tests has to be updated by hand.
- Neutral, because adding an event later is a change to `REGISTERED_HTTP_EVENTS` with a
  new ADR, and `ccobs setup` rewrites the hooks block on the next run.

### Confirmation

- `tests/test_domain_hook_config.py` checks that every event in the hooks reference is
  in exactly one category, that no forbidden event is registered, that `SessionStart`
  and `Setup` never get an `http` hook, that every registered event has a parser class
  in `ccobs.domain.hooks` and is in `ccobs.domain.sessions.HANDLED_EVENTS`, that every
  handler
  has an explicit `timeout` (1 on `http`), that `onFailure`, `statusMessage` and an
  `http` `async` appear nowhere, the exact `curl` arguments on both OSes, the accepted
  and rejected URLs, and that the output is JSON and deterministic. The module stays at
  100% coverage.
- `tests/test_domain_sessions.py` checks that every event in `HANDLED_EVENTS` changes
  the state differently from an unknown event, so the list cannot claim a rule the
  reducer does not have.
- `tests/test_domain_telemetry.py` and `tests/test_cli_light.py` keep
  `ccobs.domain.hook_config` on the standard library and free of I/O, so the light CLI
  can import it.
- `ccobs setup` tests (milestone 4) check that the hooks it writes equal
  `hook_settings`.

## More information

- Claude Code [hooks reference](https://code.claude.com/docs/en/hooks): the event list,
  matchers, `http` hook fields, exec form and async hooks.
- [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md) (delivery and
  timeouts), [ADR 0023](0023-per-event-allowlist-of-hook-metadata.md) (fields kept) and
  [ADR 0024](0024-session-agent-and-block-state-machine.md) (the rules these events
  feed). This ADR settles the open item of ADR 0011 on the events registered.
- Revisit when ADR 0024 gains a rule that needs another event, when the hooks reference
  adds or removes events, or when Claude Code accepts `http` hooks on `SessionStart`.
