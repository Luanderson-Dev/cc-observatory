---
status: accepted
date: 2026-10-18
decision-makers: Luanderson-Dev (maintainer)
---

# A diagnostics endpoint for `ccobs doctor` and `status`

## Context and problem statement

`ccobs doctor` (milestone 4) must read the `num_non_blocking_error` and `num_cancelled`
counts of `claude_code.hook_execution_complete`
([ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md), lines 101 to 109), and
it should show the app's own state next to Claude Code's
([ADR 0028](0028-app-metrics-and-their-listener.md), lines 15 and 37). `ccobs status`
needs session counts by state and the open blocks. Neither ADR says how a CLI on the
host reaches that data: Loki and Prometheus publish no port outside the debug override
([ADR 0018](0018-ports-networks-and-exposure.md), lines 63 to 65), the app's `/metrics`
sits on a listener that is never published (ADR 0028), and `/api/v1/sessions` is a
capped page ([ADR 0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)),
so counting from it means paging. How do `doctor` and `status` get the hook failure
counts and the app's state?

## Decision drivers

- No new published port and no exception to the browser defences of
  [ADR 0027](0027-the-app-http-surface-and-hook-queue.md) and
  [ADR 0034](0034-browser-hardening-csp-fetch-metadata-and-response-headers.md).
- No content, session ID or path in the answer
  ([ADR 0006](0006-privacy-metadata-only-by-default.md)).
