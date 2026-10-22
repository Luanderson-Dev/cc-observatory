---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# Discord delivery: an outbox in the alerts table, sent by a thread through the writer

## Context and problem statement

[ADR 0007](0007-alerts-browser-and-discord.md) makes a Discord webhook the optional
channel that reaches the user away from the desk, and leaves the message format to
milestone 5. [ADR 0042](0042-alert-tables-settings-precedence-and-retention.md) stores
each alert with a Discord delivery state (`none`, `pending`, `sent`, `failed`,
`expired`) and leaves the transitions between them to this stage. The webhook is the
only connection that leaves the machine ([ADR 0006](0006-privacy-metadata-only-by-default.md),
[ADR 0030](0030-the-app-container.md)), its URL is a secret, and Discord rate limits
webhooks and bans an IP that sends too many invalid requests (10,000 401, 403 or 429
answers in 10 minutes, per its
[rate limits page](https://discord.com/developers/docs/topics/rate-limits)). How does
an alert reach Discord, what does the message say, and how does the app behave when
Discord is slow, rate limits it or refuses the webhook?

## Decision drivers

- One writer ([ADR 0026](0026-app-database-schema-migrations-and-retention.md)): no
  other thread writes the database.
- A slow or absent Discord never delays the hook route, the alert ticks or the UI.
- An alert is not lost to a transient failure, and is not sent hours late.
- The webhook URL (its token) never reaches a log, a page, an API answer, a metric or
  a database row.
- A message leaves the machine: it holds no content, path or error text, and the
  project name only when the user asks (maintainer's decision P6).
- Discord's limits are respected, and a dead webhook is not hammered.
- Works behind a proxy, on a network that reaches the internet only through one.

## Considered options

- **An outbox: the alerts table, sent by a thread of its own that asks the writer.**
  The alert is stored `pending` in the tick's transaction; a sender thread reads the
  due deliveries through the writer, posts, and hands the outcome back. At least once.
- **Post from the writer thread at each tick.** Simple, but every tick would wait up
  to the HTTP timeouts, and so would every hook batch behind it.
- **A sender thread with its own database connection.** No round trip through the
  writer, but a second writer, against ADR 0026.
- **Grafana alerting or Alertmanager to Discord.** Rejected in ADR 0007 already: the
  rules live in the app.

## Decision outcome

Chosen option: "an outbox: the alerts table, sent by a thread of its own that asks the
writer", because it keeps one writer, never makes the writer or the hook route wait on
Discord, and survives a restart: a pending delivery is a row. This ADR extends ADR 0006
(what may leave the machine), ADR 0007 (the Discord channel), ADR 0030 (the app's route
out, on the `edge` network), ADR 0038 and ADR 0039 (`ccobs doctor` reads the suspension
in stage 13 of milestone 5). The message fields, the project switch and the test
button's rate are the maintainer's decisions P6 and P7 (2026-10-20); the cadence, the
timeouts, the retry and expiry rules, the coalescing, the escaping, the metric and the
`CCOBS_APP_URL` setting were set during stage 12 of milestone 5, and the diagnostics
field's shape, its 24-hour window and the severities of doctor's `alerts` check other
than Discord-not-configured during stage 13, pending the maintainer's review.

**Outbox.** With `CCOBS_DISCORD_WEBHOOK_URL` set, the writer stores each alert that fires
with its delivery `pending`, due at once (ADR 0042). The Discord sender
(`ccobs.server.discord.DiscordSender`) runs on a daemon thread and never touches the
database. Every 5 s it hands the writer a round (`Writer.submit_discord_round`, a
`concurrent.futures.Future`) and waits up to 5 s for the answer; without one it cancels
the round, which the writer then skips. In one transaction
(`Repository.take_due_deliveries`) the writer marks `expired` every delivery still
`pending` an hour after its alert fired, never sent, and reads up to 10 deliveries due,
the earliest first, with their sessions' `cwd`. The writer itself logs and counts the
deliveries it marks `expired` (`Writer`'s `on_discord_expired`, the metric's `expired`):
a sender that stopped waiting cannot cancel a round already running, so a count left to
the sender would miss the deliveries of a round whose answer nobody read. The sender
posts them and hands the
outcome back (`Writer.submit_discord_results`), which the writer records in one
transaction (`Repository.record_deliveries`), changing only deliveries still `pending`.
Both go through the writer's queue in order, so a round never reads a delivery whose
last outcome is still queued.

**At least once.** If the app stops between the POST and the write of its outcome (or
the write fails), the deliveries are still `pending` and are posted again after the
restart, within the hour: a duplicate message is possible, a lost one is not.

**Outcomes.** All the alerts of one message get the same outcome:

- 2xx: `sent`, one more attempt counted.
- 429: still `pending`, attempts unchanged, due again after Discord's `retry_after`: the
  JSON body's `retry_after` or the `Retry-After` header (seconds), the longer one when
  both are valid, 5 s when neither is, at most 15 minutes. No message goes out before
  then. The pause is timed on the monotonic clock (`time.monotonic`), not the wall
  clock, so a clock stepped back hours cannot hold sending past Discord's wait.
- A 5xx, a timeout, no connection or any other transport error: one more attempt, and
  due again after 5 s, doubling per attempt (5, 10, 20, 40 s), capped at 5 minutes;
  `failed` once 5 attempts were made.
- 401, 403 or 404: the webhook was deleted or refused. The alerts are `failed`, and the
  sender suspends all sending until the app restarts, so a dead webhook does not count
  toward Discord's ban; it logs that once, with the status only, and exposes the status
  (`DiscordSender.suspended_status`) for `ccobs doctor` (stage 13). Rounds still run
  with no alert read, so the expiry goes on.
- Any other status (a redirect, which is never followed, or another 4xx such as 400):
  `failed`, not retried, not suspended: the same message would be refused again.

**Coalescing.** Up to 10 due alerts go in one POST, one line each, which keeps a burst
well inside Discord's per-webhook rate limit at one POST per 5 s.

**Message** (`ccobs.domain.discord_message`, pure). The JSON body is
`{"username": "cc-observatory", "content": ..., "allowed_mentions": {"parse": []}}`,
posted to the webhook URL with `?wait=true`, so Discord answers once the message exists
and a 2xx means it does. Each line says what happened in the alerts page's words
(`presentation.describe_alert`, the fired wording), then the host label, the session's
short ID (8 characters) and a link to the session page,
`<CCOBS_APP_URL>/sessions/<id>`, in angle brackets so Discord shows no preview; a
`daily_cost` alert has no session and links the alerts page. The project name
(`presentation.project_name`) follows only with `CCOBS_DISCORD_INCLUDE_PROJECT=true`,
on one line and at most 100 characters. A message never holds the `cwd`, a path, a
prompt, a tool input or error text; an alert row holds none (ADR 0042). A `content`
longer than Discord's 2000 characters keeps as many whole lines as fit and ends with
`…and N more`; those alerts count as sent with the message.

**Cleaning and escaping.** Text that does not come from the app (the project name, the
host label, the session ID) is cleaned first: every character of Unicode category Cc
(controls and line breaks), Cf (format characters: bidi overrides such as U+202E,
zero-width characters such as U+200B), Zl or Zp (U+2028, U+2029), and any other
whitespace but the space, becomes a space (`unicodedata.category`), so a value can
neither break its line nor reorder or hide what Discord shows. A stored session ID
passes `is_valid_session_id`, which refuses only Cc, so it is cleaned too; its link
percent-encodes it as it is. Then it gets a backslash before each of `\ * _ ~ ` | > [ ] ( ) < @`, so it
shows as typed. `allowed_mentions` with an empty `parse` list is what stops every ping
(`@everyone`, `@here`, a role or a user), whatever the content says; escaping only
changes what Discord shows.

**Links.** `CCOBS_APP_URL` is the app as the browser opens it, `http` on `localhost`,
`127.0.0.1` or `[::1]` with an optional port and nothing else. Inside the container the
app cannot see the port compose publishes it on, so compose derives the variable from
`CCOBS_APP_PORT` (`http://localhost:${CCOBS_APP_PORT:-4300}`), as it derives
`CCOBS_GRAFANA_URL` ([ADR 0035](0035-per-session-tokens-and-cost-from-prometheus-and-grafana-links.md)).
The link opens on the machine that runs the stack only, which is where the session is.

**HTTP.** A sync `httpx.Client` of the sender's own: 5 s per phase (connect, read,
write, pool), no redirects, and `trust_env=True`. Every other client of the app sets
`trust_env=False`, since the stores are on the compose network; this one is the app's
only connection that leaves the machine, so `HTTPS_PROXY`, `NO_PROXY` and
`SSL_CERT_FILE` from the container's environment apply to it. A 429 body is read up to
64 KiB; other bodies are not read.

**Logging.** Only status codes and exception type names, never the URL, a request, a
response body or an exception's text: httpx's exceptions name the URL in their
messages. httpx logs every request's URL at INFO (logger `httpx`), and httpcore traces
at DEBUG; at start-up the sender puts a filter on each of their loggers that drops any
record naming a webhook path (`/webhooks/`). A test imports every module of httpx and
httpcore and fails if a logger they define lacks the filter, so an upgrade that adds one
is caught. The app's own log handler (`configure_logging`) carries the same filter,
since a handler sees the records of every logger that propagates to it, named or not.
Only the sender reads the secret's value
(`SecretStr.get_secret_value`), at each POST.

**Metric.** `ccobs_alert_deliveries_total{host, result}`, counted per alert, with
`result` one of `sent`, `retried`, `rate_limited`, `failed`, `expired` and `suspended`
(the alerts of the refused POST); every series is exported from the start at 0, with or
without a webhook ([ADR 0028](0028-app-metrics-and-their-listener.md)).

**Test button.** With a webhook configured, the alerts page's settings show "Send test
message", which htmx posts to `POST /ui/alerts/discord/test`, the third route behind the
state-changing routes' guard ([ADR 0045](0045-state-changing-routes-and-csrf.md)). It
posts one fixed message ("cc-observatory test message from host <host>. Alerts will
show up here.") through the same client and formatter, on a worker thread, at most once
per 10 s (a click sooner answers "Wait a few seconds before sending another test." and
sends nothing), and answers a status line that says what the answer means: "Test
message sent."; for 401, 403 or 404, "Discord did not accept the test message (status
N). Check CCOBS_DISCORD_WEBHOOK_URL."; for 429, "Discord is rate limiting the webhook.
Try again in a minute."; for a 5xx, "Discord had an error (status N). Try again
later."; for any other status, "Discord did not accept the test message (status N).";
and "Could not reach Discord (timeout)." (or `connection error`, or `error`). Only the
statuses that mean a wrong or deleted webhook point at the URL. Never the URL. It stores
nothing, is not an alert, and never suspends the sender. Answers are 200, since htmx
swaps only 2xx answers.

**Lifespan.** The sender exists only when a webhook is configured
(`app.state.discord`, `None` otherwise); its loop starts after the cost reader, and
`create_app(sweeps=False)` leaves the loop off like the other background threads, the
test button still on.

**Doctor and status.** `GET /api/v1/diagnostics`
([ADR 0038](0038-a-diagnostics-endpoint-for-ccobs-doctor-and-status.md)) gains an
`alerts` object, always present from v0.6.0: `rules` (the names of the rules on, in ADR
0041's order), `open` (the alerts still on), `fired_24h` (fired in the last 24 hours by
the app's clock) and `discord`: `configured`, `suspended_status` (401, 403 or 404 while
suspended, else `null`), `pending`, and `failed_24h` and `expired_24h` (the deliveries
of the alerts fired in the last 24 hours that ended `failed` or `expired`; a delivery has
no time of its own, and its outcome comes at most an hour after its alert fired). No URL,
no alert or session ID, no project name. The counts are one aggregate over the host's
alerts (`Repository.alert_counts`), read in the same read gate slot as the session counts
(ADR 0033), so on the same budget; if that read fails, `alerts` is `null` and the rest of
the answer stands. The field is new and optional for the client, so the contract stays
compatible both ways: a v0.5.0 CLI ignores it, and the shared parser
(`ccobs.domain.diagnostics.parse_diagnostics`) reads an answer without it as an app that
predates alerts, `null` as alerts the app could not read, and a present field of
another shape (a changed type, a missing field) as alerts not understood, while the
rest of the answer stands: a CLI and an app from different releases still show the
sessions and hook failures. Keys it does not know, inside `alerts` and `discord` as in
every part, are ignored, so a newer app may add fields.
`ccobs doctor` adds the check `alerts` to its Stack section, after `diagnostics`
([ADR 0039](0039-ccobs-doctor-and-status-checks-sources-and-exit-codes.md)): `fail` while
sending is suspended ("Discord refused the webhook (status 404); sending is suspended
until the app restarts.", fix: check `CCOBS_DISCORD_WEBHOOK_URL` in the stack's `.env`,
then `docker compose up -d`); `warn` when alerts of the last 24 hours failed or expired,
when the app could not read its alerts (fix: check the app's log), or when its alerts
were not understood ("The app's alerts were not understood (a newer or older app?).",
fix: run the CLI and the app from the same release, upgrading the CLI with
`uvx cc-observatory@latest doctor` or rebuilding the app); `info` when Discord is not configured
("Discord is not configured; alerts show in the browser only.", chosen when milestone 5
was planned, pending the maintainer's review: the browser alone is a valid setup, not a
problem) and when the app predates alerts; `ok`
otherwise. `ccobs status` adds one line, `Alerts: N on; Discord: on`, `off` or
`suspended (status N)` (or `unknown` and why), and `--json` adds `alerts`, shaped like
`hook_failures`: `available: true` with the parsed fields, or `available: false` with
`reason` (`unread`, `predates` or `not_understood`); `null` only without diagnostics. Its
exit codes stay ADR 0039's, so Discord's state never changes them.

**Shutdown budget.** The sender adds a thread whose stop may wait on a POST in flight
(5 s per phase), next to the cost reader's query and the reconciler's. Stopped one after
the other, with a stuck POST and a stuck cost query, the joins would expire in turn: 5 s
for uvicorn's open requests, then 5 s each for the reconciler, the sender, the cost
reader, the alert ticker, the sweeper and the writer, 35 s, past compose's 30 s
`stop_grace_period`; Docker would kill the app (exit 137) and the writer, stopped last,
would lose the hooks still queued. So the lifespan stops the background threads
together (`ccobs.server.app.stop_together`): it signals the reconciler, the sender, the
cost reader, the alert ticker and the sweeper, each of which interrupts its wait at
once, then joins each, in that order, for at most its own timeout and never past one
shared deadline, the longest of their timeouts (5 s) from the signal. The writer stops
after them, with its own 5 s to drain the queue. The worst case is 5 + 5 + 5 = 15 s,
half the grace period. This supersedes the "about 20 s" of
[ADR 0030](0030-the-app-container.md), which counted the reconciler, the sweeper and the
writer one after the other, before the alert ticker, the cost reader and the sender
existed; ADR 0030 stays as accepted.

### Consequences

- Good, because the hook route, the writer and the UI never wait on Discord, and one
  writer remains.
- Good, because a restart or a short outage loses no alert, and a dead webhook stops
  costing requests at once.
- Good, because the webhook's token has no way into a log line, a page or a row, and
  a message holds no path or content.
- Bad, because a duplicate message is possible after a crash at the wrong moment
  (at least once).
- Bad, because an alert waits up to 5 s for the next round, more while rate limited,
  and is dropped unsent after an hour of failures.
- Bad, because a suspended sender needs a restart once the webhook is fixed; the
  environment variable needs one anyway.
- Bad, because the links open only on the machine that runs the stack.
- Bad, because a background thread still stuck when the shared deadline passes is left
  running (a daemon thread, logged) while the writer drains, as a thread stuck past its
  own timeout was before.
- Neutral, because the proxy settings of the container's environment now matter, for
  this connection only.

### Confirmation

- `tests/test_domain_discord_message.py`: the wording per rule, the project on and
  off, no `cwd`, the escaping (markdown, `@everyone`, mentions), the 2000-character cut
  with its count, the 10-alert limit, the payload with `allowed_mentions`.
- `tests/server/test_discord.py`: each status path with an `httpx.MockTransport` and a
  fake clock (2xx, 429 by body, header, both and the cap, 5xx and transport errors with
  the back-off and the attempts limit, 401/403/404 and the suspension that stops every
  later request, other statuses, the expiry left to the writer, coalescing), the 429
  pause on the monotonic clock with a wall clock stepped back or forward hours, the
  loop, the stop, the test button's interval and its status line per status (401/403/404,
  429, 5xx, others), and the canary: every path run with every logger at DEBUG, and no
  part of the webhook URL in the logs.
- `tests/server/test_writer_discord.py`: rounds and outcomes through the writer, in
  queue order, a cancelled round skipped, failures logged by type, a round left at stop
  settled, and the expiries counted by the writer, including the race where the sender's
  wait runs out while the writer is already reading (its cancel fails, nobody reads the
  answer).
- `tests/test_domain_discord_message.py` also cleans every Cc, Cf, Zl and Zp character
  (U+2028, U+202E, U+200B and others) out of the project name, the session ID and the
  host label.
- `tests/server/test_app_shutdown.py`: `stop_together` signals every thread before it
  joins any, and stuck threads share one deadline; the lifespan signals the five
  background threads, joins them, then stops the writer.
  `tests/server/test_server_main.py` sums every `*_STOP_TIMEOUT_S` of the app (a new
  one fails the test until it is placed) and checks the worst case, 15 s, stays within
  two thirds of the 30 s grace period; it also checks the filter on every httpx and
  httpcore logger and on the app's handler, for a record from a logger nobody named.
- `tests/server/test_repository.py`: `take_due_deliveries` and `record_deliveries`.
- `tests/server/test_discord_app.py`: the lifespan, the button behind the guard, and
  the canary end to end: no part of the URL in the logs, the pages, `/api/v1/alerts`,
  `/api/v1/diagnostics`, `/metrics` or the database.
- `tests/server/test_guards.py`: the guarded POST paths.
- `tests/server/test_diagnostics.py`: the `alerts` object's shape and counts (open of any
  age, the 24-hour window on the app's clock, another host's alerts left out), the rules
  in force, a configured webhook and a real suspension reported without any part of the
  URL, a failed alerts read answered as `null` with the rest intact, one read gate slot,
  and the answer read back by the CLI's parser.
- `tests/test_domain_diagnostics.py`: the `alerts` part present, absent (an app older
  than v0.6.0), `null`, and malformed (not understood, the rest of the answer intact),
  and unknown keys inside `alerts` and `discord` accepted.
- `tests/test_domain_doctor.py`, `tests/test_cli_doctor.py` and
  `tests/test_cli_status.py`: the `alerts` check at each status (ok, info for Discord off
  and for an app that predates alerts, warn, fail), its place in the Stack section, the
  status line and `--json`, and a webhook URL planted in the answer never printed;
  `tests/server/test_discord_app.py` feeds the app's answer after a real suspension to the
  `alerts` check and finds no part of the URL.

## More information

- Discord's [Execute Webhook](https://discord.com/developers/docs/resources/webhook#execute-webhook)
  and [rate limits](https://discord.com/developers/docs/topics/rate-limits) pages.
- `ccobs doctor` and `ccobs status` report the suspension and whether a webhook is
  configured from stage 13 of milestone 5 (Doctor and status, above).
- Revisit if users want per-rule channels or embeds, or if central mode (ADR 0001)
  sends from several hosts to one webhook.
