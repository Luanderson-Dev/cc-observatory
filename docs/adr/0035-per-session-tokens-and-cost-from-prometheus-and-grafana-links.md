---
status: accepted
date: 2026-10-18
decision-makers: Luanderson-Dev (maintainer)
---

# Per-session tokens and cost from Prometheus, and links from the UI to Grafana

## Context and problem statement

Product decision P1 of milestone 3 puts each session's totals on its page: tokens by
type and by model, and cost by model and in all. The hooks carry no token or cost data
([ADR 0012](0012-observed-telemetry-and-hook-contract.md)); Claude Code's own metrics
do, and they reach Prometheus through the collector
([ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md)) under the names of
[ADR 0014](0014-stored-telemetry-names.md), with `session_id` and `host` labels
([ADR 0016](0016-cardinality-and-retention.md), [ADR 0022](0022-the-host-label.md)).
The page should also open the session's events, tokens and cost in Grafana, which
answers only at `http://localhost:<port>` and redirects every other host name
([ADR 0017](0017-grafana-access-and-dns-rebinding.md)). How does the app read a
session's totals without slowing the page or straining Prometheus, and how does it build
links that land on the right Grafana view?

## Decision drivers

- Exact totals: ADR 0013's `increase(... anchored)`, never `rate()` or plain
  `increase()`, with names from `ccobs.domain.telemetry`, never guesses (ADR 0014).
- The session page stays fast and usable when Prometheus is slow, down or wrong; the
  rest of the page does not depend on it.
- No read slot of the database held while waiting on the network
  ([ADR 0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)).
- Bounded load on Prometheus however many tabs refresh.
- The app's metrics keep a fixed cardinality
  ([ADR 0028](0028-app-metrics-and-their-listener.md)).
- No session ID written to a store's log by an automatic query
  ([ADR 0029](0029-closing-esc-denials-from-loki.md) kept Loki's query log free of
  them).
- Links that work with the stack's defaults and any `CCOBS_GRAFANA_PORT`, open a new
  tab and never leak the app's URL.

## Considered options

- **(a) Links to Grafana only.** No query from the app, but the totals, the page's main
  numbers, would need a click and a second tool.
- **(b) The app queries Prometheus per session, on the session page only, cached.** One
  small section, three instant queries per session, refreshed every minute.
- **(c) Totals on the overview's strips.** One query for every session on the board, on
  every live update; the board would depend on Prometheus. P1 leaves this to milestone 6.
- **(d) Copy tokens and cost into SQLite** from Loki's `api_request` events. A second
  pipeline with its own gaps, duplicating what Prometheus already totals.

## Decision outcome

Chosen option: "(b) the app queries Prometheus per session, on the session page only,
cached", plus the links of (a), because it shows ADR 0013's totals where they matter
(exact within that ADR's limits, see "Limits" below), keeps
the board free of Prometheus, and costs at most three small queries per session per
half minute.

### Tokens and cost

- **Setting.** `CCOBS_PROMETHEUS_URL`, Prometheus's HTTP API as the app reaches it,
  default `http://prometheus:9090` (the compose service on the `backend` network,
  [ADR 0018](0018-ports-networks-and-exposure.md)). Checked like `CCOBS_LOKI_URL`:
  `http` or `https`, a host, no user, query or fragment, trailing slashes stripped; the
  value is never echoed in an error. compose passes it through with that default, and
  `.env.example` offers it.
- **Queries.** `ccobs.domain.usage` builds three instant queries with
  `telemetry.total_query`, filtered on `host` (the app's `CCOBS_HOST_LABEL`) and
  `session_id`:
  `sum by (type)` and `sum by (model)` of `claude_code_token_usage_tokens_total`, and
  `sum by (model)` of `claude_code_cost_usage_USD_total`, each
  `increase(<selector>[<range>] anchored)`. The token types stored are `input`,
  `output`, `cacheRead` and `cacheCreation` (every OTLP fixture), listed in that order.
- **Window.** From 5 minutes before the session's `started_at` (never before the epoch)
  to its last moment plus 2 minutes, the end of the range and the query's `time`: its
  end, or its last event once stale, or now while it is live, and never later than now.
  The 5 minutes cover a clock difference between the app and the machine that stamps
  the telemetry; the 2 minutes cover Claude Code's late exports. Nothing of the session
  comes before the window, so each series' first sample and the zero sample
  Prometheus stores at its start (ADR 0013) are inside it, and `anchored` needs no
  sample from before the window: no `lookback_delta` (ADR 0014, "Windows"). The range is
  whole seconds, rounded up, at least `1s`. The limits of this window are under
  "Limits" below.
- **Parsing.** Only a well-formed instant vector of finite, non-negative values, one
  series per label value, is accepted. An error status, another result type, `NaN`, an
  infinity, a negative value or a repeated label value makes the whole answer
  `invalid`: a total that silently dropped a series would be wrong with no sign of it.
  A series without the label is kept as "Unknown type" or "Unknown model".
- **The reader** (`ccobs.server.usage`). An `httpx.AsyncClient` created in the app's
  lifespan, on its event loop, and closed at shutdown; no proxy from the environment, no
  redirects followed. The three queries go out at once (`GET /api/v1/query` with
  `query` and `time`), each bounded by 3 s per phase and 3 s in all, its answer read as
  a stream and given up past 1 MiB. If any fails, the totals are unavailable, with the
  first failure's reason: never a partial total.
- **Cache.** Successful totals only, at most 64 sessions, least recently used first
  out, keyed by the session, its window's start and, once the window has passed (end
  plus 2 minutes at or before now), its end. A live window is kept 30 s, a final one
  5 minutes. A failure is not cached, so the next refresh retries. Tabs on one session
  share the cached totals, so they cost three queries per 30 s; only requests that
  arrive while the first one's queries are in flight ask again (no request coalescing,
  which a page refreshed once a minute does not need).
