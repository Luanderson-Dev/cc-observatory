# Security policy

## Supported versions

cc-observatory is pre-1.0 software. Only the latest release receives security fixes;
fixes are not backported to earlier releases.

| Version          | Supported |
| ---------------- | --------- |
| Latest release   | Yes       |
| Earlier releases | No        |

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting.
Never put vulnerability details in public issues, pull requests or discussions.

1. Open the repository's **Security** tab.
2. Click **Report a vulnerability**, or go straight to
   <https://github.com/Luanderson-Dev/cc-observatory/security/advisories/new>.
3. Fill in the form. Only you and the maintainer can see the report.

If the **Report a vulnerability** button is missing or you cannot use it, open a public
issue titled `Security contact request` with no details about the vulnerability. The
maintainer will then open a private channel with you (a draft security advisory) where
you can share the details.

Please include:

- the cc-observatory version (`ccobs --version`) or commit, your operating system, and
  your Docker and Claude Code versions;
- the affected part: the app, the `ccobs` CLI, the compose stack, a bundled
  configuration (OpenTelemetry Collector, Prometheus, Loki, Tempo or Grafana), or the
  changes that `ccobs setup` makes to Claude Code's settings;
- steps to reproduce, or a proof of concept;
- the impact as you understand it, and a suggested fix if you have one.

Remove personal data before you attach logs, hook payloads or screenshots: the paths in
them contain your OS username.

**What to expect.** cc-observatory has a single maintainer, so responses are best
effort. The aim is to acknowledge a report within a week and to keep you informed while
it is investigated. A confirmed vulnerability is fixed in a new release and disclosed in
a GitHub security advisory that credits you, unless you prefer not to be named. Please
do not disclose it publicly before the fix is released.

## Security model

cc-observatory is built for a single machine and local use only. Several of the parts
below are still planned (see the roadmap in the [README](README.md)); this model applies
to each part from the release that ships it.

