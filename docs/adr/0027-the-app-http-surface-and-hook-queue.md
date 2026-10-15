---
status: accepted
date: 2026-10-15
decision-makers: Luanderson-Dev (maintainer)
---

# The app's HTTP surface: guarded routes, a neutral hook route and a bounded queue

## Context and problem statement

[ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md) has Claude Code post every
hook to the app, waiting up to 1 s each, and counts any non-2xx answer, plain-text body
or connection error as a hook error that the user sees in the transcript.
[ADR 0001](0001-local-single-machine-scope.md) asks for browser defences from v0.3.0,
since any web page can make the user's browser send requests to localhost, and
[ADR 0004](0004-web-app-stack.md) puts the app on FastAPI. The writer that parses and
stores hooks ([ADRs 0023](0023-per-event-allowlist-of-hook-metadata.md),
[0024](0024-session-agent-and-block-state-machine.md) and
[0026](0026-app-database-schema-migrations-and-retention.md)) needs each event in
receive order with its receive time. How does the app expose its routes, answer hooks,
and hand them to the writer?

## Decision drivers

- The hook route answers fast (target under 50 ms) and only with an empty 2xx, whatever
  happens (CLAUDE.md).
- A web page cannot read the app's data or post hooks through the user's browser
  (ADR 0001): no DNS rebinding, no cross-site form posts, no CORS.
- Memory stays bounded under a burst of hooks or of large bodies.
- The writer receives events in the order the app received them, each with its receive
  time and a `seq` that continues across restarts (ADRs 0024 and 0026).
- Nothing on the surface leaks personal data: request bodies and paths hold `cwd` and the
  OS username (ADR 0006).

## Considered options

For the hand-off between the hook route and the database:

- **`asyncio.Queue` and an async writer on `aiosqlite`.** Everything on the event loop,
  but SQLAlchemy's async layer over `aiosqlite` still runs SQLite in a thread, Alembic
  and the repository (ADR 0026) are sync, and a slow write competes with the hook route
  for the loop.
- **A bounded `queue.Queue`-style queue, by item count and by bytes, consumed by one
  writer thread with sync SQLAlchemy.** The route only enqueues; the writer batches and
  writes with the repository as it is, off the loop. The queue must be thread-safe.
