---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Metrics reach Prometheus through its OTLP receiver, made cumulative in the collector

## Context and problem statement

[ADR 0003](0003-observability-backends.md) left two details to milestone 1: how metrics
reach Prometheus, and how Claude Code's temporality meets Prometheus's cumulative
series. Claude Code exports its metrics as monotonic sums with delta temporality unless
told otherwise (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`, "default: `delta`",
in the [monitoring docs](https://code.claude.com/docs/en/monitoring-usage)), and the
fixtures of [ADR 0012](0012-observed-telemetry-and-hook-contract.md) show three things
that matter here. Consecutive deltas of a series are often not contiguous: of 80 pairs,
38 started after the previous point ended, most 6 to 13 s later and, in the interactive
sessions, up to 116 s later. Each export carries only the series that changed. And many
short sessions export once. The app needs exact totals of cost and tokens, per session
and across sessions, from PromQL queries over HTTP. A spike on 2026-10-13 replayed the 8
fixtures that carry telemetry (84 series) with `tools/stack/replay.py` through 36
throwaway stacks, comparing what Prometheus returned with the replayer's expected
totals. Which path should metrics take from the collector to Prometheus, in which
temporality, and which PromQL gives exact totals?

## Decision drivers

- Exact totals for cost and tokens, per session and across sessions, with no
  extrapolation.
- Works with any Claude Code configuration, including delta temporality set outside
  `ccobs setup`, as with the content flags of
  [ADR 0006](0006-privacy-metadata-only-by-default.md).
- Survives collector and Prometheus restarts and long idle times: interactive sessions
  spend minutes or hours waiting on a person.
- Few moving parts and little state, observable where it exists.
- Standard components, configured rather than coded (ADR 0003).

## Considered options

The spike ran each option against the same criteria: 8 sessions replayed concurrently,
a collector restart in the middle of a session, an idle gap longer than the conversion
state's expiry, a Prometheus restart, and windows that start mid-session.

- **A1: the collector's `prometheus` exporter, scraped by Prometheus.** The exporter
  converts delta itself, but adds a point to the running total only when its start time
  equals the previous point's end; otherwise it replaces the total. With Claude Code's
  gaps, 22 of the 84 series ended with a wrong value, and the session with the most
  exports (`interactive-permission-compact`, 9) stored 34 % of its cost, 41 % of its
  tokens and 31 % of its active time.
- **A2: `delta_to_cumulative` in the collector, then the `prometheus` exporter,
  scraped.** In the concurrent replay, last values were exact, and totals with
  `increase(... anchored)` were exact only with the zero sample described below. In
  compressed replays, a collector restart or an expired stream then left 5 of 13
  series wrong; the restart replayed on the wall clock was exact. State lives in two
  places (the processor, and the exporter's `metric_expiration`, 5 minutes by default)
  and results depend on scrape timing. It also adds `instance` (`collector:8889`),
  `otel_scope_name` and `otel_scope_version` labels, and no `target_info` series
  appeared.
- **C1: `delta_to_cumulative` in the collector, then `otlp_http` to Prometheus's native
  OTLP receiver.** A push path with no scrape; the conversion state, when there is any,
  sits in one processor that can be configured and observed. Totals were exact in every
  criterion with the zero sample and `anchored`. It needs the receiver flag, two
  experimental feature flags and an out-of-order window.
- **C2: delta forwarded to Prometheus, converted by its `otlp-deltatocumulative`
  feature.** No processor in the collector, but the state lives in Prometheus, which
  uses the processor "using its default settings"
  ([feature flags](https://prometheus.io/docs/prometheus/latest/feature_flags/)): a
  5-minute `max_stale` that cannot be changed, and state lost on every restart. Totals
  with `increase(... anchored)` stayed exact, since each loss is a counter reset, but
  the per-session last value was wrong after a 380 s idle gap and after a Prometheus
  restart, and it takes one more experimental flag.
- **C3: native delta ingestion (`otlp-native-delta-ingestion`).** Stores the deltas as
  they arrive; `sum_over_time` was exact in every test, with no state anywhere. But the
  feature is experimental and "in a very early stage": "the StartTimeUnixNano field is
  ignored, and deltas are given the unknown metric metadata type", and `rate()` and
  `increase()` "will produce incorrect results" (feature flags page), so every query
  and dashboard would need delta-specific PromQL. Considered, not chosen.
- **Remote write (the `prometheus_remote_write` exporter).** "Non-cumulative monotonic,
  histogram, and summary OTLP metrics are dropped by this exporter"
  ([README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/prometheusremotewriteexporter/README.md)),
  so it needs `delta_to_cumulative` as well, and the native OTLP receiver gives the
  same push path with Prometheus's own translation. Not tested.

## Decision outcome

Chosen option: "C1: `delta_to_cumulative` in the collector, then `otlp_http` to
Prometheus's native OTLP receiver", because it is the only option that was exact in
every criterion of the spike (see the limit under Temporality) while keeping state in
one configurable, observable place and using standard components. A1 stores wrong
totals, A2 depends on scrape timing and keeps state twice, C2 keeps state that cannot be
configured and loses it on restarts and idle gaps, C3 is too early and changes every
query, and remote write needs the same processor for a longer path.

**Collector** (contrib 0.162.0):

- The metrics pipeline runs `memory_limiter` and the privacy processors of
  [ADR 0021](0021-collector-privacy-filters.md), then `delta_to_cumulative`, and
  exports with `otlp_http` over the `backend` network
  ([ADR 0018](0018-ports-networks-and-exposure.md)), with
  `metrics_endpoint: http://prometheus:9090/api/v1/otlp/v1/metrics` (the exporter
  appends `/v1/metrics` to a plain `endpoint`, so `endpoint` would be
  `http://prometheus:9090/api/v1/otlp`). These are the
  component names in 0.162.0; the older `deltatocumulative` and `otlphttp` are
  deprecated aliases.
