---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# Alerts reach the browser: an alerts ping on every stream and an API to refetch

## Context and problem statement

[ADR 0041](0041-alert-rules-defaults-deduplication-and-evaluation.md) fires alerts on
the writer thread and [ADR 0042](0042-alert-tables-settings-precedence-and-retention.md)
stores them; milestone 5 shows them in the browser as notifications, besides Discord.
[ADR 0032](0032-live-updates-change-hub-sse-pings-and-refetch.md) already gives the
overview and the session page a server-sent events stream of change pings, and only
those two pages open one. The writer already tells the change hub `ALERTS` after a fire
or a clear, and the hub ignored it. How does a browser learn that an alert fired, and
read it, without session data on the stream, with the strict CSP of
[ADR 0034](0034-browser-hardening-csp-fetch-metadata-and-response-headers.md), and
whichever page of the app is open?

## Decision drivers

- Live updates are pings plus refetch, never data over SSE (ADR 0032).
- Reads go through the read gate on the readers' engine, capped (ADR 0033).
- No page or response shows a `cwd` or content: the project name stands in for it
  (`ccobs.domain.presentation`, ADR 0006).
- The CSP stays as it is: the app's own scripts only, no worker sources.
- At most 16 streams for the whole app (`MAX_STREAMS`, ADR 0032), with the same refusal.
- Several tabs open at once must not show the same notification several times.

## Considered options

- **A ping on the existing stream, every page streams, and a JSON route to refetch.**
  The stream says only "the alerts changed"; the page reads `GET /api/v1/alerts`.
- **The alert in the stream's data.** One request fewer, but session data (a session
  ID, a value) would travel over SSE, against ADR 0032, and a missed event would be lost.
- **A Service Worker with the Notifications API.** Notifications with no tab open, but a
  worker needs a `worker-src` in the CSP, a registration and its own update cycle, and
  runs when the user did not open the app.
- **Polling the alerts route.** No stream change, but a read every few seconds per tab,
  for alerts that fire rarely.

## Decision outcome

Chosen option: "a ping on the existing stream, every page streams, and a JSON route to
refetch", because it keeps ADR 0032's model (pings plus refetch), adds one read per
burst of alerts instead of a poll, and needs no CSP change. This ADR extends ADR 0032,
which stays in force. The route's shape, the cursor names and the subject's kind were
chosen in stage 8 of milestone 5; the browser behaviour below (first visit, 5 minutes,
dedup) is proposed for stage 11. Both await the maintainer's decision, which is why this
ADR is proposed.

**The ping.** `ChangeHub.publish(ALERTS)` marks every subscription, filtered on a
session or not, with a pending alerts flag; no session is marked as changed. The stream
then sends, in the same coalescing window as the changes, `event: alerts` with the
fixed `data: alerts` (an event with no data line is never dispatched by
`EventSource`), after the burst's `changed` event when there is one. The data never
holds an alert, a session or a value. `changed` and `ccobs:changed` keep their meaning:
an alert alone sends no `changed` event, and `live.js` does not turn `alerts` into
`ccobs:changed`.

