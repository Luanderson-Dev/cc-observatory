---
status: accepted
date: 2026-10-16
decision-makers: Luanderson-Dev (maintainer)
---

# Server-rendered pages with htmx 2 and vendored, checksummed assets

## Context and problem statement

[ADR 0004](0004-web-app-stack.md) chose FastAPI, Jinja2, HTMX and SSE for the web UI and
vendored JS and CSS with no CDN, and left the vendored libraries, their versions and how
the pages are put together to milestone 3. The UI must work offline, never load anything
from another origin ([ADR 0001](0001-local-single-machine-scope.md)), stay small enough
for one maintainer to update by hand, and keep the app's HTTP surface easy to guard
([ADR 0027](0027-the-app-http-surface-and-hook-queue.md)). How are the pages rendered and
updated, which third-party files ship with the app, and how are they kept honest?

## Decision drivers

- Offline and same-origin only: no CDN, no web font service, no external URL in any file
  the browser loads.
- Every third-party file is traceable: where it came from, which version, which licence,
  and a checksum that a test verifies.
- Few moving parts on the client: no build step, no client-side router, no npm.
- Each URL returns one thing, so routes are simple to test and to guard, and responses
  can be cached by URL without `Vary` headers.
- Times read correctly in the user's time zone without the server knowing it.
- The theme choice applies before the first paint (no flash of the wrong theme) and
  stays inside the app.

## Considered options

For the pages and their updates:

- **Full pages plus dedicated fragment routes under `/ui/*`.** A page route renders the
  whole page; htmx swaps parts of it from fragment routes that return only that part.
  One response shape per URL.
- **One route per view that switches on the `HX-Request` header.** Fewer routes, but
  each URL returns two shapes, which needs `Vary: HX-Request` and makes caching and
  history restores return a fragment where a page was expected.
- **`hx-boost` and `hx-push-url` for client-side navigation.** Faster page changes, but
  htmx then owns history and scroll restoration, which brings back the fragment-versus-
  page problem above and is harder to test.

For the client library:

- **htmx 2.x (2.0.11, the npm `latest` tag).** Stable and small (52 KB minified),
  licensed 0BSD.
- **htmx 4.0.** Published under the npm `next` tag, not `latest`: not yet the stable line.
- **No library, hand-written fetch and swap.** No dependency, but we would rewrite what
  htmx already does and tests well (triggers, swaps, indicators).

For live updates:

- **Our own small `live.js`.** An `EventSource` that, on each event, asks htmx to refetch
  the affected fragment routes (ping and refetch); the HTML always comes from the
  fragment routes, never from the event stream.
- **The `htmx-ext-sse` extension.** It swaps HTML sent in the event stream. Ping and
  refetch needs none of that. Its package metadata declares no licence (`package.json`
  has no `license` field and GitHub detects no licence for its repository), although its 2.2.4
  tarball carries a 0BSD `LICENSE` file; one more vendored file for no gain.

For the theme:

- **A synchronous `theme.js` in `<head>` that reads `localStorage`** and sets
  `data-theme` on `<html>` before the first paint; with no stored choice the system
  preference applies through CSS.
- **A cookie read by the server.** Cookies are not isolated by port: a `localhost`
  cookie set by the app (port 4300) would also be sent to Grafana (port 4330) and to
  anything else on `localhost`.

For times:

- **Server-rendered text plus ISO 8601 UTC in `<time datetime>`,** with a small script
  that re-renders and ticks them in the browser's time zone. Without JavaScript the
  page still shows correct UTC times.
- **Client-side rendering only.** Empty times until the script runs, and nothing without
  it.

For the assets:

- **Vendored in the Python package (`ccobs/server/static/`)** with a `VENDOR.json`
  manifest (source, version, SHA-256, SPDX licence, licence file) and the licence texts
  next to them, served by the app from the installed package.
- **A CDN, or Google Fonts.** Breaks offline use and sends every page view to a third
  party.
- **The `@fontsource` packages for the fonts.** Convenient, but their files are
  unicode-range subsets of the upstream fonts, that is, modified versions.

## Decision outcome

Chosen options: "full pages plus dedicated fragment routes under `/ui/*`", "htmx 2.x",
"our own small `live.js`", "a synchronous `theme.js` with `localStorage`",
"server-rendered text plus ISO 8601 UTC" and "vendored in the Python package", because
together they keep one response shape per URL, no build step, no third-party origin and
a manifest a test can check, with the fewest third-party files.

- Pages are rendered by Jinja2 on the server. Fragment routes live under `/ui/*` and
  return only their fragment; no route switches on `HX-Request`.
