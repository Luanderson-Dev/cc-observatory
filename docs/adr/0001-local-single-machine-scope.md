---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Local, single-machine scope with no login

## Context and problem statement

cc-observatory shows what Claude Code is doing: sessions, agents, blocks, tokens and
cost. That data is sensitive. It describes the user's work, holds local paths with the
OS username and, with the opt-in, prompts and file contents
([ADR 0006](0006-privacy-metadata-only-by-default.md)). Where the stack runs and who can
reach it decides how much security machinery the project needs. Which deployment scope
should cc-observatory support?

## Decision drivers

- The audience is one developer watching Claude Code on their own machine.
- Telemetry and hook data stay on the machine unless the user explicitly opts in.
- One maintainer: no accounts, TLS or user management to build and keep secure.
- Installation is `docker compose up -d` plus one CLI command.
- Leave room for a central mode later, without designing for it now.

## Considered options

- **Local only.** Everything on one machine, ports on loopback, no login. Smallest
  attack surface and simplest setup, but anything running on the machine can read the
  data and post hook events.
- **Multi-host central server.** Several machines report to one stack. Useful for teams,
  but it needs authentication, TLS, per-host identity and network exposure from day one.
- **SaaS.** A hosted service with no local stack, but the user's data leaves the machine,
  and accounts, billing and operations are more than one maintainer can run.

## Decision outcome

Chosen option: "Local only", because it serves the target user with the least machinery
and keeps all data on the machine.

- Every port that the compose stack publishes binds to `127.0.0.1`. There is no login.
- Stored data carries a `host` label, so that a future central mode could tell machines
  apart. Central mode itself is not implemented and is not on the roadmap.
- Any web page open in the user's browser can send requests to localhost, so the app
  adds browser-facing defences (from v0.3.0): a `Host` header allowlist (`localhost`,
  `127.0.0.1`, `[::1]`) against DNS rebinding; a hook route that ignores any request
  whose `Content-Type` is not `application/json` (still answering an empty 2xx), so a
  cross-site page cannot post to it without a CORS preflight; and no permissive CORS, so
  that preflight fails.

### Consequences

- Good, because there are no credentials, sessions or certificates to leak or expire.
- Good, because nothing is published on the network interfaces by default.
- Bad, because any local process or user on the machine can read the data and inject
  fake hook events; [SECURITY.md](../../SECURITY.md) documents this trust model.
- Bad, because putting the stack behind a reverse proxy, tunnel or port forward is unsafe
  without adding authentication, and users have to be told not to.
- Neutral, because the `host` label costs a little storage for a mode that may never
  ship.

### Confirmation

- A test over `compose.yaml` (milestone 1) fails if a published port is not bound to
  `127.0.0.1`.
- App tests (milestone 2) cover the `Host` allowlist, the `Content-Type` rule on the hook
  route and the absence of permissive CORS headers.
- The `reviewer` subagent checks every diff for ports not bound to `127.0.0.1`.

## More information

- The value of the `host` label, and whether telemetry series carry it too, are decided
  in milestones 1 and 2. A hostname would be personal data under ADR 0006, so the value
  is likely a label the user chooses.
- The app's port and Grafana's port are chosen with the stack in milestone 1 and the app
  in milestone 2, as is how Grafana itself is protected against DNS rebinding.
- Revisit if a central mode is ever planned: it needs its own ADR covering
  authentication and transport security.
