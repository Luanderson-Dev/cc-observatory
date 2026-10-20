# The app

A reference for operating the app, from v0.3.0 (its web UI from v0.4.0): what it does,
what its pages show, how to point Claude Code at it, what it stores and serves, its
settings, and what to check when something goes wrong. [stack.md](stack.md) covers the
compose stack it runs in, and [cli.md](cli.md) the `ccobs` commands; the decisions
behind the app are ADRs 0023 to 0035, and 0038 for its diagnostics route, in
[`adr/`](adr/README.md), and this page links to them instead of repeating their reasons.

Commands run from the repository root, where `compose.yaml` is, in bash (Git Bash on
Windows).

## What it does

The app is the `app` service of the compose stack, a FastAPI process
(`python -m ccobs.server`) built on your machine from the clone
([ADR 0030](adr/0030-the-app-container.md)). It:

- receives Claude Code's hooks on `POST /hooks` and answers each one at once with an
  empty 204, whatever the request holds
  ([ADR 0027](adr/0027-the-app-http-surface-and-hook-queue.md));
- parses each hook into metadata only, in a background writer, and drops the content
  before anything is stored
  ([ADR 0023](adr/0023-per-event-allowlist-of-hook-metadata.md));
- derives each session's state, its agents and subagents, and its blocks, the periods in
  which it waits on a person
  ([ADR 0024](adr/0024-session-agent-and-block-state-machine.md));
- keeps them in a SQLite database on a named volume
  ([ADR 0026](adr/0026-app-database-schema-migrations-and-retention.md));
- closes permission prompts denied with Esc, which fire no hook, from Claude Code's
  `tool_decision` events in Loki
  ([ADR 0029](adr/0029-closing-esc-denials-from-loki.md));