**The route.** `GET /api/v1/alerts`, through the read gate on the readers' engine, for
the app's host label only. Each item has `id`, `rule`, `subject_kind` (`session`, or
`day` for `daily_cost`), `session_id` (`null` for a daily alert), `project` (the
session's project name from `presentation.project_name`, never the `cwd`; `null` for a
daily alert or a session without one), `fired_at_ms`, `cleared_at_ms`, `value` and
`threshold`. The Discord delivery (`discord_state`, `discord_attempts`,
`discord_next_at_ms`) is internal and never served. `limit` defaults to 50, from 1 to
500 (ADR 0033's cap). Cursors are an alert `id`, exclusive, named like the session
routes' `before` (ADR 0027):

- no cursor, or `before=<id>`: the latest alerts, or the next older ones, newest first
  (ADR 0042's order), to list the history;
- `after=<id>`: the next newer alerts, oldest first, so a browser that keeps the last
  `id` it saw catches up page by page, never skipping one when more than `limit` fired;
- both at once, or a cursor outside 0 to 2^63 - 1, is a 422 without the value.

The read is one query, the alerts joined to their sessions' `cwd`, so the project name
comes from the same snapshot as the alert.

**Every page streams.** `base.html` gives every page a `data-stream`: the session page
keeps its filtered stream, and every other page (All sessions, Session not found, the
error pages) gets the unfiltered `/ui/stream`. A page with nothing to refetch simply
ignores `ccobs:changed`. Without JavaScript no stream opens and the pages work as
before. `MAX_STREAMS` stays 16 and the 17th stream still gets its plain 503 with
`Retry-After`; since every tab now counts, users with many tabs reach it sooner, and
those tabs show "Live updates paused" and retry as before.

**No Service Worker.** Notifications show only while a tab of the app is open; the CSP
is unchanged.

**In the browser (stage 11).** On an `alerts` ping, and when the stream opens, the page
fetches `?after=<cursor>` and shows a notification for each new alert:

- the cursor is the newest alert `id` seen, kept per browser in `localStorage` for the
  app's origin, so every tab shares it;
- on the first visit (no cursor yet), the page sets the cursor to the newest alert and
  shows nothing, so an old history never floods the screen;
- later, it shows only unseen alerts that fired less than 5 minutes ago, and moves the
  cursor past every alert it read;
- each notification has the alert `id` as its `tag`, so several tabs that fetch the
  same alert replace one notification instead of stacking copies.

### Consequences

- Good, because the stream still carries no session data, and a missed ping costs
  nothing: the next ping or reconnection refetches from the cursor.
- Good, because every page can notify, with one stream per tab as before and no CSP or
  worker change.
- Good, because the API never serves a `cwd` or the Discord delivery state.
- Bad, because every tab now holds a stream: 16 tabs of any page reach `MAX_STREAMS`,
  and the 17th pauses its live updates and its notifications until a slot frees up.
- Bad, because a browser opens about 6 HTTP/1.1 connections to one host and the app
  serves only HTTP/1.1 (ADR 0032): with five tabs of any page open besides the overview,
  the next request to the app (a page, the board's refetch, `/api/v1/alerts`) waits for
  a free connection and the UI looks stuck. Before v0.6.0 only overview and session tabs
  held one. The documented way out stays: close tabs, or open some through `127.0.0.1`
  and others through `localhost`, which the browser counts as two hosts.
- Bad, because pages that refetch nothing (All sessions) still receive `changed` events
  they ignore; they are a few bytes each, coalesced.
- Bad, because no notification shows while no tab is open; Discord covers that case.
- Neutral, because the alerts route orders newest first by default, unlike the session
  routes' oldest-first pages; `after` pages run oldest first.

### Confirmation

- `tests/server/test_live.py`: an `ALERTS` publish marks filtered and unfiltered
  subscriptions without a session change; the ping is the fixed marker, apart from the
  `changed` event, and reaches both kinds of stream over HTTP; `changed` is unchanged;
  the 17th stream still gets the 503.
- `tests/server/test_api.py`: newest first with `project` and never `cwd`, the daily
  alert's shape, no Discord fields, both cursors covering every alert once, the limits
  and their 422, `before` with `after` a 422 without echo, one read slot per request,
  the readers' engine, another host's alerts absent, and the `Host` guard.
- `tests/server/test_repository.py`: `alert_views` pages both ways, capped, with the
  session's `cwd`, on the readers' engine.
- `tests/server/test_ui_overview.py` and `test_ui_session.py`: every page, error pages
  included, has one `data-stream` and no inline code.

## More information

- Extends ADR 0032 (change hub and stream); uses ADR 0033 (read gate), ADR 0041 (rules)
  and ADR 0042 (alert table and order).
- Stage 11 of milestone 5 implements the browser side (`live.js` or a new script), the
  permission request and the notification text; stage 13 gives `ccobs doctor` a summary
  of the Discord deliveries.
- Revisit if users ask for notifications with no tab open (a Service Worker, and a CSP
  change), or if many tabs keep hitting `MAX_STREAMS` or the browser's connection limit
  (one stream shared by the tabs of a browser, through a BroadcastChannel or a
  SharedWorker).
