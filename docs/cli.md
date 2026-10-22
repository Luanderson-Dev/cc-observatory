# The `ccobs` CLI

A reference for the `ccobs` commands, from v0.5.0: `setup` and `uninstall`, which add
cc-observatory's hooks and telemetry variables to Claude Code's user settings and take
them out again, and `doctor` and `status`, which check the settings, the policies that
can block them and the stack. [app.md](app.md) and [stack.md](stack.md) cover what the
commands point Claude Code at; the decisions behind the CLI are ADRs 0036 to 0040 in
[`adr/`](adr/README.md), and this page links to them instead of repeating their
reasons.

## Install and run

The CLI is the `cc-observatory` package on PyPI. It needs [uv](https://docs.astral.sh/uv/)
(or any Python 3.12 environment) and nothing from the stack: it uses Python's standard
library only. `ccobs` and `cc-observatory` are the same program.

- **Without installing it:** `uvx cc-observatory <command>`, for example
  `uvx cc-observatory setup`. `uvx` keeps the version it downloaded the first time; add
  `@latest` (`uvx cc-observatory@latest doctor`) to pick up a new release.
- **Installed:** `uv tool install cc-observatory`, then `ccobs <command>`;
  `uv tool upgrade cc-observatory` updates it.
- **From a clone:** `uv run ccobs <command>` runs the clone's code.

`ccobs --version` prints the version, and `ccobs <command> --help` the options of each
command. Every command takes `--config-dir PATH`, Claude Code's configuration
directory. Without it, the CLI uses `$CLAUDE_CONFIG_DIR` when that variable is set (it
must be an absolute path), else `~/.claude` (`%USERPROFILE%\.claude` on Windows). A
`CLAUDE_CONFIG_DIR` set only in managed settings is invisible to the CLI; pass
`--config-dir` then. The settings file is `<config dir>/settings.json`.

The port options default to the stack's ports, all on `127.0.0.1`: `--app-port` 4300
(the app, which receives the hooks), `--otlp-port` 4318 (the collector's OTLP/HTTP
port) and `--grafana-port` 4330. If you moved one in the stack's `.env`
(`CCOBS_APP_PORT`, `CCOBS_OTLP_HTTP_PORT`, `CCOBS_GRAFANA_PORT`), pass the same port
to `setup`, `doctor` and `status` (`uninstall` takes no port options).

No command prints a value it read from a settings file, the environment or `.env`: the
user's `env` can hold API keys. They name keys, files and sources, and show only the
values cc-observatory writes itself (its hook URL and OTLP endpoint), ports and
versions.

## `ccobs setup`

~~~bash
uvx cc-observatory setup              # or: ccobs setup
~~~

`setup` writes two things into Claude Code's **user** settings, and keeps everything
else in the file as it is
([ADR 0037](adr/0037-how-ccobs-setup-and-uninstall-change-claude-codes-settings.md)):

- **The hooks** of [ADR 0025](adr/0025-registered-hook-events.md): 15 events as `http`
  hooks to `http://127.0.0.1:<app port>/hooks` with a 1-second timeout, plus
  `SessionStart` as an async `curl` command, the same block that
  [app.md](app.md#point-claude-code-at-the-app) shows (with `NUL` instead of
  `/dev/null` on Windows). Each event gets one group of its own after yours, and your
  own hooks keep running next to it.
- **The telemetry `env` block** of
  [ADR 0036](adr/0036-the-telemetry-env-block-that-ccobs-setup-writes.md):

  | Variable                                            | Value                          |
  | --------------------------------------------------- | ------------------------------ |
  | `CLAUDE_CODE_ENABLE_TELEMETRY`                      | `1`                            |
  | `OTEL_METRICS_EXPORTER`                             | `otlp`                         |
  | `OTEL_LOGS_EXPORTER`                                | `otlp`                         |
  | `OTEL_EXPORTER_OTLP_PROTOCOL`                       | `http/protobuf`                |
  | `OTEL_EXPORTER_OTLP_ENDPOINT`                       | `http://127.0.0.1:<otlp port>` |
  | `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `cumulative`                   |
  | `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`               | `1`, with `--traces` only      |
  | `OTEL_TRACES_EXPORTER`                              | `otlp`, with `--traces` only   |

It never writes a project's `.claude/settings.json` or `.claude/settings.local.json`
(Claude Code ignores the telemetry variables there), never sets a content variable
(`OTEL_LOG_*`; v0.5.0 stores metadata only,
[ADR 0040](adr/0040-defer-the-capture-content-opt-in.md)) or
`OTEL_METRICS_INCLUDE_SESSION_ID`, and never creates `allowedHttpHookUrls` or
`disableAllHooks`: an allowlist, once defined, blocks every `http` hook it does not
match, your own included. When an existing allowlist blocks the hook URL, `ccobs doctor`
says so (see [Troubleshooting](#hooks-are-blocked-by-a-policy)).

| Option              | Effect                                                                                                                                                                                |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--config-dir PATH` | Claude Code's configuration directory (see [Install and run](#install-and-run)).                                                                                                      |
| `--app-port N`      | The app's port in the hook URL (default 4300).                                                                                                                                        |
| `--otlp-port N`     | The collector's OTLP/HTTP port in the endpoint (default 4318).                                                                                                                        |
| `--traces`          | Also export traces. The stack must run with `docker compose --profile traces up -d` (Tempo).                                                                                          |
| `--force`           | Replace telemetry variables you set to other values; setup records yours, and `ccobs uninstall` puts them back.                                                                       |
| `--dry-run`         | Print a diff of what would change and write nothing. Values setup did not write show as `<your value>`, other hook groups as `<your hook group>` or `<earlier cc-observatory group>`. |

A run prints the settings file, the backup, what it wrote and the app's address:

~~~text
Claude Code settings: /home/you/.claude/settings.json
Backup: /home/you/.claude/ccobs/backups/settings.json.20261020T090308535959Z.bak
Wrote the hooks for 16 hook events; telemetry variables: 6 added, 0 updated, 0 removed.
Hooks apply to running Claude Code sessions now; telemetry starts in sessions you open from now on (restart the ones already open).
App: http://127.0.0.1:4300
~~~

- **When it applies.** Claude Code reloads its settings files when they change, so the
  hooks reach the sessions already open at once. Telemetry is read once, when a session
  starts: restart the sessions already open to see their tokens, cost and events
  ([ADR 0036](adr/0036-the-telemetry-env-block-that-ccobs-setup-writes.md)).
- **App down.** When the app does not answer at `127.0.0.1:<app port>`, setup warns on
  standard error and still writes. Until the stack is up, every `http` hook event
  shows a hook-error notice in Claude Code's transcript; Claude Code keeps working
  ([ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md)). Start the stack,
  or run `ccobs uninstall`.
- **Running it again is safe.** A second run with the same options prints "The settings
  are already up to date; nothing was written." A run with another `--app-port`,
  `--otlp-port` or `--traces` replaces setup's own entries, so there is always one
  copy of each hook. After an upgrade, run it again to bring the hooks to the new
  version (`ccobs doctor` says when they are out of date).
- **Your telemetry variables.** A variable you already set to the value setup writes is
  left as yours. One set to another value is a conflict: setup names the variables,
  writes nothing and exits 1; `--force` replaces them. Once you change a variable that
  setup wrote, it is yours again: setup and uninstall leave it alone.
- **Backups and state.** Before each write, setup copies `settings.json` to
  `<config dir>/ccobs/backups/` (the last 10 are kept). `<config dir>/ccobs/state.json`
  records the variables setup owns, the values `--force` replaced and the keys it
  created, so that `uninstall` can undo exactly that. Both are private to you (mode
  `0600` on Linux and macOS; on Windows they inherit the directory's permissions). An
  invalid state file is ignored with a warning, and setup carries on as if it had never
  run.
- **The file's format.** Setup keeps the key order, the indentation, the line endings
  (CRLF included), a UTF-8 BOM and a symlinked `settings.json` (it writes the target).
  Whitespace that the writer cannot reproduce may change, and numbers with a fraction
  are rewritten in Python's shortest form (`1e5` becomes `100000.0`); `--dry-run` shows
  it, and the backup undoes it.
- **Files it refuses.** Setup writes nothing and exits 2 when `settings.json` is not
  UTF-8 or not strict JSON (comments, trailing commas, a duplicate key, `NaN` or a
  number too large, a lone surrogate escape), is not a JSON object, nests deeper than
  100 levels, or holds `hooks` or `env` in a shape Claude Code would not accept. The
  message gives a line and column at most, never the content. Fix the file, then run
  setup again.
- **Recognition.** Setup and uninstall recognize their hooks by shape: an `http`
  handler to `http://127.0.0.1[:port]/.../hooks` with only `type`, `url` and `timeout`,
  or an async exec-form `curl` that posts its input to such a URL. A hook of yours with
  exactly that shape is taken as cc-observatory's.
- **A concurrent change.** If `settings.json` changes while setup runs (Claude Code
  writes it too, from `/config` and `/model`), setup writes nothing and exits 1; run it
  again.

Exit codes: 0 done, nothing to do, or a dry run; 1 a conflict (without `--force`) or a
concurrent change; 2 a usage error, or a settings or state file that cannot be read or
written.

## `ccobs uninstall`

~~~bash
ccobs uninstall --dry-run   # see what it would remove
ccobs uninstall
~~~

`uninstall` takes out only what setup added
([ADR 0037](adr/0037-how-ccobs-setup-and-uninstall-change-claude-codes-settings.md)):

- every hook handler it recognizes as cc-observatory's, and the groups, event keys and
  `hooks` or `env` objects that this leaves empty, when setup created them;
- every telemetry variable setup still owns: removed, or set back to the value
  `--force` replaced;
- the state file.

It keeps everything else, and says what it kept:

- "Kept ...: you changed them after setup." names the variables setup wrote that you
  changed since; they are yours now.
- "Still set, not added by ccobs setup: ..." names the telemetry variables that remain
  and that setup does not own (pasted by hand, for example). Remove them from the `env`
  block yourself to stop sending telemetry.

Without a valid state file (deleted, or rejected with a warning), uninstall still
removes the hooks it recognizes, but leaves the `env` block alone and lists the
telemetry variables in it. An event that removing those hooks leaves empty is deleted, even
if you created it yourself, since without the state it cannot tell who did. With no `settings.json`, there is nothing to remove.
`--dry-run` prints the diff, with the same masking as setup's, and writes nothing.

The hooks stop in the sessions already open at once; telemetry continues in those
sessions until you restart them. The stack keeps running: `docker compose down` in your
clone stops it, and `docker compose down -v` also deletes its data. Before writing,
uninstall backs `settings.json` up like setup.

Exit codes: 0 done or nothing to do; 1 `settings.json` changed while it ran (run it
again); 2 a usage error, or a settings file that cannot be read or written.

## `ccobs doctor`

~~~bash
ccobs doctor
ccobs doctor --json
~~~

`doctor` checks what cc-observatory needs and explains what stops data from arriving
([ADR 0039](adr/0039-ccobs-doctor-and-status-checks-sources-and-exit-codes.md)). It
changes nothing: `ccobs setup` is the only writer, and policies belong to you or your
organization. It takes `--config-dir` and the three port options, plus:

- `--stack-dir PATH`: your cc-observatory clone, for the Clone checks. Without it, they
  run only when the working directory is a clone (its `compose.yaml` has
  `name: cc-observatory`).
- `--json`: the checks as JSON instead of text.

Run it on the machine and OS where Claude Code runs: the expected hooks differ on
Windows (`NUL`), and the project checks read the working directory's `.claude/`.

### Checks

Each check has a stable id, a status and, when there is something to do, a fix. `ok`
is fine; `info` is a note; `warn` is a problem that does not stop data on its own, or
that doctor cannot be sure of; `fail` stops data.

| Section     | Id                      | What it checks                                                                                                                                                                                  |
| ----------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code | `curl`                  | `curl` is on `PATH` (warn: only the async `SessionStart` hook needs it).                                                                                                                        |
| Claude Code | `claude-version`        | `claude --version` is 2.1.296 or later, the version the contract was recorded on (warn when older or not found).                                                                                |
| Claude Code | `docker`                | Docker Engine 25.0, Compose 2.20.2 and Buildx 0.17.0 or later, and the Compose plugin present (fail); Engine 28.0 or later on Linux, the Buildx plugin present, Docker found and its daemon answering (warn).                                              |
| Settings    | `settings-file`         | The user `settings.json` exists and can be read (fail).                                                                                                                                         |
| Settings    | `hooks`                 | Every registered event holds setup's hook for `--app-port` (missing: fail; an older version's or another port's: warn).                                                                         |
| Settings    | `telemetry-env`         | The `env` block holds each value setup writes (missing or another value: fail; a temporality other than `cumulative`: warn).                                                                    |
| Settings    | `content-variables`     | No content variable (`OTEL_LOG_*`, the detailed beta tracing pair) is enabled in the user file, the shell or managed settings (warn, [ADR 0040](adr/0040-defer-the-capture-content-opt-in.md)). |
| Settings    | `session-id`            | `OTEL_METRICS_INCLUDE_SESSION_ID` is not `false` anywhere (warn: concurrent sessions' metrics collide).                                                                                         |
| Settings    | `per-signal-endpoints`  | No per-signal OTLP endpoint or protocol overrides the generic ones setup writes (warn).                                                                                                         |
| Settings    | `shell-overrides`       | Telemetry variables exported in your shell (info: the settings win, except when the Claude Desktop app or a self-hosted runner starts the session).                                             |
| Policies    | `settings-sources`      | Which managed settings doctor read or could not read (always info). Server-managed settings, from claude.ai or a gateway, are invisible to it: `/status` in Claude Code shows them.             |
| Policies    | `disable-all-hooks`     | `disableAllHooks` does not turn hooks off (fail in managed settings or the user file; warn when only this project turns them off).                                                              |
| Policies    | `managed-hooks-only`    | `allowManagedHooksOnly` is not `true` (fail in managed settings).                                                                                                                               |
| Policies    | `http-hook-allowlist`   | If `allowedHttpHookUrls` is defined anywhere, one of its patterns matches the hook URL (fail).                                                                                                  |
| Policies    | `managed-env`           | Managed settings do not send telemetry elsewhere, turn it off or change the protocol (fail).                                                                                                    |
| Policies    | `project-telemetry-off` | This project's `.claude/` files do not set an exporter to `none` (warn).                                                                                                                        |
| Stack       | `app`                   | `GET /healthz` on the app answers (fail).                                                                                                                                                       |
| Stack       | `hook-endpoint`         | A `POST {}` to the hook URL answers 2xx (fail). The app drops it as one `invalid` hook, so each run adds one `invalid` drop to its counters.                                                    |
| Stack       | `diagnostics`           | The app's `GET /api/v1/diagnostics`: a CLI and app version mismatch, Loki unavailable, hook failures in the last hour or dropped hooks warn; no hook since the app started is info.             |
| Stack       | `alerts`                | Discord delivery, from the same answer: Discord not configured (alerts in the browser only) or an app older than v0.6.0 is info; alerts that failed or expired in the last 24 h, alerts the app could not read, or alerts not understood (a CLI and an app from different releases), warn; a webhook Discord refused (401, 403 or 404, sending suspended until the app restarts) fails. |
| Stack       | `otlp`                  | The collector's OTLP/HTTP port accepts a connection (fail).                                                                                                                                     |
| Stack       | `grafana`               | Grafana's `/api/health` answers (warn).                                                                                                                                                         |
| Clone       | `stack-readable`        | Linux and macOS only: the containers can read `stack/` (fail; the fix is `chmod -R o+rX stack/`).                                                                                               |
| Clone       | `env-file`              | The clone's `.env`: `CCOBS_LOKI_RETENTION` a multiple of 24h (warn), `CCOBS_HOST_LABEL` valid (fail), and ports equal to the ones doctor used (warn).                                           |

The managed sources doctor reads are the managed settings files
(`managed-settings.json` and `managed-settings.d/*.json` in
`/Library/Application Support/ClaudeCode/` on macOS, `/etc/claude-code/` on Linux and
WSL, `C:\Program Files\ClaudeCode\` on Windows), the `Settings` registry value under
`HKLM` and `HKCU\SOFTWARE\Policies\ClaudeCode` on Windows, and the
`com.anthropic.claudecode` managed preferences on macOS. Doctor does not reproduce how
Claude Code picks among them: a key in any admin source counts, so it can report a
policy that is not in force (the detail names the source, to check in `/status`); the
user-writable `HKCU` value only warns.

The hook failure counts come from Claude Code's own `hook_execution_complete` events in
Loki, through the app ([ADR 0038](adr/0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)).
They include Claude Code's own hooks and your other hooks, so doctor can only warn on
them.

### Output and exit codes

Text output prints one line per check, grouped by section, with its detail and fix
indented below it, then a summary such as `Summary: 15 ok, 1 warn, 3 fail, 2 info`.
`--json` prints:

~~~json
{
  "version": "0.5.0",
  "checks": [
    {
      "section": "Stack",
      "id": "app",
      "status": "fail",
      "title": "the app does not answer",
      "detail": "GET http://127.0.0.1:4300/healthz: no answer.",
      "fix": "start the stack: `docker compose up -d` in your cc-observatory clone"
    }
  ],
  "summary": {
    "ok": 15,
    "warn": 1,
    "fail": 3,
    "info": 2
  }
}
~~~

`fix` is `null` when there is nothing to do. The JSON is ASCII, with other characters
escaped.

Exit codes: 0 when no check fails (warnings and notes included); 1 when one fails; 2 a
usage error (a `--stack-dir` that is not a directory, a relative `CLAUDE_CONFIG_DIR`).

### Common failures

| Check fails                                                                     | Meaning and fix                                                                                                                                                                           |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `settings-file`, `hooks`, `telemetry-env`                                       | Setup has not run for this configuration directory, or ran with other ports. Run `ccobs setup` with the same `--config-dir` and ports; for `telemetry-env` with another value, `--force`. |
| `app`, `hook-endpoint`, `otlp`                                                  | The stack is down, or listens on other ports: `docker compose ps` in your clone, then `docker compose up -d`; or pass the ports from your `.env`.                                         |
| `diagnostics` (warn)                                                            | "not understood": the app predates the CLI; rebuild it with `docker compose build app && docker compose up -d`. A version mismatch: rebuild the app, or upgrade the CLI.                  |
| `alerts` (fail)                                                                 | Discord refused the webhook: fix `CCOBS_DISCORD_WEBHOOK_URL` in the stack's `.env`, then `docker compose up -d`. Sending stays suspended until the app restarts.                          |
| `alerts` (warn)                                                                 | "not understood": the CLI and the app come from different releases; upgrade the CLI (`uvx cc-observatory@latest doctor`) or rebuild the app. Alerts that did not reach Discord: `docker compose logs app`. |
| `disable-all-hooks`, `managed-hooks-only`, `managed-env`, `http-hook-allowlist` | A policy blocks the hooks or the telemetry (see [Troubleshooting](#hooks-are-blocked-by-a-policy)).                                                                                       |
| `docker`                                                                        | Update Docker to the minimum versions in the table above ([stack.md](stack.md#requirements)).                                                                                             |
| `stack-readable`                                                                | Run `chmod -R o+rX stack/` in the clone, then `docker compose up -d`.                                                                                                                     |
| `env-file` (`CCOBS_HOST_LABEL`)                                                 | Use letters, digits, `.`, `_` or `-` in `.env`, then `docker compose up -d`.                                                                                                              |

## `ccobs status`

~~~bash
ccobs status
ccobs status --json
~~~

`status` is a short, fast subset of doctor
([ADR 0039](adr/0039-ccobs-doctor-and-status-checks-sources-and-exit-codes.md)): whether
the app, the collector and Grafana answer, whether setup's hooks and telemetry
variables are in the user settings, and the sessions by state, from the app's
diagnostics route. Its four probes run at once, so it takes at most about 3 seconds. It
runs no command, reads no managed settings and changes nothing. It takes `--config-dir`,
the three port options and `--json`.

~~~text
cc-observatory 0.5.0
App       up    http://127.0.0.1:4300
Collector up    127.0.0.1:4318 (OTLP/HTTP)
Grafana   up    http://127.0.0.1:4330
Claude Code settings: hooks installed, telemetry on (/home/you/.claude/settings.json)
Sessions: 1 working, 1 waiting on you, 0 idle, 3 ended, 0 stale; 1 open block
Hook failures (last hour): 0 non-blocking errors, 0 cancelled (42 runs)
Alerts: 1 on; Discord: on
Run `ccobs doctor` for details.
~~~

- **Settings:** the hooks are `installed`, `outdated` (setup's hooks from another
  version or port: run setup again) or `missing`; the telemetry is `on`, `off`,
  `other endpoint` or `incomplete`. "no settings file" or "unreadable" when the file is
  missing or cannot be read.
- **Sessions** and **hook failures** come from `GET /api/v1/diagnostics`. When the app
  does not give them, the line says why, with the reasons of the JSON below. The
  version line adds `(app X.Y.Z)` when the app runs another version than the CLI.
- **Alerts:** how many alerts are on, and Discord: `on`, `off` (no webhook configured)
  or `suspended (status N)` (Discord refused the webhook; `ccobs doctor` says how to
  fix it). An app older than v0.6.0 shows "unknown (the app predates alerts; rebuild
  it)"; alerts the CLI cannot read (a CLI and an app from different releases) show
  "unknown (the app's alerts were not understood)" while the other lines still show.
  Discord's state never changes the exit code.

`--json` prints:

| Field           | Value                                                                                                                                                                                                                                                       |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`       | The CLI's version.                                                                                                                                                                                                                                          |
| `app`           | `up`, `url` and `version` (the app's, `null` without diagnostics).                                                                                                                                                                                          |
| `collector`     | `up` and `address`.                                                                                                                                                                                                                                         |
| `grafana`       | `up` and `url`.                                                                                                                                                                                                                                             |
| `settings`      | `path`; `file`: `ok`, `missing` or `unreadable`; `hooks`: `installed`, `outdated` or `missing`; `telemetry`: `on`, `off`, `other_endpoint` or `incomplete` (both `null` when unreadable).                                                                   |
| `diagnostics`   | `understood` (`true` or `false`) and `reason`: `null`, or why there are none (below).                                                                                                                                                                       |
| `sessions`      | `by_state` (a count for `working`, `waiting_user`, `idle`, `ended` and `stale`) and `open_blocks`; `null` without diagnostics.                                                                                                                              |
| `hook_failures` | `available: true` with `window_s`, `executions`, `non_blocking_error` and `cancelled`; or `available: false` with `reason` (`unreachable`, `timeout`, `status`, `shape` or `unknown`: the app cannot read Loki) and `window_s`; `null` without diagnostics. |
| `alerts`        | `available: true` with the app's `alerts` fields (`rules`, `open`, `fired_24h`, `discord` with `configured`, `suspended_status`, `pending`, `failed_24h`, `expired_24h`); or `available: false` with `reason`: `unread` (the app could not read its alerts), `predates` (an app older than v0.6.0) or `not_understood` (a part of another shape); `null` without diagnostics. |

| `diagnostics.reason` | Meaning                                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| `app_down`           | The app's `/healthz` did not answer 2xx.                                                                      |
| `no_answer`          | The diagnostics route refused, reset or did not answer within 3 seconds.                                      |
| `busy`               | It answered 503: the app's database readers are busy. Try again.                                              |
| `no_route`           | It answered 404: the app predates the route. Rebuild it (`docker compose build app && docker compose up -d`). |
| `not_understood`     | Any other answer, or a document the CLI could not read.                                                       |

Exit codes: 0 when the app answers; 1 when it does not; 2 a usage error.

## The diagnostics route

`doctor` and `status` read the app's state from `GET /api/v1/diagnostics`
([ADR 0038](adr/0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)), which
[app.md](app.md#the-json-api) documents with the rest of the API. It needs no new port:
Loki and Prometheus publish none, so the app asks Loki for the hook failure counts.

## Troubleshooting

### Nothing shows up

Run `ccobs doctor` and fix what fails, top to bottom. The usual causes:

- the stack is down (`app`, `otlp`): `docker compose up -d` in your clone;
- setup has not run for the configuration directory Claude Code uses (`hooks`,
  `telemetry-env`): run `ccobs setup`, with `--config-dir` if you set
  `CLAUDE_CONFIG_DIR`;
- the session was open before setup: its hooks arrive, but its telemetry (tokens, cost,
  events) does not until you restart it;
- the stack runs on other ports: pass the ports from your `.env` to setup and doctor;
- a policy blocks the hooks or the telemetry (below).

[app.md](app.md#the-overview-stays-empty) and
[stack.md](stack.md#no-data-in-the-dashboard) cover the app's and the stack's side.

### Hook errors in the transcript

With the stack down, every event that an `http` hook sends shows a hook-error notice in
Claude Code's transcript; the notices are not sent to the model, and Claude Code keeps
working ([ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md)). Start the
stack (`docker compose up -d`), or, to stop the notices while it stays down, run
`ccobs uninstall`: the hooks stop in open sessions at once, and `ccobs setup` puts them
back later. `ccobs doctor` checks the hook endpoint (`hook-endpoint`) and reports the
last hour's hook failures (`diagnostics`).

### Hooks are blocked by a policy

Settings that `ccobs setup` never writes can switch its hooks or telemetry off. Doctor
names the file or source in each case:

- **`disableAllHooks: true`** turns every hook off. In your user file, remove it; in a
  project's `.claude/` files, it affects that project only; in managed settings, ask
  your administrator.
- **`allowManagedHooksOnly: true`** in managed settings runs only the organization's
  hooks: ask your administrator.
- **`allowedHttpHookUrls`**, once defined in any settings file, blocks every `http` hook
  whose URL no pattern matches. The lists of every file merge, so add the hook URL,
  such as `"http://127.0.0.1:4300/hooks"`, to `allowedHttpHookUrls` in your user
  `settings.json`, even when the list comes from managed settings. Setup never creates
  or edits the list, because defining it would block your other `http` hooks.
- **A managed `env`** that sets another endpoint or protocol, or turns telemetry off,
  overrides your user settings: ask your administrator.
- **Server-managed settings** (from claude.ai or a gateway) and the launch environment
  of the Claude Desktop app are invisible to the CLI. `/status` in Claude Code shows
  the settings sources in force.

### A content capture variable is enabled

`content-variables` warns when an `OTEL_LOG_*` flag, or the detailed beta tracing pair,
is enabled. cc-observatory v0.5.0 stores metadata only: the collector drops
content-bearing telemetry anyway, and the opt-in to keep content is deferred
([ADR 0040](adr/0040-defer-the-capture-content-opt-in.md)). Remove the variables, or set
them to `0`, to stop Claude Code from exporting content at all.

### Setup refuses to write

- "these telemetry variables in your settings hold other values": you set them
  yourself. Re-run with `--force` to replace them; `ccobs uninstall` puts yours back.
- "the settings file is not valid JSON (line L, column C)" and the other read errors:
  fix `settings.json` (Claude Code would not apply it either), then run setup again.
- "the file changed while setup ran": Claude Code or an editor wrote it at the same
  moment; run setup again.
- "CLAUDE_CONFIG_DIR must be an absolute path": set it to one, or pass `--config-dir`.

To undo a setup by hand, copy the latest backup from `<config dir>/ccobs/backups/` over
`settings.json`, then delete `<config dir>/ccobs/state.json`.
