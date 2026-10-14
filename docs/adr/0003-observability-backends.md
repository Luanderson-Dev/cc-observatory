---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# OpenTelemetry Collector, Prometheus, Loki, Tempo and Grafana as the backends

## Context and problem statement

Claude Code exports metrics, events (OpenTelemetry log records) and, in beta, traces over
OTLP ([ADR 0002](0002-native-telemetry-plus-hooks.md)). Something has to receive them,
remove personal attributes ([ADR 0006](0006-privacy-metadata-only-by-default.md)), store
each signal, answer the app's queries and let the user explore the data. Which backends
should the bundled stack run?

## Decision drivers

- Open source, self-hosted and able to run offline on one machine
  ([ADR 0001](0001-local-single-machine-scope.md)).
- One OTLP entry point, with a place to transform data before it is stored.
- A query API the app can call over HTTP, and a UI for deeper analysis.
- Modest memory use, and control over each component: version, limits, optional parts.
- Widely used components with good documentation.

## Considered options

- **Collector, Prometheus, Loki, Tempo and Grafana as separate services.** The common
  open-source choice for each signal; five services to configure, pin and update.
- **The Grafana LGTM all-in-one image (`grafana/otel-lgtm`).** The same components in
  one container and the quickest start, but upstream intends it for development, demo
  and testing, and a single container cannot have per-component limits, versions or
  healthchecks, or leave Tempo out.
- **VictoriaMetrics and VictoriaLogs.** Lighter on memory and disk, with a smaller
  ecosystem. VictoriaMetrics works with Grafana's Prometheus datasource, but VictoriaLogs
  needs a datasource plugin, downloaded at startup unless bundled.
- **A ClickHouse-based stack, such as SigNoz.** One store and one UI for every signal,
  but much heavier for a laptop, and its UI would overlap with the app's.
- **App-only storage, no stack.** The app receives OTLP and stores everything itself.
  Fewest moving parts, but we would rebuild a time-series store, a log store and charts.

## Decision outcome

Chosen option: "Collector, Prometheus, Loki, Tempo and Grafana as separate services",
because it meets every driver with standard components and keeps each one under our
control.

- The OpenTelemetry Collector (contrib distribution) is the single OTLP entry point, on
  4317 (gRPC) and 4318 (HTTP). It deletes personal attributes, drops content while
  content capture is off (ADR 0006), and routes metrics to Prometheus, events to Loki
  and traces to Tempo.
- Tempo runs only in the compose profile `traces`, because tracing is a beta feature in
  Claude Code and is off by default.
- Grafana starts with provisioned datasources (Prometheus, Loki, Tempo) and dashboards.
- Every service has an image pinned to an exact tag, a memory limit and a healthcheck;
  data volumes are named, and configuration files are mounted read-only. Retention is
  configurable.
- The target is under about 1.5 GB of RAM for the full stack, measured and documented in
  milestone 1.

### Consequences

- Good, because each signal lands in a store built for it, queryable over HTTP with
  PromQL and LogQL, and Grafana covers deeper analysis.
- Good, because the collector is the one place that drops personal attributes and
  content from telemetry before anything is stored.
- Bad, because five services mean five configurations to validate and five images to
  keep up to date.
- Bad, because some upstream images ship without a shell or an HTTP client, so each
  healthcheck needs per-image care.
- Neutral, because the 1.5 GB target is an estimate until milestone 1 measures and
  documents it.

### Confirmation

- `docker compose config --quiet`, and each tool's own config checker where there is
  one, validate the bundled files (milestone 1).
- A test over `compose.yaml` (milestone 1) checks that every service has an exact image
  tag, a memory limit and a healthcheck, that data volumes are named and configuration
  files are mounted read-only, and that Tempo is in the `traces` profile.
- Integration tests (`@pytest.mark.integration`, milestone 1) send OTLP to the collector
  and read the data back from Prometheus and Loki.

## More information

Decided in milestone 1, from real data:

- how metrics reach Prometheus: the collector's `prometheus` exporter scraped by
  Prometheus, remote write, or Prometheus's native OTLP receiver;
- temporality: Claude Code exports delta by default and has no default OTLP protocol,
  while Prometheus stores cumulative series, so either `ccobs setup` asks for cumulative
  or the collector converts;
- the metric names as they reach Prometheus (dots become underscores, and unit and type
  suffixes may be added), which code and dashboards then use;
- the exact image tags and the measured memory use.
