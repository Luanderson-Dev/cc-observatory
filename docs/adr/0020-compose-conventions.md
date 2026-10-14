---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Compose conventions for a no-extra-steps start on Linux, macOS and Windows

## Context and problem statement

Starting the stack must take one command, `docker compose up -d` from a clone
([ADR 0008](0008-distribution-compose-ghcr-pypi.md)), on Linux, macOS and Windows, with
no host preparation: no directories to create, no `chown`, no `.env` to write. The stack
then runs unattended for weeks ([ADR 0019](0019-restart-unless-stopped.md)), and
[ADR 0003](0003-observability-backends.md) asks for an exact image tag, a memory limit
and a healthcheck on every service. Upstream examples often bind-mount single files, pin
tags without digests, leave container logs unbounded, and the collector's image has no
shell or HTTP client for a healthcheck. Which conventions should `compose.yaml` follow?

## Decision drivers

- One command to start, with no host preparation and no required `.env`, on every OS.
- What runs is exactly what was tested.
- Bounded logs and memory for a stack that runs all the time.
- Least privilege for every container.
- A healthcheck on every service (ADR 0003), including images without a shell.
- Isolated projects, such as the integration tests', next to the user's stack.
- The project's license terms stay as they are ([ADR 0010](0010-mit-license.md)).

## Considered options

- **Portable conventions in one compose file, with a derived collector image.** The
  rules below; one small Dockerfile to maintain.
- **Upstream examples as they are.** Quickest to write, but with single-file bind
  mounts, mutable tags, unbounded logs and no healthcheck for the collector.
- **A setup script that prepares the host.** It could create directories, fix ownership
  and write `.env`, but it is one more step, in a shell that differs on each OS.

For the collector's healthcheck (its upstream image is built `FROM scratch`):

- **A derived image built locally.** The pinned upstream image plus a static busybox
  binary, used only for `wget`; a Dockerfile to keep pinned.
- **A `type: image` volume with busybox.** No Dockerfile, but image mounts need Docker
  Engine 28.0 or later (API 1.48), above the minimum below, and Dependabot's support for
  them is unclear.
- **No healthcheck in the container, liveness checked from the host.** No derived image,
  but it breaks ADR 0003's rule (it would need an ADR superseding it), and Compose's
  `service_healthy` conditions and `up --wait` could not see the collector's state.
- **A sidecar container that probes the collector.** No derived image, but one more
  service with its own image, limit and healthcheck, while the collector's own container
  still reports no health.

## Decision outcome

Chosen option: "Portable conventions in one compose file, with a derived collector
image", because it starts with one command on every OS, pins what runs, bounds what
grows and keeps ADR 0003's healthcheck rule without a host step. For the collector, the
derived image is the only option that keeps a healthcheck inside its own container,
works on the minimum Docker versions below and is handled by Dependabot.

- **Project.** `name: cc-observatory`, so volume and network names stay the same
  whatever the clone's directory is called. No `container_name`: container names are
  unique per Docker host, so a fixed name would stop an isolated project such as
  `ccobs-it` ([ADR 0018](0018-ports-networks-and-exposure.md)) from running beside the
  user's stack.
- **Configuration** is mounted read-only, as directories under `stack/<service>/`. A
  single-file bind mount follows the file the container first saw, so an editor that
  saves by writing a new file and renaming it leaves the container with the old
  version; and when the source is missing, Docker creates an empty directory in its
  place ([bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)), which the
  service then fails to read as a file. Baking the configuration into the derived image
  was considered: it avoids host file permissions (see the umask rule below), but every
  change would need a rebuild, and the content opt-in of milestone 4 edits the mounted
  configuration.
