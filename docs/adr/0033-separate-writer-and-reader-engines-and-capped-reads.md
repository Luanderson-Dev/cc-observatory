---
status: accepted
date: 2026-10-16
decision-makers: Luanderson-Dev (maintainer)
---

# Separate writer and reader engines, capped reads and bounded session detail

## Context and problem statement

[ADR 0026](0026-app-database-schema-migrations-and-retention.md) gives the app one writer
thread, and [ADR 0027](0027-the-app-http-surface-and-hook-queue.md) has the hook route
only enqueue, so a slow database never slows Claude Code. Up to v0.3.0, though, one
SQLAlchemy engine (its default pool: 5 connections plus 10 of overflow) served both the
writer thread and the read-only JSON API, whose sync `def` routes ran on Starlette's
default threadpool of 40 threads. Fifteen slow reads in flight held every connection,
the writer waited up to the pool's 30 s timeout for one, and meanwhile the hook queue
filled and hooks were dropped. Milestone 3 adds the web UI and server-sent events, which
read the database more often and for longer, and the session detail returned every agent
and block a session ever had, so one long session made one read large. How does the app
keep reads from starving the writer, and keep each read bounded?

## Decision drivers

- The writer always gets a connection, however many reads are in flight: a stalled
  writer fills the hook queue and drops hooks (ADR 0027).
- Reads cannot write: only the writer writes (ADR 0026).
- Reads never take the default threadpool's threads, which Starlette's sync routes and
  file responses share, and never block the event loop that serves the hook route.
- Overload gets a fast, neutral answer that a client can retry, never a request that
  hangs for 30 s or an error that quotes a traceback or a request value (ADR 0006).
- Each read costs a bounded amount, whatever the size of the session.
- No new package and no change to the schema: AnyIO, already in the lock through
  Starlette, is only declared in the `server` extra, since the read gate imports it.

## Considered options

For the connections and the threads:

- **One engine with a larger pool.** Simple, but any fixed size is reached by enough
  readers, and the writer still competes with them for a connection.
- **Async SQLAlchemy over `aiosqlite`.** Reads leave the threadpool, but `aiosqlite` runs
  each connection on a thread anyway, the repository and Alembic are sync (ADR 0027
  already turned this down for the writer), and nothing caps reads.
- **Two engines on the same file, a capped read gate and async routes.** The writer's
  engine has one connection that only the writer uses; the readers' engine has a few
  query-only connections; the API's routes are `async def` and run each read on a worker
  thread of their own, capped at the readers' connections, answering 503 when a read
  waits too long for one.

For the session detail and its lists:

- **Return everything, as in v0.3.0.** A long session makes one read and one response as
  large as its history.
- **`limit` and `offset`.** Bounded pages, but SQLite still walks the skipped rows, and
  rows inserted while a client pages shift every later page.
- **A bounded detail and keyset paging with `before` and `after` cursors.** Each page is
  an index range scan from a cursor, whatever its depth, and new rows never shift a page
  a client already has.

## Decision outcome

Chosen options: "two engines on the same file, a capped read gate and async routes" and
"a bounded detail and keyset paging with `before` and `after` cursors", because they
give the writer a connection no reader can take, keep reads off the default threadpool,
make overload a quick 503, and bound each read by a page size instead of a session's
history. This ADR extends ADRs 0026 and 0027, which stay in force.

**Two engines** (`ccobs.server.db.engine.create_engine_for(path, role=...)`), on the same
SQLite file, with the same pragmas, `BEGIN` listener and `hide_parameters` as ADR 0026:

- the **writer's**, `role="writer"`: a pool of exactly 1 connection with no overflow. The
  lifespan reads the start-up state on it (`max_seq`, the open sessions), then only the
  writer thread uses it. Migrations keep running first, on their own engine with foreign
  keys off (ADR 0026), before either engine serves anything;
- the **readers'**, `role="reader"`: a pool of exactly `READER_POOL_SIZE` (4) connections
  with no overflow, each set to `PRAGMA query_only=ON` after the other pragmas, so SQLite
  refuses any `INSERT`, `UPDATE`, `DELETE` or DDL on it with "attempt to write a
  readonly database". WAL lets these reads run alongside the writer's commits
  ([ADR 0005](0005-sqlite-for-app-state.md)).

The app always creates the readers' engine itself, on the writer's database file, once
the migrations have run, and disposes both at shutdown after the writer has stopped. The
API reads through `app.state.read_repository`; `app.state.repository` stays the
writer's.

