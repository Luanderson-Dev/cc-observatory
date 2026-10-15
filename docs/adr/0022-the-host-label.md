---
status: accepted
date: 2026-10-14
decision-makers: Luanderson-Dev (maintainer)
---

# The host label: one configured value, set by the collector on every signal

## Context and problem statement

[ADR 0001](0001-local-single-machine-scope.md) keeps a `host` label on stored data, so
that a future central mode could tell machines apart, and leaves its value, and whether
telemetry carries it, to milestones 1 and 2.
[ADR 0015](0015-loki-through-its-otlp-endpoint.md) already makes `host` one of Loki's
two index labels, and
[ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md) keeps it as the only
resource attribute Prometheus may promote, but nothing sets it. Claude Code sends no
such attribute: the resource of the fixtures has `service.name`, `service.version`,
`host.arch`, `os.type` and `os.version`
([ADR 0012](0012-observed-telemetry-and-hook-contract.md)). The obvious candidate,
`host.name`, is the machine's name, which
[ADR 0021](0021-collector-privacy-filters.md) deletes as personal data. And users can
add attributes through `OTEL_RESOURCE_ATTRIBUTES`, which Claude Code copies onto every
data point and event as well. Where does the value come from, which component sets it,
and how does it reach each backend?

## Decision drivers

- Privacy by default: no hostname or other personal data is stored unless the user asks
  for it ([ADR 0006](0006-privacy-metadata-only-by-default.md), ADR 0021).
- Every signal carries the label: Prometheus series, Loki streams and Tempo traces.
- One value per machine, the same whatever a client sends, with no setup in Claude Code.
- Configured rather than coded, like the stack's other settings
  ([ADR 0020](0020-compose-conventions.md)).
- Stored in each backend's own storage, so queries and dashboards can use it now.

## Considered options

- **A constant, `local`, that `CCOBS_HOST_LABEL` overrides, set by the collector.** One
  setting labels all three signals; a single machine sees a label that tells nothing
  apart until the user names it.
- **A random ID per installation.** Tells machines apart with no setup, but needs state
  that survives reinstalls and a tool to create it (`ccobs setup`, milestone 4), and
  means nothing to a person reading a dashboard.
- **The machine's hostname.** Readable and automatic, but personal data (ADR 0021), and
  inside a container the collector sees its container's name, not the machine's. Kept
  only as an explicit opt-in: a user who wants it sets `CCOBS_HOST_LABEL` to it.
- **A value each client sends through `OTEL_RESOURCE_ATTRIBUTES`.** No stack change, but
  every client (each shell, IDE and CI job) must set it, sessions without it have no
  label, and Claude Code copies the attribute onto every data point and event, where it
  would become a second label and structured metadata.
- **Prometheus `external_labels`.** One setting, but Prometheus adds external labels
  only "when communicating with external systems (federation, remote storage,
  Alertmanager)", never to its local storage, so stored series and queries would not
  have it; and it covers Prometheus only.

## Decision outcome

Chosen option: "A constant, `local`, that `CCOBS_HOST_LABEL` overrides, set by the
collector", because it labels all three signals in one place with no setup in Claude
Code, stores no personal data by default, and leaves a meaningful name to the user. A
random ID needs state and tooling for no gain on one machine, the hostname is personal
data, a value sent by clients depends on every client, and external labels are not
stored.

