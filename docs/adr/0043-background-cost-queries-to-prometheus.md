---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# A background reader sends the cost alerts' queries to Prometheus once a minute

## Context and problem statement

[ADR 0041](0041-alert-rules-defaults-deduplication-and-evaluation.md) defines the cost
rules (`session_cost`, `daily_cost`): the domain builds their queries
(`session_cost_query`, `daily_cost_query`), parses the answers into a `CostSample`, and
the writer evaluates the last sample it was given at every alert tick. It left to
stage 7 of milestone 5 who sends those queries, how often, and whether they pass
`lookback_delta`. Until now the app queried Prometheus only for the session page
([ADR 0035](0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md)), on a
person's request and cached. The cost alerts need answers with nobody watching, and the
session query's range grows with the oldest open session's age. How does the app read
the cost totals in the background without straining Prometheus, leaking session IDs or
firing on bad data?

## Decision drivers

- Alerts on the totals the session pages show: ADR 0013's `increase(... anchored)`
  through the domain's queries, never part of a total.
- Bounded load on Prometheus, since the session query reads every cost sample of the
  host in a range that can span days (ADR 0041).
- No load at all for users who never turn a cost rule on (both are off by default).
- No session ID in an automatic query
  ([ADR 0029](0029-closing-esc-denials-from-loki.md), ADR 0035).
- A Prometheus that is down, slow or wrong never fires or clears an alert, and never
  slows the hooks or the writer (ADR 0026, one writer).
- The same patterns as the app's other background readers, testable with fake clocks.

## Considered options

- **A cost reader thread, polling every 60 s while a cost rule is enabled.** A daemon
  thread like the reconciler (ADR 0029), with a sync HTTP client of its own, submitting
  each sample to the writer's control channel.
- **Queries from the writer thread at each alert tick.** No new thread, but the single
  writer would wait on the network every 5 s, holding up the hooks behind it.
- **An asyncio task on the app's event loop**, like the session page's reader. It shares
  the loop with the hook route; a slow Prometheus costs nothing there, but the loop
  then runs a timer that has nothing to do with requests, and the writer is reached
  from threads anyway.
- **Prometheus recording or alerting rules.** Prometheus would evaluate the totals
  itself, but the rules would need the session IDs the app holds and an Alertmanager
  (the stack runs none, `SECURITY.md`), and the app's episodes, cooldown and settings
  screen would no longer apply.

## Decision outcome

Chosen option: "A cost reader thread, polling every 60 s while a cost rule is enabled",
because it keeps the network off the writer thread and the event loop, sends nothing
while the rules are off, and follows the reconciler's tested pattern (its loop, back-off,
stop and logging rules). The reader is `ccobs.server.costs.CostReader`.

**Cadence.** Every 60 s, Claude Code's default metrics export interval
(`OTEL_METRIC_EXPORT_INTERVAL`, 60000 ms, which `ccobs setup` leaves alone): a faster
poll sees no new sample and reads the same range again. The reader refuses an interval
under 60 s, and its back-off starts at no less than the interval, so it never queries
more often than once a minute. The first round runs a minute after the app starts.

