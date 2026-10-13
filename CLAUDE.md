# cc-observatory

Local observability for Claude Code running on one machine: a web app with sessions,
agents and subagents, blocks (sessions waiting on a person), logs, tokens and cost,
backed by OpenTelemetry, Prometheus, Loki, Tempo and Grafana. Users install it with
`docker compose up -d` plus the `ccobs` CLI (`uvx cc-observatory setup`).

- Public repository under the MIT license. Code, identifiers, commits, issues, PRs and
  committed docs are in English; `README.pt-BR.md` mirrors `README.md`.
- Python distribution `cc-observatory`, import package `ccobs`, CLI `ccobs` (also
  exposed as `cc-observatory` so that `uvx cc-observatory ...` works).
- The maintainer's personal working preferences live in `CLAUDE.local.md`, which Git
  ignores. Follow it when it exists.

## How we work

1. **Plan, then wait.** Before each milestone, present the plan: goal, numbered
   stages, files touched, new decisions (each becomes an ADR), risks, expected
   blockers and the release it closes. Start only after the maintainer approves.
2. **Execute stage by stage without stopping.** Each code stage is test first (red),
   then implementation (green); every stage ends with local checks passing, a
   `reviewer` pass and one commit.
3. **Summarize each stage** briefly: what changed, tests run, commit hash and message.
4. **Close the milestone:** PR, green CI, merge, release.

### Subagents (`.claude/agents/`)

| Subagent          | Use it to                                                                   |
| ----------------- | --------------------------------------------------------------------------- |
| `architect`       | Plan a milestone or stage, check changes against ADRs, propose new ADRs     |
| `tdd-implementer` | Implement one stage with TDD and run the local checks                       |
| `reviewer`        | Review the diff of every stage before the commit (read-only)                |
| `docs-release`    | README, ADRs, `docs/`, compose and collector configs, CI, release steps     |

The main context coordinates: it gives each subagent the stage, the files and the
definition of done, and it makes the commits. If `reviewer` reports a problem, send
it back to `tdd-implementer` (or `docs-release`) before committing.

### Blockers

Stop when the next action depends on the maintainer: accounts (PyPI), GitHub
permissions, secrets, Docker not running, the Chrome extension not connected, product
decisions, or any destructive action outside the feature branch. Report what is
blocked, why, the exact steps to unblock, how to confirm, and what can proceed
meanwhile. Never work around a blocker by disabling checks (`--no-verify`, skipping
tests, loosening ruff or mypy).

## Git and releases

- Gitflow with git-flow-next. `main` only receives releases; `develop` is the default
  and integration branch. Branches: `feature/*`, `release/*`, `hotfix/*`; tags `vX.Y.Z`.
- **Branch protection is a process rule, not a GitHub setting.** The repository stays
  on free features and has no rulesets, so these rules are enforced by us:
    - never commit or push directly to `main` or `develop`, and never force-push them;
    - changes reach `main` and `develop` only through PRs with green CI;
    - never rewrite published history or delete published tags;
    - force push only on your own feature branch, with `--force-with-lease`.
- Merge through PRs with `gh`, not with `git flow ... finish`:
    - feature → `develop`: **squash**. The PR title becomes the changelog line, so it
      must describe the whole delivery in Conventional Commits (CI checks it);
    - release → `main` and the back-merge `main` → `develop`: **merge commit**, never squash.
- CI validates every PR title with `cz check --allowed-prefixes`: it must be a
  Conventional Commit, and the default bypass for `Merge`, `Revert`, `fixup!` and
  `squash!` is off, so release and back-merge PRs need Conventional titles too.
- Commits:
    - one commit per stage, Conventional Commits validated by the commitizen hook
      (`feat`, `fix`, `refactor`, `perf`, `docs`, `build`, `ci`, `chore`, `test`, `bump`);
    - `git add` only the stage's paths, **never** `git add .` or `git add -A`;
    - check `git status` before each commit; `.env` never goes in;
    - if pre-commit fixes files and rejects the commit, re-add the same paths and
      commit again;
    - no `Co-Authored-By` or other attribution trailers, and no "Generated with"
      footers in PR descriptions.
- Feature: `git flow feature start <name>` → stages → `git push -u origin
  feature/<name>` → `gh pr create --base develop` → `gh pr checks --watch` →
  `gh pr merge --squash --delete-branch --subject "<PR title> (#<number>)"` →
  `git switch develop && git pull`. Always pass `--subject`: without it, a
  single-commit PR is squashed with the commit message instead of the PR title.
- Release:
    1. `uv run cz bump --dry-run` to confirm the version suggested by the commits.
    2. `git flow release start X.Y.Z`, `uv version X.Y.Z`,
       `uv run cz changelog --incremental --unreleased-version vX.Y.Z`, commit
       `bump: version X.Y.Z`.
    3. PR to `main` with `gh pr create --base main --title "bump: version X.Y.Z"`,
       green CI, `gh pr merge --merge`.
    4. On the updated `main`: `git tag -a vX.Y.Z -m "vX.Y.Z"`, push the tag (this runs
       `release.yml`, which publishes to PyPI by trusted publishing),
       `gh release create vX.Y.Z --generate-notes`.
    5. Back-merge:
       `gh pr create --base develop --head main --title "chore: merge main into develop after vX.Y.Z"`,
       then `gh pr merge --merge`; confirm with
       `git merge-base --is-ancestor origin/main origin/develop`; delete the release branch.
- Hotfix: `git flow hotfix start X.Y.Z` from `main`, fix with a test, then release
  steps 2 to 5.
- The changelog is generated by Commitizen; never edit it by hand.

## Architecture

