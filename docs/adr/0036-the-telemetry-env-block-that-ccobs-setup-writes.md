---
status: accepted
date: 2026-10-18
decision-makers: Luanderson-Dev (maintainer)
---

# The telemetry env block that `ccobs setup` writes

## Context and problem statement

[ADR 0002](0002-native-telemetry-plus-hooks.md) collects Claude Code's native telemetry,
which Claude Code configures from environment variables. It honours them from the
environment it starts in, a `--settings` file, user settings and managed settings, but
drops the exporter variables (`CLAUDE_CODE_ENABLE_TELEMETRY`, the `OTEL_*_EXPORTER`
selectors, the `OTEL_LOG_*` content variables, `OTEL_EXPORTER_OTLP_*_ENDPOINT` and
`_PROTOCOL`, ...) from project and local settings, apart from values that turn
telemetry off (the settings reference, "Variables Claude Code ignores in `env`", read
on 2026-10-18). `ccobs setup` (milestone 4) therefore writes an `env` block into the
user settings, next to the hooks of [ADR 0025](0025-registered-hook-events.md). The
README already documents a block by hand, and ADRs 0006, 0013, 0016 and 0018 each
constrain part of it. Which variables does `ccobs setup` write, with which values, and
where does the block come from so that setup, doctor and the README agree?

## Decision drivers

- Metadata only by default: setup turns on no content capture
  ([ADR 0006](0006-privacy-metadata-only-by-default.md)).
- Exact totals: cumulative temporality from Claude Code, so the delta limit of
  [ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md) does not apply.
- Session IDs stay on metrics ([ADR 0016](0016-cardinality-and-retention.md)).
- The collector is reached on `127.0.0.1` only
  ([ADR 0018](0018-ports-networks-and-exposure.md)).
- Every key setup owns is one it must later check (doctor) and remove (uninstall), so
  write no key without an effect.
- One pure function as the single source, like `hook_settings` for the hooks
  (ADR 0025).

## Considered options

- **(1) The minimum block the README documents.** Telemetry on, metrics and logs over
  OTLP/HTTP to the local collector, cumulative temporality; nothing else.
- **(2) The minimum plus `OTEL_METRICS_INCLUDE_SESSION_ID="true"`.** States the default
  explicitly. ADRs 0013 and 0016 only forbid setting it to `false`, and its default is
  `true`, so the key would have no effect and still be one more key to own.
- **(3) The minimum plus `OTEL_LOG_*="0"`.** Pins redaction explicitly. ADR 0006 says
  setup does not set `OTEL_LOG_*`; writing them, even as `0`, would override a value the
  user set on purpose in the same file and make uninstall harder.

## Decision outcome

Chosen option: "(1) the minimum block the README documents", because it meets every
driver with the fewest owned keys; (2) adds a key with no effect, and (3) contradicts
ADR 0006 and would overwrite a deliberate user choice.

- **One source.** `ccobs.domain.telemetry_settings.telemetry_env(endpoint, *, traces)`
  returns the block as a `dict[str, str]`; every value is a string, as settings `env`
  requires. `ccobs setup` writes it and `ccobs doctor` compares against it. The README
  snippets (both languages) are written by hand, and `tests/test_docs_snippet.py`
  checks them against `telemetry_env()`.
- **Keys, in this order:**
    - `CLAUDE_CODE_ENABLE_TELEMETRY`: `"1"`;
    - `OTEL_METRICS_EXPORTER`: `"otlp"`;
    - `OTEL_LOGS_EXPORTER`: `"otlp"`;
    - `OTEL_EXPORTER_OTLP_PROTOCOL`: `"http/protobuf"`;
    - `OTEL_EXPORTER_OTLP_ENDPOINT`: `"http://127.0.0.1:4318"` by default; the port
      follows `--otlp-port`, and the host is always `127.0.0.1`, never `localhost` or
      `[::1]` (ADR 0018). Setup writes only the generic endpoint and protocol, never
      the per-signal ones (`PER_SIGNAL_ENDPOINT_VARIABLES`, see Precedence);
    - `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`: `"cumulative"` (ADR 0013).
