---
status: accepted
date: 2026-10-14
decision-makers: Luanderson-Dev (maintainer)
---

# Stored telemetry names: the default translation, recorded in `ccobs.domain.telemetry`

## Context and problem statement

[ADR 0012](0012-observed-telemetry-and-hook-contract.md) records the OpenTelemetry names
Claude Code 2.1.296 sends and leaves the names they get in Prometheus and Loki to
milestone 1, "never guessed".
[ADR 0013](0013-metrics-through-the-prometheus-otlp-receiver.md) sends metrics to
Prometheus's OTLP receiver with its default translation, and
[ADR 0015](0015-loki-through-its-otlp-endpoint.md) sends events to Loki's OTLP endpoint,
which stores attributes as structured metadata. Both translations change names:
`claude_code.cost.usage` (unit `USD`) becomes `claude_code_cost_usage_USD_total`, and
`claude_code.active_time.total` (unit `s`) becomes
`claude_code_active_time_seconds_total`. Prometheus cannot describe these metrics
either: `/api/v1/metadata` returns nothing for metrics ingested over OTLP. The app
(milestone 2), the Grafana dashboards (milestone 6) and `ccobs doctor` (milestone 4) all
query these names. Which names and labels do they use, where are they recorded, and how
does the record stay in step with the pipeline?

## Decision drivers

- Code uses observed names, never guesses (CLAUDE.md, ADR 0012).
- One source of names for every consumer, checked against the running pipeline.
- PromQL without quoted metric or label names.
- The exact totals of ADR 0013, queried the same way everywhere.
- `ccobs.domain` has no I/O, and the CLI stays light (CLAUDE.md).

## Considered options

- **The default translation, with the names recorded as constants in
  `ccobs.domain.telemetry`.** A pure module with the names the pipeline returned, which
  every consumer imports and tests compare with a running stack. It must follow Claude
  Code, Prometheus and Loki when they change.
- **Names hard-coded in each consumer.** Nothing shared, but every consumer repeats the
  names, and a change is found one consumer at a time.
- **Names read from Prometheus at runtime.** Always current, but `/api/v1/metadata` is
  empty for OTLP metrics, so types, units and optional labels cannot be read;
  `/api/v1/label/__name__/values` lists only metrics that already have samples, and
  Loki shows structured metadata keys only on stored entries.
- **Another `translation_strategy`.** `NoUTF8EscapingWithSuffixes` or `NoTranslation`
  would keep the dots (`claude_code.cost.usage`, `session.id`), but every query would
  need Prometheus 3's quoted syntax, such as
  `{"claude_code.cost.usage", "session.id"="<id>"}`, and tools that expect classic
  names, Grafana's query builder among them, would need it too.

## Decision outcome

Chosen option: "The default translation, with the names recorded as constants in
`ccobs.domain.telemetry`", because it gives every consumer the same observed names in
classic PromQL, and a test fails when the pipeline stores anything else. Hard-coded
names drift apart, Prometheus cannot report what the consumers need, and UTF-8 names
would make every query quote its names.

- **Executable form.** `src/ccobs/domain/telemetry.py` holds the names and labels below
  as constants, the privacy lists of [ADR 0021](0021-collector-privacy-filters.md) as
  the domain's copy, and `total_query` and `selector`, which build PromQL from them. It
  is the executable form of this ADR: tests compare it with the fixtures, the collector
  config and the running stack (see Confirmation), and a change of a name there changes
  this ADR.
- **How the names were observed.** On 2026-10-14,
  `tests/integration/test_metric_names.py` replayed the 8 fixture scenarios that carry
  telemetry (ADR 0012) through the stack of this milestone (collector contrib 0.162.0,
  Prometheus 3.13.4, Loki 3.7.8) and read the names back.
  `claude_code.pull_request.count`, documented but never captured (ADR 0012), was
  observed through the pipeline with a synthetic point: the fixtures'
  `claude_code.commit.count` point (no unit, a monotonic delta sum, the same resource
  and attributes) under the documented name, in a session of its own.

### Metrics in Prometheus

