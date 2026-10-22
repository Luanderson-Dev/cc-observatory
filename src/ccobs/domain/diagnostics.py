"""The hook failure counts that ``ccobs doctor`` reads, from Loki (ADRs 0011 and 0038).

Claude Code reports each run of a hook event's hooks as a
``claude_code.hook_execution_complete`` event (ADR 0012) with, among its attributes,
``num_non_blocking_error`` (hooks that failed: the app down, a refused connection) and
``num_cancelled`` (hooks cut short by their timeout). The counts include the hooks
Claude Code adds itself and the user's own (ADR 0012), so doctor can only warn on them.
The events reach Loki through the collector (ADR 0015), with their attributes as
structured metadata under the same names (no dot to turn into an underscore, ADR 0014),
their values integer strings, which ``unwrap`` reads as sample values.

This module holds the pure half: one instant LogQL query that returns the three counts
over the last ``window_s`` of the host's stream, each tagged with a ``field`` label by
``label_replace``, and the parser of Loki's answer. A sum over no record has no series,
so a missing field counts 0. The query carries no session ID, so Loki's info-level query
log never records one (ADR 0029). ``ccobs.server.diagnostics`` does the HTTP.

It also holds the client side: ``parse_diagnostics`` reads the app's
``GET /api/v1/diagnostics`` document for ``ccobs doctor`` and ``ccobs status``, with the
one set of Loki reasons (``UnavailableReason``) that the server writes, and its optional
``alerts`` part (ADR 0046), which an app older than v0.6.0 does not send.

Data and string building only: no I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

from ccobs.domain.alerts import ALERT_RULES, AlertRule
from ccobs.domain.denials import EVENT_NAME_KEY, SERVICE_NAME_LABEL
from ccobs.domain.sessions import SessionStatus
from ccobs.domain.telemetry import HOST_LABEL, SERVICE_NAME, quote
from ccobs.domain.usage import parse_vector

__all__ = [
    "ALERTS_NOT_UNDERSTOOD",
    "ALERTS_PREDATE",
    "ALERTS_UNREAD",
    "CANCELLED",
    "CANCELLED_KEY",
    "EXECUTIONS",
    "FIELD_LABEL",
    "HOOK_COMPLETE_EVENT",
    "HOOK_FAILURES_WINDOW_S",
    "NON_BLOCKING_ERROR",
    "NON_BLOCKING_ERROR_KEY",
    "UNAVAILABLE_REASONS",
    "AlertsUnavailable",
    "CountedHookFailures",
    "Diagnostics",
    "HookFailures",
    "ReportedAlerts",
    "ReportedDiscord",
    "ReportedHooks",
    "ReportedReason",
    "ReportedSessions",
    "UnavailableReason",
    "UncountedHookFailures",
    "hook_failures_query",
    "parse_diagnostics",
    "parse_hook_failures",
]

# The event_name of the records counted (ADR 0012), and the attributes summed.
HOOK_COMPLETE_EVENT: Final = "hook_execution_complete"
NON_BLOCKING_ERROR_KEY: Final = "num_non_blocking_error"
CANCELLED_KEY: Final = "num_cancelled"
# How far back the counts reach: the last hour.
HOOK_FAILURES_WINDOW_S: Final = 3600
# The label that tells the three series of the answer apart, and its values.
FIELD_LABEL: Final = "field"
EXECUTIONS: Final = "executions"
NON_BLOCKING_ERROR: Final = "non_blocking_error"
CANCELLED: Final = "cancelled"
_FIELDS: Final = frozenset({EXECUTIONS, NON_BLOCKING_ERROR, CANCELLED})


@dataclass(frozen=True, slots=True)
class HookFailures:
    """Over the window: how many ``hook_execution_complete`` records Loki holds, and the
    sums of their ``num_non_blocking_error`` and ``num_cancelled``."""

    executions: int
    non_blocking_error: int
    cancelled: int


def _tagged(expression: str, field: str) -> str:
    return f'label_replace({expression}, "{FIELD_LABEL}", "{field}", "", "")'


def hook_failures_query(host: str, window_s: int = HOOK_FAILURES_WINDOW_S) -> str:
    """The instant LogQL of the three counts over the last ``window_s`` seconds of
    ``host``'s stream. ``host`` is quoted (``telemetry.quote``). Raises ``ValueError``
    for a window that is not positive or a host that is not valid UTF-8."""
    if window_s <= 0:
        raise ValueError(f"the window must be positive, got {window_s}")
    selector = f"{{{SERVICE_NAME_LABEL}={quote(SERVICE_NAME)}, {HOST_LABEL}={quote(host)}}}"
    pipeline = f"{selector} | {EVENT_NAME_KEY}={quote(HOOK_COMPLETE_EVENT)}"
    window = f"[{window_s}s]"

    def summed(key: str) -> str:
        # A value that is not a number would fail the whole query: it is left out.
        return f'sum(sum_over_time({pipeline} | unwrap {key} | __error__="" {window}))'

    return " or ".join(
        (
            _tagged(f"sum(count_over_time({pipeline} {window}))", EXECUTIONS),
            _tagged(summed(NON_BLOCKING_ERROR_KEY), NON_BLOCKING_ERROR),
            _tagged(summed(CANCELLED_KEY), CANCELLED),
        )
    )


def parse_hook_failures(document: object) -> HookFailures | None:
    """The counts in Loki's answer to ``hook_failures_query`` (the JSON already
    decoded); ``None`` when it is not a valid vector (``usage.parse_vector``: finite,
    non-negative values, one series per field), names a field the query does not make,
    or holds a value that is not a whole number."""
    vector = parse_vector(document, FIELD_LABEL)
    if vector is None:
        return None
    counts: dict[str, int] = {}
    for field, value in vector:
        if field not in _FIELDS or not value.is_integer():
            return None
        counts[field] = int(value)
    return HookFailures(
        executions=counts.get(EXECUTIONS, 0),
        non_blocking_error=counts.get(NON_BLOCKING_ERROR, 0),
        cancelled=counts.get(CANCELLED, 0),
    )


# The client side: the app's ``GET /api/v1/diagnostics`` document, as ``ccobs doctor``
# and ``ccobs status`` read it. One parser for both, so they agree on what a valid
# document is and on the reasons they show.

# Why Loki gave no counts (ADR 0038): no connection, no answer within the timeout, a
# non-2xx status, or an answer that is not the vector of counts.
type UnavailableReason = Literal["unreachable", "timeout", "status", "shape"]
UNAVAILABLE_REASONS: Final[frozenset[str]] = frozenset(get_args(UnavailableReason.__value__))
# What the CLI shows for a reason outside ADR 0038's set, so a stray string never
# reaches the output.
type ReportedReason = UnavailableReason | Literal["unknown"]
# A version from the app is kept only when it looks like one.
_VERSION_TEXT: Final = re.compile(r"[0-9A-Za-z.+-]{1,64}")
_STATES: Final[tuple[SessionStatus, ...]] = get_args(SessionStatus.__value__)


@dataclass(frozen=True, slots=True)
class ReportedSessions:
    """A count for every session state of ADR 0024, and the blocks not closed."""

    by_state: dict[SessionStatus, int]
    open_blocks: int


@dataclass(frozen=True, slots=True)
class ReportedHooks:
    """Hooks received by event label and dropped by reason, since the app started."""

    received: dict[str, int]
    dropped: dict[str, int]


@dataclass(frozen=True, slots=True)
class CountedHookFailures:
    """Loki's counts over the last ``window_s`` seconds."""

    window_s: int
    counts: HookFailures


