---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Loki receives events over OTLP, with session fields as structured metadata

## Context and problem statement

[ADR 0003](0003-observability-backends.md) routes Claude Code's events (OpenTelemetry
log records) from the collector to Loki. In the fixtures of
[ADR 0012](0012-observed-telemetry-and-hook-contract.md), each record has the body
`claude_code.<event.name>`, no severity, and up to 28 attributes, among them
`session.id`, `event.name`, `event.sequence`, `prompt.id` and, on 324 of the 696
records, a trace and span ID. The resource carries `service.name` (`claude-code`),
`service.version`, `host.arch`, `os.type` and `os.version`. Loki indexes only stream
labels: each distinct label set is a stream, and the docs warn that high-cardinality
labels such as IDs make Loki build a huge index and flush many tiny chunks
([cardinality](https://grafana.com/docs/loki/latest/get-started/labels/cardinality/)).
The OpenTelemetry Collector contrib 0.162.0 no longer has a `loki` exporter: its
`components` list shows only a `loki` receiver. How should events reach Loki, and which
of their fields become index labels?

## Decision drivers

- Standard components, configured rather than coded (ADR 0003).
- A small index, so that single-binary Loki stays light on a laptop.
- Queries by session, event, prompt and trace work: the app's session view (milestone 3)
  and Grafana's links between logs and traces (milestone 6).
- Every event is stored: none dropped or merged on the way.
- The privacy filters of [ADR 0021](0021-collector-privacy-filters.md) run before
  anything leaves the collector.
- Retention can be configured (ADR 0003).

## Considered options

- **`otlp_http` to Loki's native OTLP endpoint.** Loki 3 receives OTLP at
  `/otlp/v1/logs`, turns chosen resource attributes into index labels and stores
  everything else as structured metadata: key-value pairs attached to each entry,
  neither indexed nor part of the log line
  ([structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/)).
- **The collector's `loki` exporter.** It mapped attributes to labels through hints,
  but it is not in contrib 0.162.0; using it would mean pinning an old collector.
- **Promoting `event.name` to an index label.** The fixtures have 14 event names, a
  low cardinality, and `{event_name="api_request"}` would select faster. But Loki's OTLP
  configuration indexes only resource attributes, so the collector would have to move a
  log record attribute onto the resource (splitting each export into one resource per
  event name) with an extra processor. A `session.id` label would be unbounded and is
  not considered.

## Decision outcome

Chosen option: "`otlp_http` to Loki's native OTLP endpoint", because it uses standard
OTLP on both ends, keeps the index to one stream per machine, and still lets every
session, event, prompt and trace field be queried. The removed exporter is not
available, and an `event.name` label would add a processor for a speed-up that one
machine's volume does not need.

- **Collector.** The logs pipeline runs `memory_limiter` and the privacy processors of
  ADR 0021, then exports with `otlp_http/loki` to `http://loki:3100/otlp` (the exporter
  appends `/v1/logs`), in plain HTTP on the internal `backend` network
  ([ADR 0018](0018-ports-networks-and-exposure.md)), where Loki runs single-tenant
  without authentication. Its in-memory queue holds at most 1000 requests and never
  blocks the receiver; a request that keeps failing is retried for 5 minutes, then
  dropped with an error in the collector's logs. The collector has no `depends_on`: it
  accepts OTLP as soon as it starts, and the queue rides out Loki's start (about 16 s,
  since `/ready` waits 15 s after the ingester is ready).
- **Loki 3.7.8**, single binary and single tenant (`auth_enabled: false`), with an
  in-memory ring, replication factor 1, schema v13 on TSDB with a 24h index period and
  filesystem storage in the `loki-data` volume at `/loki`, close to the upstream local
  example. `analytics.reporting_enabled: false` (ADR 0006). Stream sharding is off: it
  splits streams above 1.5 MB/s, which one machine never reaches, and while it is on the
  distributor logs an "empty ring" error at every start. The ruler stays in the `all`
  target: keeping it is simpler than listing every other module in `-target`, and with
  no rules it does nothing.
- **Index labels.** `limits_config.otlp_config.resource_attributes` has
  `ignore_defaults: true`, which drops Loki's default list of promoted attributes
  (`k8s.*`, `cloud.*` and others), and one `index_label` rule for `service.name` and
  `host`. `service.name` becomes `service_name="claude-code"`. `host`, the stack's own
  label of [ADR 0001](0001-local-single-machine-scope.md), is set by the collector from
  configuration in a later stage of this milestone; until then it is absent.
- **Structured metadata: everything else**, with dots turned into underscores. The
  integration test observed, through the collector:
    - session and event fields: `session_id`, `event_name`, `event_sequence`,
      `event_timestamp` and `prompt_id`;
    - trace context: `trace_id` and `span_id` (lowercase hex, as sent) and `flags`;
    - the other event attributes under their own names, such as `tool_name`, `model`,
      `cost_usd`, `input_tokens`, `duration_ms`, `decision`, `hook_event` and
      `status_code`;
    - the resource attributes that are not labels: `service_version`, `host_arch`,
      `os_type` and `os_version`;
    - fields Loki adds: `scope_name`, `scope_version`, `observed_timestamp` and
      `detected_level`, which is `unknown` because Claude Code sends no severity.

  No identity or content key reaches Loki (ADR 0021). `session_id` can never become a
  label: Loki's OTLP configuration indexes only resource attributes, and `session.id` is
  a log record attribute.
- **Queries** select the stream, then filter on structured metadata, for example
  `{service_name="claude-code"} | session_id="<id>"` or
  `{service_name="claude-code"} | session_id="<id>" | event_name="tool_result"`, always
  with a bounded time range. In the query API, structured metadata comes back merged
  into the stream labels unless the request sends
  `X-Loki-Response-Encoding-Flags: categorize-labels`
  ([HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)); the app's
  queries (milestone 2 on) send it.
- **No merged events.** The fixtures hold records that share their timestamp and body,
  up to 25 `hook_registered` records in one millisecond. Loki 3.7.8 kept every one (696
  of 696 records across the 8 fixtures), since their attributes differ.
- **Retention** is applied by the compactor (`retention_enabled: true`, with the
  filesystem `delete_request_store` it requires), for the period that
  [ADR 0016](0016-cardinality-and-retention.md) sets.

### Consequences

- Good, because the path is standard OTLP end to end, with no exporter of our own and
  the OpenTelemetry field names kept (with underscores).
- Good, because one stream per machine keeps the index tiny: sessions, prompts and
  traces add structured metadata, not streams.
- Good, because `trace_id` and `span_id` on each event let Grafana link events and
  Tempo traces (milestone 6).
- Bad, because a query by session reads every chunk of the stream in its time range and
  filters it, instead of reading the index. At one machine's volume (about 100 events
  per session) this is fast, but queries must bound their time range, for example to
  the session's start and end.
