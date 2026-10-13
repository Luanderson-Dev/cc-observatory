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

| ADR                                              | Decision                                                                     | Status   |
| ------------------------------------------------ | ---------------------------------------------------------------------------- | -------- |
| [0001](0001-local-single-machine-scope.md)       | Local, single-machine scope with no login                                    | Accepted |
| [0002](0002-native-telemetry-plus-hooks.md)      | Collect from Claude Code's native telemetry and from hooks                   | Accepted |
| [0003](0003-observability-backends.md)           | OpenTelemetry Collector, Prometheus, Loki, Tempo and Grafana as the backends | Accepted |
| [0004](0004-web-app-stack.md)                    | Python, FastAPI, Jinja2, HTMX and SSE for the web app                        | Accepted |
| [0005](0005-sqlite-for-app-state.md)             | SQLite for the app's own state                                               | Accepted |
| [0006](0006-privacy-metadata-only-by-default.md) | Metadata only by default, with content capture as an opt-in                  | Accepted |
| [0007](0007-alerts-browser-and-discord.md)       | Alerts through browser notifications and an optional Discord webhook         | Accepted |
| [0008](0008-distribution-compose-ghcr-pypi.md)   | Distribution with Docker Compose, GHCR and PyPI                              | Accepted |
| [0009](0009-engineering-workflow-and-quality.md) | Engineering workflow and quality gates                                       | Accepted |
| [0010](0010-mit-license.md)                      | MIT license                                                                  | Accepted |
