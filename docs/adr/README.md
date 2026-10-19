# Architecture decision records

An architecture decision record (ADR) captures one significant decision about how
cc-observatory is built: the problem, the options considered, the option chosen and its
consequences. The ADRs here follow the [MADR 4](https://adr.github.io/madr/) format.
[architecture.md](../architecture.md) gives the overview; when it and an ADR disagree,
the ADR wins.

## Rules

- **One decision per ADR.**
- **File name:** `NNNN-short-title.md`, with the next free four-digit number and a short,
  lowercase, hyphenated title.
- **Start from the template:** copy [template.md](template.md) and fill in every section.
  The Confirmation section names something checkable: a test, a CI job, a config rule or
  a reviewer checklist item.
- **Statuses:** `proposed`, `accepted`, `deprecated` (no longer applies and has no
  replacement) and `superseded by ADR-NNNN`.
- **Same PR as the code:** an ADR is sent in the pull request that implements the
  decision, and that PR also adds the ADR to the index below. An ADR may record a
  decision before its code exists; it then names the milestone that brings the code.
- **Accepted ADRs are never rewritten.** To change a decision, write a new ADR that
  supersedes the old one. The only change to the old ADR is its status line, which
  becomes `superseded by ADR-NNNN`, and its row in the index changes to match.
- **Details decided later:** when an ADR leaves a detail open, it names the milestone
  that decides it and, if it is an architecture decision, it gets its own ADR.

## Index

| ADR                                                                           | Decision                                                                              | Status   |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | -------- |
| [0001](0001-local-single-machine-scope.md)                                    | Local, single-machine scope with no login                                             | Accepted |
| [0002](0002-native-telemetry-plus-hooks.md)                                   | Collect from Claude Code's native telemetry and from hooks                            | Accepted |
| [0003](0003-observability-backends.md)                                        | OpenTelemetry Collector, Prometheus, Loki, Tempo and Grafana as the backends          | Accepted |
| [0004](0004-web-app-stack.md)                                                 | Python, FastAPI, Jinja2, HTMX and SSE for the web app                                 | Accepted |
| [0005](0005-sqlite-for-app-state.md)                                          | SQLite for the app's own state                                                        | Accepted |
| [0006](0006-privacy-metadata-only-by-default.md)                              | Metadata only by default, with content capture as an opt-in                           | Accepted |
| [0007](0007-alerts-browser-and-discord.md)                                    | Alerts through browser notifications and an optional Discord webhook                  | Accepted |
| [0008](0008-distribution-compose-ghcr-pypi.md)                                | Distribution with Docker Compose, GHCR and PyPI                                       | Accepted |
| [0009](0009-engineering-workflow-and-quality.md)                              | Engineering workflow and quality gates                                                | Accepted |
| [0010](0010-mit-license.md)                                                   | MIT license                                                                           | Accepted |
| [0011](0011-hook-delivery-http-plus-async-sessionstart.md)                    | Hook delivery: http hooks plus an async command hook for SessionStart                 | Accepted |
| [0012](0012-observed-telemetry-and-hook-contract.md)                          | Observed telemetry and hook contract of Claude Code 2.1.296                           | Accepted |
| [0013](0013-metrics-through-the-prometheus-otlp-receiver.md)                  | Metrics reach Prometheus through its OTLP receiver, made cumulative in the collector  | Accepted |
| [0014](0014-stored-telemetry-names.md)                                        | Stored telemetry names: the default translation, recorded in `ccobs.domain.telemetry` | Accepted |
| [0015](0015-loki-through-its-otlp-endpoint.md)                                | Loki receives events over OTLP, with session fields as structured metadata            | Accepted |
| [0016](0016-cardinality-and-retention.md)                                     | Session IDs on metrics, not in Loki's index, and retention of 90, 30 and 7 days       | Accepted |
| [0017](0017-grafana-access-and-dns-rebinding.md)                              | Grafana: anonymous Editors, no login, answering only to `localhost`                   | Accepted |
| [0018](0018-ports-networks-and-exposure.md)                                   | Ports and networks: minimal exposure in a dedicated port block                        | Accepted |
| [0019](0019-restart-unless-stopped.md)                                        | The stack restarts unless stopped                                                     | Accepted |
| [0020](0020-compose-conventions.md)                                           | Compose conventions for a no-extra-steps start on Linux, macOS and Windows            | Accepted |
| [0021](0021-collector-privacy-filters.md)                                     | Privacy filters in the collector                                                      | Accepted |
| [0022](0022-the-host-label.md)                                                | The host label: one configured value, set by the collector on every signal            | Accepted |
| [0023](0023-per-event-allowlist-of-hook-metadata.md)                          | The app keeps a per-event allowlist of hook metadata                                  | Accepted |
| [0024](0024-session-agent-and-block-state-machine.md)                         | A hook-driven reducer derives session, agent and block state                          | Accepted |
| [0025](0025-registered-hook-events.md)                                        | Register only the hook events the state machine needs                                 | Accepted |
| [0026](0026-app-database-schema-migrations-and-retention.md)                  | The app database: metadata-only schema, Alembic at start-up, retention by last event  | Accepted |
| [0027](0027-the-app-http-surface-and-hook-queue.md)                           | The app's HTTP surface: guarded routes, a neutral hook route and a bounded queue      | Accepted |
| [0028](0028-app-metrics-and-their-listener.md)                                | The app's metrics: fixed-cardinality series on a separate, unpublished listener       | Accepted |
| [0029](0029-closing-esc-denials-from-loki.md)                                 | Permission blocks denied with Esc are closed from Loki's `tool_decision` rejects      | Accepted |
| [0030](0030-the-app-container.md)                                             | The app container: built locally from the clone, non-root, published on loopback      | Accepted |
| [0031](0031-server-rendered-pages-with-htmx-and-vendored-assets.md)           | Server-rendered pages with htmx 2 and vendored, checksummed assets                    | Accepted |
| [0032](0032-live-updates-change-hub-sse-pings-and-refetch.md)                 | Live updates: a change hub fed by the writer, SSE pings and refetch                   | Accepted |
| [0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)           | Separate writer and reader engines, capped reads and bounded session detail           | Accepted |
| [0034](0034-browser-hardening-csp-fetch-metadata-and-response-headers.md)     | Browser hardening: a strict CSP, Fetch Metadata and response headers                  | Accepted |
| [0035](0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md) | Per-session tokens and cost from Prometheus, and links from the UI to Grafana         | Accepted |
| [0036](0036-the-telemetry-env-block-that-ccobs-setup-writes.md)               | The telemetry env block that `ccobs setup` writes                                     | Accepted |
| [0037](0037-how-ccobs-setup-and-uninstall-change-claude-codes-settings.md)    | `ccobs setup` and `uninstall` recognize their entries and keep a state file           | Accepted |
| [0038](0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)            | A read-only diagnostics route in the app for `ccobs doctor` and `status`              | Accepted |
| [0039](0039-ccobs-doctor-and-status-checks-sources-and-exit-codes.md)         | `ccobs doctor` and `status`: graded checks, read-only sources and exit codes          | Accepted |