- **Write in the request.** No queue, but each hook waits for SQLite (a commit, a
  checkpoint, a reader's lock up to the 5 s busy timeout of ADR 0026), against a 1 s
  hook timeout.

## Decision outcome

Chosen option: "a bounded queue by item count and by bytes, consumed by one writer
thread with sync SQLAlchemy", because it keeps the hook route O(1) and off the database,
bounds memory in both dimensions, and reuses the sync repository and migrations. This
ADR extends ADRs 0001, 0004 and 0011, which stay in force.

**One process, one uvicorn worker.** `python -m ccobs.server` runs
`uvicorn.run(create_app(settings), workers=1)` on `CCOBS_LISTEN_HOST` and `CCOBS_PORT`.
The queue lives in the process's memory and the writer is a thread of it, so a second
worker would split the hooks between two queues, two writers and two `seq` counters.

**Guards, in a pure ASGI middleware** (`ccobs.server.guards.HostGuard`; Starlette's
`BaseHTTPMiddleware` adds a task and a stream per request that the hook route does not
need):

- **Host allowlist.** Exactly one `Host` header, naming `localhost` (any case),
  `127.0.0.1` or `[::1]`, with a port from 1 to 65535 or none. Anything else is refused
  before routing: a missing or repeated header, another name (`localhost.evil.example`,
  `localhost.`), userinfo, an empty or non-numeric port. A page using DNS rebinding sends
  its own host name, so it reads nothing. Websocket handshakes are guarded the same way
  (the app has no websocket route, but a later one must not come unguarded): a refused
  one is closed with code 1008, policy violation. The `Host` value and the path
  are never logged.
- **Content-Type by media type,** on the hook route: the part before any `;`, trimmed,
  compared case-insensitively with `application/json`, so `application/json;
  charset=utf-8` is accepted. A missing, repeated or other type is dropped. A cross-site
  page can only send `application/json` after a CORS preflight.
- **Body cap,** `CCOBS_HOOK_BODY_MAX_BYTES`: a declared `Content-Length` over the cap is
  refused before any byte is read, and a body without one (chunked) is read chunk by
  chunk and dropped as soon as it passes the cap, without reading the rest. A malformed
  or conflicting `Content-Length` is dropped too.
- **No CORS.** No CORS middleware, and no response carries an `Access-Control-Allow-*`
  header, so every preflight fails in the browser.

**The hook route answers 204 with an empty body and no headers, for every request.**
Valid or invalid JSON, a bad or missing `Host`, a bad `Content-Type`, a body over the
cap, a full queue, a client gone mid-body, an internal error, and any method other than
`POST` (an `OPTIONS` preflight included) all get the same answer. CLAUDE.md says the hook
route never answers non-2xx, and under ADR 0011 any other answer to a real hook becomes
a notice in the user's transcript. A `GET` or `OPTIONS` is never a Claude Code hook, so a
405 would harm no hook today, but one rule with no exceptions cannot be broken by a
future change of client or method, and the 204 tells a browser nothing it can use: no
CORS headers, no body. 204 rather than 200 because it is the neutral answer the capture
server gave in milestone 0 and Claude Code counted as success (ADR 0012), and it cannot
carry a body that Claude Code might read as a decision. The route is a bare ASGI
endpoint mounted for all methods, and its whole body runs in one `try`, so not even an
exception can produce another status. It is served at `/hooks` and `/hooks/`, and the
router's `redirect_slashes` is off, so a hook URL with a trailing slash gets the same
204 instead of a 307 (and other routes with a stray slash get a 404, never a redirect).
The one exception is below the app: uvicorn's HTTP layer (h11) answers 400 on its own to
malformed HTTP and to an HTTP/1.1 request without a `Host` header, before the app sees
it; Claude Code's requests are well-formed and carry a `Host`, so they never meet it.

**Every drop is counted by reason** in the queue (`full`, `too_large`,
`bad_content_type`, `bad_host`, `bad_request`, `invalid`, `internal`), as plain integers
that a later stage of milestone 2 exposes as metrics. `bad_request` covers requests that
are not hooks (another method, a malformed or conflicting `Content-Length`, a client
gone mid-body); `invalid` is counted by the writer, for bodies it cannot parse as hook
payloads. Drops are logged at debug level with the reason only; an internal error is
logged with the exception's type only, since its message or traceback could quote the
body.

**Other routes answer 400** with a short plain-text body when the `Host` is refused:
they are not hook routes, so a non-2xx is right there.

**The handler does no parsing.** It checks the method, the headers and the body size,
then enqueues the raw bytes. JSON is parsed by the writer, which counts what it cannot
parse as `invalid`, so the route's cost does not grow with the payload's structure.

**`received_at_ms` and `seq` are assigned in the handler,** together, once the whole
body is in: the handler reads the injectable clock (wall-clock UTC milliseconds), takes
the next `seq` and offers the item, with no `await` between the three. On the single
event loop, queue order is then `seq` order, which the reducer needs, since it ignores
an event whose `seq` is not above the session's last one (ADR 0024); and receive times
follow `seq` order, even for concurrent requests. Concurrent hooks do happen: ADR 0011
notes that parallel tool calls fire `PostToolUse` concurrently, and subagents can post
alongside the main agent. The wall clock can still step back (an NTP correction), so
neither the writer nor the reducer may assume that `received_at_ms` rises with `seq`;
the reducer already tolerates it (ADR 0024 keeps the earliest and latest times and
clamps closes to the block's opening). Hooks dropped before the queue take no number
and read no clock; a hook refused by a full queue leaves a gap, which is harmless. The
counter starts at `Repository.max_seq() + 1` at start-up, so numbering continues after
a restart.

**The queue** (`ccobs.server.ingest.HookQueue`) is bounded by
`CCOBS_HOOK_QUEUE_MAX_ITEMS` and `CCOBS_HOOK_QUEUE_MAX_BYTES` together, checked under one
lock; `offer` never blocks and returns False on a full queue. An item holds its share of
both bounds until the writer calls `task_done` with it, so the bounds cover what the app
holds in memory, items being written included.

**Start-up and shutdown.** The app's lifespan creates the engine for the database file,
runs `upgrade_to_head` before the app accepts requests (ADR 0026), builds the repository
and seeds the `seq` counter; at shutdown it disposes the engine. `create_app` itself
touches no file. Until the lifespan has run, hooks are dropped as `internal`; uvicorn
runs it before it accepts a connection.

**Nothing extra is exposed.** FastAPI's `/docs`, `/redoc` and `/openapi.json` are off.
uvicorn runs with `access_log=False` (the access log would record client addresses and
request lines), `proxy_headers=False` (no proxy sits in front of the app, so
`X-Forwarded-*` headers are never trusted) and `server_header=False` (the server
software and version are not advertised). The `Date` header stays: RFC 9110 asks an
origin server with a clock to send it, and it reveals nothing. Logging is configured on
the app's own loggers (`ccobs.*`) and on Alembic's (at `WARNING` or above), never on the
root logger: SQLAlchemy's engine logs statements and rows at `INFO` and `DEBUG`, and rows
hold `cwd` (ADR 0006), so not even `CCOBS_LOG_LEVEL=debug` lets them through.

**`GET /healthz`** answers 200 with `{"status": "ok"}`. It passes the `Host` guard like
every route, so the container's healthcheck (a later stage of milestone 2) calls it with
`Host: 127.0.0.1`.

**The metrics listener is separate.** `/metrics` is served on `CCOBS_METRICS_PORT` by a
listener of its own, decided in ADR 0028 (a later stage of milestone 2), not on this
app's routes.

### Consequences

- Good, because the hook route costs well under a millisecond in the tests (a p99 of
  about 0.3 ms inside the app over 500 real payloads) and the app never answers a hook
  with anything but an empty 204, so a hook never becomes a notice while the app is up.
- Good, because a web page can neither read the app (DNS rebinding meets the `Host`
  allowlist) nor post hooks through the browser (the `Content-Type` rule needs a
  preflight that no CORS header lets through).
- Good, because memory stays bounded by count and by bytes, and a slow database never
  slows Claude Code: hooks are dropped and counted instead.
- Bad, because a full queue, a crash or a stop loses the hooks still in memory; the
  counters make losses visible, and the reducer tolerates missing events (ADR 0024).
- Bad, because one worker caps the app at one process; one machine's hooks are far
  below what one event loop serves.
- Bad, because the uniform 204 hides misuse from a client that is not Claude Code: a
  wrong method or content type looks like success. The drop counters are where to look.
- Neutral, because a client without a `Host` header (HTTP/1.0) is refused; uvicorn
  already refuses HTTP/1.1 requests without one.

### Confirmation

- `tests/server/test_guards.py` checks the `Host` allowlist (allowed and refused
  values, a missing or repeated header), the 400 on other routes and the 204 on the hook
  route without reading the body, `/healthz` with `localhost:4300`, `127.0.0.1:4300`,
  `[::1]:4300` and `LOCALHOST`, that `/docs`, `/redoc` and `/openapi.json` are not
  served, that no response carries an `Access-Control-*` header (a preflight included),
  that a websocket with a refused `Host` is closed with 1008, the media type rule and the
  body reader's cap.
- `tests/server/test_hook_endpoint.py` checks the empty 204 without a content type for
  every case above, a full queue by count and by bytes, an exception in the handler,
  every other method, `/hooks/` with a good and a bad `Host`, a 404 without redirect on
  `/healthz/`, the drop counter of each, the queue's bounds and thread safety,
  `received_at_ms` from the injected clock and read only once the body is in, `seq` from
  1 and after the stored maximum and a restart, the migrations in the lifespan, a p99
  under 50 ms measured inside the app over 500 requests, that the route never calls
  `json.loads`, and that no log line holds a body, a canary or a path.
- `tests/server/test_server_main.py` checks that bad settings exit with code 2 and the
  message on stderr, that `uvicorn.run` gets one worker, no access log, no proxy headers
  and no server header, and that SQLAlchemy's loggers stay at `WARNING` or above with
  the root logger untouched, at the `info` and `debug` levels alike.
- The `reviewer` subagent checks that no route adds CORS headers or a non-2xx answer on
  the hook route.

## More information

- Claude Code [hooks docs](https://code.claude.com/docs/en/hooks): HTTP response
  handling.
- [ASGI HTTP spec](https://asgi.readthedocs.io/en/latest/specs/www.html) and uvicorn's
  [settings](https://www.uvicorn.org/settings/) (`access_log`, `proxy_headers`,
  `server_header`, `date_header`, `workers`).
- RFC 9110: [Host and `:authority`](https://www.rfc-editor.org/rfc/rfc9110#name-host-and-authority),
  [Date](https://www.rfc-editor.org/rfc/rfc9110#name-date); RFC 9112:
  [message body length](https://www.rfc-editor.org/rfc/rfc9112#name-message-body-length).
- The writer thread, the metrics listener and the container image are later stages of
  milestone 2.
- Revisit if the app ever needs more than one process, or if Claude Code starts sending
  hooks concurrently within one session.