```text
Claude Code ──OTLP (4317/4318)──► OTel Collector ──► Prometheus (metrics)
     │                                         ├──► Loki (events/logs)
     │                                         └──► Tempo (traces, optional profile)
     └──hooks──► app (FastAPI) ──► SQLite (sessions, agents, blocks, alerts)
                    │  └─ /metrics ◄── scraped by Prometheus (live state)
                    └─ queries Prometheus and Loki over HTTP
Grafana ◄── Prometheus, Loki, Tempo (provisioned datasources and dashboards)
```

Decisions are recorded as ADRs in `docs/adr/` (MADR). Every new architecture decision
becomes an ADR in the same PR that implements it. An accepted ADR is never rewritten:
a new ADR supersedes it, and only the old one's status line changes.

## Rules

### Hooks must never interfere with Claude Code

- The hook endpoint answers fast (target < 50 ms): validate, enqueue, respond.
  Persistence happens in the background.
- Invalid payloads, a full queue and internal errors still get an empty 2xx (log and
  drop). The hook route never returns 422 or 500: any non-2xx shows up as a hook error
  in the user's transcript.
- The only response is a neutral one: 2xx with an empty body. Never return JSON
  decision fields (`decision`, `permissionDecision`, `continue`, `additionalContext`,
  `systemMessage`, ...) and never plain text (it counts as a hook error).
- Hook configuration written by `ccobs setup` always has a short explicit `timeout`,
  never `onFailure: "block"` and never `statusMessage`.
- Never register `WorktreeCreate`, `WorktreeRemove`, `PreModelSwitch`,
  `MessageDisplay` or `FileChanged`: registering them changes Claude Code's behaviour.
- `SessionStart` and `Setup` do not accept `http` hooks.
- Claude Code must keep working normally with the stack down; this is tested. A down
  stack can still produce hook-error notices on some events; how we handle that
  trade-off is recorded in the hooks ADR.

### Privacy

- Metadata only by default. Without the capture-content opt-in, prompts, tool inputs
  and outputs, assistant messages and error text are dropped before anything is stored.
- The collector deletes personal attributes (`user.email`, `user.id` (the identity
  provider subject on gateway sessions), `user.account_uuid`, `user.account_id`,
  `organization.id`, and `user.groups` and `identity.source` on gateway sessions) by
  default.
- Paths in hook payloads (`cwd`, `transcript_path`, ...) contain the OS username;
  treat them as personal data.
- Test fixtures come from real sessions but are sanitized; a guard test fails if
  personal data (emails, account IDs, usernames in paths, hostnames) shows up in them.
- Secrets (such as `CCOBS_DISCORD_WEBHOOK_URL`) are `SecretStr` and never logged.

### Local only

- Every published port binds to `127.0.0.1`; there is no login.
- Browser defences (from v0.3.0), since any web page can send requests to localhost: the
  app only accepts a `Host` header in the allowlist (`localhost`, `127.0.0.1`, `[::1]`),
  the hook endpoint ignores any request whose `Content-Type` is not `application/json`
  (still answering an empty 2xx), and there is no permissive CORS.
- Keep a `host` label on stored data for a future central mode, without implementing it.

### Code

- Python 3.12, `src/` layout, ruff, mypy strict, pytest with coverage.
- `ccobs.domain` has no I/O (no database, HTTP or filesystem).
- The CLI must stay light: it never imports server-only dependencies at import time.
- Telemetry names: OTel names (`claude_code.token.usage`) differ from the names that
  reach Prometheus. Use the names recorded in the ADRs, never guesses.
- UI (from milestone 3): Jinja2 + HTMX + SSE with vendored JS and CSS (no CDN, works
  offline). No JavaScript dialogs (`alert`, `confirm`, `prompt`); use HTML modals.
  Before any UI change, the main context loads the `frontend-design` skill (when
  available) and passes the visual direction to the implementer; after it, the main
  context verifies the change in a real browser (Claude in Chrome, when available)
  before calling a UI stage done.

## Environment

- Developed on Windows with Git Bash and Docker Desktop; must work on Linux, macOS and
  Windows. Shell commands use bash syntax.
- Text files are UTF-8 without BOM and LF (`.editorconfig`, `.gitattributes`). Never
  create text files with `>` or `>>` in PowerShell (Windows PowerShell 5.1 writes
  UTF-16; PowerShell 7 writes CRLF).

## Commands

```bash
uv sync
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
uv run pre-commit run --all-files
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                               # unit tests (what CI runs)
uv run pytest tests/test_cli.py --no-cov    # one file (skip the coverage gate)
```

## Layout

```text
src/ccobs/        package (CLI entry point in cli.py)
tests/            unit tests; integration tests are marked @pytest.mark.integration
docs/adr/         architecture decision records (MADR)
.claude/agents/   project subagents
```

## Milestones

| Milestone | Release | Scope                                                                                  |
| --------- | ------- | -------------------------------------------------------------------------------------- |
| 0         | v0.1.0  | Foundation, tooling, CI, ADRs, telemetry and hooks spike, fixtures, first PyPI release |
| 1         | v0.2.0  | Observability stack in compose, provisioned Grafana                                    |
| 2         | v0.3.0  | Hook ingestion, session state machine, SQLite, blocks, `/metrics`                      |
| 3         | v0.4.0  | Web UI (HTMX + SSE)                                                                    |
| 4         | v0.5.0  | `ccobs setup`, `uninstall`, `doctor`, `status`                                         |
| 5         | v0.6.0  | Alerts (browser and Discord)                                                           |
| 6         | v0.7.0  | Grafana dashboards and polish                                                          |
| 7         | v1.0.0  | Distribution: GHCR multi-arch images, release hardening, contributor docs              |

Current: milestone 0.
