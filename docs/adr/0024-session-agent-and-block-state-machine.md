---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# A hook-driven reducer derives session, agent and block state

## Context and problem statement

[ADR 0002](0002-native-telemetry-plus-hooks.md) leaves to milestone 2 the session state
machine and the signals that open and close a block,
[ADR 0007](0007-alerts-browser-and-discord.md) builds its first alert rule on the
`waiting_user` state that this machine defines, and
[architecture.md](../architecture.md) lists the five states but leaves the transitions,
the block signals and the `stale` timeout to "real fixtures, not the documentation
alone". The fixtures of Claude Code 2.1.296
([ADR 0012](0012-observed-telemetry-and-hook-contract.md), `tests/fixtures/hooks/`)
constrain the answer:

- hook payloads carry no timestamp
  ([ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)), so the only clock is
  the time the app receives each event, and the only order is the sequence number the
  app persists with it;
- `SessionStart` is best effort and can arrive late or never (ADR 0011);
- `PermissionRequest` has no `tool_use_id`; a dialog denied with Esc, or a request
  denied in `-p`, fires no closing hook;
- `/compact` and `/exit` change `prompt_id` without a `UserPromptSubmit`, and
  `/compact` has no `Stop`;
- internal agents fire `SubagentStop` without `SubagentStart`;
- with async delivery, a `PostToolUse` arrived before its `PreToolUse`, and a background
  subagent outlives the main `Stop`.

How does the app turn hook events into sessions, agents and blocks?

## Decision drivers

- Live state within a second of the hook, above all `waiting_user` (ADRs 0002, 0007).
- One deterministic rule set that tests can replay over the fixtures, with no wall
  clock, so that replays, restarts and the storage stage give the same result.
- Tolerance of what the fixtures show: missing, late and out-of-order events.
- `ccobs.domain` stays free of I/O (CLAUDE.md), so the rules are a pure function the
  ingestion, the sweep and the telemetry join can all call.
- The state is persisted in SQLite ([ADR 0005](0005-sqlite-for-app-state.md)) by a later
  stage, so every change must come out as a record to write.

## Considered options

- **Derive state from the telemetry in Loki.** `claude_code.tool_decision`,
  `claude_code.api_error` and the `blocked_on_user` span hold decisions and errors, but
  events are exported in batches (every 5 s by default) and the wait span arrives after
  the wait ends (ADR 0002). It cannot say that a session is waiting now.
- **A hook-driven pure reducer.** `apply(state, event, received_at_ms, seq)` returns the
  new immutable state and a list of effects. Live, deterministic and testable over the
  fixtures; signals that only telemetry has (an Esc denial) need a separate entry point.
- **A mutable state machine inside the app's database layer.** Fewer types, but every
  rule would need a database to test, and replaying the fixtures would need the app.
- **Timer-based idle**: a session is idle after N seconds without events. Simple, but a
  long tool call or a slow API request looks idle, and the `idle_prompt` notification
  shows that Claude Code itself waits 60 s. **Turn-based idle**: idle when the main
  turn has ended and no subagent runs, from `Stop`/`StopFailure` and `SubagentStop`.

## Decision outcome

Chosen option: "A hook-driven pure reducer" with turn-based idle, because it is the only
option that knows a session is waiting at the moment the hook arrives, and as a pure
function in `ccobs.domain.sessions` it is replayed over every fixture by the unit tests.
Telemetry still matters: `close_block_denied` lets the later telemetry stage close a
block that no hook closes. A timer remains only where nothing else can tell: `stale`.

**Inputs.** `apply` takes the parsed event (ADR 0023), the receive time in milliseconds
and the persisted `seq`. An event whose `seq` is not above the session's `last_seq` was
already applied and changes nothing, so a replay after a restart is harmless. An event
of another session than the state's raises `ValueError`: that is a programming error,
which the caller that persists events handles. Times that
go backwards are tolerated: `started_at` is the earliest receive time, `last_event_at`
the latest, and every close or notification time is clamped to the block's opening, so
no duration is negative.

