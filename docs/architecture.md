# Architecture

A short technical overview for contributors. cc-observatory is pre-alpha. Today the
repository holds:

- the observability stack of milestone 1: `compose.yaml` runs the OpenTelemetry
  Collector, Prometheus, Loki, Grafana and, in an optional profile, Tempo, with their
  configs in `stack/`; [stack.md](stack.md) is the reference for running it;
- the Python package: a CLI that prints its version, and `ccobs.domain.telemetry`, the
  stored telemetry names ([ADR 0014](adr/0014-stored-telemetry-names.md));
- the tooling around them, unit tests, and integration tests that run the stack in an
  isolated compose project;
- development-only tools, not packaged: fixture capture (`tools/capture/`) and the
  stack tools (`tools/stack/`: OTLP replay, the isolated test project, memory per
  service, an egress check);
- sanitized fixtures recorded from real Claude Code sessions (`tests/fixtures/`).

The app, the hooks and the `ccobs` commands on this page are the planned design, and
each part names the milestone that brings it (milestones and releases are listed in the
roadmap in the [README](../README.md)).

Decisions and their reasons are recorded as architecture decision records (ADRs, MADR
format) in [`docs/adr/`](adr/README.md). When this page and an ADR disagree, the ADR
wins.

## Overview

~~~text
Claude Code ──OTLP (4317/4318)──► OTel Collector ──► Prometheus (metrics)
     │                                         ├──► Loki (events/logs)
     │                                         └──► Tempo (traces, optional profile)
     └──hooks──► app (FastAPI) ──► SQLite (sessions, agents, blocks, alerts)
                    │  └─ /metrics ◄── scraped by Prometheus (live state)
                    └─ queries Prometheus and Loki over HTTP
Grafana ◄── Prometheus, Loki, Tempo (provisioned datasources and dashboards)
~~~

Everything runs on one machine. The stack runs in Docker Compose and every published
port binds to `127.0.0.1` ([ADR 0018](adr/0018-ports-networks-and-exposure.md)):

- Only the collector's OTLP receivers (4317 gRPC, 4318 HTTP) and Grafana (4330) are
  published; the app's port (4300) joins them in milestone 2. Every port can be moved
  with a variable.
- Prometheus, Loki and Tempo, which have no authentication, publish no port and join
  only `backend`, an internal network with no route out. The collector and Grafana join
  `backend` and `edge`, a normal bridge network. `compose.debug.yaml` publishes the
  three stores on loopback, for debugging and the integration tests.
- Tempo runs only in the `traces` profile (`docker compose --profile traces up -d`).
- Every service restarts unless stopped
  ([ADR 0019](adr/0019-restart-unless-stopped.md)) and has a memory limit; the limits
  add up to 2 GiB, 2.5 GiB with Tempo.
- Retention is 90 days of metrics, 30 of events and 7 of traces by default, each set by
  a variable ([ADR 0016](adr/0016-cardinality-and-retention.md)).

[stack.md](stack.md) has the services, settings, measured memory and troubleshooting.

## Components

| Component               | Responsibility                                                                                                                                                                                      | Milestone                                                |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Claude Code             | The program being observed. It exports telemetry over OTLP and calls the app's hook endpoint, both configured in the user settings: by hand until `ccobs setup` writes them (milestone 4).          | (external)                                               |
| OpenTelemetry Collector | Receives OTLP on 4317 (gRPC) and 4318 (HTTP), deletes personal attributes and, by default, content, sets the `host` label and routes each signal to its backend.                                    | 1                                                        |
| Prometheus              | Stores the metrics that the collector pushes to its OTLP receiver. Scrapes its own and the collector's metrics, and the app's `/metrics` from milestone 2.                                          | 1                                                        |
| Loki                    | Stores Claude Code's events (OpenTelemetry log records).                                                                                                                                            | 1                                                        |
| Tempo                   | Stores traces. Runs only in the optional `traces` compose profile, because tracing is a beta feature in Claude Code and is off by default.                                                          | 1                                                        |
| Grafana                 | Deep analysis, with provisioned datasources (Prometheus, Loki, Tempo) and dashboards. No login: whoever opens it is an Editor.                                                                      | 1 (datasources, overview dashboard), 6 (more dashboards) |
| App (FastAPI, SQLite)   | Receives hooks, runs the session state machine, stores sessions, agents, blocks and alerts, exposes `/metrics`, queries Prometheus and Loki over HTTP, serves the web UI and sends the alerts.      | 2 (ingestion, `/metrics`), 3 (UI), 5 (alerts)            |
| `ccobs` CLI             | `setup`, `uninstall`, `doctor` and `status`: edits `~/.claude/settings.json` and checks the stack. Published on PyPI as `cc-observatory`; it never imports server-only dependencies at import time. | 4                                                        |

## Two data paths

Claude Code reports through two independent channels, and cc-observatory needs both.

### Telemetry: OTLP to the collector

Claude Code's native OpenTelemetry support exports:

- **metrics**, such as `claude_code.token.usage`, `claude_code.cost.usage`,
  `claude_code.session.count` and `claude_code.active_time.total`;
