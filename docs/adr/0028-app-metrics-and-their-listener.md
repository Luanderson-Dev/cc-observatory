---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# The app's metrics: fixed-cardinality series on a separate, unpublished listener

## Context and problem statement

The app holds live state that no other signal has: sessions by state, open blocks,
running agents, and what happened to each hook it received
([ADRs 0024](0024-session-agent-and-block-state-machine.md) and
[0027](0027-the-app-http-surface-and-hook-queue.md)). Prometheus should scrape it, so
that Grafana (milestone 6) and `ccobs doctor` (milestone 4) can show it next to Claude
Code's own metrics. Prometheus stores Claude Code's metrics through its OTLP receiver
with `created-timestamp-zero-ingestion` on
([ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md)), keeps `session.id`
on them ([ADR 0016](0016-cardinality-and-retention.md)), and every signal carries the
host label ([ADR 0022](0022-the-host-label.md)). The app's routes sit behind a `Host`
allowlist against DNS rebinding (ADR 0027), the same reason Grafana's own `/metrics` was
turned off ([ADR 0017](0017-grafana-access-and-dns-rebinding.md)), while Prometheus
reaches the app over the compose networks of
[ADR 0018](0018-ports-networks-and-exposure.md), under the compose service name, a
`Host` outside that allowlist. Which series does the app export, and how is `/metrics`
served without opening a read path to the browser?

## Decision drivers

- A fixed number of series, whatever the number of sessions, events or clients: the app
  runs for months on a laptop, and Prometheus keeps 90 days (ADR 0016).
- Gauges that never go stale, and series that always exist, so that queries and alerts
  do not need `or vector(0)` and a missing series means a missing scrape.
- No content and no personal data in any label (ADR 0006).
- No new read path for a web page, and no exception to the rules of CLAUDE.md and
  ADR 0017.
- The names live in the domain, so that the app, the dashboards and `ccobs doctor` agree
  (as `ccobs.domain.telemetry` does for Claude Code's metrics, ADR 0014).

## Considered options

For serving `/metrics`:

- **(a) Exempt `/metrics` from the `Host` check on the app's port.** One port, but it
  is exactly the exposure ADR 0017 removed from Grafana: the app's port is published on
  loopback, so a page using DNS rebinding could read the counts.
- **(b) Add the compose service name (`app`) to the `Host` allowlist.** One port and no
  exemption, but CLAUDE.md fixes the allowlist at `localhost`, `127.0.0.1` and `[::1]`;
  and an attacker's DNS name cannot be `app`, but a rebinding page needs only a name it
  controls, while `app` would also accept requests from any container on the network to
  every route, the hook route included.
- **(c) A separate listener on `CCOBS_METRICS_PORT` (9464), never published.** One more
  socket and thread, but `/metrics` is reachable only from the compose network, and the
  app's port keeps its rules unchanged.

## Decision outcome

Chosen option: "(c) a separate listener on `CCOBS_METRICS_PORT` (9464), never
published", because it keeps the `Host` allowlist and ADR 0017's reasoning intact while
Prometheus scrapes over the compose network, and it costs one daemon thread. This ADR
extends ADRs 0013, 0016, 0018 and 0022, which stay in force.

**The series.** Names, types, labels and help texts are constants in
`ccobs.domain.app_metrics` (`AppMetric`, `APP_METRICS`); no metric name is spelled out
in the server's code. Every series has the `host` label first, from
`CCOBS_HOST_LABEL` (ADR 0022). The names are stored as written: Prometheus scrapes them,
so there is no OTLP translation (unlike ADR 0014).

| Series                              | Type      | Labels            | Help text                                                                                                            |
| ----------------------------------- | --------- | ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| `ccobs_sessions`                    | gauge     | `host`, `state`   | Sessions that have not ended, by state.                                                                              |
| `ccobs_sessions_ended_total`        | counter   | `host`            | Sessions that moved to ended since the app started.                                                                  |
| `ccobs_blocks_open`                 | gauge     | `host`, `kind`    | Open blocks (sessions waiting on a person), by kind.                                                                 |
| `ccobs_agents_running`              | gauge     | `host`            | Running agents and subagents, in sessions that are neither ended nor stale.                                          |
| `ccobs_block_duration_seconds`      | histogram | `host`, `kind`    | How long each closed block stayed open, by kind.                                                                     |
| `ccobs_hook_events_processed_total` | counter   | `host`            | Hook events stored in the database since the app started.                                                            |
| `ccobs_hook_events_received_total`  | counter   | `host`, `event`   | Hook payloads parsed by the writer since the app started, by event (other for events ccobs setup does not register). |
| `ccobs_hook_events_dropped_total`   | counter   | `host`, `reason`  | Hooks dropped instead of stored since the app started, by reason.                                                    |
| `ccobs_hook_queue_depth`            | gauge     | `host`            | Hooks received and not yet handled by the writer.                                                                    |
| `ccobs_hook_queue_bytes`            | gauge     | `host`            | Body bytes of the hooks received and not yet handled by the writer.                                                  |
| `ccobs_build_info`                  | gauge     | `host`, `version` | Always 1; the version label is the running cc-observatory version.                                                   |
| `ccobs_database_wal`                | gauge     | `host`            | 1 if the SQLite database runs in WAL mode, 0 if its filesystem refused WAL.                                          |

- `state` takes every session status but `ended`: `working`, `waiting_user`, `idle`,
  `stale`. Ended sessions leave the writer's memory, and a gauge of them would only grow;
  `ccobs_sessions_ended_total` counts them instead.
- `kind` is `permission` or `elicitation` (ADR 0024).
- `event` is one of the events `ccobs setup` registers (`SessionStart` and the http
  events of [ADR 0025](0025-registered-hook-events.md)) or `other`, for every other name
  a payload carries: events the parser knows but that are not registered, and names it
  does not know. "Received" means parsed as a hook payload by the writer, whether or not
  it was then stored; a body that does not parse is a drop (`invalid`), not a receipt.
- `reason` is each drop reason of ADR 0027: `full`, `too_large`, `bad_content_type`,
  `bad_host`, `bad_request`, `invalid` and `internal`. **`internal`** is every loss on
  the app's side: an exception in the hook route, a parser bug (an exception other than
  a bad payload), a session whose stored state could not be loaded, an event the reducer
  could not apply, a write that kept failing after the item-by-item retry, and hooks
  still queued when the writer's stop timeout ran out at shutdown.