- **Local-only ports.** Every port that the compose stack publishes binds to
  `127.0.0.1`, so nothing is published on your network interfaces
  ([ADR 0018](docs/adr/0018-ports-networks-and-exposure.md)). Only the collector's OTLP
  receivers (4317 and 4318), Grafana (4330) and, from v0.3.0, the app (4300) are
  published. Prometheus, Loki and Tempo, which have no authentication of their own,
  publish no port and sit on an internal Docker network; the debug override
  `compose.debug.yaml` publishes them on `127.0.0.1` and gives them outbound access
  while you use it. It also publishes the app's metrics listener on `127.0.0.1:4301`.
  On Linux, Docker Engine releases older than 28.0 let hosts on the same local network
  segment (for example, behind the same switch) reach ports published to `127.0.0.1`
  ([Docker's port publishing docs](https://docs.docker.com/engine/network/port-publishing/)),
  so use Docker Engine 28.0 or later there.
- **No authentication.** Anyone with access to your user account, and any process that
  can reach localhost on your machine (including other users of a shared machine), can
  read the collected data, including the app's web UI and JSON API, and post events to
  the hook endpoint. Any local process can inject fake hook events or telemetry, so what
  the app and Grafana show is only as trustworthy as the software running on your
  machine.
- **Grafana has no login** (from v0.2.0,
  [ADR 0017](docs/adr/0017-grafana-access-and-dns-rebinding.md)). Whoever opens it is an
  Editor of its only organization: they can read all the data, run queries and save
  dashboards, but cannot create, change or delete datasources, install plugins, manage
  users or change server settings. There is no login form, no basic auth and no admin
  account at all, so there is no default password. The datasources are provisioned
  read-only from the repository, and Grafana's own Alerting and public dashboards (which
  would share a dashboard with anyone who has its link) are off.
- **Web pages in your browser.** Without a login, any web page you open can send
  requests to localhost: a malicious page could post forged events through cross-site
  requests, and a DNS rebinding attack could let it read what answers there.
    - Grafana (from v0.2.0) answers only as `localhost`: with `enforce_domain` and
      `localhost` as its domain, it redirects any other `Host` header, `127.0.0.1`
      included, so a DNS rebinding page reads only redirects. A few routes answer any
      host name by Grafana's design (`/api/health`, its static files), none with Claude
      Code data. With `csrf_always_check`, it refuses any request other than HEAD,
      OPTIONS and TRACE whose `Origin` header names another host, a GET included, even
      from an anonymous visitor. Its legacy datasource
      proxy, which a cross-site GET could otherwise use to reach a store's own API
      (Loki serves `GET /flush` and `GET /ingester/shutdown`, which stop its
      ingester), is closed by a datasource proxy allowlist that matches no datasource
      (`127.0.0.1:9`). Its `/metrics` endpoint, which a DNS rebinding page could read,
      is off.
    - Residual risk in Grafana: a simple cross-site GET, such as an `<img>` tag, carries
      no `Origin` header, so it can still make Grafana query a datasource through
      `/api/datasources/uid/<uid>/resources/*`. The page cannot read the answer, and
      that route reaches no endpoint that changes or stops a store, but the page can
      send expensive queries blindly, for example a long-range PromQL query, that could
      push Prometheus to its memory limit; Docker then restarts it. Closing this would
      need a proxy in front of Grafana that checks Fetch Metadata, which the stack does
      not have. Pages served by other local software on another `localhost` port also
      pass the `Origin` check, which compares host names only.
    - The collector's OTLP/HTTP receiver (port 4318, from v0.2.0) accepts any `Host`
      header, so a DNS rebinding page could post fake telemetry to it; it cannot read
      anything back through it, and a plain cross-site POST of JSON fails the CORS
      preflight.
    - The app (from v0.3.0,
      [ADR 0027](docs/adr/0027-the-app-http-surface-and-hook-queue.md)) answers only a
      `Host` header of `localhost`, `127.0.0.1` or `[::1]`, so a DNS rebinding page,
      which sends its own host name, gets a 400 from the API and reads nothing. Its hook
      endpoint ignores any request whose `Content-Type` is not `application/json`, and
      a cross-site page can send that type only after a CORS preflight, which fails:
      the app sends no CORS headers at all. The hook endpoint answers every request,
      ignored ones included, with the same empty 204, so a page learns nothing from it.
      The read-only JSON API serves the working directory (`cwd`), which contains your
      OS username, and it reaches only clients on localhost that send an allowed
      `Host`. The app serves no OpenAPI schema or interactive docs.
    - The app's web UI (from v0.4.0,
      [ADR 0034](docs/adr/0034-browser-hardening-csp-fetch-metadata-and-response-headers.md))
      adds these defences:
        - **Fetch Metadata.** The app refuses, with a 403, any browser request whose
          `Sec-Fetch-Site` header says it comes from another site, other `localhost`
          ports such as Grafana's included, unless it is a top-level `GET` navigation (a
          link or a typed URL); a websocket gets close code 1008. So a page cannot load
          the app's responses as images or scripts, fetch from it, frame it or hold its
          live updates stream. A link from another site still opens an app page in a
          tab of its own, which the linking page cannot read. Requests without the
          header, such as `curl`'s, are not browser requests and pass.
        - **Content Security Policy.** Every response but the hook endpoint's carries a
          strict policy: only the app's own scripts, styles, fonts and images (plus
          `data:` images), nothing from another origin, no inline or evaluated code, no
          plugins, no `<base>`, no form submission and no framing
          (`frame-ancestors 'none'`). htmx and the fonts are vendored files with recorded checksums
          ([ADR 0031](docs/adr/0031-server-rendered-pages-with-htmx-and-vendored-assets.md)).
        - **Other headers.** `Cross-Origin-Resource-Policy: same-origin`,
          `Cross-Origin-Opener-Policy: same-origin`, `X-Content-Type-Options: nosniff`,
          `Referrer-Policy: no-referrer` (a link to Grafana carries no address of the
          app's) and, outside the static files, `Cache-Control: no-store`, so the
          browser keeps no copy of a page on disk.
        - **Read-only, but for the alert settings.** Every route but the hook endpoint
          serves only `GET`, and the UI changes nothing in Claude Code. From v0.6.0 the
          Alerts page has the only exceptions, three `POST` routes: save and reset the
          alert settings, and send the Discord test message. They accept only a
          same-origin htmx request: `Sec-Fetch-Site: same-origin` when the browser
          sends it, exactly one `Origin` equal to the app's own (`<scheme>://<Host>`,
          with `Host` already on the allowlist), `HX-Request: true` and a urlencoded
          form; anything else gets a 403, or a 415 for another body type, before the
          body is read
          ([ADR 0045](docs/adr/0045-state-changing-routes-and-csrf.md)). A page of
          another origin cannot forge `Origin`, and can add `HX-Request` only after a
          CORS preflight, which fails since the app sends no CORS headers; a plain HTML
          form cannot send it at all. So no web page can change your alert settings or
          make the app post to Discord through your browser. Any local process still
          can, like it can post fake hooks. The UI keeps two values in the browser's
          local storage: the theme and, from v0.6.0, the ID of the newest alert the
          browser has read, for its notifications.
        - **Less personal data on screen.** The pages never show the full working
          directory: the overview shows the project name, and a session's page the
          path with the home directory as `~`. Homes are recognised by the shape of
          the path, so the username can still show: a home in an unusual place (such
          as `/home2/<user>`) shows as it is, a `/home/<group>/<user>` layout hides
          the group but not the user (`/home/staff/<user>/x` shows as
          `~/<user>/x`), and an MSYS or Cygwin home mounted outside `/home` or
          `Users` is not recognised.
        - The hook endpoint keeps its empty 204, with no added header, whatever these
          headers say.
    - The app's `/metrics` is on a separate listener that compose never publishes, so
      a browser on your machine cannot reach it
      ([ADR 0028](docs/adr/0028-app-metrics-and-their-listener.md)). Two exceptions,
      accepted because the series hold only counts and labels from fixed lists, never
      content, paths or IDs: the debug override publishes it on `127.0.0.1:4301` with
      no `Host` check, so while you use the override a DNS rebinding page could read
      the counts; and on Linux the host can reach a container's address on the Docker
      bridge network, so a page that guessed that address could read them too.
