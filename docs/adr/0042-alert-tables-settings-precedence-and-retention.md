---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# Alert tables, settings saved over the environment, and their retention

## Context and problem statement

[ADR 0005](0005-sqlite-for-app-state.md) puts alerts and the user's settings in the
app's SQLite file, and [ADR 0026](0026-app-database-schema-migrations-and-retention.md)
fixed the schema of milestone 2, its migrations and its retention, leaving the alert
tables to milestone 5. [ADR 0041](0041-alert-rules-defaults-deduplication-and-evaluation.md)
defines the rules, their keys `(rule, subject)`, episodes and the cooldown; the
evaluator's memory must survive a restart, or an alert would fire again right after it.
The maintainer decided (P3, 2026-10-20) that a value saved on the settings screen
overrides the environment's, and that Reset goes back to the environment's value. What
do the alert tables hold, how does a saved setting combine with `CCOBS_ALERT_*`, how is
the evaluator's state rebuilt, and when are alert rows deleted?

## Decision drivers

- No content and no personal data at rest beyond what ADR 0026 already keeps: an alert
  never stores a project name, a path or a message (ADRs 0006 and 0023).
- A restart never fires an alert twice and never forgets one that fires (ADR 0041).
- One writer (ADR 0026); the API reads on the readers' engine with capped reads
  ([ADR 0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)).
- A saved setting round-trips exactly: a switch stays a bool, seconds an int and a dollar
  limit the number typed.
- Disk stays bounded by the same retention as sessions.
- The `host` label on every row ([ADR 0022](0022-the-host-label.md)).

## Considered options

For the settings:

- **One row per overridden field, its value as JSON text.** Only what the user changed
  is stored; Reset deletes the row. JSON keeps `true`, `30` and `30.0` apart.
- **One row per host with a column per setting.** Typed columns, but every new setting
  needs a migration, and "not overridden" needs a null per column.
- **A numeric column.** SQLite's affinity stores `10.0` as `10` and `true` as `1`, so
  the type would not round-trip.

For the evaluator's state after a restart:

- **Rebuild it from the alert rows and the hook events.** No new table: the keys come
  from the alerts, and the failure windows from the `StopFailure` rows that
  `hook_events` already holds.
- **A table for the evaluator's state.** Exact, but it duplicates the alerts and must
  change with `AlertState`.

## Decision outcome

Chosen options: "one row per overridden field, its value as JSON text" and "rebuild it
from the alert rows and the hook events", because they store only what the user chose,
round-trip each type exactly, and add no copy of state that other rows already hold.
This ADR extends ADR 0005 and ADR 0026, which stay in force. The table and column names,
the delivery states and the precedence rule below were set by the implementer of stage
5 of milestone 5 on the main context's brief; precedence itself is the maintainer's
decision P3.

**Tables** (revision `0002`, `ccobs.server.db.models`). Times are UTC milliseconds.

