---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# Alert rules: defaults, one key per rule and subject, episodes and a cooldown

## Context and problem statement

[ADR 0007](0007-alerts-browser-and-discord.md) chose browser and Discord alerts on three
conditions (a session waiting on a person, repeated API failures, cost above a limit),
with deduplication and a minimum interval, and left to milestone 5 the default
thresholds, what counts as "repeated" and how deduplication works.
[ADR 0024](0024-session-agent-and-block-state-machine.md) defines `waiting_user` and the
blocks it rests on, and records `StopFailure` as an alert, not a block. Which rules does
v0.6.0 evaluate, with which defaults and bounds, and when does an alert fire, clear and
fire again?

## Decision drivers

- No alert storms (ADR 0007): one condition notifies once, and a flapping one does not
  notify again and again.
- Alerts reflect the state the UI shows, within seconds, with no extra service.
- The rules stay pure and replayable with a fake clock: `ccobs.domain` has no I/O and
  never reads the clock (CLAUDE.md, ADR 0024).
- Settings typed on a screen are bounded, and their errors never echo what was typed.
- No double counting: one API failure is one failure.

## Considered options

- **Deduplication by (rule, subject), with episodes and one global cooldown.** Fire
  once per episode of a condition, clear when it ends, and fire a new episode only after
  a minimum interval since the key's last fire.
- **Deduplication by rule only.** Simpler, but a second session waiting would be hidden
  by the first one's alert.
- **A fixed re-notify interval while a condition holds** (every N minutes). Reminds a
  user who missed the first alert, but turns a long wait into a stream of messages.
- **Per-rule cooldowns.** More control, one more setting per rule; nothing yet shows the
  need.

## Decision outcome

Chosen option: "Deduplication by (rule, subject), with episodes and one global
cooldown", because it notifies each session's condition once, never hides one session
behind another, and keeps the settings to one interval. The rules live in
`ccobs.domain.alerts`.