- **Do not expose it.** Do not publish the ports on other interfaces, and do not put the
  stack behind a reverse proxy, tunnel or port forward without adding authentication in
  front of it.
- **What is stored, and where.** All data stays in the compose stack's Docker volumes on
  your machine:
    - the app's SQLite database (from v0.3.0): sessions, agents and subagents, blocks
      and hook events, as metadata only: IDs, event, tool and MCP server names, agent
      types (your own agents by name), enumerated values, durations, sizes, the model
      and the working directory; never prompts, tool inputs or outputs, messages, error
      text or other paths
      ([ADR 0023](docs/adr/0023-per-event-allowlist-of-hook-metadata.md)). From
      v0.6.0 it also holds the alerts (the rule, the session ID, the times, the value
      and the threshold; no content, path or project name) and the alert settings saved
      on the Alerts page
      ([ADR 0042](docs/adr/0042-alert-tables-settings-precedence-and-retention.md)).
      [docs/app.md](docs/app.md#privacy) has the details;
    - Prometheus: metrics from Claude Code's telemetry (tokens, cost, counters) and the
      app's live state;
    - Loki: events from Claude Code's telemetry (API requests and errors, tool results
      and decisions);
    - Tempo, only when the optional `traces` profile is enabled: traces;
    - Grafana: its own database of settings and dashboards.
- **Local paths.** Hook payloads include local paths, such as the working directory and
  the transcript path, that contain your OS username. The app stores only the working
  directory (`cwd`) and drops the others; on Amazon Bedrock the stored model name can be
  an ARN that contains your AWS account ID. Keep them in mind when you share
  screenshots, API output, backups of the volumes or bug reports: a screenshot of the
  web UI shows project names and the paths below your home directory. The app's logs
  never contain them.
- **Content capture is opt-in.** By default, prompt text, tool inputs and outputs,
  assistant messages and error text are dropped before anything is stored.
  `ccobs setup` never turns content capture on in Claude Code, the collector or the app:
  v0.5.0 stores metadata only, and the opt-in is deferred
  ([ADR 0040](docs/adr/0040-defer-the-capture-content-opt-in.md)). Without it, the
  collector drops content even if Claude Code sends it.
  Once an opt-in exists, captured content can include source code, file contents and
  any secret that shows up in a prompt or a tool's output.
- **Personal attributes.** The collector always deletes every attribute whose name
  starts with `user.` or `enduser.` (such as `user.email`, `user.id`, which on gateway
  sessions is the identity provider subject, `user.account_uuid`, `user.account_id` and
  `user.groups`), plus `organization.id`, `identity.source` and `host.name`, from the
  telemetry before it is stored, including any you add through
  `OTEL_RESOURCE_ATTRIBUTES` ([ADR 0021](docs/adr/0021-collector-privacy-filters.md)).