**State and effects.** The state holds the session, its running agents keyed by
`agent_id`, the IDs of the last 64 stopped agents, its open blocks, the pending tool
calls by `tool_use_id`, the last 64 completed `tool_use_id`s and the unclaimed
background launches. A closed block and a stopped agent leave the state (their last
effect carried them to storage), so the state stays bounded however long the session
runs: an interactive session gets a new internal agent every turn. Each call returns
effects that carry the full new record, so the storage stage can upsert them:
`AgentChanged`, `BlockOpened`, `BlockNotified` and `BlockClosed` in event order, then
always one `SessionChanged`, with the previous status for the metrics and the UI. A
block's ID is the `seq` of the event that opened it.

**Session states** (`working`, `waiting_user`, `idle`, `ended`, `stale`):

- `waiting_user` if any block is open;
- otherwise `working` if the main thread has a turn open, a compaction is running, or a
  non-internal subagent is running (background agents included);
- otherwise `idle`;
- `ended` after `SessionEnd`, with its `reason` as `end_reason`. It is not terminal: any
  later event other than `SessionStart` reopens the session and clears the end;
- `stale` only through `mark_stale(state, now_ms, stale_after_ms)`, which the sweep
  calls: a session silent for `stale_after_ms`, neither ended nor stale, becomes
  `stale`, its open blocks close with reason `stale` and source `sweep` at `now_ms`
  (the time the app gave up waiting, clamped to the opening), and its running agents
  stop with `stop_reason` `stale`, so the live "agents running" series never counts
  ghosts. Any later event derives the state again.
- `mark_stale` also stops agents in a session that is not silent: a running agent with
  no event in its scope for `stale_after_ms` stops with `stop_reason` `stale` (its
  `SubagentStop` was lost, which would keep the session `working` forever), and the
  blocks of its scope close as `stale`. A stale agent's `stopped_at` is its
  `last_event_at`, the last time it was seen.
- The threshold is the maintainer's product decision **P3**: 12 hours, applied by the
  sweep stage. That is far longer than any realistic wait at a permission dialog, so
  the sweep closing a block that a person would still have answered is accepted at
  that threshold. The sweep stage must not use a short threshold: with no hook during a
  long wait or a long tool call, a short one would mark live sessions and agents
  stale.

**Lazy creation** (ADR 0011). The first event of any type creates the session.
`SessionStart` fills `source` (the first one wins, so `compact` after `/compact` does
not replace `startup`) and `model` (the latest wins), and never moves the state: it
does not reopen an ended or stale session.

**Turns.** The main thread's turn opens on `UserPromptSubmit` and closes on `Stop` or
`StopFailure`. `StopFailure` records its `error` as `last_error_code`; by the
maintainer's product decision **P2** it is not a block (ADR 0007 alerts on repeated
failures instead). `prompt_id` is kept as `last_prompt_id` from any event, but never
keys the turn, because `/compact` and `/exit` change it. A new prompt clears
`idle_notified_at`.

