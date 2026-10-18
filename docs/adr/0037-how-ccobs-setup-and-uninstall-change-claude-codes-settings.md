---
status: accepted
date: 2026-10-18
decision-makers: Luanderson-Dev (maintainer)
---

# How `ccobs setup` and `uninstall` change Claude Code's settings

## Context and problem statement

`ccobs setup` (milestone 4) writes the hooks of
[ADR 0025](0025-registered-hook-events.md) and the `env` block of
[ADR 0036](0036-the-telemetry-env-block-that-ccobs-setup-writes.md) into the user
settings only ([ADR 0002](0002-native-telemetry-plus-hooks.md)), and
`ccobs uninstall` takes them out. That file belongs to the user, and Claude Code writes
it too, from `/config` and `/model` (settings, "Find or create your settings files" and
"Change a setting for one session"). It is strict JSON: a comment or a trailing comma,
like "a value the schema rejects", is a Settings Error, and the docs do not say whether
an unknown top-level key is one (settings, "Fix a broken settings file"). Hook edits
reach running sessions on save, while OpenTelemetry reads its variables once at session
start (settings, "When edits take effect"; environment variables, "Set variables in
settings files"; all read on 2026-10-18).
[ADR 0008](0008-distribution-compose-ghcr-pypi.md) left backup, merge, markers and
`--dry-run` to milestone 4. How do setup and uninstall find, change and later remove
cc-observatory's entries without damaging the user's?

## Decision drivers

- Never break the user's Claude Code: nothing in the file that the schema might reject.
- Never touch or lose a user entry, and leave the file as it was after an uninstall.
- No change to how the user starts Claude Code (terminal, IDE, desktop app).
- Re-runs and a port change are idempotent: one copy of each handler.
- Survive Claude Code rewriting the file between runs.
- Pure, testable merge logic in `ccobs.domain`, with file I/O in the CLI.

## Considered options

- **(1) A marker key inside `settings.json`.** Simple to find, but whether Claude Code
  accepts an unknown key is undocumented, and if it is a Settings Error the user's
  whole file stops applying.
- **(2) A sidecar settings file passed with `claude --settings`.** Nothing to merge,
  but the user must change how Claude Code starts in every shell, launcher, IDE and the
  desktop app, and a session started without the flag is silently unobserved.
- **(3) Recognition of our entries plus a state file.** Setup recognizes its handlers
  by their exact shape and records, outside `settings.json`, the `env` keys and
  containers it owns.

## Decision outcome

Chosen option: "(3) recognition of our entries plus a state file", because it writes
only documented keys, needs no change to how Claude Code starts and lets uninstall undo
exactly what setup did; (1) risks a Settings Error and (2) misses every session started
without the flag.

- **Target.** `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set (an
  absolute path), otherwise `~/.claude/settings.json` (`%USERPROFILE%\.claude` on
  Windows); `--config-dir` overrides both, and setup prints the target. Never a
  project's `.claude/settings.json` or `.claude/settings.local.json` (ADR 0002). A
  `CLAUDE_CONFIG_DIR` set only in managed settings is invisible to the CLI;
  `--config-dir` covers it.
- **Recognition.** `ccobs.domain.settings_merge` (pure) owns a handler only if it is:
    - `http`: keys a subset of `type`, `url` and `timeout`, `type` `"http"`, and a URL
      that `hook_config.is_hook_url` accepts (`http` to `127.0.0.1`, an optional port,
      a path ending in `/hooks`; ADR 0025), with any `timeout`, so older shapes stay
      recognized;
    - async `curl`: keys a subset of `type`, `command`, `args`, `async` and `timeout`,
      `type` `"command"`, `command` `"curl"`, `async` `true`, and `args` that contain
      `--data-binary` followed by `@-` and end with such a URL (ADR 0011's exec form).
  Anything else (extra keys such as `headers`, another URL, a shell-form command) is
  the user's and is never touched. Merge removes every recognized handler and the
  groups that removal emptied, then appends one group per event after the user's; an
  emptied event key that is still registered keeps its place and gets the group back
  there, and only keys no longer registered are dropped when emptied, so a re-run or a
  port change leaves exactly one copy.
- **State.** `<config dir>/ccobs/state.json` (versioned) holds the `env` keys setup
  wrote and owns, the user values `--force` replaced, and whether setup created the
  `env` object, the `hooks` object and each event key. The created event keys persist
  across runs (the union with the previous state, limited to registered events still
  present). It holds only values from `telemetry_env()` or from the user's own file. A
  state with any shape error, or with an `env` key that is not a cc-observatory
  telemetry variable, is rejected and treated as missing, so a corrupt state cannot
  remove a user's key. Without a valid state, uninstall still removes the recognized
  handlers but leaves `env` alone.
- **Env.** A key is setup's only while its current value equals what setup wrote; once
  the user changes it, setup stops owning it and treats it like any user key. A key
  that is absent is added and owned; a user key with the same value (pasted from the
  README) is left and not owned, so uninstall keeps it; a user key with another value
  is a conflict: setup lists the keys and writes nothing unless `--force`, which
  overwrites, owns and records the old value for uninstall to restore. An owned key
  that is no longer desired (`--traces` turned off) is restored or removed.
- **Uninstall.** Removes every recognized handler and the groups, event keys and
  containers emptied by that removal (with a state, a container or event key only if
  setup created it, so a user's own empty event list survives; without one, every
  event key the removal emptied, and no container); restores or deletes each `env` key
  setup still owns. It then lists the owned keys it kept because the user changed them
  and every cc-observatory telemetry variable still present that setup does not own
  (pasted by hand, or left after a lost state and a re-run), so telemetry is never left
  on silently.
- **Never** creates `allowedHttpHookUrls` or `disableAllHooks`. The allowlist merges
  across settings files and, once defined, blocks every `http` hook it does not match
  (settings reference, `allowedHttpHookUrls`), so creating it would block the user's
  other `http` hooks. When an existing allowlist blocks our URL, setup only warns (the
  doctor stage).
- **File handling** (the CLI stage):
    - invalid JSON or an unexpected shape aborts without writing or repairing anything;
    - before every write, a backup of the current file goes to
      `<config dir>/ccobs/backups/`, keeping the last 10; backups and the state get mode
      `0600` where the OS supports it;
    - the write goes to a temporary file in the same directory, then `os.replace`, after
      checking (by hash) that the file did not change since it was read, with a short
      retry on Windows while the file is held open; a symlinked `settings.json` is
      written through to its target;
    - key order, indentation, a UTF-8 BOM and CRLF line endings are kept; every `env`
      value is a string.
- **`--dry-run`** writes nothing and prints a diff of only the keys setup touches,
  masking every value it did not write, since the user's `env` can hold API keys.
- **Messages.** Hooks apply to running sessions at once, and with the stack down every
  `http` event shows a hook-error notice (ADR 0011); telemetry starts only in sessions
  started afterwards; after uninstall, running sessions keep exporting until restarted,
  since removing a variable from the file does not unset it in a running session
  (environment variables, "Set variables in settings files").
- **App unreachable during setup:** warn and write anyway.

### Consequences

- Good, because nothing changes in how Claude Code is started, and the file holds only
  documented keys.
- Good, because merge then unmerge with the state restores the original file, key
  order included, and recognition works even after Claude Code rewrites the file.
- Bad, because a user's own `http` hook to `127.0.0.1/.../hooks` with only `type`,
  `url` and `timeout`, or exec-form async `curl` that posts stdin (`--data-binary @-`)
  to such a URL, is taken as ours; the docs say so.
- Bad, because whitespace details of a hand-edited file that the writer cannot
  reproduce may change; `--dry-run` and the backups show and undo it.
- Bad, because a lost or rejected state file leaves the `env` keys for the user to
  remove; the uninstall output lists them, as it does for any unowned telemetry key.
- Neutral, because Claude Code may rewrite the file between runs (reordering or
  reformatting it); recognition still finds our entries, and the hash check stops a
  write that would lose a change made while setup ran.

### Confirmation

- `tests/test_domain_settings_merge.py` checks the round trip, idempotence, port change,
  older shapes, `env` conflicts with and without `--force`, foreign handlers left
  untouched, unchanged inputs, shape errors without file values, and that
  `allowedHttpHookUrls` and `disableAllHooks` are never created.
- The `ccobs setup` and `uninstall` tests of milestone 4 check target resolution, that
  no project settings file is ever written (ADR 0002), backups, the atomic write, the
  masked `--dry-run`, and that BOM, CRLF, indentation and symlinks are kept.

## More information

- Claude Code [settings](https://code.claude.com/docs/en/settings),
  [settings reference](https://code.claude.com/docs/en/settings-reference)
  (`allowedHttpHookUrls`, `disableAllHooks`, `env`),
  [hooks](https://code.claude.com/docs/en/hooks) ("Disable or remove hooks") and
  [environment variables](https://code.claude.com/docs/en/env-vars)
  (`CLAUDE_CONFIG_DIR`).
- Closes the open item of ADR 0008 (backup, merge, markers, `--dry-run`); builds on
  ADRs 0002, 0011, 0025 and 0036. `README.md` says setup marks what it adds; the
  docs stage of milestone 4 corrects it to recognition plus the state file.
- Revisit when Claude Code documents how it treats unknown keys, or offers a supported
  way to add and remove settings entries.
