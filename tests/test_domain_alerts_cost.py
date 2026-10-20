"""Unit tests of the cost rules of ``ccobs.domain.alerts``: their PromQL, the parser of
Prometheus's answers and the ``session_cost`` and ``daily_cost`` evaluators (ADR 0041).

Time is a plain integer in milliseconds (a fake clock): the evaluators never read the
clock, and a sample carries the time its queries ran.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final

import pytest

from ccobs.domain.alerts import (
    COST_CLEAR_RATIO,
    COST_STALE_MS,
    DAILY_COST_WINDOW,
    DAY_SUBJECT,
    AlertCleared,
    AlertConfig,
    AlertEffect,
    AlertFired,
    AlertKey,
    AlertRule,
    AlertState,
    CostSample,
    DailyCostRule,
    KeyState,
    SessionCostRule,
    daily_cost_query,
    evaluate_daily_cost,
    evaluate_session_cost,
    evaluate_waiting_user,
    parse_cost_sample,
    session_cost_query,
    session_cost_since,
)
from ccobs.domain.sessions import Session, SessionState, SessionStatus
from ccobs.domain.telemetry import COST_USAGE
from ccobs.domain.usage import WINDOW_BEFORE_MS, usage_queries, usage_window

T0: Final = 1_800_000_000_000
COOLDOWN_MS: Final = 300_000
ON: Final = AlertConfig(
    session_cost=SessionCostRule(enabled=True, limit_usd=10.0),
    daily_cost=DailyCostRule(enabled=True, limit_usd=50.0),
)
OFF: Final = AlertConfig()


def session(
    session_id: str = "s1", status: SessionStatus = "working", started_at_ms: int = T0 - 60_000
) -> SessionState:
    return SessionState(
        session=Session(
            session_id=session_id,
            status=status,
            started_at_ms=started_at_ms,
            last_event_at_ms=T0,
            last_seq=1,
        )
    )


def sample(at: int, daily: float | None = None, **sessions: float) -> CostSample:
    return CostSample(observed_at_ms=at, sessions=sessions, daily_usd=daily)


def fired(rule: AlertRule, subject: str, at: int, value: float, limit: float) -> AlertFired:
    return AlertFired(rule=rule, subject=subject, fired_at_ms=at, value=value, threshold=limit)


def cleared(rule: AlertRule, subject: str, at: int) -> AlertCleared:
    return AlertCleared(rule=rule, subject=subject, cleared_at_ms=at)


class Sessions:
    """Feeds ``evaluate_session_cost`` one call at a time and keeps its state."""

    def __init__(self, config: AlertConfig = ON) -> None:
        self.state = AlertState()
        self.config = config

    def __call__(
        self, now_ms: int, cost: CostSample | None, *sessions: SessionState
    ) -> list[AlertEffect]:
        self.state, effects = evaluate_session_cost(
            self.state, cost, sessions, config=self.config, now_ms=now_ms
        )
        return effects


class Daily:
    """Feeds ``evaluate_daily_cost`` one call at a time and keeps its state."""

    def __init__(self, config: AlertConfig = ON) -> None:
        self.state = AlertState()
        self.config = config

    def __call__(self, now_ms: int, cost: CostSample | None) -> list[AlertEffect]:
        self.state, effects = evaluate_daily_cost(
            self.state, cost, config=self.config, now_ms=now_ms
        )
        return effects


# The queries.


def test_the_constants() -> None:
    assert COST_STALE_MS == 180_000
    assert COST_CLEAR_RATIO == 0.9
    assert DAILY_COST_WINDOW == "24h"


def test_the_session_query_has_every_session_and_no_session_id_matcher() -> None:
    query = session_cost_query("local", since_ms=T0 - 60_000, now_ms=T0)
    assert query == (
        "sum by (session_id) "
        '(increase(claude_code_cost_usage_USD_total{host="local"}[360s] anchored))'
    )
    assert COST_USAGE.name in query
    assert 'session_id="' not in query


def test_the_session_query_reaches_back_as_far_as_the_session_pages_window() -> None:
    since, now = T0 - 3_600_000, T0 + 1_234
    window = usage_window(since, None, now)
    query = session_cost_query("local", since_ms=since, now_ms=now)
    # The range the session page uses for the oldest live session, so its total matches.
    assert f"[{window.range}] anchored" in query
    assert window.range == f"{-(-(3_600_000 + WINDOW_BEFORE_MS + 1_234) // 1000)}s"
    page = usage_queries("local", "s1", window).cost_by_model
    assert (
        page.replace('host="local",session_id="s1"', 'host="local"').replace(
            "by (model)", "by (session_id)"
        )
        == query
    )


def test_the_query_starts_at_the_oldest_open_session() -> None:
    held = (
        session("a", "working", T0 - 10_000),
        session("b", "idle", T0 - 50_000),
        session("c", "ended", T0 - 900_000),
        session("d", "stale", T0 - 800_000),
        session("e", "waiting_user", T0 - 30_000),
    )
    # Ended and stale sessions cannot alert, so they never stretch the range.
    assert session_cost_since(held) == T0 - 50_000
    assert session_cost_since(iter(held)) == T0 - 50_000


def test_no_open_session_means_no_query() -> None:
    assert session_cost_since(()) is None
    assert session_cost_since((session("c", "ended"), session("d", "stale"))) is None


def test_the_daily_query_is_the_rolling_24_hours_across_sessions() -> None:
    assert daily_cost_query("local") == (
        'sum(increase(claude_code_cost_usage_USD_total{host="local"}[24h] anchored))'
    )


@pytest.mark.parametrize(
    ("host", "quoted"),
    [
        ('a"b', r'"a\"b"'),
        ("a\\b", r'"a\\b"'),
        ("a\nb}", r'"a\nb}"'),
    ],
)
def test_the_host_label_is_escaped(host: str, quoted: str) -> None:
    assert f"{{host={quoted}}}" in session_cost_query(host, since_ms=T0, now_ms=T0)
    assert f"{{host={quoted}}}" in daily_cost_query(host)


def test_a_host_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        daily_cost_query("\ud800")
    with pytest.raises(ValueError, match="UTF-8"):
        session_cost_query("\ud800", since_ms=T0, now_ms=T0)


# The parser.


def answer(*series: tuple[Mapping[str, object], object]) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": metric, "value": [1.0, value]} for metric, value in series],
        },
    }


def test_parses_the_sessions_and_the_day() -> None:
    parsed = parse_cost_sample(
        observed_at_ms=T0,
        sessions=answer(({"session_id": "s1"}, "1.5"), ({"session_id": "s2"}, "12")),
        daily=answer(({}, "42.25")),
    )
    assert parsed == CostSample(
        observed_at_ms=T0, sessions={"s1": 1.5, "s2": 12.0}, daily_usd=42.25
    )


def test_the_day_is_optional() -> None:
    parsed = parse_cost_sample(observed_at_ms=T0, sessions=answer(({"session_id": "s1"}, "1")))
    assert parsed == CostSample(observed_at_ms=T0, sessions={"s1": 1.0}, daily_usd=None)


def test_empty_answers_are_no_sessions_and_no_cost() -> None:
    # sum() over no series is an empty vector: nothing was spent in the last 24 h.
    parsed = parse_cost_sample(observed_at_ms=T0, sessions=answer(), daily=answer())
    assert parsed == CostSample(observed_at_ms=T0, sessions={}, daily_usd=0.0)


def test_a_series_without_a_session_id_is_left_out() -> None:
    # OTEL_METRICS_INCLUDE_SESSION_ID=false: one series with no session_id label.
    parsed = parse_cost_sample(
        observed_at_ms=T0, sessions=answer(({}, "3"), ({"session_id": "s1"}, "1"))
    )
    assert parsed is not None
    assert parsed.sessions == {"s1": 1.0}


VALID: Final = answer(({"session_id": "s1"}, "1"))
MALFORMED: Final[list[object]] = [
    None,
    [],
    "success",
    {"status": "error", "data": {"resultType": "vector", "result": []}},
    {"status": "success"},
    {"status": "success", "data": {"resultType": "matrix", "result": []}},
    {"status": "success", "data": {"resultType": "vector", "result": {}}},
    {"status": "success", "data": {"resultType": "vector", "result": [None]}},
    {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}},
    answer(({"session_id": "s1"}, 1.0)),
    answer(({"session_id": "s1"}, "one")),
    answer(({"session_id": "s1"}, "NaN")),
    answer(({"session_id": "s1"}, "+Inf")),
    answer(({"session_id": "s1"}, "-Inf")),
    answer(({"session_id": "s1"}, "-1")),
    answer(({"session_id": 1}, "1")),
    answer(({"session_id": None}, "1")),
    answer(({"session_id": "s1"}, "1"), ({"session_id": "s1"}, "2")),
]


@pytest.mark.parametrize("document", MALFORMED)
def test_malformed_sessions_are_refused(document: object) -> None:
    assert parse_cost_sample(observed_at_ms=T0, sessions=document, daily=answer()) is None


# None is no daily answer at all (the day was not asked), not a malformed one; a day of
# more than one series is not the sum() it asked for.
@pytest.mark.parametrize(
    "document",
    [
        *(document for document in MALFORMED if document is not None),
        answer(({}, "1"), ({"model": "m"}, "2")),
    ],
)
def test_a_malformed_day_is_refused(document: object) -> None:
    assert parse_cost_sample(observed_at_ms=T0, sessions=VALID, daily=document) is None


def test_negative_zero_reads_as_zero() -> None:
    parsed = parse_cost_sample(
        observed_at_ms=T0, sessions=answer(({"session_id": "s1"}, "-0")), daily=answer(({}, "-0"))
    )
    assert parsed is not None
    assert math.copysign(1.0, parsed.sessions["s1"]) == 1.0
    assert parsed.daily_usd is not None
    assert math.copysign(1.0, parsed.daily_usd) == 1.0


# The session rule.


def session_fired(at: int, value: float, subject: str = "s1") -> AlertFired:
    return fired("session_cost", subject, at, value, 10.0)


def session_cleared(at: int, subject: str = "s1") -> AlertCleared:
    return cleared("session_cost", subject, at)


def test_fires_at_the_limit_with_the_total_and_not_below() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=9.99), session()) == []
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=10.0), session()) == [
        session_fired(T0 + 5_000, 10.0)
    ]
    assert rule.state.keys == {
        AlertKey(rule="session_cost", subject="s1"): KeyState(
            last_fired_at_ms=T0 + 5_000, firing=True
        )
    }


def test_no_repeat_while_the_total_stays_at_or_above_the_limit() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=11.0), session()) == [session_fired(T0, 11.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=20.0), session()) == []


def test_hysteresis_clears_only_below_ninety_percent_of_the_limit() -> None:
    rule = Sessions(replace_cooldown(0))
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    # Under the limit but at 90 % of it: still the same episode.
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=9.0), session()) == []
    assert rule(T0 + 10_000, sample(T0 + 10_000, s1=10.5), session()) == []
    assert rule(T0 + 15_000, sample(T0 + 15_000, s1=8.99), session()) == [
        session_cleared(T0 + 15_000)
    ]
    # Between 90 % and the limit a new episode has not started.
    assert rule(T0 + 20_000, sample(T0 + 20_000, s1=9.5), session()) == []
    assert rule(T0 + 25_000, sample(T0 + 25_000, s1=10.0), session()) == [
        session_fired(T0 + 25_000, 10.0)
    ]


def replace_cooldown(cooldown_s: int) -> AlertConfig:
    return AlertConfig(
        session_cost=ON.session_cost, daily_cost=ON.daily_cost, cooldown_s=cooldown_s
    )


def test_a_new_episode_fires_again_only_after_the_cooldown() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=1.0), session()) == [session_cleared(T0 + 5_000)]
    almost = T0 + COOLDOWN_MS - 1
    assert rule(almost, sample(almost, s1=12.0), session()) == []
    assert rule(T0 + COOLDOWN_MS, sample(T0 + COOLDOWN_MS, s1=12.0), session()) == [
        session_fired(T0 + COOLDOWN_MS, 12.0)
    ]


def test_a_sample_at_the_staleness_limit_still_counts() -> None:
    rule = Sessions()
    assert rule(T0 + COST_STALE_MS, sample(T0, s1=10.0), session()) == [
        session_fired(T0 + COST_STALE_MS, 10.0)
    ]


def test_a_stale_sample_neither_fires_nor_clears() -> None:
    rule = Sessions()
    late = T0 + COST_STALE_MS + 1
    assert rule(late, sample(T0, s1=10.0), session()) == []
    assert rule.state == AlertState()
    assert rule(late, sample(late, s1=10.0), session()) == [session_fired(late, 10.0)]
    later = late + 5_000
    assert rule(later + COST_STALE_MS + 1, sample(later, s1=0.0), session()) == []
    assert rule(later + COST_STALE_MS + 1, None, session()) == []
    assert rule.state.keys[AlertKey(rule="session_cost", subject="s1")].firing


def test_no_sample_at_all_neither_fires_nor_clears() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    assert rule(T0 + 5_000, None, session()) == []
    assert rule.state.keys[AlertKey(rule="session_cost", subject="s1")].firing


def test_a_sample_from_the_future_counts() -> None:
    # The clock went backwards since the queries ran.
    rule = Sessions()
    assert rule(T0, sample(T0 + 60_000, s1=10.0), session()) == [session_fired(T0, 10.0)]


def test_a_session_that_leaves_the_sample_clears() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000), session()) == [session_cleared(T0 + 5_000)]


@pytest.mark.parametrize("status", ["ended", "stale"])
def test_an_ended_or_stale_session_clears_even_with_a_stale_sample(
    status: SessionStatus,
) -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    late = T0 + COST_STALE_MS + 1
    assert rule(late, sample(T0, s1=10.0), session(status=status)) == [session_cleared(late)]


@pytest.mark.parametrize("status", ["ended", "stale"])
def test_an_ended_or_stale_session_never_fires(status: SessionStatus) -> None:
    assert Sessions()(T0, sample(T0, s1=50.0), session(status=status)) == []


def test_a_session_the_caller_does_not_hold_never_fires_and_clears() -> None:
    rule = Sessions()
    # A session of the sample that the writer no longer holds: an older session still
    # inside the query's range.
    assert rule(T0, sample(T0, s1=10.0, old=99.0), session()) == [session_fired(T0, 10.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=10.0)) == [session_cleared(T0 + 5_000)]


@pytest.mark.parametrize("status", ["working", "waiting_user", "idle"])
def test_every_open_status_can_fire(status: SessionStatus) -> None:
    assert Sessions()(T0, sample(T0, s1=10.0), session(status=status)) == [session_fired(T0, 10.0)]


def test_a_disabled_rule_fires_nothing() -> None:
    rule = Sessions(OFF)
    assert rule(T0, sample(T0, s1=1_000.0), session()) == []
    assert rule.state == AlertState()


def test_disabling_the_rule_clears_what_it_fired_even_with_a_stale_sample() -> None:
    rule = Sessions()
    assert rule(T0, sample(T0, s1=10.0), session()) == [session_fired(T0, 10.0)]
    rule.config = OFF
    late = T0 + COST_STALE_MS + 1
    assert rule(late, None, session()) == [session_cleared(late)]
    assert rule(T0 + COOLDOWN_MS, None, session()) == []
    assert rule.state == AlertState()


def test_a_custom_limit() -> None:
    config = AlertConfig(session_cost=SessionCostRule(enabled=True, limit_usd=0.5))
    rule = Sessions(config)
    assert rule(T0, sample(T0, s1=0.5), session()) == [fired("session_cost", "s1", T0, 0.5, 0.5)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=0.45), session()) == []
    assert rule(T0 + 10_000, sample(T0 + 10_000, s1=0.44), session()) == [
        session_cleared(T0 + 10_000)
    ]


def session_limit(limit_usd: float) -> AlertConfig:
    return AlertConfig(session_cost=SessionCostRule(enabled=True, limit_usd=limit_usd))


def test_raising_the_limit_above_the_total_while_fired_clears() -> None:
    rule = Sessions(session_limit(10.0))
    assert rule(T0, sample(T0, s1=15.0), session()) == [session_fired(T0, 15.0)]
    rule.config = session_limit(20.0)
    # 15 is below 90 % of the new limit (18): the episode ends.
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=15.0), session()) == [session_cleared(T0 + 5_000)]


def test_raising_the_limit_within_the_hysteresis_while_fired_holds() -> None:
    rule = Sessions(session_limit(10.0))
    assert rule(T0, sample(T0, s1=11.0), session()) == [session_fired(T0, 11.0)]
    rule.config = session_limit(12.0)
    # 11 is at or above 90 % of the new limit (10.8): the same episode, no clear.
    assert rule(T0 + 5_000, sample(T0 + 5_000, s1=11.0), session()) == []
    assert rule.state.keys[AlertKey(rule="session_cost", subject="s1")].firing


def test_lowering_the_limit_while_fired_does_not_fire_again() -> None:
    rule = Sessions(session_limit(10.0))
    assert rule(T0, sample(T0, s1=15.0), session()) == [session_fired(T0, 15.0)]
    rule.config = session_limit(5.0)
    assert rule(T0 + COOLDOWN_MS, sample(T0 + COOLDOWN_MS, s1=15.0), session()) == []
    assert rule.state.keys[AlertKey(rule="session_cost", subject="s1")] == KeyState(
        last_fired_at_ms=T0, firing=True
    )


def test_sessions_are_independent_and_effects_sorted_by_subject() -> None:
    rule = Sessions()
    held = (session("c"), session("a"), session("b"))
    assert rule(T0, sample(T0, c=10.0, a=10.0, b=1.0), *held) == [
        session_fired(T0, 10.0, "a"),
        session_fired(T0, 10.0, "c"),
    ]
    assert rule(T0 + 5_000, sample(T0 + 5_000, c=1.0, a=1.0, b=10.0), *held) == [
        session_cleared(T0 + 5_000, "a"),
        session_fired(T0 + 5_000, 10.0, "b"),
        session_cleared(T0 + 5_000, "c"),
    ]


def test_a_key_is_forgotten_once_its_cooldown_is_over() -> None:
    rule = Sessions()
    rule(T0, sample(T0, s1=10.0), session())
    rule(T0 + 5_000, sample(T0 + 5_000), session())
    assert AlertKey(rule="session_cost", subject="s1") in rule.state.keys
    rule(T0 + COOLDOWN_MS, sample(T0 + COOLDOWN_MS), session())
    assert rule.state == AlertState()


# The daily rule.


def day_fired(at: int, value: float) -> AlertFired:
    return fired("daily_cost", DAY_SUBJECT, at, value, 50.0)


def day_cleared(at: int) -> AlertCleared:
    return cleared("daily_cost", DAY_SUBJECT, at)


def test_the_day_fires_at_the_limit_and_not_below() -> None:
    rule = Daily()
    assert rule(T0, sample(T0, daily=49.99)) == []
    assert rule(T0 + 5_000, sample(T0 + 5_000, daily=50.0)) == [day_fired(T0 + 5_000, 50.0)]
    assert rule(T0 + 10_000, sample(T0 + 10_000, daily=80.0)) == []


def test_the_day_has_hysteresis_as_the_rolling_total_falls() -> None:
    rule = Daily()
    assert rule(T0, sample(T0, daily=60.0)) == [day_fired(T0, 60.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, daily=45.0)) == []
    assert rule(T0 + 10_000, sample(T0 + 10_000, daily=44.99)) == [day_cleared(T0 + 10_000)]


def test_the_day_fires_again_only_after_the_cooldown() -> None:
    rule = Daily()
    assert rule(T0, sample(T0, daily=50.0)) == [day_fired(T0, 50.0)]
    assert rule(T0 + 5_000, sample(T0 + 5_000, daily=0.0)) == [day_cleared(T0 + 5_000)]
    assert rule(T0 + 10_000, sample(T0 + 10_000, daily=55.0)) == []
    assert rule(T0 + COOLDOWN_MS, sample(T0 + COOLDOWN_MS, daily=55.0)) == [
        day_fired(T0 + COOLDOWN_MS, 55.0)
    ]


def test_a_stale_day_neither_fires_nor_clears() -> None:
    rule = Daily()
    late = T0 + COST_STALE_MS + 1
    assert rule(late, sample(T0, daily=100.0)) == []
    assert rule(late, sample(late, daily=100.0)) == [day_fired(late, 100.0)]
    assert rule(late + COST_STALE_MS + 1, sample(late, daily=0.0)) == []
    assert rule(late + 5_000, None) == []
    assert rule.state.keys[AlertKey(rule="daily_cost", subject=DAY_SUBJECT)].firing


def test_a_sample_without_the_day_neither_fires_nor_clears() -> None:
    rule = Daily()
    assert rule(T0, sample(T0, s1=1_000.0)) == []
    assert rule(T0 + 5_000, sample(T0 + 5_000, daily=50.0)) == [day_fired(T0 + 5_000, 50.0)]
    assert rule(T0 + 10_000, sample(T0 + 10_000)) == []
    assert rule.state.keys[AlertKey(rule="daily_cost", subject=DAY_SUBJECT)].firing


def test_a_disabled_day_fires_nothing_and_clears_what_it_fired() -> None:
    rule = Daily(OFF)
    assert rule(T0, sample(T0, daily=1_000.0)) == []
    rule.config = ON
    assert rule(T0 + 5_000, sample(T0 + 5_000, daily=1_000.0)) == [day_fired(T0 + 5_000, 1_000.0)]
    rule.config = OFF
    assert rule(T0 + 10_000, None) == [day_cleared(T0 + 10_000)]


def test_changing_the_day_limit_while_fired_follows_the_new_limit() -> None:
    def day_limit(limit_usd: float) -> AlertConfig:
        return AlertConfig(daily_cost=DailyCostRule(enabled=True, limit_usd=limit_usd))

    rule = Daily(day_limit(50.0))
    assert rule(T0, sample(T0, daily=60.0)) == [day_fired(T0, 60.0)]
    # Lowered: the same episode, no new fire.
    rule.config = day_limit(40.0)
    assert rule(T0 + COOLDOWN_MS, sample(T0 + COOLDOWN_MS, daily=60.0)) == []
    # Raised within the hysteresis (90 % of 65 is 58.5): it holds.
    rule.config = day_limit(65.0)
    assert rule(T0 + COOLDOWN_MS + 5_000, sample(T0 + COOLDOWN_MS + 5_000, daily=60.0)) == []
    # Raised past it (90 % of 100 is 90): it clears.
    rule.config = day_limit(100.0)
    late = T0 + COOLDOWN_MS + 10_000
    assert rule(late, sample(late, daily=60.0)) == [day_cleared(late)]


def test_a_day_clear_with_the_clock_behind_is_clamped_to_the_fire() -> None:
    rule = Daily()
    rule(T0, sample(T0, daily=50.0))
    assert rule(T0 - 10_000, sample(T0 - 10_000, daily=0.0)) == [day_cleared(T0)]


# Both rules with the others.


def test_the_cost_rules_keep_each_others_keys_and_the_failure_windows() -> None:
    other = AlertKey(rule="waiting_user", subject="s1")
    state = AlertState(
        keys={other: KeyState(last_fired_at_ms=T0, firing=True)}, failures={"s1": (T0,)}
    )
    cost = sample(T0, daily=50.0, s1=10.0)
    state, effects = evaluate_session_cost(state, cost, (session(),), config=ON, now_ms=T0)
    assert effects == [session_fired(T0, 10.0)]
    state, effects = evaluate_daily_cost(state, cost, config=ON, now_ms=T0)
    assert effects == [day_fired(T0, 50.0)]
    assert set(state.keys) == {
        other,
        AlertKey(rule="session_cost", subject="s1"),
        AlertKey(rule="daily_cost", subject=DAY_SUBJECT),
    }
    assert state.failures == {"s1": (T0,)}
    # And the waiting rule keeps theirs.
    state, _ = evaluate_waiting_user(state, (), config=ON, now_ms=T0 + 1)
    assert AlertKey(rule="daily_cost", subject=DAY_SUBJECT) in state.keys


def test_the_input_state_is_not_changed() -> None:
    state = AlertState()
    evaluate_session_cost(state, sample(T0, s1=10.0), (session(),), config=ON, now_ms=T0)
    evaluate_daily_cost(state, sample(T0, daily=50.0), config=ON, now_ms=T0)
    assert state == AlertState()
