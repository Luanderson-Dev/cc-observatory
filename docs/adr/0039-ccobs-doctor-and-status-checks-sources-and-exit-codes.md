---
status: accepted
date: 2026-10-18
decision-makers: Luanderson-Dev (maintainer)
---

# `ccobs doctor` and `status`: checks, sources and exit codes

## Context and problem statement

Earlier ADRs left a list of checks to `ccobs doctor` (milestone 4): probe the hook URL
and read the hook failure counts
([ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md), lines 101 to 109, 128
and 147 to 148), warn on Claude Code older than 2.1.296
([ADR 0012](0012-observed-telemetry-and-hook-contract.md), line 312), on
`OTEL_METRICS_INCLUDE_SESSION_ID=false` and on a Loki retention that is not a multiple
of 24h ([ADR 0016](0016-cardinality-and-retention.md), lines 81 to 82, 102 and 159 to
161), check `stack/` permissions and the Docker minimums
([ADR 0020](0020-compose-conventions.md), lines 158 and 223 to 224), validate the host
label ([ADR 0022](0022-the-host-label.md), lines 75 to 76), warn on content flags
([ADR 0006](0006-privacy-metadata-only-by-default.md), lines 60 and 93) and on
per-signal endpoints
([ADR 0036](0036-the-telemetry-env-block-that-ccobs-setup-writes.md)), and show the
app's state ([ADR 0028](0028-app-metrics-and-their-listener.md), lines 15 and 37,
through the route of
[ADR 0038](0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)).
[ADR 0018](0018-ports-networks-and-exposure.md) (lines 146 to 149) left open whether
doctor warns on Docker Engine older than 28. Beyond those, hooks and telemetry can be
switched off by settings `ccobs setup` never writes: `disableAllHooks`,
`allowManagedHooksOnly`, an `allowedHttpHookUrls` that does not match our URL, or a
managed `env`. Which checks does doctor run, where does it read settings from, what
does it print, and how does it exit? And what does the lighter `ccobs status` show?

## Decision drivers

- One command that explains most "no data" cases, for a user and for a script.
- `ccobs setup` stays the only writer of Claude Code's settings
  ([ADR 0037](0037-how-ccobs-setup-and-uninstall-change-claude-codes-settings.md));
  policies belong to the user or the organization.
- Never print a secret: settings `env` and `.env` can hold API keys and tokens.
- No reimplementation of Claude Code's managed precedence, which changes between
  releases and depends on the credential a session uses.
- The CLI stays light: stdlib only, pure rules in `ccobs.domain`, I/O injected.

## Considered options

- **(1) A single pass/fail.** Simple to script, but it hides partial problems: a
  missing `curl` and a down app give the same answer.
- **(2) Checks with `ok`, `warn`, `fail` and `info`, and exit 1 only on `fail`.** Each
  finding says how serious it is and how to fix it.
- **(3) Doctor fixes what it finds.** Convenient, but it makes doctor a second writer
  next to setup (ADR 0037), and it would edit policies that are not ours to change.

## Decision outcome

Chosen option: "(2) checks with `ok`, `warn`, `fail` and `info`, and exit 1 only on
`fail`", because it reports every problem at its own level and stays scriptable; (1)
hides partial problems, and (3) breaks ADR 0037's single writer and touches the user's
or the organization's policies.

- **Output.** Each check has a stable `id`, a `section` (Claude Code, Settings,
  Policies, Stack, Clone), a status (`ok`, `warn`, `fail` or `info`), a title, a detail
  and an optional fix. Text output groups the checks by section and ends with counts;
  `--json` gives `{"version": ..., "checks": [...], "summary": {...}}`, the checks with
  the same fields. The ids are part of the interface: tests and docs refer to them.
- **Encoding.** Output survives a console that is not UTF-8: the JSON is ASCII-escaped,
  and text output replaces the characters the console cannot encode.
- **Exit codes.** 0 when there are only `ok`, `warn` and `info`; 1 when any check
  fails; 2 for a usage error.
- **Options.** `--config-dir` (resolved as setup does, ADR 0037), `--app-port`,
  `--otlp-port`, `--grafana-port`, `--stack-dir` and `--json`.
