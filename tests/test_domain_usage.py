"""Unit tests of ``ccobs.domain.usage``: a session's tokens and cost from Prometheus.

The window covers the whole session, from five minutes before it started to its last
moment plus two minutes (capped at now), so no ``lookback_delta`` is needed (ADR 0014).
The queries are ``telemetry.total_query`` exactly, and the parser accepts only a
well-formed instant vector of finite, non-negative values: anything else is invalid as a
whole, never a partial total.
"""

from __future__ import annotations

import math
from typing import Any, Final

import pytest

from ccobs.domain.grafana_links import WINDOW_AFTER_MS
from ccobs.domain.telemetry import COST_USAGE, TOKEN_USAGE, total_query
from ccobs.domain.usage import (
    TOKEN_TYPES,
    WINDOW_BEFORE_MS,
    ModelRow,
    SessionUsage,
    UsageQueries,
    UsageWindow,
    format_tokens,
    format_usd,
    model_rows,
    parse_vector,
    session_usage,
    token_type_label,
    usage_queries,
    usage_window,
)

HOST: Final = "local"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
STARTED: Final = 1_800_000_000_000
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS


def vector(*items: tuple[dict[str, str], Any]) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": metric, "value": [1_800_000_000.5, value]} for metric, value in items
            ],
        },
    }


# The window.


def test_the_stored_token_types_are_those_of_the_fixtures() -> None:
    # The type values of claude_code.token.usage in every OTLP fixture (ADR 0012).
    assert TOKEN_TYPES == ("input", "output", "cacheRead", "cacheCreation")


def test_a_live_session_is_read_up_to_now_from_five_minutes_before_it_started() -> None:
    now = STARTED + HOUR_MS

    window = usage_window(STARTED, None, now)

    assert WINDOW_BEFORE_MS == 5 * MINUTE_MS
    assert window == UsageWindow(start_ms=STARTED - WINDOW_BEFORE_MS, time_ms=now, final=False)
    assert window.range == "3900s"


def test_an_ended_session_is_read_up_to_two_minutes_after_its_end() -> None:
    ended = STARTED + HOUR_MS
    now = ended + HOUR_MS

    window = usage_window(STARTED, ended, now)

    assert window.time_ms == ended + WINDOW_AFTER_MS
    assert window.final is True
    assert window.range == f"{(HOUR_MS + WINDOW_BEFORE_MS + WINDOW_AFTER_MS) // 1000}s"


def test_a_session_that_just_ended_is_read_up_to_now_and_is_not_final() -> None:
    ended = STARTED + HOUR_MS
    now = ended + MINUTE_MS

    window = usage_window(STARTED, ended, now)

    assert window.time_ms == now
    assert window.final is False


def test_the_end_two_minutes_after_is_final_exactly_then() -> None:
    ended = STARTED + HOUR_MS

    window = usage_window(STARTED, ended, ended + WINDOW_AFTER_MS)

    assert window.time_ms == ended + WINDOW_AFTER_MS
    assert window.final is True


def test_the_range_rounds_partial_seconds_up() -> None:
    window = usage_window(STARTED, None, STARTED + 1)

    # 300.001 s: the range must reach back to the window's start.
    assert window.range == "301s"


def test_a_clock_behind_the_session_still_gives_a_positive_range() -> None:
    window = usage_window(STARTED, None, STARTED - HOUR_MS)

    assert window.time_ms == STARTED - HOUR_MS
    assert window.range == "1s"


def test_the_window_never_starts_before_the_epoch() -> None:
    window = usage_window(1_000, None, 61_000)

    assert window.start_ms == 0
    assert window.range == "61s"


# The queries.


def test_the_queries_are_total_queries_over_the_window() -> None:
    window = usage_window(STARTED, STARTED + HOUR_MS, STARTED + 2 * HOUR_MS)
    matchers = {"host": HOST, "session_id": SID}

    queries = usage_queries(HOST, SID, window)

    assert queries == UsageQueries(
        tokens_by_type=total_query(TOKEN_USAGE, window="4020s", by=("type",), matchers=matchers),
        tokens_by_model=total_query(TOKEN_USAGE, window="4020s", by=("model",), matchers=matchers),
        cost_by_model=total_query(COST_USAGE, window="4020s", by=("model",), matchers=matchers),
    )
    assert queries.tokens_by_type == (
        "sum by (type) (increase(claude_code_token_usage_tokens_total"
        f'{{host="local",session_id="{SID}"}}[4020s] anchored))'
    )
    assert queries.cost_by_model == (
        "sum by (model) (increase(claude_code_cost_usage_USD_total"
        f'{{host="local",session_id="{SID}"}}[4020s] anchored))'
    )


def test_the_queries_quote_a_hostile_session_id() -> None:
    queries = usage_queries(HOST, 'x"}) or vector(1) #', usage_window(STARTED, None, STARTED))

    assert 'session_id="x\\"}) or vector(1) #"' in queries.tokens_by_type


def test_the_query_time_is_in_seconds() -> None:
    window = UsageWindow(start_ms=0, time_ms=1_800_000_000_123, final=False)

    assert window.time_s == "1800000000.123"


# Parsing.


def test_a_vector_parses_into_label_values() -> None:
    document = vector(({"type": "input"}, "120"), ({"type": "output"}, "4.5"))

    assert parse_vector(document, "type") == (("input", 120.0), ("output", 4.5))