- **events** (log records), such as `claude_code.api_request`, `claude_code.api_error`,
  `claude_code.tool_result` and `claude_code.tool_decision`;
- **traces** (beta, opt-in), in which a subagent's API and tool spans nest under the
  span of the Agent tool call that started it.

Good for: tokens, cost (an estimate computed by Claude Code), API requests and errors,
tool results, and the subagent hierarchy (from traces). Prometheus, Loki, Tempo and
Grafana aggregate and chart it.

Not good for: live state. Signals are exported in batches (by default every 60 seconds
for metrics and every 5 seconds for events), and none of them says that a session is
waiting on a person right now.

In the stack, every collector pipeline runs `memory_limiter` first, then the privacy
processors, which always delete identity and, while content capture is off, content
([ADR 0021](adr/0021-collector-privacy-filters.md)), then `transform/host`, which sets
the `host` label ([ADR 0022](adr/0022-the-host-label.md)). Then:

- metrics pass through `delta_to_cumulative` and go to Prometheus's OTLP receiver
  ([ADR 0013](adr/0013-metrics-through-the-prometheus-otlp-receiver.md)), where totals
  are queried as `increase(x[R] anchored)`;
- events go to Loki's OTLP endpoint, as one stream per machine with the session, prompt
  and trace fields as structured metadata
  ([ADR 0015](adr/0015-loki-through-its-otlp-endpoint.md));
- traces go to Tempo, when the `traces` profile runs it.

The names that reach Prometheus and Loki are recorded in
[ADR 0014](adr/0014-stored-telemetry-names.md) and, as constants, in
`ccobs.domain.telemetry`.

### Hooks: HTTP to the app

Claude Code calls the app's hook endpoint on lifecycle events (milestone 2).

Good for: real-time session state (prompt submitted, tool use, turn finished),
permission requests, notifications (permission prompts, idle prompts, MCP
elicitation), subagent start and stop, compaction, API failures that end a turn, and
the end of a session.

Not good for: tokens and cost, which come from the telemetry. Hooks are point events, so
the app keeps the current state and the recent history it needs, and long-term analysis
stays with the telemetry backends.

How hooks reach the app is decided in
[ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md): `http` hooks with a
1-second timeout, plus an async command hook that posts `SessionStart` with `curl`,
because `SessionStart` does not accept `http` hooks. The exact list of hook events that
`ccobs setup` registers is decided with the session state machine in milestone 2, from
the payloads that [ADR 0012](adr/0012-observed-telemetry-and-hook-contract.md) records.

### Live state in `/metrics`

The app exposes the live state it derives from hooks (for example, sessions by state and
open blocks) on `/metrics` in the Prometheus format, and Prometheus scrapes it
(milestone 2). That puts live state next to the telemetry in Grafana. Its series will
carry the same `host` label as the telemetry, which the collector already sets on every
metric, event and trace from `CCOBS_HOST_LABEL`, `local` by default
([ADR 0022](adr/0022-the-host-label.md)), so that a future central mode, in which
several machines report to one stack, could tell machines apart. Central mode is not
implemented and is not on the current roadmap.

## Sessions and blocks (planned, milestone 2)

Each session is in one of these states:

| State          | Meaning                                                                                             |
| -------------- | --------------------------------------------------------------------------------------------------- |
| `working`      | Claude is handling a prompt: calling the API or running tools.                                      |
| `waiting_user` | The session cannot continue until a person acts, for example on a permission prompt.                |
| `idle`         | The turn finished and the session is waiting for the next prompt.                                   |
| `ended`        | Claude Code reported that the session ended.                                                        |
| `stale`        | No signal arrived for too long and no end was reported, for example because the process was killed. |

A **block** is a period in which a session waits on a person. It opens when the session
starts waiting (candidate signals: a permission request, a permission or idle prompt
notification, an MCP elicitation, an API failure that ends the turn) and closes on the
next signal that the session is making progress again. Its duration is how long the
session waited. Open blocks drive the alerts (milestone 5).

The exact signals that open and close a block, the transitions between states and the
timeout that marks a session as `stale` are defined in milestone 2 from real fixtures,
not from the documentation alone.

## Key constraints

- **Hooks never interfere with Claude Code.** The endpoint validates, enqueues and
  answers (target: under 50 ms); persistence happens in the background. It answers with
  an empty 2xx even for an invalid payload, a full queue or an internal error, and never
  returns decision fields or plain text. Hooks written by `ccobs setup` have a short,
  explicit `timeout`, and never `onFailure: "block"` or `statusMessage`. Events whose
  registration alone changes Claude Code's behaviour (`WorktreeCreate`,
  `WorktreeRemove`, `PreModelSwitch`, `MessageDisplay`, `FileChanged`) are never
  registered.
- **Claude Code keeps working with the stack down.** It treats a failed `http` hook as a
  non-blocking error and carries on, but each event sent by an `http` hook then shows a
  hook-error notice in the transcript (the notices are not sent to the model). How
  cc-observatory handles that trade-off is recorded in
  [ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md).
- **`SessionStart` and `Setup` do not accept `http` hooks.** `SessionStart` reaches the
  app through an async command hook, which can arrive after later events or not at all,
  so the app creates a session from whichever event arrives first
  ([ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md)).