- **Value.** `CCOBS_HOST_LABEL`, default `local`. `compose.yaml` passes
  `${CCOBS_HOST_LABEL:-local}` to the collector, whose config reads
  `${env:CCOBS_HOST_LABEL:-local}`; that default applies when `CCOBS_HOST_LABEL` is
  unset, as in the CI check of its config (a variable set to an empty value expands to
  an empty string there, unlike compose's `:-`). `.env.example` lists the variable with
  the same default, and `ccobs.domain.telemetry` holds it as `DEFAULT_HOST`, with the
  label's name as `HOST_LABEL`; tests keep the four values equal. Letters, digits, `.`,
  `_` and `-` are the supported set. The stack does not validate the value: it goes into
  an OTTL string literal, so a double quote or a backslash can stop the collector at
  start-up or change the value (an escape such as `\t` becomes a tab). `ccobs setup`
  and `ccobs doctor` (milestone 4) can validate it.
- **Collector.** A `transform/host` processor runs in every pipeline right after the
  privacy processors of ADR 0021: `memory_limiter`, `transform/identity`, the content
  processors, `transform/host`, then anything else, such as `delta_to_cumulative`. So
  `host.name` stays deleted, and the new step touches only `host`. It sets the resource
  attribute `host` (OTTL `set`, which replaces a value the client sent) and deletes
  `host` from data points, log records, spans and span events, where Claude Code copies
  `OTEL_RESOURCE_ATTRIBUTES`, so the resource's value is the only one. Unlike the
  privacy processors it has `error_mode: ignore`: a statement error lets the batch
  through, with the error logged, instead of losing it.
- **Prometheus.** `otlp.promote_resource_attributes: [host]`, the only promoted
  attribute (ADR 0013), so every series of Claude Code's metrics has `host="local"`.
  The promoted attribute also stays on `target_info`. The series of the `prometheus`
  and `otel-collector` scrape jobs, the stack's own metrics, have no `host` label.
- **Loki.** `host` is the second index label (ADR 0015): one stream per machine,
  `{service_name="claude-code", host="local"}`.
- **Tempo.** `host` is a resource attribute of every span, which TraceQL selects with
  `{ resource.host = "local" }`.
- **The app** (milestone 2) uses the same variable and default for the `host` label of
  its `/metrics` series and of its database rows
  ([ADR 0005](0005-sqlite-for-app-state.md)); milestone 2 records how.

### Consequences

- Good, because a central mode, if it ever comes, can tell machines apart by a label
  that every signal already carries, and dashboards can have a `host` variable from the
  start (milestone 6).
- Good, because no hostname is stored by default, and a client can neither change nor
  split the label: the collector's value replaces whatever arrives.
- Good, because a readable name is one variable in `.env`.
- Bad, because a single-machine user sees `host="local"` on every series, stream and
  trace, a label that tells nothing apart.
- Bad, because changing `CCOBS_HOST_LABEL` starts new series and a new Loki stream, so a
  query over a period that spans the change must aggregate without `host`, as totals
  (`sum by (...)`) already do.
- Bad, because a value with a double quote or a backslash can stop the collector with
  an OTTL error at start or silently change the value, until milestone 4 validates it.
- Neutral, because a `host` that a user puts in `OTEL_RESOURCE_ATTRIBUTES` is dropped
  from data points and records, and the resource keeps the collector's value.
- Neutral, because the label costs one short label per series and stream, the cost that
  ADR 0001 accepted.

### Confirmation

- `tests/test_collector_config.py` checks that every pipeline runs `transform/host`
  right after the privacy processors and before any other processor, and that it sets
  exactly the resource `host` from `${env:CCOBS_HOST_LABEL:-local}` and deletes `host`
  from data points, log records, spans and span events, with no other statement.
  `tests/test_compose.py` checks that compose passes the variable to the collector only,
  with the default, and that `.env.example` lists and documents it with the default of
  every config; `tests/test_prometheus_config.py` that Prometheus promotes exactly
  `[host]` and sets no external labels; `tests/test_loki_config.py` that `service.name`
  and `host` are Loki's index labels; and `tests/test_domain_telemetry.py` that
  `ccobs.domain.telemetry` lists `host` on every metric, on `target_info` and among
  Loki's index labels, with the default and the configs above.
- `tests/integration/test_host_label.py` replays the fixtures and finds `host="local"`
  on every Claude Code series of the run, on every Loki entry as an index label, and on
  every Tempo trace as a resource attribute that `{ resource.host = "local" }` finds.
  It also replays a scenario with a client's own `host` and `host.name` on every
  resource, data point, log record, span and span event, and finds only
  `host="local"`, no `host.name`, and the same series as without them.
  `tests/integration/test_metric_names.py` compares the stored labels, `target_info`'s
  included, with the domain's.
- The `reviewer` subagent checks that no change takes the label from the hostname or
  from an attribute a client sends.

## More information

- Prometheus: [OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)
  (`promote_resource_attributes`) and the
  [configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
  (`external_labels`).
- OpenTelemetry Collector:
  [transform processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/transformprocessor/README.md)
  (`error_mode`), [OTTL functions](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/pkg/ottl/ottlfuncs/README.md)
  (`set`, `delete_key`) and
  [configuration](https://opentelemetry.io/docs/collector/configuration/#environment-variables)
  (`${env:NAME}`).
- Tempo: [TraceQL](https://grafana.com/docs/tempo/latest/traceql/) (the `resource.`
  scope).
- Revisit if a central mode is planned (it needs its own ADR, as ADR 0001 says), or if
  milestone 4 decides that `ccobs setup` writes a value other than `local`.