- **The read gate.** `GET /ui/sessions/{id}/usage` reads the session row alone
  (`Repository.find_session`) in one call on the read gate, which releases its slot
  before Prometheus is asked: a slow Prometheus holds no database connection and no
  worker thread.
- **The section.** The session page has a "Tokens and cost" section that loads the
  fragment with htmx on `load` and every 60 s (`hx-trigger="load, every 60s"`), not on
  live updates: the hooks that drive the change hub
  ([ADR 0032](0032-live-updates-change-hub-sse-pings-and-refetch.md)) say nothing about
  tokens, and Claude Code exports metrics about once a minute. It shows the total cost
  (four decimals under a dollar, two from a dollar up), the total tokens, a table by
  type and a table by model, with a note that the numbers can lag up to a minute.
  Every state is a 200, since htmx 2 does not swap a 4xx or 5xx by default: the
  values, "No usage recorded for this session yet", "Prometheus is unavailable, so
  tokens and cost can't be shown right now" (an error or a timeout), a message for
  values it can't read, and the read gate's busy or failed message. Only an unknown
  session is a 404, left unswapped like the other fragments' errors. Model names come
  from telemetry and are escaped like every value (autoescape).
- **Metric.** `ccobs_prometheus_queries_total{host, result}`, a counter of the queries
  sent, by `result`: `ok`, `error` (no connection, a non-2xx status, a redirect),
  `timeout` and `invalid` (too large, not JSON, not a valid vector). Every value is
  exported, with 0 when empty, as ADR 0028 requires: the app now exports 71 series per
  host (67 before). Totals served from the cache count nothing.
- **Logs** hold exception type names, status codes and reasons only, never the URL, a
  session ID or a body.

### Links to Grafana

- **`CCOBS_GRAFANA_URL`**, Grafana as the browser opens it. Outside compose it defaults
  to `http://localhost:4330`. compose derives it from `CCOBS_GRAFANA_PORT` as
  `http://localhost:${CCOBS_GRAFANA_PORT:-4330}`, the same value as Grafana's
  `GF_SERVER_ROOT_URL`, and `.env` does not offer it: Grafana's `enforce_domain`
  redirects any other host name (`127.0.0.1` included) to its root URL, so a link to any
  other URL would lose its path and query in the redirect. The app accepts only `http`
  or `https` on the host name `localhost`, with an optional port (1 to 65535) and path,
  no user, query or fragment, and only characters that need no escaping in an `href`.
- **Anatomy** (`ccobs.domain.grafana_links`). The datasource UIDs `prometheus` and
  `loki` (provisioned, ADR 0017) and the dashboard UID `claude-code-overview` with its
  `host` variable (the links leave `model` at All). Explore links follow Grafana 13.2's
  schema, `/explore?schemaVersion=1&panes=<JSON>&orgId=1`, ASCII-only JSON,
  percent-encoded. Every value in a query is quoted with `telemetry.quote`.
  - **Logs**: one Loki pane,
    `{service_name="claude-code", host="<host>"} | session_id="<id>"`.
  - **Tokens and cost**: two Prometheus panes, the session's tokens by type and its
    cost by model, each on its own scale, in the dashboard's per-interval form
    `sum by (<label>) (increase(<selector>[$__interval] anchored offset -$__interval))`
    with the dashboard's minimum interval `1m` and 100 data points, so the chart has the
    dashboard's steps. Not `total_query`: Explore draws a series over time, and a total
    over the whole range is one number, which the page already shows.
  - **Dashboard**: the overview for the app's host label over the session's range; the
    page header links it over its own default range.
- **Range.** From 1 minute before the session started to 2 minutes after its end, or
  after its last event once stale (it gets no more); for a live session the range ends
  at `now`, so Grafana keeps it growing.
- **Markup.** Plain anchors with `target="_blank"` and `rel="noopener noreferrer"`, so
  Grafana gets neither a handle on the app's window nor its URL. They sit outside the
  regions the live refresh replaces, and carry no htmx attribute.
