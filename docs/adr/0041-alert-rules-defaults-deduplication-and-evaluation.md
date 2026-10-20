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
  `AlertCleared(rule, subject, cleared_at_ms)`. Each evaluator returns its effects
  sorted by subject; the writer calls the rules in a fixed order (stage 6 may add a
  combined evaluation).

**Evaluation.** The rules are pure functions of the current time, the configuration, the
evaluator's state and the sessions. The writer thread, which already holds every open
session in memory, evaluates them every 5 s (stage 6 of milestone 5). Two episodes of
one session less than 5 s apart can therefore count as one. A condition that keeps
coming back fires and clears repeatedly, at most once per cooldown: for example one
failure every 4 minutes with N = 3 and W = 10 minutes.

How the writer runs them (stage 6 of milestone 5, set by its implementer on the main
context's brief):

- **Cadence.** An alert ticker (`ccobs.server.sweeps.AlertTicker`), a daemon thread like
  the sweeper, submits an `AlertTick` to the writer's control channel every 5 s, with
  the hook route's clock. It never writes; the evaluation and its writes run on the
  writer thread (ADR 0026, one writer), off the hook route, and read no network: a
  tick is a walk over the sessions in memory plus the keys and failure windows.
- **Order.** Each tick runs the rules in a fixed order, `waiting_user`,
  `repeated_failures`, `session_cost` and `daily_cost`, over the committed sessions the
  writer holds and the last `CostSample` it was given (the cost reader of stage 7,
  [ADR 0043](0043-background-cost-queries-to-prometheus.md), submits one; until then
  there is none, and the cost rules neither fire nor clear). Hooks queued before a tick
  are written first, so it sees them.
- **One transaction per tick.** The tick's effects are written in that order in one
  transaction (`Repository.apply_alert_effects`): an `AlertFired` becomes an `alerts`
  row whose Discord delivery is `pending`, due at once, when a webhook is configured,
  else `none`; an `AlertCleared` clears its key's open row. A tick without effects
  writes nothing. If the write fails, nothing changes in memory and the next tick
  computes the same effects again; nothing fires twice, since memory only moves once
  the rows are in.
- **Failure windows.** Every hook event a commit stores is recorded with
  `record_failure_event`, in the order the writer applied it; an event the writer did
  not store (a duplicate `seq`) is not.
- **State after a start.** The first tick rebuilds the evaluators' state from the
  database (ADR 0042's `alert_keys` and `failure_windows`, the windows dropped while
  `repeated_failures` is off) with that tick's time as now, and loads the saved
  settings over the environment's. Until then no failure is recorded in memory: the
  rebuild reads every committed one. A rebuild that fails is tried again at the next
  tick. A key that was firing before a restart stays firing, so its episode does not
  fire again, and its clear updates the same row.
- **Configuration.** The effective rules are the environment's (`CCOBS_ALERT_*`) with
  the saved settings on top (`apply_saved_settings`, ADR 0042): a saved value of the
  wrong type or out of bounds is ignored, logged by its setting's name only, and the
  environment's value stays. The writer reloads the saved settings when the settings
  screen tells it one changed (an `AlertSettingsChanged` control); the next tick uses
  them.
- **After a write.** The writer tells the live updates hub (ADR 0032) that the alerts
  changed (`ALERTS`) once the tick's transaction has committed, and only when it wrote
  something; the stream event and the API that refetch on it come with stage 8.
- **Metric.** `ccobs_alerts_fired_total{host, rule}` counts the alerts that fired since
  the app started, once their rows are committed, with a series for each of the four
  rules from the start at 0 (ADR 0028's closed label values). The fixed total of the
  app's series goes from 71 to 75 per host.

**"Repeated" failures** (this narrows ADR 0007). The rule counts `StopFailure` hook
events only, of every `error` code, in one session within the window. Each session
keeps a sliding window of failure times, from the receive time of each hook event, and
nothing else (no code, no text).

- A failure counts while it is at most W old: at `now - W` it still counts. An episode
  runs while N or more count, and fires once as above with the count as its value.
- `StopFailure` counts in any scope: a subagent's API failure is a failure of the
  session. Only a main-thread `Stop` (no `agent_id`) is a successful turn and drops
  every failure of the session recorded before it, in processing order
  ([ADR 0024](0024-session-agent-and-block-state-machine.md)), whatever their times, so
  a clock that steps back never keeps an alert firing. A real inversion between one
  turn's `Stop` and another's `StopFailure` cannot happen: a `UserPromptSubmit` and
  seconds separate them. A subagent's `Stop` or `SubagentStop` proves nothing about the
  main turn and resets nothing.
- The episode clears, if it fired, when the count falls under N: failures age out, a
  successful `Stop` drops them, or `SessionEnd` drops the session's window.
- A failure that arrives out of order is inserted in time order. The window keeps only
  the newest N times, since older ones can never complete a streak, so a late failure
  older than a full window is dropped.
  A failure timed after `now` (the clock went backwards) counts.
- Memory stays bounded: at most N times per session, failures leave once they no longer
  count, and a session with none leaves. A disabled rule records nothing and drops
  every window. Changing N applies to the next failure recorded: lowering it fires on
  the failures already in the window and trims the window at the next failure; raising
  it lets the window grow to the new N.
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

**The cost rules.** Both read Claude Code's cost counter in Prometheus
(`claude_code_cost_usage_USD_total`, ADR 0014) through ADR 0013's exact totals,
`increase(... anchored)`, built by `telemetry.total_query` and filtered on the app's
`host` label only. The reader that sends the queries in the background is stage 7 of
milestone 5 ([ADR 0043](0043-background-cost-queries-to-prometheus.md)); the domain
builds the queries, parses the answers into a `CostSample` (the totals and the time the
queries ran) and evaluates it.

- **`session_cost`** sends one query for every session,
  `sum by (session_id) (increase(claude_code_cost_usage_USD_total{host="<host>"}[<range>] anchored))`,
  with no `session_id` matcher, so no session ID reaches Prometheus and the number of
  sessions does not change the number of queries. It is the session page's query
  ([ADR 0035](0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md))
  grouped by `session_id` instead of `model`. Its range is the page's window of the
  oldest session evaluated, from 5 minutes before its start to now: every session it
  covers from its start has nothing before the range, so `anchored` starts from its zero
  sample and its total is the one its page shows. `session_cost_since` gives that start:
  the earliest start of the sessions the writer holds that are neither `ended` nor
  `stale`, or none, and then the reader sends no query.
- **Cost of the range.** The range grows with the oldest open session's age: a session
  idle but alive for days makes a range of days, and every poll reads all the host's
  cost samples in it. The reader must therefore not query more often than Claude Code's
  metrics export interval (about one minute), since a faster poll sees no new sample;
  the reader and its cadence are decided in stage 7 of milestone 5
  ([ADR 0043](0043-background-cost-queries-to-prometheus.md): every 60 s, only while a cost
  rule is enabled).
- Only a session the writer holds that is neither `ended` nor `stale` can alert. A
  session of the answer the writer does not hold (an older one still inside the range,
  whose total the range cuts) is ignored. A session that ends, turns stale or leaves the
  answer ends its episode. Accepted consequence: a session above the limit that turns
  `stale` clears, and if it works again its total, which never drops, starts a new
  episode that fires again once the cooldown has passed. Making a stale session hold its
  alert instead was rejected: a session that never comes back would keep a firing alert
  until the writer drops it, and the evaluator would need a third session state; the
  clear-and-refire keeps the code to one rule (open or not).
- A limit changed while an episode fires applies at the next evaluation, with the same
  hysteresis: raised so that the total falls below 90 % of the new limit, it clears;
  raised less, it holds; lowered, it never fires again in the same episode.
- **`daily_cost`** sends
  `sum(increase(claude_code_cost_usage_USD_total{host="<host>"}[24h] anchored))`: the
  rolling 24 hours (decision P2), across sessions, with the subject `day`. An empty
  answer, which `sum` gives over no series, is US$ 0.
- An episode fires when the total reaches the limit, with the total as its value and the
  limit as its threshold. **Hysteresis:** once fired, it lasts while the total stays at
  or above 90 % of the limit, and clears below it, so a rolling day that loses an old
  request and gains a new one around the limit does not clear and fire again. This 90 %
  was set by the implementer of stage 3 on the main context's brief, pending the
  maintainer's objection.
- **Staleness:** a sample more than 180 s old at the evaluation (three of Claude Code's
  one-minute exports), or no sample at all (Prometheus down or its answer invalid),
  neither fires nor clears: an alert never clears because Prometheus stopped answering.
  A session that ends or leaves the writer still clears, since that comes from the hooks,
  not from the sample. A sample timed after now (the clock went backwards) counts.
- The parser accepts only the answers ADR 0035's parser accepts: a well-formed instant
  vector of finite, non-negative values, one series per label value. Anything else
  (`NaN`, an infinity, a negative value, a non-string label or value, a repeated
  `session_id`, or a day of more than one series) makes the whole sample invalid, never
  a partial total and never an exception. A series without `session_id` belongs to no
  session and is left out.
- **Limitations.** With `OTEL_METRICS_INCLUDE_SESSION_ID=false` set outside
  `ccobs setup` (ADR 0016), the series carry no `session_id`: `session_cost` has no data
  and never fires, while `daily_cost` still works. A series idle for more than
  Prometheus's 5-minute lookback across the start of the 24-hour range takes its
  starting value from its first sample inside the range (ADR 0013), so the day can
  undercount a session idle at that moment. Stage 7 of milestone 5
  ([ADR 0043](0043-background-cost-queries-to-prometheus.md)) decided, with ADR 0013's
  evidence, that the reader passes no `lookback_delta`. The totals lag Claude Code's
  export interval, about a minute.

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
- The same file covers `repeated_failures`: N failures within W fire once and spread
  over more than W do not, a main-thread `Stop` resets (even one timed before the
  failures) and a subagent's does not, aging out and `SessionEnd` clear, the cooldown,
  a disabled rule, out-of-order failures, the bound of N times per session, lowering
  and raising N, and a replay of the `headless-api-error`
  fixture, whose single `StopFailure` (`model_not_found`) does not fire.
- `tests/test_domain_alerts_cost.py` covers the cost rules: the exact queries (no
  `session_id` matcher, the session page's range, the 24-hour range) and the escaping of
  the host label, the parser on valid, empty and malformed answers (wrong shapes, `NaN`,
  infinities, negatives, non-string labels, repeated sessions, a day of two series),
  firing at the limit and not below, the hysteresis, stale and missing samples, a
  disabled rule, the cooldown, limits raised or lowered while an alert fires, sessions
  that end, turn stale or leave the answer, sessions the writer does not hold, and the
  start of the session query's range (`session_cost_since`: the oldest open session,
  none when every session is ended or stale).
- `tests/server/test_writer_alerts.py` covers the writer's evaluation with a fake clock:
  a replay of `interactive-permission-compact` that fires once at the first tick past
  30 s and clears, three `StopFailure` events that fire once and a successful `Stop`
  that clears, the `headless-api-error` fixture that fires nothing, failures committed
  before the first tick, restarts in the middle of an episode (no second fire, the same
  row cleared), within a cooldown and with a failure streak, the failures rule off at a
  restart, saved settings applied and an invalid one ignored by name, a settings
  reload and its failure, a cost sample stored and evaluated (and a stale one), disabled
  rules, the fixed order in one write per tick, a failed write that changes nothing and
  is retried, a failed rebuild, the Discord delivery states, the hub event only after a
  write, and the metric. `tests/server/test_sweeps.py` covers the ticker (cadence,
  errors, stop, the app's lifespan) and an alert fired end to end from it;
  `tests/test_domain_alerts_settings.py` the merge of saved settings;
  `tests/server/test_repository.py` the one-transaction write and its rollback; and
  `tests/server/test_metrics.py` the new series.
- `tests/server/test_costs.py` covers the cost reader (ADR 0043).
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
