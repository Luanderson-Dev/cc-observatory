---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Python, FastAPI, Jinja2, HTMX and SSE for the web app

## Context and problem statement

The app receives hooks, keeps session state, serves a web UI that updates live and sends
alerts. The `ccobs` CLI, which edits Claude Code's settings, is a Python package on PyPI
([ADR 0008](0008-distribution-compose-ghcr-pypi.md)). Which language and web stack should
the app use?

## Decision drivers

- One language for the app and the CLI, so they share the domain code.
- Live updates in the browser as sessions change state and blocks open and close.
- Works offline: no CDN, and nothing leaves the machine
  ([ADR 0001](0001-local-single-machine-scope.md)).
- A small surface for one maintainer: no frontend build toolchain, few dependencies.
- Testable over plain HTTP with pytest.

## Considered options

- **Python with FastAPI, Jinja2 and HTMX, live updates over SSE.** Server-rendered HTML
  with partial updates, one language, no build step; less suited to very rich
  client-side interactions.
- **An SPA (React with Vite).** Rich interactions and a large ecosystem, but a Node
  toolchain, a build step, a second language and a larger dependency tree.
- **Grafana only.** Dashboards for everything and no app UI. Grafana is good at charts,
  not at a live session list, an agent tree or block workflows, and it cannot receive
  hooks.
- **Streamlit or NiceGUI.** Quick to build in Python, but each brings its own frontend
  bundle and execution model (script reruns, a websocket per client), with less control
  over the HTML and harder HTTP-level tests.
- **Go.** A single static binary and low memory use, but the CLI stays in Python to run
  with `uvx`, so the domain code would be written twice.

## Decision outcome

Chosen option: "Python with FastAPI, Jinja2 and HTMX, live updates over SSE", because it
keeps one language and no build step and still gives live updates.

- Python 3.12, managed with uv. FastAPI serves the hook endpoint, the pages and
  `/metrics`; Jinja2 templates render the HTML and HTMX swaps parts of it.
- Live updates use Server-Sent Events: one-way, plain HTTP, and the browser reconnects on
  its own.
- JS and CSS libraries are vendored into the repository and served by the app: no CDN,
  works offline, no frontend build step.
- No JavaScript dialogs (`alert`, `confirm`, `prompt`); confirmations and forms use HTML
  modals.

### Consequences

- Good, because the app and the CLI share and test the same domain code.
- Good, because the UI works without network access and has no npm supply chain.
- Bad, because vendored files are updated by hand; Dependabot does not see them.
- Bad, because each open tab holds an SSE connection, and browsers allow only about six
  HTTP/1.1 connections per host, so many open tabs can stall requests.
- Neutral, because a richer client can still be added later without changing hook
  ingestion or storage.

### Confirmation

- `requires-python = ">=3.12"` in `pyproject.toml`; CI tests on Python 3.12, 3.13 and
  3.14.
- Tests (milestone 3) fail if a template or static file loads a script or stylesheet
  from an external URL, or calls `alert`, `confirm` or `prompt`.
- Every UI stage is checked in a real browser before it is called done (CLAUDE.md).

## More information

- The visual direction, the vendored libraries and their versions, and the SSE event
  format are decided in milestone 3.
- Server-only dependencies such as FastAPI live in the `server` extra, so the CLI stays
  light (ADR 0008).
