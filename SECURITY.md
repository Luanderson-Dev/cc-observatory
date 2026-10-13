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
  `127.0.0.1`, so nothing is published on your network interfaces.
- **No authentication.** Anyone with access to your user account, and any process that
  can reach localhost on your machine (including other users of a shared machine), can
  read the collected data and post events to the hook endpoint. Any local process can
  inject fake hook events, so what the app shows is only as trustworthy as the software
  running on your machine.
- **Web pages in your browser.** Without a login, any web page you open can send
  requests to `127.0.0.1`: a malicious page could post forged hook events through
  cross-site requests, and a DNS rebinding attack could let it read the app or Grafana.
  Planned defences, in the app from v0.3.0: a `Host` header allowlist (`localhost`,
  `127.0.0.1`, `[::1]`); a hook endpoint that ignores any request whose `Content-Type`
  is not `application/json` (still answering with an empty 2xx); and no permissive CORS.
- **Do not expose it.** Do not publish the ports on other interfaces, and do not put the
  stack behind a reverse proxy, tunnel or port forward without adding authentication in
  front of it.
- **What is stored, and where.** All data stays in the compose stack's Docker volumes on
  your machine:
    - the app's SQLite database: sessions, agents and subagents, blocks and alerts,
      built from hook payloads;
    - Prometheus: metrics from Claude Code's telemetry (tokens, cost, counters) and the
      app's live state;
    - Loki: events from Claude Code's telemetry (API requests and errors, tool results
      and decisions);
    - Tempo, only when the optional `traces` profile is enabled: traces;
    - Grafana: its own database of settings and dashboards.
- **Local paths.** Hook payloads include local paths, such as the working directory and
  the transcript path, that contain your OS username. They are stored as metadata, so
  keep them in mind when you share screenshots, exports or bug reports.
- **Content capture is opt-in.** By default, prompt text, tool inputs and outputs,
  assistant messages and error text are dropped before anything is stored.
  `ccobs setup --capture-content` turns content capture on in Claude Code, the collector
  and the app; without it, the collector drops content even if Claude Code sends it.
  Captured content can include source code, file contents and any secret that shows up
  in a prompt or a tool's output.
- **Personal attributes.** The collector deletes `user.email`, `user.id` (on gateway
  sessions it is the identity provider subject), `user.account_uuid`, `user.account_id`
  and `organization.id` (and `user.groups` and `identity.source` on gateway sessions)
  from the telemetry before it is stored.
- **No outbound traffic.** At runtime, nothing leaves your machine except the optional
  Discord webhook, and only if you configure one; alerts sent through it leave your
  machine. This is a requirement of the bundled stack: from v0.2.0 its configs turn off
  upstream usage reporting and update checks (in Grafana: analytics reporting, update
  checks for Grafana and its plugins, the news feed and Gravatar; in Loki:
  `analytics.reporting_enabled`; in Tempo: `usage_report.reporting_enabled`), and tests
  check that they stay off.
- **Secrets.** Secrets such as `CCOBS_DISCORD_WEBHOOK_URL` are read from the environment
  and never logged.
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
