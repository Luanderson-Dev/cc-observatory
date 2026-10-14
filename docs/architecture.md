# Architecture

A short technical overview for contributors. cc-observatory is pre-alpha: today the
repository holds the Python package skeleton, a CLI that prints its version, the tooling
around them, the development-only fixture capture tooling (`tools/capture/`, not
packaged) and sanitized fixtures recorded from real Claude Code sessions
(`tests/fixtures/`). Everything else on this page is the planned design, and each part
names the milestone that brings it (milestones and releases are listed in the roadmap in
the [README](../README.md)).

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
port binds to `127.0.0.1`.

## Components

| Component               | Responsibility                                                                                                                                                                                      | Milestone                                     |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Claude Code             | The program being observed. It exports telemetry over OTLP and calls the app's hook endpoint, both configured by `ccobs setup` in the user settings.                                                | (external)                                    |
| OpenTelemetry Collector | Receives OTLP on 4317 (gRPC) and 4318 (HTTP), deletes personal attributes and routes each signal to its backend.                                                                                    | 1                                             |
| Prometheus              | Stores the metrics from the collector and scrapes the app's `/metrics`.                                                                                                                             | 1                                             |
| Loki                    | Stores Claude Code's events (OpenTelemetry log records).                                                                                                                                            | 1                                             |
| Tempo                   | Stores traces. Runs only in the optional `traces` compose profile, because tracing is a beta feature in Claude Code and is off by default.                                                          | 1                                             |
| Grafana                 | Deep analysis, with provisioned datasources (Prometheus, Loki, Tempo) and dashboards.                                                                                                               | 1 (datasources), 6 (dashboards)               |
| App (FastAPI, SQLite)   | Receives hooks, runs the session state machine, stores sessions, agents, blocks and alerts, exposes `/metrics`, queries Prometheus and Loki over HTTP, serves the web UI and sends the alerts.      | 2 (ingestion, `/metrics`), 3 (UI), 5 (alerts) |
| `ccobs` CLI             | `setup`, `uninstall`, `doctor` and `status`: edits `~/.claude/settings.json` and checks the stack. Published on PyPI as `cc-observatory`; it never imports server-only dependencies at import time. | 4                                             |

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
(milestone 2). That puts live state next to the telemetry in Grafana. Every series
carries a `host` label so that a future central mode, in which several machines report
to one stack, could tell machines apart. Central mode is not implemented and is not on
the current roadmap.

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
  protocol, so `ccobs setup` always sets one. Its default metrics temporality is delta,
  while Prometheus stores cumulative series; how the stack bridges the two is recorded
  in an ADR.
- **Metric names change on the way to Prometheus.** OpenTelemetry names such as
  `claude_code.token.usage` are translated (dots become underscores, and unit and type
  suffixes may be added), so code and dashboards use the names recorded in the ADRs,
  never guesses. [ADR 0012](adr/0012-observed-telemetry-and-hook-contract.md) records
  the names Claude Code sends; the translated names come with milestone 1.
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
- **No outbound traffic.** At runtime nothing leaves the machine except the optional
  Discord webhook. From v0.2.0 the bundled configs turn off upstream usage reporting and
  update checks, and tests check that they stay off:
    - Grafana: analytics reporting (`[analytics] reporting_enabled`), update checks for
      Grafana and its plugins (`check_for_updates`, `check_for_plugin_updates`), the news
      feed (`[news] news_feed_enabled`) and Gravatar (`[security] disable_gravatar`);
    - Loki: `analytics.reporting_enabled`;
    - Tempo: `usage_report.reporting_enabled`.
- **Cross-platform.** Everything must work on Linux, macOS and Windows.