- `version` is `ccobs.__version__`, from `importlib.metadata`.
- `ccobs_database_wal` is read once at start-up with `wal_enabled`: the engine sets WAL
  on every connection, so the mode cannot change while the app runs, and a scrape never
  touches the database.

**No `session_id` label, ever.** A session label would add series for every session
for 90 days, from a value a client chooses. Per-session data is in the app's database
and API, and in Claude Code's own metrics, which keep `session_id` by design (ADR 0016).
Every label of the app takes values from a closed list, so the app exports 63 series
for each host (4 states, 2 kinds, 17 events, 7 reasons, 13 histogram series for each of
the 2 kinds, and 7 single series), however many sessions, events or bad requests
arrive.

**Every state, kind, reason and event is always exported, with 0 when empty.** A
series that disappears when its count drops to zero reads in PromQL as "no data", not
as 0: sums over it go empty, `rate` breaks across the gap, and an alert on
`ccobs_blocks_open > 0` cannot tell "no block" from "no scrape". Exporting the closed
list every time costs a few series and keeps queries simple.

**Live values are read at scrape time.** A custom collector asks the writer for
`counts()` and the queue for its depth, bytes and drop counters on each scrape; nothing
is pushed into gauges, so nothing goes stale between updates. The counters are the
writer's and the queue's own cumulative integers. The histogram is the only state the
metrics keep: a `prometheus_client` histogram fed by the writer's `on_block_closed`
callback, with one child per kind created at start-up so that both are exported with
zeros. Each app has its own `CollectorRegistry`, never the library's global one.

**Counters reset when the app restarts.** They live in memory, as the queue and the
writer do (ADR 0027). Prometheus treats a drop as a reset in `rate` and `increase`, so
queries over the app's counters use them as usual; the `anchored` and
`lookback_delta` rules of ADR 0013 are for Claude Code's OTLP metrics and do not apply.

**Histogram buckets:** 1, 5, 15, 30, 60, 120, 300, 600, 1800 and 3600 seconds, plus
`+Inf`. A block is a person's wait: an answer at the keyboard takes seconds, a glance
back from another window a minute or two, a return from a break up to an hour. The
buckets are finer below a minute, where an attentive person answers, and stop at an
hour; longer waits land in `+Inf`, and their exact length is in the database. Thirteen
series per kind: eleven buckets, `_sum` and `_count`.

**No `_created` series.** `prometheus_client` adds a `_created` series to every counter
and histogram unless `disable_created_metrics()` is called; the app calls it each time it
builds its metrics (the flag is process-wide). In the text format Prometheus scrapes
(`prometheus_client` serves no protobuf, so the `PrometheusProto` preference that
`created-timestamp-zero-ingestion` gives scrapes falls back to text, ADR 0013), each
`_created` would be one more stored series per counter and histogram child, holding the
app's start time, which no query needs: resets already show in the counters.

**The listener.** `ccobs.server.metrics.MetricsListener` serves the registry with
`prometheus_client`'s WSGI app on a `wsgiref` server in a daemon thread, bound to
`CCOBS_LISTEN_HOST` and `CCOBS_METRICS_PORT`:

- **Only `/metrics`.** Any other path gets a plain 404; the public app has no `/metrics`
  route, so `/metrics` on the app's port is a 404 behind the `Host` guard.
