---
status: accepted
date: 2026-10-16
decision-makers: Luanderson-Dev (maintainer)
---

# Browser hardening: a strict CSP, Fetch Metadata and response headers

## Context and problem statement

[ADR 0027](0027-the-app-http-surface-and-hook-queue.md) defends the app against web
pages in the user's browser with a `Host` allowlist, a JSON-only hook route and no CORS
([ADR 0001](0001-local-single-machine-scope.md)). The allowlist stops DNS rebinding,
but a page on any origin can still point an `<img>`, `<script>` or `<link>` at
`http://127.0.0.1:4300/...`: the browser sends an allowed `Host` and the request runs.
The page cannot read the answer, yet such no-cors requests can probe for a response, run
expensive reads, or hold the connection slots of the server-sent events stream that
milestone 3 adds, and `EventSource` itself can be pointed at the stream from another
page. Milestone 3 also serves HTML pages that show `cwd`, personal data
([ADR 0006](0006-privacy-metadata-only-by-default.md)), which a browser could cache or
frame. Grafana, on `localhost:4330`, is a page from the same site when the app is opened
as `localhost:4300`, and from another site when it is opened as `127.0.0.1:4300`; the
app treats both cases alike. Which response
headers and request checks keep foreign pages away from the app without touching the
hook route?

## Decision drivers

- Hooks never interfere: the hook route keeps its neutral empty 204, with no extra
  header, whatever the request says (CLAUDE.md,
  [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)). Claude Code sends no
  `Sec-Fetch-*` headers, and the route's latency budget stays under 50 ms.
- A page on another origin, including another `localhost` port, can neither load our
  responses nor spend our resources, beyond opening a page the user can see.
- A link (from Grafana, a chat or a bookmark) and a typed URL still open the UI.
- Clients that are not browsers (curl, Prometheus, the tests) keep working.
- Pages that carry personal data are never stored by the browser.
- No new dependency; pure ASGI, as for the `Host` guard.

## Considered options

For the response headers:

- **A strict Content Security Policy plus `nosniff`, no referrer, CORP and COOP.** Only
  our own scripts, styles, fonts and images, no framing, no `<base>`, no form
  submission; responses cannot be loaded by another origin.
- **No headers, relying on the `Host` allowlist.** Leaves no-cors loads, framing and
  caching open.

For foreign requests:

- **Fetch Metadata: refuse `cross-site` only.** Simple, but `localhost` on any other
  port is `same-site`, since a site ignores the port: any local page, a dev server or
  something served on another port, would pass.
- **Fetch Metadata: refuse `cross-site` and `same-site` unless the request is a
  top-level `GET` navigation.** Grafana's links still open pages (a navigation);
  Grafana or any other local page cannot embed, fetch or stream from the app.
- **Check `Origin`.** Simple no-cors `GET`s (an `<img>`) send no `Origin`, so it misses
  the case that matters.
- **CSRF tokens.** The UI is read-only in milestone 3: there is no state to change, so
  tokens would protect nothing yet.

## Decision outcome

Chosen options: "a strict Content Security Policy plus `nosniff`, no referrer, CORP and
COOP" and "Fetch Metadata: refuse `cross-site` and `same-site` unless the request is a
top-level `GET` navigation", because together they close no-cors loads, embedding,
event streams from foreign pages and browser caching of personal data, while links,
typed URLs and non-browser clients keep working.

- `SecurityHeaders` (`ccobs.server.guards`) adds to every HTTP response that the app's
  routes and guards produce, but the hook route's (an unhandled 500 from Starlette's
  error middleware, which sits outside every user middleware, carries none; its body is
  generic):
    - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
      font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none';
      form-action 'none'; frame-ancestors 'none'`;
    - `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
      `Cross-Origin-Resource-Policy: same-origin`,
      `Cross-Origin-Opener-Policy: same-origin`;
    - `Cache-Control: no-store`, except under `/static/`, whose vendored files hold no
      personal data and get their own cache policy in the stage that mounts them.
  A header that a route already set is kept, never duplicated. The `Host` guard's 400
  carries the same headers, since the guard answers before the middleware runs.
