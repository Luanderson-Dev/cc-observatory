---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# The app database: metadata-only schema, Alembic at start-up, retention by last event

## Context and problem statement

[ADR 0005](0005-sqlite-for-app-state.md) puts the app's state in a SQLite file in WAL
mode, with SQLAlchemy 2 and Alembic, and leaves to milestone 2 the schema, when
migrations run and the retention defaults. Since then,
[ADR 0023](0023-per-event-allowlist-of-hook-metadata.md) has fixed the hook metadata the
app may keep, [ADR 0024](0024-session-agent-and-block-state-machine.md) has defined the
reducer whose effects the database stores, and
[ADR 0022](0022-the-host-label.md) has asked for a `host` label on database rows. The
reducer also keeps working state that no effect carries (pending tool calls, recently
completed calls, recently stopped agents, unclaimed background launches), and the app
must rebuild it after a restart, or a session in the middle of a turn would come back
wrong. What does the database hold, how does its schema reach a user's machine, how is
state rebuilt, and when are rows deleted?

## Decision drivers

- No content at rest: the schema itself must make storing a prompt or a tool output
  impossible without a reviewed change (ADRs 0006 and 0023).
- After a restart, the reducer continues exactly where it stopped: replaying the rest
  of a session gives the same rows as a replay with no restart.
- No manual step for users when the schema changes (ADR 0005).
- A crash never leaves an event half stored, and writes stay cheap: hooks arrive in
  bursts (CLAUDE.md: the endpoint enqueues, a background task persists).
- Disk stays bounded, with the same default history as Prometheus's metrics
  ([ADR 0016](0016-cardinality-and-retention.md)).
- `ccobs.domain` stays free of I/O, and the CLI never imports the database code.

## Considered options

For when migrations run:

- **Plain SQL scripts run at start-up.** No dependency, but the app would reimplement
  versioning, and SQLite's limited `ALTER TABLE` would make every script copy tables by
  hand.
- **Alembic, run in the app's lifespan at start-up.** Versioned, with batch mode for
  SQLite, and nothing for the user to run; a failed migration stops the app before it
  takes a request.
- **A separate migrate command** (`ccobs migrate` or a one-shot container). Explicit,
  but a user who upgrades the image and forgets it gets an app on an old schema, and
  compose would need an ordering between two services.

For state after a restart:

- **Rebuild from the tables only.** Not exact: the pending tool calls, the completed
  call IDs, the stopped agent IDs, the unclaimed launches and the order of the running
  agents are in no table, and losing them changes later decisions (a late `PreToolUse`
  would create a pending call again, a stopped agent's event would bring it back).
- **A `session_state` table for the working state no other table holds**, with the
  session, running agents and open blocks read from their own tables.
- **A full snapshot of `SessionState` as JSON.** Exact too, but it stores every running
  agent and open block twice, and the two copies could drift.

## Decision outcome

Chosen options: "Alembic, run in the app's lifespan at start-up" and "a `session_state`
table for the working state no other table holds", because the first needs no step from
the user and fails before the app takes a request, and the second rebuilds the exact
state with one copy of each record. This ADR extends ADR 0005, which stays in force.

**Schema** (`ccobs.server.db.models`, revision `0001`). Every table has `host`
(`CCOBS_HOST_LABEL`, ADR 0022), and every time is an integer of UTC milliseconds, the
receive time of ADR 0024.

- `sessions`, primary key `session_id`: the reducer's `Session` field for field
  (`status`, `started_at_ms`, `last_event_at_ms`, `last_seq`, `turn_open`, `compacting`,
  `source`, `model`, `permission_mode`, `effort_level`, `last_prompt_id`,
  `last_error_code`, `idle_notified_at_ms`, `ended_at_ms`, `end_reason`, `cwd`).
  Indexes on `status` and on `last_event_at_ms` (retention and the stale sweep).
- `agents`, primary key `(session_id, agent_id)`: every agent a session had, running or
  stopped, as the reducer's `Agent`.
- `blocks`, primary key `(session_id, block_id)`: every block, open or closed, as the
  reducer's `Block`, with `scope` stored as `scope_agent_id`. A partial index on
  `session_id` covers the open blocks only (`WHERE closed_at_ms IS NULL`).
- `hook_events`, primary key `seq`, assigned by the app in receive order: one row per
  hook event with `session_id`, `received_at_ms`, `hook_event_name`, `agent_id`,
  `agent_type`, `prompt_id`, `tool_name`, `tool_use_id`, `mcp_server_name`,
  `notification_type`, `elicitation_id`, `elicitation_action` (ElicitationResult's
  `action`), `compact_trigger`, `start_source` (SessionStart's `source`), `end_reason`
  (SessionEnd's `reason`), `error_code` (StopFailure's `error` code),
  `tool_response_status`, `duration_ms`, `is_interrupt`, `prompt_length`,
  `tool_input_bytes` and `tool_response_bytes`. Indexes on `(session_id, seq)` and on
  `received_at_ms`. It holds neither `cwd` nor `model`, which the session row holds, nor
  `background_tasks`, `permission_mode`, `effort_level`, `stop_hook_active` or `mode`.
- `session_state`, primary key `session_id`: JSON columns `agent_order` (the running
  agents' IDs in the order the state holds them), `pending_tools` (`tool_use_id`,
  `tool_name`, `scope`, `mcp_server_name`), `completed_tool_use_ids`,
  `stopped_agent_ids` and `async_launches`. Identifiers and tool names only, never a
  path: `cwd` is a column of `sessions`.
- `agents`, `blocks`, `hook_events` and `session_state` refer to `sessions` with
  `ON DELETE CASCADE`. Constraints follow a naming convention, so later migrations in
  batch mode can name them.

**No content columns.** Every column is a field that ADR 0023 keeps, or a value derived
from them by ADR 0024. Nothing stores a prompt, a tool input or output, a message, error
text or a path other than `cwd`. A column is a decision: adding one needs a new ADR (or
one that supersedes this) and an update of the test's column allowlist.

**Personal data at rest.** `sessions.cwd` contains the OS username, and `sessions.model`
on Bedrock can be an inference-profile ARN with the AWS account ID (ADR 0023). Both stay
in the database file on the app's named volume; the API and UI stages decide how to show
them, and no log line may contain them. The engine sets `hide_parameters`, so a failed
statement's error, which the writer logs, never shows its bound values, and no log line
of the database code names the file's path.

**Pragmas,** on every connection (`ccobs.server.db.engine`): `journal_mode=WAL`, so
readers do not block the writer (ADR 0005); `synchronous=NORMAL`, which in WAL mode
survives an app crash and can lose only the last commits on a power loss, never corrupt
the file; `foreign_keys=ON`, which SQLite leaves off and the cascades need; and
`busy_timeout=5000`, so a reader that meets the writer's lock waits up to 5 s instead
of failing. SQLite answers `journal_mode=WAL` with the mode it ended up in, which is not
`wal` where WAL cannot work (some network or shared filesystems). The app then keeps
running, logs a warning without the file's path, and `wal_enabled(engine)` reports it,
for the healthcheck and the metrics of later stages.

**Real transactions.** Python's `sqlite3` driver sends `BEGIN` only before DML, so DDL
and reads would run outside any transaction. The engine follows SQLAlchemy's pysqlite
recipe: the driver is in autocommit mode (`isolation_level = None`), and a `begin`
listener sends `BEGIN` for every SQLAlchemy transaction. So a migration's DDL rolls back
with it, and a read sees one snapshot of all the tables it reads.

**Migrations.** The Alembic environment is packaged in `ccobs.server.migrations`
(`env.py`, `versions/`, `script.py.mako`) and configured in code: no `alembic.ini` at
runtime, and `script_location` is the package's own directory, wherever it is
installed. `upgrade_to_head(engine)` runs every pending revision in one transaction
(a real one, DDL included, as above), so a revision that fails leaves the database as
it was; on an up-to-date database it is a no-op. The app's lifespan calls it before the
app accepts requests (stage 7 of milestone 2), so a migration that fails stops the app
at start-up with the error. `env.py` uses `render_as_batch`, as SQLite needs
(ADR 0005). New revisions are written by hand from the template, as
`versions/NNNN_<slug>.py`.

**Migrations run with foreign keys off.** A batch migration copies a table and drops the
original; with foreign keys on, dropping `sessions` would cascade-delete every agent,
block, event and state row. `upgrade_to_head` therefore migrates on an engine of its own
whose connections set `foreign_keys=OFF` when they open, before any transaction starts
(SQLite ignores the pragma inside one). Before the commit it runs `PRAGMA
foreign_key_check`, and any violation rolls the whole upgrade back, with an error that
gives only the count. The app's own engine keeps foreign keys on.

**One writer, one transaction per event or batch.** Only the app's background writer
writes (ADR 0005, CLAUDE.md); the API reads. For each reducer call,
`Repository.apply_effects` inserts the `hook_events` row (none for the sweep and the
telemetry join, which have no hook event), upserts the session from the new state, each
agent and block from the effects, and the `session_state` row, in one transaction.
`apply_batch` does the same for several calls in one transaction, so the writer can
micro-batch; within a batch each record is written once, in its last version. If any
statement fails, nothing of the batch is stored. Upserts use SQLite's `INSERT ... ON
CONFLICT DO UPDATE`.

**State after a restart.** `load_open_states()` rebuilds the `SessionState` of every
session that has not ended (stale ones included): the session from `sessions`, the
running agents from `agents` in `agent_order`, the open blocks from `blocks` in
`block_id` order (the order the reducer opens them in), and the rest from
`session_state`, all in one read transaction. `load_state(session_id)` does the same for
one session, ended or not, since an ended session can reopen (ADR 0024). `max_seq()`
gives the highest `seq`, so the app continues numbering after it. Because the reducer
ignores an event whose `seq` is not above `last_seq`, events are never applied twice.
The rows `apply_batch` writes are always consistent. If they ever are not (a file edited
by hand, a bug), a load still never fails: a session without its `session_state` row
gets empty bookkeeping, an ID in `agent_order` without a running agent row is dropped,
and a running agent missing from `agent_order` joins at the end. Each skipped row is
counted in `Repository.skipped_rows` and logged as a count, never with values.

**Retention.** `CCOBS_APP_RETENTION`, default `90d` (the metrics' default, ADR 0016),
at least `1d`, at most `100y`, and never shorter than the stale threshold, or rows of
sessions still open would be deleted; the settings refuse such a value at start-up.
`delete_older_than(cutoff_ms)` deletes the `ended` and `stale` sessions whose
`last_event_at_ms` is before the cutoff, and the cascade takes their agents, blocks,
events and state. A session still open is never deleted, so the writer's in-memory
state never outlives its rows. That loses nothing: retention is never shorter than the
stale threshold, so by the cutoff the sweep has made every silent session stale. The
stage that schedules it (milestone 2) passes `now - retention`.

**Stale threshold.** ADR 0024 describes the sweep's threshold as fixed at 12 hours (the
maintainer's product decision P3). It is configurable as `CCOBS_SESSION_STALE_AFTER`,
default `12h`, the P3 value, with a floor of `1h`, below which live sessions waiting on a
long tool call or a person would be marked stale. ADR 0024 stays as it is; this ADR
records the setting.

### Consequences

- Good, because the schema cannot hold content: a new column needs a test change and an
  ADR, and the stored rows are exactly the reducer's records.
- Good, because a restart is invisible: the rebuilt state equals the in-memory one at
  every point of every fixture, and continuing gives the same rows as no restart.
- Good, because upgrades need nothing from the user, and a broken migration fails at
  start-up instead of on the first hook.
- Good, because one transaction per batch keeps the database consistent after a crash
  and amortises the commit over bursts of hooks.
- Bad, because a long migration on a large database delays the app's start, and
  Claude Code's hooks get hook-error notices meanwhile (ADR 0011); revisions that copy
  large tables must say so in their changelog line.
- Bad, because `session_state` must change with the reducer: a new field of
  `SessionState` needs a migration and a test, or it is lost on restart.
- Bad, because `cwd` and Bedrock `model` ARNs are personal data in the database file,
  which a backup of the volume copies.
- Bad, because a downgrade of the app image after a migration is not supported: the
  older app does not know the newer revision.
- Neutral, because the alerts and the user's settings of ADR 0005 get their tables in
  the milestones that bring them (milestone 5 for alerts).

### Confirmation

- `tests/server/test_db.py` checks the pragmas on every connection (WAL, foreign keys,
  busy timeout, synchronous), the warning and `wal_enabled` when the journal mode is not
  WAL, the driver's autocommit mode, that DDL rolls back with its transaction, that a
  read transaction sees one snapshot, and that errors hide bound values; that
  `upgrade_to_head` on an empty file leaves no difference between Alembic's
  `compare_metadata` and the models, that a second run is a no-op, that the initial
  revision downgrades to an empty database, that a revision failing partway leaves no
  table, that a batch copy of `sessions` keeps every child row (foreign keys off), and
  that a foreign key violation rolls the upgrade back; that no `alembic.ini` is needed; every column of every table against an explicit allowlist,
  and no column named like content; `host` on every table; the cascades and the
  indexes; and that a wheel built with `uv build` ships the migrations.
- `tests/server/test_repository.py` replays every hook fixture through `parse_hook`,
  `apply` and `apply_effects`, checks the rows against the reducer's records, rebuilds
  the state from a new engine before every event and checks that it equals the
  in-memory state and that the final tables equal those of an uninterrupted replay; and
  covers the sweep and the telemetry join without a hook event, coalescing in a batch, a
  failing batch that stores nothing, a failed write whose error hides its values,
  `max_seq`, retention of closed sessions only, with its cascade, each kind of
  inconsistent row a load skips and counts, `host` on every row, the event rows of each
  event type, and a canary planted in the never-kept fields of the fixtures and the
  hooks reference that never reaches the database file.
- `tests/test_cli_light.py` keeps `ccobs.server` out of the CLI's imports.
- The `reviewer` subagent checks that a new column or table comes with an ADR.

## More information

- SQLite: [WAL](https://sqlite.org/wal.html),
  [pragmas](https://sqlite.org/pragma.html) (`synchronous`, `foreign_keys`,
  `busy_timeout`), [UPSERT](https://sqlite.org/lang_upsert.html) and
  [partial indexes](https://sqlite.org/partialindex.html).
- SQLAlchemy: [SQLite dialect](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)
  (`insert().on_conflict_do_update`, `sqlite_where`, foreign key support).
- Alembic: [batch migrations](https://alembic.sqlalchemy.org/en/latest/batch.html) and
  [autogenerate's `compare_metadata`](https://alembic.sqlalchemy.org/en/latest/api/autogenerate.html).
- The writer, the lifespan that runs the migrations, the sweep and the retention job are
  later stages of milestone 2.
- Revisit if central mode is planned (ADR 0001), if the content opt-in of milestone 4
  needs columns, or if start-up migrations become too slow for large databases.
