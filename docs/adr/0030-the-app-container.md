---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# The app container: built locally from the clone, non-root, published on loopback

## Context and problem statement

The app (`python -m ccobs.server`, [ADR 0027](0027-the-app-http-surface-and-hook-queue.md))
now ingests hooks, keeps its SQLite database
([ADR 0026](0026-app-database-schema-migrations-and-retention.md)), exports metrics on a
separate listener ([ADR 0028](0028-app-metrics-and-their-listener.md)) and reads Loki
back ([ADR 0029](0029-closing-esc-denials-from-loki.md)), but it does not run in the
stack yet. [ADR 0008](0008-distribution-compose-ghcr-pypi.md) publishes the app image on
GHCR from v1.0.0 (milestone 7), so until then users who run `docker compose up -d` from a
clone need another way to get it. The service must follow the stack's rules: ports on
loopback in the 43xx block and two networks
([ADR 0018](0018-ports-networks-and-exposure.md)), `restart: unless-stopped`
([ADR 0019](0019-restart-unless-stopped.md)), pinned images, a memory limit, a
healthcheck, least privilege, named volumes and no host preparation
([ADR 0020](0020-compose-conventions.md)), and the host label
([ADR 0022](0022-the-host-label.md)). How is the app built, configured and run in
compose?

## Decision drivers

- One command from a clone, on Linux, macOS and Windows, with no registry account, no
  `.env` and no `chown` (ADR 0020).
- What runs is the code of the clone, with dependencies exactly as in `uv.lock`.
- The classic builder must work: compose falls back to it without Buildx (ADR 0020).
- Least privilege, and a small image that builds fast after a source change.
- Hooks are accepted from the first second, and a stop never loses queued hooks to a
  kill.
- No exception to the exposure rules of ADRs 0018, 0027 and 0028.

## Considered options

- **Build from the clone with compose (`build:` and `pull_policy: build`).** The same
  pattern as the derived collector image (ADR 0020); a build of about 15 s on the first
  `up` and after each change, and a Dockerfile to keep pinned.
- **Publish the image to GHCR now.** No build on the user's machine, but it brings
  milestone 7 forward: multi-arch builds, signing and a release workflow for images,
  with an image tag that must match the clone's code, which a pull cannot guarantee for
  a clone of `develop` or a feature branch.
- **Run the app on the host with `uvx` instead of in compose.** No image at all, but a
  process to keep running on every OS (a service manager each), outside the restart
  policy, and Prometheus and the reconciler would have to reach it, and Loki, across the
  host boundary.

## Decision outcome

Chosen option: "Build from the clone with compose", because it starts with the same one
command, runs exactly the clone's code with the locked dependencies, needs no registry
and reuses a pattern the stack already has. Publishing to GHCR stays with milestone 7
(ADR 0008), and a host process would leave the stack's restart, network and exposure
rules. This ADR extends ADRs 0008, 0018, 0019, 0020 and 0022, which stay in force.

**How users get the app.** `compose.yaml` builds the service `app` with
`build: {context: ., dockerfile: stack/app/Dockerfile}`, `image: cc-observatory-app:local`
and `pull_policy: build`, so `docker compose up -d` builds it on the first start and
whenever the source or the Dockerfile changes, and never pulls it. The image name has
no version: the image is the clone's code, whatever its version or branch, and a
version tag would claim a release the code may not be; it is never pushed. The build
context is the repository root, which the app needs for `pyproject.toml`, `uv.lock` and
`src/`. The root `.dockerignore` is an allowlist: everything is left out and only
`pyproject.toml`, `uv.lock`, `README.md` and `LICENSE` (which the build backend reads),
`.python-version` and `src/` come back, without bytecode or local databases. So `.env`,
`.git`, `.venv`, `tests`, `docs`, `tools` (raw captures in `tools/capture/out` included),
`.github`, the caches, `dist` and `build` never reach the build, and neither does a
file added later. The collector keeps its own `stack/otel-collector` context; the rule
for built services is now a Dockerfile under `stack/`, with either its directory or the
repository root as context.

**The image** (`stack/app/Dockerfile`), in two build stages (plus a `uv` stage that is
only copied from):

- **Base:** `python:3.12.15-slim-trixie`, the line of `.python-version` and the lowest
  Python of CI, pinned by its multi-arch index digest like every image (ADR 0020), for
  both the builder and the runtime, so the virtual environment's interpreter links
  resolve in the runtime. uv comes from `ghcr.io/astral-sh/uv:0.12.24`, pinned the same
  way, as a stage that the builder copies `/uv` from; 0.12 is the minor of
  `[tool.uv] required-version`, which uv enforces.