- `alerts`, one row per alert that fired: `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`, so
  an ID is never reused and a page cursor never meets another alert), `host`, `rule`,
  `session_id`, `subject`, `fired_at_ms`, `cleared_at_ms`, `value` and `threshold`
  (`REAL`, in the rule's unit), `discord_state`, `discord_attempts` (default 0) and
  `discord_next_at_ms`.
    - `session_id` refers to `sessions` with `ON DELETE CASCADE`; it is null for
      `daily_cost`, whose subject is `day`.
    - CHECK constraints: `rule` is one of ADR 0041's four rules; `discord_state` is one
      of the delivery states below; the subject is the session's ID
      (`subject = session_id`) for every rule but `daily_cost`, and `day` with no
      session for `daily_cost`, so it can never hold free text; a `pending` delivery
      has a `discord_next_at_ms`; `discord_attempts >= 0`; and
      `cleared_at_ms >= fired_at_ms`.
    - Delivery states (`discord_state`): `none`, Discord was not configured when the
      alert fired, so it is never sent; `pending`, waiting to be sent, the next attempt
      due at `discord_next_at_ms`; `sent`, Discord accepted it; `failed`, given up, the
      attempts exhausted or the webhook refused (401, 403 or 404); `expired`, still
      pending an hour after it fired, and dropped. There is no "skipped": a disabled
      rule fires no alert at all, and every alert goes to both channels (no switch per
      channel). The transitions between these states belong to the Discord stage
      (stage 12) of milestone 5; the table only bounds the set.
    - Revision `0002` freezes the rules and the delivery states in its CHECK
      constraints. Changing either set (`ALERT_RULES`, `DISCORD_STATES`) needs a new
      revision that recreates them; a test compares the stored DDL of `alerts` and its
      indexes after the migrations with that of the models, so a drift fails.
    - Indexes: `ix_alerts_open` on `(host, rule, subject)` where `cleared_at_ms IS
      NULL` (a clear looks its key up); `ix_alerts_discord_pending` on `(host,
      discord_next_at_ms, id)` where `discord_state = 'pending'` (the deliveries due);
      `ix_alerts_fired_at_ms` (retention, the cooldowns rebuilt at start-up); and
      `ix_alerts_session_id` (the cascade).
- `alert_settings`, primary key `(host, field)`, with `value` and `updated_at_ms`: one
  row per setting the settings screen overrides.
    - `field` is the flat setting name, the `Settings` field behind `CCOBS_<FIELD>`
      (`alert_waiting_enabled`, `alert_waiting_threshold_s`, `alert_failures_enabled`,
      `alert_failures_count`, `alert_failures_window_s`, `alert_session_cost_enabled`,
      `alert_session_cost_usd`, `alert_daily_cost_enabled`, `alert_daily_cost_usd`,
      `alert_cooldown_s`). The allowlist is `repository.ALERT_SETTING_TYPES`, in code,
      not a CHECK, so a new setting needs no migration; a test keeps it equal to the
      `alert_*` fields of `Settings` and their types. The Discord webhook URL is never a
      row: it lives only in the environment (decision P7).
    - `value` is the JSON text of the value (`json.dumps`): `true`/`false` for a switch,
      an integer for seconds and counts, an integer or a finite decimal for dollars.
      Python writes the shortest decimal that reads back as the same float, so a value
      round-trips bit for bit, and `bool`, `int` and `float` stay apart. NaN and the
      infinities are refused.

**No personal data.** Neither table stores a project name, a `cwd`, a path, a model, a
prompt or any text from Claude Code. A page or a Discord message derives the project
name from the session row when it shows the alert (decision P6 decides whether Discord
gets it at all). The column allowlist of `tests/server/test_db.py` lists every new
column, and `subject`, which the content check would otherwise flag, is allowed for
`alerts` only because its CHECK keeps it to a session ID or `day`.

**Precedence.** For each alert setting, the effective value is the saved row when there
is one, else the `CCOBS_ALERT_*` variable when it is set, else the built-in default of
`ccobs.domain.alerts` (decision P3).

- Saving writes one row (`save_alert_setting`, an upsert); Reset deletes it
  (`reset_alert_setting`), and the setting falls back to the environment, then the
  default; `reset_alert_settings` deletes every row of the host.
- The repository refuses a name outside the allowlist or a value of the wrong type (a
  `bool` is never a number, seconds are whole), with an error that never echoes the
  value, nor an unknown name. Bounds are not the repository's: before saving, the
  settings screen validates the whole effective configuration, the environment's with
  the new value applied, with `validate_config` (ADR 0041), and saves nothing on an
  error.
- On load (`alert_settings`), a row that is not an allowlisted setting of the right
  type (a file edited by hand) is ignored and counted in a log line without names or
  values. The stage that applies the overrides validates the merged configuration with
  `validate_config` too, so a saved value out of bounds falls back to the environment's
  instead of stopping the app.

**State after a restart.** The writer rebuilds `AlertState` from the database:

- `alert_keys(host, cooldown_since_ms)` gives `AlertState.keys`: every key with an alert
  still open, firing, and every key that fired at or after `now - cooldown` (its
  cooldown still runs), each with its latest fire.
- `failure_windows(host, since_ms, count)` gives `AlertState.failures` from
  `hook_events`, which already stores every `StopFailure` with its `received_at_ms`, its
  `agent_id` and its `seq` (ADR 0026): for each session that has not ended, the
  `StopFailure` times after its last main-thread `Stop` or `SessionEnd` in `seq` order
  (the order the writer processed them, as ADR 0041's reset is by processing order, not
  by time), at or after `now - window`, the newest `count` of them. The last `Stop`'s
  time is not needed: the reset compares `seq`. No new column is needed.
    - A failure counts when no reset follows it in its session, a `NOT EXISTS` on the
      `(session_id, seq)` index of `hook_events`: it reads only the events after the
      failure, never the session's whole history.
    - It rebuilds the windows whether or not `repeated_failures` is enabled, unlike
      `record_failure_event`, which records nothing while the rule is off. The caller,
      the writer at start-up (stage 6), drops the rebuilt windows when the rule is
      disabled.

**Reads and writes.** Every write (`insert_alert`, `clear_alert`, `set_delivery`, the
settings writes, retention) runs on the writer's engine; the API's reads (`list_alerts`,
`alert_settings`) run on the readers' engine, through the read gate. `list_alerts` pages
by `id` (keyset, newest first, the cursor exclusive) and returns at most 500 rows,
ADR 0033's cap; `due_deliveries` returns the pending deliveries due, the earliest first,
at most 100. A clear never sets a time earlier than its fire, even if the clock went
back.

**Retention.** The retention job of ADR 0026 (`delete_older_than`, `CCOBS_APP_RETENTION`)
also deletes, in the same transaction, the `daily_cost` alerts that are cleared and
fired before the cutoff. A session's alerts go with the session, by the cascade. A daily
alert still open is kept, whatever its age, so the evaluator still knows the key fires
after a restart; it goes once it clears and ages out.

### Consequences

- Good, because no alert row can hold content: the subject is checked by the database,
  and the project name is never stored.
- Good, because a restart fires nothing twice and keeps every cooldown and failure
  streak, with no table of evaluator state to keep in step with `AlertState`.
- Good, because Reset is a delete, and a new setting needs only the allowlist and the
  `Settings` field, no migration.
- Bad, because a saved setting outlives a change of the environment: a user who edits
  `CCOBS_ALERT_*` sees no effect until they press Reset. The settings screen must show
  which values are saved.
- Bad, because a database file restored by hand could hold a row of the wrong type,
  which is then ignored silently but for a count in the log.
- Neutral, because the CHECK constraints freeze the set of rules and delivery states: a
  new rule or state needs a revision that recreates `alerts` (batch mode).

### Confirmation

- `tests/server/test_db.py`: the column allowlist with both tables, `compare_metadata`
  clean after `upgrade_to_head`, the stored DDL of `alerts` and its indexes (CHECK
  constraints and partial index `WHERE` clauses, which `compare_metadata` ignores) the
  same after the migrations as from the models, an upgrade from revision `0001` with data in it that
  keeps every row and passes `foreign_key_check`, the downgrade to `0001` that keeps the
  other tables' rows, the CHECK constraints accepting each valid row and rejecting each
  bad one, the foreign key, the cascade from `sessions`, the indexes (partial ones
  included), `AUTOINCREMENT`, and a batch copy of `sessions` with foreign keys off that
  keeps the alert rows.
- `tests/server/test_repository.py`: each function, keyset pagination over every alert
  once, the caps, host isolation, a clear never earlier than its fire, the delivery
  states, the keys and cooldowns rebuilt after a restart (and an evaluation on them that
  does not fire again), failure windows equal to those of `record_failure_event` after a
  replay, the reads on the readers' engine, the settings' exact round trip of bools,
  ints and floats, refused names and types without their values, ignored rows, Reset,
  and retention of daily alerts with the cascade of session alerts.

## More information

- Extends ADR 0005 (alerts and settings in SQLite) and ADR 0026 (schema, migrations,
  retention); uses ADR 0041's keys and episodes and ADR 0033's engines and caps.
- Later stages of milestone 5 wire these functions into the writer (evaluation every
  5 s), the Discord sender (delivery states, retries) and the settings screen.
- Revisit if central mode is planned (ADR 0001), or if users ask for alert history to
  outlive the sessions it belongs to.