def test_an_empty_vector_is_valid_and_empty() -> None:
    assert parse_vector(vector(), "model") == ()


def test_a_series_without_the_label_is_kept_under_an_empty_value() -> None:
    assert parse_vector(vector(({}, "3")), "model") == (("", 3.0),)


def test_negative_zero_reads_as_zero() -> None:
    assert parse_vector(vector(({"model": "m"}, "-0")), "model") == (("m", 0.0),)


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf", "-1", "-0.001", "abc", "", 3, None])
def test_a_value_that_is_not_a_finite_non_negative_number_is_invalid(value: object) -> None:
    document = vector(({"model": "good"}, "1"), ({"model": "bad"}, value))

    assert parse_vector(document, "model") is None


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "text",
        {"status": "error", "errorType": "bad_data", "error": "parse error"},
        {"status": "success"},
        {"status": "success", "data": []},
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
        {"status": "success", "data": {"resultType": "vector"}},
        {"status": "success", "data": {"resultType": "vector", "result": {}}},
        {"status": "success", "data": {"resultType": "vector", "result": [1]}},
        {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "1"]}]}},
        {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}},
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": [], "value": [1, "1"]}]},
        },
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": ["1"]}]},
        },
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": "1"}]},
        },
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"model": 7}, "value": [1, "1"]}],
            },
        },
    ],
)
def test_anything_but_a_vector_is_invalid(document: object) -> None:
    assert parse_vector(document, "model") is None


def test_a_label_value_repeated_is_invalid() -> None:
    document = vector(({"model": "m"}, "1"), ({"model": "m"}, "2"))

    assert parse_vector(document, "model") is None


# The usage.


def test_tokens_by_type_follow_the_stored_order_then_the_rest_by_name() -> None:
    usage = session_usage(
        tokens_by_type=(("zeta", 1.0), ("cacheCreation", 4.0), ("output", 2.0), ("alpha", 5.0)),
        tokens_by_model=(),
        cost_by_model=(),
    )

    assert [name for name, _ in usage.tokens_by_type] == [
        "output",
        "cacheCreation",
        "alpha",
        "zeta",
    ]


def test_totals_add_every_type_and_model() -> None:
    usage = session_usage(
        tokens_by_type=(("input", 10.0), ("output", 5.0)),
        tokens_by_model=(("m", 15.0),),
        cost_by_model=(("m", 0.25), ("n", 0.5)),
    )

    assert usage.total_tokens == 15.0
    assert usage.total_cost == 0.75
    assert usage.empty is False


def test_no_series_at_all_is_empty() -> None:
    usage = session_usage(tokens_by_type=(), tokens_by_model=(), cost_by_model=())

    assert usage == SessionUsage(tokens_by_type=(), tokens_by_model=(), cost_by_model=())
    assert usage.empty is True
    assert usage.total_tokens == 0
    assert usage.total_cost == 0


def test_model_rows_join_tokens_and_cost_the_costliest_first() -> None:
    usage = session_usage(
        tokens_by_type=(),
        tokens_by_model=(("cheap", 900.0), ("dear", 100.0), ("tokens-only", 50.0)),
        cost_by_model=(("dear", 1.5), ("cheap", 0.01), ("cost-only", 0.2)),
    )

    assert model_rows(usage) == [
        ModelRow(model="dear", tokens=100.0, cost=1.5),
        ModelRow(model="cost-only", tokens=None, cost=0.2),
        ModelRow(model="cheap", tokens=900.0, cost=0.01),
        ModelRow(model="tokens-only", tokens=50.0, cost=None),
    ]


def test_model_rows_with_equal_cost_go_by_tokens_then_name() -> None:
    usage = session_usage(
        tokens_by_type=(),
        tokens_by_model=(("b", 10.0), ("a", 10.0), ("c", 20.0)),
        cost_by_model=(),
    )

    assert [row.model for row in model_rows(usage)] == ["c", "a", "b"]


# Formatting.


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.0, "0"), (1.0, "1"), (1234.0, "1,234"), (1234567.4, "1,234,567"), (2.5, "2")],
)
def test_tokens_are_whole_numbers_with_thousands_separators(value: float, text: str) -> None:
    assert format_tokens(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, "$0.00"),
        (0.00004, "under $0.0001"),
        (0.0050783, "$0.0051"),
        (0.5, "$0.5000"),
        (0.99994, "$0.9999"),
        (1.0, "$1.00"),
        (12.345, "$12.35"),
        (1234.5, "$1,234.50"),
    ],
)
def test_costs_show_four_decimals_under_a_dollar_and_two_from_one(value: float, text: str) -> None:
    assert format_usd(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("input", "Input"),
        ("output", "Output"),
        ("cacheRead", "Cache read"),
        ("cacheCreation", "Cache creation"),
        ("other", "other"),
        ("", "Unknown type"),
    ],
)
def test_token_types_read_as_words(value: str, text: str) -> None:
    assert token_type_label(value) == text


def test_nan_never_reaches_the_formatters_through_the_parser() -> None:
    assert parse_vector(vector(({"model": "m"}, "NaN")), "model") is None
    assert not math.isnan(session_usage((), (), ()).total_cost)
