# cc-observatory

Local observability for Claude Code: see your sessions, subagents, blocks, logs, tokens
and cost, on your own machine.

[Leia em português](https://github.com/Luanderson-Dev/cc-observatory/blob/main/README.pt-BR.md)

[![CI](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml?query=branch%3Adevelop)
[![PyPI](https://img.shields.io/pypi/v/cc-observatory)](https://pypi.org/project/cc-observatory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/Luanderson-Dev/cc-observatory/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

## Status

**Pre-alpha.** v0.4.0 closes milestone 3 of 8: the web UI. The `cc-observatory` package
on PyPI still only prints its version; the stack and the app run from a clone of this
repository.

What v0.4.0 adds:

- a web UI at `http://127.0.0.1:4300` (see "Try the app" below and
  [docs/app.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/app.md)):
  an overview of sessions grouped by state, with how long each one has been waiting on
  you, and a page per session with its blocks, its agent tree and its hook timeline;
- live updates over server-sent events, without a reload;
- tokens and cost per session, read from Prometheus, and links from each session to
  its logs, tokens and cost in Grafana;
- vendored htmx and fonts with checksums and licenses, so the UI works offline;
- browser hardening: a strict content security policy, Fetch Metadata checks and
  defensive response headers;
- separate database engines for the writer and the readers, with capped concurrent
  reads, and bounded pages of blocks, agents and events in the API;
- ADRs 0031 to 0035 for the UI's decisions.

Until `ccobs setup` arrives in v0.5.0, you point Claude Code at the app with a settings
file passed to `claude --settings`. Earlier releases: v0.3.0 (milestone 2) added the app
that receives Claude Code's hooks, v0.2.0 (milestone 1) the observability stack in Docker
Compose with a provisioned Grafana, and v0.1.0 (milestone 0) laid the foundation.

What comes next: milestone 4 (v0.5.0), the `ccobs setup`, `uninstall`, `doctor` and
`status` commands. The full plan is in the roadmap below.

Unless a section says otherwise, the rest of this README describes the planned design.

## What it shows

The release that brings each part is in parentheses. The parts up to v0.4.0 run from a
clone of this repository (see "Run the observability stack" and "Try the app" below);
the rest is planned.

- **Live overview** of the Claude Code sessions on your machine, in a web UI at
  <http://127.0.0.1:4300>: the sessions waiting on you first, with how long each has
  waited, then the ones working, idle, recently ended or stale. It updates as Claude
  Code works, without a reload (v0.4.0).
- **Blocks**: sessions waiting on you, such as a permission prompt or an MCP
  elicitation, with how long each one has been waiting and how it ended (tracked from
  v0.3.0, shown in the web UI from v0.4.0).
- **A page per session** (v0.4.0) with its blocks, its **agent tree** (the subagents,
  each under the agent that started it), its **tokens and cost** (tokens by type, tokens
  and cost by model) and its **hook timeline**, metadata only.
- **Events and logs** for each session: the hook timeline on its page, and a link to its
  Claude Code events in Grafana (v0.4.0).
- **Tokens and cost**: per session on its page (v0.4.0), and across sessions by day and
  model in Grafana's overview dashboard (v0.2.0); by agent type (built-in agent names;
  your own agents show as `custom` unless you opt in to tool details) and by project
  (joined from the working directory that hooks report) are planned for v0.7.0. Cost
  is an estimate that comes from Claude Code's telemetry, not the amount you are billed.
- **Health page**: whether each part of the stack is up and receiving data (planned, v0.7.0).
- **Grafana** for deeper analysis, provisioned with its datasources and a Claude Code
  overview dashboard (v0.2.0); each session's page links to its logs, tokens and cost
  there (v0.4.0), and later dashboards will open already filtered to the agent or
  project you were looking at (v0.7.0).
- **Alerts** (v0.6.0): browser notifications and, optionally, a Discord webhook when a
  session has been waiting on you for longer than a threshold, when API errors keep
  repeating, or when the estimated cost of a session or of a day goes over a limit. You
  set the thresholds with environment variables or on a settings screen.

## How it works

~~~text
Claude Code ──OTLP (4317/4318)──► OTel Collector ──► Prometheus (metrics)
     │                                         ├──► Loki (events/logs)
     │                                         └──► Tempo (traces, optional profile)
     └──hooks──► app (FastAPI) ──► SQLite (sessions, agents, blocks, alerts)
                    │  └─ /metrics ◄── scraped by Prometheus (live state)
                    └─ queries Prometheus and Loki over HTTP
Grafana ◄── Prometheus, Loki, Tempo (provisioned datasources and dashboards)
~~~

- **Telemetry.** Claude Code's native OpenTelemetry support exports metrics, events and,
  optionally, traces over OTLP to an OpenTelemetry Collector. The collector removes
  personal attributes and sends metrics to Prometheus, events to Loki and traces to
  Tempo. Tempo runs only in an optional `traces` profile, because tracing is a beta
  feature in Claude Code.
- **Hooks.** Claude Code hooks send lifecycle events (a prompt submitted, a permission
  request, a notification, a subagent starting or stopping, a compaction, the end of a
  session) to the app, a FastAPI service that keeps sessions, agents, blocks and alerts
  in SQLite.
- **Live state as metrics.** The app exposes that live state on `/metrics`, which
  Prometheus scrapes, so it can be charted next to the telemetry.
- **The web UI.** The app serves its own pages, which follow the sessions live over
  server-sent events, and reads each session's tokens and cost from Prometheus. It also
  closes permission prompts denied with Esc from Claude Code's events in Loki.
- **Grafana** comes provisioned with the Prometheus, Loki and Tempo datasources and with
  the dashboards.
- **Why both sources:** telemetry tells you what a session consumed (tokens, cost, API
  calls, tool results) but not that it is waiting on you right now, which only hooks
  report, at the moment it happens.

More detail for contributors is in [docs/architecture.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/architecture.md).
The decisions behind this design, and their reasons, are recorded as architecture decision records in [docs/adr/](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/README.md).

## Quick start (from v0.5.0)

1. Start the stack:

   ~~~bash
   git clone https://github.com/Luanderson-Dev/cc-observatory
   cd cc-observatory
   docker compose up -d
   ~~~

   The first start downloads about 750 MB of images and builds the collector's and the
   app's images. `docker compose ps` shows each service and whether it is healthy.

2. Connect Claude Code to it:

   ~~~bash
   uvx cc-observatory setup
   ~~~

   `setup` backs up your user settings (`~/.claude/settings.json`) and adds the hooks
   and the telemetry `env` block to them, keeping your own entries. It records what it
   added, so that `ccobs uninstall` removes only that; `--dry-run` shows the diff without
   writing anything, and running it again is safe.

3. Start a new Claude Code session. The hooks reach the sessions already open at once,
   but telemetry (tokens, cost, events) starts only in sessions started after `setup`,
   so restart the ones you want to see in full.

4. Open the app at <http://127.0.0.1:4300>, the address `setup` prints, and Grafana at
   <http://localhost:4330>.

If nothing shows up, run `uvx cc-observatory doctor`: it checks Claude Code, your
settings, the policies that can block hooks or telemetry, and the stack, and says how to
fix what it finds. `uvx cc-observatory status` is the short version.

- **The CLI.** `ccobs` and `cc-observatory` are the same CLI: `uvx cc-observatory
  <command>` runs it without installing it, and `uv tool install cc-observatory`
  installs it as `ccobs`. [docs/cli.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/cli.md) covers `setup`, `uninstall`,
  `doctor` and `status`, their options and exit codes, and troubleshooting.
- **Without changing your settings.** To connect one session only, start Claude Code
  with the hooks in a file of their own, `claude --settings <file>`, as
  [docs/app.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/app.md#point-claude-code-at-the-app) shows.
- **Undo.** `uvx cc-observatory uninstall` takes out what `setup` added and puts back
  any value it replaced. The stack keeps running until `docker compose down`.

## Run the observability stack (from v0.2.0)

From v0.2.0 you can run the observability stack from a clone of this repository and see
Claude Code's metrics and events in Grafana. From v0.3.0 the stack also runs the app
(hooks, sessions and blocks; see "Try the app" below), and from v0.4.0 the app's web UI.

1. Start the stack as in the quick start. To also store Claude Code's traces, a beta
   feature, start it with `docker compose --profile traces up -d`, which adds Tempo, and
   pass `--profile traces` every time you start or update the stack.

2. Open Grafana at <http://localhost:4330>. Use `localhost`: Grafana redirects any other
   host name, `127.0.0.1` included, there. There is no login. The home page is the
   **Claude Code overview** dashboard: cost, tokens, sessions, active time, code
   changes, API errors and recent events.

3. Point Claude Code at the stack. `ccobs setup` writes these variables into the `env`
   block of your user settings, `~/.claude/settings.json` (`--traces` adds the two trace
   variables below, `--otlp-port` changes the port). To set them by hand instead, add
   them to that block, keeping the entries you already have, or export them in the shell
   that starts Claude Code. Project settings (`.claude/settings.json` and
   `.claude/settings.local.json`) cannot set them.

   <!-- telemetry-env: telemetry_env(), kept equal by tests/test_docs_snippet.py -->

   ~~~json
   {
     "env": {
       "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
       "OTEL_METRICS_EXPORTER": "otlp",
       "OTEL_LOGS_EXPORTER": "otlp",
       "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
       "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
       "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative"
     }
   }
   ~~~

   - Use `127.0.0.1`, not `localhost`: the ports listen on IPv4 only, and `localhost`
     can resolve to `::1`.
   - Claude Code has no default protocol, so set one. `grpc` also works, with
     `http://127.0.0.1:4317`.
   - Cumulative temporality is recommended: with Claude Code's default, delta, some
     totals can come out low after a pause
     ([the known limit](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/stack.md#known-limit-delta-temporality)).
   - Leave `OTEL_METRICS_INCLUDE_SESSION_ID` at its default (`true`): without session
     IDs, the series of concurrent sessions collide.
   - For traces, also set `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` to `1` and
     `OTEL_TRACES_EXPORTER` to `otlp` (or run `ccobs setup --traces`), and run the stack
     with `--profile traces`.

4. Restart Claude Code so that new sessions load the settings. Metrics arrive every 60
   seconds and events every 5 seconds, and the dashboard refreshes every minute. If
   nothing shows up, run `ccobs doctor`, or see
   [No data in the dashboard](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/stack.md#no-data-in-the-dashboard).

- **Settings.** Every setting is an environment variable with a default, so a `.env`
  file is optional: to change one, copy
  [`.env.example`](https://github.com/Luanderson-Dev/cc-observatory/blob/main/.env.example)
  to `.env`, edit it and run `docker compose up -d` again. `CCOBS_HOST_LABEL` (default
  `local`) is the `host` label stored on every metric, event and trace; set it to name
  the machine. It is stored as you write it, so use the machine's real host name only if
  you want it stored. The ports and the retention (by default 90 days of metrics, 30 of
  events and 7 of traces) are set the same way. If you move a port, pass the same port
  to `ccobs` (`--app-port`, `--otlp-port`, `--grafana-port`).
- **After a reboot.** The services restart unless you stop them with
  `docker compose stop`, so the stack comes back whenever Docker starts
  ([ADR 0019](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0019-restart-unless-stopped.md)).
  Docker Desktop starts when you sign in only if its "Start Docker Desktop when you sign
  in to your computer" setting is on, and it is off by default; on Linux, enable the
  Docker service at boot.
- **More.** [docs/stack.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/stack.md)
  covers stopping and updating the stack, its settings, data and retention, memory, and
  troubleshooting.

## Try the app (from v0.3.0, web UI from v0.4.0)

From v0.3.0, `docker compose up -d` also builds the app's image from your clone and
starts it at <http://127.0.0.1:4300>. The app receives Claude Code's hooks and tracks
sessions, agents and blocks in SQLite. From v0.4.0 it shows them in a web UI at that
address; it also serves them as a read-only JSON API.

1. Start or update the stack as above. `docker compose ps` shows `app` as healthy once
   it serves requests.

2. Point Claude Code at the app: `ccobs setup` writes the hooks (see the quick start).
   The hook URL uses `127.0.0.1`, not `localhost`. To send the hooks of one session only,
   without changing your settings, use the hooks file and `claude --settings` of
   [docs/app.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/app.md#point-claude-code-at-the-app).

3. Open <http://127.0.0.1:4300> in your browser. The overview shows your sessions in
   bays by state, the ones waiting on you first, and follows them live; click a session
   for its blocks, agents, tokens and cost, and hook timeline, and for links to its logs
   and usage in Grafana. Until the first session arrives, the overview shows the
   `ccobs setup` command for its port. The same data is in the JSON API:

   ~~~bash
   curl -s http://127.0.0.1:4300/api/v1/sessions
   ~~~

   Each session has its state (`working`, `waiting_user`, `idle`, `ended` or `stale`),
   and `/api/v1/sessions/<session_id>` adds its agents and blocks.

- **Stack down.** While the stack is down, Claude Code shows a hook-error notice in the
  transcript on most hook events; start the stack, or run `ccobs uninstall` to take the
  hooks out until it is back.
- **Esc denials, tokens and cost.** A permission prompt denied with Esc fires no hook;
  the app closes it from Claude Code's events in Loki. A session's tokens and cost come
  from Claude Code's metrics in Prometheus. Both need the telemetry settings that
  `ccobs setup` writes (step 3 of the previous section).
- **More.** [docs/app.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/app.md)
  covers the web UI, the API, the app's metrics in Grafana, its settings and retention,
  privacy and troubleshooting.

## Privacy

- **Metadata only by default.** Prompt text, tool inputs and outputs, assistant messages
  and error text are not stored.
- **No content capture yet.** `ccobs setup` never turns content capture on, and the
  collector drops content from the telemetry even if Claude Code was set to send it;
  `ccobs doctor` warns when Claude Code's content variables are on. The explicit opt-in
  to keep content is deferred to a later release
  ([ADR 0040](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0040-defer-the-capture-content-opt-in.md)).
- **Personal attributes are removed.** The collector deletes your email address, your
  user, account and organization IDs and your machine's host name from the telemetry
  before anything is stored.
- **Everything stays on your machine.** That is a design goal that the bundled configs
  enforce: every port binds to `127.0.0.1`; Prometheus, Loki and Tempo publish no port
  and sit on an internal network with no route out; and the usage reporting and update
  checks of Grafana, Loki and Tempo are turned off. At runtime, nothing leaves your
  machine except the optional Discord webhook, if you configure one, and DNS lookups of
  a store's name while that store is stopped (see
  [SECURITY.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/SECURITY.md)).
- **The web UI** (from v0.4.0) has no login either, loads nothing from the internet and
  changes nothing. It shows the working directory only as the project name, or with your
  home directory as `~`, and refuses requests that other web pages make through your
  browser (a link to it still just opens it).
- **Grafana has no login.** Whoever opens it is an Editor, who can query the data and
  save dashboards but cannot change its datasources, plugins, users or settings. It
  answers only as `localhost`, which keeps other web pages from reading it through DNS
  rebinding, and it refuses the cross-site requests that could change it or read its
  answers; a page can still make it run a query blindly, without seeing the result.
  Anything that runs on your machine can read the data.
- **Retention is configurable** (the observability stack from v0.2.0, by default 90 days
  of metrics, 30 of events and 7 of traces; the app database from v0.3.0), so old data
  is deleted after the period you choose.

See [SECURITY.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/SECURITY.md) for the security model, including what a web page
can still do, and how to report a vulnerability.

## No interference with Claude Code

- **Hooks never change Claude Code's decisions.** The app answers every hook call with an
  empty 2xx response, never with a decision, and `setup` gives every hook a short
  timeout.
- **If the stack is down, Claude Code keeps working.** While the stack is down, Claude
  Code shows a hook-error notice in the transcript on most hook events (Claude itself does
  not see these notices).
  [ADR 0011](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0011-hook-delivery-http-plus-async-sessionstart.md)
  explains this trade-off and how cc-observatory keeps it small: `ccobs doctor` checks
  that the hook endpoint answers and reports failing hooks, and `ccobs uninstall` removes
  the hooks.

## Requirements

- Docker Engine 25.0 or later with the Docker Compose plugin 2.20.2 or later (and, if
  the Buildx plugin is installed, Buildx 0.17.0 or later): Docker Desktop on Windows and
  macOS, or Docker's own packages on Linux, where Docker Engine 28.0 or later also keeps
  ports published to `127.0.0.1` out of reach of other hosts on the local network.
- Memory: leave Docker at least 2304 MiB for the stack (2816 MiB with Tempo), the sum of
  its memory limits. Measured use was about 260 to 290 MiB idle and 420 to 450 MiB
  while the dashboard's queries ran through Grafana's API, before the app joined the
  stack; the app, measured on its own, used about 64 to 70 MiB. Loading Grafana's UI in
  a browser briefly took Grafana alone to about 630 MiB.
- Disk: about 3 GB for the images, plus about 250 MB for the app's image, plus the
  data.
- Tested on Linux and on Windows 11 with Docker Desktop, both on amd64; macOS and arm64
  have not been tested. Details are in
  [docs/stack.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/stack.md#requirements).
- [uv](https://docs.astral.sh/uv/), for the `ccobs` CLI (`uvx cc-observatory`; see
  [docs/cli.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/cli.md)).
- Claude Code 2.1.296 or later. That is the version cc-observatory was tested against
  ([ADR 0012](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0012-observed-telemetry-and-hook-contract.md));
  older versions are not supported until they are tested.

## Roadmap

| Milestone | Release | Scope                                                                                  | Status      |
| --------- | ------- | -------------------------------------------------------------------------------------- | ----------- |
| 0         | v0.1.0  | Foundation, tooling, CI, ADRs, telemetry and hooks spike, fixtures, first PyPI release | Released    |
| 1         | v0.2.0  | Observability stack in compose, provisioned Grafana                                    | Released    |
| 2         | v0.3.0  | Hook ingestion, session state machine, SQLite, blocks, `/metrics`                      | Released    |
| 3         | v0.4.0  | Web UI (HTMX + SSE)                                                                    | Released    |
| 4         | v0.5.0  | `ccobs setup`, `uninstall`, `doctor`, `status`                                         | Planned     |
| 5         | v0.6.0  | Alerts (browser and Discord)                                                           | Planned     |
| 6         | v0.7.0  | Grafana dashboards and polish                                                          | Planned     |
| 7         | v1.0.0  | Distribution: GHCR multi-arch images, release hardening, contributor docs              | Planned     |

## Development

You need [uv](https://docs.astral.sh/uv/); it installs the right Python version for you.

~~~bash
uv sync                                # create the virtualenv with the dev dependencies
uv run pre-commit install              # install the pre-commit and commit-msg hooks
uv run pytest                          # unit tests with the coverage gate
uv run mypy                            # strict type checks
uv run pre-commit run --all-files      # every check that the CI lint job runs
uv run pytest -m integration --no-cov  # integration tests (need Docker)
~~~

- The integration tests start their own compose project, `ccobs-it`, on ports of their
  own, so they can run next to your stack; see
  [docs/stack.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/stack.md#running-the-integration-tests).
- The project uses Gitflow. `develop` is the default branch: feature pull requests
  target `develop` and are squash-merged, and release pull requests go to `main` with
  merge commits.
- The title of a feature pull request becomes its commit on `develop` (and, for `feat`,
  `fix`, `refactor` and `perf`, a changelog line), so every PR title must
  be a [Conventional Commit](https://www.conventionalcommits.org/) (for example,
  `feat: add the session state machine`). CI checks it.
- A contributor guide comes with v1.0.0.

## Disclaimer

cc-observatory is an independent open-source project. It is not affiliated with,
endorsed by or sponsored by Anthropic. "Claude" and "Claude Code" are trademarks of
Anthropic, PBC.

## License

[MIT](https://github.com/Luanderson-Dev/cc-observatory/blob/main/LICENSE). Copyright (c) 2026 Luanderson Pimenta Mendes (Luanderson-Dev).
