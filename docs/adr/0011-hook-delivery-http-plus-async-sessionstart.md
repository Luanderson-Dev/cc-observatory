---
status: accepted
date: 2026-10-13
decision-makers: Luanderson-Dev (maintainer)
---

# Hook delivery: http hooks plus an async command hook for SessionStart

## Context and problem statement

Hooks bring live session state to the app ([ADR 0002](0002-native-telemetry-plus-hooks.md)),
but they run inside Claude Code's loop. Claude Code waits for every `http` hook (`async`
exists only for `command` hooks) until its `timeout`, which defaults to 600 s; any
failure (connection refused, a non-2xx status, a plain-text body) is a non-blocking error
that the transcript shows as a notice; and `SessionStart` accepts only `command` and
`mcp_tool` hooks. The milestone 0 spike measured the options on Claude Code 2.1.296, in
isolated captures (fixtures in `tests/fixtures/`, observed contract in
[ADR 0012](0012-observed-telemetry-and-hook-contract.md)). How should Claude Code deliver
hook events to the app?

## Decision drivers

- Hooks never slow Claude Code down or change its decisions (CLAUDE.md).
- Claude Code keeps working with the stack down, with as few notices as possible.
- The app learns about every session, including the `SessionStart` that `http` hooks
  cannot carry.
- Events arrive in the order they happened: hook payloads carry no timestamp.
- Nothing to install or keep running on the host besides Claude Code and the stack.
- Linux, macOS and Windows.

## Considered options

- **http hooks only.** Fast (typically 4 to 8 ms of hook execution per event in the
  headless captures) and ordered, but no `SessionStart`: an `http` hook there is silently
  ignored. With the app down, every event writes a hook-error notice.
