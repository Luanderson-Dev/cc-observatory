# cc-observatory

Local observability for Claude Code: see your sessions, subagents, blocks, logs, tokens
and cost, on your own machine.

[Leia em português](https://github.com/Luanderson-Dev/cc-observatory/blob/main/README.pt-BR.md)

[![CI](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml?query=branch%3Adevelop)
[![PyPI](https://img.shields.io/pypi/v/cc-observatory)](https://pypi.org/project/cc-observatory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/Luanderson-Dev/cc-observatory/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

## Status

**Pre-alpha.** v0.1.0 is the first release and closes milestone 0 of 8 (foundation).
There is still nothing useful to install.

What v0.1.0 contains:

- the repository foundation: the Python package `cc-observatory` (import package
  `ccobs`), published to [PyPI](https://pypi.org/project/cc-observatory/), with a
  `ccobs` CLI that only prints its version;
- tooling: uv, ruff, mypy in strict mode, pytest with a coverage gate, pre-commit and
  Commitizen;
- CI on GitHub Actions: lint, type checks and tests on Linux, macOS and Windows, a
  package build, a Conventional Commits check on PR titles, and a release workflow that
  publishes to PyPI with trusted publishing;
- architecture decision records (ADRs) 0001 to 0012 for the decisions made so far,
  including how hooks reach the app
  ([ADR 0011](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0011-hook-delivery-http-plus-async-sessionstart.md))
  and what Claude Code 2.1.296 really sends in its telemetry and hooks
  ([ADR 0012](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0012-observed-telemetry-and-hook-contract.md));
- fixture capture tooling for development (`tools/capture/`, not part of the package) and
  sanitized test fixtures recorded from real, isolated Claude Code sessions
  (`tests/fixtures/`).

What comes next: milestone 1 (v0.2.0), the observability stack in Docker Compose with a
provisioned Grafana. The full plan is in the roadmap below.

Unless a section says otherwise, the rest of this README describes the planned design.

## What it will show

None of this works yet. The release that is planned to bring each part is in
parentheses.

- **Live overview** of the Claude Code sessions on your machine: which ones are working,
  waiting on you, idle or ended (v0.4.0).
- **Agent tree**: each session's main agent and the subagents it started (v0.4.0).
- **Blocks**: sessions waiting on you, such as a permission prompt, an idle prompt, an
  MCP elicitation or a failed API request, with how long each one has been waiting
  (tracked from v0.3.0, shown in the web UI from v0.4.0).
- **Events and logs** for each session (v0.4.0).
- **Tokens and cost** (v0.4.0) by day, model and agent type (built-in agent names; your
  own agents show as `custom` unless you opt in to tool details) and by project (joined
  from the working directory that hooks report). Cost is an estimate that comes from
  Claude Code's telemetry, not the amount you are billed.
- **Health page**: whether each part of the stack is up and receiving data (v0.4.0).
- **Grafana** for deeper analysis, provisioned with its datasources (v0.2.0) and with
  dashboards that the app links to, already filtered to the session, agent or project
  you were looking at (v0.7.0).
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
- **The app** also queries Prometheus and Loki over HTTP for its token, cost and event
  views.
- **Grafana** comes provisioned with the Prometheus, Loki and Tempo datasources and with
  the dashboards.
- **Why both sources:** telemetry tells you what a session consumed (tokens, cost, API
  calls, tool results) but not that it is waiting on you right now, which only hooks
  report, at the moment it happens.

More detail for contributors is in [docs/architecture.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/architecture.md).
The decisions behind this design, and their reasons, are recorded as architecture decision records in [docs/adr/](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/README.md).

## Quick start (planned, available from v0.5.0)

**These steps do not work yet.** The stack arrives in v0.2.0 and the `setup` command in
v0.5.0.

1. Start the stack:

   ~~~bash
   git clone https://github.com/Luanderson-Dev/cc-observatory
   cd cc-observatory
   docker compose up -d
   ~~~

2. Connect Claude Code to it:

   ~~~bash
   uvx cc-observatory setup
   ~~~

   `setup` backs up `~/.claude/settings.json` and merges in the telemetry `env` block
   and the hooks without touching your existing entries, marking what it adds so that
   `ccobs uninstall` removes only that; `--dry-run` shows the diff without writing
   anything.

3. Open the app at the address that `setup` prints.
4. Restart Claude Code so that new sessions load the new settings.

`ccobs` and `cc-observatory` are the same CLI; `uvx cc-observatory <command>` runs it
without installing it.

## Privacy

- **Metadata only by default.** Prompt text, tool inputs and outputs, assistant messages
  and error text are not stored.
- **Content capture is an explicit opt-in.** `ccobs setup --capture-content` shows a
  warning and then turns content capture on in Claude Code's telemetry settings and in
  the stack (the collector and the app). Without it, the collector drops content from the
  telemetry even if Claude Code was set to send it.
- **Personal attributes are removed.** The collector deletes your email address and your
  user, account and organization IDs from the telemetry before anything is stored.
- **Everything stays on your machine.** That is a design goal that the bundled configs
  enforce: every port binds to `127.0.0.1`, and the usage reporting and update checks of
  Grafana, Loki and Tempo are turned off. At runtime, nothing leaves your machine except
  the optional Discord webhook, and only if you configure one.
- **Retention is configurable** (the observability stack from v0.2.0, the app database
  from v0.3.0), so old data is deleted after the period you choose.

See [SECURITY.md](https://github.com/Luanderson-Dev/cc-observatory/blob/main/SECURITY.md) for the security model and how to report a
vulnerability.

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

## Requirements (planned)

- Docker with Compose v2 (Docker Desktop on Windows and macOS, or Docker Engine with the
  Compose plugin on Linux).
- [uv](https://docs.astral.sh/uv/).
- Claude Code 2.1.296 or later. That is the version cc-observatory was tested against
  ([ADR 0012](https://github.com/Luanderson-Dev/cc-observatory/blob/main/docs/adr/0012-observed-telemetry-and-hook-contract.md));
  older versions are not supported until they are tested.

## Roadmap

| Milestone | Release | Scope                                                                                  | Status      |
| --------- | ------- | -------------------------------------------------------------------------------------- | ----------- |
| 0         | v0.1.0  | Foundation, tooling, CI, ADRs, telemetry and hooks spike, fixtures, first PyPI release | Released    |
| 1         | v0.2.0  | Observability stack in compose, provisioned Grafana                                    | Planned     |
| 2         | v0.3.0  | Hook ingestion, session state machine, SQLite, blocks, `/metrics`                      | Planned     |
| 3         | v0.4.0  | Web UI (HTMX + SSE)                                                                    | Planned     |
| 4         | v0.5.0  | `ccobs setup`, `uninstall`, `doctor`, `status`                                         | Planned     |
| 5         | v0.6.0  | Alerts (browser and Discord)                                                           | Planned     |
| 6         | v0.7.0  | Grafana dashboards and polish                                                          | Planned     |
| 7         | v1.0.0  | Distribution: GHCR multi-arch images, release hardening, contributor docs              | Planned     |

## Development

You need [uv](https://docs.astral.sh/uv/); it installs the right Python version for you.

~~~bash
uv sync                            # create the virtualenv with the dev dependencies
uv run pre-commit install          # install the pre-commit and commit-msg hooks
uv run pytest                      # unit tests with the coverage gate
uv run mypy                        # strict type checks
uv run pre-commit run --all-files  # every check that the CI lint job runs
~~~

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
