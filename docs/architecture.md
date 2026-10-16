# Architecture

A short technical overview for contributors. cc-observatory is pre-alpha. Today the
repository holds:

- the observability stack of milestone 1: `compose.yaml` runs the OpenTelemetry
  Collector, Prometheus, Loki, Grafana and, in an optional profile, Tempo, with their
  configs in `stack/`; [stack.md](stack.md) is the reference for running it;
- the Python package: a CLI that prints its version, `ccobs.domain` (the stored
  telemetry names of [ADR 0014](adr/0014-stored-telemetry-names.md), the hook parser,
  the session state machine and the hook settings, all free of I/O), and
  `ccobs.server`, the app of milestone 2 (from v0.3.0), which the stack builds and runs
  as its `app` service; [app.md](app.md) is the reference for operating it;
- the tooling around them, unit tests, and integration tests that run the stack in an
  isolated compose project;
- development-only tools, not packaged: fixture capture (`tools/capture/`) and the
  stack tools (`tools/stack/`: OTLP replay, the isolated test project, memory per
  service, an egress check);
- sanitized fixtures recorded from real Claude Code sessions (`tests/fixtures/`).

The app's web UI and alerts and the `ccobs` commands on this page are the planned
design, and each part names the milestone that brings it (milestones and releases are
listed in the roadmap in the [README](../README.md)).

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

- Only the collector's OTLP receivers (4317 gRPC, 4318 HTTP), Grafana (4330) and the
  app (4300, from v0.3.0) are published. Every port can be moved with a variable.
- Prometheus, Loki and Tempo, which have no authentication, publish no port and join
  only `backend`, an internal network with no route out. The collector, Grafana and the
  app join `backend` and `edge`, a normal bridge network. `compose.debug.yaml` publishes
  the three stores and the app's metrics listener on loopback, for debugging and the
  integration tests.
- Tempo runs only in the `traces` profile (`docker compose --profile traces up -d`).
- Every service restarts unless stopped
  ([ADR 0019](adr/0019-restart-unless-stopped.md)) and has a memory limit; the limits
  add up to 2304 MiB, 2816 MiB with Tempo.
- Retention is 90 days of metrics, 30 of events and 7 of traces by default, each set by
  a variable ([ADR 0016](adr/0016-cardinality-and-retention.md)), and 90 days after a
  session's last event in the app's database
  ([ADR 0026](adr/0026-app-database-schema-migrations-and-retention.md)).

[stack.md](stack.md) has the services, settings, measured memory and troubleshooting,
and [app.md](app.md) the app's.

## Components

| Component               | Responsibility                                                                                                                                                                                                                                                                                           | Milestone                                                |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Claude Code             | The program being observed. It exports telemetry over OTLP and calls the app's hook endpoint, both configured in the user settings: by hand until `ccobs setup` writes them (milestone 4).                                                                                                               | (external)                                               |
| OpenTelemetry Collector | Receives OTLP on 4317 (gRPC) and 4318 (HTTP), deletes personal attributes and, by default, content, sets the `host` label and routes each signal to its backend.                                                                                                                                         | 1                                                        |
| Prometheus              | Stores the metrics that the collector pushes to its OTLP receiver. Scrapes its own and the collector's metrics, and the app's metrics listener (from v0.3.0).                                                                                                                                            | 1                                                        |
| Loki                    | Stores Claude Code's events (OpenTelemetry log records). The app reads its `tool_decision` rejects back (from v0.3.0).                                                                                                                                                                                   | 1                                                        |
| Tempo                   | Stores traces. Runs only in the optional `traces` compose profile, because tracing is a beta feature in Claude Code and is off by default.                                                                                                                                                               | 1                                                        |
| Grafana                 | Deep analysis, with provisioned datasources (Prometheus, Loki, Tempo) and dashboards. No login: whoever opens it is an Editor.                                                                                                                                                                           | 1 (datasources, overview dashboard), 6 (more dashboards) |
| App (FastAPI, SQLite)   | From v0.3.0: receives hooks, runs the session state machine, stores sessions, agents and blocks, serves a read-only JSON API and its live state as metrics, and reads Claude Code's rejects from Loki. Later: serves the web UI, queries Prometheus and Loki for its views, stores and sends the alerts. | 2 (ingestion, API, metrics), 3 (UI), 5 (alerts)          |
| `ccobs` CLI             | `setup`, `uninstall`, `doctor` and `status`: edits `~/.claude/settings.json` and checks the stack. Published on PyPI as `cc-observatory`; it never imports server-only dependencies at import time.                                                                                                      | 4                                                        |

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

