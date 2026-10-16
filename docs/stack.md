# The compose stack

A reference for running the observability stack: what runs, how to start, stop and
update it, its settings, where its data lives and for how long, how much memory and disk
it uses, and what to check when something goes wrong. The decisions behind it are
recorded as ADRs in [`adr/`](adr/README.md); this page links to them instead of
repeating their reasons. [app.md](app.md) is the reference for the app itself: pointing
Claude Code at it, its API, metrics, data and troubleshooting.

Commands run from the repository root, where `compose.yaml` is, in bash (Git Bash on
Windows).

## What runs

| Service          | Image                                                              | Stores                                           | Networks          | Published on `127.0.0.1`            |
| ---------------- | ------------------------------------------------------------------ | ------------------------------------------------ | ----------------- | ----------------------------------- |
| `otel-collector` | `cc-observatory-otel-collector:0.162.0`, built locally (see below) | Nothing on disk; its export queues are in memory | `backend`, `edge` | 4317 (OTLP gRPC), 4318 (OTLP HTTP)  |
| `prometheus`     | `prom/prometheus:v3.13.4`                                          | Metrics                                          | `backend`         | none (4390 with the debug override) |
| `loki`           | `grafana/loki:3.7.8`                                               | Events (OpenTelemetry log records)               | `backend`         | none (4310 with the debug override) |
| `tempo`          | `grafana/tempo:3.1.0`, only with `--profile traces`                | Traces                                           | `backend`         | none (4320 with the debug override) |
| `grafana`        | `grafana/grafana:13.2.4`                                           | Its own database                                 | `backend`, `edge` | 4330                                |
| `app`            | `cc-observatory-app:local`, built locally (see below)              | Sessions, agents, blocks and hook events         | `backend`, `edge` | 4300 (4301 with the debug override) |

- **Images.** Every upstream image is pinned to an exact tag and its multi-arch index
  digest (`name:tag@sha256:...`), in `compose.yaml` and in the collector's Dockerfile
  ([ADR 0020](adr/0020-compose-conventions.md)). The collector image is
  `otel/opentelemetry-collector-contrib:0.162.0` plus a static busybox binary from
  `busybox:1.38.0-musl`, used by the healthcheck
  ([`stack/otel-collector/Dockerfile`](../stack/otel-collector/Dockerfile)). Compose
  builds it on your machine (`pull_policy: build`, from the build cache after the first
  time) under a local tag with no digest, and never pushes it.
- **The app's image** is built the same way from the clone, with the repository root as
  context ([`stack/app/Dockerfile`](../stack/app/Dockerfile),
  [ADR 0030](adr/0030-the-app-container.md)): `python:3.12.15-slim-trixie` with the
  locked dependencies in a virtual environment, about 250 MB. Its name has no version,
  because it is the code of your clone, whatever its branch; no registry has it, so
  `docker compose pull` never updates it, and `docker compose up -d` rebuilds it when
  the source changes (`docker compose build app` rebuilds it alone).
- **Path of the data.** Claude Code sends OTLP to the collector, which deletes identity
  attributes and content ([ADR 0021](adr/0021-collector-privacy-filters.md); an opt-in
  to keep content comes with `ccobs setup` in v0.5.0,
  [ADR 0006](adr/0006-privacy-metadata-only-by-default.md)), then sets the host label
  ([ADR 0022](adr/0022-the-host-label.md)). It sends metrics to Prometheus's OTLP
  receiver ([ADR 0013](adr/0013-metrics-through-the-prometheus-otlp-receiver.md)),
  events to Loki's OTLP endpoint ([ADR 0015](adr/0015-loki-through-its-otlp-endpoint.md))
  and traces to Tempo ([ADR 0003](adr/0003-observability-backends.md)). Prometheus also
  scrapes its own metrics and the collector's (`otelcol_*`, on port 8888 inside the
  stack's networks). Grafana queries the three stores. Claude Code's hooks go to the
  app, which keeps sessions, agents and blocks in SQLite, reads Claude Code's
  `tool_decision` events back from Loki, and serves its live state on a metrics listener
  (port 9464 inside the stack, never published) that Prometheus scrapes as the job
  `ccobs-app` ([app.md](app.md)).
- **Networks** ([ADR 0018](adr/0018-ports-networks-and-exposure.md)). `backend` is an
  internal network with no route out, so Prometheus, Loki and Tempo, which join only
  `backend`, cannot reach the internet. `edge` is a normal bridge network for the
  three services that publish ports: the collector, Grafana and the app. Every published
  port binds to `127.0.0.1` only.
- **Debug override.** `compose.debug.yaml` publishes Prometheus, Loki and Tempo on
  loopback, for debugging, and the app's metrics listener on `127.0.0.1:4301`
  (`CCOBS_APP_METRICS_PORT`). It also attaches the three stores to `edge`, which gives
  them egress while it is in use. To unpublish them, run `docker compose up -d` again
  without it (`docker compose --profile traces up -d` if Tempo runs): without the
  profile, a running Tempo keeps its published port and stays on `edge`.

  ~~~bash
  docker compose -f compose.yaml -f compose.debug.yaml up -d
  docker compose -f compose.yaml -f compose.debug.yaml --profile traces up -d   # with Tempo
  ~~~

- **Grafana** answers only at `http://localhost:<port>`: any other host name,
  `127.0.0.1` included, redirects there. There is no login; whoever opens it is an
  Editor ([ADR 0017](adr/0017-grafana-access-and-dns-rebinding.md)).