**The read gate** (`ccobs.server.reads.ReadGate`, one per app, built in the lifespan on
the event loop that serves requests). The API's routes and their dependencies are
`async def`, so FastAPI never runs them on its default threadpool. Each database read
goes through `ReadGate.run(read, *args)`:

- it waits for one of `READ_SLOTS` slots, as many as the readers' connections, for at
  most `READ_WAIT_TIMEOUT_S` (5 s). A read that gets no slot in time answers **503 with
  `{"detail": "database busy"}`**: no traceback, nothing from the request;
- the slot held, it runs the sync read on a worker thread through
  `anyio.to_thread.run_sync`, with a capacity limiter of its own (as many threads as
  slots, which a slot holder never waits for), never the default limiter;
- **the timeout bounds only the wait for a slot, not the read.** A sync database call
  cannot be stopped safely from outside, so once its thread starts the read runs to its
  end, even if the request is cancelled (the client went away, or shutdown): anyio
  shields the thread (`abandon_on_cancel=False`), and the slot is released only when the
  thread returns. The slots in use then always match the reader connections in use, so
  a read holding a slot never waits for a connection, and a cancelled request never lets
  a new read start on a connection that is still busy. A read still ends: SQLite's busy
  timeout bounds each lock wait (ADR 0026), and every query is bounded by a page size;
- one request takes one slot at a time: a route that needs two queries (the session
  check and the events) runs both in one call on the gate.

**Bounded session detail and keyset paging**: the session detail no longer returns every
agent and block a session ever had. It holds every running agent and open block (what
the session is doing or waiting on now), plus the latest `DETAIL_CLOSED_LIMIT` (50)
stopped agents and closed blocks, and `agents_total` and `blocks_total`, the counts of
all stored. The lists that grow with a session's history page by **keyset**, never by
`OFFSET`, on routes of their own (`/agents`, `/blocks`, `/events` under a session), each
with `limit` (default 50, from 1 to 500):

- **blocks** page on `block_id`: the `seq` of the hook that opened the block, so it never
  changes and grows with every new block, and a page is a range of the `blocks` primary
  key `(session_id, block_id)`;
- **agents** have no monotonic column, so they page on (when the agent was first seen,
  `agent_id`): `started_at_ms`, or `stopped_at_ms` for an internal agent, which is stored
  already stopped and never starts. That value is set when the row is created (with one
  exception, under consequences). The cursor is the `agent_id` of a row, and the page starts below that row's
  position; a cursor that names no agent of the session is a 422. The read is a range of
  the `agents` primary key on `session_id`, sorted in memory: a session's agents are far
  fewer than its hooks;
- **events** page on `seq`, from the `(session_id, seq)` index of ADR 0026, with
  `before_seq` or `after_seq`.

A page is asked for with `before` (`before_seq`), the rows just older than a cursor, or,
for events, `after_seq`, the rows just newer than it, which is how the UI tails a
session; cursors are exclusive, and `before_seq` and `after_seq` are not combined in one
request (422). Without a cursor a route returns the latest `limit` rows, as `/events` did
in v0.3.0. Every page, and the detail's lists, are in the lists' order of v0.3.0, oldest
first (agents now by when they were first seen instead of by their last event, which
changes as an agent runs and would move it between pages). An invalid cursor (not an
integer from 0 to 2^63 - 1, or an `agent_id` that the hook parser would refuse) is a 422
that does not echo the value, like every validation error of the API (ADR 0027). Agents
also carry `parent_agent_id`, worked out from stored rows with no new column: the
`agent_id` of the `PreToolUse` row (ADR 0023) whose `tool_use_id` is the agent's
launching call, `null` for the main thread or when that row is missing.

### Consequences

- Good, because no number of readers can take the writer's connection: with 40 reads in
  flight, every reader connection held, the writer still commits its batch at once.
- Good, because a reader connection cannot write, even through a bug: SQLite refuses it.
- Good, because reads never take a thread of the default threadpool nor block the event
  loop, and overload answers in at most 5 s with a neutral 503 instead of hanging.
- Good, because what a read returns is bounded by its page size, whatever the session's
  length, blocks and events are read as index ranges, and keyset pages stay stable while
  new hooks arrive.