- **Builder:** `uv sync --frozen --no-dev --extra server --no-install-project` with only
  `pyproject.toml`, `uv.lock` and `.python-version` copied, then the source, then
  `uv sync --frozen --no-dev --extra server --no-editable`. The dependency layer is
  rebuilt only when the lockfile or the project metadata change; a source change
  rebuilds only the last layer (about 1 s). The project is installed as a package in
  `/opt/ccobs/venv`, with the image's own Python (`UV_PYTHON`, no downloads), bytecode
  compiled at build time and no uv cache in the layer.
- **No `RUN --mount`** (cache or bind mounts) and no `# syntax=` line: the classic builder,
  which compose uses without Buildx (ADR 0020), rejects them. The layer order gives the
  caching that matters.
- **Runtime:** the venv only, owned by root, so the app cannot change its own code; uv
  stays in the builder. The image is about 250 MB as Docker Desktop 29.8 reports it
  (the venv is 54 MB; the rest is the Python base).
- **Non-root:** a system user and group `ccobs`, UID and GID 10001, with no home and no
  shell, and `USER 10001:10001`. `/var/lib/ccobs` is created in the image, owned by that
  user, mode 0750: a new named volume mounted there copies that ownership, so the app
  writes its database with no `chown` on the host (ADR 0020).
- **`CMD ["python", "-m", "ccobs.server"]`** in exec form, with the venv first on `PATH`:
  Python is PID 1 and gets Docker's SIGTERM, which uvicorn handles. No `init: true`: the
  app starts no child processes to reap, and uvicorn installs its own SIGTERM handler, so
  the kernel's rule that PID 1 ignores signals without a handler does not apply.
- `ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1`, and `EXPOSE 4300 9464` as
  documentation. No secret, `ARG` or `.env` in the image.

**Listening on all interfaces in the container.** `CCOBS_LISTEN_HOST` defaults to
`127.0.0.1` in the code, the safe value for an app run directly on a machine; the image
sets it to `0.0.0.0`, without which neither Docker's port publishing nor Prometheus, on
the compose network, could reach the app. That is not exposure: compose publishes the
app on `127.0.0.1` only and the metrics port not at all.

**Ports.** `127.0.0.1:${CCOBS_APP_PORT:-4300}:4300`, the app's slot in ADR 0018's block;
the app's port behind its `Host` allowlist (ADR 0027). The metrics listener, 9464, is
never published by `compose.yaml` (ADR 0028); `compose.debug.yaml` publishes it on
`127.0.0.1:${CCOBS_APP_METRICS_PORT:-4301}`, for debugging, with the read exposure that
ADR 0028 accepts for it. The ports inside the container, the listen address and the data
directory (`CCOBS_PORT`, `CCOBS_METRICS_PORT`, `CCOBS_LISTEN_HOST`, `CCOBS_DATA_DIR`) are
fixed by the image: compose never passes them, and `.env.example` does not offer them, so
the healthcheck, the port mappings, the scrape target and the volume always match.

**Networks.** `backend` and `edge`, like the collector and Grafana: `edge` publishes the
port (Docker publishes no port of a container that is only on internal networks,
ADR 0018), and `backend` reaches Loki and lets Prometheus scrape `app:9464`. Being on
`edge` gives the app egress, which it does not use: it calls only Loki, and the egress
sink check (`tools/stack/compose.egress-sink.yaml`) now covers it.

**Volume.** `app-data:/var/lib/ccobs`, a named volume declared in `compose.yaml`, holding
`ccobs.db` and its WAL files (ADR 0026). `docker compose down -v` deletes it with the
other volumes.

**Memory.** `mem_limit: 256m`, and no `GOMEMLIMIT`, which tunes only Go's runtime; the
rule that requires it now applies to the Go services only. Measured on Docker Desktop
29.8 (Windows, WSL 2) with `tools/stack/memory.py`: 63.8 MiB idle after a fresh start
(cgroup `memory.peak` 79.1 MiB); 66.5 MiB after replaying the 131 hook payloads of the
fixtures, and 70.1 MiB after 2,620 more (`memory.peak` 89.3 MiB); 63.3 MiB after a restart
with that data. The peak includes the healthcheck's short-lived Python process. The
limit leaves room for the hook queue, which may hold up to 64 MiB of bodies
(`CCOBS_HOOK_QUEUE_MAX_BYTES`, ADR 0027), on top of the steady state. The stack's limits
now add up to 2304 MiB, or 2816 MiB with Tempo.

**Healthcheck.** The image has Python and no HTTP client, so the check is
`python -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:4300/healthz', timeout=2)"`,
which fails on any error or non-2xx answer. `/healthz` is behind the `Host` guard, and
urllib sends `Host: 127.0.0.1:4300`, which it allows (ADR 0027). The empty
`ProxyHandler` ignores proxy variables that Docker may inject into containers from the
client's configuration. Interval, timeout, retries, start period and start interval are
those of the collector: 30 s, 5 s, 3, 30 s and 2 s.

