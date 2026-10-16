"""Durations in Prometheus's units, the format of ``CCOBS_PROMETHEUS_RETENTION`` (ADR 0016).

The app's own durations (``CCOBS_APP_RETENTION``, ``CCOBS_SESSION_STALE_AFTER``) take the
same format, so one rule covers every ``CCOBS_*`` duration that is not Tempo's or Loki's.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ccobs.domain.durations import DURATION_FORMAT, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90d", timedelta(days=90)),
        ("12h", timedelta(hours=12)),
        ("30m", timedelta(minutes=30)),
        ("1w", timedelta(weeks=1)),
        ("1y", timedelta(days=365)),
        ("45s", timedelta(seconds=45)),
        ("250ms", timedelta(milliseconds=250)),
        ("720h", timedelta(days=30)),
        # Units in Prometheus's order may be combined.
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1y2w3d4h5m6s7ms", timedelta(days=365 + 14 + 3, hours=4, minutes=5, seconds=6.007)),
        # A zero part is fine as long as the total is positive.
        ("0d12h", timedelta(hours=12)),
    ],
)
def test_parses_prometheus_durations(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "90",
        "d",
        "90 d",
        " 90d",
        "90d ",
        "90D",
        "-1d",
        "+1d",
        "1.5h",
        "30m1h",
        "1h1h",
        "7days",
        "P90D",
        "90d!",
        # A fullwidth digit one: \d would match it, Prometheus's grammar does not.
        "\uff11d",
    ],
)
def test_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="is not a duration"):
        parse_duration(text)


@pytest.mark.parametrize("text", ["0", "0d", "0s", "0h0m", "0ms"])
def test_rejects_zero(text: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        parse_duration(text)


def test_rejects_durations_too_large_to_represent() -> None:
    with pytest.raises(ValueError, match="too large"):
        parse_duration("99999999999999y")


def test_error_names_the_format_and_quotes_the_value() -> None:
    with pytest.raises(ValueError, match="is not a duration") as caught:
        parse_duration("soon")
    message = str(caught.value)
    assert "'soon'" in message
    assert DURATION_FORMAT in message


def test_format_lists_prometheus_units() -> None:
    # The wording .env.example uses for CCOBS_PROMETHEUS_RETENTION.
    assert "y, w, d, h, m, s or ms" in DURATION_FORMAT