**Compaction.** `PreCompact` to `PostCompact` counts as work. A main-thread `manual`
compaction is typed at the prompt, so it also ends a turn still open, the same way
`Stop` does (blocks superseded, the main thread's pending calls dropped): in the
`interactive-permission-compact` fixture that turn was left open by the dialog denied
with Esc, which fires no `Stop`, and restoring it would show the session working from
`/compact` to `/exit`. Any other trigger (`auto`, unverified) leaves the turn as it was,
so an automatic compaction in the middle of a turn returns to `working`. A main-thread
`UserPromptSubmit`, `Stop` or `StopFailure` also ends the compaction: a new or ended
turn proves it is over, which covers a lost `PostCompact` and a compaction that failed
or was cancelled (unverified: none of these is in the fixtures).

**Scope.** An event belongs to the main thread when `agent_id` is absent, otherwise to
that agent. Agents are keyed by `agent_id`, never by `agent_type`. Only main-thread
events open or close the turn.

**Agents.**

- `SubagentStart` makes an agent `running`, with its type; `SubagentStop` makes it
  `stopped` with `stop_reason` `stopped`. Every event in an agent's scope, its start
  and its stop update its `last_event_at`.
- An agent whose first event is `SubagentStop` is `internal` and never makes the
  session work (the fixtures' compaction agent and the agents after `Stop`, with
  `agent_type` `""`).
- Agent events that arrive before `SubagentStart` create the agent as `running`. Any
  event of an agent among the remembered stopped IDs (a late `SubagentStart`, a
  repeated `SubagentStop`, a late tool event) leaves it stopped and emits nothing;
  storage keeps its record.
- Known limitation: events from a stopped agent's scope only update session metadata
  (so a `PermissionRequest` there cannot open a block that nothing would close); the
  stale sweep can wrongly stop a live agent silent for 12 h, and its later events are
  then ignored.
- `SubagentStart` links the agent to its Agent tool call: first to the oldest unclaimed
  `PostToolUse` of `Agent` with `tool_response_status` `async_launched`, which marks it
  `background`; otherwise to the latest pending `PreToolUse` of `Agent` that no agent
  has claimed. An `async_launched` that arrives after the start marks the linked agent
  `background`. With several launches in flight the pairing is by order, unverified. A
  stopped agent releases its Agent call, so a later start cannot claim it.
- `SessionEnd` stops the agents still running, with `stop_reason` `session_ended`.

**Blocks.** Each block has a `kind` (`permission` or `elicitation`), a `scope`,
`tool_name`, `tool_use_id`, `mcp_server_name`, `elicitation_id`, `opened_at`,
`notified_at`, `closed_at`, `close_reason` and `close_source` (`hook`, `telemetry` or
`sweep`).

- Opening:
    - `PermissionRequest` opens a permission block and borrows the `tool_use_id` of the
      latest pending `PreToolUse` with the same tool and scope that no open block links
      to, so parallel calls of one tool link one by one;
    - `Elicitation` opens an elicitation block, linked the same way to the latest
      pending call of the same MCP server in its scope;
    - `Notification` `permission_prompt` or `elicitation_dialog` sets `notified_at` on
      the oldest open, not yet notified block of that kind, preferring the
      notification's scope. When no block of that kind is open, it opens one with
      `notified_at` equal to `opened_at`, linked to the latest pending call of the scope
      (an MCP call for an elicitation): the request hook was missed;
    - by the maintainer's product decision **P1**, `idle_prompt` is not a block: it sets
      `idle_notified_at` on the session.
- Closing, at the event's receive time:
    - `PostToolUse` and `PostToolUseFailure` of the linked call close it as
      `tool_completed` or `tool_failed`; a block with no `tool_use_id` is closed by the
      same tool in its scope, or by any tool if it has no tool either;
    - `PermissionDenied` closes the same way as `denied` (unverified: it never fired in
      the capture);
    - progress in the same scope closes it as `superseded`: a `PreToolUse` of another
      call, `UserPromptSubmit`, `Stop`, `StopFailure`, `PreCompact` and `PostToolBatch`.
      A `PreToolUse` of a call already completed (async delivery) is not progress;
    - `ElicitationResult` closes the oldest matching elicitation block, by
      `elicitation_id` when both have one, else by MCP server, with reason
      `elicitation_accept`, `elicitation_decline` or `elicitation_cancel`, and
      `elicitation_other` for any other action, so reasons stay a bounded set;
    - `SubagentStop` closes the blocks of that agent's scope as `agent_stopped`;
    - `SessionEnd` closes every block as `session_ended`;
    - `close_block_denied(state, *, tool_use_id, at_ms)`, for the telemetry stage,
      closes the blocks of that call as `denied` with source `telemetry`, clamped to
      the opening.

**Pending tool calls.** `PreToolUse` adds a pending call unless its `tool_use_id`
already completed. The call leaves on its outcome (`PostToolUse`,
`PostToolUseFailure`, `PermissionDenied`), and every pending call of a scope leaves on
its `PostToolBatch`, on `Stop`, `StopFailure` and a manual `PreCompact` for the main
thread, when an agent stops, and on `SessionEnd`. That covers the calls denied in `-p`
and with Esc, which have no outcome.

**Other events**, unknown ones included, update only `last_event_at`, `last_seq` and
the metadata they carry (`prompt_id`, `permission_mode`, `effort_level`, `cwd`).

What the fixtures confirm: every rule above that names a fixture event, through the
replay of all eight hook fixtures. Unverified, from the hooks reference or by design:
resumed, cleared and forked sessions (and whether their `SessionStart` should reopen an
ended session), automatic compaction, `PermissionDenied`, `elicitation_id`, actions
other than `cancel`, a `Notification`, `UserPromptSubmit` or `Stop` inside a subagent,
nested subagents, concurrent background launches, parallel tool calls whose events
interleave, what happens to background agents at `SessionEnd`, a lost `PostCompact` or
a failed compaction, and a signal for an Esc that interrupts a response (the hooks
reference says `Stop` does not fire then).

### Consequences

- Good, because the session is `waiting_user` the moment `PermissionRequest` or
  `Elicitation` arrives, about 6 s before Claude Code's own notification.
- Good, because the reducer has no clock and no I/O: the fixtures replay in the unit
  tests, and the storage stage, the sweep and the telemetry join share one rule set.
- Good, because a missing `SessionStart`, out-of-order delivery, duplicate deliveries
  and a receive clock that goes backwards are all tolerated.
- Bad, because a dialog denied with Esc fires no hook: its block stays open, and the
  session `waiting_user`, until the next progress event or `close_block_denied`. In the
  `interactive-permission-compact` fixture that is 102.8 s, closed by `/compact`.
  Mitigated by the telemetry stage, which closes it from `tool_decision` with
  `source=user_reject`.
- Bad, because an Esc that interrupts a response also leaves the turn open: the hooks
  reference says `Stop` does not fire when the user interrupts (unverified in the
  fixtures), so the session shows `working` until the next prompt, `/exit`, a manual
  `/compact` or the stale sweep. Mitigated by the next prompt or event that ends the
  turn; a recapture should look for a signal of the interrupt, and the telemetry may
  provide one later.
- Bad, because a request denied in `-p` stays open until the next event of its scope
  (13 to 556 ms in the `headless-tools-subagent` fixture), so it is recorded as a short
  block, not as a denial.
- Bad, because block times are receive times, late by the hook's delivery delay, and
  durations are as exact as the app's clock.
- Bad, because at the 12-hour threshold (P3) a session whose process was killed, or an
  agent whose `SubagentStop` was lost, shows as live for up to 12 hours.
- Neutral, because `stale` needs a sweep, which a later stage of milestone 2 runs with
  the P3 threshold.

### Confirmation

- `tests/test_domain_sessions.py` has one test per rule above, synthetic cases for
  parallel calls of one tool, a session reopened after its end, compaction inside a
  turn, a lost `PostCompact`, a late `SessionStart`, `mark_stale` on sessions and
  agents, `close_block_denied`, receive times that go backwards, the `Notification`
  fallback, the bound on agents over 2000 turns and an event of another session, and
  replays every hook fixture, checking
  the sessions, agents and blocks each produces. The module stays at 100% coverage.
- `tests/test_domain_telemetry.py` and `tests/test_cli_light.py` keep
  `ccobs.domain.sessions` on the standard library (plus `ccobs.domain.hooks`) and free
  of I/O.
- The `reviewer` subagent checks that a change to these rules comes with a new ADR that
  supersedes this one.

## More information

- Claude Code [hooks reference](https://code.claude.com/docs/en/hooks): the events and
  their order.
- The storage of states, agents and blocks (ADR 0005), the sweep (with the P3
  threshold), the `/metrics` series and the telemetry join are later stages of
  milestone 2; the alert rules on `waiting_user` and `StopFailure` are milestone 5
  (ADR 0007).
- Revisit when a recapture (ADR 0012) shows resumed or cleared sessions, automatic
  compaction, `PermissionDenied`, parallel tool calls whose events interleave, or a
  timestamp in hook payloads.
