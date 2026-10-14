---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# SQLite for the app's own state

## Context and problem statement

Hooks are point events. To show live state, the app keeps what it derives from them
(sessions and their state, agents and subagents, blocks), plus its own data: the alerts
it sent and the user's settings. Unlike the time series in Prometheus and the events in
Loki, this state changes in place (a block closes, a session ends). Where should the app
keep it?

## Decision drivers

- State survives a restart of the app container.
- In-place updates and simple queries over current state.
- No extra service to run, back up or upgrade on a single machine
  ([ADR 0001](0001-local-single-machine-scope.md)).
- Schema changes as the project grows, with no manual steps for users.
- Works the same on Linux, macOS and Windows hosts.

## Considered options

- **A SQLite file on a named volume.** Embedded, no extra service; one writer at a time.
- **PostgreSQL.** Concurrent writers and richer SQL, but another container, more memory
  and an upgrade path to manage for data that fits in a small file.
- **In memory only.** Simplest and fastest, but state, block history and alert
  deduplication are lost on every restart.
- **Everything in Loki.** One store fewer, but Loki is append-only: current state would
  be rebuilt by scanning events, never updated in place.

## Decision outcome

Chosen option: "A SQLite file on a named volume", because it keeps state across restarts
with no extra service, and the app's write load is small.

- The database file lives on a named Docker volume and runs in WAL mode, so readers do
  not block the writer.
- SQLAlchemy 2 for data access; Alembic for schema migrations.
- The app database holds what only hooks tell us (sessions, agents, blocks) and the
  app's own data (alerts and the user's settings). Time series stay in Prometheus and
  events in Loki ([ADR 0003](0003-observability-backends.md)).
- Retention is configurable (from v0.3.0): rows older than the chosen period are
  deleted.

### Consequences

- Good, because there is nothing to install or operate besides the app container.
- Good, because Alembic gives existing databases a defined upgrade path.
- Bad, because SQLite allows one writer at a time; the app already persists from a
  background queue (CLAUDE.md), so its writes can be serialized.
- Bad, because WAL needs a local filesystem: a bind mount to the host goes through
  Docker Desktop's file sharing on Windows and macOS, which may not give WAL the locking
  it needs. A named volume avoids that, but the file is less easy to reach from the host.
- Bad, because SQLite's limited `ALTER TABLE` makes some Alembic migrations use batch
  mode, which copies and rebuilds the table.
- Neutral, because the app database stores the `host` label from ADR 0001 too.

### Confirmation

- Tests (milestone 2) check that the database reports `journal_mode` `wal`, that
  `alembic upgrade head` on an empty database matches the models, and that retention
  deletes old rows.
- A test over `compose.yaml` (milestone 2) checks that the app's data directory is a
  named volume.

## More information

- The schema, when migrations run, the session state machine and the retention defaults
  are decided in milestone 2.
- Revisit if central mode (ADR 0001) is ever planned: several hosts writing to one
  database would favour PostgreSQL.
