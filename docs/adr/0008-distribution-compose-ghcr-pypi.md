---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Distribution with Docker Compose, GHCR and PyPI

## Context and problem statement

Users need two things: the stack (collector, Prometheus, Loki, Tempo, Grafana and the
app) running in the background, and a command that connects Claude Code to it by editing
`~/.claude/settings.json`. The stack runs in containers; the command runs on the host,
where the settings live. How should each part be shipped?

## Decision drivers

- Two steps to install: start the stack, run setup.
- The CLI installs quickly and pulls in no server dependencies.
- Works on Linux, macOS and Windows, on amd64 and arm64.
- Releases are reproducible and need no long-lived secrets.
- The PyPI name is secured early.

## Considered options

- **Compose from a clone, the app image on GHCR, the CLI on PyPI.** Standard tools for
  each part; the user needs Docker and uv.
- **pip or pipx only.** One install command, but the CLI would have to run and supervise
  the stack, or users would install each backend by hand.
- **A single all-in-one container.** One `docker run`, but it cannot edit the host's
  Claude Code settings, and it loses per-service limits and the optional Tempo
  ([ADR 0003](0003-observability-backends.md)).
- **Homebrew or npm.** Familiar to many developers, but Homebrew does not cover Windows,
  npm adds a Node runtime to a Python project, and both are one more package to maintain.
- **Separate CLI and server distributions.** A clean dependency split, but two names to
  reserve and release together, and shared code duplicated or moved to a third package.

## Decision outcome

Chosen option: "Compose from a clone, the app image on GHCR, the CLI on PyPI", because
each part ships with the standard tool for its job, and the CLI is one `uvx` away.

- **Stack.** `docker compose up -d` from a clone of the repository, with upstream images
  pinned to exact tags. From v1.0.0 the app image is published on GHCR as
  `ghcr.io/luanderson-dev/cc-observatory`, multi-arch (amd64 and arm64).
- **CLI.** One distribution, `cc-observatory`, on PyPI, with two console scripts,
  `ccobs` and `cc-observatory`, so `uvx cc-observatory setup` runs without an install.
  Commands: `setup`, `uninstall`, `doctor` and `status`.
- **Server dependencies** go in a `server` extra, which the app image installs. The CLI
  never imports server-only modules at import time, so it stays light.
- **Releases.** `release.yml` publishes to PyPI with trusted publishing (OIDC, no stored
  token) when a `vX.Y.Z` tag is pushed. v0.1.0 is published early, at the end of
  milestone 0, to reserve the name.

### Consequences

- Good, because the CLI is a small install that runs straight from `uvx`.
- Good, because there is no PyPI token to leak.
- Bad, because users need Docker, uv and a clone of the repository.
- Bad, because nothing in packaging stops the CLI from importing a server module by
  mistake; only a test catches it.
- Neutral, because the CLI and the app share one version number, since they come from one
  distribution.

### Confirmation

- `[project.scripts]` in `pyproject.toml` declares both `ccobs` and `cc-observatory`.
- A test (from milestone 2, when server dependencies arrive) imports the CLI in a fresh
  interpreter and fails if a server-only module was loaded.
- `release.yml` checks that the tag matches the project version and that the tagged
  commit is on `main` before it publishes.
- The multi-arch GHCR build joins the release workflow in milestone 7.

## More information

- Until v1.0.0 there is no published app image; how compose builds it from the clone
  until then is settled when the app joins the stack in milestone 2.
- What `setup` and `uninstall` change (backup, merge, markers, `--dry-run`) is decided in
  milestone 4.