- `delta_to_cumulative` has `max_stale: 24h`, so the streams of a session that waits on
  a person for hours keep their running totals; with the spike's 1-minute value, a
  130 s gap reset them, and with 24h the same gap did not. `max_streams` keeps its
  default (no practical limit): the fixtures have at most 18 series per session. The
  state shows in `otelcol_deltatocumulative_streams_tracked` and the other
  `otelcol_deltatocumulative_*` metrics.
- Cumulative points pass through `delta_to_cumulative` untouched (with cumulative input
  the spike saw no data points processed), so with `ccobs setup`'s settings it holds no
  state. It stays as the safety net for delta senders.
- The exporter keeps its defaults: gzip, retries and an in-memory sending queue. An
  HTTP 400 is permanent ("The client MUST NOT retry the request when it receives
  `HTTP 400 Bad Request`", [OTLP](https://opentelemetry.io/docs/specs/otlp/)), and the
  collector drops the request, which is why Prometheus needs the out-of-order window
  below.

**Prometheus:**

- `--web.enable-otlp-receiver`, which serves "OTLP metrics receiving on HTTP
  `/api/v1/otlp/v1/metrics` path"
  ([OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)).
- `--enable-feature=created-timestamp-zero-ingestion,promql-extended-range-selectors`.
  The first injects a 0-valued sample at each stream's start time (the zero sample), so
  a series' first value and every reset count in `increase()`. The second enables the
  `anchored` modifier, which uses real samples at both ends of the range with "no
  extrapolation or interpolation".
- `storage.tsdb.out_of_order_time_window: 30m`, the value the OTLP guide sets. It is
  required: concurrent sessions share one resource (Claude Code sends no
  `service.instance.id`), so they share one `target_info` series, and an out-of-order
  sample on it makes Prometheus answer 400 and roll back the whole request. Without the
  window, 8 concurrent sessions lost 18 of 28 requests (102 points), and one session
  lost its whole `session.count`.
- The `otlp` translation keeps its defaults: `translation_strategy`
  `UnderscoreEscapingWithSuffixes`, which gives classic names that PromQL needs no
  quotes for. No resource attribute is promoted to a label except the `host` label,
  which a later ADR of this milestone adds. `service.name` becomes `job="claude-code"`,
  and the other resource attributes stay on `target_info`. The names and labels as
  stored are recorded in a later ADR of this milestone (0014).

**Temporality** (applied by `ccobs setup`, milestone 4):

- `ccobs setup` sets `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`.
  The running total then lives in the Claude Code process, and C1 was exact in every
  criterion, last values included, through collector restarts, idle gaps and
  Prometheus restarts.
- `ccobs setup` never sets `OTEL_METRICS_INCLUDE_SESSION_ID=false` (the default is
  `true`). Without `session.id`, the cumulative streams of concurrent sessions collide
  on one series, which failed in the spike; with delta it only worked because the points
  arrived strictly in order.
- Delta stays supported for users who set it outside `ccobs setup`: totals remain exact
  through `delta_to_cumulative` and `anchored`, with one limit found while building the
  stack, after the spike. When the collector restarts and a series' next delta is
  contiguous (its start time equals the previous point's time, as in about half of
  Claude Code's consecutive deltas), the new stream's zero sample lands on the timestamp
  of the last stored sample and Prometheus drops it as a duplicate; if the new running
  total is not lower than the old one, `increase(... anchored)` loses the old total (a
  probe on 3.13.4 returned 7 where 12 was expected). Per-session last values also break
  after a collector restart or an idle gap beyond `max_stale`. Cumulative input, the
  `ccobs setup` default, is not affected.

**PromQL for totals:**

- Totals use `increase(x[R] anchored)`, summed by the labels needed, for example
  `sum by (session_id) (increase(claude_code_cost_usage_USD_total{session_id="<id>"}[R] anchored))`
  for one session and
  `sum by (model) (increase(claude_code_token_usage_tokens_total[R] anchored))` across
  sessions. They were exact for single-export sessions, collector restarts (counter
  resets; see the limit under Temporality), expired streams and Prometheus restarts.
- Never plain `increase()` or `rate()` for totals, and never `sum_over_time()` over these
  cumulative series. Plain `increase()` was exact in no test: with the zero sample it
  gave 1.5 times the truth for single-export sessions and 1.167 times for the session
  with the most exports; without it, nothing for a one-sample series and 0.80 to 1.19
  times for the others. `anchored` without the zero sample drops each series' first
  value.
- `anchored` takes the window's starting value from "the most recent sample (within the
  lookback delta)" before the window. Windows that start mid-session were exact (47 of
  47) when the series had a sample within the lookback (5 minutes by default) before the
  window's start. Otherwise the query passes `lookback_delta` on the query API (it
  worked on 3.13.4), or the deployment sets `--query.lookback-delta`.
