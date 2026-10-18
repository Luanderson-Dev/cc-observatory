---
status: accepted
date: 2026-10-16
decision-makers: Luanderson-Dev (maintainer)
---

# Live updates: a change hub fed by the writer, SSE pings and refetch

## Context and problem statement

The web UI ([ADR 0031](0031-server-rendered-pages-with-htmx-and-vendored-assets.md))
answers "who is waiting on me, and for how long?", so a page must follow the sessions
without a reload. [ADR 0004](0004-web-app-stack.md) chose server-sent events for that;
the app already serializes every write in one writer thread
([ADR 0027](0027-the-app-http-surface-and-hook-queue.md)) and reads through a capped
read gate ([ADR 0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)).
What does the stream carry, who feeds it, what does the page do with it, and how do
open streams stay cheap and let the app stop on time?

## Decision drivers

- Hooks never interfere: nothing the UI does may slow the writer or the hook route
  (CLAUDE.md, [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)).
- One source of truth for what a page shows: the same server-rendered fragments as a
  reload, so a live page and a reloaded one never differ.
- Bounded cost per browser tab: memory, reads and connections, whatever the hook rate.
- A missed event must never leave a page wrong for good (reconnects, sleep, a restart).
- A stop stays inside compose's 30 s grace period
  ([ADR 0030](0030-the-app-container.md)).
- Foreign pages cannot open the stream
  ([ADR 0034](0034-browser-hardening-csp-fetch-metadata-and-response-headers.md)).

## Considered options

- **Change pings plus refetch.** The stream only says "these sessions changed"; the
  page refetches its own fragments through the read gate. One rendering path; the
  stream never reads the database.