- **Grafana's "Sign in" button** stays as it is (product decision P7): there are no
  accounts behind it (ADR 0017), and `docs/stack.md` says so.

### Session IDs in the stores' logs

- A **Logs** click puts the session ID in Loki's query log, which records every query at
  `info`. That query is user-initiated, like any query typed in Grafana, so ADR 0029's
  rule, which covers the app's automatic queries, still holds; `docs/app.md` says it.
- The **automatic** usage queries carry the session ID to Prometheus. Prometheus logs
  queries only to its query log, which is off unless `global.query_log_file` is set
  ("This is disabled by default",
  [query log docs](https://prometheus.io/docs/guides/query-log/)), and
  `stack/prometheus/prometheus.yaml` does not set it. So no session ID reaches a log,
  and ADR 0029's reasoning holds for Prometheus too. Setting `query_log_file` would
  change that and needs a new decision.

### Limits

- **Exact within ADR 0013's limits.** The totals are those of
  `increase(... anchored)`, exact in every case ADR 0013's spike ran, with the limit
  that ADR records under "Temporality": with delta temporality set outside `ccobs setup`,
  a collector restart while the session runs can lose part of a series' total (the new
  stream's zero sample can be dropped as a duplicate). With cumulative temporality, the
  `ccobs setup` default, the limit does not apply.
- **A telemetry clock ahead of the app's.** The window of an ended or stale session ends
  2 minutes after its last moment by the app's clock, and the last export can come up to
  about a minute after that moment (Claude Code's export interval). Samples stamped by a
  clock more than about a minute ahead of the app's fall after the window's end and are
  left out, so the last export of the session is missing. A live session's window ends
  at the app's now, so samples stamped in its future show up only once now reaches them.
  The 5 minutes before the start cover a clock behind the app's, not ahead.
- **Missed first hooks.** The window starts from the session's `started_at`, the first
  hook the app stored. If the app missed the session's first hooks (it was down, or
  dropped them, while the collector was up and received the metrics), `started_at` is
  later than the session's real start: samples from more than 5 minutes before it fall
  before the window, `anchored` takes the window's starting value from them (or from the
  first sample inside it), and the totals undercount what came before.

### Consequences

- Good, because the page shows ADR 0013's totals, from the same queries and names the
  dashboard and ADR 0014 use, with no new pipeline or table; exact within the limits
  above.
- Good, because Prometheus is optional for the page: down, slow or wrong, it costs one
  message in one section and at most 3 s on a request that holds no database slot.
- Good, because the cache bounds the load to about three queries per session per 30 s,
  and per 5 minutes once the session is over.
- Bad, because the numbers lag up to a minute (Claude Code's export interval) plus the
  30 s cache; the page says so.
- Bad, because a session whose metrics are off (`OTEL_METRICS_EXPORTER` unset, or
  `OTEL_METRICS_INCLUDE_SESSION_ID=false` set outside `ccobs setup`) shows "No usage
  recorded" with no way to tell why; `docs/app.md` lists the causes.
- Bad, because a telemetry clock more than about a minute ahead of the app's, or first
  hooks the app missed, make the totals of a session lower than its real usage (see
  "Limits"); the Grafana link over the same session shows the series to compare.
- Neutral, because the overview's strips still show no totals (P1); milestone 6 may add
  them, with a query for many sessions at once.

### Confirmation

- `tests/test_domain_usage.py`: the window (live, ended, just ended, stale-like, the cap
  at now, rounding, the epoch), the exact queries and their quoting, the parser on
  valid, empty and malformed answers (`NaN`, infinities, negatives, wrong shapes,
  repeated labels), sorting and formatting.
- `tests/server/test_usage.py`: the requests against an `httpx.MockTransport` (path,
  `query`, `time`), each failure and its counted result, the size cap, the whole-query
  timeout, the cache (TTL, final windows, LRU eviction, no failure cached) and logs
  without a URL, session ID or body.
- `tests/server/test_ui_usage.py`: the section's `hx-get` with `load, every 60s` and no
  live trigger, every state with 200, escaping of model names, the window per session
  status, one read per request with no slot held while Prometheus answers, the counter,
  and the guards.
- `tests/server/test_config.py`, `tests/test_compose.py` and
  `tests/server/test_metrics.py`: the setting, compose's environment and `.env.example`,
  and the 71 series.
- `tests/test_domain_grafana_links.py` and `tests/server/test_ui_session.py`: the links'
  UIDs, panes, queries and ranges against the provisioned files, and their markup.

## More information

- [Prometheus HTTP API, instant queries](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries)
- [Prometheus query log](https://prometheus.io/docs/guides/query-log/)
- [htmx `hx-trigger` polling](https://htmx.org/attributes/hx-trigger/) and
  [response handling](https://htmx.org/docs/#response-handling)
- Extends ADRs 0013, 0014, 0017 and 0028. Revisit when the overview shows totals
  (milestone 6), or if Prometheus's query log is turned on.