- **Async command hooks everywhere.** An exec-form `curl` with `async: true` on every
  event: silent when the app is down, `SessionStart` works, and launching each one
  (other than `SessionStart`) took 3 to 35 ms (typically 4 to 7). But every event starts a process, and Claude Code does
  not wait for it, so arrival order is not event order: in the capture, the `PostToolUse`
  of a background Agent call (status `async_launched`) arrived before its `PreToolUse`,
  and no timestamp in the payload can restore the order. Delivery is also lossy: that
  capture never received `SessionEnd`, and the
  [hooks reference](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
  says that in `-p` Claude Code kills async hooks still running at teardown.
- **http hooks plus an async command hook for SessionStart.** `http` wherever it is
  accepted, the async `curl` only where it is not. Ordered and fast, with `SessionStart`
  delivered on a best-effort basis; the notices with the app down remain.
- **A long-running local forwarder.** A host process, started by `ccobs`, that receives
  the hooks and buffers them for the app. It could hide outages, but it is one more
  process to install, start at login and keep alive on three operating systems, and when
  it is down the notices come back.
- **Transcript parsing.** Already rejected in ADR 0002: an internal format, mostly
  content.

## Decision outcome

Chosen option: "http hooks plus an async command hook for SessionStart", because it is
the only option that delivers the events the product needs, in order, without a host
process. Its cost, the notices while the stack is down, is visible, bounded and
mitigated.

- **http.** Every registered event that accepts `http` gets an `http` hook with
  `timeout: 1` (seconds). Healthy hook executions took 2 to 25 ms in the headless
  captures (typically 4 to 8 ms) and 4 to 93 ms in the interactive ones, so 1 s leaves
  room and bounds what a hung app costs each event: with the hook server asleep, each
  hook was cancelled at 1001 to 1007 ms and reported as `num_cancelled`. Fractional
  timeouts work (0.5 s cancelled at about 503 ms in an exploratory run outside the
  fixtures).
- **SessionStart.** It gets an async exec-form command hook that posts the same JSON
  body: `curl -s -m 2 -o <null device> -H "Content-Type: application/json"
  --data-binary @- <hook URL>`, with `NUL` as the null device on Windows and `/dev/null`
  elsewhere. The header is required because the hook endpoint ignores other content
  types (CLAUDE.md). The
  [hooks reference](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
  says Claude Code does not enforce `timeout` on a running async hook, so `-m 2` bounds
  the process (the hook still gets a short explicit `timeout`, like every hook). curl
  prints nothing, so nothing reaches Claude. Launching it took 16 to 72 ms at startup and
  10 ms after `/compact`. `Setup`, the other event without `http`, would use the same
  form if milestone 2 registers it.
- **SessionStart is best effort.** It can arrive after later events, or never (in `-p`,
  async hooks still running at teardown are killed; `curl` may be missing; the app may
  be down), so the app never depends on it: a session is created from whichever event
  arrives first, and `SessionStart` only adds its `source` (and `model` in interactive
  sessions) when it comes.
- **Responses.** The app answers every hook with an empty 2xx, invalid payloads
  included (CLAUDE.md).
- **Never** `onFailure`, `statusMessage` or the events whose registration changes Claude
  Code's behaviour (CLAUDE.md).
- **Which events.** Only those the product uses. This moves the open item of ADR 0002,
  the events registered, to milestones 2 and 4: the list is decided with the session
  state machine in milestone 2 and written by `ccobs setup` in milestone 4.
- **Stack down.** Each `http` hook fails at once (connection refused, 2 to 4 ms), the
  session completes normally, and each event counts a `num_non_blocking_error`. The async
  `curl` stays silent. In the first exploratory capture (2026-10-12, outside the
  fixtures), each failure also wrote a `hook_non_blocking_error` notice
  ("connect ECONNREFUSED") in the transcript; raw API bodies showed that these notices
  are not sent to the model; and in `-p` only the `SessionEnd` failure reached stderr.
- **Mitigations.**
    - Compose services with `restart: unless-stopped`: proposed for milestone 1, where
      its plan brings it to the maintainer for approval; not decided here.
    - `ccobs doctor` (milestone 4) probes the hook URL directly, and reads the
      `num_non_blocking_error` and `num_cancelled` counts of
      `claude_code.hook_execution_complete`. The counts travel through the collector, so
      they reveal an outage of the app alone, not of the whole stack, which the direct
      probe covers. They also cover only the events that emit `hook_execution_*`:
      `InstructionsLoaded`, `Notification`, `Elicitation`, `ElicitationResult`,
      `PreCompact`, `PostCompact`, `StopFailure` and `SessionEnd` never did in the
      fixtures, so failures on them, `Notification` included, are invisible to the
      counts.
    - `ccobs uninstall` removes the hooks for anyone who stops using the stack.

### Consequences

- Good, because Claude Code waits a few milliseconds per hook while the app is up, and
  at most 1 s per event if the app hangs.
- Good, because `http` events arrive in the order Claude Code fired them, one request
  each, without a process per event. The docs say parallel tool calls fire `PostToolUse`
  concurrently; the two-call batches in the fixtures ran one tool at a time, each call
  closing before the next began.
- Good, because `SessionStart` (with its `source`, and `model` in interactive sessions)
  usually reaches the app, and fails silently when the app is down.
- Bad, because with the stack down every `http` event writes a notice the user sees,
  until the stack is back or the hooks are removed; mitigated as above.
- Bad, because `SessionStart` is best effort: it arrived after `InstructionsLoaded` in
  the headless captures and after `PostCompact` on `/compact`, and it can be lost, so the
  app has to create sessions from any event.
- Bad, because the async hook needs `curl` on `PATH`. Windows 10 and 11 and macOS ship
  it, and most Linux distributions do; `ccobs doctor` checks it (milestone 4).
- Bad, because `ccobs doctor` cannot see failures on the events that emit no
  `hook_execution_*` telemetry; its direct probe of the hook URL covers the outage
  itself.
- Neutral, because hook payloads carry no timestamp: the app records when it receives
  each hook event, and joins the telemetry for exact times on `session.id`, `prompt.id`
  (the hooks' `prompt_id`), `tool_use_id` and `event.timestamp`.

### Confirmation

- `ccobs setup` tests (milestone 4) check the hooks it writes: `http` with `timeout: 1`
  on every registered event that accepts it, the async exec-form `curl` only on events
  without `http`, an explicit `timeout` everywhere, no `onFailure`, no `statusMessage`
  and none of the forbidden events.
- Hook endpoint tests (milestone 2) check the empty 2xx for every request, invalid
  payloads and a full queue included, and that a session is created from whichever event
  arrives first, with or without `SessionStart`.
- If milestone 1's plan approves the restart policy, the `compose.yaml` test
  (milestone 1) checks `restart: unless-stopped` on every service.
- `ccobs doctor` tests (milestone 4) check the direct probe of the hook URL, the `curl`
  check and the reading of `num_non_blocking_error` and `num_cancelled`.
- A test with the stack down (milestone 4) checks that a Claude Code session still
  completes.
- The fixtures `headless-sessionstart-http-ignored`, `headless-async-command-transport`,
  `hooks-endpoint-down` and `hooks-endpoint-timeout` in `tests/fixtures/` record the
  behaviour this decision relies on.

## More information

- Claude Code [hooks docs](https://code.claude.com/docs/en/hooks): HTTP response
  handling, timeouts, running hooks in the background, and `SessionStart`.
- [ADR 0012](0012-observed-telemetry-and-hook-contract.md) records the payloads, delays
  and ordering that the session state machine (milestone 2) builds on, and the capture
  conditions.
- The hook URL, its port and the exact `timeout` of the async hook are set in
  milestones 2 and 4.
- Revisit if Claude Code accepts `http` hooks on `SessionStart`, adds an async option to
  `http` hooks or a timestamp to hook payloads, or stops showing notices for failed
  non-blocking hooks.