- Bad, because two parts of a read still grow with the session rather than the page: the
  agents are sorted in memory over the session's agents, and `parent_agent_id` scans the
  session's hook events for the launching `PreToolUse` rows, since no index covers
  `tool_use_id`. On a synthetic session with 200,000 hooks and 2,000 agents, the detail
  and an agents page took about 60 ms each. An index on
  `hook_events (session_id, tool_use_id)` would make the lookup a seek, at the cost of a
  migration.
- Bad, because an agent's position can still move once: the reducer remembers only the
  last 64 stopped agent IDs of a session, so a late event of a stopped agent it has
  forgotten builds a fresh running `Agent`, and its upsert replaces the stored
  `started_at_ms` and `tool_use_id`. That agent then jumps to the newest page (a client
  paging back may see it twice or miss it) and may lose its `parent_agent_id`. It needs
  more than 64 agents to stop and a hook to arrive late, so it is rare; the follow-up is
  for the upsert to keep a stored `started_at_ms` and `tool_use_id`.
- Bad, because at most 4 reads run at once and a fifth waits; for one person's browser
  tabs on one machine that is ample, and the 503 makes overload visible rather than slow.
- Bad, because a read holds its slot until its query ends even when its client has gone;
  bounded queries keep that short. At shutdown, uvicorn cancels requests still running
  after its 5 s grace period, so a read can still hold a reader connection when the
  lifespan disposes the readers' engine; that is harmless, since `dispose()` leaves
  checked-out connections alone and they close when the read returns them.
- Bad, because the session detail and the events need more than one request for a long
  history; the UI pages on demand.
- Neutral, because a third role (a second writer, say) is not allowed: ADR 0026 keeps one
  writer, and `create_engine_for` takes only these two roles.

### Confirmation

- `tests/server/test_db.py` checks that the writer's pool is 1 with no overflow and the
  readers' is 4 with no overflow, that a reader connection sets the same pragmas plus
  `query_only` on every new connection, keeps real transactions and hidden parameters,
  and refuses `INSERT`, `UPDATE`, `DELETE`, `CREATE` and `DROP`.
- `tests/server/test_reads.py` checks that `READ_SLOTS` matches `READER_POOL_SIZE`, that a
  read runs on another thread and never waits on the default thread limiter, that a read
  that finds no slot gets the 503 after the timeout and the slot works again afterwards,
  that a read longer than the timeout still returns, and that a cancelled request keeps
  its slot until its read ends.
- `tests/server/test_api.py` checks that every route and dependency is `async def`, that
  the API reads through the query-only engine, that shutdown disposes both engines, that
  a failed migration opens no reader engine, that the writer commits within 1 s while 40
  reads hold every reader connection, and that a read with no free slot gets the neutral
  503 on every route; blocked reads wait on an event, not a sleep, so the test does not
  depend on the machine's speed. It also checks that a session with 500 blocks gets its
  open blocks and the latest 50 closed ones with the right totals, that paging agents,
  blocks and events to the end returns every row once, in order, that `after_seq`
  returns only newer events, that `before_seq` with `after_seq` and invalid cursors are
  422s that do not echo the value, that each request takes exactly one read slot (with a
  gate of one slot), and that an agent names the agent that launched it.
  `tests/server/test_repository.py` checks the same reads below the API.
- The `reviewer` subagent checks that no API route is a sync `def`, that every database
  read of the API goes through the read gate, and that no list query uses `OFFSET`.

## More information

- SQLite: [`PRAGMA query_only`](https://sqlite.org/pragma.html#pragma_query_only),
  [WAL](https://sqlite.org/wal.html).
- SQLAlchemy: [connection pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
  (`pool_size`, `max_overflow`).
- AnyIO: [running sync code in worker threads](https://anyio.readthedocs.io/en/stable/threads.html)
  and [capacity limiters](https://anyio.readthedocs.io/en/stable/synchronization.html#capacity-limiters).
- FastAPI: [concurrency and `async def`](https://fastapi.tiangolo.com/async/).
- Keyset pagination: [use the index, Luke](https://use-the-index-luke.com/no-offset).
- Revisit if reads routinely meet the 503, if the UI needs more than 4 reads at once, or
  if the app ever needs more than one writer. If the detail or the agents pages show up
  as 503s or as slow reads under the UI's live refetches (the server-sent events stage
  coalesces them), add the partial index
  `hook_events (session_id, tool_use_id) WHERE hook_event_name = 'PreToolUse'` with a
  migration. If revived agents show up in practice, make the agents upsert keep the
  stored `started_at_ms` and `tool_use_id`.
