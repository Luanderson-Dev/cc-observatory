---
status: proposed
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# State-changing routes: alert settings over a guarded POST, without a CSRF token

## Context and problem statement

Until milestone 5 every route but the hook route served only `GET`, and the UI changed
nothing on `GET` ([ADR 0034](0034-browser-hardening-csp-fetch-metadata-and-response-headers.md)).
[ADR 0042](0042-alert-tables-settings-precedence-and-retention.md) adds alert settings
that the settings screen saves over the environment's, and Reset; ADR 0034 left the CSRF
protection of such routes to milestone 5. The app has no login and binds to `127.0.0.1`
([ADR 0001](0001-local-single-machine-scope.md)), but any page open in the user's browser
can send requests to localhost. How do the settings routes accept a change from the
app's own pages, and from no other page?

## Decision drivers

- The hook route never changes behaviour: its empty 2xx, no added header, exempt from
  every guard (CLAUDE.md, [ADR 0011](0011-hook-delivery-http-plus-async-sessionstart.md)).
- The strict CSP stays as it is (ADR 0034): own scripts only, `form-action 'none'`.
- There is no session, no cookie and no login to tie a token to.
- The `Host` allowlist already defeats DNS rebinding
  ([ADR 0017](0017-grafana-access-and-dns-rebinding.md), ADR 0034), and the app sends no
  CORS headers.
- A save must never echo a submitted value in an error or a log line, and must not block the event loop on the
  single writer thread ([ADR 0026](0026-app-database-schema-migrations-and-retention.md)).

## Considered options

- **Synchronizer token.** A random token in the page, checked on each POST. The OWASP
  CSRF cheat sheet's primary defence, but it needs server state or a secret per process,
  a token in every fragment, and a rule for tokens that outlive a restart.
- **Double-submit cookie.** A cookie plus the same value in a header or field. No server
  state, but it brings the app's first cookie, and a cookie is shared by every port of
  `localhost`, so another local app (Grafana, a dev server) could plant it.
- **Fetch Metadata + Origin + a custom header.** No token: the browser's own headers
  and a header only same-origin JavaScript can add without a preflight.

## Decision outcome

Chosen option: "Fetch Metadata + Origin + a custom header", because on a local app with
no CORS, no cookie and a `Host` allowlist it gives the same guarantee as a token with no
state, no cookie and no change to the pages beyond htmx's `hx-post`. This ADR extends
ADR 0034, which stays in force. The approach is the maintainer's decision for milestone
5; the exact order of the checks, the 415, the 503 and the 5 s timeout were set during
stage 9's implementation, pending the maintainer's review, which is why this ADR is
proposed.

**The routes.** `POST /ui/alerts/settings` (save the fields the form holds whose value,
parsed by its type, differs from the one in force; the form posts every field, and a
field submitted unchanged writes nothing, so a saved value stays saved and an
environment value keeps following `CCOBS_ALERT_*`, ADR 0042's precedence; with no
change at all the writer is not called and the fragment says "No changes to save.") and
`POST /ui/alerts/settings/reset` (`field=<name>` resets one setting, no field resets
all) are the only routes besides the hook route that take a POST. `GET
/ui/alerts/settings` renders the settings fragment and follows the `GET` rules. Every
other route still serves only `GET`, and any other method, on these paths too, still
gets the 405.

**The checks.** `UnsafeMethodGuard`, innermost of the middlewares (after `HostGuard`,
`SecurityHeaders` and `FetchMetadataGuard`), checks every POST to those two paths, in
this order, before the body is read:

1. `Sec-Fetch-Site`, when sent, is exactly one `same-origin` (any case). `cross-site`,
   `same-site` (another `localhost` port such as Grafana's), `none` and unknown values
   get 403. A browser without Fetch Metadata still has to pass 2 and 3.
2. `Origin` is exactly one value equal to `<scheme>://<Host>` (any case), where `Host`
   has passed the allowlist. Missing, `null`, another port, another scheme or another
   name get 403.
3. `HX-Request: true`, exactly once. A page of another origin can add a custom header
   only after a CORS preflight, which fails here because no response carries CORS
   headers. A plain HTML form, even one on our own pages, cannot send it, and the CSP's
   `form-action 'none'` blocks such forms anyway. Missing or any other value: 403.
4. `Content-Type` is exactly one `application/x-www-form-urlencoded`, parameters
   allowed: what htmx's `hx-post` sends. Anything else gets 415, since the request has
   already proved it comes from our own pages; 415 says what to fix.

The 403 body is `forbidden`, the 415 body `unsupported media type`, both short, plain
text, with the security headers, and never quoting the request. The guard skips the
hook route, every other method and every other path.

**Why this suffices.** A cross-origin page can still send a "simple" POST, but it can
neither set `HX-Request` nor forge `Origin`, and a modern browser reports where the
request comes from in `Sec-Fetch-Site`. DNS rebinding, the way a foreign page would
become "same-origin", is defeated earlier by the `Host` allowlist. The app sends no
`Access-Control-Allow-*` header, so no preflight ever succeeds. Requiring `Origin`
refuses non-browser clients too (curl must set it), which is fine: the routes exist for
the UI, and any local process can already write the database file.

**Saving.** The route reads at most 4 KiB of form, parses each setting by its type
(`true` or `false` for a switch, the last value winning so that a hidden `false` before
a checkbox's `true` sends an unchecked box; digits for whole numbers; digits with an
optional decimal part for dollar amounts), and hands the change to the writer as a
control with a `concurrent.futures.Future`. The writer checks it on the effective
configuration with `validate_config` (through `apply_settings`), saves all the fields
in one transaction or none, puts the effective configuration in force for the next
alert tick, and settles the future with the saved values and any errors. Those values
are the ones read before the write with the change applied in memory, never a second
read: once the write commits, nothing else can fail the change, so a 503 always means
that nothing was saved. A change that turns `repeated_failures` on, or raises its window
or its count, reloads the failure windows from the database in the same writer step, as
a restart does ([ADR 0042](0042-alert-tables-settings-precedence-and-retention.md)), so
failures recorded while the rule was off or narrower count at once; if that read
fails, the alert state is dropped and the next tick rebuilds it. On any invalid
field nothing is saved and the fragment comes back with 200 (htmx swaps only 2xx by
default), each error naming the field and its range and never the value, and each
submitted field refilled with the text typed (autoescaped), so the user sees what to fix;
the rule against echoing a value covers messages and logs, not the user's own input.
A save or reset that took answers with `HX-Trigger: ccobs:alerts`, the event the alerts
ping dispatches ([ADR 0044](0044-alerts-reach-the-browser.md)): it bubbles to body,
so the alerts page's history refetches and its empty state names the rules now on.
Errors, refusals and a save with no change send no event.

**503.** The route awaits the future with `asyncio.wrap_future`, so the event loop is
never blocked, for at most 5 s. With no answer in time, when 8 changes already wait
for the writer, when the writer stops first or when the write fails, the answer is a
503 fragment saying that the settings were not saved. On a timeout the future is
cancelled: a change the writer had not started is then never applied; one it was
already applying may still land, which the message covers ("reload to see the settings
in force").

### Consequences

- Good, because there is no token, cookie or server state, and pages need no change
  beyond `hx-post`.
- Good, because every refusal happens before the body is read, and nothing a request
  sends reaches a response or a log line.
- Good, because the hook route and every `GET` route behave exactly as before.
- Bad, because the routes depend on browser behaviour (Origin on every POST, Fetch
  Metadata, CORS) rather than on a secret; a browser that sent a forged `Origin` would
  break the model, as it would break CORS itself.
- Bad, because a timed-out change the writer had already started may still be saved
  after the 503.
- Neutral, because non-browser clients must send `Origin` and `HX-Request` to change
  settings; there is no API for it.

### Confirmation

- `tests/server/test_guards.py`: `post_refusal`'s matrix (cross-site, same-site, `none`,
  missing or `null` Origin, another port, scheme or name, repeated headers, missing or
  wrong `HX-Request`, a plain form, wrong or repeated media types, and the order of the
  checks); the guard refuses without reading the body and checks only POSTs to its
  paths; the middleware order; every route still 405 on `PUT`, `PATCH` and `DELETE`, and
  on `POST` but the two settings routes, which give 403 without the headers; the hook
  route unchanged.
- `tests/server/test_alert_settings_routes.py`: the fragment's values and sources, the
  hx-post save, a partial save, a full form that saves only its changed field or
  nothing, `HX-Trigger: ccobs:alerts` on a save or reset that took and on no other
  answer, every type, validation errors without the value, the typed text refilled and
  nothing saved, reset of one and of all, the header matrix through the whole app, 415,
  413 and 400, and 503 on a writer timeout (the change cancelled) and on a full queue.
- `tests/server/test_writer_alert_settings.py`: the writer's save, reset, validation,
  reload, cancelled change, pending cap, failed write and stop; a committed change
  reported without a second read; the failure windows reloaded when the rule is turned
  on, its window or count raised, or the settings reloaded, rebuilt at the next tick
  when that read fails, and not read for a change that keeps or narrows the rule.
- `tests/test_domain_alerts_settings.py`: `apply_settings`, `setting_value` and
  `setting_error`.

## More information

- OWASP [Cross-Site Request Forgery Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html):
  Fetch Metadata headers, verifying the origin with standard headers, and custom request
  headers for AJAX/API.
- Stage 10 of milestone 5 builds the settings page around the fragment.
- Revisit if the app ever serves CORS, a login, or clients other than its own pages.