**Only when enabled.** Each round reads the writer's effective rules
(`Writer.alert_config`: the environment's with the saved settings on top, ADR 0042),
so a rule turned on or off on the settings screen applies from the next round. With
both cost rules off, a round sends nothing. Otherwise:

- `session_cost_query` only while `session_cost` is on and the writer holds a session
  that can alert. `Writer.session_cost_since`, read under the writer's lock, gives the
  earliest start of the sessions held that are neither `ended` nor `stale`, and so the
  query's range; with none, the query is not sent. The reader never touches the
  database.
- `daily_cost_query` only while `daily_cost` is on.
- Both are instant queries, `GET /api/v1/query` with `query` and `time` (the round's
  time by the hook route's clock, the same that stamps the sample), and nothing else.

**No session ID in a query.** The session query groups by `session_id` and has no
`session_id` matcher (ADR 0041), so one query covers every session and no session ID
reaches Prometheus. The daily query names no session either. Both filter on the `host`
label only.

**Failures and staleness.** Each query is bounded like the session page's (ADR 0035):
3 s per phase of the exchange and 3 s for the whole answer, checked as it streams in;
answers over 8 MiB are given up; redirects are never followed and no proxy is taken
from the environment. A round submits a sample only when every query it sent was
answered with a valid vector (`parse_cost_sample`'s rules, ADR 0041). Any failure (no
connection, a timeout, a non-2xx status, a redirect, an answer too large, not JSON or
not a valid vector) submits nothing and skips the round's other query. The writer
keeps its last sample, which ages: past ADR 0041's 180 s the cost rules neither fire
nor clear, so an alert never clears because Prometheus stopped answering. After a
failure the next wait doubles, from 60 s up to 600 s (60, 120, 240, 480, 600), and a
successful or idle round returns it to 60 s. A single failed round is retried at the
poll interval, so the sample is at most about 120 s old at the next attempt and the
rules keep working; only consecutive failures age it past 180 s and put the rules on
hold until the next good round, which is the intended reading of a missing answer. An error from the writer or the clock is
logged and the loop goes on without backing off.

**Observability and logs.** Each query counts its result in
`ccobs_prometheus_queries_total{host, result}` (ADR 0035's `ok`, `error`, `timeout`,
`invalid`), with no new series. Logs hold exception type names, status codes and
reasons only: never the URL with its query, a session ID or an answer.

**Load bound.** At most two instant queries a minute while a cost rule is on. The daily
query reads 24 hours of the host's cost series. The session query's range runs from 5
minutes before the oldest open session's start to now (ADR 0035's window), so it grows
with that session's age: a session idle but alive for days makes every round read days
of samples. It stays bounded by the stale sweep (`CCOBS_SESSION_STALE_AFTER`, 12 hours
by default since the last event, ADR 0024) and by the answer's size cap; one query
covers every session, so the number of sessions changes the answer's size, not the
number of queries.

**`lookback_delta`: not passed.** [ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md)
says a window needs `lookback_delta` only when a series has no sample within
Prometheus's lookback (5 minutes) before the window's start. Neither query needs it:

- The session query's range starts 5 minutes before the oldest open session's start,
  and nothing of the sessions it covers from their start comes before it, so `anchored`
  starts from their zero samples (ADR 0035, "Window"). Older series that cross the
  range's start belong to sessions the writer no longer holds, which the evaluator
  ignores (ADR 0041).
- The daily query's range starts 24 hours ago, so a series that crosses its start needs
  a sample within 5 minutes before it. ADR 0013 left one behaviour to confirm: that
  Claude Code in cumulative mode, which `ccobs setup` sets, re-exports unchanged series
  at every export interval. Milestone 4 added that check with a real Claude Code
  session, `test_cumulative_metrics_reexport_unchanged_series` in
  `tests/integration/test_claude_real.py` (opt-in, `CCOBS_IT_CLAUDE=1`): it requires at
  least four samples in a row with the same value while the session is idle, at a 5 s
  interval. The maintainer ran it on 2026-10-20, before v0.5.0, and it passed; ADR 0013
  is accepted and stays as written. So a session
  running across the range's start has a sample at least every export interval (60 s
  by default), well inside the lookback, and its total is exact. A process that ended
  before the range has no sample after its last export, and its cumulative value then
  adds nothing to the range, which is correct.
- What remains is the case ADR 0041 records: delta temporality set outside
  `ccobs setup`, where an idle series exports nothing and the day can undercount a
  session idle across the range's start. A larger `lookback_delta` would cover a longer
  pause there, but no value covers every pause, it makes every query read further back,
  and the supported setup does not need it. Revisit if a recapture shows Claude Code
  stopping its cumulative re-exports.

**Lifecycle.** The app's lifespan starts the reader after the alert ticker, when the
sweeps run (`create_app(sweeps=False)` leaves it off with the sweeper and the ticker),
and stops it before the ticker and the writer, after the reconciler, so nothing is
submitted to a stopping writer. Its stop interrupts the wait at once; a query in flight
is bounded by its timeouts, and shutdown moves on, with a warning, after 5 s.