- **Checks, by section.** The thresholds come from the ADRs above; the exact rules
  live in `ccobs.domain.doctor` and its tests.
    - Claude Code and host: `curl` (missing: warn, only the async `SessionStart` hook
      needs it), `claude-version` (older than 2.1.296 or unknown: warn) and `docker`
      (Engine below 25.0, Compose below 2.20.2 or Buildx below 0.17.0: fail; Engine
      below 28.0 on Linux: warn, which closes ADR 0018's open item; Docker missing or
      its daemon silent: warn, since the stack checks tell whether the stack is up).
    - Settings, in the user file: `settings-file` (missing or unreadable: fail),
      `hooks` (compared with `hook_settings()` for `--app-port`; older or other-port
      cc-observatory handlers warn), `telemetry-env` (compared with `telemetry_env()`;
      a temporality other than `cumulative` warns), `content-variables`, `session-id`
      and `per-signal-endpoints` (each a warn, also read from the shell; content
      variables set in managed settings warn too), `shell-overrides` (info, see
      Sources) and `settings-sources` (always info: the managed documents read, the
      managed and project files that could not be read, and the note that
      server-managed settings are invisible locally).
    - Project files follow "Variables Claude Code ignores in `env`" (settings
      reference): from v2.1.282 Claude Code ignores the content variables and the
      `OTEL_EXPORTER_OTLP_*_ENDPOINT` variables in project and local files, so doctor
      does not count them there; `OTEL_METRICS_INCLUDE_SESSION_ID` is not on that
      list, so `session-id` still reads the project files.
    - Policies: `disable-all-hooks`, `managed-hooks-only`, `http-hook-allowlist`,
      `managed-env` and `project-telemetry-off`.
    - Stack: `app` (`GET /healthz`), `hook-endpoint` (a direct `POST {}` to `/hooks`,
      ADR 0011), `diagnostics` (ADR 0038: version mismatch, Loki unavailable, hook
      failure counts and drops warn; no event received is info), `otlp` (a TCP
      connect) and `grafana` (`/api/health`, warn).
    - Clone, only with `--stack-dir` or in a clone of this repository:
      `stack-readable` (POSIX only, fail with `chmod -R o+rX stack/`) and `env-file`
      (the Loki retention and the host label of ADRs 0016 and 0022, and ports that
      differ from the flags doctor used).
