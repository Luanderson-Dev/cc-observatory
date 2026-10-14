---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Ports and networks: minimal exposure in a dedicated port block

## Context and problem statement

The stack runs on the user's own machine, with every published port bound to
`127.0.0.1` and no login ([ADR 0001](0001-local-single-machine-scope.md)). Loopback
keeps other machines out, but not the browser: any web page can send requests to
localhost, through DNS rebinding or cross-site POSTs. The storage services have no
authentication of their own: Prometheus keeps its OTLP receiver off by default because
it "can work without any authentication"
([OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)), Loki "does not come
with any included authentication layer"
([docs](https://grafana.com/docs/loki/latest/operations/authentication/)), and Tempo,
run single-tenant, has none either. The backends must not report usage or check for
updates ([ADR 0006](0006-privacy-metadata-only-by-default.md), "No outbound traffic"),
and their upstream default ports (Grafana 3000, Prometheus 9090, Loki 3100, Tempo 3200)
clash with other local tools. Which ports should the stack publish, and how should its
services be networked?

## Decision drivers

- Nothing reachable from other machines (ADR 0001), and as little as possible from the
  browser.
- The storage services, which have no authentication, stay out of reach.
- The backends cannot phone home, by construction rather than by settings alone.
- Ports that avoid common local tools, and that the user can move.
- Debugging and the integration tests can still reach every backend.
- The same behaviour on Linux, macOS and Windows.

## Considered options

- **Every service on loopback, on the upstream default ports.** Familiar, but the ports
  clash with other tools and dev servers, and every unauthenticated backend is reachable
  from any web page the user opens.
- **Every service on loopback, in a dedicated port block.** No clashes, but the backends
  are still published and reachable from the browser, and nothing structural keeps them
  off the internet.
- **Minimal exposure in a dedicated port block.** Only what Claude Code and the user
  need is published; the backends sit on an internal network, and a debug override
  publishes them when needed. One more compose file, which debugging requires.

## Decision outcome

Chosen option: "Minimal exposure in a dedicated port block", because it publishes only
what Claude Code and the user need, keeps the unauthenticated backends out of the
browser's reach and off the internet by construction, and avoids common ports while
keeping every port configurable.

- **Port block.** One block, every port set by a variable with a default:

  | Service              | Default | Variable                | Published           |
  | -------------------- | ------- | ----------------------- | ------------------- |
  | Collector, OTLP gRPC | 4317    | `CCOBS_OTLP_GRPC_PORT`  | always              |
  | Collector, OTLP HTTP | 4318    | `CCOBS_OTLP_HTTP_PORT`  | always              |
  | App                  | 4300    | `CCOBS_APP_PORT`        | from milestone 2    |
  | Grafana              | 4330    | `CCOBS_GRAFANA_PORT`    | always              |
  | Loki                 | 4310    | `CCOBS_LOKI_PORT`       | debug override only |
  | Tempo                | 4320    | `CCOBS_TEMPO_PORT`      | debug override only |
  | Prometheus           | 4390    | `CCOBS_PROMETHEUS_PORT` | debug override only |

  OTLP keeps 4317 and 4318, the
  [default ports of the protocol](https://opentelemetry.io/docs/specs/otlp/), so
  standard exporter settings work.
- **Two networks.** `backend` has `internal: true`; `edge` is a normal bridge network.
  Prometheus, Loki and Tempo join only `backend`. The collector and Grafana (and the
  app, from milestone 2) join both: they reach the backends on `backend` and publish
  their ports through `edge`. On an internal network "no default route is configured
  and firewall rules are set up to drop all traffic to or from other networks"
  ([`docker network create`](https://docs.docker.com/reference/cli/docker/network/create/)),
  so the backends cannot report usage, check for updates or send data anywhere, even
  with a wrong setting. The settings of ADR 0006 that turn reporting off stay, for
  Grafana (on `edge`) and for the debug override.
- **Debug override.** `compose.debug.yaml`, applied with
  `docker compose -f compose.yaml -f compose.debug.yaml up -d`, publishes Prometheus,
  Loki and Tempo on loopback, for debugging and for the integration tests. It also
  attaches them to `edge`, because Docker does not publish the ports of a container
  whose only networks are internal (by design, according to a maintainer in
  [moby/moby#36174](https://github.com/moby/moby/issues/36174); confirmed on Docker
  Engine 29.8). So while the override is in use, those three services have egress. The
  file is not named `compose.override.yaml`, which Compose would load by default.
- **IPv4 loopback.** Ports bind to `127.0.0.1` only (ADR 0001), so clients use
  `127.0.0.1` rather than `localhost`, which may resolve to `::1`. `ccobs setup`
  (milestone 4) writes `http://127.0.0.1:4318` as the OTLP endpoint.
- **Port conflicts.** Windows can reserve TCP port ranges (list them with
  `netsh int ipv4 show excludedportrange protocol=tcp`); any port in the block that
  falls in one, or that another tool uses, can be moved with its variable.
- **Integration tests** run an isolated compose project, `ccobs-it`, with both files, on
  the 24xxx equivalents (24317, 24318, 24330, 24310, 24320, 24390), so they never touch
  the user's stack, its ports or its volumes.

### Consequences

- Good, because by default only OTLP and Grafana listen on the host, and Prometheus,
  Loki and Tempo publish no port at all, on loopback or elsewhere.
- Good, because the backends' silence comes from the network, not from settings that an
  upgrade could rename or reset.
- Good, because the block avoids the usual ports, and every port can be moved.
- Bad, because querying Prometheus, Loki or Tempo directly needs the debug override,
  which also gives them egress while it is in use.
- Bad, because a client that resolves `localhost` to `::1` and does not fall back to
  IPv4 is refused; documentation and `ccobs setup` use `127.0.0.1`.
- Bad, because the collector's OTLP/HTTP receiver on 4318 accepts any `Host` header, so
  a page using DNS rebinding could post fake telemetry. It reads nothing back, since the
  receiver only accepts writes, and a plain cross-site POST of JSON fails the CORS
  preflight (the receiver enables no CORS). As with fake hook events, the data is only
  as trustworthy as the software on the machine ([SECURITY.md](../../SECURITY.md)).
- Neutral, because an internal network is isolated from other networks, not from the
  host: on Linux the host (a browser on it included) can still reach a container's
  address on the bridge, and containers can reach host services listening on the
  bridge's gateway address (Docker's `network create` reference). Nothing is published
  on the host's interfaces, and Docker Desktop keeps the bridge inside its VM.

### Confirmation

- `tests/test_compose.py` (milestone 1) checks, over `compose.yaml` and
  `compose.debug.yaml`, that every published port starts with `127.0.0.1:`; that
  `backend` is internal and `edge` is not; that Prometheus, Loki and Tempo join only
  `backend` and publish nothing in `compose.yaml`; that the override publishes only
  services on `edge`; and that every port variable has a default, listed in
  `.env.example`.
- `docker compose config --quiet` validates `compose.yaml`, alone and with
  `compose.debug.yaml`.
- The integration tests (`tests/integration/`) start the `ccobs-it` project with both
  files on the 24xxx ports.
- The `reviewer` subagent checks every diff for ports not bound to `127.0.0.1` and for
  storage services attached to `edge` outside the override.

## More information

- Docker: the Compose
  [`internal`](https://docs.docker.com/reference/compose-file/networks/) attribute,
  [port publishing](https://docs.docker.com/engine/network/port-publishing/) and
  [merging compose files](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).
- Docker Engine releases older than 28.0.0 let hosts on the same L2 segment reach ports
  published to localhost (Docker's port publishing docs). Whether the README and
  `ccobs doctor` (milestone 4) warn about such engines is decided with them.
- Grafana is published to the browser on purpose. ADR 0001 leaves to milestone 1 how
  Grafana itself is protected against DNS rebinding; that is settled when Grafana joins
  the stack. The app's browser defences (ADR 0001) arrive with it in v0.3.0.
- Revisit if Docker starts publishing ports of containers on internal networks, or if a
  service needs egress (for example, Grafana plugins downloaded at startup).
