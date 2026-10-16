"""Durations in Prometheus's units, such as ``90d``, ``12h`` or ``1h30m``.

``CCOBS_PROMETHEUS_RETENTION`` takes Prometheus's own format (ADR 0016), and the app's
durations (``CCOBS_APP_RETENTION``, ``CCOBS_SESSION_STALE_AFTER``) take the same one, so
that a user writes every one of them alike. The grammar is Prometheus's
``model.ParseDuration``: one or more of ``y``, ``w``, ``d``, ``h``, ``m``, ``s`` and
``ms``, each once and in that order, with a year of 365 days. Unlike Prometheus, zero
is refused: every duration here is a period that must be positive.

Pure parsing: no I/O.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Final

__all__ = ["DURATION_FORMAT", "parse_duration"]

# The wording that .env.example uses for CCOBS_PROMETHEUS_RETENTION.
DURATION_FORMAT: Final = "a duration in Prometheus's units: y, w, d, h, m, s or ms (such as 90d)"

# Prometheus's duration grammar. [0-9] and not \d, which also matches non-ASCII digits.
_UNITS: Final = (
    ("y", timedelta(days=365)),
    ("w", timedelta(weeks=1)),
    ("d", timedelta(days=1)),
    ("h", timedelta(hours=1)),
    ("m", timedelta(minutes=1)),
    ("s", timedelta(seconds=1)),
    ("ms", timedelta(milliseconds=1)),
)
_PATTERN: Final = re.compile("".join(f"(?:(?P<{unit}>[0-9]+){unit})?" for unit, _ in _UNITS))


def parse_duration(text: str) -> timedelta:
    """Return the positive duration ``text`` stands for.

    Raises ``ValueError`` for anything outside the grammar, for zero, and for a value
    too large for ``timedelta``.
    """
    # Prometheus also takes a bare "0", which is refused below like any other zero.
    match = _PATTERN.fullmatch("0s" if text == "0" else text)
    if not text or match is None:
        raise ValueError(f"{text!r} is not a duration: use {DURATION_FORMAT}")
    try:
        total = sum(
            (step * int(match[unit]) for unit, step in _UNITS if match[unit] is not None),
            timedelta(0),
        )
    except OverflowError:
        raise ValueError(f"{text!r} is too large for a duration") from None
    if total <= timedelta(0):
        raise ValueError(f"{text!r} is zero: a duration must be positive")
    return total
