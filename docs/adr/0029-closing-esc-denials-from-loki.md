---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# Permission blocks denied with Esc are closed from Loki's `tool_decision` rejects

## Context and problem statement

A permission dialog denied with Esc fires no closing hook: no `PostToolUse`, no
`PermissionDenied`, no `Stop` ([ADR 0012](0012-observed-telemetry-and-hook-contract.md)).
The hook-driven reducer of [ADR 0024](0024-session-agent-and-block-state-machine.md)
therefore keeps the block open, and the session `waiting_user`, until the next progress
event of its scope. In the `interactive-permission-compact` fixture that is 102.8 s,
closed only by `/compact`. ADR 0024 left an entry point for this,
`close_block_denied(state, *, tool_use_id, at_ms)`, which closes the blocks of a call as
`denied` with source `telemetry`, and the writer already accepts it on its control
channel (`Writer.submit_telemetry_close`).

Telemetry does see the denial. In the same fixture, Claude Code exported a
`claude_code.tool_decision` record with `decision=reject`, `source=user_reject`, the
`session.id` of the session and the `tool_use_id` of the denied call
(`toolu_01EXAMPLE000000000000023`), the same call ID that the `PreToolUse` before the
`PermissionRequest` carried, and that the block borrowed (ADR 0024). The record reaches
Loki through the collector, with its attributes as structured metadata
([ADR 0015](0015-loki-through-its-otlp-endpoint.md)). ADR 0015 also left a decision to
this milestone: Loki logs every query, with its LogQL text, at `info`, so queries with
session IDs would put them in Loki's log. How does the app learn of an Esc denial, and
what does it ask Loki?

## Decision drivers

- `waiting_user` must mean a person is being waited on: milestone 5 alerts on it
  ([ADR 0007](0007-alerts-browser-and-discord.md)), and a false one would page people.
- No session ID in Loki's log, and no content read back (ADR 0006).
- Nothing new for the user to configure, and no new failure for Claude Code: the app
  must keep working, as it does now, with telemetry off or Loki down.
- Bounded cost: a laptop runs the stack for months; queries only when they can matter,
  over a bounded time range (ADR 0015).
- Every write on the writer's thread ([ADR 0026](0026-app-database-schema-migrations-and-retention.md)),
  and the app's metrics at a fixed cardinality
  ([ADR 0028](0028-app-metrics-and-their-listener.md)).

## Considered options

- **(a) Close only on the next hook.** No new code, but a denied dialog leaves a false
  `waiting_user` for as long as the person does something else (the 102.8 s of the
  fixture, or hours), a false open block on `ccobs_blocks_open`, a wrong duration in the
  histogram, and false alerts in milestone 5.
- **(b) Poll Loki while permission blocks are open.** A small background thread and one
  query every few seconds, only while a permission block is open; the block closes a few
  seconds after the denial, at the denial's own time.
- **(c) Defer to milestone 3.** The UI could query Loki when a session is shown, but the
  metrics and the alerts of milestone 5 do not go through the UI, so they would still see
  the false wait.

## Decision outcome

Chosen option: "(b) poll Loki while permission blocks are open", because it is the only
option that keeps `waiting_user`, the block metrics and the future alerts true, and it
costs no query at all while no permission block is open. `ccobs.domain.denials` holds the
pure half (the query, the window, the parser and the matching) and
`ccobs.server.reconcile` the polling thread.

- **The query** is exactly
  `{service_name="claude-code", host="<host>"} | event_name="tool_decision" | decision="reject"`:
  the host's stream (index labels `service_name` and `host`, ADRs 0015 and
  [0022](0022-the-host-label.md)), filtered on the structured metadata `event_name` and
  `decision`. The host value is quoted with the PromQL and LogQL escaping of
  `ccobs.domain.telemetry` (`quote`), although the settings already restrict it to
  `HOST_VALUE_PATTERN`. It carries **no session ID**: one query covers every open block
  of the host, and the matching happens in the app.
- **Loki stays at the `info` log level.** ADR 0015 asked this milestone to decide whether
  to lower it. The app's only query has no session or call ID, so the query log holds the
  host label and the time range, nothing personal; `info` keeps Loki's own operational
  messages for troubleshooting. Revisit when a query has to filter on a session (the
  milestone 3 session view).