### Consequences

- Good, because users who never enable a cost rule get no extra query, and those who
  do get at most two instant queries a minute, whatever the number of sessions.
- Good, because the writer thread and the event loop never wait on Prometheus, and a
  failing Prometheus only puts the cost rules on hold.
- Good, because no session ID reaches Prometheus from the alerts, and the existing
  query counter shows the reader's health with no new series.
- Bad, because alerts lag: Claude Code's export interval (about a minute), plus up to
  the reader's minute, plus up to the 5 s tick. Accepted: the cost limits are dollars,
  not seconds.
- Bad, because the session query's range grows with the oldest open session's age, so
  a session kept open for many hours makes each round read many hours of samples.
  Mitigated by the stale sweep, the size cap and the once-a-minute cadence.
- Bad, because with `OTEL_METRICS_INCLUDE_SESSION_ID=false` set outside `ccobs setup`
  ([ADR 0016](0016-cardinality-and-retention.md)) the series carry no `session_id`: the
  session query's answer has no session to match, `session_cost` never fires, and the
  reader still sends it every minute while the rule is on. `daily_cost` still works.
- Bad, because with delta temporality set outside `ccobs setup`, the day can undercount
  a session idle across its start (no `lookback_delta`, above).
- Neutral, because the app now queries Prometheus in the background as well as for the
  session page, over the `backend` network only. The networks of
  [ADR 0030](0030-the-app-container.md) are unchanged; its note that the app calls only
  Loki predates ADR 0035, and the app's connections are now Loki and Prometheus.
  `SECURITY.md` and the no-outbound integration test list both.

### Confirmation

- `tests/server/test_costs.py`, with an `httpx.MockTransport`, a fake writer, a fake
  wall clock and a fake wait: no query while no cost rule is on, nor for the session
  rule alone with no open session; the session query alone (its path, `query` and
  `time`), the daily query alone, and both making one sample; no query with a
  `session_id` matcher or a session ID in its URL; the rules read at every round; each
  failure kind (no connection, timeouts, error statuses, a redirect, not JSON, too deep,
  an error answer, a matrix, `NaN`, a negative value, a day of two series, an infinite
  day, an answer too large, one that trickles past the whole-query timeout) submits
  nothing, counts its result and logs no URL, query, session ID or answer; the cadence
  (never under 60 s, refused below it) and the back-off up to its cap and its reset; a
  failing writer or clock; a prompt stop that closes the client; a stop that times out;
  a sample read into a real writer that fires both rules at its next tick; and the
  lifespan (started with the sweeps, off without them, after the ticker and stopped
  before it, its queries counted in the app's metric).
- `tests/server/test_writer_alerts.py` covers `Writer.session_cost_since`: none without
  sessions, the oldest open start, ended and stale sessions left out.
- `tests/integration/test_stack_no_outbound.py`
  (`test_the_app_connects_to_nothing_but_loki_and_prometheus`) allows the app's
  connections to Prometheus's port on Prometheus's addresses, besides Loki's.
- The `reviewer` subagent checks that the reader's queries come from the domain, carry
  no `session_id` matcher and no `lookback_delta`, and that its cadence stays at 60 s or
  more.

## More information

- Extends ADR 0035 (the app's Prometheus client, timeouts and failure handling, now
  also used in the background) and ADR 0041 (who sends the cost queries, how often, and
  the `lookback_delta` it left to this stage). Neither is rewritten.
- Prometheus [HTTP API, instant queries](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries)
  (`time`, `lookback_delta`).
- Claude Code [monitoring docs](https://code.claude.com/docs/en/monitoring-usage):
  `OTEL_METRIC_EXPORT_INTERVAL` (default 60000 ms) and
  `OTEL_METRICS_INCLUDE_SESSION_ID`.
- Revisit if Claude Code changes its export interval or stops re-exporting unchanged
  cumulative series, if users need faster cost alerts, or if the session query's range
  becomes a measurable load.