- Bad, because API clients must send the `categorize-labels` header to tell labels from
  structured metadata; without it, each distinct metadata set comes back as a stream of
  its own.
- Bad, because Loki limits each entry's structured metadata to 64 KB and 128 entries
  (`max_structured_metadata_size`, `max_structured_metadata_entries_count`) and rejects
  an entry over the limit. Metadata-only events are far below it (the largest fixture
  record has about 1.3 KB of attributes), but with the content opt-in of milestone 4 a
  long prompt or tool output would arrive as an attribute. Milestone 4 must keep long
  content out of structured metadata, for example by moving it into the log line
  (limited to 256 KB by `max_line_size`) or truncating it.
- Bad, because the exporter's queue is in memory: a collector restart loses what it
  holds, and events that cannot reach Loki for 5 minutes are dropped.
- Neutral, because `detected_level` is always `unknown`; levels, if needed, come from
  the event name (`api_error`).

### Confirmation

- `tests/test_loki_config.py` checks schema v13 on TSDB with a 24h index, single-tenant
  mode, `analytics.reporting_enabled: false`, stream sharding off, retention from
  `CCOBS_LOKI_RETENTION` with compactor retention on, `ignore_defaults: true` with
  exactly `service.name` and `host` as index labels and no other OTLP rule, structured
  metadata allowed, and every data path under the mounted `/loki` volume.
- `tests/test_collector_config.py` checks that the logs pipeline exports only to
  `otlp_http/loki` at `http://loki:3100/otlp`, matching the compose service and Loki's
  port, with no credentials or TLS settings, a bounded queue that never blocks and
  retries that end; `tests/test_compose.py` checks the pinned image, the `-health`
  healthcheck, the memory settings, the named volume, the debug publishing and that the
  collector does not wait for Loki.
- `tests/integration/test_stack_logs.py` replays every fixture scenario with fresh
  session IDs and checks that each session's records are all stored; that the only
  index labels are `service_name` (and later `host`); that `session_id`, `event_name`,
  `event_sequence`, `event_timestamp`, `prompt_id`, `trace_id` and `span_id` come back
  as structured metadata, with the trace and span IDs that were sent and no identity or
  content keys; that LogQL filters on them; that events sent while Loki is stopped are
  stored once it is back, from the collector's queue; that Loki's `/config` has 30 days
  of retention; and that Loki logs no errors.
- `tests/integration/test_stack_no_outbound.py` reads usage reporting off from Loki's
  `/config` and from Tempo's `/status/config`.
- `loki -config.expand-env=true -verify-config` and the collector's `validate` run on
  the bundled files in the CI `stack` job (a later stage of this milestone). Until
  then, `docker compose up --wait` in the integration tests already fails when either
  service rejects its config.
- The `reviewer` subagent checks that no change adds an index label or a `drop` rule for
  log record attributes.

## More information

- Loki: [OTLP ingestion](https://grafana.com/docs/loki/latest/send-data/otel/),
  [structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/),
  [cardinality](https://grafana.com/docs/loki/latest/get-started/labels/cardinality/),
  [retention](https://grafana.com/docs/loki/latest/operations/storage/retention/) and
  the [configuration reference](https://grafana.com/docs/loki/latest/configure/)
  (`limits_config`, `compactor`, `analytics`).
- OpenTelemetry Collector:
  [`otlp_http` exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)
  and the
  [exporter helper](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md)
  (`sending_queue`, `retry_on_failure`).
- At `info` level, Loki logs every query with its LogQL text, session IDs included;
  milestone 2, when the app starts querying, decides whether to lower the level.
- Revisit if Loki's OTLP mapping changes, if a stream label becomes necessary (for
  example, different retention per event type, which Loki selects by labels), or when
  milestone 4 designs the content opt-in.
