"""A session's tokens and cost, read from Prometheus (ADR 0035).

The session page shows each session's totals: its tokens by type and by model, and its
cost by model and in all. They come from Claude Code's own metrics in Prometheus (ADR
0013), with the queries of ADR 0014, ``telemetry.total_query``:
``sum by (<label>) (increase(<name>{host="<host>",session_id="<id>"}[<range>] anchored))``,
as instant queries at ``UsageWindow.time_ms``.

The window covers the whole session: from ``WINDOW_BEFORE_MS`` before it started, so
that each series' first sample, and the zero sample Prometheus stores at its start time
(ADR 0013), fall inside it, to two minutes after its last moment (its end, or its last
event once stale), for the metrics Claude Code exports late, or to now while it is live.
Since nothing of the session comes before the window, ``anchored`` needs no sample from
the lookback before it, and the query needs no ``lookback_delta`` (ADR 0014). A window
whose end has passed is ``final``: its totals no longer change, so they can be cached
longer.

The parser accepts only a well-formed instant vector whose values are finite and not
negative, with one series per label value. Anything else (an error status, another
result type, ``NaN``, an infinity, a negative value, a repeated label value) makes the
whole answer invalid: a total that silently left out a series would be wrong without a
sign of it.

Data and string building only: no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ccobs.domain.grafana_links import SESSION_ID_KEY, WINDOW_AFTER_MS
from ccobs.domain.telemetry import COST_USAGE, HOST_LABEL, TOKEN_USAGE, total_query

__all__ = [
    "TOKEN_TYPES",
    "WINDOW_BEFORE_MS",
    "ModelRow",
    "SessionUsage",
    "UsageQueries",
    "UsageWindow",
    "Vector",
    "format_tokens",
    "format_usd",
    "model_rows",
    "parse_vector",
    "session_usage",
    "token_type_label",
    "usage_queries",
    "usage_window",
]

# How far before the session's start the window begins: past the first sample of each
# series even with a few minutes of clock difference between the app and the machine
# that stamps the telemetry (Docker Desktop's VM clock can drift).
WINDOW_BEFORE_MS: Final = 5 * 60_000
# The type label values of claude_code.token.usage in the fixtures (ADR 0012), in the
# order the page lists them; any other value follows, by name.
TOKEN_TYPES: Final = ("input", "output", "cacheRead", "cacheCreation")
_TYPE_LABELS: Final = {
    "input": "Input",
    "output": "Output",
    "cacheRead": "Cache read",
    "cacheCreation": "Cache creation",
}
_UNKNOWN_TYPE: Final = "Unknown type"
_MS_PER_S: Final = 1000
_SMALLEST_COST: Final = 0.0001
_PAIR: Final = 2

# Label value and value of each series of an instant vector.
type Vector = tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """An instant query's evaluation time and the start its range reaches back to, in
    epoch milliseconds; ``final`` once the session's window has fully passed."""

    start_ms: int
    time_ms: int
    final: bool

    @property
    def range(self) -> str:
        """The range selector's duration, whole seconds rounded up, at least 1 s."""
        span_ms = max(self.time_ms - self.start_ms, 1)
        return f"{-(-span_ms // _MS_PER_S)}s"

    @property
    def time_s(self) -> str:
        """``time_ms`` as the ``time`` parameter of Prometheus's query API: seconds."""
        seconds, millis = divmod(self.time_ms, _MS_PER_S)
        return f"{seconds}.{millis:03d}"


@dataclass(frozen=True, slots=True)
class UsageQueries:
    """The three instant queries of a session's usage."""

    tokens_by_type: str
    tokens_by_model: str
    cost_by_model: str


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """A session's totals: tokens by type (``TOKEN_TYPES`` first), tokens by model and
    cost by model (in USD)."""

    tokens_by_type: Vector
    tokens_by_model: Vector
    cost_by_model: Vector

    @property
    def empty(self) -> bool:
        """No series at all: Prometheus has nothing for the session (yet)."""
        return not (self.tokens_by_type or self.tokens_by_model or self.cost_by_model)

    @property
    def total_tokens(self) -> float:
        return math.fsum(value for _, value in self.tokens_by_type)

    @property
    def total_cost(self) -> float:
        return math.fsum(value for _, value in self.cost_by_model)


@dataclass(frozen=True, slots=True)
class ModelRow:
    """One model's tokens and cost; ``None`` where Prometheus had no series."""

    model: str
    tokens: float | None
    cost: float | None