- htmx is pinned to 2.0.11 (`static/vendor/htmx-2.0.11.min.js`, unmodified
  `dist/htmx.min.js` from the npm tarball, checked against its `dist.integrity`). The
  version is in the file name. Templates never use `hx-boost`, `hx-push-url` or
  `hx-confirm` (which calls `window.confirm`; confirmations use HTML modals, ADR 0004).
- Live updates: our own `live.js` with an `EventSource`; events only say what changed,
  and the browser refetches the fragments. No `htmx-ext-sse`.
- Theme: a synchronous external `theme.js` (no inline script) stores the manual choice
  in `localStorage`; `app.css` defines light tokens on `:root`, dark ones under
  `@media (prefers-color-scheme: dark)` for `:root:not([data-theme="light"])`, and again
  for `:root[data-theme="dark"]`, so a manual choice wins over the system.
- Times: the server renders a readable text and an ISO 8601 UTC `datetime`; the browser
  re-renders and ticks them in its own time zone.
- Fonts are self-hosted: Atkinson Hyperlegible Next (variable, upright and italic) and
  Atkinson Hyperlegible Mono (variable, upright), version 2.001, the unmodified woff2
  files from the `googlefonts/atkinson-hyperlegible-next` and
  `googlefonts/atkinson-hyperlegible-next-mono` repositories, pinned to a commit. They are
  SIL OFL 1.1, and neither `OFL.txt` declares a Reserved Font Name. The files are only
  renamed (lowercase, no brackets), which the OFL allows. About 126 KB for all three.
- Every third-party file under `static/vendor/` and `static/fonts/` has an entry in
  `static/VENDOR.json`; its licence text is in `static/licenses/`. `.gitattributes`
  turns off line-ending normalisation for vendored files and licence texts, and
  pre-commit's whitespace fixers skip them, so their bytes stay as published.
- The distribution's licence becomes `MIT AND OFL-1.1 AND 0BSD`, and the licence texts
  in `static/licenses/` are `license-files` in `pyproject.toml`, so the wheel's metadata
  lists them.
- The wheel ships `static/` and `templates/`: `uv_build` packages every file in the
  module directory, so the CLI-only install carries them too (a few hundred KB) without
  importing anything server-only.

### Consequences

- Good, because the UI works with no network and makes no request to another origin.
- Good, because every vendored file can be traced to its source and checked: a changed
  byte fails a test.
- Good, because each URL has one response shape, so the `Host` guard, tests and caching
  treat page and fragment routes alike.
- Bad, because updates are by hand, and Dependabot does not see vendored files: an
  update means a new file, a new `VENDOR.json` entry and its checksum (ADR 0004).
- Bad, because there are more routes (a page plus its fragments); mitigated by sharing
  templates between the page and its fragments.
- Neutral, because htmx 4 can be adopted once it reaches `latest`, with a new ADR if
  its behaviour changes what is decided here.

### Confirmation

`tests/server/test_static.py` checks that every file in `static/vendor/` and
`static/fonts/` is in `VENDOR.json` with a matching SHA-256 and an existing licence file;
that no static file or template contains an external `http(s)` URL (licence texts and
the `VENDOR.json` source fields aside) and the stylesheet loads only local files; that
our own scripts and templates call no `alert`, `confirm` or `prompt` and use no
`hx-confirm`, `hx-boost` or `hx-push-url`; that `app.css` has the light and dark token
blocks in the order above; that `.gitattributes` and `.pre-commit-config.yaml` leave
vendored bytes alone; and that the built wheel contains every file of `static/` and
`templates/`. The fragment routes, `live.js`, `theme.js` and the time script are
checked by the tests of the milestone 3 stages that add them.

## More information

- htmx: [docs](https://htmx.org/docs/), [npm](https://www.npmjs.com/package/htmx.org).
- Atkinson Hyperlegible Next:
  [repository](https://github.com/googlefonts/atkinson-hyperlegible-next) and
  [Mono](https://github.com/googlefonts/atkinson-hyperlegible-next-mono);
  [SIL OFL 1.1](https://openfontlicense.org).
- MDN: [`EventSource`](https://developer.mozilla.org/en-US/docs/Web/API/EventSource),
  [`prefers-color-scheme`](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-color-scheme).
- RFC 6265, section 8.5: cookies do not provide isolation by port.
- Extends [ADR 0004](0004-web-app-stack.md); the SSE event format and the read limits of
  the live routes follow [ADR 0033](0033-separate-writer-and-reader-engines-and-capped-reads.md)
  and are decided in the milestone 3 stage that adds them.