- **OpenTelemetry defaults need explicit settings.** Claude Code has no default OTLP
  protocol, so one is always set: by hand today, by `ccobs setup` from milestone 4. Its
  default metrics temporality is delta, while Prometheus stores cumulative series: the
  collector's `delta_to_cumulative` processor converts delta, and cumulative
  temporality, which `ccobs setup` will set, avoids the limits of delta
  ([ADR 0013](adr/0013-metrics-through-the-prometheus-otlp-receiver.md),
  [stack.md](stack.md#known-limit-delta-temporality)).
- **Metric names change on the way to Prometheus.** OpenTelemetry names such as
  `claude_code.token.usage` are translated (dots become underscores, and unit and type
  suffixes are added: `claude_code_token_usage_tokens_total`), so code and dashboards
  use the names recorded in [ADR 0014](adr/0014-stored-telemetry-names.md) and
  `ccobs.domain.telemetry`, never guesses.
  [ADR 0012](adr/0012-observed-telemetry-and-hook-contract.md) records the names Claude
  Code sends.
- **Personal attributes are dropped in the collector**, always: every key that starts
  with `user.` or `enduser.` (such as `user.email`, `user.id`, which on gateway sessions
  is the identity provider subject, `user.account_uuid`, `user.account_id` and
  `user.groups`), plus `organization.id`, `identity.source` and `host.name`
  ([ADR 0021](adr/0021-collector-privacy-filters.md)).
- **Metadata only by default.** Without the capture-content opt-in, prompts, tool inputs
  and outputs, assistant messages and error text are dropped before anything is stored.
  The opt-in has to be turned on in Claude Code's content variables, the collector and
  the app; without it, the collector drops content even if Claude Code sends it.
- **Telemetry settings are ignored in project settings.** Claude Code applies the
  OpenTelemetry exporter variables from the environment it starts in, a `--settings`
  file, the user settings (`~/.claude/settings.json`) and managed settings, but ignores
  them in a project's `.claude/settings.json` and `.claude/settings.local.json`. That is
  why `ccobs setup` writes to the user settings. A project can still turn a signal off
  by setting its exporter to `none`.
- **Paths are personal data.** `cwd`, `transcript_path` and other paths in hook payloads
  contain the OS username. Test fixtures come from real sessions but are sanitized, and
  a guard test fails if personal data shows up in them.
- **Local only.** Published ports bind to `127.0.0.1` and there is no login. See
  [SECURITY.md](../SECURITY.md).
- **Grafana: no login, anonymous Editors, `localhost` only**
  ([ADR 0017](adr/0017-grafana-access-and-dns-rebinding.md)). Its settings are
  environment variables of the Grafana service in `compose.yaml`:
    - access: anonymous visitors are Editors of its one organization, who query and save
      dashboards but cannot change datasources, plugins, users or server settings; no
      login form, no basic auth and no admin user;
    - one host name: `server.domain = localhost` with `server.enforce_domain`, so any
      other `Host`, `127.0.0.1` included, gets a redirect, and a DNS rebinding page
      reads only redirects;
    - `security.csrf_always_check`: a request other than HEAD, OPTIONS and TRACE whose
      `Origin` names another host is refused, a GET included, even without a login
      cookie;
    - `security.data_source_proxy_whitelist = 127.0.0.1:9`, a host:port no datasource
      uses, which closes the legacy datasource proxy;
    - `/metrics` off (`metrics.enabled = false`);
    - unified alerting and public dashboards off, and the bundled backend datasource
      plugins that run as separate processes and that no provisioned datasource uses
      are disabled (core ones such as CloudWatch, Graphite and TestData stay);
    - the datasources (Prometheus, Loki, Tempo) are provisioned read-only from
      `stack/grafana/`, and the home page is the provisioned Claude Code overview
      dashboard.
- **No outbound traffic.** At runtime nothing leaves the machine except the optional
  Discord webhook and, while one of the stores is stopped, DNS lookups of its name
  ([SECURITY.md](../SECURITY.md)). The bundled configs turn off upstream usage reporting
  and update checks, and tests check that they stay off, in the configs and in the
  running stack:
    - Grafana: usage reporting, the update checks for Grafana and its plugins, feedback
      links, the news feed, Gravatar, external snapshots, the plugin signing key
      download, the plugin preinstall and its updates, plugin installs from the catalog
      and Cloud migration are off, and `grafana_com.api_url` points at a closed port on
      Grafana's own loopback (`127.0.0.1:9`) (ADR 0017);
    - Loki: `analytics.reporting_enabled: false`;
    - Tempo: `usage_report.reporting_enabled: false`;
    - Prometheus has no remote write or read and no Alertmanager, and the collector
      exports only to Prometheus, Loki and Tempo.

  Prometheus, Loki and Tempo also sit on the internal `backend` network, with no route
  out (ADR 0018). [stack.md](stack.md#checking-that-nothing-leaves-the-machine)
  describes the settings test and a manual check that no connection leaves.
- **Cross-platform.** Everything must work on Linux, macOS and Windows.