Claude Code calls the app's hook endpoint, `POST /hooks` on port 4300, on lifecycle
events (from v0.3.0). The endpoint only checks and enqueues each hook, then answers an
empty 204; a background writer parses it into metadata, applies the state machine and
stores the result in SQLite
([ADRs 0023](adr/0023-per-event-allowlist-of-hook-metadata.md),
[0026](adr/0026-app-database-schema-migrations-and-retention.md) and
[0027](adr/0027-the-app-http-surface-and-hook-queue.md)).

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
because `SessionStart` does not accept `http` hooks. The 15 `http` events and the
async `SessionStart` that cc-observatory registers, the ones the state machine needs,
are decided in [ADR 0025](adr/0025-registered-hook-events.md), from the payloads that
[ADR 0012](adr/0012-observed-telemetry-and-hook-contract.md) records. Until
`ccobs setup` writes them (milestone 4), [app.md](app.md#point-claude-code-at-the-app)
shows how to pass them to Claude Code with `--settings`.

### Live state in `/metrics`

The app exposes the live state it derives from hooks (sessions by state, open blocks,
running agents, block durations, and what happened to each hook) on `/metrics` in the
Prometheus format, from v0.3.0. It serves them on a listener of its own, port 9464
inside the stack, which compose never publishes, so the app's port keeps its `Host`
allowlist; Prometheus scrapes it over the compose network as the job `ccobs-app`
([ADR 0028](adr/0028-app-metrics-and-their-listener.md)). That puts live state next to
the telemetry in Grafana. Every label takes values from a closed list, so the number of
series is fixed. The series carry the same `host` label as the telemetry, which the
collector sets on every metric, event and trace from `CCOBS_HOST_LABEL`, `local` by
default, and the app puts it on its database rows too
([ADR 0022](adr/0022-the-host-label.md)), so that a future central mode, in which
several machines report to one stack, could tell machines apart. Central mode is not
implemented and is not on the current roadmap.

### Esc denials read back from Loki

One signal reaches only the telemetry: a permission dialog denied with Esc fires no
hook, but Claude Code exports a `tool_decision` event with `decision=reject`. While a
permission block is open, the app queries Loki every 2 seconds for the machine's
rejects, with no session ID in the query, and closes the matching block at the
reject's time (from v0.3.0,
[ADR 0029](adr/0029-closing-esc-denials-from-loki.md)). With telemetry off or Loki
down, blocks close on the next hook as before.

## Sessions, agents and blocks (from v0.3.0)

A pure reducer in `ccobs.domain.sessions` derives every state from the hooks, in the
order the app receives them and by the app's clock, since hook payloads carry no
timestamp. [ADR 0024](adr/0024-session-agent-and-block-state-machine.md) holds the full
rules and what confirms each one; when this summary and the ADR disagree, the ADR wins.

| State          | When                                                                                                                                            |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `waiting_user` | At least one block is open.                                                                                                                     |
| `working`      | Otherwise, the main thread has a turn open, a compaction is running, or a subagent (not one of Claude Code's internal agents) runs.             |
| `idle`         | Otherwise: the turn ended and no subagent runs.                                                                                                 |
| `ended`        | After `SessionEnd`. Not final: any later event other than `SessionStart` reopens the session.                                                   |
| `stale`        | Silent for `CCOBS_SESSION_STALE_AFTER`, 12 hours by default, without an end. Any later event other than `SessionStart` derives the state again. |

- **Turns** open on `UserPromptSubmit` and close on `Stop` or `StopFailure`. A
  `StopFailure` records its error code on the session; it is not a block. Background
  subagents keep the session `working` after the main turn's `Stop`.
- A **block** is a period in which a session waits on a person: a permission prompt
  (`PermissionRequest`, or a `permission_prompt` notification when the request hook was
  missed) or an MCP elicitation (`Elicitation`, or an `elicitation_dialog` notification
  when the `Elicitation` hook was missed). The `idle_prompt` notification is not a
  block: it only marks the session as notified.
- A block **closes** as `tool_completed` or `tool_failed` when its tool call finishes;
  as `superseded` on progress in its scope (another tool call, a new prompt, `Stop`,
  `StopFailure`, `PreCompact`); with the action of its `ElicitationResult`; as
  `agent_stopped` or `session_ended`; as `denied`, from Loki's reject (above), or from
  a `PermissionDenied` hook, which the shipped settings do not register; or as `stale`.
  Each closed block records whether a hook, the telemetry or the sweep closed it. Its
  duration is how long the session waited.
- The **stale sweep** runs every minute: a silent session becomes `stale`, its open
  blocks close and its running agents stop, so the live metrics never count them; a
  running agent silent that long stops too, in a session that is still active.
- Known gaps: an Esc that interrupts a response fires no `Stop`, so the session shows
  `working` until the next prompt or event that ends the turn; and a dialog denied with
  Esc stays open until Loki's reject or the next progress event.

Open blocks drive the alerts (milestone 5).

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
