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

Data and string building only: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ccobs.domain.denials import EVENT_NAME_KEY, SERVICE_NAME_LABEL
from ccobs.domain.telemetry import HOST_LABEL, SERVICE_NAME, quote
from ccobs.domain.usage import parse_vector

__all__ = [
    "CANCELLED",
    "CANCELLED_KEY",
    "EXECUTIONS",
    "FIELD_LABEL",
    "HOOK_COMPLETE_EVENT",
    "HOOK_FAILURES_WINDOW_S",
    "NON_BLOCKING_ERROR",
    "NON_BLOCKING_ERROR_KEY",
    "HookFailures",
    "hook_failures_query",
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