@dataclass(frozen=True, slots=True)
class UncountedHookFailures:
    """Loki gave no counts, for ``reason`` (``unknown`` outside ADR 0038's set)."""

    window_s: int
    reason: ReportedReason


@dataclass(frozen=True, slots=True)
class ReportedDiscord:
    """Where Discord delivery stands (ADR 0046): whether a webhook is configured, the
    status (401, 403 or 404) that suspended sending until the app restarts, the
    deliveries still ``pending``, and those of the alerts fired in the last 24 hours
    that ended ``failed`` or ``expired``."""

    configured: bool
    suspended_status: int | None
    pending: int
    failed_24h: int
    expired_24h: int


@dataclass(frozen=True, slots=True)
class ReportedAlerts:
    """The alerts part: the rules on (ADR 0041's names, in its order), the alerts
    still on, those fired in the last 24 hours, and Discord's delivery."""

    rules: tuple[AlertRule, ...]
    open: int
    fired_24h: int
    discord: ReportedDiscord


# Why the document has no alerts part to show: the app could not read its alerts
# (``alerts: null``), it predates them (no ``alerts`` field: older than v0.6.0), or the
# part is not of the shape this CLI reads (an app newer or older than the CLI).
type AlertsUnavailable = Literal["unread", "predates", "not_understood"]
ALERTS_UNREAD: Final[AlertsUnavailable] = "unread"
ALERTS_PREDATE: Final[AlertsUnavailable] = "predates"
ALERTS_NOT_UNDERSTOOD: Final[AlertsUnavailable] = "not_understood"


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """The parts of the document doctor and status use. ``version`` is the app's, or
    ``unknown`` when it does not look like a version, so it is safe to show."""

    version: str
    sessions: ReportedSessions
    hooks: ReportedHooks
    hook_failures: CountedHookFailures | UncountedHookFailures
    alerts: ReportedAlerts | AlertsUnavailable = ALERTS_PREDATE


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    counts: dict[str, int] = {}
    for key, item in value.items():
        count = _count(item)
        if count is None:
            return None
        counts[key] = count
    return counts