| OpenTelemetry name                    | Unit     | Prometheus name                             | Labels on every series                        | Optional labels                                  |
| ------------------------------------- | -------- | ------------------------------------------- | --------------------------------------------- | ------------------------------------------------ |
| `claude_code.session.count`           | none     | `claude_code_session_count_total`           | `start_type`                                  |                                                  |
| `claude_code.active_time.total`       | `s`      | `claude_code_active_time_seconds_total`     | `type`                                        |                                                  |
| `claude_code.token.usage`             | `tokens` | `claude_code_token_usage_tokens_total`      | `type`, `model`, `query_source`, `effort`     | `agent_name`, `mcp_server_name`, `mcp_tool_name` |
| `claude_code.cost.usage`              | `USD`    | `claude_code_cost_usage_USD_total`          | `model`, `query_source`, `effort`             | `agent_name`, `mcp_server_name`, `mcp_tool_name` |
| `claude_code.lines_of_code.count`     | none     | `claude_code_lines_of_code_count_total`     | `type`, `model`                               |                                                  |
| `claude_code.code_edit_tool.decision` | none     | `claude_code_code_edit_tool_decision_total` | `tool_name`, `decision`, `source`, `language` |                                                  |
| `claude_code.commit.count`            | none     | `claude_code_commit_count_total`            |                                               |                                                  |
| `claude_code.pull_request.count`      | none     | `claude_code_pull_request_count_total`      |                                               |                                                  |

- **Every series** also has `job="claude-code"` (from `service.name`), `session_id` and
  `terminal_type`, and optionally `app_version`.
- **Names** follow the observed translation (`UnderscoreEscapingWithSuffixes`): the
  name's words joined by underscores, a `total` word moved to the end, the unit added as
  a word (`s` becomes `seconds`, `tokens` and `USD` stay as they are, case included, and
  no unit adds nothing), then `_total`, since every Claude Code metric is a monotonic
  sum, stored as a counter.
- **Labels** are the data point attributes with dots turned into underscores
  (`session.id` becomes `session_id`). There is no `instance` label, since Claude Code
  sends no `service.instance.id`, and no `otel_scope_*` labels. Identity attributes
  never become labels (ADR 0021).
- **On every series** means on every data point of that metric in the fixtures.
  `session_id` is absent only if the user sets `OTEL_METRICS_INCLUDE_SESSION_ID=false`,
  which `ccobs setup` never does
  ([ADR 0016](0016-cardinality-and-retention.md)).
- **Optional labels.** `app_version` is on every fixture point only because the capture
  set `OTEL_METRICS_INCLUDE_VERSION=true`, which is off by default. `agent_name` is on
  the requests of a subagent, and `mcp_server_name` and `mcp_tool_name` on requests made
  for an MCP tool.
- **Labels that are not listed** can appear: attributes a user adds through
  `OTEL_RESOURCE_ATTRIBUTES` reach every data point (ADR 0021), and so would the
  repository attributes of `OTEL_METRICS_INCLUDE_REPOSITORY`, whose stored names were
  not observed. Queries aggregate with `sum by (...)`, so extra labels do not split
  their results.
- **`target_info`.** The resource attributes other than `service.name` stay on one
  `target_info{job="claude-code"}` series with `service_version`, `os_type`,
  `os_version` and `host_arch`, shared by every session on the machine (ADR 0013).
- **No metadata.** `/api/v1/metadata` returns nothing for any of these metrics, so their
  types and units come from this ADR and ADR 0012, not from Prometheus.
- **`host`.** The `host` label of [ADR 0001](0001-local-single-machine-scope.md) is
  added to both backends by a later stage of this milestone, which extends these lists.

### Events in Loki

- **Index label:** `service_name="claude-code"` only (ADR 0015); `host` joins it with
  the stage that sets it.
- **Structured metadata on every entry:** `session_id`, `event_name`, `event_sequence`,
  `event_timestamp` and `terminal_type`; the resource attributes `service_version`,
  `host_arch`, `os_type` and `os_version`; and the fields Loki adds, `scope_name`,
  `scope_version`, `observed_timestamp` and `detected_level`.
- **On some entries:** `prompt_id`, after the first prompt; `trace_id`, `span_id` and
  `flags` (the trace flags), on events emitted during an interaction with tracing on;
  and `app_version`, with `OTEL_METRICS_INCLUDE_VERSION=true`.
- **Every other key** is an attribute of the event (ADR 0012) under its own name, with
  dots turned into underscores, such as `cost_usd`, `input_tokens`, `tool_name` and
  `managed_settings_helper_state`. No identity or content key is stored (ADR 0021).