- **Every service** has `restart: unless-stopped`, a healthcheck, a memory limit,
  `no-new-privileges`, no Linux capabilities (`cap_drop: [ALL]`), container logs capped
  at three files of 10 MB, and 30 seconds to stop cleanly after SIGTERM (15 for Tempo).
  Every container runs as a non-root user.
- **The app stops well inside its 30 seconds.** After SIGTERM, uvicorn waits at most 5
  seconds for open requests; then the app stops its metrics listener, then its Loki
  reconciler and its sweeper (up to 5 seconds each), and gives its writer up to 5
  seconds to store the hooks still queued, counting any left as dropped. The worst case
  is about 20 seconds; measured, `docker compose stop app` took 1.2 seconds and the
  container exited with code 0 ([ADR 0030](adr/0030-the-app-container.md)).

## Start, stop and update

~~~bash
docker compose up -d                     # start; the first time also pulls and builds the images
docker compose --profile traces up -d    # start with Tempo
docker compose ps                        # state and health of each service
~~~

Then open <http://localhost:4330> (the port is `CCOBS_GRAFANA_PORT`). Unlike Claude
Code's exporters, which use `127.0.0.1`, Grafana must be opened as `localhost`, since it
redirects any other host name there; browsers fall back to `127.0.0.1` when `localhost`
also resolves to `::1`. The home page is the **Claude Code overview** dashboard, also in
the *Claude Code* folder under Dashboards: cost, tokens, sessions, active time, code
changes, API errors and recent events, for the last 24 hours by default, refreshed every
minute, with Host and Model filters. Traces are in Explore, with the Tempo datasource;
without Tempo, that datasource shows an error and nothing else is affected.

