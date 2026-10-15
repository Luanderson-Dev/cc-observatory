---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# The stack restarts unless stopped

## Context and problem statement

The stack is meant to run in the background whenever Claude Code does. Claude Code
delivers hook events to the app through `http` hooks, and with the stack down every such
event writes a hook-error notice in the transcript
([ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)); the telemetry it
exports meanwhile is lost. Compose's default restart policy is `no`: after a reboot or a
restart of Docker, nothing runs until the user types `docker compose up -d` again.
ADR 0011 proposed `restart: unless-stopped` as a mitigation and left the decision to
the plan of milestone 1. Which restart policy should the services have?

## Decision drivers

- The stack is up whenever Claude Code runs, without the user remembering to start it.
- A deliberate stop is respected: the user can turn the stack off and keep it off.
- A crashed service comes back on its own.
- Nothing to install on the host besides Docker (ADR 0011 rejected a host process).

## Considered options

- **`unless-stopped`.** Docker restarts the container after it exits and when Docker
  starts, except for a container the user stopped.
- **`no` (the Compose default).** Nothing restarts: after each reboot the stack stays
  down until the user starts it, and every `http` hook event writes a notice meanwhile.
- **`on-failure`.** Restarts after a non-zero exit only, so a crash is covered, but a
  container stopped cleanly by a reboot or a Docker restart need not come back.
- **`always`.** Like `unless-stopped`, except that a container the user stopped by hand
  starts again the next time Docker starts, which overrides a deliberate stop.

## Decision outcome

Chosen option: "`unless-stopped`", because it is the only policy that brings the stack
back after a crash, a reboot or a Docker restart and still respects a stop by the user.

- Every service in `compose.yaml` has `restart: unless-stopped`, the app too when it
  joins the stack in milestone 2.
- After a reboot, the stack comes back when Docker starts. Docker Desktop starts at
  sign-in only with its "Start Docker Desktop when you sign in to your computer"
  setting, which is off by default
  ([settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)); on
  Linux, the Docker service has to be enabled at boot.
- `docker compose stop` keeps the stack stopped, across Docker restarts too, and
  `docker compose down` removes its containers; `docker compose up -d` brings it back.

### Consequences

- Good, because once Docker starts with the machine, the app is there for the `http`
  hooks and the collector for telemetry with no action from the user, so the notices of
  ADR 0011 appear only while Docker is down or the user has stopped the stack.
- Good, because a stop by the user holds, even across Docker restarts.
- Good, because a crashed service restarts by itself.
- Bad, because the stack uses memory in the background whenever Docker runs, used or
  not; the per-service memory limits of [ADR 0003](0003-observability-backends.md) cap
  it, and `docker compose stop` frees it.
- Bad, because a service that keeps crashing keeps restarting instead of staying down;
  `docker compose ps` shows it restarting.
- Neutral, because surviving a reboot depends on Docker starting with the machine,
  which is the user's setting.

### Confirmation

- `tests/test_compose.py` (milestone 1) checks `restart: unless-stopped` on every
  service in `compose.yaml`. This is the test that ADR 0011's Confirmation makes
  conditional on milestone 1's approval.
- The `reviewer` subagent checks that every new service keeps the policy.

## More information

- Docker: [restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
  ("Similar to `always`, except that when the container is stopped (manually or
  otherwise), it isn't restarted even after Docker daemon restarts") and the Compose
  [`restart`](https://docs.docker.com/reference/compose-file/services/#restart)
  attribute.
- [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md) describes what Claude
  Code shows while the stack is down; this ADR settles the first of its mitigations.
- Revisit if the stack gains a mode meant to run only on demand.