- **Data** lives in named volumes. When a container first mounts an empty named volume,
  Docker copies the image's content at the mount point into it
  ([volumes](https://docs.docker.com/engine/storage/volumes/)), ownership included, so
  services that run as an unprivileged user can write to it with no `chown` or `user:`
  override.
- **Variables.** Every interpolation is `${VAR:-default}`, so `.env` is optional.
  `.env.example` lists every variable with the same default, and `.env` is never
  committed (`.gitignore`).
- **Line endings.** Text files are LF (`.gitattributes`), so configs cloned on Windows
  reach Linux containers unchanged.
- **Logs.** The `json-file` driver with `max-size: 10m` and `max-file: "3"` on every
  service: by default `max-size` is unlimited
  ([json-file](https://docs.docker.com/engine/logging/drivers/json-file/)), and the
  stack never stops.
- **Privileges.** `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]` on
  every service, with no `cap_add` and no `privileged`. A read-only root filesystem is
  left to the release hardening of milestone 7.
- **Memory.** A `mem_limit` on every service (ADR 0003); `GOMEMLIMIT` at about 80% of it
  in the Go services, so the runtime collects garbage before the container reaches its
  limit, as the collector's
  [memory limiter docs](https://github.com/open-telemetry/opentelemetry-collector/blob/main/processor/memorylimiterprocessor/README.md)
  recommend; and the collector's `memory_limiter` processor, first in every pipeline
  and set as a percentage of the limit. The numbers are measured in a later stage of
  this milestone.
- **Shutdown.** An explicit `stop_grace_period` on every service: 30 s, from the shared
  settings, so that each one can stop cleanly after SIGTERM before Docker kills it.
  Without it, the daemon's `default-stop-timeout` applies, which differs between
  installs: 10 s upstream, about 3 s measured on Docker Desktop 29.8. A backend killed
  mid-shutdown replays its write-ahead log at the next start. A service that exits
  sooner does not wait. Tempo has 15 s, and its config shortens the backend worker's
  `finish_on_shutdown_timeout` to 5 s: in Tempo 3.1.0 the worker keeps asking the
  stopped scheduler for jobs until that timeout runs out, and the upstream 30 s would
  outlast the grace period.
- **Images.** Every image, in `compose.yaml` and in Dockerfile `FROM` lines, is
  `name:exact-tag@sha256:<multi-arch index digest>`. The tag says what runs and the
  digest makes it immutable, because a tag can be re-published upstream, as the
  collector's `0.162.0` tag was during this milestone; the index digest stays valid on
  amd64 and arm64. Dependabot (set up in a later stage of this milestone) updates tag
  and digest together, with the 7-day cooldown of
  [ADR 0009](0009-engineering-workflow-and-quality.md). Dependabot has no security
  updates for Docker images
  ([supported ecosystems](https://docs.github.com/en/code-security/dependabot/ecosystems-supported-by-dependabot/supported-ecosystems-and-repositories)),
  so a release that fixes vulnerabilities may be applied by hand before the cooldown
  ends, as Grafana 13.2.4 was in this milestone.
- **Healthchecks**, chosen per image:
    - Prometheus: `wget`, from its busybox-based image;
    - Loki and Tempo: their own `-health` flag, which requests `/ready`;
    - Grafana: `curl` on
      [`/api/health`](https://grafana.com/docs/grafana/latest/developers/http_api/other/);
    - the collector: the derived image below.
- **Derived collector image.** `stack/otel-collector/Dockerfile` starts from the pinned
  contrib image and copies in the static busybox binary of a pinned
  `busybox:1.38.0-musl` image, used only for `wget` against the collector's
  `health_check` extension inside the container. Compose builds it on the user's machine
  (`build:` with `pull_policy: build`) under a local image name; it is never pushed.
- **License.** busybox is GPL-2.0. The repository ships only the Dockerfile, which names
  the upstream images; the image is built on the user's machine and the project does not
  distribute it, and busybox runs unmodified, as a separate program next to the
  collector. cc-observatory still runs upstream programs unmodified and ships only
  their configuration, so ADR 0010 holds.
- **SELinux.** On hosts that enforce SELinux (Fedora, RHEL), the bind mounts may need
  the `:z` suffix. It is documented, not set by default, because relabelling "affects
  the file or directory on the host machine itself" (Docker's bind mounts docs).
- **Restrictive umask on Linux.** A clone made with a umask such as `027` or `077`
  (common on CIS-hardened distributions) leaves `stack/` unreadable for the containers'
  non-root users: the collector runs as uid 10001, exits with "permission denied" and
  keeps restarting. The fix is `chmod -R o+rX stack/`. It is documented with the stack
  later in this milestone, and `ccobs doctor` checks it in milestone 4. Docker Desktop
  on macOS and Windows is not affected.
- **Minimum Docker versions**, which the README repeats:
    - Docker Engine 25.0 or later (API 1.44), for `healthcheck.start_interval`, which
      "requires Docker Engine version 25.0 or later"
      ([Dockerfile reference](https://docs.docker.com/reference/dockerfile/#healthcheck));
      on Linux, [ADR 0018](0018-ports-networks-and-exposure.md) notes what engines
      older than 28.0 do with ports published to localhost;
    - the Docker Compose plugin (`docker compose`, not the old `docker-compose`) 2.20.2
      or later, where `start_interval` was introduced
      ([services](https://docs.docker.com/reference/compose-file/services/#healthcheck));
    - the Buildx plugin 0.17.0 or later, which Compose requires when the plugin is
      present (its `buildxMinVersion`). Compose 5 delegates builds to Bake; without
      Buildx it falls back to the deprecated classic builder, with a warning.

  Docker Desktop ships all three. On Linux, the official packages (`docker-ce`,
  `docker-compose-plugin`, `docker-buildx-plugin`) from Docker's repository qualify;
  distribution packages such as Debian 12's `docker.io` (20.10.24) are too old, and
  Docker's [install guide](https://docs.docker.com/engine/install/debian/) asks to
  remove them first.

### Consequences

- Good, because `docker compose up -d` from a fresh clone is the whole start on every
  OS: no `.env`, no `chown`, no directories to create.
- Good, because what runs is exactly what was tested, and upgrades arrive as reviewable
  pull requests.
- Good, because logs, memory and privileges are bounded for a stack that never stops.
- Good, because every service, the collector included, reports its health to
  `docker compose ps`, to `depends_on` conditions and to `up --wait`.
- Bad, because the collector image is built on the first `up` and after each collector
  update, which takes a little time.
- Bad, because digest pins need tooling to stay current, and security fixes for images
  need a manual check, since Dependabot raises no security updates for them.
- Bad, because SELinux users add `:z` themselves, and Linux users who clone with a
  restrictive umask must run `chmod -R o+rX stack/` before the collector can start.
- Bad, because Docker from distribution packages (such as Debian 12's `docker.io`) is
  too old, so Linux users may first have to switch to Docker's own packages.
- Neutral, because a read-only root filesystem and published images are left to
  milestone 7.

### Confirmation

- `tests/test_compose.py` (milestone 1) checks every service in `compose.yaml`: an image
  pinned to tag and digest, or built locally from a Dockerfile under `stack/` with
  `pull_policy: build`; pinned `FROM` lines in every Dockerfile, and the collector's
  derived image from the pinned contrib and busybox images; a memory limit, with
  `GOMEMLIMIT` at 70 to 90% of it in the Go services; a healthcheck; the log rotation
  options; an explicit `stop_grace_period`, with Tempo's above its
  `finish_on_shutdown_timeout` (`tests/test_tempo_config.py`); `no-new-privileges` and
  `cap_drop: [ALL]`; no `container_name`; read-only
  configuration directories under `stack/` and data on declared named volumes;
  `name: cc-observatory`; and a default for every variable, listed in `.env.example`
  with the same value.
- `docker compose config --quiet` validates `compose.yaml`, alone and with
  `compose.debug.yaml`.
- The integration tests (`tests/integration/`) start the stack as the `ccobs-it` project
  with `docker compose up --wait`, which fails unless every service becomes healthy.
- `ccobs doctor` (milestone 4) checks that `stack/` is readable for the containers and
  that Docker Engine, Compose and Buildx meet the minimums.
- The `reviewer` subagent checks every new service against these rules.

## More information

- Docker Compose: [services](https://docs.docker.com/reference/compose-file/services/)
  (`restart`, `container_name`, `pull_policy`, `logging`, `security_opt`, `cap_drop`,
  `mem_limit`, `healthcheck`, `volumes`) and
  [project names](https://docs.docker.com/compose/how-tos/project-name/).
- Compose builds: the [v5.0.0 release notes](https://github.com/docker/compose/releases/tag/v5.0.0)
  ("build is delegated to Docker Bake"), and `buildxMinVersion` and the image-mount API
  version in Compose's
  [`api_versions.go`](https://github.com/docker/compose/blob/main/pkg/compose/api_versions.go).
- If the project ever publishes this image, for example with the GHCR images of
  milestone 7, it distributes busybox and takes on GPL-2.0's source obligations:
  revisit then.
- The memory numbers and the Dependabot configuration for `docker-compose` and `docker`
  (the collector's Dockerfile) come in later stages of this milestone.
- Revisit the derived image if the upstream collector image gains a way to run a
  healthcheck, or if image mounts become widely supported by Docker, Compose and
  Dependabot.
