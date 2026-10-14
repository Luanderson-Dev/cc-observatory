# Fixture capture tool

This tool records what Claude Code really sends: hook payloads, and the metrics, events
and traces it exports over OpenTelemetry. It then sanitizes that data into
`tests/fixtures/`, so parser tests run on real shapes instead of guessed ones. Run it
again whenever Claude Code changes (a new version, new hook events, new telemetry
attributes) and review what changed.

This is a developer tool, not the product. Its collector (`compose.yaml`) only writes to
files and has nothing to do with the cc-observatory stack.

> **Privacy warning.** `tools/capture/out/` holds raw data from real sessions. That data
> includes your home directory and username (in every path), your email address, account
> and organization IDs, your hostname, and the prompts and tool output of the capture
> sessions. Git ignores the directory. Never commit it, copy it elsewhere or paste it
> into an issue. Only sanitized output goes into the repository, and you must review the
> diff every time.

> **Cost.** The headless scenarios and the interactive driver run real Claude Code
> sessions, which call the Anthropic API (Haiku by default). A full recapture costs a few
> cents. Nothing here runs in CI.

## Files

| File                   | What it does                                                                  |
| ---------------------- | ----------------------------------------------------------------------------- |
| `compose.yaml`         | OTel Collector (contrib 0.162.0) on 127.0.0.1:4317/4318, writing to `out/`    |
| `collector.yaml`       | Collector config: OTLP in, debug and file exporters out                       |
| `capture_server.py`    | Records each hook request to a JSONL file and answers an empty 204            |
| `make_settings.py`     | Writes the settings file loaded with `claude --settings`: hooks and telemetry |
| `run_claude.sh`        | Runs `claude` as a fresh, isolated session with telemetry and hook settings   |
| `run_headless.sh`      | Runs every headless (`claude -p`) scenario; costs a little API money          |
| `drive_interactive.py` | Drives interactive scenarios in a pseudo-terminal (Windows only, pywinpty)    |
| `elicit_server.py`     | MCP server whose only tool asks for input (MCP elicitation)                   |
| `inspect_capture.py`   | Summarizes the raw telemetry and lists hook timings                           |
| `sanitize.py`          | Turns `out/` into sanitized fixtures, with a guard against personal data      |
| `workspace.py`         | Checks workspace roots and workspaces, writes their synthetic `CLAUDE.md`     |

## Prerequisites