- **HTML over the stream** (htmx's SSE extension swapping fragments sent as events).
  Renders per change per client inside the stream, outside the read gate's cap, and
  needs a vendored extension; a missed event leaves a stale fragment.
- **Polling.** Simple, but either slow to show a wait or a read every few seconds per
  tab while nothing changes.
- **WebSockets.** Two-way, which the read-only UI does not need, and a protocol upgrade
  each guard would have to handle.

For feeding the stream:

- **The writer notifies a hub on the event loop after each commit.** Exact (only
  committed changes), O(1) for the writer, no polling.
- **Each stream polls the database or the writer's counts.** A read per tab per
  interval, and changes that do not move a count are missed.

## Decision outcome

Chosen options: "change pings plus refetch" and "the writer notifies a hub on the event
loop after each commit", because they keep one rendering path and the read gate's cap,
cost the writer one non-blocking call per commit, and recover from any missed event by
refetching on every (re)connection.

- **The hub** (`ccobs.server.live.ChangeHub`) is built with the app and bound to the
  event loop at start-up. After each successful commit the writer calls its
  `on_committed` callback with the IDs of the sessions the batch changed, and with `ALL`
  after retention deletes sessions; never for a failed batch. The callback is
  `notify_threadsafe`: one `loop.call_soon_threadsafe`, skipped when no stream is open,
  harmless when the loop is closed; an exception in it is logged and never reaches the
  writer. The writer gets it with the block observer as `WriterObservers`.
- **Subscriptions** hold a pending set of session IDs, optionally filtered to one
  session (the session page), capped at 64 IDs before collapsing to `ALL`: bounded
  memory per client.
- **`GET /ui/stream`** (`?session=<id>` to filter) answers `text/event-stream`: a
  `retry: 5000` line, then one `event: changed` per burst, coalesced for 250 ms, with
  `data: all` or a JSON list of IDs, and a `: keep-alive` comment every 15 s while idle.
  It never opens a database connection. At most 16 streams are open at once; the next
  gets a plain-text 503 with `Retry-After: 5`, counted. A stopping app answers 503 too.
  The response is a Starlette `StreamingResponse`, not FastAPI's
  `EventSourceResponse`, which forces `Cache-Control: no-cache` (the app sends
  `no-store`), takes its keep-alive interval from a module global and could refuse only
  through a dependency, as JSON. The stream sits behind the `Host` guard and Fetch
  Metadata like every route: `EventSource` from the app's own page is `same-origin`;
  from any other page it gets a 403.
- **The client** (`static/js/live.js`, our own, no `eval` or `innerHTML`) opens one
  `EventSource` per tab on the overview and the session page (All sessions, a paged
  list, has none) and dispatches `ccobs:changed` on `document.body` for each event and
  on every `open`, first connection and reconnections alike, so a page catches up on
  whatever it missed with no `Last-Event-ID`. On an error it closes the source, shows
  "Live updates paused. Reconnecting..." in a `role="status"` element, and reopens after
  5 s, doubling to 60 s, so a refused stream is not retried by the browser every few
  seconds. It closes on `pagehide` and reopens from the back-forward cache.
- **Refetch targets.** The overview's `#board` refetches `/ui/board` and replaces
  itself; the fragment carries the same trigger, and `app.js` keeps open bays open and
  the focus on the same strip across the swap. The session page has two: the timeline
  tail (milestone 3, stage 8) and a hidden element that refetches
  `/ui/sessions/{id}/live`, which returns the header, the blocks and the agents out of
  band. "Show older" pages land in lists of their own that the refresh leaves alone. To
  keep both parts contiguous the refresh reads a window, not the latest rows: every
  open block and running agent, the closed and stopped ones from the page's boundaries
  (the cursors of its "Show older" controls) up, the blocks open and agents running
  below a boundary when it was set, which the link names (at most 8 of each), and those
  that closed or stopped after the window's start. Naming them matters because a close
  time can predate the page: the stale sweep stops an agent at its last event, and a
  hook received before the render may be committed after it. A row the page showed open
  or running therefore stays on it once it closes, unless more than 8 sat below a
  boundary. Past 200 rows a section's window starts over at the latest page, with the
  older list emptied, a new pager, the refresh's own time as its new start and the rows
  then open below the new boundary named, so the next refresh does not start over
  again. A "Show older" request still on its way when a reset replaces its control is
  aborted: `app.js` listens for htmx's `htmx:beforeCleanupElement`, which fires on the
  old control's button while it is still in the document, and triggers `htmx:abort` on
  a button still loading, so htmx aborts its XHR and the old cursor never comes back.
  Every trigger uses `hx-sync="this:replace"`, so with the 250 ms coalescing one change
  event costs each tab at most one detail read and one tail read. That bound is per
  event: an XHR that `hx-sync` aborts still finishes its read inside the read gate,
  which caps how many run at once.
- **Shutdown.** uvicorn waits for open connections before it runs the lifespan's
  shutdown, so a stream ended by the lifespan would hold every stop for the graceful
  timeout and then be cancelled. `python -m ccobs.server` runs a `uvicorn.Server`
  subclass whose `shutdown` closes the hub first: every stream's generator returns, and
  the connections close. `timeout_graceful_shutdown=5` stays as a safety net.
- **Metrics** ([ADR 0028](0028-app-metrics-and-their-listener.md)'s rules): the gauge
  `ccobs_ui_streams{host}` and the counter `ccobs_ui_streams_refused_total{host}`, from
  `ccobs.domain.app_metrics`. The app's fixed total grows from 65 to 67 series per host.

### Consequences

- Good, because the writer's cost is one scheduled callback per commit, with no stream
  open none, and a slow or stuck browser cannot slow it.
- Good, because a live page shows exactly what a reload would, through the same
  templates and the read gate's cap.
- Good, because missed events heal on the next (re)connection, and pages the user paged
  through are wiped only when a section's window starts over, after 200 rows.
- Bad, because each change costs a read per open tab (two on a session page); the
  coalescing, `hx-sync` and the 16-stream cap bound it.
- Bad, because browsers allow about 6 HTTP/1.1 connections per host: each tab with a
  stream holds one, so many tabs of the app in one browser can stall its other requests.
  The app serves HTTP/1.1 only; the docs say to close extra tabs.
- Bad, because the reconnection refetch adds one board or session read right after
  every page load.
- Neutral, because a strip moving between bays has no animation yet; View Transitions
  behind `prefers-reduced-motion` can come with the polish of milestone 6.

### Confirmation

`tests/server/test_live.py` covers the hub (coalescing, the session filter, the ID cap,
the 16-stream cap and its counter, close waking every stream, calls from another
thread, a closed loop), the wire format through the app (retry line, events,
heartbeats, headers), the 400 and 503 answers, that the stream never touches the
database, the `Host` guard and Fetch Metadata on `/ui/stream`, a real uvicorn server
stopping in under 2 s with a stream open and no cancellation, and `live.js`'s
reconnection under node. `tests/server/test_writer.py` checks that the callback comes
only after a commit, never for a failed batch, with `ALL` after retention, and that its
errors never stop the writer. `tests/server/test_ui_overview.py` and
`tests/server/test_ui_session.py` check the triggers, the live fragment's window and its
reset, and `app.js`'s handling of swaps; `tests/server/test_repository.py` the windowed
detail; `tests/server/test_metrics.py` the 67 series.

## More information

- WHATWG HTML, [server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html);
  htmx [triggers from other elements](https://htmx.org/attributes/hx-trigger/) and
  [`hx-sync`](https://htmx.org/attributes/hx-sync/).
- uvicorn's [graceful shutdown](https://www.uvicorn.org/settings/#timeouts) waits for
  open connections before the lifespan's shutdown.
- Extends [ADR 0004](0004-web-app-stack.md), [ADR 0027](0027-the-app-http-surface-and-hook-queue.md)
  and [ADR 0030](0030-the-app-container.md); the operator side is in
  [docs/app.md](../app.md#live-updates).
