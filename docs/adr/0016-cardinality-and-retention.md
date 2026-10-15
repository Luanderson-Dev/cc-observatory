---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Session IDs on metrics, not in Loki's index, and retention of 90, 30 and 7 days

## Context and problem statement

The stack runs unattended for months ([ADR 0019](0019-restart-unless-stopped.md)), so
two things decide how its storage grows. The first is cardinality: Claude Code adds
`session.id` to every metric data point by default (`OTEL_METRICS_INCLUDE_SESSION_ID`,
default `true`, in the
[monitoring docs](https://code.claude.com/docs/en/monitoring-usage)), so every session
creates new Prometheus series, and every event carries it too. The
second is retention: [ADR 0003](0003-observability-backends.md) asks for configurable
retention, and the upstream defaults differ widely. Prometheus keeps 15 days
(`--storage.tsdb.retention.time`), Loki deletes nothing until compactor retention is
turned on (`retention_period` defaults to `0s` in 3.7.8), and Tempo keeps 14 days
(`block_retention: 336h`). Should session IDs stay on metrics, where should they live
in Loki, and how long should each backend keep its data?

## Decision drivers

- Per-session views (cost, tokens, active time) from PromQL, with the exact totals of
  [ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md).
- Bounded memory and disk on a laptop that runs the stack for months.
- Defaults that fit common use: a monthly or quarterly look at cost, recent debugging.
- Each signal is different: metrics are small and useful for long; events are larger
  and mostly read while debugging recent sessions; traces are the largest, and a beta
  feature of Claude Code.
- Settings the user can change without editing the bundled configuration
  ([ADR 0020](0020-compose-conventions.md)).

## Considered options

For session IDs on metrics:

- **Keep `session.id` on metrics**, Claude Code's default. One set of series per
  session.
- **Drop it**, with `OTEL_METRICS_INCLUDE_SESSION_ID=false` or a collector processor.
  Fewer series, but no per-session metrics; and with the cumulative temporality that
  `ccobs setup` sets (ADR 0013), the streams of concurrent sessions collide on one
  series, which broke totals in ADR 0013's spike.

For session IDs in Loki, a `session_id` index label is ruled out by
[ADR 0015](0015-loki-through-its-otlp-endpoint.md): one stream per session is the
high-cardinality pattern Loki's docs warn against.

For retention:

- **90 days of metrics, 30 of events, 7 of traces, each set by a variable.** Sized to
  each signal's volume and use.
- **The upstream defaults.** No settings to add, but Loki would grow without limit, and
  15 days of metrics cannot show a month of cost.
- **Longer everywhere, such as a year.** Metrics would still be small, but a year of
  events and traces grows to sizes a laptop should not hold by default.
- **Size-based limits.** Prometheus can cap its storage by size
  (`--storage.tsdb.retention.size`), which bounds disk directly but makes the history's
  length depend on activity; Loki and Tempo offer only time-based retention.

## Decision outcome

Chosen options: "Keep `session.id` on metrics" and "90 days of metrics, 30 of events, 7
of traces, each set by a variable", because per-session metrics and exact totals need
the label at a cost that stays small, and per-signal retention bounds disk with
defaults that match how each signal is used. In Loki, session IDs stay in structured
metadata (ADR 0015).

**Cardinality:**

- `session.id` stays on every metric. The fixtures have at most 18 series per session
  (ADR 0013), and a long session with several models, tools and languages stays around
  50, the bound used here. At 50 sessions a day that is about 2,500 new series a day: a
  small head block in Prometheus, where series that receive no samples are dropped at
  the next head compaction, and roughly 1 MB a day on disk. These are estimates. The
  stack's memory was measured with a few replayed sessions
  ([`docs/stack.md`](../stack.md#measured-use)); disk and memory with 30 to 90 days of
  real history have not been.
- `ccobs setup` (milestone 4) never sets `OTEL_METRICS_INCLUDE_SESSION_ID=false`, as
  ADR 0013 also requires. If the user sets it elsewhere, `ccobs doctor` (milestone 4)
  warns, and the app must degrade gracefully: it cannot split metrics by session, and
  can fall back to the `api_request` events in Loki, which carry `cost_usd` and token
  counts per request. Milestone 3 decides how the UI shows it.
- In Loki, session IDs (and prompt, trace and span IDs) are structured metadata, never
  index labels: one stream per machine (ADR 0015).

**Retention**, a default per backend, each changed through its variable:

| Backend    | Default          | Variable                     | Applied by         |
| ---------- | ---------------- | ---------------------------- | ------------------ |
| Prometheus | 90 days (`90d`)  | `CCOBS_PROMETHEUS_RETENTION` | TSDB retention     |
| Loki       | 30 days (`720h`) | `CCOBS_LOKI_RETENTION`       | its compactor      |
| Tempo      | 7 days (`168h`)  | `CCOBS_TEMPO_RETENTION`      | its backend worker |

- Loki's retention lands with Loki: `retention_period` reads `CCOBS_LOKI_RETENTION`
  (`-config.expand-env=true`), which `compose.yaml` passes with the default `720h`.
  Loki's docs require a multiple of the 24h index period
  ([retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)),
  but Loki 3.7.8 accepts other values without a warning, at `-verify-config` and at
  start; `.env.example` says so, and `ccobs doctor` (milestone 4) checks it.
- Prometheus's retention is a flag: `compose.yaml` passes
  `--storage.tsdb.retention.time=${CCOBS_PROMETHEUS_RETENTION:-90d}` on its command
  line.
- Tempo 3.1.0 applies retention in its backend worker, which runs the retention jobs
  with `backend_worker.compaction.block_retention` (default 336h). The config sets it
  to `${CCOBS_TEMPO_RETENTION:-168h}` (`-config.expand-env=true`), and `compose.yaml`
  passes the variable. Tempo's configuration has a second copy of the key, under
  `backend_scheduler.provider.compaction.compaction`, which only affects how the
  scheduler selects compaction jobs and applies no retention; it stays unset. Tempo
  runs only in the `traces` profile (ADR 0003).
- Each variable takes its own backend's duration format, and `.env.example` says
  which:
    - Prometheus: its own units (`y`, `w`, `d`, `h`, `m`, `s`, `ms`), such as `90d`;
    - Loki: a multiple of 24h, such as `720h` (above);
    - Tempo: a Go duration, hours at most, such as `168h`. Tempo refuses `7d` while it
      parses its configuration, and does not start.
- Every variable has its default in the compose files and in `.env.example`, so
  setting one in `.env` is enough (ADR 0020). The app database's retention is separate
  ([ADR 0005](0005-sqlite-for-app-state.md), from v0.3.0).

### Consequences

- Good, because per-session cost, tokens and active time come from PromQL with exact
  totals, and Loki's index stays one stream per machine.
- Good, because every backend's disk use is bounded by time, with defaults that keep a
  quarter of cost history, a month of events and a week of traces.
- Bad, because metric series grow with the number of sessions: scripts that start many
  short `claude -p` sessions create many series. Retention bounds the total; a size cap
  for Prometheus is the next step if real use needs it.
- Bad, because time-based retention does not bound disk for a very heavy user.
- Bad, because the three variables take different formats: Loki's must be a multiple
  of 24h, and Tempo's a Go duration with no days, or Tempo does not start.
- Neutral, because Loki deletes expired data some time after it expires: its compactor
  marks expired chunks every 15 minutes (`apply_retention_interval`) and deletes them 2
  hours later (`retention_delete_delay`).
- Neutral, because lowering a retention deletes older data at the next compaction, and
  raising it does not bring back what was deleted.

### Confirmation

- `tests/test_loki_config.py` checks that `retention_period` comes from
  `CCOBS_LOKI_RETENTION` with the default `720h`, a multiple of 24h, that the compactor
  applies retention, and that `service.name` and `host` are the only index labels;
  `tests/test_compose.py` checks that every variable has a default listed in
  `.env.example` with the same value.
- `tests/integration/test_stack_logs.py` reads Loki's `/config` and finds 30 days of
  retention, and finds no session, prompt, event or trace field among the index labels.
- `tests/test_prometheus_config.py` checks that `--storage.tsdb.retention.time` comes
  from `CCOBS_PROMETHEUS_RETENTION` with the default `90d`, and
  `tests/test_tempo_config.py` that `backend_worker.compaction.block_retention` comes
  from `CCOBS_TEMPO_RETENTION` with the default `168h`, a Go duration, and that the
  config sets no other `block_retention`. The CI `stack` job runs Tempo's
  `-config.verify=true` on its config.
- `tests/integration/test_stack_metrics.py` finds `90d` in Prometheus's flags, and
  `tests/integration/test_stack_traces.py` finds `168h0m0s` as the backend worker's
  block retention in Tempo's `/status/config`.
- CLI tests (milestone 4) check that `ccobs setup` never sets
  `OTEL_METRICS_INCLUDE_SESSION_ID=false` and that `ccobs doctor` warns when it is
  `false` and when `CCOBS_LOKI_RETENTION` is not a multiple of 24h.
- The `reviewer` subagent checks that no change deletes `session.id` from metrics or
  adds a per-session label to Loki.

## More information

- Prometheus: [storage](https://prometheus.io/docs/prometheus/latest/storage/)
  (`--storage.tsdb.retention.time`, `--storage.tsdb.retention.size`).
- Loki: [retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)
  and [cardinality](https://grafana.com/docs/loki/latest/get-started/labels/cardinality/).
- Tempo: [configuration](https://grafana.com/docs/tempo/latest/configuration/)
  (`backend_worker`, `block_retention`); in the 3.1.0 source, the worker's retention
  job (`modules/backendworker/backendworker.go`) and `tempodb/retention.go`.
- Claude Code [monitoring docs](https://code.claude.com/docs/en/monitoring-usage):
  `OTEL_METRICS_INCLUDE_SESSION_ID`.
- Revisit if real use shows many more series per session than estimated, if users ask
  for longer history, or if disk use calls for a size cap.