- One HTTP call per command, to a port the CLI already uses (the app's, ADR 0018).
- A broken Loki must not break the command: doctor reports it and goes on.
- The Loki and Prometheus client rules already in force
  ([ADR 0029](0029-closing-esc-denials-from-loki.md),
  [ADR 0035](0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md)):
  short timeouts, a capped answer, logs without URLs or bodies, no session ID in a
  query.

## Considered options

- **(1) Doctor queries Loki and Prometheus directly.** No app change, but neither
  publishes a port (ADR 0018); the debug override that publishes them is for
  development, and asking users to run it would widen the exposure ADR 0018 removed.
- **(2) Publish the app's `/metrics` listener and have doctor scrape it.** Reuses
  ADR 0028's series, but they hold no Loki counts and no count of ended sessions, and a
  second published port is a read path a page using DNS rebinding could reach, which
  ADR 0028 chose to avoid.
- **(3) One read-only JSON route in the app.** `GET /api/v1/diagnostics` gathers the
  app's state, its hook counters and one Loki query, behind the guards every API route
  already has.

## Decision outcome

Chosen option: "(3) one read-only JSON route in the app", because it needs no new port,
keeps every browser defence, and gives doctor and status one call each; (1) needs ports
that stay closed by design, and (2) lacks the data and widens the surface. The milestone
6 health page can reuse the same route. This ADR extends ADRs 0027, 0028, 0029, 0033 and
0035, which stay in force.

- **Route.** `GET /api/v1/diagnostics`, on the same router and guards as the rest of
  `/api/v1`: the `Host` allowlist, Fetch Metadata, `GET` only and the response headers
  of ADR 0034. JSON from a pydantic response model, like the other API models.
- **Fields.**
    - `version`: `ccobs.__version__`; `host`: the configured host label
      ([ADR 0022](0022-the-host-label.md)); `started_at_ms`: when the app started, by
      the wall clock, so doctor can say "since the app started".
    - `sessions.by_state`: a count for every session state of
      [ADR 0024](0024-session-agent-and-block-state-machine.md), each present with 0
      when empty; `sessions.open_blocks`: the blocks not closed. Read from SQLite with
      one bounded aggregate, never a list.
    - `hooks.received` and `hooks.dropped`: the in-process counters that already feed
      `ccobs_hook_events_received_total` and `ccobs_hook_events_dropped_total`
      (ADR 0028), with every registered event (plus `other`) and every drop reason
      present, 0 when none, and names from the domain constants.
    - `hook_failures`: `available`, `window_s` (3600), `executions` (the number of
      `hook_execution_complete` records), `non_blocking_error` and `cancelled`.
- **Never** content, a session ID, a `cwd` or other path, or a URL with a query.
- **SQLite through the read gate, Loki outside it.** The sessions part goes through
  ADR 0033's read gate like every API read, so a saturated gate answers the same 503
  as the other API routes. The gate is for SQLite only; the Loki query does not take
  a slot.
- **The Loki query.** One instant metric query (`GET /loki/api/v1/query`) over the last
  hour of the host's stream (ADRs 0015 and 0022), filtered on
  `event_name="hook_execution_complete"`: it counts the records and sums
  `num_non_blocking_error` and `num_cancelled` with `unwrap` (structured metadata, dots
  turned into underscores, ADRs 0014 and 0015). It carries **no session ID**, as
  ADR 0029 requires. A pure function in `ccobs.domain` builds the query and parses the
  answer, with the host quoted as ADR 0029 does; the exact LogQL is fixed there and in
  its tests, not here.
- **The Loki client** follows `ccobs.server.reconcile` and `ccobs.server.usage`: one
  httpx call with a short per-phase timeout (a constant, shorter than the reconciler's
  5 s, ADR 0029), the answer read as a stream under a size cap, and logs with exception
  type names and status codes only, never the URL or a body. Each query counts in
  `ccobs_loki_queries_total` as `ok` or `error` (ADR 0029); no new label or series.
- **Degraded, never failed.** When Loki is unreachable, times out, answers non-2xx or
  with an unexpected shape, `hook_failures` becomes `available: false` with a short
  `reason` (`unreachable`, `timeout`, `status` or `shape`) and `window_s`; the route
  still answers 200 with the SQLite and counter parts. A Loki failure never turns into
  a 500.
- **Doctor's own probe shows up.** Doctor probes the hook URL with `POST {}` (ADR 0011),
  which the app drops as `invalid`; each doctor run adds one `invalid` drop, and the
  code says so where the counters are read.

### Consequences

- Good, because doctor and status need one HTTP call to a port that is already
  published, and the stack publishes no new port.
- Good, because the route sits behind the same guards as every API route, and its
  answer holds only counts, the version, the host label and a start time.
- Bad, because each call sends one Loki query; it is a cheap metric query over one
  hour of one stream, and the route is `GET`-only behind the browser defences, so a web
  page cannot drive it.
- Bad, because the failure counts include the hooks Claude Code adds itself and the
  user's own hooks ([ADR 0012](0012-observed-telemetry-and-hook-contract.md), lines 155
  to 157), and miss the events that emit no `hook_execution_*` (ADR 0011): doctor can
  only warn, not name our hook as the cause.
- Neutral, because the hook counters live in memory and reset when the app restarts
  (ADR 0028); `started_at_ms` tells doctor how far back they go.

### Confirmation

- Domain tests check the query (host quoted, no session ID) and the parsing of a Loki
  vector answer into the three numbers, with malformed answers rejected.
- Server tests, with a fake Loki on an httpx `MockTransport` as the usage tests do,
  check the shape and zeros on an empty database, counts by state and open blocks,
  the counters, Loki ok and each failure (unreachable, timeout, 500, garbage) as
  `available: false` with its reason and HTTP 200, the read gate path, the guards
  (cross-site Fetch Metadata, a bad `Host`, `POST`), and that seeded session IDs,
  paths and content never appear in the body.
- An integration test checks that the route answers 200 against the `ccobs-it` stack.

## More information

- Grafana Loki:
  [metric queries](https://grafana.com/docs/loki/latest/query/metric_queries/)
  (unwrapped range aggregations) and the
  [HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/) (instant
  queries).
- Related: ADRs 0011, 0012, 0014, 0015, 0018, 0024, 0027, 0028, 0029, 0033, 0034 and
  0035.
- Revisit when Claude Code reports hook failures per hook (so doctor could tell ours
  apart), or when the milestone 6 health page needs more than this route returns.
