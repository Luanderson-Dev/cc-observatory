"""The names Claude Code's telemetry takes in Prometheus and Loki (ADR 0014).

Claude Code sends OpenTelemetry names (ADR 0012); Prometheus and Loki store translated
ones, and Prometheus has no metadata for metrics that arrive over OTLP. The constants
below are the names the stack returned when the fixtures of Claude Code 2.1.296 were
replayed through it, and ``tests/integration/test_metric_names.py`` compares them with a
running stack: this module is the executable form of ADR 0014. The app, the Grafana
dashboards and ``ccobs doctor`` take names from here, never from guesses.

Every Claude Code metric is a monotonic sum, stored as a Prometheus counter whose name
ends in ``_total``. Totals follow ADR 0013, ``increase(<name>{...}[<window>] anchored)``
summed by the labels needed, which ``total_query`` builds. The privacy lists of ADR 0021
are here too, as the domain's copy of what the collector deletes, and the host label
that the collector sets on every signal (ADR 0022).

Data and string building only: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ccobs.domain.durations import parse_duration

# Claude Code's service.name: the value of Prometheus's job label and of Loki's
# service_name index label.
SERVICE_NAME: Final = "claude-code"

# The stack's host label (ADR 0022): the collector sets the resource attribute host on
# every signal from CCOBS_HOST_LABEL, this default when it is unset. Never the machine's
# hostname by default: that is personal data, and the collector deletes host.name (ADR
# 0021). Prometheus promotes it to a label and Loki indexes it.
HOST_LABEL: Final = "host"
DEFAULT_HOST: Final = "local"
# The values the label supports (ADR 0022), matched in full: letters, digits, ".", "_"
# and "-". The value goes into an OTTL string literal in the collector's config, where
# a double quote or a backslash would break or change it. The app refuses anything else
# at start-up; the collector does not validate it.
HOST_VALUE_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+")

# Prometheus (ADR 0013).

# The label Prometheus derives from service.name, on every series of Claude Code's
# metrics. There is no instance label: Claude Code sends no service.instance.id.
JOB_LABEL: Final = "job"
# The resource attributes Prometheus promotes to a label on every series: only host.
PROMOTED_LABELS: Final = frozenset({HOST_LABEL})
# The series that holds the other resource attributes, and its labels; the promoted
# host label stays on it too.
TARGET_INFO: Final = "target_info"
TARGET_INFO_LABELS: Final = frozenset(
    {JOB_LABEL, "service_version", "os_type", "os_version", "host_arch"} | PROMOTED_LABELS
)
# Standard attributes on every data point: session.id (gone only if the user sets
# OTEL_METRICS_INCLUDE_SESSION_ID=false, which ccobs setup never does, ADR 0016) and
# terminal.type.
COMMON_LABELS: Final = frozenset({"session_id", "terminal_type"})
# app.version, sent only with OTEL_METRICS_INCLUDE_VERSION=true (the capture set it).
OPTIONAL_COMMON_LABELS: Final = frozenset({"app_version"})


@dataclass(frozen=True, slots=True)
class Metric:
    """One Claude Code metric: its OpenTelemetry name and unit (``""`` when it has
    none), its Prometheus name, and the labels of its series besides ``job``.

    ``labels`` are on every series and ``optional_labels`` only on some, depending on
    the request or on a setting; both include the standard attributes, and ``labels``
    the promoted host label. Attributes a user adds through ``OTEL_RESOURCE_ATTRIBUTES``
    can add labels that are not listed."""

    otel_name: str
    name: str
    unit: str
    labels: frozenset[str]
    optional_labels: frozenset[str]

    @property
    def all_labels(self) -> frozenset[str]:
        """Every label a series of this metric can have, ``job`` included."""
        return frozenset({JOB_LABEL}) | self.labels | self.optional_labels


def _metric(
    otel_name: str,
    name: str,
    unit: str = "",
    *,
    labels: Sequence[str] = (),
    optional: Sequence[str] = (),
) -> Metric:
    return Metric(
        otel_name,
        name,
        unit,
        COMMON_LABELS | PROMOTED_LABELS | frozenset(labels),
        OPTIONAL_COMMON_LABELS | frozenset(optional),
    )


# On some requests only: agent_name on a subagent's, the MCP labels on those made for an
# MCP tool.
_REQUEST_LABELS: Final = ("agent_name", "mcp_server_name", "mcp_tool_name")

SESSION_COUNT: Final = _metric(
    "claude_code.session.count", "claude_code_session_count_total", labels=("start_type",)
)
ACTIVE_TIME: Final = _metric(
    "claude_code.active_time.total",
    "claude_code_active_time_seconds_total",
    "s",
    labels=("type",),
)
TOKEN_USAGE: Final = _metric(
    "claude_code.token.usage",
    "claude_code_token_usage_tokens_total",
    "tokens",
    labels=("type", "model", "query_source", "effort"),
    optional=_REQUEST_LABELS,
)
COST_USAGE: Final = _metric(
    "claude_code.cost.usage",
    "claude_code_cost_usage_USD_total",
    "USD",
    labels=("model", "query_source", "effort"),
    optional=_REQUEST_LABELS,
)
LINES_OF_CODE: Final = _metric(
    "claude_code.lines_of_code.count",
    "claude_code_lines_of_code_count_total",
    labels=("type", "model"),
)
CODE_EDIT_TOOL_DECISION: Final = _metric(
    "claude_code.code_edit_tool.decision",
    "claude_code_code_edit_tool_decision_total",
    labels=("tool_name", "decision", "source", "language"),
)
COMMIT_COUNT: Final = _metric("claude_code.commit.count", "claude_code_commit_count_total")
# Documented but never captured: only its name was observed through the pipeline, with
# a synthetic point shaped like commit.count's; its labels are assumed to be
# commit.count's until a real pull request is captured (ADR 0014).
PULL_REQUEST_COUNT: Final = _metric(
    "claude_code.pull_request.count", "claude_code_pull_request_count_total"
)
METRICS: Final = (
    SESSION_COUNT,
    ACTIVE_TIME,
    TOKEN_USAGE,
    COST_USAGE,
    LINES_OF_CODE,
    CODE_EDIT_TOOL_DECISION,
    COMMIT_COUNT,
    PULL_REQUEST_COUNT,
)

# Loki (ADR 0015).

# Index labels: one stream per machine, told apart by the host label (ADR 0022).
LOKI_INDEX_LABELS: Final = frozenset({"service_name", HOST_LABEL})
# Structured metadata on every entry: the attributes on every event, the resource
# attributes other than service.name, and the fields Loki adds (detected_level is
# "unknown", since Claude Code sends no severity).
LOKI_METADATA: Final = frozenset(
    {"session_id", "event_name", "event_sequence", "event_timestamp", "terminal_type"}
    | {"service_version", "host_arch", "os_type", "os_version"}
    | {"scope_name", "scope_version", "observed_timestamp", "detected_level"}
)
# On some entries: prompt_id after the first prompt; trace_id, span_id and flags (the
# trace flags) on events emitted during an interaction, with tracing on; app_version
# with OTEL_METRICS_INCLUDE_VERSION=true. Any other key is an event attribute under its
# own name, with dots turned into underscores (cost_usd, tool_name, hook_event, ...).
LOKI_OPTIONAL_METADATA: Final = frozenset(
    {"prompt_id", "trace_id", "span_id", "flags", "app_version"}
)
# Each entry's line is the body Claude Code sends: this prefix and the event name.
EVENT_BODY_PREFIX: Final = "claude_code."
# The event_name values of the fixtures (ADR 0012).
EVENTS: Final = frozenset(
    {
        "user_prompt",
        "api_request",
        "api_error",
        "assistant_response",
        "tool_decision",
        "tool_result",
        "subagent_completed",
        "compaction",
        "hook_registered",
        "hook_execution_start",
        "hook_execution_complete",
        "mcp_server_connection",
        "plugin_loaded",
        "managed_settings_resolved",
    }
)

# Privacy (ADR 0021): what the collector deletes, by OpenTelemetry name.

# Identity, always deleted at every level: keys with these prefixes, and these keys.
IDENTITY_PREFIXES: Final = ("user.", "enduser.")
IDENTITY_KEYS: Final = frozenset({"organization.id", "identity.source", "host.name"})
# Content, deleted while content capture is off: attributes per level, ...
RESOURCE_CONTENT_KEYS: Final = frozenset({"workspace.host_paths"})
DATA_POINT_CONTENT_KEYS: Final = frozenset({"workspace.host_paths"})
LOG_CONTENT_KEYS: Final = frozenset(
    {"prompt", "prompt_text", "response", "tool_parameters", "tool_input", "error"}
    | {"body", "body_ref", "hook_definitions", "workspace.host_paths"}
)
SPAN_CONTENT_KEYS: Final = frozenset(
    {"user_prompt", "new_context", "system_reminders", "system_prompt_preview"}
    | {"user_system_prompt", "response.model_output", "tool_input", "full_command"}
    | {"file_path", "hook_definitions", "error", "workspace.host_paths"}
)
SPAN_EVENT_CONTENT_KEYS: Final = frozenset({"workspace.host_paths"})
# ... whole events (log records) with these names, and span events with this name.
CONTENT_EVENTS: Final = frozenset({"api_request_body", "api_response_body", "system_prompt"})
CONTENT_SPAN_EVENT: Final = "tool.output"


def is_identity_key(key: str) -> bool:
    """Whether the collector deletes the attribute ``key`` as identity (ADR 0021)."""
    return key in IDENTITY_KEYS or key.startswith(IDENTITY_PREFIXES)


# PromQL.

# A classic label name, which PromQL takes unquoted; names starting with __ are
# Prometheus's own.
_LABEL_NAME: Final = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# Escapes inside a double-quoted PromQL string: the backslash and the quote; the newline
# and U+FFFD, which Prometheus's lexer refuses raw ("unterminated quoted string",
# "invalid UTF-8 rune"); and every other control character, for readability.
_ESCAPES: Final = str.maketrans(
    {chr(code): f"\\x{code:02x}" for code in (*range(0x20), 0x7F)}
    | {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    | {"\ufffd": "\\ufffd"}
)


def _label(metric: Metric, label: str) -> str:
    if not _LABEL_NAME.fullmatch(label) or label.startswith("__"):
        raise ValueError(f"{label!r} is not a label name a query can use")
    if label not in metric.all_labels:
        raise ValueError(f"{metric.name} has no label {label!r}")
    return label


def _quote(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("a label value is not valid UTF-8 (it has a lone surrogate)") from None
    return '"' + value.translate(_ESCAPES) + '"'


def _window(window: str) -> str:
    # A Prometheus duration, units from the largest down, such as 1h30m or 90d.
    try:
        parse_duration(window)
    except ValueError:
        raise ValueError(
            f"window {window!r} is not a Prometheus duration above zero, such as 1h or 90d"
        ) from None
    return window


def selector(metric: Metric, matchers: Mapping[str, str] | None = None) -> str:
    """The PromQL selector of ``metric``, with one equality matcher per item of
    ``matchers`` in their order. Each value is quoted, with the characters Prometheus
    refuses inside a string (the newline and U+FFFD) and the other control characters
    escaped, so any value that is valid UTF-8 matches itself and cannot change the query.

    A selector also matches series with labels it does not name: to select one series,
    add ``label=""`` for each label of ``metric.all_labels`` that the series lacks.
    Raises ``ValueError`` for a label ``metric`` never has, and for a value that is not
    valid UTF-8."""
    if not matchers:
        return metric.name
    parts = ",".join(f"{_label(metric, key)}={_quote(value)}" for key, value in matchers.items())
    return f"{metric.name}{{{parts}}}"


def total_query(
    metric: Metric,
    *,
    window: str,
    by: Sequence[str] = (),
    matchers: Mapping[str, str] | None = None,
) -> str:
    """The PromQL of ADR 0013 for the exact total of ``metric`` over ``window``:
    ``sum by (<by>) (increase(<selector>[<window>] anchored))``.

    ``window`` must cover what is summed. When it starts long after a series' previous
    sample (more than the 5-minute lookback), the query also needs ``lookback_delta`` on
    the query API. Raises ``ValueError`` for a label ``metric`` never has, a label
    repeated in ``by``, a window that is not a positive duration, or a value that is not
    valid UTF-8, and ``TypeError`` when ``by`` is one string."""
    if isinstance(by, str):
        raise TypeError("by must be a sequence of label names, not one string")
    labels = [_label(metric, label) for label in by]
    if len(set(labels)) != len(labels):
        raise ValueError(f"by names a label more than once: {labels}")
    grouping = f" by ({', '.join(labels)}) " if labels else ""
    return f"sum{grouping}(increase({selector(metric, matchers)}[{_window(window)}] anchored))"