The app answers at <http://127.0.0.1:4300> (the port is `CCOBS_APP_PORT`): its hook
endpoint, `/healthz` and its JSON API, such as `/api/v1/sessions`; its web UI comes in
v0.4.0. Claude Code sends it hooks only once you point Claude Code at it
([app.md](app.md#point-claude-code-at-the-app)).

| Command                            | Effect                                                                                                                                |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `docker compose stop`              | Stops the containers and keeps them. They stay stopped across Docker restarts and reboots; `docker compose start` or `up -d` resumes. |
| `docker compose down`              | Removes the containers and networks. The volumes, and so all stored data, stay.                                                       |
| `docker compose down -v`           | Also deletes the volumes: every stored metric, event and trace, Grafana's database and the app's. It cannot be undone.                |
| `docker compose restart <service>` | Restarts one service, for example after editing its files under `stack/`.                                                             |
| `docker compose logs <service>`    | Shows a service's logs.                                                                                                               |

- **Tempo's profile.** If you run Tempo, pass `--profile traces` every time you start or
  update the stack: `docker compose up -d` without it leaves a running Tempo as it is,
  without recreating it ([profiles](https://docs.docker.com/compose/how-tos/profiles/)).
  Setting `COMPOSE_PROFILES=traces` in `.env` passes the profile to every command.
  `ps`, `stop` and `down` include a running Tempo without it (checked with Compose
  5.5.1); passing it to them does no harm.
- **Restart policy** ([ADR 0019](adr/0019-restart-unless-stopped.md)). Docker restarts
  a container that exits, and starts the stack again when Docker itself starts, unless
  you stopped it with `docker compose stop`. After a reboot the stack comes back only if
  Docker starts with the machine: Docker Desktop does so only with its "Start Docker
  Desktop when you sign in to your computer" setting, which is off by default; on Linux,
  the Docker service must be enabled at boot.
- **Updating.** The stack's version is the version of your clone. Pull it and recreate
  the containers:

  ~~~bash
  git pull
  docker compose up -d --force-recreate    # add --profile traces if you run Tempo
  ~~~

  Compose pulls any newly pinned image and rebuilds the collector's and the app's images
  when their sources change. It recreates a container only when its definition in the
  compose files or its image changes, not when a mounted file under `stack/` changes,
  hence `--force-recreate`; the data stays in the volumes.
  The images of earlier versions stay on disk: `docker image ls` lists them and
  `docker image rm <image>` removes one.

## Configuration

Every setting is an environment variable with a default in the compose files, so `.env`
is optional. To change one, copy [`.env.example`](../.env.example) to `.env`, edit the
value and run `docker compose up -d` (with `--profile traces` if you run Tempo): Compose
recreates the services whose settings changed. Git ignores `.env`.

| Variable                     | Default            | Effect                                                                                                                                   |
| ---------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `CCOBS_OTLP_GRPC_PORT`       | `4317`             | Host port of the collector's OTLP/gRPC receiver, for Claude Code's `grpc` protocol.                                                      |
| `CCOBS_OTLP_HTTP_PORT`       | `4318`             | Host port of the collector's OTLP/HTTP receiver, for Claude Code's `http/protobuf` and `http/json` protocols.                            |
| `CCOBS_GRAFANA_PORT`         | `4330`             | Host port of Grafana, and the port of its root URL, which Grafana redirects to.                                                          |
| `CCOBS_HOST_LABEL`           | `local`            | The `host` label on every metric, event and trace, and on the app's rows and series ([ADR 0022](adr/0022-the-host-label.md)); see below. |
| `CCOBS_PROMETHEUS_RETENTION` | `90d`              | How long Prometheus keeps metrics, in Prometheus's units: `y`, `w`, `d`, `h`, `m`, `s` or `ms`.                                          |
| `CCOBS_LOKI_RETENTION`       | `720h`             | How long Loki keeps events, as a multiple of `24h`. It is also how far back a Loki query reaches.                                        |
| `CCOBS_TEMPO_RETENTION`      | `168h`             | How long Tempo keeps traces, as a Go duration in hours at most. Tempo refuses days, such as `7d`, and does not start.                    |
| `CCOBS_PROMETHEUS_PORT`      | `4390`             | Host port of Prometheus's HTTP API; debug override only.                                                                                 |
| `CCOBS_LOKI_PORT`            | `4310`             | Host port of Loki's HTTP API; debug override only.                                                                                       |
| `CCOBS_TEMPO_PORT`           | `4320`             | Host port of Tempo's HTTP API; debug override only, with the `traces` profile.                                                           |
| `CCOBS_APP_PORT`             | `4300`             | Host port of the app: the hook URL that Claude Code posts to, the API and `/healthz`.                                                    |
| `CCOBS_APP_METRICS_PORT`     | `4301`             | Host port of the app's metrics listener; debug override only.                                                                            |
| `CCOBS_APP_RETENTION`        | `90d`              | How long the app keeps a session after its last event, in Prometheus's units; at least `1d` and never below `CCOBS_SESSION_STALE_AFTER`. |
| `CCOBS_SESSION_STALE_AFTER`  | `12h`              | How long a session or agent can stay silent before the app marks it stale, in the same units; at least `1h`.                             |
| `CCOBS_TELEMETRY_RECONCILE`  | `true`             | Whether the app reads Claude Code's `tool_decision` events from Loki to close permission prompts denied with Esc.                        |
| `CCOBS_LOKI_URL`             | `http://loki:3100` | Loki's HTTP API as the app reaches it inside the stack.                                                                                  |
| `CCOBS_LOG_LEVEL`            | `info`             | The app's log level: `debug`, `info`, `warning` or `error`.                                                                              |

Advanced: the app's hook queue and body limits
([ADR 0027](adr/0027-the-app-http-surface-and-hook-queue.md)). A hook that does not fit
is dropped, still answered with an empty 2xx, and counted in
`ccobs_hook_events_dropped_total`.

| Variable                     | Default    | Effect                                                                                     |
| ---------------------------- | ---------- | ------------------------------------------------------------------------------------------ |
| `CCOBS_HOOK_QUEUE_MAX_ITEMS` | `10000`    | Most hooks the queue holds at once, 1 to 1000000.                                          |
| `CCOBS_HOOK_QUEUE_MAX_BYTES` | `67108864` | Most body bytes the queue holds at once (64 MiB), 1 MiB to 1 GiB.                          |
| `CCOBS_HOOK_BODY_MAX_BYTES`  | `8388608`  | Largest hook body read (8 MiB), 64 KiB to 64 MiB and at most `CCOBS_HOOK_QUEUE_MAX_BYTES`. |

The app checks its own settings at start-up and stops with a message that names each
invalid variable; [app.md](app.md#configuration) has the details, including
`CCOBS_LISTEN_HOST`, which the image sets, and the three other settings that compose
never passes.

`CCOBS_HOST_LABEL` takes letters, digits, `.`, `_` and `-`. The stack does not check the
value: a double quote or a backslash can stop the collector at start-up or change the
value. A new value starts new series and a new Loki stream, so a query over a period
that spans the change must aggregate without `host`. Set it to the machine's hostname
only if you want the hostname stored.

## Data and retention

| Volume            | Mounted at         | Holds                                                                                                                                            |
| ----------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `prometheus-data` | `/prometheus`      | Prometheus's TSDB: metric blocks and the write-ahead log.                                                                                        |
| `loki-data`       | `/loki`            | Loki's chunks, index, write-ahead log and compactor state.                                                                                       |
| `tempo-data`      | `/var/tempo`       | Tempo's write-ahead log and trace blocks (`traces` profile).                                                                                     |
| `grafana-data`    | `/var/lib/grafana` | Grafana's database: dashboards saved in the UI, stars and preferences. The datasources and the provisioned dashboard come from `stack/grafana/`. |
| `app-data`        | `/var/lib/ccobs`   | The app's SQLite database, `ccobs.db`, and its WAL files: sessions, agents, blocks and hook events, metadata only.                               |

Docker names them `cc-observatory_<volume>`, because `compose.yaml` fixes the project
name; `docker system df -v` shows their sizes. The collector has no volume: at a stop
it lets the export attempts in progress finish (5 s at most per store) and drops the
requests waiting to be retried, so what it has not delivered by then is lost.

| Data         | Default                                      | Variable                     | Applied by                     |
| ------------ | -------------------------------------------- | ---------------------------- | ------------------------------ |
| Metrics      | 90 days (`90d`)                              | `CCOBS_PROMETHEUS_RETENTION` | Prometheus's TSDB retention    |
| Events       | 30 days (`720h`)                             | `CCOBS_LOKI_RETENTION`       | Loki's compactor               |
| Traces       | 7 days (`168h`)                              | `CCOBS_TEMPO_RETENTION`      | Tempo's backend worker         |
| App sessions | 90 days (`90d`) after a session's last event | `CCOBS_APP_RETENTION`        | The app's hourly retention job |

- The defaults and the format of each variable come from
  [ADR 0016](adr/0016-cardinality-and-retention.md). To change one, set its variable in
  `.env` and run `docker compose up -d` (`docker compose --profile traces up -d` for
  `CCOBS_TEMPO_RETENTION`). Lowering a retention deletes the older data at the next
  cleanup; raising it does not bring back what was deleted.
- Data goes some time after it expires. Prometheus removes a block only once all of it
  has expired, which "may take up to two hours"
  ([storage](https://prometheus.io/docs/prometheus/latest/storage/)). Loki's compactor
  marks expired chunks every 15 minutes and deletes them 2 hours later.
- Loki answers a query only as far back as its retention (`max_query_lookback`), and its
  query length limit is off. A longer range, such as "Last 90 days" in the dashboard
  (Prometheus keeps 90 days), shows events from the start of Loki's retention instead of
  failing.
- The app deletes only ended and stale sessions, with their agents, blocks and hook
  events; a session still open is never deleted. Its retention job runs about a minute
  after the app starts, then every hour, and a sweep every minute marks sessions silent
  for `CCOBS_SESSION_STALE_AFTER` stale ([app.md](app.md#data-and-retention)).
- Disk use over 30 to 90 days of real use has not been measured. ADR 0016 estimates
  roughly 1 MB of metrics a day at 50 sessions a day.

## Memory and disk

### Limits

| Service          | `mem_limit` | `GOMEMLIMIT` |
| ---------------- | ----------- | ------------ |
| `otel-collector` | 256m        | 204MiB       |
| `prometheus`     | 512m        | 409MiB       |
| `loki`           | 512m        | 409MiB       |
| `tempo`          | 512m        | 409MiB       |
| `grafana`        | 768m        | 614MiB       |
| `app`            | 256m        | none         |

- The limits add up to 2304 MiB without Tempo and 2816 MiB with it.
- Every service but the app is a Go program, and `GOMEMLIMIT` is about 80% of its limit,
  so the Go runtime collects garbage before the container reaches the limit
  ([ADR 0020](adr/0020-compose-conventions.md)). The app is a Python process, which
  `GOMEMLIMIT` does not tune; its limit leaves room for the hook queue, which may hold
  up to 64 MiB of bodies (`CCOBS_HOOK_QUEUE_MAX_BYTES`) on top of its steady state
  ([ADR 0030](adr/0030-the-app-container.md)). The collector's `memory_limiter`
  processor, first in every pipeline, is set in percentages of the container's limit
  (`limit_percentage: 80`, `spike_limit_percentage: 25`): it refuses new data, with an
  error the sender may retry, once the collector's heap passes its soft limit, 55% of
  the container's limit (80% minus 25%), and above 80% it also forces garbage
  collection.
- **Grafana has 768m** because it also runs the plugins of its three datasources
  (Prometheus, Loki and Tempo) as separate processes, whose memory `GOMEMLIMIT` does not
  cover; the other datasource plugins bundled with it are disabled. Loading its UI in a
  browser takes it well above the numbers measured through the API (see
  [Measured use](#measured-use)).
- A process that takes its container over the limit is killed by the kernel; when it is
  the main process, Docker restarts the container, and
  `docker inspect --format '{{.State.OOMKilled}}' "$(docker compose ps -q <service>)"`
  prints `true`.

### Measured use

Measured on 2026-10-15 with Docker Desktop on Windows 11 (WSL 2 backend, a VM with 4
CPUs and 7.8 GiB), Docker Engine 29.8.2. Two runs, without and with Tempo, went through
four phases each, in order:

1. **First start**: right after every service became healthy.
2. **Idle**: 10 minutes with no traffic.
3. **Replay**: the project's sanitized fixtures (`tests/fixtures/otlp/`) replayed five
   times through the collector with `tools/stack/replay.py`.
4. **Dashboard**: the overview dashboard's queries, sent through Grafana's query API as
   its page sends them, for the last 24 hours, 7 days and 90 days, three times each.

Each cell is *end / sampled peak*, in MiB: the usage at the end of the phase and the
highest sample during it, from `python -m tools.stack.memory` (which reads
`docker stats`: a container's memory minus its inactive page cache). During the replay
and the dashboard queries the readings ran back to back, about 2 seconds apart; while
idle and while the stack settled after each phase, about 10 seconds apart. Sampling
misses spikes shorter than its interval, so the last column is the cgroup's
`memory.peak` at the end of the run: the exact high-water mark since the container
started, page cache included.

Without Tempo (`docker compose up -d`):

| Service          | First start   | Idle          | Replay        | Dashboard     | `memory.peak` |
| ---------------- | ------------- | ------------- | ------------- | ------------- | ------------- |
| `otel-collector` | 45.8 / 46.0   | 40.1 / 50.8   | 63.9 / 64.6   | 62.6 / 64.8   | 68.6          |
| `prometheus`     | 32.8 / 37.6   | 32.3 / 36.6   | 37.0 / 37.0   | 42.5 / 42.7   | 46.6          |
| `loki`           | 33.0 / 33.2   | 48.4 / 48.6   | 69.8 / 70.4   | 89.0 / 111.3  | 137.4         |
| `grafana`        | 165.6 / 165.7 | 141.8 / 166.1 | 141.3 / 142.0 | 202.2 / 202.3 | 210.5         |
| Total            | 277.2 / 280.6 | 262.7 / 289.1 | 312.1 / 312.8 | 396.3 / 417.1 |               |

With Tempo (`docker compose --profile traces up -d`):

| Service          | First start   | Idle          | Replay        | Dashboard     | `memory.peak` |
| ---------------- | ------------- | ------------- | ------------- | ------------- | ------------- |
| `otel-collector` | 43.1 / 43.2   | 39.8 / 43.6   | 68.6 / 68.8   | 55.9 / 68.9   | 72.8          |
| `prometheus`     | 29.1 / 34.7   | 33.1 / 33.1   | 34.5 / 34.5   | 37.7 / 37.7   | 42.2          |
| `loki`           | 33.5 / 33.9   | 48.6 / 48.6   | 64.5 / 65.0   | 84.4 / 96.9   | 140.7         |
| `tempo`          | 18.7 / 18.7   | 25.3 / 25.8   | 39.8 / 39.8   | 42.3 / 42.6   | 71.8          |
| `grafana`        | 165.7 / 165.9 | 140.1 / 166.1 | 140.0 / 144.2 | 204.2 / 208.2 | 213.8         |
| Total            | 290.1 / 294.1 | 286.8 / 300.9 | 347.5 / 347.5 | 424.6 / 446.3 |               |

- The stack used about 260 to 290 MiB idle and 420 to 450 MiB at the dashboard peak.
  These runs came before the app joined the stack; its own figures are below. The limits
  now add up to 2304 MiB (2816 MiB with Tempo).
- A total's sampled peak is the highest sum of one sample, not the sum of each service's
  peak.
- Loki's high-water mark (137.4 and 140.7 MiB) is well above its highest sample (111.3
  and 96.9 MiB): `memory.peak` also counts page cache from its write-ahead log and
  chunks, and catches spikes shorter than the sampling interval.
- The run with Tempo replayed traces too, and Tempo's high-water mark was read after 40
  TraceQL searches; the run without Tempo replayed metrics and events only.
- These runs loaded the dashboard through the API, not in a browser. Loading Grafana's
  UI 30 times as a browser does (the page, its scripts and the first API calls), with
  the dashboard's queries in between, took Grafana to a high-water mark of 627.3 MiB,
  below its 768m limit, and it settled at about 250 MiB.
- On a host that has not read Grafana's files yet, its binaries can show up as extra
  page cache at first start, which the kernel can reclaim.
- The runs held a few replayed sessions. Memory with 30 to 90 days of real history,
  where Prometheus and Loki hold and query much more data, has not been measured.

The app was measured on its own, with the same Docker Desktop 29.8 (Windows 11, WSL 2)
and `tools/stack/memory.py`, when it joined the stack, in MiB:

| Phase                                   | Usage | `memory.peak` |
| --------------------------------------- | ----- | ------------- |
| Idle, after a fresh start               | 63.8  | 79.1          |
| After replaying the fixtures' 131 hooks | 66.5  | 83.0          |
| After 2,620 more hooks                  | 70.1  | 89.3          |
| After a restart with that data          | 63.3  |               |

- The peak includes the healthcheck's short-lived Python process. The app stays far
  below its 256m limit.
- In the same measurement the other services, idle, used about 167 MiB (Grafana), 49
  MiB (the collector), 44 MiB (Prometheus) and 39 MiB (Loki).

### Docker Desktop's memory

Docker Desktop runs the containers in a VM with a fixed amount of memory. Leave at least
2304 MiB of it for the stack (2816 MiB with Tempo), the sum of the limits above, on top
of what your other containers use: with less, the VM can run out of memory before a
service reaches its own limit.

- macOS, Linux, and Windows with Hyper-V: *Settings > Resources > Advanced > Memory
  limit*, by default half of the host's memory
  ([settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)).
- Windows with WSL 2: `memory` under `[wsl2]` in `%UserProfile%\.wslconfig`, by default
  half of Windows's memory, or the WSL Settings app; `wsl --shutdown` applies it
  ([WSL configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)).

### Download and disk

Compressed download sizes, from the registry manifests:

| Image                                          | amd64       | arm64       |
| ---------------------------------------------- | ----------- | ----------- |
| `otel/opentelemetry-collector-contrib:0.162.0` | 112.7 MB    | 102.6 MB    |
| `busybox:1.38.0-musl` (collector build)        | 0.9 MB      | 0.9 MB      |
| `prom/prometheus:v3.13.4`                      | 106.7 MB    | 98.2 MB     |
| `grafana/loki:3.7.8`                           | 48.0 MB     | 42.7 MB     |
| `grafana/grafana:13.2.4`                       | 482.6 MB    | 445.1 MB    |
| **Default stack**                              | **~751 MB** | **~690 MB** |
| `grafana/tempo:3.1.0` (`traces` profile)       | 40.9 MB     | 35.9 MB     |
| **With Tempo**                                 | **~792 MB** | **~725 MB** |

Unpacked, the default stack's images take about 3.0 GB of disk (amd64, containerd image
store): Grafana 1938 MB, the collector about 520 MB, Prometheus 362 MB and Loki 211 MB.
Tempo adds 178 MB. The data volumes come on top.

The app's image is built on your machine from the `python:3.12.15-slim-trixie` and
`ghcr.io/astral-sh/uv:0.12.24` images, which the first build downloads (their download
size is not measured here); the built image is about 250 MB as Docker Desktop 29.8
reports it, 54 MB of which is the app's virtual environment
([ADR 0030](adr/0030-the-app-container.md)).

## Measuring memory

[`tools/stack/memory.py`](../tools/stack/memory.py), a development tool in the
repository (standard library only, not part of the published package), prints the
memory of a compose project's containers, per service. `uv run` sets up the project's
development environment on first use; with Python 3.12 or later,
`python3 -m tools.stack.memory` from the repository root also works.

~~~bash
uv run python -m tools.stack.memory                              # cc-observatory, one reading
uv run python -m tools.stack.memory --samples 50 --interval 10  # about ten minutes, keeping peaks
uv run python -m tools.stack.memory --project ccobs-it --json   # another project, as JSON
~~~

| Option           | Default          | Meaning                                                                |
| ---------------- | ---------------- | ---------------------------------------------------------------------- |
| `--project NAME` | `cc-observatory` | The compose project.                                                   |
| `--samples N`    | `1`              | Readings to take; each service keeps its peak.                         |
| `--interval S`   | `5`              | Seconds between two readings; each reading takes about 2 seconds more. |
| `--json`         | off              | JSON (bytes, MiB and percentages) instead of a table.                  |

The table has one line per service, with `USED` (the last reading), `PEAK` (the
highest reading), `LIMIT` and both as percentages of the limit, then a `total` line
whose peak is the highest sum of one reading. The command exits with 1 when no reading
succeeds: Docker fails at the first one, or the project has no running container. A
later reading that fails is reported on stderr and skipped, a container whose stats
Docker could not read (`-- / --`) is left out of that reading, and Ctrl+C prints the
summary of the readings so far. Usage is what `docker stats` reports, which Docker
rounds to four significant digits; a spike between two readings is missed.

For the exact high-water mark, read the cgroup's `memory.peak`: bytes, since the
container last started, page cache included. It needs cgroup v2 on Linux 5.19 or later,
which Docker Desktop has. In the containers that have `cat`:

~~~bash
docker compose exec prometheus cat /sys/fs/cgroup/memory.peak
docker compose exec grafana cat /sys/fs/cgroup/memory.peak
docker compose exec otel-collector /busybox cat /sys/fs/cgroup/memory.peak
~~~

Loki and Tempo have no shell or `cat`: read their cgroup from a throwaway container that
mounts the host's cgroup tree. The path depends on Docker's cgroup driver
(`docker info --format '{{.CgroupDriver}}'`):

~~~bash
id=$(docker inspect --format '{{.Id}}' "$(docker compose ps -q loki)")
docker run --rm --network none -v /sys/fs/cgroup:/cg:ro busybox:1.38.0-musl \
  cat "/cg/docker/$id/memory.peak"                     # cgroupfs driver (Docker Desktop)
# cat "/cg/system.slice/docker-$id.scope/memory.peak"  # systemd driver (most Linux hosts)
~~~

In Git Bash on Windows, run these commands with `MSYS_NO_PATHCONV=1` set, so that the
container paths are not turned into Windows paths.

## Requirements

- Docker Engine 25.0 or later, for the healthchecks' `start_interval`.
- The Docker Compose plugin (`docker compose`, not the old `docker-compose`) 2.20.2 or
  later.
- The Buildx plugin 0.17.0 or later, if it is installed: Compose requires that version
  when the plugin is present. Without Buildx, Compose builds the collector image with
  the deprecated classic builder, with a warning.
- Docker Desktop ships all three. On Linux, use Docker's own packages (`docker-ce`,
  `docker-compose-plugin`, `docker-buildx-plugin`): distribution packages such as Debian
  12's `docker.io` (20.10.24) are too old. On Linux, Docker Engine releases older than
  28.0 also let hosts on the same local network segment reach ports published to
  `127.0.0.1` ([port publishing](https://docs.docker.com/engine/network/port-publishing/)).
- About 750 MB to download (790 MB with Tempo) and about 3 GB of disk for the images,
  on amd64, plus the app's base images and its 250 MB image, and the data. Memory: see
  [Memory and disk](#memory-and-disk).
- The app's image builds on linux/amd64 and linux/arm64, the platforms the uv image
  offers; 32-bit ARM hosts cannot build it.
- Tested on Linux (GitHub Actions' Ubuntu runners) and on Windows 11 with Docker
  Desktop, both on amd64. macOS and arm64 have not been tested.

`docker version`, `docker compose version` and `docker buildx version` print the
versions. The reasons for each minimum are in
[ADR 0020](adr/0020-compose-conventions.md).

## Troubleshooting

### A port is already in use

`docker compose up` fails with an error such as `port is already allocated` or
`ports are not available`. Find what holds the port (`lsof -nP -iTCP:4330 -sTCP:LISTEN`
on Linux and macOS, `netstat -ano | grep ':4330'` on Windows), or move the stack's port:
set its variable in `.env` (see [Configuration](#configuration)) and run
`docker compose up -d`. If you move an OTLP port, change Claude Code's
`OTEL_EXPORTER_OTLP_ENDPOINT` to match.

### Windows: a port in an excluded range

Windows can reserve ranges of TCP ports (Hyper-V and WSL do), and Docker cannot publish
a port inside one; the error usually ends with `An attempt was made to access a socket
in a way forbidden by its access permissions`. List the ranges, then move any of the
stack's ports that falls in one with its variable in `.env`:

~~~bash
netsh int ipv4 show excludedportrange protocol=tcp
~~~

### Linux: the collector keeps restarting with "permission denied"

A clone made with a restrictive umask (such as `027` or `077`) leaves `stack/`
unreadable for the containers. They run as non-root users (the collector, Loki and Tempo
as uid 10001, Prometheus as `nobody`, Grafana as uid 472) and read their configuration
through read-only bind mounts of `stack/<service>/`. The collector exits with
"permission denied" and keeps restarting. Make the directory readable for them and
restart the stack:

~~~bash
chmod -R o+rX stack/
docker compose restart
~~~

Docker Desktop on macOS and Windows is not affected
([ADR 0020](adr/0020-compose-conventions.md)).

### SELinux: the containers cannot read `stack/`

On hosts that enforce SELinux (Fedora, RHEL), the bind mounts may need the `:z` suffix,
which relabels the host directory so that containers can read it. The stack does not set
it, because relabelling changes the files on the host
([ADR 0020](adr/0020-compose-conventions.md),
[bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)). To add it without
editing `compose.yaml`, create a `compose.override.yaml` next to it; each entry replaces
the mount with the same container path:

~~~yaml
services:
  otel-collector:
    volumes:
      - ./stack/otel-collector:/etc/ccobs/otel-collector:ro,z
  prometheus:
    volumes:
      - ./stack/prometheus:/etc/ccobs/prometheus:ro,z
  loki:
    volumes:
      - ./stack/loki:/etc/ccobs/loki:ro,z
  tempo:
    volumes:
      - ./stack/tempo:/etc/ccobs/tempo:ro,z
  grafana:
    volumes:
      - ./stack/grafana:/etc/ccobs/grafana:ro,z
~~~

Compose reads `compose.override.yaml` by itself only when no `-f` is given; with the
debug override, list it too
(`-f compose.yaml -f compose.override.yaml -f compose.debug.yaml`). Git shows the file
as untracked.

### Services restart or stay unhealthy

Check `docker compose ps` and
`docker inspect --format '{{.State.OOMKilled}}' "$(docker compose ps -q <service>)"`. If
a container was killed
for lack of memory, either the VM ran out (give Docker Desktop more memory: see
[Docker Desktop's memory](#docker-desktops-memory)) or the service reached its own
limit, which `uv run python -m tools.stack.memory --samples 12` shows as a peak close to
100%.

### The app is unhealthy, or Claude Code shows hook errors

`docker compose ps app` shows the app's health and `docker compose logs app` why it
failed: most often an invalid setting, which it names, or port 4300 taken by another
program. A hook-error notice in Claude Code's transcript means a hook could not reach
the app: the stack is down or starting, or the hook URL is wrong (it must use
`127.0.0.1`). [app.md](app.md#troubleshooting) covers these, a session stuck in
`working` or `waiting_user`, WAL being off and dropped hooks.

### An edited dashboard does not show on the home page

Grafana rereads `stack/grafana/dashboards/` every 30 seconds, so the copy in the
*Claude Code* folder updates by itself. The home page reads the same file, but Grafana
watches it for changes, which a mount without file events (Docker Desktop on Windows),
an editor that saves by renaming or a `git checkout` can miss. If the home page shows an
old version, restart Grafana:

~~~bash
docker compose restart grafana
~~~

### A dashboard cannot be saved

The provisioned dashboards come from the files in `stack/grafana/dashboards/`, which are
the source of truth: Grafana refuses to save or delete them from the UI or the API. Save
a copy under another name (*Save as*), which goes into Grafana's database, or edit the
JSON file.

### The collector logs errors about Tempo

If Claude Code sends traces (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and
`OTEL_TRACES_EXPORTER=otlp`) while the stack runs without the `traces` profile, the
collector cannot reach Tempo. It retries each request for 30 seconds and then drops it,
with an info line per attempt and a "Dropping data" error per request (the collector
samples its logs, so a burst shows about 10 identical lines per 10 seconds). Its trace
queue holds at most 20 requests, and while it is full new traces are refused at once,
each refusal logged as a "Rejecting data" error ("sending queue is full"). Metrics and
events are not affected, and the collector still stops cleanly: at `docker compose stop`
it lets the attempts in progress end, 5 seconds at most, and drops the queued traces.
Start the stack with `--profile traces`, or turn tracing off in Claude Code. The comment
above `otlp_http/tempo` in
[`stack/otel-collector/config.yaml`](../stack/otel-collector/config.yaml) has the
details.

### No data in the dashboard

1. `docker compose ps`: every service is running and healthy.
2. Claude Code's environment, in the `env` block of `~/.claude/settings.json` or in the
   shell that starts it. Project settings (`.claude/settings.json` and
   `.claude/settings.local.json`) cannot set the exporter variables, but an exporter set
   to `none` there turns that signal off
   ([ADR 0002](adr/0002-native-telemetry-plus-hooks.md)):

   | Variable                                            | Value                                                                             |
   | --------------------------------------------------- | --------------------------------------------------------------------------------- |
   | `CLAUDE_CODE_ENABLE_TELEMETRY`                      | `1`                                                                               |
   | `OTEL_METRICS_EXPORTER`                             | `otlp`                                                                            |
   | `OTEL_LOGS_EXPORTER`                                | `otlp`                                                                            |
   | `OTEL_EXPORTER_OTLP_PROTOCOL`                       | `http/protobuf` (or `grpc`)                                                       |
   | `OTEL_EXPORTER_OTLP_ENDPOINT`                       | `http://127.0.0.1:4318` (or `http://127.0.0.1:4317` with `grpc`)                  |
   | `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `cumulative`, recommended (see [the known limit](#known-limit-delta-temporality)) |
   | `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`               | `1`, only for traces (with the `traces` profile)                                  |
   | `OTEL_TRACES_EXPORTER`                              | `otlp`, only for traces (with the `traces` profile)                               |

   Claude Code has no default protocol, so set one
   ([monitoring docs](https://code.claude.com/docs/en/monitoring-usage)). Use
   `127.0.0.1`, not `localhost`: the ports listen on IPv4 only, and `localhost` can
   resolve to `::1` ([ADR 0018](adr/0018-ports-networks-and-exposure.md)). Restart
   Claude Code after a change, so that new sessions load it.
3. Timing and filters: Claude Code exports metrics every 60 seconds and events every 5
   seconds by default, and the dashboard refreshes every minute. Check its time range
   and its Host filter; after a change of `CCOBS_HOST_LABEL`, both values are listed.
4. The collector's logs: `docker compose logs otel-collector` shows refused or dropped
   exports.
5. With the debug override, query the stores directly:

   ~~~bash
   curl -s 'http://127.0.0.1:4390/api/v1/query?query=claude_code_session_count_total'
   curl -s -G 'http://127.0.0.1:4310/loki/api/v1/query_range' \
     --data-urlencode 'query={service_name="claude-code"}'
   ~~~

### Messages that are not faults

- Grafana logs at every start that an anonymous role other than Viewer is deprecated; it
  still applies the Editor role
  ([ADR 0017](adr/0017-grafana-access-and-dns-rebinding.md)).
- Grafana logs some of the requests its access control refuses (on organizations and
  dashboards) at error level.
- Grafana shows a *Sign in* link, but there are no accounts and the login form is off:
  there is nothing to sign in to.

## Known limit: delta temporality

Claude Code exports metrics with delta temporality by default. The collector turns them
into the cumulative series that Prometheus stores
([ADR 0013](adr/0013-metrics-through-the-prometheus-otlp-receiver.md)), and every metric
total in the dashboard is `increase(x[R] anchored)`, which takes a range's starting
value from the last sample before it, but only within Prometheus's lookback delta, 5
minutes. With delta, Claude Code exports only the series that changed, so an idle series
has no samples. After a pause of more than about 6 minutes, the first bar after the
pause, and the start of a range that begins during such a pause, can undercount.

With delta, a collector restart also starts every series again from zero, which
`increase(... anchored)` counts as a counter reset; ADR 0013 records the case in which a
total still comes out low.

- Set `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` in Claude Code's
  environment, as `ccobs setup` will from v0.5.0. The running total then lives in Claude
  Code, whose SDK is expected to export every series at each interval, keeping a sample
  within the lookback (ADR 0013 notes that this is still to be confirmed with a real
  session).
- Keep `OTEL_METRICS_INCLUDE_SESSION_ID` at its default (`true`): without session IDs,
  the cumulative series of concurrent sessions collide.

## Checking that nothing leaves the machine

[`tests/integration/test_stack_no_outbound.py`](../tests/integration/test_stack_no_outbound.py)
checks the settings of the running stack: Loki's and Tempo's usage reports are off;
Grafana applied every outbound setting and ran none of the jobs that call grafana.com;
Prometheus has no remote write or read, no Alertmanager and no service discovery; the
collector exports only to the three stores; and `backend` is internal. The app calls
nothing but Loki, over `backend`, although it is on `edge` too to publish its port.

That no connection leaves, whatever the settings say, is a manual check with
[`tools/stack/compose.egress-sink.yaml`](../tools/stack/compose.egress-sink.yaml), a
development-only override. It adds a sink container that takes, on both networks, the
names of every host the components call by default (stats.grafana.org, grafana.com,
secure.gravatar.com, snapshots.raintank.io, secret-scanning.grafana.net), logs every
connection to it, and makes `edge` internal too. It runs as a separate project,
`ccobs-egress`, on ports of its own, so it touches neither your stack nor the
integration tests'. About 25 minutes, with Docker running:

~~~bash
egress() {
  CCOBS_OTLP_GRPC_PORT=44317 CCOBS_OTLP_HTTP_PORT=44318 CCOBS_GRAFANA_PORT=44330 \
  CCOBS_APP_PORT=44300 \
    docker compose -p ccobs-egress --env-file .env.example -f compose.yaml \
    -f tools/stack/compose.egress-sink.yaml --profile traces "$@"
}
egress up -d --build --wait    # every service starts once the sink listens
# wait 15 minutes
egress logs sink               # only the sink's three start-up lines
egress logs | grep -Ei 'lookup |dial tcp|no such host|network is unreachable'   # nothing
egress down -v
~~~

A connection adds "sink: connection to ..." lines to the sink's log, with the bytes it
sent; a `dial tcp 127.0.0.1:9` line in the `grep` output is a Grafana call to
grafana.com's API that its closed port caught. The usage reports come hours after a
start, beyond the 15 minutes; for them the proof is the settings test above and, for
Loki and Tempo, the internal network. The file also shows how to confirm that the host
names resolve to the sink, and two negative controls that each turn one outbound setting
back on.

One kind of query can still leave while a store is not running: when the collector or
Grafana, which are also on `edge`, looks up the name of a stopped service (such as
`tempo` when traces arrive without the `traces` profile), Docker's DNS forwards the name
it does not know to the host's resolvers. The query carries only that name (with your
network's search domain, if any).

## Running the integration tests

~~~bash
uv run pytest -m integration --no-cov
~~~

With Docker running, the tests start their own compose project, `ccobs-it`, with
`compose.yaml`, `compose.debug.yaml` and the `traces` profile, on ports of their own
([ADR 0018](adr/0018-ports-networks-and-exposure.md)): OTLP gRPC 24317, OTLP HTTP 24318,
Grafana 24330, Prometheus 24390, Loki 24310, Tempo 24320, the app 24300 and the app's
metrics 24301.
They read `.env.example`, never your `.env`, and drop inherited `CCOBS_*` and
`COMPOSE_*` variables ([`tools/stack/isolated.py`](../tools/stack/isolated.py)), so they
can run next to your stack.

- A port that is taken, or in a Windows excluded range, moves with `CCOBS_IT_<NAME>`,
  such as `CCOBS_IT_OTLP_HTTP_PORT=34318` for `CCOBS_OTLP_HTTP_PORT`.
- The project is removed with its volumes at the end, unless `CCOBS_IT_KEEP=1`, which
  leaves it running for the next session to reuse. To remove it by hand:

  ~~~bash
  docker compose -p ccobs-it --env-file .env.example -f compose.yaml -f compose.debug.yaml \
    --profile traces down --volumes --remove-orphans
  ~~~

- `uv run pytest` (the unit tests) leaves them out. CI runs them in the `stack` job,
  with `CCOBS_IT_KEEP=1`, then prints the project's memory per service with
  `python -m tools.stack.memory --project ccobs-it` before removing it.