**No `depends_on`.** The hook route must accept events from the first second, whether
Loki is up or not, and Loki serves only the reconciler, which treats a failed query as
"try again at the next poll" (ADR 0029). Waiting for Loki's health (up to its 60 s start
period after an unclean stop) would turn every hook in that window into a hook error in
the user's transcript. The collector starts the same way.

**Shutdown.** The shared `stop_grace_period` of 30 s applies. `uvicorn.run` gets
`timeout_graceful_shutdown=5`: after SIGTERM uvicorn waits at most 5 s for open requests
(the SSE streams of milestone 3 would otherwise hold it forever), then the lifespan stops
the metrics listener, the reconciler (5 s at most), the sweeper (5 s), the writer (5 s to
drain the queue, ADR 0027) and disposes of the engine. The worst case is about 20 s, well
inside the 30 s; measured, `docker compose stop app` took 1.2 s and the container exited
with code 0.

**Settings passed by compose**, each `${VAR:-default}` with the default of
`ccobs.server.config`, and listed in `.env.example` with the same value:

| Variable                     | Default            | Meaning                                                     |
| ---------------------------- | ------------------ | ----------------------------------------------------------- |
| `CCOBS_HOST_LABEL`           | `local`            | The host label on series and rows (ADR 0022)                |
| `CCOBS_APP_RETENTION`        | `90d`              | How long the app keeps sessions (ADR 0026)                  |
| `CCOBS_SESSION_STALE_AFTER`  | `12h`              | Silence before a session is stale (ADR 0024)                |
| `CCOBS_TELEMETRY_RECONCILE`  | `true`             | Read `tool_decision` rejects from Loki (ADR 0029)           |
| `CCOBS_LOKI_URL`             | `http://loki:3100` | Loki's HTTP API inside the stack (ADR 0029)                 |
| `CCOBS_LOG_LEVEL`            | `info`             | The app's log level                                         |
| `CCOBS_HOOK_QUEUE_MAX_ITEMS` | `10000`            | Advanced: hooks the queue holds (ADR 0027)                  |
| `CCOBS_HOOK_QUEUE_MAX_BYTES` | `67108864`         | Advanced: body bytes the queue holds, 64 MiB (ADR 0027)     |
| `CCOBS_HOOK_BODY_MAX_BYTES`  | `8388608`          | Advanced: largest hook body, 8 MiB (ADR 0027)               |

Plus `CCOBS_APP_PORT` (4300) and `CCOBS_APP_METRICS_PORT` (4301, debug override only) for
the host ports. A test keeps this list equal to the settings the app reads, minus the
four the image fixes.

**The host label reaches the app too.** Compose passes `CCOBS_HOST_LABEL` with the same
default to the collector and to the app, which labels its `/metrics` series and its rows
with it (ADRs 0026 and 0028), as ADR 0022 announced for milestone 2. ADR 0022's
confirmation said that compose passes the variable to the collector only; that check now
covers both services, and this ADR is where that is recorded.

**The scrape job.** `stack/prometheus/prometheus.yaml` gets the job of ADR 0028:
`ccobs-app` (`ccobs.domain.app_metrics.SCRAPE_JOB`), target `app:9464`, at the global
15 s interval. No `honor_labels`: Prometheus renames a scraped label to
`exported_<name>` only when a target label has the same name, and the targets carry only
`job` and `instance`, so the app's `host` label is stored as the app sent it. With
`honor_labels: true`, a scraped `job` or `instance` would also win over the target's,
which nothing needs. Measured: the target is up, `ccobs_sessions{host="local"}` and the
other series are stored with `job="ccobs-app"` and no `exported_host`.

**Integration ports.** The `ccobs-it` project (`tools/stack/isolated.py`) gets
`CCOBS_APP_PORT` 24300 and `CCOBS_APP_METRICS_PORT` 24301, extending ADR 0018's list of
24xxx ports; `CCOBS_IT_APP_PORT` and `CCOBS_IT_APP_METRICS_PORT` move them. The egress sink
project moves the app's port to 44300.

**Dependabot.** A `docker` entry for `/stack/app`, with the 7-day cooldown, `develop` as
target and the `build` prefix of the other image entries, updates the Python and uv
images (tag and digest together, ADR 0020). It ignores minor and major updates of both:
Python stays on the 3.12 line of `.python-version`, and uv within the
`required-version` range, each moved by hand with those settings. The `docker-compose`
entry ignores `cc-observatory-app`, which no registry has, as it ignores the collector's
local image.

**Restart and privileges.** The service merges the shared `x-service` settings:
`restart: unless-stopped` (ADR 0019), the 30 s grace period, `no-new-privileges`,
`cap_drop: [ALL]` and log rotation. The root filesystem stays writable, as for the other
services; a read-only one is milestone 7's release hardening (ADR 0020).

