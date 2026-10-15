---
status: accepted
date: 2026-10-14
decision-makers: Luanderson-Dev (maintainer)
---

# Grafana: anonymous Editors, no login, answering only to `localhost`

## Context and problem statement

Grafana is the one part of the stack published for the browser, on `127.0.0.1:4330`
([ADR 0018](0018-ports-networks-and-exposure.md)). The stack has no login
([ADR 0001](0001-local-single-machine-scope.md)), but Grafana's defaults assume one: a
login form, basic auth, and an `admin` user with the password `admin`, created at the
first start. Loopback keeps other machines out, not the web pages open in the same
browser. A page using DNS rebinding points its own host name at `127.0.0.1` and then
reads Grafana as same-origin, unless Grafana checks the `Host` header, which it does not
by default ([DNS rebinding](https://en.wikipedia.org/wiki/DNS_rebinding)). And any page
can send a cross-site request without a CORS preflight if it uses a simple content type
such as `text/plain`
([CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)), while Grafana
checks the `Origin` of a write only when the request carries its login cookie, which an
anonymous visitor never has. Grafana also calls grafana.com and other
sites by default, against [ADR 0006](0006-privacy-metadata-only-by-default.md) ("No
outbound traffic"). ADR 0001 and ADR 0018 leave Grafana's protection to milestone 1.
Who can use Grafana, with which rights, and how is it kept from the pages in the same
browser?

## Decision drivers

- No login (ADR 0001): no password to set, leak or forget, and nothing to do before the
  first page loads.
- The user explores the data and keeps their own dashboards: Explore, editing, saving.
- Datasources and server settings come from the repository, not from the UI.
- A web page in the user's browser can neither read Grafana nor change it.
- No outbound traffic (ADR 0006).
- The same behaviour on Linux, macOS and Windows, with the default ports of ADR 0018.

## Considered options

- **Anonymous Viewer, with an admin login for changes.** Anyone reads; an admin account
  edits. A password to manage (from `.env`, or generated and shown once), and editing a
  dashboard means logging in first; the default `admin`/`admin` is a well-known password
  unless the stack replaces it.
- **Anonymous Editor, with no login form, no basic auth and no admin user.** Anyone who
  opens Grafana reads, queries and saves dashboards, and nobody administers it.
- **Anonymous Admin.** No login and no limits: whatever reaches the port can change the
  datasources, create users and API tokens, and change server-wide settings.
- **A login for everyone (Grafana's default).** Against ADR 0001, and it starts with the
  `admin`/`admin` account.

Against DNS rebinding:

- **Grafana's `enforce_domain`, with `localhost` as its domain.** Grafana redirects any
  other `Host` to its root URL; one host name only.
- **A reverse proxy with a `Host` allowlist in front of Grafana.** It could also accept
  `127.0.0.1`, and by checking Fetch Metadata (refusing a request whose
  `Sec-Fetch-Site` is `cross-site`) it would also stop the blind cross-site GETs that
  Grafana's own settings let through (see the consequences), but it is one more service,
  image and configuration to keep up to date.
- **Nothing beyond loopback.** Simplest, and any page can read Grafana through DNS
  rebinding.

## Decision outcome

Chosen option: "Anonymous Editor, with no login form, no basic auth and no admin user",
with "Grafana's `enforce_domain`, with `localhost` as its domain", because together they
keep ADR 0001's no-login model, let the user save dashboards with no account, leave
nobody who can administer Grafana from the browser, and keep web pages out without
another service. A Viewer login means a password to manage, Admin lets anything that
reaches the port take over Grafana, and a login for everyone breaks ADR 0001. A reverse
proxy would add a service, its image and its configuration where a few Grafana settings
cover DNS rebinding, cross-origin requests and the legacy datasource proxy; what it
would add beyond them, refusing blind cross-site GETs through Fetch Metadata, is left as
a residual risk (see the consequences).

The settings are environment variables of the Grafana service in `compose.yaml`, which
explains each one; `tests/test_grafana_config.py` lists them.

- **Access.** `auth.anonymous.enabled` with `org_role = Editor`, in Grafana's single
  organization. `auth.disable_login_form`, `auth.basic.enabled = false` and
  `security.disable_initial_admin_creation`: no admin user is created, and neither the
  login endpoint nor basic auth accepts a password, so no account exists or can be used.
  `auth.disable_signout_menu` hides the menu entry that would lead to the login page.
  The anonymous Editor can query every datasource and save dashboards, folders and
  annotations, but cannot create, change or delete datasources, install plugins, or see
  or create users, service accounts and organizations.
- **One host name.** `server.domain = localhost`, `server.enforce_domain = true` and
  `server.root_url = http://localhost:${CCOBS_GRAFANA_PORT:-4330}/`. Any other `Host`,
  `127.0.0.1` and `[::1]` included, gets a 301 to the same path under the root URL, so a
  page using DNS rebinding, which sends the attacker's host name, reads only redirects.
  Grafana is opened at `http://localhost:4330`, and links to it use `localhost`, the
  app's included (from milestone 3). A few routes answer any `Host`, by Grafana's
  design: `/api/health` (the healthcheck's target, so `curl -f` never mistakes a
  redirect for health), `/healthz`, the static files under `/public`, and `/bootdata`,
  the page's data, which answers another host name with a 204 that only names the
  domain. None of them reads Claude Code data, and `/metrics`, the one that showed
  Grafana's own counters, is off (below).
- **No cross-site request from another origin.** `security.csrf_always_check = true`:
  Grafana refuses (403) any request other than HEAD, OPTIONS and TRACE whose `Origin`
  names another host, a GET included (`csrf.go`'s `safeMethods` is only those three),
  even without a login cookie. Browsers send `Origin` on every CORS-mode request, that
  is any cross-origin `fetch` or XHR, simple or not, and on every cross-origin form
  POST; `null`, from sandboxed frames, is refused too. Only no-cors GETs lack it: an
  `<img>`, `<script>` or `<link>`, a top-level navigation, or a `fetch` with
  `mode: "no-cors"`. Such a simple cross-site GET passes, which is why the two settings
  below are also needed. A request with no
  `Origin` from the same machine (curl, local scripts) passes as well. Grafana's JSON
  API also refuses a body that is not `application/json`.
- **The legacy datasource proxy is off.** Grafana's `/api/datasources/proxy/...` route
  forwards a request to the datasource's own HTTP API. A simple cross-site GET to it
  (no `Origin`, so the check above lets it through; `Host` is `localhost`, so
  `enforce_domain` lets it through; the anonymous Editor has `datasources:query`) would
  otherwise reach a store: Loki 3.7.8 serves `GET /flush` and `GET /ingester/shutdown`,
  which stop its ingester, after which its `/ready` fails, Docker marks it unhealthy
  without restarting it, and the collector drops events. When no plugin route matches,
  `ds_proxy.go`'s `validateRequest` refuses DELETE, PUT and POST for Prometheus and Loki
  but forwards GET, and forwards any method for Tempo.
  `security.data_source_proxy_whitelist = 127.0.0.1:9` (a host:port no datasource uses)
  makes `checkWhiteList()`, the first step of `validateRequest`, answer 403 to every
  proxy request, of any method and path, before it is forwarded. The datasources are
  queried through `/api/ds/query` and `/resources`, which do not use this proxy.
- **The resource routes reach no store-changing endpoint.** The route
  `/api/datasources/uid/<uid>/resources/*` (which `datasources:query` also allows, and a
  simple GET reaches) is not covered by the whitelist. Observed against the running
  stack: Loki's and Tempo's datasource plugins forward only an allowlist of read paths
  and answer 404 to any other, even a read the store serves itself (so `/flush`,
  `/ingester/shutdown` and the like are unreachable). Prometheus's plugin forwards raw
  paths, but its lifecycle and admin APIs are off (its flags set neither
  `--web.enable-lifecycle` nor `--web.enable-admin-api`): `/-/quit` and `/-/reload` are
  registered for POST and PUT only, so a GET gets 405 whatever the flags, and with the
  flag off a POST or PUT would get 403; the admin API's delete and snapshot endpoints
  are refused while its flag is off. Grafana rejects `..` or `%2e%2e` in the resource
  path (400, `CleanRelativePath`). The app plugin routes,
  `/api/plugins/<id>/resources/*` and `/api/plugin-proxy/<id>/*`, also answer a GET and
  are not covered by the whitelist, but no app plugin is installed or enabled (the
  preinstall is off). So a simple cross-site GET to the route can make Grafana run a
  read, but it cannot stop or wipe a store, and the page that sent it cannot read the
  answer: the browser hides a cross-origin response from the page, Grafana sends no
  CORS headers, and DNS rebinding gets only redirects.
- **`/metrics` is off.** `metrics.enabled = false`. Grafana's own Prometheus metrics,
  nothing scrapes them, and the endpoint was the widest route outside the `Host` check:
  a DNS-rebinding page could read Grafana's usage (`grafana_stat_*`) from it. With it
  off the path answers like any unknown one (the app shell, 404).
- **No outbound traffic** (ADR 0006), each default checked in Grafana 13.2.4's
  `conf/defaults.ini` and in its logs on a network with no route out: usage reporting,
  the update checks for Grafana and its plugins (the plugin one also fetches the Angular
  detection patterns), feedback links, the news feed, Gravatar, external snapshots, the
  plugin signing key download, the plugin preinstall at every start and its updates,
  plugin installs from the catalog, and Cloud migration are off.
  `grafana_com.api_url = http://127.0.0.1:9/api` is the base URL of the grafana.com API
  calls, both what the UI asks for through Grafana's `/api/gnet` proxy (the plugin
  catalog, dashboard templates, community dashboards) and the server's plugin calls
  (plugin update checks, the signing key, the preinstall and the Angular patterns, which
  read `GrafanaComAPIURL` in `dynamic.go`), so a closed port on Grafana's own loopback
  is also their safety net if a check above is missed or reset upstream; those pages
  show errors instead. Grafana's own update check is not covered: it calls a fixed URL
  (`https://grafana.com/api/grafana/versions/stable`, in `updatemanager/grafana.go`), so
  only `analytics.check_for_updates = false` keeps it off. Not covered: the Geomap
  panel's default map loads tiles from CARTO in the browser, and no bundled dashboard
  has one.
  [SECURITY.md](../../SECURITY.md) states the rule.
- **Unused features off.** `unified_alerting.enabled = false`, since alerts are the
  app's job ([ADR 0007](0007-alerts-browser-and-discord.md)), and
  `public_dashboards.enabled = false`, which would share a dashboard with anyone who has
  its link. `plugins.disable_plugins` keeps Grafana from loading the backend datasource
  plugins bundled with Grafana 13.2.4 that no provisioned datasource uses
  (Elasticsearch, Google Cloud Monitoring, Grafana Pyroscope, InfluxDB, Jaeger,
  Microsoft SQL Server, MySQL, OpenTSDB, PostgreSQL and Zipkin). With the datasources
  provisioned read-only and no admin, none of them can be used, and Grafana runs each
  one as a process of its own, outside `GOMEMLIMIT`
  ([ADR 0020](0020-compose-conventions.md)), so only the Prometheus, Loki and Tempo
  plugins run. The core datasources that run inside Grafana's server process
  (CloudWatch, Azure Monitor, Graphite, TestData and others) are left alone.
- **Provisioning.** The Prometheus, Loki and Tempo datasources are provisioned from
  `stack/grafana/provisioning` with fixed UIDs (`prometheus`, `loki`, `tempo`) and
  `editable: false`, so with no admin they change only in the repository. Tempo is
  always provisioned; without the `traces` profile its queries fail.

### Consequences

- Good, because there are no credentials at all: Grafana opens straight to its pages,
  and there is no default password to change.
- Good, because the user explores and saves dashboards without an account.
- Good, because nobody can change the datasources, plugins, users or server settings
  from the browser: they come from the repository.
- Good, because a page using DNS rebinding reads only redirects, a cross-site request
  from another origin is refused even as a GET, and the legacy datasource proxy that a
  simple cross-site GET could use to stop a store is closed.
- Bad, because anything that can reach the port, any local process or any user of a
  shared machine, reads all the data and edits the dashboards, as ADR 0001 accepts;
  [SECURITY.md](../../SECURITY.md) documents this trust model.
- Bad, because a simple cross-site GET to `/api/datasources/uid/<uid>/resources/*`
  carries no `Origin` and still makes Grafana query the datasource. The page cannot
  read the answer and cannot change or stop a store (the finding above), but it can
  send expensive queries blindly, for example a long-range PromQL query, and load
  Prometheus up to its memory limit, after which Docker restarts it
  ([ADR 0019](0019-restart-unless-stopped.md)). Closing that path would need a proxy
  that checks Fetch Metadata in front of Grafana, which this milestone does not build.
- Bad, because nobody can administer Grafana from the UI: no plugin installs, user
  management or server settings, even for the machine's owner. Changes go through
  `compose.yaml` and `stack/grafana`.
- Bad, because only `http://localhost:<port>` works: `127.0.0.1` redirects there, and a
  client that resolves `localhost` only to `::1` fails, since the port is published on
  IPv4 loopback (ADR 0018). Behind a reverse proxy or tunnel, which ADR 0001 rules out,
  Grafana would redirect to `localhost`.
- Bad, because Grafana 13.2 logs at every start that an anonymous role other than
  Viewer is deprecated ("only viewer role is supported"). It still applies the role
  (`pkg/services/anonymous/anonimpl/client.go`), and the documentation still lists
  Editor, but a later release may make anonymous visitors Viewers, who cannot save
  dashboards. The integration test that reads the visitor's role fails on the
  Dependabot pull request that brings such a release.
- Bad, because pages served by other local software on another `localhost` port pass the
  `Origin` check, which compares host names only; local software is trusted (ADR 0001).
- Neutral, because Grafana logs some of the requests its access control denies (those on
  organizations and dashboards) at error level, so those lines in its logs are not
  faults.
- Neutral, because a Grafana update that bundles another backend datasource plugin has
  to add it to `GF_PLUGINS_DISABLE_PLUGINS`; the integration test
  `test_only_the_provisioned_datasources_run_a_plugin_process` fails on the Dependabot
  pull request that brings such a release until it does.

### Confirmation

- `tests/test_grafana_config.py` checks every access, browser, outbound, feature and
  plugin setting with its value, that Grafana has no other setting, that the comments in
  `compose.yaml` explain each outbound one and the disabled plugins, that only plugins
  no provisioned datasource uses are disabled, that `root_url` uses the published port,
  and that there are no admin credentials; that the proxy whitelist is a non-empty list
  of host:port entries that match no datasource's host:port; and, for the datasources,
  the UIDs, types, `editable: false`, proxy access, URLs on the backend network, no
  credentials, no external URLs, and links between Loki and Tempo that match their
  configs.
- `tests/test_compose.py` checks the pinned image, the port on loopback, the `edge` and
  `backend` networks, the read-only provisioning directory, the named volume, the
  healthcheck, the memory settings, and that only the collector and Grafana publish
  ports.
- `tests/integration/test_stack_grafana.py`, against a running Grafana: the anonymous
  visitor is an Editor who can query and save dashboards; administration requests are
  refused; logging in fails and Grafana's own database holds no user and no admin; other
  host names get a redirect; a cross-site request is refused as a GET too, same-origin
  writes work, the legacy datasource proxy answers 403 for every datasource (on a
  harmless read path, so no store is reached, and Loki stays healthy) and `/metrics` is
  not served; the resource routes of Loki and Tempo answer 404 to a read their store
  serves but their plugin's allowlist leaves out (`/config`, `/status/config`), which
  fails on the Grafana update that drops an allowlist; no app plugin is enabled; the
  only plugin processes are those of the provisioned datasources, which fails on a
  Grafana update that bundles another backend plugin; public dashboards and Grafana
  Alerting are off; and each datasource is healthy and returns a session replayed
  through the collector.
- `tests/integration/test_stack_no_outbound.py`, against the running stack: Grafana
  applied every setting `compose.yaml` gives it, the outbound settings are off, the
  background jobs that call grafana.com never ran, avatars are not fetched and the
  grafana.com proxy goes nowhere.
- Prometheus's lifecycle and admin APIs stay off:
  `tests/test_prometheus_config.py::test_no_flag_opens_admin_lifecycle_or_remote_write_endpoints`
  checks its flags in `compose.yaml`, and
  `tests/integration/test_stack_metrics.py::test_prometheus_runs_with_the_receiver_its_features_and_retention`
  the flags of the running Prometheus.
- The `reviewer` subagent checks that no change adds a login, an admin user, an
  anonymous Admin role, or a setting that calls out.

## More information

- Grafana: [configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/)
  (`enforce_domain`, `root_url`, `csrf_always_check`, `disable_initial_admin_creation`,
  `[analytics]`, `[plugins]`),
  [anonymous authentication](https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/anonymous-auth/)
  and [provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
- Grafana 13.2.4's source: the `Host` check in
  [`validate_host.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/middleware/validate_host.go),
  the paths outside it in
  [`http_server.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/api/http_server.go),
  the `Origin` check (`safeMethods` is HEAD, OPTIONS, TRACE) in
  [`csrf.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/middleware/csrf/csrf.go),
  the datasource proxy's `validateRequest` and `checkWhiteList` in
  [`ds_proxy.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/api/pluginproxy/ds_proxy.go),
  the deprecation warning in
  [`setting_anonymous.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/setting/setting_anonymous.go)
  and the grafana.com proxy in
  [`grafana_com_proxy.go`](https://github.com/grafana/grafana/blob/v13.2.4/pkg/api/grafana_com_proxy.go);
  Loki 3.7.8's `GET /flush` and `GET /ingester/shutdown` in
  [`modules.go`](https://github.com/grafana/loki/blob/v3.7.8/pkg/loki/modules.go).
- Revisit if Grafana drops the anonymous Editor role (the choice is then a Viewer, or a
  login, in a new ADR), if a central mode is planned (ADR 0001), if Grafana removes the
  `data_source_proxy_whitelist` key or its datasource plugins start using the legacy
  proxy (the resource routes would have to be checked again, and the block moved), or if
  Grafana gains a setting for its grafana.com proxy.