- **Events.** Each entry's line is `claude_code.<event_name>`. The `event_name` values
  of the fixtures are `user_prompt`, `api_request`, `api_error`, `assistant_response`,
  `tool_decision`, `tool_result`, `subagent_completed`, `compaction`,
  `hook_registered`, `hook_execution_start`, `hook_execution_complete`,
  `mcp_server_connection`, `plugin_loaded` and `managed_settings_resolved`.

### PromQL

- **Totals** are `sum by (<labels>) (increase(<name>{<matchers>}[<R>] anchored))`
  (ADR 0013), which `total_query` builds. It accepts only labels the metric can have,
  and quotes each value, escaping the backslash and the double quote, the newline and
  U+FFFD (which Prometheus's lexer refuses raw) and the other control characters. Any
  value that is valid UTF-8, such as a session ID or a model name, then matches itself
  and cannot change the query; a value with a lone surrogate is refused.
- **One series.** A selector also matches series with labels it does not name. To
  select one series, add `label=""` for each label of the metric that the series lacks;
  `agent_name=""`, for example, leaves out a subagent's requests.
- **Windows.** `anchored` takes a window's starting value from the most recent sample
  within the lookback (5 minutes by default). A window that starts longer than that
  after a series' previous sample needs `lookback_delta` on the query API (ADR 0013).

### Consequences

- Good, because every consumer takes its names from one module, and the integration
  test fails when the pipeline stores something else: a Claude Code upgrade, a new
  translation in Prometheus or a new OTLP mapping in Loki.
- Good, because classic names need no quoting in PromQL, Grafana or other tools.
- Good, because `total_query` applies the rule of ADR 0013 in one place and keeps
  request values from changing a query.
- Bad, because the record covers what the fixtures exercised: user attributes, the
  repository attributes and Claude Code versions, platforms or models other than those
  of ADR 0012 can add or remove labels. A recapture, or a new observation through the
  pipeline, extends the module and this ADR.
- Bad, because `claude_code_pull_request_count_total` comes from a synthetic point: its
  name follows from the translation, but its labels are assumed to be those of
  `claude_code.commit.count` until a real pull request is captured.
- Neutral, because the privacy lists of ADR 0021 now exist in three places (the
  collector config, the replayer's canaries and the module), kept equal by tests.
- Neutral, because the module is pure data and string building: nothing imports it
  when the CLI starts.

### Confirmation

- `tests/test_domain_telemetry.py` checks the constants against the fixtures (metric
  names, units, labels on every point and on some, resource attributes, event names and
  lines), the translation rule above, that every label is a valid Prometheus label name
  and none is an identity or content key, that the privacy lists equal the replayer's
  canary lists and what `stack/otel-collector/config.yaml` deletes, the PromQL of
  `total_query` and `selector` with their escaping and validation, and that
  `ccobs.domain` imports nothing that does I/O.
- `tests/integration/test_metric_names.py` replays every fixture scenario and the
  synthetic `claude_code.pull_request.count` point with fresh session IDs, then checks
  that Prometheus stores exactly the module's metric names; that each metric's series
  carry the labels on every series and no label outside the module's list, and that
  every listed label shows up; the `target_info` labels; that `/api/v1/metadata` is
  empty for each metric; that `total_query` gives each session's replayed totals and
  that its escaped values parse; and that Loki's index labels, structured metadata keys
  (each entry against its own event's attributes) and event names match the module.
- The `reviewer` subagent checks that consumers take metric, label and event names from
  `ccobs.domain.telemetry` and build totals with `total_query` or the same form.

## More information

- Prometheus: [OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)
  (`translation_strategy`) and the
  [HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
  (`/api/v1/metadata`, `/api/v1/label/<name>/values`, `/api/v1/series`,
  `lookback_delta`).
- Loki: [OTLP ingestion](https://grafana.com/docs/loki/latest/send-data/otel/) and
  [structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/).
- Claude Code [monitoring docs](https://code.claude.com/docs/en/monitoring-usage):
  `claude_code.pull_request.count`, `OTEL_METRICS_INCLUDE_VERSION` and
  `OTEL_METRICS_INCLUDE_SESSION_ID`.
- Revisit when Claude Code changes its metrics, attributes or events (a recapture, ADR
  0012), when Prometheus changes its OTLP translation or its default strategy, when Loki
  changes its OTLP mapping, and when the `host` label lands.