def usage_window(started_at_ms: int, last_ms: int | None, now_ms: int) -> UsageWindow:
    """The window of a session that started at ``started_at_ms``: from
    ``WINDOW_BEFORE_MS`` before (never before the epoch) to two minutes after
    ``last_ms``, its last moment, capped at ``now_ms``; up to ``now_ms`` while it is
    live (``None``)."""
    start = max(0, started_at_ms - WINDOW_BEFORE_MS)
    if last_ms is None:
        return UsageWindow(start_ms=start, time_ms=now_ms, final=False)
    end = last_ms + WINDOW_AFTER_MS
    return UsageWindow(start_ms=start, time_ms=min(end, now_ms), final=end <= now_ms)


def usage_queries(host: str, session_id: str, window: UsageWindow) -> UsageQueries:
    """The PromQL of a session's usage over ``window`` (ADR 0014's totals). Raises
    ``ValueError`` for a value that is not valid UTF-8."""
    matchers = {HOST_LABEL: host, SESSION_ID_KEY: session_id}
    return UsageQueries(
        tokens_by_type=total_query(
            TOKEN_USAGE, window=window.range, by=("type",), matchers=matchers
        ),
        tokens_by_model=total_query(
            TOKEN_USAGE, window=window.range, by=("model",), matchers=matchers
        ),
        cost_by_model=total_query(
            COST_USAGE, window=window.range, by=("model",), matchers=matchers
        ),
    )


def _sample(item: object, label: str) -> tuple[str, float] | None:
    if not isinstance(item, Mapping):
        return None
    metric, pair = item.get("metric"), item.get("value")
    if not isinstance(metric, Mapping) or not isinstance(pair, list) or len(pair) != _PAIR:
        return None
    name = metric.get(label, "")
    raw = pair[1]
    if not isinstance(name, str) or not isinstance(raw, str):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    # -0 reads as 0.
    return name, value + 0.0


def parse_vector(document: object, label: str) -> Vector | None:
    """The series of an instant query's answer as (value of ``label``, value) pairs, in
    the answer's order; ``None`` when the answer is not a valid vector (see the module's
    docstring). A series without ``label`` is kept under ``""``."""
    if not isinstance(document, Mapping) or document.get("status") != "success":
        return None
    data = document.get("data")
    if not isinstance(data, Mapping) or data.get("resultType") != "vector":
        return None
    result = data.get("result")
    if not isinstance(result, list):
        return None
    samples: list[tuple[str, float]] = []
    for item in result:
        sample = _sample(item, label)
        if sample is None:
            return None
        samples.append(sample)
    if len({name for name, _ in samples}) != len(samples):
        return None
    return tuple(samples)


def _type_order(item: tuple[str, float]) -> tuple[int, str]:
    name = item[0]
    known = TOKEN_TYPES.index(name) if name in TOKEN_TYPES else len(TOKEN_TYPES)
    return known, name


def session_usage(
    tokens_by_type: Vector, tokens_by_model: Vector, cost_by_model: Vector
) -> SessionUsage:
    """The usage of three parsed answers, tokens by type in the page's order."""
    return SessionUsage(
        tokens_by_type=tuple(sorted(tokens_by_type, key=_type_order)),
        tokens_by_model=tokens_by_model,
        cost_by_model=cost_by_model,
    )


def model_rows(usage: SessionUsage) -> list[ModelRow]:
    """One row per model, the costliest first, then by tokens, then by name."""
    tokens = dict(usage.tokens_by_model)
    costs = dict(usage.cost_by_model)
    rows = [
        ModelRow(model=model, tokens=tokens.get(model), cost=costs.get(model))
        for model in tokens.keys() | costs.keys()
    ]
    return sorted(rows, key=lambda row: (-(row.cost or 0.0), -(row.tokens or 0.0), row.model))


def format_tokens(value: float) -> str:
    """A token count: a whole number with thousands separators."""
    return f"{round(value):,}"


def format_usd(value: float) -> str:
    """A cost in USD: four decimals under a dollar, where the cost of a short session
    sits, two from a dollar up."""
    if value == 0:
        return "$0.00"
    if value < _SMALLEST_COST:
        return "under $0.0001"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def token_type_label(value: str) -> str:
    """A token type in words; an unknown one as stored."""
    if not value:
        return _UNKNOWN_TYPE
    return _TYPE_LABELS.get(value, value)