### Consequences

- Good, because `docker compose up -d` from a clone runs the app too, with no registry
  account and exactly the clone's code and locked dependencies.
- Good, because the app runs unprivileged, with no capabilities and its code read-only
  to it, and only its loopback port reaches the host.
- Good, because a source change rebuilds one layer, and the classic builder works.
- Good, because a stop drains the hook queue and exits cleanly, far inside the grace
  period.
- Bad, because the first `up`, and the first after each change, builds the image (about
  15 s with a warm Docker cache; longer on a first pull of the Python image), and the
  image uses about 250 MB of disk.
- Bad, because a user who pulls new code must let compose rebuild (`up -d` does it, as
  `pull_policy: build` always builds); `docker compose pull` does not update the app.
- Bad, because the Python and uv minors need a manual update with their settings.
- Neutral, because the app is on `edge` and so has egress it does not use, like Grafana
  and the collector.
- Neutral, because the build needs linux/amd64 or linux/arm64, the platforms the uv
  image offers; 32-bit ARM hosts cannot build the app.
- Neutral, because the GHCR image of milestone 7 replaces the local build; this ADR is
  revisited then.

### Confirmation

- `tests/test_compose.py` checks that the app is built from `stack/app/Dockerfile` with the
  root as context, a root `.dockerignore`, `pull_policy: build` and the image
  `cc-observatory-app:local`; that it merges `*service`; that it publishes
  `127.0.0.1:${CCOBS_APP_PORT:-4300}:4300` and joins `backend` and `edge`; its named
  volume at the settings' data directory, `mem_limit: 256m`, no `GOMEMLIMIT` (required
  only for the Go services), the healthcheck command and timings, and no `depends_on`;
  that its environment holds exactly the settings the app reads minus the four fixed by
  the image, whose compose defaults give the same `Settings` as no variable at all; that
  only the collector and the app read `CCOBS_HOST_LABEL`; and that the debug override
  publishes `127.0.0.1:${CCOBS_APP_METRICS_PORT:-4301}:9464` and nothing else for the app.
- `tests/test_app_image.py` checks that every `FROM` is pinned by digest, the Python base
  follows `.python-version` and uv the `required-version` range; no `--mount`, no `ADD`
  and no remote source; `uv sync --frozen --no-dev --extra server`, dependencies first and
  the project `--no-editable`; a numeric non-root `USER` that owns `/var/lib/ccobs`;
  `CCOBS_LISTEN_HOST=0.0.0.0` as the only app setting in the image; `EXPOSE 4300 9464`;
  `CMD` in exec form; no secret; and that `.dockerignore` leaves out `.env`, `.git`,
  `tests`, `tools/capture/out` and the rest, and keeps what the build reads.
- `tests/test_prometheus_config.py` checks the `ccobs-app` job, its target, the global
  interval, and no `honor_labels` or target labels.
- `tests/test_dependabot.py` checks a `docker` entry for every Dockerfile under `stack/`,
  the minor and major ignores of the app's entry, and that the compose entry ignores both
  local images.
- `tests/server/test_server_main.py` checks `timeout_graceful_shutdown=5` and that it plus
  the lifespan's stop timeouts stays within two thirds of the grace period.
- `tests/tools/test_isolated.py` checks the 24300 and 24301 ports, and
  `tests/tools/test_egress_sink.py` that the egress project moves the app's port and
  starts it after the sink.
- The CI `stack` job builds the image (`docker compose build`) and the integration tests
  start it with `up --build --wait`, which fails unless its healthcheck passes; stage 14
  of milestone 2 adds integration tests of the app in the stack.
- The `reviewer` subagent checks that no app port other than 4300 is published outside
  `compose.debug.yaml`, and that the image stays non-root.

## More information

- Docker: [Compose `build`](https://docs.docker.com/reference/compose-file/build/),
  [`pull_policy`](https://docs.docker.com/reference/compose-file/services/#pull_policy),
  [`.dockerignore`](https://docs.docker.com/build/concepts/context/#dockerignore-files),
  [`USER`](https://docs.docker.com/reference/dockerfile/#user) and
  [`CMD`](https://docs.docker.com/reference/dockerfile/#cmd) (exec form).
- uv: [using uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/)
  (`--no-install-project`, `--no-editable`, `UV_COMPILE_BYTECODE`, `UV_PYTHON_DOWNLOADS`).
- uvicorn: [settings](https://www.uvicorn.org/settings/) (`timeout_graceful_shutdown`).
- Prometheus: [`scrape_config`](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config)
  (`honor_labels` and the `exported_` prefix).
- Revisit with milestone 7 (GHCR images, a read-only root filesystem), if the app starts
  child processes (then `init: true`), or if the measured memory nears the limit.