def _sessions(value: object) -> ReportedSessions | None:
    if not isinstance(value, dict):
        return None
    by_state, open_blocks = value.get("by_state"), _count(value.get("open_blocks"))
    if not isinstance(by_state, dict) or open_blocks is None:
        return None
    # Only the states of ADR 0024, so a stray name never reaches the output.
    counts: dict[SessionStatus, int] = {}
    for state in _STATES:
        count = _count(by_state.get(state, 0))
        if count is None:
            return None
        counts[state] = count
    return ReportedSessions(counts, open_blocks)


def _hooks(value: object) -> ReportedHooks | None:
    if not isinstance(value, dict):
        return None
    received, dropped = _counts(value.get("received")), _counts(value.get("dropped"))
    if received is None or dropped is None:
        return None
    return ReportedHooks(received, dropped)


def _reason(value: object) -> ReportedReason:
    if isinstance(value, str) and value in UNAVAILABLE_REASONS:
        return cast(UnavailableReason, value)
    return "unknown"


def _failures(value: object) -> CountedHookFailures | UncountedHookFailures | None:
    if not isinstance(value, dict):
        return None
    window_s, available = _count(value.get("window_s")), value.get("available")
    if window_s is None:
        return None
    if available is False:
        return UncountedHookFailures(window_s, _reason(value.get("reason")))
    if available is not True:
        return None
    executions = _count(value.get(EXECUTIONS))
    errors = _count(value.get(NON_BLOCKING_ERROR))
    cancelled = _count(value.get(CANCELLED))
    if executions is None or errors is None or cancelled is None:
        return None
    return CountedHookFailures(window_s, HookFailures(executions, errors, cancelled))


def _status_code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _discord(value: object) -> ReportedDiscord | None:
    if not isinstance(value, dict) or "suspended_status" not in value:
        return None
    configured, suspended = value.get("configured"), value.get("suspended_status")
    status = _status_code(suspended)
    pending = _count(value.get("pending"))
    failed = _count(value.get("failed_24h"))
    expired = _count(value.get("expired_24h"))
    if (
        not isinstance(configured, bool)
        or (suspended is not None and status is None)
        or pending is None
        or failed is None
        or expired is None
    ):
        return None
    return ReportedDiscord(configured, status, pending, failed, expired)


def _rules(value: object) -> tuple[AlertRule, ...] | None:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        return None
    # Only ADR 0041's names, in its order, so a stray name never reaches the output.
    return tuple(rule for rule in ALERT_RULES if rule in value)


def _alerts(document: dict[str, object]) -> ReportedAlerts | AlertsUnavailable:
    if "alerts" not in document:
        return ALERTS_PREDATE
    value = document["alerts"]
    if value is None:
        return ALERTS_UNREAD
    if not isinstance(value, dict):
        return ALERTS_NOT_UNDERSTOOD
    rules, discord = _rules(value.get("rules")), _discord(value.get("discord"))
    open_, fired = _count(value.get("open")), _count(value.get("fired_24h"))
    if rules is None or discord is None or open_ is None or fired is None:
        return ALERTS_NOT_UNDERSTOOD
    return ReportedAlerts(rules, open_, fired, discord)


def parse_diagnostics(body: bytes) -> Diagnostics | None:
    """The app's diagnostics document (ADR 0038) from the body of its answer; ``None``
    when it is not JSON, or a part doctor or status use is missing or not of the shape
    the app writes (counts are non-negative integers). A state the app did not list
    counts 0; a state ADR 0024 does not name is left out. The ``alerts`` part (ADR
    0046) is optional: absent (an app older than v0.6.0) it is ``ALERTS_PREDATE``,
    ``null`` (the app could not read its alerts) ``ALERTS_UNREAD``, and not of the shape
    the app writes ``ALERTS_NOT_UNDERSTOOD``, while the rest of the answer stands: an
    app newer or older than the CLI may change it. Keys this CLI does not know are
    ignored, there as in every part."""
    try:
        document: object = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    version = document.get("version")
    sessions = _sessions(document.get("sessions"))
    hooks = _hooks(document.get("hooks"))
    failures = _failures(document.get("hook_failures"))
    if not isinstance(version, str) or sessions is None or hooks is None or failures is None:
        return None
    shown = version if _VERSION_TEXT.fullmatch(version) else "unknown"
    return Diagnostics(shown, sessions, hooks, failures, _alerts(document))