- **No outbound traffic.** At runtime, nothing leaves your machine except the optional
  Discord webhook, if you configure one (alerts sent through it leave your machine), and
  the DNS lookups of a stopped store's name described below. This is a requirement of
  the bundled stack, and from v0.2.0 its configs enforce it:
    - Grafana: usage reporting, the update checks for Grafana and its plugins, feedback
      links, the news feed, Gravatar, external snapshots, the plugin signing key
      download, the plugin preinstall and its updates, plugin installs from the catalog
      and Cloud migration are off, and the base URL of its grafana.com API calls points
      at a closed port on Grafana's own loopback (`127.0.0.1:9`);
    - Loki and Tempo: their usage reports are off (`analytics.reporting_enabled` and
      `usage_report.reporting_enabled`);
    - Prometheus has no remote write or read and no Alertmanager, and the collector
      exports only to Prometheus, Loki and Tempo;
    - Prometheus, Loki and Tempo sit on an internal Docker network with no route out,
      so they cannot reach the internet even with a wrong setting;
    - the app connects to nothing but Loki and Prometheus, over that internal network:
      Loki to close permission prompts denied with Esc and for the diagnostics of
      `ccobs doctor` and `status`, and Prometheus for a session page's tokens and
      cost and, from v0.6.0, for the cost alerts' totals, at most once a minute and
      only while a cost alert is enabled; those queries hold no session ID and stay on
      your machine. The optional Discord webhook above is its only connection that
      leaves your machine, and it exists only while `CCOBS_DISCORD_WEBHOOK_URL` is set:
      a message per alert (up to ten together) with the rule in words ("Waiting on you
      for over 31s"), the host label, the short session ID and a local link to the
      session's page, and the project name only with
      `CCOBS_DISCORD_INCLUDE_PROJECT=true`; never the working directory or another
      path, a prompt, a tool input or output, a message or error text, since an alert
      stores none of them. A message mentions nobody (`allowed_mentions` is empty).
      The "Send test message" button posts one fixed line with the host label. That
      connection alone follows the container's `HTTPS_PROXY`, `NO_PROXY` and
      `SSL_CERT_FILE`, and stops at the first 401, 403 or 404 until the app restarts
      ([ADR 0046](docs/adr/0046-discord-delivery.md)).

  Tests check these settings in the configs and in the running stack, and
  [docs/stack.md](docs/stack.md#checking-that-nothing-leaves-the-machine) describes a
  manual check that no connection leaves. Not covered: Grafana's Geomap panel loads its
  default map tiles from CARTO in your browser; no bundled dashboard has one. And while
  one of the stores is not running, a lookup of its name by the collector or Grafana
  (such as `tempo` when traces arrive without the `traces` profile) goes to your host's
  DNS resolvers; the query carries only that name (with your network's search domain,
  if any).
- **Secrets.** Secrets such as `CCOBS_DISCORD_WEBHOOK_URL` are read from the environment
  and never logged.
    - The webhook URL holds its token: anyone who has it can post to your channel. The
      app keeps it in a `SecretStr`, read only by the Discord sender at each post, and
      never puts it in a log line, a page, an API answer, a metric, a database row or a
      settings error: it logs only status codes and error types for Discord, and drops
      the HTTP client's own log lines that name a webhook path
      ([ADR 0046](docs/adr/0046-discord-delivery.md)). The Alerts page says only
      whether a webhook is configured.
    - Docker does show it: `docker compose config` (without `--quiet`) prints it with
      the other resolved settings, and `docker inspect` of the app container lists it
      in the container's environment. Keep it in `.env`, which Git ignores, and keep
      that output, and `.env`, out of issues and screenshots.
    - If it leaks, delete the webhook in Discord (the server's **Server Settings**,
      **Integrations**, its webhooks), which makes the old URL useless, create a new
      one, put the new URL in `.env` and run `docker compose up -d`
      ([docs/app.md](docs/app.md#discord)).
- **Settings backup.** `ccobs setup` backs up `~/.claude/settings.json` before changing
  it. The backup holds everything that file holds, including any secrets in its `env`
  block.

## Scope

In scope: the code and configuration in this repository, that is, the app, the `ccobs`
CLI, the compose file, the bundled configurations for the OpenTelemetry Collector,
Prometheus, Loki, Tempo and Grafana, and the changes that `ccobs setup` makes to Claude
Code's settings. An insecure default in any of them (for example, a port that is not
bound to `127.0.0.1`, a personal attribute that reaches storage, or an upstream usage
report that is left on) is in scope.

Out of scope, and reported upstream instead:

- vulnerabilities in Claude Code itself go to Anthropic, as described in
  [Claude Code's security policy](https://github.com/anthropics/claude-code/security/policy);
- vulnerabilities in the OpenTelemetry Collector, Prometheus, Loki, Tempo or Grafana go
  to those projects, following their own security policies.

If you are not sure where an issue belongs, report it here privately and the maintainer
will help route it.