- `last_over_time()` and `max_over_time()` per session are exact only while the series
  has no reset. With cumulative input and `session.id` they were exact in every test.
- The feature flags page says that with `anchored`, `increase()` results "are integers";
  fractional values came out exact (cost totals such as 0.0050783 USD).

### Consequences

- Good, because totals were exact in every scenario the spike ran: concurrent sessions
  with non-contiguous deltas, single-export sessions, collector and Prometheus restarts,
  expired streams and windows that start mid-session (with delta input, a collector
  restart has the limit under Temporality).
- Good, because it works whichever temporality Claude Code uses (with delta, within the
  limit under Temporality), and with the default of `ccobs setup` the only running
  total is in Claude Code itself.
- Good, because there is no scrape between the collector and Prometheus: no timing to
  tune, no second copy of the state, and no `instance` or `otel_scope_*` labels.
- Bad, because `created-timestamp-zero-ingestion` and `promql-extended-range-selectors`
  are experimental feature flags, which can change or go away, and the LTS line excludes
  experimental features from its fixes
  ([release cycle](https://prometheus.io/docs/introduction/release-cycle/)). The docs
  already present `st-storage` as "meant to be a replacement of
  `created-timestamp-zero-ingestion`". Mitigations: Prometheus 3.13 is the LTS line,
  supported until 2027-07-31; the integration test below catches a change on upgrade;
  and an exact fallback exists without either flag: the app reads the raw samples
  (`x[R]`) and sums the positive differences, treating a drop as a reset and a series'
  first sample as an increase from zero.
- Bad, because `delta_to_cumulative` is an alpha component. With `ccobs setup`'s
  cumulative setting it holds no state; it matters only for delta senders.
- Bad, because with delta input a collector restart can undercount a series whose next
  delta is contiguous (see Temporality). The cumulative default avoids it; `st-storage`
  is the candidate fix once it works with `anchored`.
- Bad, because the out-of-order window is a setting that must never be removed: without
  it, concurrent sessions lose data, and the loss shows only in metrics and logs (400
  responses in Prometheus, failed points in the collector's exporter).
- Bad, because Prometheus's OTLP receiver has no authentication. ADR 0018 keeps
  Prometheus on the internal `backend` network, unpublished except in the debug
  override.
- Bad, because queries are more delicate: totals must use `anchored`, the range must
  cover what is being summed, and windows that start after a long pause need
  `lookback_delta`. The app's queries and the Grafana dashboards (milestone 6) follow
  the same rules.
- Bad, because `/api/v1/metadata` returns nothing for metrics ingested over OTLP, so
  types, units and help texts come from ADR 0012 and the names ADR (0014), not from
  Prometheus.
- Neutral, because `created-timestamp-zero-ingestion` also changes the default
  `scrape_protocols` to negotiate `PrometheusProto` first for the targets Prometheus
  scrapes, such as the app's `/metrics` from milestone 2.
- Neutral, because one behaviour is still unobserved: that Claude Code in cumulative
  mode re-exports unchanged series every interval, as the OpenTelemetry JS SDK is
  expected to (the replayer's cumulative output is sparse). Milestone 4 confirms it with
  a real session; if it does not, more windows need `lookback_delta`.
- Neutral, because ADR 0003 stays in force; this ADR settles two of the details it left
  to milestone 1.

### Confirmation

- `tests/test_collector_config.py` (extended in a later stage of this milestone) checks
  that the metrics pipeline runs the privacy processors of ADR 0021 before
  `delta_to_cumulative`, that the processor has `max_stale: 24h`, and that metrics
  leave through an `otlp_http` exporter to the OTLP receiver, with no `prometheus`
  exporter.
- A static test (a later stage of this milestone) over `compose.yaml` and the
  Prometheus configuration under `stack/prometheus/` checks `--web.enable-otlp-receiver`,
  both feature names in `--enable-feature` and `out_of_order_time_window: 30m`.
  `promtool check config` validates the configuration inside the running Prometheus
  container, in `tests/integration/test_stack_metrics.py`, and the CI `stack` job will
  run it too, from a later stage of this milestone.
- An integration test in `tests/integration/` (stage 4 of this milestone) replays the
  fixtures through the stack and checks exact sums per series in the non-contiguous,
  concurrent scenario; exact `increase(... anchored)` for single-export sessions; exact
  totals after a collector restart in the middle of a replay, at a split where no
  continuing series is contiguous (a strict xfail test records the contiguous case);
  and that no identity label reaches Prometheus.
- The spike harness is not committed; its method and numbers are recorded here.
- The `reviewer` subagent checks that queries for totals use `anchored`, never plain
  `increase()`, `rate()` or `sum_over_time()`.

## More information

- Spike, 2026-10-13: OpenTelemetry Collector contrib 0.162.0; Prometheus 3.13.4 and
  3.15.0, which behaved identically; fixtures of Claude Code 2.1.296 (ADR 0012). 36
  automated runs replayed the 8 fixtures with telemetry, interleaved as 8 concurrent
  sessions, in compressed time and following the wall clock, with and without the zero
  sample, with delta and cumulative input, and without the out-of-order window.
- Start-timestamp storage (`st-storage` with `use-start-timestamps`) was tried as a
  variant of C1 in place of the zero sample: plain `increase()` was still wrong (4 of 84
  series exact), and `use-start-timestamps` "doesn't currently work with extended range
  selectors" (feature flags page). Revisit when it does, since it is meant to replace
  the zero sample.
- Prometheus: [OTLP guide](https://prometheus.io/docs/guides/opentelemetry/),
  [feature flags](https://prometheus.io/docs/prometheus/latest/feature_flags/),
  [HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
  (`lookback_delta`),
  [command-line flags](https://prometheus.io/docs/prometheus/latest/command-line/prometheus/)
  (`--query.lookback-delta`) and the
  [release cycle](https://prometheus.io/docs/introduction/release-cycle/) (LTS).
- OpenTelemetry Collector:
  [`delta_to_cumulative`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/deltatocumulativeprocessor/README.md)
  (`max_stale`, default `5m`; `max_streams`) and its
  [telemetry](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/deltatocumulativeprocessor/documentation.md),
  [`otlp_http`](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md),
  [`prometheus`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/prometheusexporter/README.md)
  (`metric_expiration`) and
  [`prometheus_remote_write`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/prometheusremotewriteexporter/README.md).
- Claude Code [monitoring docs](https://code.claude.com/docs/en/monitoring-usage):
  `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` ("Set to `cumulative` if your
  backend expects cumulative temporality") and `OTEL_METRICS_INCLUDE_SESSION_ID`
  (default `true`).
- Revisit when the two feature flags graduate, change or are removed; when native delta
  ingestion becomes stable; or when Claude Code changes its default temporality or
  starts sending `service.instance.id`.