- serves the stored metadata as a read-only web UI at <http://127.0.0.1:4300>, which
  follows your sessions live ([The web UI](#the-web-ui),
  [ADR 0031](adr/0031-server-rendered-pages-with-htmx-and-vendored-assets.md),
  [ADR 0032](adr/0032-live-updates-change-hub-sse-pings-and-refetch.md)), and as a
  read-only JSON API under `/api/v1`
  ([ADR 0033](adr/0033-separate-writer-and-reader-engines-and-capped-reads.md));
- reads each session's tokens and cost from Claude Code's metrics in Prometheus, for the
  session page, and links each session to its logs, tokens and cost in Grafana
  ([ADR 0035](adr/0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md));
- exports its live state as Prometheus metrics on a separate listener that Prometheus
  scrapes ([ADR 0028](adr/0028-app-metrics-and-their-listener.md)).

`ccobs setup` writes the hooks into Claude Code's user settings for you
([cli.md](cli.md)); [Point Claude Code at the app](#point-claude-code-at-the-app) shows
what it writes, and how to connect one session without changing your settings.

`docker compose up -d` builds the app's image on the first start and whenever the source
changes, then starts it at <http://127.0.0.1:4300> (the port is `CCOBS_APP_PORT`).
`docker compose ps` shows it healthy once `GET /healthz` answers, which it checks every
30 seconds. After `git pull`, `docker compose up -d` rebuilds it; `docker compose pull`
does not update it, since no registry has the image.

## The web UI

Open <http://127.0.0.1:4300> in a browser (the port is `CCOBS_APP_PORT`). The UI is
server-rendered HTML with htmx and files that ship with the app: it loads nothing from
another origin, works offline and, like the rest of the app, has no login
([ADR 0031](adr/0031-server-rendered-pages-with-htmx-and-vendored-assets.md)). It is
read-only: it shows what the app stored and changes nothing, in the app or in Claude
Code. Until Claude Code sends hooks to the app, the overview shows how to connect it
(see [Point Claude Code at the app](#point-claude-code-at-the-app)).

### Pages

- **Overview** (`GET /`): a board of sessions in bays by state, each session a strip.
  "Waiting on you" comes first, the longest wait first, with its time since the oldest
  open block; then "Working" and "Idle", the latest activity first. "Ended in the last
  24 hours" and "Stale" are collapsed and show at most 20 sessions each, with a link to
  the rest. A strip shows the project name (the last part of the session's working
  directory, never the whole path), what the session does or waits on, its running
  subagents and its last error code. The page title counts the sessions waiting on you,
  such as `(2) cc-observatory`. With no session stored yet, the board shows the
  `ccobs setup` command to copy (with `--app-port` when you opened the app on a port
  other than 4300), points to `ccobs doctor` if nothing shows up, and keeps the hook
  settings for that port under "Connect one session without changing your settings",
  ready to save for `claude --settings`.
- **All sessions** (`GET /sessions`): every stored session as a strip, the latest
  activity first, 50 per page; "Older sessions" pages on with a cursor. It has no live
  updates; reload it.
- **Session** (`GET /sessions/{session_id}`, the link of each strip): a header with the
  project name, the working directory with your home directory shown as `~`, the state
  in words, how long the session has waited on you, its last error code, when it
  started, last had an event and ended (and why), and the full session ID. Under the
  header, **Open in Grafana** (see [Links to Grafana](#links-to-grafana)). Below it:
  - **Blocks**: the ones waiting on you now, then the closed ones, newest first, with
    how long each waited and how it closed (allowed, denied, answered, closed by later
    activity, ...);
  - **Agents**: the subagents as a tree, each under the agent that launched it, newest
    first, with their type, state and how long they ran;
  - **Tokens and cost**: the session's total cost and tokens, its tokens by type
    (input, output, cache read, cache creation) and its tokens and cost by model, from
    Prometheus (see [Tokens and cost](#tokens-and-cost));
  - **Hook timeline**: the session's hook events, newest first, with their metadata only
    (tool, MCP server, agent, notification type, error code, duration, sizes).

  The page shows every open block and running agent, the latest 50 closed blocks and
  stopped agents, and the latest 50 hook events; "Show older ..." appends the next 50
  in place. An unknown session ID gets a "Session not found" page (404).
- **Times** are elapsed times ticking in the browser; hover one for the moment in your
  time zone.
- **Theme**: system, light or dark, from the toggle in the header; the choice is kept in
  the browser's local storage for the app's origin, and nothing else is.
- **Errors**: when a read waits more than 5 seconds for one of the app's 4 database
  readers, the page or the part of it says "The database is busy", with a 503; reload it
  a few seconds later. "The app could not read its database" (also a 503) means a read
  failed: the app's log (`docker compose logs app`) says why. Neither page shows the
  error itself.

The pages fetch their parts from routes under `/ui/` and `/static/`. They serve the
pages and may change in any release; scripts should use the
[JSON API](#the-json-api) instead.

| Route                                                | Serves                                                               |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| `GET /ui/board`                                      | The overview's board alone, for the live updates.                    |
| `GET /ui/sessions/{session_id}/live`                 | A session's header, blocks and agents, for the live updates.         |
| `GET /ui/sessions/{session_id}/blocks`, `.../agents` | The next page of closed blocks or stopped agents ("Show older ..."). |
| `GET /ui/sessions/{session_id}/events`               | Older hook events (`before`), or the new ones (`after`).             |
| `GET /ui/sessions/{session_id}/usage`                | The "Tokens and cost" section.                                       |
| `GET /ui/stream`                                     | The live updates stream (below).                                     |
| `GET /static/...`                                    | The stylesheet, scripts, fonts and icon, with their licenses.        |

Static files are cached by the browser: the versioned htmx file for a year, everything
else revalidated with its `ETag` on each load, so an upgrade never leaves an old
stylesheet behind. Pages and their parts are never cached (`Cache-Control: no-store`).

### What the UI never shows

- **The full working directory.** The overview and All sessions show only the project
  name; the session page shows the path with your home directory as `~` (on a network
  path, without its server and share). Homes are recognised by the shape of the path,
  so your username can still show: a home in an unusual place (such as `/home2/<user>`
  or `/export/home/<user>`) shows as it is, and the project name of a session started
  in such a home directory itself is your username; a `/home/<group>/<user>` layout
  hides the group, not the user (`/home/staff/<user>/x` shows as `~/<user>/x`); and an
  MSYS or Cygwin home mounted outside `/home` or `Users` is not recognised. The JSON API still serves the full `cwd`, to localhost only (see
  [Privacy](#privacy)).
- **Content.** No prompt, tool input or output, assistant message or error text: the
  app never stores them, so no page can show them. Errors show as Claude Code's error
  codes.
- **Anything from another origin.** The pages load no script, style, font or image from
  elsewhere, and send no referrer when you follow a link out, to Grafana included.

### Links to Grafana

- The header's **Grafana** link opens the Claude Code overview dashboard for the app's
  host label in a new tab.
- A session's **Open in Grafana** has three links, each opening a new tab:
  - **Logs**: Explore on Loki with the session's Claude Code events,
    `{service_name="claude-code", host="<host>"} | session_id="<id>"`, up to the newest
    1000 events (raise the line limit in Explore's options for a longer session);
  - **Tokens and cost**: Explore on Prometheus, split in two: the session's tokens by
    type and its cost by model, per interval, as the dashboard draws them;
  - **Dashboard**: the Claude Code overview for the app's host label.

  Each covers the session from a minute before it started to two minutes after it
  ended (or after its last event, once stale); for a live session it runs up to now.
- Grafana answers only at `http://localhost:<port>`
  ([ADR 0017](adr/0017-grafana-access-and-dns-rebinding.md)), so every link starts
  there, on the port of `CCOBS_GRAFANA_PORT` (see `CCOBS_GRAFANA_URL` under
  [Configuration](#configuration)). Grafana opens with no login; its *Sign in* button
  has nothing to sign in to ([stack.md](stack.md#messages-that-are-not-faults)).
- Loki logs every query it runs, so the session ID of a Logs link you open ends up in
  Loki's log, like any query you type in Grafana.

### Tokens and cost

The session page's "Tokens and cost" section reads the session's totals from Claude
Code's own metrics in Prometheus
([ADR 0035](adr/0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md)),
the same totals as the dashboard
([ADR 0013](adr/0013-metrics-through-the-prometheus-otlp-receiver.md)): the total cost
in USD (four decimals under a dollar, two from a dollar up), the total tokens, the
tokens by type, and the tokens and cost by model. The cost is Claude Code's estimate,
not the amount you are billed.

The totals are exact within ADR 0013's limits, and can come out low in three cases:

- Claude Code sends delta temporality (set outside `ccobs setup`, which sets
  cumulative) and the collector restarted while the session ran;
- the clock that stamps the telemetry runs more than about a minute ahead of the app's
  (an ended session then misses its last export);
- the app missed the session's first hooks (it was down, or dropped them) while the
  collector received the metrics, so the session's start time in the app is later than
  its real start and the earliest usage falls outside the window.

The **Tokens and cost** link to Grafana shows the session's series over time, to compare.

- The section loads after the page, then refreshes every minute; it does not follow the
  live updates, since the hooks carry no token data. Claude Code exports its metrics
  about once a minute, so the numbers can lag up to a minute (plus up to 30 seconds of
  the app's cache).
- It covers the whole session, from 5 minutes before it started to 2 minutes after it
  ended (or after its last event, once stale); for a live session, up to now.
- The app sends three small queries to `CCOBS_PROMETHEUS_URL` per session and keeps the
  answer for 30 seconds while the session is live, 5 minutes once it is over; every tab
  showing the session shares it. Each query gives up after 3 seconds.
- **No usage recorded for this session yet**: Prometheus has no tokens or cost for the
  session (see [Troubleshooting](#tokens-and-cost-are-missing-or-unavailable)).
- **Prometheus is unavailable, so tokens and cost can't be shown right now**: the app
  could not reach Prometheus, or it did not answer in time. The rest of the page works
  as usual, and the section tries again a minute later.
- The queries filter on the session ID; Prometheus logs no query unless its query log is
  turned on, which the stack does not do.

### Live updates

The overview and the session page follow your sessions without a reload
([ADR 0032](adr/0032-live-updates-change-hub-sse-pings-and-refetch.md)):

- Each of these pages opens one server-sent events stream, `GET /ui/stream` (the session
  page adds `?session=<id>`). The stream carries no session data, only "these sessions
  changed", at most once every quarter of a second, and a keep-alive comment every 15
  seconds. The page then refetches its own parts: the overview its board, the session
  page its header, blocks and agents, and the new rows of its timeline. A change shows
  up once the writer commits it, normally within tens of milliseconds of the hook.
- Older pages you loaded with "Show older ..." stay as they are; new and newly closed
  rows appear above them, and a block or agent the page showed open or running stays
  on it once it closes (up to 8 of each that sit below the older pages). After 200 such
  rows in one visit, that list starts over at the latest page, with a fresh
  "Show older ..." button.
- On the overview, a collapsed bay you opened stays open, and the focus stays on the
  strip you were on.
- Each time the stream connects, the first time included, the page refetches once, so
  whatever changed while it was not listening shows up.
- If the stream drops (the app restarts, the laptop sleeps), the page says
  "Live updates paused. Reconnecting…" and tries again after 5 seconds, then 10, 20 and
  so on up to a minute, until it connects; then the message goes away (see
  [Troubleshooting](#live-updates-paused-reconnecting)).
- The app serves at most 16 streams at once, for all tabs and browsers together; the
  17th gets a 503, and that tab shows the paused message and retries.
- The stream is read-only, never reads the database, and answers only the app's own
  pages: another site, Grafana included, gets a 403 (see [Browser defences](#browser-defences)).

## Point Claude Code at the app

The hook URL is `http://127.0.0.1:4300/hooks`. Use `127.0.0.1`, not `localhost`: the
stack publishes its ports on IPv4 loopback only, and `localhost` can resolve to `::1`,
where nothing listens ([ADR 0018](adr/0018-ports-networks-and-exposure.md),
[ADR 0025](adr/0025-registered-hook-events.md)). If you move `CCOBS_APP_PORT`, change
the port in every URL below, and pass `--app-port` to `ccobs`.

`ccobs setup` writes these hooks, and the telemetry variables, into your user settings,
`~/.claude/settings.json`, next to the entries you already have
([cli.md](cli.md#ccobs-setup)): every Claude Code session then sends hooks, the ones
already open included. That is the usual way.

### Connect one session without changing your settings

To send the hooks of some sessions only, and leave your `~/.claude/settings.json` as
it is, put the hooks in a file of their own and pass it with `claude --settings`.
Claude Code merges hook entries across settings levels, so the hooks you already have
keep running next to these, in the sessions you start with the flag only
([CLI reference](https://code.claude.com/docs/en/cli-reference),
[hooks reference](https://code.claude.com/docs/en/hooks)).

1. Save this as `ccobs-hooks.json` somewhere outside any repository, for example in your
   home directory. It is the block that `ccobs setup` writes, generated from
   `ccobs.domain.hook_config.hook_settings`, and a test keeps this copy equal to it:

   <!-- hook-settings: hook_settings(DEFAULT_HOOK_URL, windows=False),
        kept equal by tests/test_docs_snippet.py -->

   ~~~json
   {
     "hooks": {
       "UserPromptSubmit": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PreToolUse": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PermissionRequest": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PostToolUse": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PostToolUseFailure": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "Notification": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "Elicitation": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "ElicitationResult": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "SubagentStart": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "SubagentStop": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "Stop": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "StopFailure": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PreCompact": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "PostCompact": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "SessionEnd": [
         {"hooks": [{"type": "http", "url": "http://127.0.0.1:4300/hooks", "timeout": 1}]}
       ],
       "SessionStart": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "curl",
               "args": [
                 "-s", "-m", "2", "-o", "/dev/null",
                 "-H", "Content-Type: application/json", "--data-binary", "@-",
                 "http://127.0.0.1:4300/hooks"
               ],
               "async": true,
               "timeout": 5
             }
           ]
         }
       ]
     }
   }
   ~~~

   **On Windows** (Claude Code running natively, not in WSL), change `"/dev/null"` to
   `"NUL"`, Windows's null device, and leave the rest as it is. Claude Code starts
   `curl` with these arguments directly, with no shell, so the null device must be the
   one the OS knows; `hook_settings(..., windows=True)` gives that variant, and the same
   test checks it. Windows 10 and 11 and macOS ship `curl`, and most Linux distributions
   do.

2. Start Claude Code with the file:

   ~~~bash
   claude --settings "$HOME/ccobs-hooks.json"
   ~~~

   Sessions started without the flag send no hooks to the app. Telemetry (tokens, cost,
   events) is set separately, in the `env` block of your user settings, which
   `ccobs setup` writes, or by hand as the README's "Run the observability stack"
   section shows; the Esc-denial close below needs those events.

3. Check that the app received the session: open <http://127.0.0.1:4300>, where it shows
   on the overview, or ask the API:

   ~~~bash
   curl -s 'http://127.0.0.1:4300/api/v1/sessions?limit=5'
   ~~~

### Which events, and why

The hooks register 15 events as `http` hooks with a 1-second timeout, plus
`SessionStart` as an async `curl` command, since `SessionStart` accepts no `http` hook:
the events the state machine needs, and no others. Every handler has a short explicit
timeout, and none has `onFailure`, `statusMessage` or a matcher. The events that would
change Claude Code's behaviour (`WorktreeCreate`, `WorktreeRemove`, `PreModelSwitch`,
`MessageDisplay`, `FileChanged`) are never registered, and the other unregistered
events, each with its reason (content, paths, or nothing the app tracks), are listed in
[ADR 0025](adr/0025-registered-hook-events.md).

### When the stack is down

Claude Code keeps working with the stack down, but every event sent by an `http` hook
then shows a hook-error notice in your transcript; the notices are not sent to the
model. The async `SessionStart` hook fails silently. While the app is up it answers
every hook with an empty 204, so no hook becomes a notice; if the app hangs, Claude Code
cancels each hook after 1 second. That trade-off, and how cc-observatory keeps it small,
is [ADR 0011](adr/0011-hook-delivery-http-plus-async-sessionstart.md). If you stop the
stack for a while, run `ccobs uninstall` (the hooks stop in open sessions at once, and
`ccobs setup` puts them back), or, with the hooks file, start Claude Code without
`--settings`, and nothing is posted.

## Sessions, agents and blocks

The app derives every state from the hooks it receives, in the order it receives them,
and times every change by its own clock, since hook payloads carry no timestamp. The
full rules, and what each one is confirmed by, are in
[ADR 0024](adr/0024-session-agent-and-block-state-machine.md).

| State          | When                                                                                                                                         |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `waiting_user` | At least one block is open.                                                                                                                  |
| `working`      | Otherwise, the main thread has a turn open (from `UserPromptSubmit` to `Stop` or `StopFailure`), a compaction runs, or a subagent runs.      |
| `idle`         | Otherwise: the turn ended and no subagent runs.                                                                                              |
| `ended`        | After `SessionEnd`. Not final: a later event other than `SessionStart` reopens the session.                                                  |
| `stale`        | Silent for `CCOBS_SESSION_STALE_AFTER` (12 hours by default) with no end; any later event other than `SessionStart` derives the state again. |

- A session is created by whichever of its events arrives first, since `SessionStart`
  can arrive late or never.
- **Agents** are keyed by `agent_id`: a subagent runs from `SubagentStart` to
  `SubagentStop`. An agent whose first event is `SubagentStop` is internal (Claude
  Code's own, such as compaction's) and never makes the session work. Background
  subagents keep the session `working` after the main turn's `Stop`.
- **Blocks** are permission prompts (`PermissionRequest`, or a `permission_prompt`
  notification when the request hook was missed) and MCP elicitations (`Elicitation`,
  or an `elicitation_dialog` notification when the `Elicitation` hook was missed).
  The `idle_prompt` notification is not a block: it sets `idle_notified_at_ms` on the
  session. A `StopFailure` is not a block either: it ends the turn and records its error
  code as `last_error_code`.
- **A block closes** when its tool call completes (`tool_completed`) or fails
  (`tool_failed`); when the scope makes progress (`superseded`: another tool call, a new
  prompt, `Stop`, `StopFailure`, `PreCompact`); on its `ElicitationResult`
  (`elicitation_accept`, `elicitation_decline`, `elicitation_cancel` or
  `elicitation_other`); when its agent stops (`agent_stopped`) or the session ends
  (`session_ended`); as `denied`, from Loki's reject (below), or from a
  `PermissionDenied` hook, which the shipped settings do not register; or as `stale`.
  Each closed block records whether a hook, the telemetry or the sweep closed it
  (`close_source`).
- **Stale** sessions have their open blocks closed and their running agents stopped, so
  the live metrics never count them. A running agent silent for the same time stops as
  `stale` too, in a session that is still active.

### Permissions denied with Esc

A permission dialog denied with Esc fires no hook, so the hooks alone would keep its
block open, and the session `waiting_user`, until the next progress event: a new prompt,
for instance. Claude Code's telemetry does see the denial, as a `tool_decision` event
with `decision=reject`. While a permission block is open, the app asks Loki every 2
seconds for the machine's rejects (one query with no session ID, for blocks open at
least 2 seconds and at most 15 minutes) and closes the matching block as `denied`, from
`telemetry`, at the reject's own time
([ADR 0029](adr/0029-closing-esc-denials-from-loki.md)). With no permission block open,
it sends no query.

- In the integration tests the app closed the block about 2.5 to 3 seconds after the
  reject's timestamp. Add Claude Code's log export interval (5 seconds by default) for
  the delay after you press Esc.
- **It needs telemetry.** Claude Code must export events to the stack
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp` and the endpoint, as in
  the README). Without them, with Loki down, or with `CCOBS_TELEMETRY_RECONCILE=false`,
  the app works as before: the block closes on the next progress event of its scope, or
  at the stale sweep, and its duration includes the time until then.
- A reject exported more than 15 minutes after its block opened is ignored.

## The JSON API

The app serves a read-only JSON API under `/api/v1`. `GET /api/v1/sessions` lists
sessions with the latest activity first, filtered by `status` (`working`,
`waiting_user`, `idle`, `ended`, `stale`) and limited by `limit` (default 50, from 1 to
500). `GET /api/v1/sessions/{session_id}` returns one session, bounded: every running
agent and open block, the latest 50 stopped agents and the latest 50 closed blocks, and
`agents_total` and `blocks_total`, how many agents and blocks the session has in all.
When a total is larger than its list, the rest are on the paged routes:

- `GET /api/v1/sessions/{session_id}/agents` returns the session's agents, running or
  stopped, in the order they were first seen (their start time; an internal agent, which
  never starts, by when it stopped), ties broken by `agent_id`. The detail lists them in
  the same order.
- `GET /api/v1/sessions/{session_id}/blocks` returns its blocks, open or closed, in
  `block_id` order, which is the order they opened in (a block's ID is the `seq` of the
  hook that opened it). The detail lists them in the same order.
- `GET /api/v1/sessions/{session_id}/events` returns its hook events in `seq` order.

Each takes `limit` (default 50, from 1 to 500) and pages by cursor, never by offset, so a
page never shifts when new hooks arrive. With no cursor, a route returns the latest
`limit` rows. `before` (agents: an `agent_id`; blocks: a `block_id`) and `before_seq`
(events) return the `limit` rows just before the cursor; `after_seq` (events only)
returns the `limit` events just after it, oldest first, which is how a client tails a
session: pass the last `seq` it has, then the last one of each page, until a page comes
back empty. Cursors are exclusive, so the row named by a cursor is never in its page, and
every page is oldest first, whatever the cursor: to walk back through a history, pass
the first row of each page as the next `before` until a page comes back empty.
`before_seq` and `after_seq` cannot be combined (422), and neither can a cursor outside 0
to 2^63 - 1 or an `agent_id` that is not one of the session's agents (422). Each agent
also carries `parent_agent_id`, the agent whose Agent tool call launched it, found from
that call's stored `PreToolUse`; it is `null` for an agent launched by the main thread,
and also when the launch is unknown (its `tool_use_id` is `null`, or the `PreToolUse`
was not stored).

Every field is a stored column of metadata kept under
[ADR 0023](adr/0023-per-event-allowlist-of-hook-metadata.md), or a count or a
`parent_agent_id` worked out from those columns, and nothing else: no prompt, tool input
or output, message or error text is ever served. Times are
integers of UTC milliseconds since the epoch. Sessions carry the `host` label and `cwd`,
which is personal data ([ADR 0006](adr/0006-privacy-metadata-only-by-default.md)): the
API serves it to localhost only, because every route sits behind the `Host` allowlist
(`localhost`, `127.0.0.1`, `[::1]`; any other host gets a 400) and the app sends no CORS
headers, so no web page can read it. Reads go to the database, not the writer's memory,
so a hook shows up once the writer commits its batch: normally within tens of
milliseconds of being received (a batch waits up to 50 ms), and longer only when writes
are backing off after an error. An unknown or malformed session ID gets a 404 with
`{"detail": "session not found"}`, and a validation error (422) names the parameter and
the rule without echoing the value sent. The OpenAPI schema and the interactive docs are
not served.

The API and the web UI read through query-only connections of their own, at most 4
reads at once, so readers never hold up the writer
([ADR 0033](adr/0033-separate-writer-and-reader-engines-and-capped-reads.md)); an API
read that waits more than 5 seconds for a free connection gets a 503 with
`{"detail": "database busy"}`, and can simply be retried.

From v0.5.0, `GET /api/v1/diagnostics`
([ADR 0038](adr/0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)) gives the
app's state for `ccobs doctor` and `ccobs status` ([cli.md](cli.md)), as counts only:
no session ID, path or content.

- `version` (the app's cc-observatory version), `host` (the host label) and
  `started_at_ms`, when the app started, since the counters below count from then;
- `sessions`: `by_state`, a count for each of `working`, `waiting_user`, `idle`,
  `ended` and `stale`, and `open_blocks`, the blocks not closed;
- `hooks`: `received`, the hooks parsed by event (`other` for events not registered),
  and `dropped`, the hooks dropped by reason (see [Metrics](#metrics)), every event and
  reason listed with 0 when none. Each `ccobs doctor` run adds one `invalid` drop: its
  probe of the hook route posts `{}`;
- `hook_failures`: Claude Code's `hook_execution_complete` events of the last hour, read
  from Loki: `available: true` with `window_s` (3600), `executions` (how many runs of a
  hook event's hooks), `non_blocking_error` and `cancelled` (the hooks that failed or
  timed out, Claude Code's own and your other hooks included); or, when Loki gives no
  usable answer within 2 seconds, `available: false` with `window_s` and a `reason`
  (`unreachable`, `timeout`, `status` or `shape`). The route still answers 200 then.

Its database read goes through the same readers as the rest of the API, so it can get
the 503 `database busy`; it then sends no query to Loki.

~~~bash
curl -s 'http://127.0.0.1:4300/api/v1/sessions?status=waiting_user'
curl -s 'http://127.0.0.1:4300/api/v1/sessions/<session_id>'
curl -s 'http://127.0.0.1:4300/api/v1/sessions/<session_id>/events?limit=100'
curl -s 'http://127.0.0.1:4300/api/v1/sessions/<session_id>/events?after_seq=<seq>'
curl -s 'http://127.0.0.1:4300/api/v1/sessions/<session_id>/blocks?before=<block_id>'
curl -s 'http://127.0.0.1:4300/api/v1/sessions/<session_id>/agents?before=<agent_id>'
curl -s 'http://127.0.0.1:4300/api/v1/diagnostics'
curl -s 'http://127.0.0.1:4300/healthz'      # {"status":"ok"}
~~~

**Changed in v0.4.0.** In v0.3.0 the session detail held every agent and block the
session ever had, and `/events` returned only the latest `limit` events. From v0.4.0:

- the detail is bounded as above and adds `agents_total` and `blocks_total`; a client
  that read every agent or block from it now pages through `/agents` and `/blocks` when
  a total is larger than its list;
- `/agents` and `/blocks` are new, and `/events` takes the `before_seq` and `after_seq`
  cursors;
- each agent carries `parent_agent_id`;
- a read can get the 503 `database busy` above;
- cross-site browser requests get a 403 (see [Browser defences](#browser-defences)).

## Metrics

The app exports its live state in the Prometheus format on a listener of its own, port
9464 inside the stack, which compose never publishes. Prometheus scrapes it every 15
seconds as the job `ccobs-app`, so the series are in Grafana's Prometheus datasource
(Explore). The debug override publishes the listener on `127.0.0.1:4301`
(`CCOBS_APP_METRICS_PORT`) for debugging. Names, labels and the reasons behind them are
in [ADR 0028](adr/0028-app-metrics-and-their-listener.md); the Loki counter is in
[ADR 0029](adr/0029-closing-esc-denials-from-loki.md), the live stream series in
[ADR 0032](adr/0032-live-updates-change-hub-sse-pings-and-refetch.md), the Prometheus
counter in
[ADR 0035](adr/0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md).

| Series                              | Type      | Labels            | Meaning                                                                              |
| ----------------------------------- | --------- | ----------------- | ------------------------------------------------------------------------------------ |
| `ccobs_sessions`                    | gauge     | `host`, `state`   | Sessions that have not ended, by state (`working`, `waiting_user`, `idle`, `stale`). |
| `ccobs_sessions_ended_total`        | counter   | `host`            | Sessions that moved to ended since the app started.                                  |
| `ccobs_blocks_open`                 | gauge     | `host`, `kind`    | Open blocks, by kind (`permission`, `elicitation`).                                  |
| `ccobs_agents_running`              | gauge     | `host`            | Running agents and subagents, in sessions neither ended nor stale.                   |
| `ccobs_block_duration_seconds`      | histogram | `host`, `kind`    | How long each closed block stayed open, by kind.                                     |
| `ccobs_hook_events_processed_total` | counter   | `host`            | Hook events stored in the database.                                                  |
| `ccobs_hook_events_received_total`  | counter   | `host`, `event`   | Hook payloads parsed, by event (`other` for events not registered).                  |
| `ccobs_hook_events_dropped_total`   | counter   | `host`, `reason`  | Hooks dropped instead of stored, by reason.                                          |
| `ccobs_hook_queue_depth`            | gauge     | `host`            | Hooks received and not yet handled by the writer.                                    |
| `ccobs_hook_queue_bytes`            | gauge     | `host`            | Body bytes of those hooks.                                                           |
| `ccobs_build_info`                  | gauge     | `host`, `version` | Always 1; `version` is the running cc-observatory version.                           |
| `ccobs_database_wal`                | gauge     | `host`            | 1 if the database runs in WAL mode, 0 if its filesystem refused WAL.                 |
| `ccobs_loki_queries_total`          | counter   | `host`, `result`  | Queries sent to Loki (Esc denials, diagnostics), by result (`ok`, `error`).          |
| `ccobs_prometheus_queries_total`    | counter   | `host`, `result`  | Queries sent to Prometheus for tokens and cost, by result (see below).               |
| `ccobs_ui_streams`                  | gauge     | `host`            | Live update streams open to the web UI.                                              |
| `ccobs_ui_streams_refused_total`    | counter   | `host`            | Live update streams refused because 16 were already open.                            |

- Every label takes its values from a closed list, and every value is exported, with 0
  when empty: 71 series per host, however many sessions there are. No series has a
  session ID.
- The Prometheus query results are `ok`, `error` (no connection, a non-2xx status),
  `timeout` (no answer within 3 seconds) and `invalid` (an answer too large, not JSON,
  or with values that are not finite, non-negative numbers). Totals from the app's
  cache send no query and count nothing.
- The drop reasons are `full` (the queue is full), `too_large` (a body over
  `CCOBS_HOOK_BODY_MAX_BYTES`), `bad_content_type`, `bad_host`, `bad_request` (not a
  hook: another method, a malformed length, a client gone mid-body), `invalid` (a body
  that is not a hook payload) and `internal` (an error on the app's side, including
  hooks still queued when a stop ran out of time).
- The counters live in memory and start from 0 when the app restarts; `rate` and
  `increase` handle that as a counter reset.

## Data and retention

The database is `ccobs.db`, with its WAL files, in the `app-data` volume
(`cc-observatory_app-data`), mounted at `/var/lib/ccobs`. The app runs its schema
migrations at start-up, before it accepts a request, so an upgrade needs no step from
you; going back to an older app image after a migration is not supported
([ADR 0026](adr/0026-app-database-schema-migrations-and-retention.md)).
`docker compose down -v` deletes the volume with the others.

| Setting                     | Default | Range                                         | Effect                                                                                                    |
| --------------------------- | ------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `CCOBS_APP_RETENTION`       | `90d`   | `1d` to `100y`, never below the stale setting | Deletes ended and stale sessions, with their agents, blocks and events, this long after their last event. |
| `CCOBS_SESSION_STALE_AFTER` | `12h`   | at least `1h`                                 | Marks a silent session, or a silent running agent, stale.                                                 |

- Both take Prometheus's units (`y`, `w`, `d`, `h`, `m`, `s`, `ms`). An invalid value,
  or a retention shorter than the stale setting, stops the app at start-up with a
  message that names the variable.
- The sweep checks for stale sessions and agents every 60 seconds. Retention runs about
  60 seconds after the app starts, so it catches up after downtime, then every hour. A
  session that has not ended or gone stale is never deleted.
- Both intervals are fixed in the code; they have no setting.

## Privacy

- **Stored:** metadata only, field by field as
  [ADR 0023](adr/0023-per-event-allowlist-of-hook-metadata.md) lists it: session,
  prompt, agent, tool call and elicitation IDs, event names, tool and MCP server names,
  agent types (your own agents by name), enumerated values (permission mode, effort,
  notification type, end reason, error code), durations, sizes in bytes, the prompt's
  length in characters, the model, the working directory (`cwd`) and the `host` label.
- **Never stored:** prompts, tool inputs and outputs, assistant messages, error text,
  task subjects, compaction summaries, and every path other than `cwd`
  (`transcript_path` and the others). A field Claude Code adds later is dropped until an
  ADR lists it. v0.5.0 stores metadata only: the opt-in to keep content
  ([ADR 0006](adr/0006-privacy-metadata-only-by-default.md)) is deferred
  ([ADR 0040](adr/0040-defer-the-capture-content-opt-in.md)).
- **`cwd` is personal data**: it contains your OS username. On Amazon Bedrock the model
  can be an inference-profile ARN that contains your AWS account ID. Both are in the
  database file, which a backup of the volume copies, and the API serves them to
  localhost only. The app's logs never hold a hook body, `cwd` or another path, or a
  session ID, at any log level.
- **The web UI** never puts the full `cwd` in a page: it shows the project name, or the
  path with your home directory as `~` (see
  [What the UI never shows](#what-the-ui-never-shows)). Pages are sent with
  `Cache-Control: no-store`, so the browser keeps no copy of them on disk.
- The app's only query to Loki holds no session or call ID, so nothing personal reaches
  Loki's query log. Its queries to Prometheus hold a session ID, and Prometheus logs no
  query in the stack.

## Browser defences

Any web page you open can make your browser send requests to localhost, so the app,
which has no login, defends itself
([ADR 0027](adr/0027-the-app-http-surface-and-hook-queue.md), and from v0.4.0
[ADR 0034](adr/0034-browser-hardening-csp-fetch-metadata-and-response-headers.md)):

- **`Host` allowlist.** Every route answers only a `Host` header of `localhost`,
  `127.0.0.1` or `[::1]`, with or without a port. A page that uses DNS rebinding sends
  its own host name and gets a 400 on the pages and the API (the hook route gives its
  usual empty 204 and drops the request).
- **JSON only on the hook route.** A request whose `Content-Type` is not
  `application/json` is dropped, still with an empty 204. A cross-site page can send
  JSON only after a CORS preflight, which fails.
- **No CORS.** No response carries an `Access-Control-Allow-*` header.
- **Fetch Metadata** (ADR 0034). A browser request whose `Sec-Fetch-Site` is
  `cross-site` or `same-site` (any other `localhost` port, Grafana's included) gets a
  403 with the body `forbidden`, unless it is a top-level `GET` navigation, such as a
  link or a typed URL; a websocket gets close code 1008. Requests without
  `Sec-Fetch-Site` (curl, Prometheus) and from the app's own pages pass. So another
  page cannot load the app's responses as images or scripts, fetch from it, frame it
  or hold its live updates stream, while a link to the app still opens it.
- **Response headers** (ADR 0034). Every response that the app's routes and guards
  produce, but the hook route's, carries a strict `Content-Security-Policy` (only the
  app's own scripts, styles, fonts and images, plus `data:` images, nothing from another
  origin, no inline or eval'd code, no framing, no form submission),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Resource-Policy: same-origin` and
  `Cross-Origin-Opener-Policy: same-origin`, plus `Cache-Control: no-store` outside
  `/static/`. A header a route sets itself is kept. An unhandled 500 from Starlette's
  error middleware carries none of them; its body is generic.
- **Read-only.** Every route but the hook route serves only `GET`; any other method gets
  a 405.
- **The hook route is outside all of this.** It answers its empty 204 with no added
  header, whatever `Sec-Fetch-*` headers say (Claude Code sends none).
- **Nothing extra.** No `/docs`, `/redoc` or `/openapi.json`, no access log, no
  `X-Forwarded-*` trust and no `Server` header on the app's port.
- **Metrics on an unpublished port.** A browser cannot reach a container port that
  compose does not publish. The debug override, and on Linux a guessed container address
  on the bridge network, let a rebinding page read the counts, which hold no content,
  paths or IDs (ADR 0028).

Any local process can still post fake hooks: see [SECURITY.md](../SECURITY.md).

## Configuration

Compose passes these variables to the app, each with the default below, so `.env` is
optional ([`.env.example`](../.env.example) lists them with the same values). Change one
in `.env` and run `docker compose up -d`.

| Variable                     | Default                  | Effect                                                                                                             |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `CCOBS_APP_PORT`             | `4300`                   | Host port of the app: the web UI, the hook URL, the API and `/healthz`.                                            |
| `CCOBS_APP_METRICS_PORT`     | `4301`                   | Host port of the metrics listener; debug override only.                                                            |
| `CCOBS_HOST_LABEL`           | `local`                  | The `host` label on the app's rows and series, the collector's value too ([ADR 0022](adr/0022-the-host-label.md)). |
| `CCOBS_APP_RETENTION`        | `90d`                    | How long the app keeps sessions after their last event (see above).                                                |
| `CCOBS_SESSION_STALE_AFTER`  | `12h`                    | Silence before a session or agent is stale (see above).                                                            |
| `CCOBS_TELEMETRY_RECONCILE`  | `true`                   | Read `tool_decision` rejects from Loki to close Esc denials: `true` or `false`.                                    |
| `CCOBS_LOKI_URL`             | `http://loki:3100`       | Loki's HTTP API as the app reaches it: `http` or `https`, with no user, query or fragment.                         |
| `CCOBS_PROMETHEUS_URL`       | `http://prometheus:9090` | Prometheus's HTTP API as the app reaches it, for tokens and cost: the same rules as `CCOBS_LOKI_URL`.              |
| `CCOBS_LOG_LEVEL`            | `info`                   | `debug`, `info`, `warning` or `error`.                                                                             |
| `CCOBS_HOOK_QUEUE_MAX_ITEMS` | `10000`                  | Advanced: most hooks the queue holds at once, 1 to 1000000.                                                        |
| `CCOBS_HOOK_QUEUE_MAX_BYTES` | `67108864`               | Advanced: most body bytes the queue holds at once (64 MiB), 1 MiB to 1 GiB.                                        |
| `CCOBS_HOOK_BODY_MAX_BYTES`  | `8388608`                | Advanced: largest hook body read (8 MiB), 64 KiB to 64 MiB and at most the queue's bytes.                          |

Compose also passes `CCOBS_GRAFANA_URL`, Grafana as the browser opens it, for the UI's
links. It is not offered in `.env`: compose sets it to Grafana's root URL,
`http://localhost:<CCOBS_GRAFANA_PORT>`, the only URL Grafana does not redirect, so it
follows `CCOBS_GRAFANA_PORT`. Outside compose it defaults to `http://localhost:4330`,
and the app accepts only `http` or `https` on the host name `localhost` (not
`127.0.0.1`, which Grafana redirects), with an optional port and path and no user,
query or fragment.

The image sets `CCOBS_LISTEN_HOST` and relies on the code's defaults for three more,
which compose never passes, so that the port mappings, the healthcheck, the scrape
target and the volume always match. They matter only when you run the app outside
compose:

| Variable             | In the container | Default in the code | Effect                                                      |
| -------------------- | ---------------- | ------------------- | ----------------------------------------------------------- |
| `CCOBS_LISTEN_HOST`  | `0.0.0.0`        | `127.0.0.1`         | The address the app and its metrics listen on.              |
| `CCOBS_PORT`         | `4300`           | `4300`              | The app's port.                                             |
| `CCOBS_METRICS_PORT` | `9464`           | `9464`              | The metrics listener's port; must differ from `CCOBS_PORT`. |
| `CCOBS_DATA_DIR`     | `/var/lib/ccobs` | `/var/lib/ccobs`    | The directory of `ccobs.db`.                                |

The app reads no `.env` file itself, and an invalid value stops it with exit code 2 and
one line per bad variable. For development, run it from the clone outside compose, on
another data directory, without the Loki reconcile (Loki's compose name does not resolve
on the host), after stopping the stack's app so that port 4300 is free:

~~~bash
docker compose stop app
CCOBS_DATA_DIR=.ccobs-dev CCOBS_TELEMETRY_RECONCILE=false uv run python -m ccobs.server
~~~

It serves <http://127.0.0.1:4300> and its metrics on `127.0.0.1:9464`; Ctrl+C stops it,
and `docker compose start app` brings the stack's app back. Git ignores the database
files it creates in `.ccobs-dev`. The session page's tokens and cost then say Prometheus
is unavailable, since Prometheus's compose name does not resolve on the host either:
start the stack with the debug override and add
`CCOBS_PROMETHEUS_URL=http://127.0.0.1:4390` to see them. The links to Grafana work as
they are, while Grafana runs on its default port.

## Troubleshooting

### The app is unhealthy or keeps restarting

`docker compose ps` shows its health, and `docker compose logs app` why it stopped:

- `invalid cc-observatory settings:`, followed by one line per bad variable: fix the
  value in `.env` and run `docker compose up -d`;
- a migration error at start-up: the database was not changed; report it with the log;
- `port is already allocated` from `docker compose up`: another program holds 4300 (see
  [A port is already in use](stack.md#a-port-is-already-in-use)).

`curl -s http://127.0.0.1:4300/healthz` answers `{"status":"ok"}` once the app serves
requests.

### The overview stays empty

The overview shows how to connect Claude Code until the app stores its first session.
Run `ccobs doctor` ([cli.md](cli.md#ccobs-doctor)): it checks that setup's hooks are in
your user settings for the app's port, that no policy turns them off, and that the app
answers. With the hooks file instead, check that you started Claude Code with
`claude --settings` and that file (see
[Point Claude Code at the app](#point-claude-code-at-the-app)). A session shows up with
its first hook, normally its first prompt; `SessionStart` alone can arrive late. If
Claude Code shows hook errors, see below.

### Claude Code shows hook errors

A hook-error notice means that a hook could not reach the app in time, or got an
answer other than an empty 2xx. Check:

1. that the app is running and healthy (above); while it starts, hooks fail;
2. that the hook URL is `http://127.0.0.1:<CCOBS_APP_PORT>/hooks`, with `127.0.0.1`,
   not `localhost`: `ccobs doctor` compares the hooks in your user settings with the
   app's port (`hooks`) and posts to the hook URL (`hook-endpoint`).

To stop the notices while the stack is down, run `ccobs uninstall`, or, with the hooks
file, start Claude Code without `--settings`.

### A session stays `working` after you interrupted it with Esc

Known limitation ([ADR 0024](adr/0024-session-agent-and-block-state-machine.md)): when
you interrupt a response with Esc, Claude Code fires no `Stop`, according to the hooks
reference (not seen in the fixtures), and no other hook says the turn ended. The
session shows `working` until your next prompt, `/exit`, a manual `/compact`, or the
stale sweep.

### A session stays `waiting_user` after you denied a prompt with Esc

The Esc-denial close needs Claude Code's events in Loki. Check that telemetry is on (see
[No data in the dashboard](stack.md#no-data-in-the-dashboard)), that
`CCOBS_TELEMETRY_RECONCILE` is `true`, and `ccobs_loki_queries_total{result="error"}`
in Grafana's Explore: a rising count means the app cannot reach Loki
(`docker compose ps loki`). Without telemetry, the block closes at the next prompt or
tool call of that session.

### Tokens and cost are missing or unavailable

- **"Prometheus is unavailable"**: check `docker compose ps prometheus` (it should be
  `healthy`) and `ccobs_prometheus_queries_total` in Grafana's Explore: `error` means
  the app cannot reach `CCOBS_PROMETHEUS_URL` or got an error status, `timeout` that
  Prometheus did not answer within 3 seconds (a busy or starting Prometheus), and
  `invalid` an answer the app could not read. The app's log says
  `prometheus query failed (<reason>)`, with no URL or session ID.
- **"No usage recorded for this session yet"**: Prometheus has nothing for the session.
  A session that just started shows this until Claude Code's first export, about a
  minute in. Otherwise Claude Code's metrics are not reaching the stack (see
  [No data in the dashboard](stack.md#no-data-in-the-dashboard)), or they carry no
  session ID because `OTEL_METRICS_INCLUDE_SESSION_ID=false` is set somewhere, or the
  session is older than Prometheus's retention (`CCOBS_PROMETHEUS_RETENTION`, 90 days
  by default). Running the app outside compose, set `CCOBS_PROMETHEUS_URL` to a
  Prometheus it can reach (the debug override publishes it on `127.0.0.1:4390`).

### "Live updates paused. Reconnecting…"

The page lost its live updates stream and retries on its own, from every 5 seconds up to
once a minute; what it shows may be out of date until the message goes away. Reload the
page to retry at once.

- **The app is down or restarting**: `docker compose ps app` should say `healthy`; see
  [The app is unhealthy](#the-app-is-unhealthy-or-keeps-restarting).
- **Too many streams**: the app serves 16 at once, one per overview or session tab, for
  every browser together. `ccobs_ui_streams` shows how many are open and
  `ccobs_ui_streams_refused_total` how many were refused; the log says
  `live stream refused`. Close tabs you do not need.
- **Many tabs in one browser**: browsers open at most about 6 connections to one host
  over HTTP/1.1, and each tab with live updates holds one. With six or more such tabs,
  other requests to the app (pages, refetches) wait for a free connection and the pages
  look frozen. Close some tabs, or open the extra ones as `127.0.0.1:4300` and
  `localhost:4300`, which browsers count as two hosts.
- **A proxy or extension buffering responses**: the stream must reach the page as it is
  written. The app needs no proxy; open it directly at `http://127.0.0.1:4300`.

### A page says "The database is busy"

A read waited more than 5 seconds for one of the app's 4 database readers, so the page,
or a part of it, gave up with a 503. The writer is not affected and no hook is lost;
reload the page a few seconds later. The app's log says
`UI read refused: database busy`. If it keeps happening, check whether
`ccobs_database_wal` is 0 (below): without WAL, reads wait for the writer. "The app
could not read its database" is a failed read, not a busy one: `docker compose logs app`
names the error.

### WAL is off

`ccobs_database_wal` is 0 and the log says `SQLite journal mode is ... not 'wal'`: the
filesystem under the data directory refused WAL, so reads block the writer. The named
volume of the stack supports it; this happens when the data directory is on a network
or shared filesystem. Keep it on a local disk.

### Checking for dropped hooks

In Grafana's Explore, with the Prometheus datasource, query
`increase(ccobs_hook_events_dropped_total[1h]) > 0`; or, with the debug override:

~~~bash
curl -s http://127.0.0.1:4301/metrics | grep -E '^ccobs_hook_(events_dropped_total|queue)'
~~~

`full` means the writer fell behind (see `ccobs_hook_queue_depth`), `too_large` a body
over `CCOBS_HOOK_BODY_MAX_BYTES`, `invalid` a body that is not a hook payload, and
`internal` an error in the app, which its log names by type. Dropped hooks still got an
empty 204, so Claude Code showed nothing.

### Reading the app's logs

~~~bash
docker compose logs app            # add -f to follow, --since 10m to narrow
~~~

Set `CCOBS_LOG_LEVEL=debug` in `.env` and run `docker compose up -d` for one line per
dropped hook, with its reason. The logs hold counts, reasons and exception types, never
a hook body, a path or a session ID.

## Limits and known gaps

Each item is marked as in the ADRs: verified by the fixtures, the tests or a
measurement, or unverified.

- **Hook latency**: inside the app the hook route costs about 0.3 ms at p99 over 500
  real payloads (unit tests); from the client (measured: client-side through Docker
  Desktop 29.8 port forwarding, 131 requests, p95 23.6–35 ms; the integration test
  asserts under 50 ms after a warm-up).
- **Esc interrupts** (unverified): a response interrupted with Esc leaves the session
  `working`, as described above.
- **Esc denials** (verified against the real Loki in the integration tests;
  unverified: that every Esc denial produces a reject, and the delay between the Esc and
  its export): closed from telemetry, within 15 minutes of the block opening, and only
  with telemetry on.
- **Requests denied in `-p`** (verified in the fixtures): recorded as a short block
  closed by the next event of its scope, not as a denial.
- **Auto mode denials** (unverified: `PermissionDenied` never fired in the capture and
  is not registered): the block stays open until the next progress event or a reject in
  Loki.
- **Resumed, cleared and forked sessions, automatic compaction, nested subagents and
  concurrent background launches** (unverified): handled by the general rules, never
  seen in a capture.
- **Block times** (by design): receive times, late by the hook's delivery delay.
- **The 12-hour stale threshold** (by design): a killed session, or an agent whose
  `SubagentStop` was lost, shows as live for up to 12 hours, and an agent silent that
  long is stopped even if it was alive.
- **Losses** (by design): a full queue, a crash or a stop loses the hooks still in
  memory; each is counted in `ccobs_hook_events_dropped_total`. A stop drains the queue
  for up to 5 seconds first.
- **Counters** reset when the app restarts (by design).
- **Fake hooks** (by design): any local process can post them; there is no
  authentication ([SECURITY.md](../SECURITY.md)).
- **Live updates** (by design): at most 16 streams at once; each change costs every open
  overview tab one read and every session tab two, coalesced over a quarter of a second.
  A strip that changes bay moves without an animation. A browser holds about 6
  connections to one host, so six or more live tabs in one browser stall each other.
- **Tokens and cost** (by design,
  [ADR 0035](adr/0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md)):
  per session only, on the session page; totals across sessions are on Grafana's
  dashboard. They lag Claude Code's export by up to a minute, and can come out low in
  the three cases under [Tokens and cost](#tokens-and-cost).
- **Home directories** (by design): recognised by the shape of the path, so a home in
  an unusual place shows as it is (see
  [What the UI never shows](#what-the-ui-never-shows)).
- **Memory** (measured on Docker Desktop 29.8, WSL 2, in v0.3.0, before the web UI):
  63.8 MiB idle, 70.1 MiB after about 2,750 hooks, against a 256m limit
  ([stack.md](stack.md#memory-and-disk)).
