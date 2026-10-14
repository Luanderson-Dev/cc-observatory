---
status: accepted
date: 2026-10-12
decision-makers: Luanderson-Dev (maintainer)
---

# Alerts through browser notifications and an optional Discord webhook

## Context and problem statement

The most useful thing cc-observatory knows is that a session is waiting on a person
([ADR 0002](0002-native-telemetry-plus-hooks.md)). That helps only if the user notices it
while working in another window or away from the desk. Other conditions deserve a nudge
too: API failures that keep ending turns, and cost above a limit. How should the app
alert, and on what?

## Decision drivers

- Reach the user outside the app's tab and, optionally, away from the machine.
- Nothing leaves the machine unless the user configures it
  ([ADR 0001](0001-local-single-machine-scope.md)).
- The app runs in a container, with no access to the host's desktop.
- Thresholds can be changed without editing files or restarting.
- No alert storms: one condition does not notify again and again.

## Considered options

- **Browser notifications plus an optional Discord webhook, evaluated by the app.**
  Notifications from the app's own page, plus a phone-friendly channel that takes one
  URL to set up. Browser notifications need an open app tab.
- **Grafana alerting or Alertmanager.** Mature routing and silencing, but the rules would
  read the app's state through `/metrics` with scrape delay, thresholds would live in
  another UI, and neither shows notifications from the app's page.
- **OS-native notifications.** The best desktop integration, but the app in a container
  cannot reach the host's notification service; it would need a host daemon per OS.
- **Email, Slack or ntfy.** Email needs SMTP credentials, Slack works much like Discord,
  and ntfy's public server is one more third party. Each can be added later.

## Decision outcome

Chosen option: "Browser notifications plus an optional Discord webhook, evaluated by the
app", because the app already holds the live state, and the two channels cover the user
at the desk and away from it with no credential beyond one webhook URL.

- **Browser.** The Notification API, with permission requested from the UI. `localhost`
  is a secure context, so it works without HTTPS.
- **Discord.** Optional, set with `CCOBS_DISCORD_WEBHOOK_URL`. The URL is a secret: it is
  held as a `SecretStr` and never logged.
- **Rules.** A session in `waiting_user` for longer than X seconds; repeated
  `StopFailure` hook events or `claude_code.api_error` events; estimated cost per session
  or per day above a limit.
- **Thresholds** come from environment variables and from a settings screen whose values
  are stored in SQLite ([ADR 0005](0005-sqlite-for-app-state.md)).
- **Noise control.** Identical alerts are deduplicated, with a minimum interval between
  two of them.

### Consequences

- Good, because alerts use the same state the UI shows, with no extra service.
- Good, because the only outbound traffic is the Discord webhook, and only if it is set.
- Bad, because browser notifications need an open app tab and the user's permission.
- Bad, because cost alerts are as late and as approximate as the telemetry: cost is an
  estimate, and metrics are exported every 60 s by default.
- Bad, because anyone who gets the webhook URL can post to that Discord channel.
- Neutral, because more channels can plug into the same rules later.

### Confirmation

- Tests (milestone 5) cover each rule, deduplication and the minimum interval, with a
  fake clock.
- A test (milestone 5) checks that the webhook URL never appears in logs.
- The permission request and a fired notification are checked in a real browser
  (CLAUDE.md).

## More information

- The default thresholds, how environment variables and the settings screen take
  precedence over each other, what counts as "repeated" and the Discord message format
  are decided in milestone 5.
- `waiting_user` is a state of the session state machine, decided in milestone 2.