**Rules and defaults** (the maintainer's decisions, 2026-10-20):

| Rule                | Condition                                                       | Default            | Bounds                         |
| ------------------- | --------------------------------------------------------------- | ------------------ | ------------------------------ |
| `waiting_user`      | a session `waiting_user` for at least the threshold             | on, 30 s           | 5 s to 24 h                    |
| `repeated_failures` | N `StopFailure` events in one session within a window W         | on, 3 in 600 s     | N 2 to 100, W 60 s to 24 h     |
| `session_cost`      | a session's estimated cost at or above a limit                  | off, US$ 10        | US$ 0.01 to 100000             |
| `daily_cost`        | the estimated cost of the last 24 hours (rolling, no time zone) | off, US$ 50        | US$ 0.01 to 100000             |

The cooldown, the minimum interval between two alerts with the same key, is 300 s by
default, from 0 to 24 h. The global 300 s cooldown, and the choice of both channels for
every rule, were set by the main context of milestone 5 on the architect's
recommendation, not by the maintainer; the maintainer may still change them before this
ADR is accepted. A disabled rule keeps a valid value, so turning it on never meets a bad
one. Seconds and failures take a whole number (an `int`) only, and dollar limits a whole
or finite decimal number; a `bool` or any other type is invalid. A value out of bounds,
or of the wrong type, is an error that names the field and the allowed range, never the
value, and never an exception.

**Key.** An alert's key is `(rule, subject)`. The subject is the `session_id`, or `day`
for `daily_cost`.

**Episodes, fire, clear and cooldown.** An episode is a period in which a rule's
condition holds for a subject. For `waiting_user`, it runs while the session is
`waiting_user` and its wait counts from the opening of its oldest open block.

- An episode fires once, as soon as its value reaches the threshold (at the threshold,
  not only above it) and the cooldown allows it; it never fires again while it lasts.
- It clears when it ends, only if it fired. A session that leaves memory (ended and
  dropped by the writer) or turns `ended` or `stale` ends its episode.
- A new episode fires only if at least the cooldown has passed since the key's last
  fire. If the cooldown ends while the episode still holds, it fires then.
- A clock that goes backwards never fires an episode twice and never raises; a clear is
  never earlier than its fire.
- A disabled rule fires nothing and clears what it had fired.
- The evaluator keeps a key only while it fires or its cooldown runs, so its memory is
  bounded by the sessions the writer holds plus the keys that fired in the last
  cooldown.
- Effects are `AlertFired(rule, subject, fired_at_ms, value, threshold)`, with value and
  threshold in the rule's unit (seconds, failures or US dollars), and
  `AlertCleared(rule, subject, cleared_at_ms)`, sorted by key.

**Evaluation.** The rules are pure functions of the current time, the configuration, the
evaluator's state and the sessions. The writer thread, which already holds every open
session in memory, evaluates them every 5 s (stage 6 of milestone 5). Two episodes of
one session less than 5 s apart can therefore count as one.

**"Repeated" failures** (this narrows ADR 0007). The rule counts `StopFailure` hook
events only, of every `error` code, in one session within the window. It clears on a
successful `Stop` in that session, or when the count within the window falls under N.
Stage 2 of milestone 5 implements this clear rule; it must match this ADR before the
ADR is accepted.
ADR 0007's "repeated `StopFailure` hook events or `claude_code.api_error` events" is
narrowed to `StopFailure` in v0.6.0, because:

- `claude_code.api_error` is a telemetry event that reaches Loki only
  ([ADR 0015](0015-loki-through-its-otlp-endpoint.md)); the app would have to poll Loki
  for an alert that hooks already give it live;
- the collector drops the event's free-text `error` while the capture-content opt-in is
  off ([ADR 0021](0021-collector-privacy-filters.md)), so it adds little beyond the
  `StopFailure` code;
- the `headless-api-error` fixture holds one `api_error` (status 404) and one
  `StopFailure` (`model_not_found`) for the same failure, so counting both would count
  one failure twice.

**Out of v0.6.0:** immediate alerts on terminal error codes (one failure that no retry
can fix), `api_error` from Loki, and per-rule channels (every rule goes to both
channels).

### Consequences

- Good, because each session's wait or failure streak notifies once, and a second
  session is never hidden by the first.
- Good, because the rules are pure: `tests/test_domain_alerts.py` replays them with a
  fake clock and over a real fixture, and the writer, the settings screen and the
  diagnostics share them.
- Good, because bounded settings and messages without the value keep typed input out of
  logs and pages.
- Bad, because an alert missed in the browser is not repeated while the same episode
  lasts. Mitigated by the Discord channel and by the UI, which shows the waiting
  session.
- Bad, because evaluation every 5 s fires up to 5 s after the threshold and merges two
  episodes closer than that. Accepted: the thresholds are tens of seconds or more.
- Bad, because a failure seen only by telemetry (an `api_error` retried successfully,
  with no `StopFailure`) never counts. Accepted: the user is alerted only when a turn
  ends in failure.
- Neutral, because the cost rules depend on Prometheus samples and are as late as the
  telemetry export (ADR 0007).

### Confirmation

- `tests/test_domain_alerts.py` covers the defaults, the bounds and their messages,
  firing at the threshold and not before, no repeat within an episode, the clear, the
  cooldown (including 0), a clock that goes backwards, a disabled rule, sessions that
  disappear, and a replay of the `interactive-permission-compact` fixture whose dialog
  denied with Esc waits 102.8 s and fires once at the first evaluation past 30 s.
- Later stages of milestone 5 add the tests of `repeated_failures`, of the cost rules
  and of the writer's 5 s evaluation.
- `tests/test_domain_telemetry.py` and `tests/test_cli_light.py` keep
  `ccobs.domain.alerts` free of I/O.

## More information

- Extends [ADR 0007](0007-alerts-browser-and-discord.md) (defaults, deduplication,
  "repeated", narrowed to `StopFailure`) and
  [ADR 0024](0024-session-agent-and-block-state-machine.md) (the `waiting_user` episode
  and the `StopFailure` alert it left to milestone 5). Neither is rewritten.
- How environment variables and the settings screen take precedence, and the Discord
  message format, are decided by later stages of milestone 5.
- Revisit when users ask for reminders during a long wait, per-rule channels or
  cooldowns, or when a recapture shows `StopFailure` missing for a failed turn.