- **Traces** are off by default. `--traces` appends
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`: `"1"` and `OTEL_TRACES_EXPORTER`: `"otlp"`, and
  the stack must then run with `--profile traces` (Tempo).
- **Not written**, in two groups that doctor uses. Doctor evaluates values, not
  presence, so `OTEL_LOG_ASSISTANT_RESPONSES="0"` or
  `OTEL_METRICS_INCLUDE_SESSION_ID="true"` set by the user is fine.
    - `CONTENT_VARIABLES`: `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
      `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES` and
      the detailed beta tracing pair `ENABLE_BETA_TRACING_DETAILED` and
      `BETA_TRACING_ENDPOINT` (the variables that export session content, settings
      reference, "Variables Claude Code ignores in `env`"). The default block writes
      none of them; ADR 0006's `--capture-content` opt-in, deferred to a later
      milestone, is the only planned exception. Doctor flags each one set to an
      enabling value.
    - `SESSION_ID_VARIABLE`: `OTEL_METRICS_INCLUDE_SESSION_ID`. Never written; doctor
      flags it only when it is `"false"` (ADR 0016).
- **Precedence.** A settings `env` value overwrites the same variable exported in the
  shell (settings reference, "How `env` values interact with your shell"), so the block
  wins over a generic endpoint or protocol the user exports by hand. It does not win
  over a per-signal one: `OTEL_EXPORTER_OTLP_{METRICS,LOGS,TRACES}_{ENDPOINT,PROTOCOL}`
  are used instead of the generic variables for their signal (OpenTelemetry
  specification; monitoring, "overrides general setting"), wherever they are set.
  Doctor therefore warns when any `PER_SIGNAL_ENDPOINT_VARIABLES` entry is set outside
  the block. One documented exception reverses the shell rule: when the Claude Desktop
  app or a self-hosted environment runner starts the session, a variable already set
  in the launch environment it builds wins over every settings file (same section).
- **When it applies.** OpenTelemetry reads its variables once at startup: a running
  session keeps its exporter until Claude Code is relaunched (environment variables
  reference, "Set variables in settings files").

### Consequences

- Good, because setup, doctor and the README come from one function, and
  `tests/test_docs_snippet.py` keeps both READMEs equal to it.
- Good, because cumulative temporality avoids the delta limit described in
  `docs/stack.md` ("Known limit: delta temporality").
- Good, because setup owns only the keys that change something, so uninstall removes
  exactly those and leaves the user's own `OTEL_LOG_*` choices alone.
- Bad, because the endpoint is pinned to `127.0.0.1`: a collector moved to another port
  needs `ccobs setup --otlp-port`.
- Bad, because a telemetry change takes effect only after Claude Code is restarted.
- Bad, because a per-signal endpoint or protocol, or a Desktop or runner launch
  environment, silently overrides the block; doctor can only warn about it.
- Neutral, because the troubleshooting table in `docs/stack.md` ("No data in the
  dashboard") also lists these variables and no test checks it against
  `telemetry_env()`, so it must be kept in step by hand.
- Neutral, because OTLP gRPC on 4317 stays published and supported for a block written
  by hand, but setup writes `http/protobuf` on 4318.

### Confirmation

- `tests/test_domain_telemetry_settings.py` checks the exact block and its order with and
  without traces, the endpoint for default and custom ports, that every value is a
  string, and that no `CONTENT_VARIABLES`, `SESSION_ID_VARIABLE` or
  `PER_SIGNAL_ENDPOINT_VARIABLES` key is ever returned.
- `tests/test_docs_snippet.py` checks that the snippets in `README.md` and
  `README.pt-BR.md` equal `telemetry_env()`.
- The `ccobs setup` tests of milestone 4 check that the `env` it writes equals
  `telemetry_env()`.

## More information

- Claude Code [monitoring](https://code.claude.com/docs/en/monitoring-usage),
  [settings reference, `env`](https://code.claude.com/docs/en/settings-reference#env)
  and [environment variables](https://code.claude.com/docs/en/env-vars).
- Extends ADRs 0013 and 0018; consistent with ADRs 0002, 0006 and 0016; mirrors
  ADR 0025's single source for the hooks.
- Revisit when the content capture opt-in is decided, when Claude Code changes which
  settings files may set telemetry variables, or when it reads them during a session.