- **Only due blocks.** Every `2 s` (the poll interval), the reconciler reads the writer's
  open permission blocks that have a `tool_use_id` (`Writer.open_permission_blocks`,
  under the writer's lock). A block without one (a `PermissionRequest` with no pending
  call to borrow from) cannot be matched and is left to the hooks. It keeps those open for
  at least `2 s` (the grace period), which leaves an answer at the keyboard to its
  closing hook, and at most `15 min` (the horizon), which bounds the query's window;
  a block older than that is left to the hooks and the stale sweep. With no due block, it
  sends **no query**.
- **The window.** One `GET /loki/api/v1/query_range` with `start` at the oldest due
  block's opening minus `10 min` and `end` at now plus `10 min`, in nanoseconds, so at
  most about the horizon plus twice the margin (35 min); `limit=1000`;
  `direction=backward`, so that a full answer drops the oldest rejects rather than the
  newest; the `X-Loki-Response-Encoding-Flags: categorize-labels` header (ADR 0015); and
  a 5 s timeout per phase (connecting, and each read or write), not a cap on the whole
  request. The answer is read as a stream and given up past 8 MiB (an `error`, logged as
  "answer too large"): 1000 entries fit well within, since the largest fixture record has
  about 1.3 KB of attributes (ADR 0015). The margin covers a clock difference between
  the app's container, whose clock dates the block's opening, and the machine that
  stamps Claude Code's events (Docker Desktop runs containers in a VM whose clock can
  drift); it widens `end` too, since an event stamped by a clock ahead of the
  container's would otherwise fall after now. A full answer is logged as a warning once
  each time the answers become full, not on every poll.
- **Matching** is on the pair `session_id` and `tool_use_id`: a call ID alone is unique
  in practice, but the pair keeps one session's telemetry from ever closing another's
  block. For each open block whose pair a reject names, the reconciler submits
  `submit_telemetry_close(session_id, tool_use_id, at_ms)`, and the writer applies
  `close_block_denied` on its own thread (ADR 0026). A close that arrives after the
  block was closed some other way changes nothing. Any reject closes the block, whatever
  its `source`: `user_reject` for Esc, and `config` for a call denied by settings in `-p`,
  whose block the next event of its scope usually closes first (ADR 0012).
- **The event time** is Loki's entry timestamp, in nanoseconds, cut to milliseconds, not
  the `event_timestamp` structured metadata: Loki stores an OTLP record at its
  `timeUnixNano`, which on both `tool_decision` records of the fixtures is the same
  millisecond as `event.timestamp`; the entry timestamp is always there and numeric, and
  it is what `start` and `end` bound. The close time is that time clamped to
  `[opened_at, now]` (`match_denials`), so a clock difference can neither close a block
  before it opened nor in the future; the earliest reject of a call wins.
- **Parsing never raises.** An answer that is not a successful `streams` result counts as
  an error. A malformed stream or entry (no timestamp, a timestamp that is not decimal
  nanoseconds, no `session_id`, `tool_use_id`, `event_name` or `decision`) is skipped,
  and the count is logged; an entry of another event or decision is ignored. Structured
  metadata merged into the stream labels, as Loki returns it without
  `categorize-labels`, is read too.
- **Back-off.** A connection error, a timeout, a non-2xx status, a body that is not JSON
  or not a streams result makes the next wait `2 s`, doubling to at most `60 s`, until a
  round succeeds or has nothing to ask. Logs hold the exception type or the status code
  only: never the URL with its query, a session or call ID, or a response body. httpx's
  own request log (with the URL) is at `info`, below the `WARNING` the root logger keeps
  for libraries.