- The policy has no `'unsafe-inline'` and no `'unsafe-eval'`. Templates use no inline
  `<script>`, no `<style>` element and no `style` attribute, and htmx's own indicator
  `<style>` is turned off with `includeIndicatorStyles: false`. Without `'unsafe-eval'`,
  htmx's eval-based features are unavailable: `hx-on:*` attributes, event filters in
  triggers (`hx-trigger="click[...]"`) and `js:` values in `hx-vals` and `hx-vars`; the
  templates use none of them, and htmx is told so with `allowEval: false`. The base
  template sets both options in htmx's `<meta name="htmx-config">` (milestone 3,
  stage 7).
- `FetchMetadataGuard` lets a request through when it has no `Sec-Fetch-Site` (not a
  browser that sends Fetch Metadata), or when the value is `same-origin` (our own pages)
  or `none` (a typed URL or a bookmark). With `same-site` or `cross-site`, only a `GET`
  with `Sec-Fetch-Mode: navigate` and, when sent, `Sec-Fetch-Dest: document` passes.
  Anything else, a repeated header or an unknown value included, gets a 403 with the
  short body `forbidden`, which echoes nothing; a websocket gets close code 1008 before
  the handshake. This covers the UI, the API, the static files and the event stream.
- The hook route (`/hooks` and `/hooks/`) is outside both middlewares.
- Order, outermost first: `HostGuard`, `SecurityHeaders`, `FetchMetadataGuard`, then
  the routes. Every route but the hook route serves only `GET`; any other method gets
  FastAPI's 405, with the security headers.
- The metrics listener ([ADR 0028](0028-app-metrics-and-their-listener.md)) is a
  separate server on an unpublished port, not browser-facing, and is unchanged.

### Consequences

- Good, because a page on any other origin, `localhost` ports included, can no longer
  load a response as an image, script, style or frame, nor fetch or stream from the
  app; only a navigation the user sees remains.
- Good, because the UI's personal data is never written to the browser's cache, and no
  referrer leaks a session URL to a linked site.
- Good, because an injected inline script would not run.
- Bad, because a foreign page can still navigate the user to the app, a top-level `GET`;
  it reads nothing from it, and the UI changes nothing on `GET`.
- Bad, because browsers that send no Fetch Metadata (very old ones) get only the
  headers' protection, CORP included where they support it.
- Neutral, because milestone 5 adds `POST` routes for alerts and decides CSRF
  protection then, in a new ADR; Fetch Metadata already refuses foreign form posts,
  which are navigations by `POST`.

### Confirmation

`tests/server/test_guards.py` checks the headers on pages, the API, 404, 405, 422, the
`Host` guard's 400 and the 403, and pins that an unhandled 500 carries none and a generic
body; that `/static/` paths get no `no-store` and that a
header a route set is neither overwritten nor duplicated; that foreign no-cors, cors,
frame and form requests get 403 and foreign websockets 1008, on the API, static paths,
`/ui/stream` and unknown paths; that foreign top-level `GET` navigations, same-origin
requests, typed URLs and requests without Fetch Metadata pass; that `/hooks` and
`/hooks/` with `Sec-Fetch-*` headers still get the empty 204 with no added header; the
middleware order; and that every route but the hook route answers 405 to `POST`, `PUT`,
`PATCH` and `DELETE`. The hook route's p99 test in `tests/server/test_hook_endpoint.py`
runs through all the middlewares.

## More information

- W3C [Fetch Metadata Request Headers](https://www.w3.org/TR/fetch-metadata/) and
  web.dev's [resource isolation policy](https://web.dev/articles/fetch-metadata).
- MDN: [Content-Security-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy),
  [Cross-Origin-Resource-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cross-Origin-Resource-Policy),
  [Cross-Origin-Opener-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cross-Origin-Opener-Policy).
- htmx [configuration](https://htmx.org/docs/#config) (`includeIndicatorStyles`, `allowEval`).
- Extends [ADR 0001](0001-local-single-machine-scope.md),
  [ADR 0006](0006-privacy-metadata-only-by-default.md) and
  [ADR 0027](0027-the-app-http-surface-and-hook-queue.md); Grafana's own defences are in
  [ADR 0017](0017-grafana-access-and-dns-rebinding.md).
