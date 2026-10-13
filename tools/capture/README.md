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
| `run_claude.sh`        | Runs `claude` as a fresh session with telemetry on and the hook settings      |
| `run_headless.sh`      | Runs every headless (`claude -p`) scenario; costs a little API money          |
| `drive_interactive.py` | Drives interactive scenarios in a pseudo-terminal (Windows only, pywinpty)    |
| `elicit_server.py`     | MCP server whose only tool asks for input (MCP elicitation)                   |
| `inspect_capture.py`   | Summarizes the raw telemetry and lists hook timings                           |
| `sanitize.py`          | Turns `out/` into sanitized fixtures, with a guard against personal data      |

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
workspace under a scratch directory (set `CAPTURE_WORKSPACE_ROOT` to choose it) and with
a session ID chosen up front. When it finishes, the script prints:

- `out/headless-sessions.tsv`: scenario name, session ID and hooks file, one line per
  run (the script appends to it, so delete it before a fresh capture);
- the workspace root, which the sanitizer needs as `--workspace-root`.

The capture runs are kept apart from your own Claude Code setup:

- the settings file from `make_settings.py` carries an `env` block that sends telemetry
  to the capture collector (`--otlp-endpoint`, default `http://127.0.0.1:4318`, also
  written as the per-signal `/v1/metrics`, `/v1/logs` and `/v1/traces` endpoints and
  protocols) with every `OTEL_LOG_*` content flag set to `"0"`, `OTEL_RESOURCE_ATTRIBUTES`
  and `OTEL_EXPORTER_OTLP_HEADERS` blank, and delta temporality for metrics (Claude
  Code's default). Settings given with `--settings` outrank your user settings, so an
  `env` block there (from `ccobs setup`, say) cannot redirect a capture or turn content
  on; it can only override keys, which is why the unwanted ones are set to blank values;
- `run_claude.sh` also unsets the `OTEL_LOG_*` flags, `OTEL_RESOURCE_ATTRIBUTES` and the
  per-signal OTLP endpoints and headers in case your shell exports them;
- every headless run passes `--strict-mcp-config`, so your own MCP servers stay out.

Your own hooks and plugins from `~/.claude/settings.json` still load. To skip your user
settings too, set `CAPTURE_SETTING_SOURCES=project,local` for `run_headless.sh` (it
becomes `claude --setting-sources project,local`; the scratch workspaces have no project
or local settings). This is not the default: it also drops anything else in your user
settings, and the fixtures should show a normal setup.

## 3. Run the interactive scenarios (Windows)

Start a capture server in one terminal (it keeps running until Ctrl+C):

~~~bash
uv run python -m tools.capture.capture_server \
  --out tools/capture/out/hooks-interactive-permission-compact.jsonl
~~~

In another terminal, write the settings, create a workspace inside the same scratch
directory as the headless runs, and run the driver:

~~~bash
uv run python -m tools.capture.make_settings http --out tools/capture/out/settings-http.json
workspace="<workspace root>/interactive-permission-compact"
mkdir -p "$workspace"
session_id="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
echo "$session_id"
uv run --with pywinpty python tools/capture/drive_interactive.py \
  --scenario permission-compact \
  --hooks tools/capture/out/hooks-interactive-permission-compact.jsonl \
  -- bash tools/capture/run_claude.sh tools/capture/out/settings-http.json "$workspace" \
  --model haiku --session-id "$session_id" --strict-mcp-config
~~~

`--strict-mcp-config` keeps your own MCP servers out of the session; add
`--setting-sources project,local` as well to skip your user settings (see step 2).

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

Then add `--mcp-config "$PWD/tools/capture/out/mcp.json"` next to `--strict-mcp-config`
in the `claude` arguments, so the session gets this server and none of yours. The path
must be absolute, because `run_claude.sh` changes into the workspace before it starts
`claude`.

## 4. Describe the scenarios in `out/scenarios.json`

The sanitizer reads `tools/capture/out/scenarios.json`. Write or update it with the
session IDs from step 2 and step 3:

~~~json
{
  "claude_code_version": "2.1.296",
  "collector_version": "0.162.0",
  "captured_on": "2026-10-12",
  "platform": "windows",
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
`--workspace-root` in the form Claude Code saw it (on Windows, `C:\...`).

More options:

- `--forbid TEXT` (repeatable): another text to remove and guard against, such as your
  full name, a GitHub handle or your employer. `git config user.name` and
  `git config user.email` are added on their own (skipped when git or the value is
  missing, or with a note when it would hit a placeholder).
- `--home-alias PATH` (repeatable): another spelling of your home directory. On Windows
  the 8.3 short name (`C:\Users\JOHNSM~1`) is added on its own.
- `--allow-content`: keep content-bearing telemetry (see below) instead of refusing it.

The sanitizer refuses, and writes nothing, when the selected telemetry carries content:
the `api_request_body`, `api_response_body` and `system_prompt` events; the `prompt`,
`prompt_text`, `response`, `tool_input`, `tool_parameters` and `user_prompt` attributes
with any value other than empty or `<REDACTED>`; and `tool.output` span events. The
capture settings turn that content off, so finding it means a flag leaked in from
somewhere. Hook payloads keep their content on purpose: the capture prompts are
synthetic.

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

| Data                                                  | Becomes                                     |
| ----------------------------------------------------- | ------------------------------------------- |
| `user.email`                                          | `user@example.com`                          |
| `user.account_uuid`, `organization.id`                | fixed fake UUIDs                            |
| `user.account_id`                                     | `user_01EXAMPLE`                            |
| `user.id`                                             | 64 zeros                                    |
| `user.groups`, `identity.source`, `host.name`         | fixed placeholders                          |
| those identity values anywhere else                   | the same placeholders                       |
| the domain of a `user.email`, anywhere                | `example.com`                               |
| home directory and its aliases, in every spelling     | `C:\Users\user` or `/home/user`             |
| `--workspace-root`, in every spelling                 | `<fake home>/projects/capture`              |
| username, hostname                                    | `user`, `example-host`                      |
| `--forbid` texts                                      | `redacted` (an email: `user@example.com`)   |
| Anthropic request IDs (`req_...`)                     | `req_EXAMPLE0001`, ... (same everywhere)    |
| plugin and marketplace names, anywhere                | `plugin-1`, `marketplace-1`, ...            |
| skill names that are not built in, anywhere           | `skill-1`, ...                              |
| MCP server names (except `spike-elicit`), anywhere    | `mcp-server-1`, ...                         |
| any other email, `X:\Users\<name>`, `/home/<name>`    | `user@example.com`, the fake home           |

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
- **As whole words**: paths, identity values, and plugin, marketplace, skill and MCP
  server names that are not plain words (`acme-tools`, `corp-db`).
- **Only where used as names**: names that are plain words (`git`, `memory`, `commit`),
  which are too common to replace everywhere. They are replaced as the whole value of a
  name field (`plugin.name`, `skill.name`, `mcp_server_name`, `skill`, ...), as the server
  in `mcp__<server>__<tool>` and as a segment of a `.claude/plugins/...` path, so
  `git commit` stays and `mcp__git__status` becomes `mcp__mcp-server-1__status`.

Plugin, marketplace, skill and MCP server names are collected first: from `plugin.name`,
`marketplace.name`, `skill.name` and `mcp_server_name`, from `.claude/plugins/...` paths
and from `mcp__<server>__<tool>` names.

Matches never cut into an ID: UUIDs, runs of 16 to 64 hex digits (trace, span and user
IDs) and Anthropic IDs (`toolu_...`, `msg_...`) stay whole unless they are exactly one of
the values. Dict keys are only matched as whole words.

Everything else stays as captured: session, trace and span IDs, model names, the
synthetic prompts and tool content, and every number.

Before writing anything, two checks run:

- **Structure.** Redaction must not change any dict key, OTLP attribute key, metric,
  span or event name, `hook_event_name`, ID field (`session_id`, `traceId`,
  `tool_use_id`, ...) or a value of a fixed vocabulary (`event.name`, `terminal.type`,
  `service.name`, `model`, ...), and must not make two keys of an object equal.
  Otherwise the sanitizer stops and names the label of the value that caused it (for
  example `username`), never the value.
- **Guard.** It looks for emails outside `example.com`; home directories of anyone but
  the fake user (including `C:\Users\user'Brien` or `C:\Users\user alice\...`); paths
  under the fake home other than `projects/capture/...`, `.claude/...`, `.claude.json`
  and `AppData/Local/Temp/...`, or with `..` in them (reported as
  `path outside workspace`); Anthropic request IDs that are not `req_EXAMPLE` fakes; and
  every value above, matched the same way it is replaced. It lists the kind and location
  of each finding, never the value.

If either finds something, the sanitizer writes nothing.

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
- Percent-encoded paths are redacted but skip the workspace allowlist.
- `--dest` trusts any `manifest.json` that has a `scenarios` list, and it does not
  guard against symlinks or junctions planted inside the destination. Point it only at
  `tests/fixtures`.
- Every email outside `example.com` becomes `user@example.com`, including public
  addresses such as `noreply@anthropic.com`.
- Error messages can echo the location (a key path) of a failed check on stderr; the
  fixtures are never written in that case.