- **No `Host` check, safely.** In the container, `CCOBS_LISTEN_HOST` is all interfaces
  so that Prometheus can reach `app:9464` over the compose network, and compose never
  publishes 9464. A browser on the host cannot open a container's unpublished port, so a
  web page, DNS rebinding included, cannot reach it; Grafana's `/metrics` was different
  because Grafana's port is published (ADR 0017). Two exceptions, both accepted because
  the series hold counts and labels from closed lists, never content, paths or IDs:
  on Linux the host can reach a container's address on the bridge network (ADR 0018,
  Neutral), so a rebinding page that guessed the container's address could read the
  counts; and the debug override (`compose.debug.yaml`, stage 13 of milestone 2)
  publishes the listener on `127.0.0.1:4301`, which reintroduces the DNS-rebinding read
  exposure of ADR 0017 for these counts, for debugging only.
- **Started after the writer, stopped before it.** The lifespan builds the metrics
  before the writer (whose callback feeds the histogram), attaches the writer once it
  runs, then starts the listener; at shutdown it stops the listener and closes its
  socket before it stops the writer, so no new scrape reads a writer going away.
- **A busy port does not stop the app.** If the port cannot be bound, the app logs an
  error with the port and the exception's type and runs without the listener: hooks
  matter more than metrics, and Prometheus shows the target down. In the container the
  port is the app's own.
- **Quiet.** No access log (it would record client addresses and request lines), no
  reverse DNS lookup of the bound address, and `Server: ccobs` on every answer, the
  handler's own errors included, instead of `wsgiref`'s header with the Python version.
- **Exclusive port.** On Windows the socket sets `SO_EXCLUSIVEADDRUSE` before the bind
  and never `SO_REUSEADDR`, so no other socket can bind the listener's port while it
  runs; elsewhere `SO_REUSEADDR` only skips the `TIME_WAIT` of closed connections on a
  restart and never shares a port that is in use.

**The scrape job** (stage 13 of milestone 2 adds it to `stack/prometheus/prometheus.yaml`):
job `ccobs-app` (`SCRAPE_JOB`), target `app:9464`, every 15 s. The series keep the app's
own `host` label; the job adds `job` and `instance`.

### Consequences

- Good, because the app adds a fixed, small number of series (63 per host), and
  a client can add none: every label value comes from a closed list.
- Good, because queries need no `or vector(0)`, gauges are never stale, and a missing
  series means a failed scrape.
- Good, because the `Host` allowlist and ADR 0017's reasoning stay as they are, and the
  browser cannot reach `/metrics` on macOS and Windows, nor on Linux without guessing a
  container address.
- Good, because the names are importable by the dashboards and `ccobs doctor`, and a
  test keeps them out of the server's code.
- Bad, because counters reset on every restart of the app, and a histogram observation
  made while nothing scrapes is only in the next scrape's cumulative counts.
- Bad, because one more thread and port; the port must differ from `CCOBS_PORT`, which
  the settings check.
- Bad, because a busy metrics port is only an error in the log and a target down in
  Prometheus, not a failed start.
- Neutral, because the debug override's loopback port reopens a DNS-rebinding read of
  counts while it is in use.

### Confirmation

- `tests/test_domain_app_metrics.py` checks every name, type, label and help text, the
  host label first and no `session_id` on any metric, the states, kinds and event labels
  against the domain, and the buckets.
- `tests/server/test_metrics.py` parses the exposition with `prometheus_client`'s
  parser: exactly the documented families and types, the host label on every series, no
  `_created` and no `session_id`; the gauges and counters equal the database after
  replaying real fixtures; zeros for every state, kind, reason and event; each drop
  reason counted once after causing it; received hooks by event with `other`; the
  histogram equal to the stored block durations; build info with the version; WAL at 1
  on a temporary database and 0 when refused; the listener serving `/metrics` on an
  ephemeral port, 404 on other paths, no access log, `Server: ccobs` with no Python
  version, and an exclusive bind on Windows; `/metrics` a 404 on the public app; the
  listener's port free after shutdown and before the writer stops; a busy port leaving
  the app running; and no metric name spelled out in `src/ccobs/server/`. The other app
  tests turn the listener off, so only these tests open sockets.
- `tests/test_cli_light.py` and `tests/test_domain_telemetry.py` keep the domain module
  stdlib-only and free of I/O.
- The `reviewer` subagent checks that no label is added with values outside a closed
  list, and that the metrics port is never published outside `compose.debug.yaml`.

## More information

- `prometheus_client`: [custom collectors](https://prometheus.github.io/client_python/collector/custom/),
  [disabling `_created` metrics](https://prometheus.github.io/client_python/instrumenting/#disabling-_created-metrics),
  [WSGI exporting](https://prometheus.github.io/client_python/exporting/http/wsgi/).
- Prometheus: [metric and label naming](https://prometheus.io/docs/practices/naming/),
  [histograms](https://prometheus.io/docs/practices/histograms/), and the
  [`created-timestamp-zero-ingestion`](https://prometheus.io/docs/prometheus/latest/feature_flags/)
  feature flag.
- The scrape job, the container image with `CCOBS_LISTEN_HOST` set to all interfaces,
  and the debug override's port for the listener come in stage 13 of milestone 2.
- Revisit if the app gets a central mode (ADR 0022), or if a metric needs a label whose
  values are not a closed list.