- **Degradation.** With `CCOBS_TELEMETRY_RECONCILE=false` the reconciler does not start.
  With Claude Code's telemetry off, Loki holds no rejects: queries come back empty while
  a block is open, and blocks close by hooks as before (option (a)'s behaviour). With
  Loki down, the queries fail with back-off, and the same holds. Claude Code is never
  involved: the reconciler only reads Loki and writes the app's database.
- **Thread and lifecycle.** A daemon thread, like the sweeper: a stop `Event` that
  interrupts the wait at once, an injectable clock (the hook route's) and wait, and one
  `httpx.Client` per reconciler (no proxy from the environment, no redirects), closed on
  stop. `stop` does not cut short a query in flight; the lifespan waits up to 5 s for the
  thread, then moves on with a warning. The lifespan starts it after the sweeper and stops it after the metrics listener,
  before the sweeper and the writer. `create_app(..., reconcile=False)` leaves it off for
  tests.
- **A new series**, extending ADR 0028: `ccobs_loki_queries_total{host, result}`, a
  counter of the queries sent, with `result` in the closed list `ok` and `error`, both
  always exported, from 0. A round with nothing to ask sends nothing and counts nothing,
  so the counter shows Loki's health only while it matters. The app now exports 65
  series per host (ADR 0028's 63, plus these 2).

### Consequences

- Good, because a dialog denied with Esc stops showing as `waiting_user` within about the
  grace period plus the poll interval plus Claude Code's log export interval (1 s in the
  capture, 5 s by default) and Loki's ingestion, instead of until the next prompt; and the
  block's duration is the real one, since it closes at the event's time.
- Good, because no query is sent while no permission block is open, and none carries a
  session ID.
- Good, because the reconciler degrades to today's behaviour when telemetry, Loki or the
  setting is off, and the counter shows when Loki fails.
- Bad, because of the horizon: a reject exported more than 15 min after its block opened
  is ignored, and that block then waits for the next hook of its scope or the stale sweep
  (12 h, ADR 0024), as without this ADR. In exchange, every query, sent every 2 s while a
  due block is open, reads at most about 35 min of the host's stream (the horizon plus
  twice the margin; ADR 0015 notes that a query reads every chunk in its range), however
  long a block stays open.
- Bad, because with telemetry off or Loki down, queries keep going (with back-off when
  they fail) for up to the horizon after each permission block opens, for nothing.
- Bad, because the close depends on Claude Code exporting the event: a session killed
  before its next export never sends the reject, and its block waits for the next hook,
  the end of the session or the stale sweep.
- Neutral, because a `PermissionDenied` hook (registered, never fired in the capture,
  ADR 0024) would close the block first when it comes; the reject then changes nothing.

### Confirmation

- `tests/test_domain_denials.py` builds Loki answers from the fixture's real
  `tool_decision` records, in the shape `tests/integration/test_stack_logs.py` reads
  with `categorize-labels`, and checks the exact query, its escaping and that it holds
  no session ID; the due blocks and the window; the parser on the fixture's reject, on
  merged labels, on empty and invalid answers and on every kind of malformed entry; and
  the matching on both IDs with the clamping. It also checks, in the fixture, that the
  reject carries `tool_use_id` and that each record's time equals its `event.timestamp`.
- `tests/server/test_reconcile.py` checks, with an `httpx.MockTransport`, the request
  (path, query, bounded `start` and `end`, `limit`, `direction`, the header, the
  timeout, no session ID); no query without due blocks; the back-off sequence and its
  recovery; the horizon (no query for blocks past it, a window from the due blocks
  only); `direction=backward` and the limit warning once per transition; an answer over
  8 MiB, with and without a length, counted as an error; errors of every kind counted and
  logged without URL, IDs or body; a prompt
  stop that closes the client; and, end to end through the real app and writer with the
  hook fixture up to the Esc-denied `PermissionRequest`, the block closed as `denied`
  from `telemetry` at the event's time in the API, the series counted, and the lifespan
  order. The reconciler is off with `telemetry_reconcile=False`.
- `tests/test_domain_app_metrics.py` and `tests/server/test_metrics.py` check the new
  series, its zeros and the total of 65.
- Unverified until the integration test of stage 14 of milestone 2 runs against the
  real Loki 3.7.8: that the `tool_use_id` attribute comes back as structured metadata
  under that name (ADR 0015 observed `decision` and the rule for other attributes, but
  its integration test does not list `tool_use_id`); that Loki's entry timestamp is the
  record's `timeUnixNano`; that the query above selects the replayed reject and only
  rejects; that Loki 3.7.8 accepts an `end` up to 10 min in the future without clamping
  or rejecting it; and the end-to-end delay from the denial to the close.
- Also unverified: that every Esc denial produces a reject carrying its `tool_use_id`
  (the capture has one), and the delay between the Esc and the event's export.

## More information

- Loki: [`query_range`](https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time)
  and the `categorize-labels` encoding flag,
  [structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/)
  and [LogQL label filters](https://grafana.com/docs/loki/latest/query/log_queries/#label-filter-expression).
- httpx: [timeouts](https://www.python-httpx.org/advanced/timeouts/) and
  [`MockTransport`](https://www.python-httpx.org/advanced/transports/#mock-transports).
- Revisit if Claude Code adds a hook for an Esc denial (the reducer would then close the
  block and this would become redundant), if milestone 3's session view needs queries
  with session IDs (and so Loki's log level), or if the poll interval or window prove
  costly on a long-running stack.