- Docker with Compose, for the collector.
- [uv](https://docs.astral.sh/uv/), and `uv sync` run in the repository.
- Claude Code (`claude`), logged in.
- `curl` and `git` on `PATH`, and bash (Git Bash on Windows).
- Windows for the interactive driver: it uses pywinpty, which `uv run --with pywinpty`
  installs on the fly.

Run every command below from the repository root.

## 1. Start the collector

~~~bash
mkdir -p tools/capture/out
docker compose -f tools/capture/compose.yaml up -d
docker compose -f tools/capture/compose.yaml logs -f otel-collector   # optional
~~~

Create `out/` yourself before `up`. Otherwise Docker creates it owned by root, and on
Linux you could not delete what the collector writes there without `sudo` (the
collector runs as root in its container and writes files readable by everyone).

The collector appends every export request to `out/otlp-metrics.jsonl`,
`out/otlp-logs.jsonl` and `out/otlp-traces.jsonl` (`append: true` in `collector.yaml`,
so a restart keeps earlier runs; delete the files for a fresh capture). Sessions are
mixed in these files; the sanitizer separates them by `session.id`.

## 2. Run the headless scenarios

~~~bash
bash tools/capture/run_headless.sh                          # every scenario
bash tools/capture/run_headless.sh headless-api-error       # or only some
~~~

The script starts and stops its own capture servers (ports 8790 and 8791; port 8799
must stay free for the "endpoint down" scenario). Each scenario runs in its own
workspace under a workspace root and with a session ID chosen up front. When it
finishes, the script prints:

- `out/headless-sessions.tsv`: scenario name, session ID, hooks file and isolation (the
  run's `CAPTURE_ISOLATION`, `1` or `0`), one line per run (the script appends to it,
  so delete it before a fresh capture);
- the workspace root, which the sanitizer needs as `--workspace-root`.

Choose the workspace root with `CAPTURE_WORKSPACE_ROOT`. The default is
`C:/ccobs-capture/run-<UTC time>` in Git Bash and `/tmp/ccobs-capture/run-<UTC time>`
elsewhere. Whatever you choose, the root must:

- have no `CLAUDE.md`, `CLAUDE.local.md` or `.claude/` in it or in any directory above
  it. Claude Code loads those from every directory above its working directory, and
  `--setting-sources project,local` does not stop it (they count as project or local
  sources). On Windows your home has `.claude/`, so the root must be outside your home,
  such as `C:/ccobs-capture/<run>`;
- sit outside any git repository, whose branch and commits would reach the session
  (a workspace may hold its own repository: `headless-edit-commit-tasks` makes one);
- sit outside any Claude Code scratchpad. The model sees the workspace path, and a
  scratchpad path carries the project directory of the session that made it (such as
  `C--Projects-<repo>`), which the model can turn into new absolute paths.

`run_headless.sh` checks the root (`python -m tools.capture.workspace check-root`) and
`run_claude.sh` checks each workspace (`python -m tools.capture.workspace prepare`); both
refuse and name the offending path. `prepare` then writes a synthetic `CLAUDE.md` (one
line: "Synthetic project instructions for the capture workspace.") in the workspace, so
InstructionsLoaded still fires, as Project memory. A refused workspace makes
`run_claude.sh` exit with status 3 before anything runs; `run_headless.sh` then stops
the whole capture and does not record that session.

Every capture run is isolated from your own Claude Code setup by default:

- `run_claude.sh` passes `--setting-sources project,local`, so nothing from `~/.claude`
  takes effect: no user settings, hooks, plugins, `CLAUDE.md`, rules, skills or agents.
  Your `CLAUDE.md` would otherwise reach the context, and from there the compaction
  summary. (The `hook_registered` telemetry event still lists your user-settings hooks
  as an inventory, but they don't run: `hook_execution_complete` counts only the
  capture's hooks plus the ones Claude Code adds itself);
- it passes `--strict-mcp-config`, so only the servers given with `--mcp-config` load
  (none in the headless runs), and `--no-chrome`, so the Claude in Chrome integration
  does not connect (it adds a built-in MCP server of its own);
- it sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, because auto memory
  (`~/.claude/projects/<project>/memory/`) loads whatever `--setting-sources` says, and
  `ENABLE_CLAUDEAI_MCP_SERVERS=false`, which stops Claude Code from fetching your
  claude.ai connectors (the docs name it in `env-vars.md` and `mcp.md`);
- the settings file from `make_settings.py` still loads: `--settings` is the
  command-line level of the settings, not one of the setting sources (`user`,
  `project`, `local`). It carries the hooks and an `env` block that sends telemetry to
  the capture collector (`--otlp-endpoint`, default `http://127.0.0.1:4318`, also
  written as the per-signal `/v1/metrics`, `/v1/logs` and `/v1/traces` endpoints and
  protocols) with every `OTEL_LOG_*` content flag set to `"0"`,
  `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_EXPORTER_OTLP_HEADERS` blank, and delta
  temporality for metrics (Claude Code's default). Command-line settings outrank user
  settings, so even when your user settings load (`CAPTURE_ISOLATION=0`), an `env`
  block there cannot redirect a capture or turn content on; it could only add keys,
  which is why the unwanted ones are set to blank values;
- `run_claude.sh` also unsets the `OTEL_LOG_*` flags, `OTEL_RESOURCE_ATTRIBUTES` and the
  per-signal OTLP endpoints and headers in case your shell exports them.

To confirm the isolation: hooks reaching the capture server show that `--settings`
loaded; in an interactive session, `/status` lists the setting sources in use and `/mcp`
lists the MCP servers (only `spike-elicit` in the elicitation scenario, no connectors).
Claude Code still reports the hooks of your user settings in its `hook_registered`
events (event and type only, as an inventory), but does not run them: every
`hook_execution_start` and `hook_execution_complete` for Stop, UserPromptSubmit,
PreToolUse and SessionStart (events with no hooks of Claude Code's own) counts
`num_hooks` 1, the capture's hook, and `tests/test_fixtures.py` checks that.
The sanitizer checks it again (step 6). `~/.claude.json` (trust, account) and managed
settings are read in any case.

For an exploratory run that should load your own setup, set `CAPTURE_ISOLATION=0`
(`run_claude.sh` then only refuses a scratchpad and writes no `CLAUDE.md`). Never turn
such a run into fixtures.

## 3. Run the interactive scenarios (Windows)

Start a capture server in one terminal (it keeps running until Ctrl+C):

~~~bash
uv run python -m tools.capture.capture_server \
  --out tools/capture/out/hooks-interactive-permission-compact.jsonl
~~~

In another terminal, write the settings, create a workspace under a workspace root that
follows the rules of step 2 (such as the headless runs' root), and run the driver:

~~~bash
uv run python -m tools.capture.make_settings http --out tools/capture/out/settings-http.json
workspace="C:/ccobs-capture/<run>/interactive-permission-compact"
mkdir -p "$workspace"
session_id="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
echo "$session_id"
uv run --with pywinpty python tools/capture/drive_interactive.py \
  --scenario permission-compact \
  --hooks tools/capture/out/hooks-interactive-permission-compact.jsonl \
  -- bash tools/capture/run_claude.sh tools/capture/out/settings-http.json "$workspace" \
  --model haiku --session-id "$session_id"
~~~

`run_claude.sh` checks the workspace, writes its synthetic `CLAUDE.md` and isolates
the session as in step 2, so do not pass the isolation flags yourself.

The driver accepts the workspace trust dialog, types the prompts, presses the keys and
waits for the matching hook events in the hooks file. It saves the terminal text next to
the hooks file (`screen-<scenario>.log`).

For `--scenario elicitation-idle`, restart the capture server with another `--out` file
(for example `hooks-interactive-elicitation-idle.jsonl`), and give Claude Code the MCP
server. Save this as `tools/capture/out/mcp.json`, with the absolute path of your
checkout:

~~~json
{
  "mcpServers": {
    "spike-elicit": {
      "command": "uv",
      "args": [
        "run",
        "--no-project",
        "--with",
        "mcp<2",
        "python",
        "C:/path/to/cc-observatory/tools/capture/elicit_server.py"
      ]
    }
  }
}
~~~

Then add `--mcp-config "$PWD/tools/capture/out/mcp.json"` to the `claude` arguments.
With the `--strict-mcp-config` that `run_claude.sh` passes, the session gets this server
and none of yours. The path must be absolute, because `run_claude.sh` changes into the
workspace before it starts `claude`.

## 4. Describe the scenarios in `out/scenarios.json`

The sanitizer reads `tools/capture/out/scenarios.json`. Write or update it with the
session IDs from step 2 and step 3:

~~~json
{
  "claude_code_version": "2.1.296",
  "collector_version": "0.162.0",
  "captured_on": "2026-10-13",
  "platform": "windows",
  "isolated": true,
  "scenarios": [
    {
      "name": "headless-tools-subagent",
      "session_id": "00000000-0000-4000-8000-000000000000",
      "hooks": "hooks-headless-tools-subagent.jsonl",
      "otlp": true,
      "description": "What the session did and which events it shows."
    }
  ]
}
~~~

- `isolated`: `true` only when every run used the default isolation (the isolation
  column of `out/headless-sessions.tsv` is `1`, and the interactive runs went through
  `run_claude.sh` without `CAPTURE_ISOLATION=0`). The sanitizer refuses a capture
  without it (unless `--allow-unisolated`) and copies it into the fixtures manifest.
- `name`: lowercase letters, digits and dashes, unique; it becomes the fixture file name.
- `hooks`: the hooks file in `out/`, or `null` when no hook reached a server (the
  "endpoint down" scenario).
- `otlp`: `true` to extract the session's metrics, events and traces.
- `description`: what happened and what to look for. It is copied, sanitized, into the
  fixtures manifest.

`claude --version` gives the Claude Code version; the collector version is the image tag
in `compose.yaml`.

## 5. Inspect the raw data (optional)

~~~bash
uv run python -m tools.capture.inspect_capture summary
uv run python -m tools.capture.inspect_capture hook-timings timeout=<session id> down=<session id>
~~~

`summary` lists metric names, units, kinds, temporality and attribute keys, event names
and attribute keys, and span names, attribute keys and agent IDs. `hook-timings` lists
the `hook_execution_complete` events of each labelled session. The output can contain
raw values, so keep it local.

## 6. Sanitize into `tests/fixtures`

~~~bash
uv run python -m tools.capture.sanitize --raw tools/capture/out --dest tests/fixtures \
  --workspace-root "<workspace root>"
~~~

`--home`, `--user` and `--host` default to this machine's home directory, username and
hostname; pass them only when sanitizing a capture made elsewhere, or pass `--user ''`
or `--host ''` to not look for them at all (see "Conflicts" below). Pass
`--workspace-root` in the form Claude Code saw it, as `run_headless.sh` prints it (on
Windows, `C:\ccobs-capture\<run>`). It becomes `<fake home>/projects/capture` in every
spelling, so a root outside your home ends up under the fake home like the rest. The
sanitizer refuses a relative root, a drive or filesystem root, a single top-level
directory (`C:\ccobs-capture` alone), and the home directory or one above it: mapping
any of them would pass far more than the capture workspaces off as workspace.

More options:

- `--forbid TEXT` (repeatable): another text to remove and guard against, such as your
  full name, a GitHub handle or your employer. `git config user.name` and
  `git config user.email` are added on their own (skipped when git or the value is
  missing, or with a note when it would hit a placeholder).
- `--home-alias PATH` (repeatable): another spelling of your home directory. On Windows
  the 8.3 short name (`C:\Users\JOHNSM~1`) is added on its own.
- `--allow-content`: keep content-bearing telemetry (see below) instead of refusing it.
- `--allow-unisolated`: accept a capture that `scenarios.json` does not mark as
  `"isolated": true`, and InstructionsLoaded from outside the workspace, for
  exploratory use only; the manifest then records `"isolated": false`.

The sanitizer refuses, and writes nothing, when the selected telemetry carries content:
the `api_request_body`, `api_response_body` and `system_prompt` events; the `prompt`,
`prompt_text`, `response`, `tool_input`, `tool_parameters` and `user_prompt` attributes
with any value other than empty or `<REDACTED>`; and `tool.output` span events. The
capture settings turn that content off, so finding it means a flag leaked in from
somewhere.

Hook payloads keep their content (prompts, tool input and output, the main agent's
messages) on purpose: the prompts are synthetic and the sessions are isolated from your
own setup (step 2). Summaries written by internal agents are the exception, because
they summarize the whole context: PostCompact's `compact_summary`, and the
`last_assistant_message` of every SubagentStop whose `agent_type` the session did not
start through the Agent tool (a PreToolUse of `Agent` with that `subagent_type`) and
that is not built in (`general-purpose`, `Explore`, `Plan`). That covers compaction and
prompt suggestion, which run with an empty type, and fails closed on anything unknown.
They become `<internal summary removed>` before anything else runs; the keys stay, and
redaction never rewrites the placeholder.

The sanitizer writes the files below. It first deletes only the files the previous
`manifest.json` under `--dest` lists (and the directories they leave empty); other files
stay. If `--dest` has `hooks/` or `otlp/` but no manifest written by this tool (a wrong
`--dest`, such as `.git` or `~/.claude`), it refuses and touches nothing. The manifest
also records the `env` block of the `settings-*.json` files in `--raw` as
`telemetry_env` (unless `scenarios.json` has one).

~~~text
tests/fixtures/
  manifest.json                    versions, date, platform, scenarios and their files
  hooks/<scenario>.jsonl           {"offset_ms", "transport", "payload"} per hook request
  otlp/<scenario>/metrics.jsonl    the session's OTLP export requests, one per line
  otlp/<scenario>/logs.jsonl
  otlp/<scenario>/traces.jsonl
~~~

What it replaces:

| Data                                                  | Becomes                                       |
| ----------------------------------------------------- | --------------------------------------------- |
| summaries written by internal agents                  | `<internal summary removed>`                  |
| `user.email`                                          | `user@example.com`                            |
| `user.account_uuid`, `organization.id`                | fixed fake UUIDs                              |
| `user.account_id`                                     | `user_01EXAMPLE`                              |
| `user.id`                                             | 64 zeros                                      |
| `user.groups`, `identity.source`, `host.name`         | fixed placeholders                            |
| those identity values anywhere else                   | the same placeholders                         |
| the domain of a `user.email`, anywhere                | `example.com`                                 |
| home directory and its aliases, in every spelling     | `C:\Users\user` or `/home/user`               |
| `--workspace-root`, in every spelling                 | `<fake home>/projects/capture`                |
| username, hostname                                    | `user`, `example-host`                        |
| `--forbid` texts                                      | `redacted` (an email: `user@example.com`)     |
| Anthropic request IDs (`req_...`)                     | `req_EXAMPLE0001`, ... (same everywhere)      |
| tool use IDs (`toolu_...`, `srvtoolu_...`)            | `toolu_01EXAMPLE000000000000001`, ... (same)  |
| `client_request_id` values, anywhere                  | `00000000-0000-4000-a000-000000000001`, ...   |
| `plugin_id_hash` values, anywhere                     | `0000000000000001`, ...                       |
| plugin and marketplace names, anywhere                | `plugin-1`, `marketplace-1`, ...              |
| skill names that are not built in, anywhere           | `skill-1`, ...                                |
| MCP server names, anywhere                            | `mcp-server-1`, ...                           |
| any other email, `X:\Users\<name>`, `/home/<name>`    | `user@example.com`, the fake home             |

Tool use IDs keep their prefix, including the Vertex AI and Bedrock forms
(`toolu_vrtx_...`, `toolu_bdrk_...`). Names are collected from every name field
(`plugin.name`, `plugin_name`, `marketplace.name`, `marketplace_name`, `skill.name`,
`skill_name`, `skill`, `mcp_server_name`, `mcp_server.name`, `server_name`). Names that
Claude Code itself puts in name fields (`custom`, `third-party`, `mcp_tool`) and the
capture's own MCP server (`spike-elicit`) are kept. A name field holding a collected
name gets the placeholder of its own kind, so a marketplace named like one of its
plugins still becomes `marketplace-N` in `marketplace.name`; in free text such a name
gets the first placeholder (`plugin-N`).

"Every spelling" covers backslashes, forward slashes, escaped backslashes, Git Bash
paths (`/c/Users/...`) and the projects-directory form Claude Code uses, where every
non-alphanumeric character becomes `-` (`C--Users-<name>-...`). Matching ignores case.
The safety net also catches mixed separators (`C:\Users/<name>`), names with spaces or
apostrophes, WSL paths (`/mnt/c/Users/<name>`) and percent-encoded paths
(`C%3A%5CUsers%5C<name>`); it leaves URL paths such as `https://host/users/<name>` alone.

How each value is matched:

- **Inside longer words**: the username, hostname, `--forbid` texts and email domains of
  four characters or more. The whole word around them is replaced (dots, dashes and
  underscores at its edges stay): if your username is a prefix of your name or handle,
  `github.com/<handle>/repo` becomes `github.com/user/repo`. Shorter ones only match as
  whole words.
- **As whole words**: paths, identity values, plugin ID hashes, client request IDs, and
  plugin, marketplace, skill and MCP server names that are not plain words
  (`acme-tools`, `corp-db`).
- **Only where used as names**: names that are plain words (`git`, `memory`, `commit`),
  which are too common to replace everywhere. They are replaced as the whole value of a
  name field (`plugin.name`, `skill.name`, `mcp_server_name`, `mcp_server.name`, `skill`,
  ...), as the server in `mcp__<server>__<tool>` and as a segment of a
  `.claude/plugins/...` path, so `git commit` stays and `mcp__git__status` becomes
  `mcp__mcp-server-1__status`.

Plugin, marketplace, skill and MCP server names, plugin ID hashes and client request IDs
are collected first: from `plugin.name`, `marketplace.name`, `skill.name`,
`mcp_server_name`, `mcp_server.name` (an OTLP attribute, and the name in a hook
payload's `mcp_server` object), `plugin_id_hash` and `client_request_id`, from
`.claude/plugins/...` paths and from `mcp__<server>__<tool>` names.

Matches never cut into an ID: UUIDs, runs of 16 to 64 hex digits (trace, span and user
IDs) and Anthropic IDs (`toolu_...`, `msg_...`) stay whole unless they are exactly one of
the values. Dict keys are only matched as whole words.

Everything else stays as captured: session, prompt, agent, trace and span IDs, `msg_`
IDs, model names, the synthetic prompts and tool content, and every number.

Before writing anything, three checks run:

- **Structure.** Redaction must not change any dict key, OTLP attribute key, metric,
  span or event name, `hook_event_name`, ID field (`session_id`, `traceId`,
  `tool_use_id`, ...) or a value of a fixed vocabulary (`event.name`, `terminal.type`,
  `service.name`, `model`, ...), and must not make two keys of an object equal. The one
  change allowed there is a request or tool use ID turned into its fake. Otherwise the
  sanitizer stops and names the label of the value that caused it (for example
  `username`), never the value.
- **Isolation.** Every InstructionsLoaded must come from the capture workspace: its
  `memory_type` must be `Project` or `Local` and its `file_path` must be inside one of
  the workspaces under `<fake home>/projects/capture` once `--workspace-root` is
  replaced (a file directly in the root does not count). Otherwise the session saw your
  own instructions (user memory, rules, a `CLAUDE.md` above the workspace), and the
  sanitizer refuses with the location of each finding. `--allow-unisolated` skips this
  check.
- **Guard.** It looks for emails outside `example.com`; home directories of anyone but
  the fake user (including `C:\Users\user'Brien` or `C:\Users\user alice\...`); paths
  under the fake home other than `projects/capture/...`, `.claude/...`, `.claude.json`
  and `AppData/Local/Temp/...`, or with `..` in them (reported as
  `path outside workspace`); absolute paths that do not start with a fake home
  (`absolute path outside fake home`): another drive such as `D:\work`, `C:\Projects`,
  Git Bash and WSL drive paths (`/d/...`, `/mnt/d/...`), `/tmp`, `/opt`, `/var`,
  `/private`, `/mnt`, `/Users`, `/home/<name>`, `/Volumes`, `/media`, `/run/media`,
  `/root`, `/srv`, `/workspaces`, `/cygdrive`, home-relative paths (`~/x`, `~name`) and
  a drive in the projects-dir form (`C--Projects-...`), as a path segment or in prose;
  request and tool use IDs that are not fakes; `plugin_id_hash` and
  `client_request_id` values that are not fakes; letters and combining marks outside
  ASCII (`non-ascii letter`), after decoding literal `\uXXXX` escapes and NFC
  normalization: the capture prompts are English, so such letters point at text that
  came from somewhere else, such as your own instructions (punctuation such as an em
  dash, symbols, digits, the micro sign and the ordinal indicators are fine); and every
  value above, matched the same way it is replaced. It lists the kind and location of
  each finding, never the value.

If any of them finds something, the sanitizer writes nothing.

### Conflicts

A value that would match inside the placeholders or the allowed path words (`user`,
`example-host`, `example.com`, `redacted`, `AppData\Local\Temp`, `projects\capture`,
`.claude`) would rewrite the fakes themselves. If it is your username, hostname, home
or a `--forbid` text, the sanitizer refuses with a message naming its label; pass
`--user ''` or `--host ''` (your home directory is still replaced as a path) and add
distinctive forms of your name with `--forbid`, or leave out the `--forbid` text. If it
was found in the capture (an identity value or a plugin name), it is a generic word, so
it is not looked for.

A short username that is also a common word (`node`, `code`) still matches inside
longer words such as `node_modules`. The structure check stops the run if that touches
a key, a name or an ID; for free text, review the diff, and use `--user ''` with
`--forbid` if the result is not acceptable.

## 7. Review the diff

~~~bash
git status tests/fixtures
git diff --stat tests/fixtures
git diff tests/fixtures
~~~

Read the new fixtures before committing. Search them for your name, username, email
address, hostname, employer, and the names of your own plugins, skills and MCP servers;
the guard only knows the values listed above (add more with `--forbid`). Then stop the
collector:

~~~bash
docker compose -f tools/capture/compose.yaml down
~~~

Delete `tools/capture/out/` when you no longer need the raw data.

## Known limitations

The sanitizer fails closed on everything it knows about, but it is not a substitute for
reading the diff in step 7. Known gaps, none of which affect the values it derives for
the person running it (username, hostname, home directory, git identity):

- Hook fields such as `tool_name`, `permission_mode`, `source`, `reason`, `trigger`,
  `notification_type` and `agent_type` are not part of the structure check, so a
  `--forbid` value equal to a Claude Code name (for example `Read` or `default`)
  silently replaces it. Don't forbid Claude Code's own vocabulary.
- A plugin, marketplace, skill or MCP server named with a single plain word is only
  replaced where it is used as a name. If such a name is personal, also pass it with
  `--forbid`.
- The domain of an email given through git config or `--forbid` is not added as a
  literal on its own; pass the bare domain with `--forbid` if it can appear alone.
- With `--user ''`, UNC and WSL forms such as `\localhost\c$\Users\<name>`,
  `\wsl.localhost\<distro>\home\<name>` and `~<name>` are not caught by the safety net.
- Percent-encoded paths are redacted but skip the workspace allowlist, and the guard
  does not look for percent-encoded or UNC (`\\server\share`) paths outside the fake
  home, nor for relative paths that climb out of the workspace (`..\..\x`, `../../x`).
- The guard looks for the username, hostname and `--forbid` texts inside dict keys
  too, so a username that occurs inside a Claude Code key (`mary` in
  `compact_summary`) makes it refuse. Use `--user ''` and `--forbid` with distinctive
  forms of your name then.
- There is no fake for the system temp directory. Claude Code puts `scratchpad_dir`
  under it, which is inside the home on Windows (`AppData\Local\Temp`) but is `/tmp` or
  `/var/folders/...` on Linux and macOS, so the guard refuses a capture made there until
  the sanitizer learns to map it (a workspace root under `/tmp` is fine: it is mapped).
  Every capture so far ran on Windows.
- Only the internal summaries named above are replaced. The main agent's messages, and
  the messages of the subagents the session started, stay; if a capture prompt makes
  Claude quote its context, review those too.
- `--dest` trusts any `manifest.json` that has a `scenarios` list, and it does not
  guard against symlinks or junctions planted inside the destination. Point it only at
  `tests/fixtures`.
- Every email outside `example.com` becomes `user@example.com`, including public
  addresses such as `noreply@anthropic.com`.
- When `run_headless.sh` refuses a workspace root, it removes the root with `rmdir`
  if it is empty, so an empty `CAPTURE_WORKSPACE_ROOT` that existed before the run
  is removed too.
- The built-in subagent types (`general-purpose`, `Explore`, `Plan`) keep their last
  message even when no Agent tool call in the session started them.
- `run_claude.sh` uses exit status 3 for a refused workspace; if Claude Code itself
  ever exits with 3, `run_headless.sh` takes it for a refusal and stops.
- Error messages can echo the location (a key path) of a failed check on stderr; the
  fixtures are never written in that case.