- **Sources.** Doctor reads the user file, the project files `.claude/settings.json`
  and `.claude/settings.local.json` of the current directory, the shell environment,
  and the managed sources of the managed settings page ("Where each mechanism stores
  the policy"): `managed-settings.json` and `managed-settings.d/*.json` in the system
  directory (`/Library/Application Support/ClaudeCode/` on macOS, `/etc/claude-code/`
  on Linux and WSL, `C:\Program Files\ClaudeCode\` on Windows, never the legacy
  `C:\ProgramData` path), the `Settings` value (`REG_SZ` or `REG_EXPAND_SZ`) under
  `HKLM\SOFTWARE\Policies\ClaudeCode` and `HKCU\SOFTWARE\Policies\ClaudeCode` on
  Windows, and the `com.anthropic.claudecode` managed preferences on macOS. A managed
  document it cannot read or parse is an info. Server-managed settings, fetched from
  claude.ai or a gateway, are invisible locally: an info always says so and points to
  `/status` in Claude Code, which names the sources in force. In `.env`, an empty
  value means unset, as compose's `${VAR:-default}` treats it.
- **Commands.** Doctor runs `claude` and `docker` without a shell (`curl` is only
  looked up on `PATH`), with a 10 s timeout that kills the process without waiting on
  pipes a grandchild may still hold (`claude.cmd` on Windows, Docker's CLI plugins).
- **No reimplementation of managed precedence.** Claude Code picks one managed source
  (or merges them) by rules that depend on `managedSourcesBehavior` and on the
  credential ("How Claude Code combines managed sources"). Doctor does not follow them:
  a key set by any admin source (the plist, HKLM or the files) counts, and HKCU, which
  is user-writable and not an admin source, is only warn-level evidence.
- **Policy rules.**
    - `disableAllHooks` set in an admin source decides (true fails, false is ok);
      otherwise the highest of local, project and user that sets it decides: true in
      the user file fails, true only in the project files warns ("hooks are off in
      this project"), and an info notes a user file that disables hooks when only this
      project re-enables them. Claude Code "reads the value left after settings
      precedence applies, so a `"disableAllHooks": false` in a project's
      `.claude/settings.json` overrides a `true` in your user settings" (hooks,
      "Disable or remove hooks"); a `true` passed with `--settings` is invisible to
      doctor.
    - `allowManagedHooksOnly: true` in an admin source fails (HKCU: warn); it blocks
      every user hook (settings reference, "What runs under `allowManagedHooksOnly`").
    - `allowedHttpHookUrls` arrays merge across settings files and, once defined, block
      every `http` hook they do not match; an empty array blocks all, and `*` is the
      wildcard (settings reference, `allowedHttpHookUrls`; hooks, "the merged
      allowlist"). Doctor takes the union and matches our hook URL with `*` as any run
      of characters and the rest literal, the host case-insensitively and a trailing
      dot on the host ignored (the reference: hostname matching is case-insensitive
      and `example.com.` equals `example.com`), and the scheme case-insensitively too,
      our own choice (schemes are case-insensitive, RFC 3986 §3.1; the reference does
      not say). It fails when nothing matches and always tells the user to add
      `http://127.0.0.1:<port>/hooks` to the user settings file: the lists merge, so
      an organization's list can be extended from there. It never adds it: setup does
      not create or edit the list (ADR 0037).
    - A managed admin `env` that sets the endpoint, a per-signal endpoint, telemetry
      off, an exporter other than `otlp` (`none` included) or a protocol other than
      `http/protobuf` fails (HKCU: warn). Exporter selectors are read as
      comma-separated lists, as Claude Code takes them: `otlp,console` passes, and only
      `none` on its own counts as off. `project-telemetry-off` warns only on an
      exporter set to `none` in the project or local files, the value Claude Code
      still honours there (settings reference, "Variables Claude Code ignores in
      `env`").
- **Shell and launch environment.** A settings `env` value overwrites the same
  variable exported in the shell, so shell exports of the block's variables are info
  only; when the Claude Desktop app or a self-hosted runner starts the session, the
  launch environment it builds wins over every settings file instead (settings
  reference, "How `env` values interact with your shell"). Doctor cannot see that
  environment, and the info says so.
- **Values never printed.** Doctor prints no value read from a settings file, the
  environment or `.env`, except what cc-observatory writes itself (its hook URL and
  endpoint), ports and versions; any other value is "holds another value". It may
  print the paths of the files it read, since it runs in the user's own terminal.
- **The probe's drop.** The `POST {}` probe is dropped as `invalid` (ADR 0038), so
  each run adds one `invalid` drop; the `hook-endpoint` detail says so.
- **`ccobs status`** (stage 8) is a short, fast subset: app, OTLP and Grafana
  reachability, sessions by state and open blocks from the diagnostics route, whether
  setup's hooks and `env` are in place, and the URLs, with `--json`. It exits 0 unless
  the app is unreachable (1).

### Consequences

- Good, because one command explains most "no data" cases: hooks off, an allowlist,
  a managed override, a down service, an old Claude Code or Docker.
- Good, because the JSON output and the exit codes make doctor scriptable, and the
  stable ids let docs and a future UI refer to each check.
- Bad, because server-managed settings and the Desktop launch environment are
  invisible to the CLI; doctor says so with an info and points to `/status`.
- Bad, because managed precedence is approximated: a key in an admin source that
  Claude Code does not select still counts, so doctor can report a policy that is not
  in force; the fix text names the source so the user can check it in `/status`.
- Bad, because the hook failure counts include Claude Code's own hooks and the user's
  other hooks (ADR 0012, lines 155 to 157), so they can only warn.
- Neutral, because each run adds one `invalid` drop to the app's counters.

### Confirmation

- `tests/test_domain_doctor.py` covers every rule branch with data structures, and
  checks that placeholder secrets seeded in settings, the environment and `.env` never
  appear in any detail or fix.
- `tests/test_cli_doctor.py` covers the text and `--json` output, exit codes 0, 1 and
  2, the managed readers per OS (files, registry, plist) with injected readers and no
  real registry, `.env` parsing and stack-dir detection, with no network and no real
  subprocess; `tests/test_cli_light.py` keeps the CLI light.
- The integration test of stage 10 runs doctor against the `ccobs-it` stack.

## More information

- Claude Code [managed settings](https://code.claude.com/docs/en/managed-settings)
  ("Where each mechanism stores the policy", "How Claude Code combines managed
  sources", "Read the source in /status"),
  [server-managed settings](https://code.claude.com/docs/en/server-managed-settings),
  [settings](https://code.claude.com/docs/en/settings) ("Settings precedence"), the
  [settings reference](https://code.claude.com/docs/en/settings-reference)
  (`allowedHttpHookUrls`, `allowManagedHooksOnly`, `disableAllHooks`, `env`,
  "Variables Claude Code ignores in `env`") and
  [hooks](https://code.claude.com/docs/en/hooks) (the HTTP hook allowlists), all read
  on 2026-10-19.
- Closes the doctor items of ADRs 0006, 0011, 0012, 0016, 0018, 0020, 0022, 0028 and
  0036; builds on ADRs 0037 and 0038. Content capture itself stays deferred (ADR 0040,
  later in this milestone).
- Revisit when Claude Code exposes the effective settings to other programs, or
  changes which files may set hooks and telemetry variables.
