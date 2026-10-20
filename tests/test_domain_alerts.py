"""Unit tests of ``ccobs.domain.alerts``: the alert rules, their defaults and bounds, and
the waiting-on-you evaluator (ADRs 0007 and 0041).

Time is a plain integer in milliseconds (a fake clock): the evaluator never reads the
clock. The last test replays the ``interactive-permission-compact`` hook fixture through
``ccobs.domain.sessions.apply`` and evaluates every 5 s, as the writer does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Final, cast

import pytest

from ccobs.domain.alerts import (
    ALERT_RULES,
    COOLDOWN_BOUNDS,
    COST_BOUNDS,
    DAY_SUBJECT,
    FAILURE_COUNT_BOUNDS,
    FAILURE_WINDOW_BOUNDS,
    WAITING_BOUNDS,
    AlertCleared,
    AlertConfig,
    AlertEffect,
    AlertFired,
    AlertKey,
    AlertState,
    DailyCostRule,
    FieldError,
    KeyState,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
    evaluate_waiting_user,
    validate_config,
    waiting_since,
)
from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import Block, Session, SessionState, SessionStatus, apply
from tools.stack.session_replay import BASE, HOOK_FIXTURES, replay, waited

T0: Final = 1_800_000_000_000
THRESHOLD_MS: Final = 30_000
COOLDOWN_MS: Final = 300_000


def session(
    session_id: str = "s1",
    status: SessionStatus = "waiting_user",
    opened: Iterable[int] = (T0,),
) -> SessionState:
    """A session with one open block per opening time (none unless it is waiting)."""
    blocks = tuple(
        Block(block_id=index + 1, kind="permission", scope=None, opened_at_ms=at)
        for index, at in enumerate(opened if status == "waiting_user" else ())
    )
    return SessionState(
        session=Session(
            session_id=session_id,
            status=status,
            started_at_ms=T0 - 60_000,
            last_event_at_ms=T0,
            last_seq=1,
        ),
        open_blocks=blocks,
    )


def key(subject: str = "s1") -> AlertKey:
    return AlertKey(rule="waiting_user", subject=subject)


class Evaluator:
    """Feeds the evaluator one call at a time and keeps its state."""

    def __init__(self, config: AlertConfig | None = None) -> None:
        self.state = AlertState()
        self.config = AlertConfig() if config is None else config

    def __call__(self, now_ms: int, *sessions: SessionState) -> list[AlertEffect]:
        self.state, effects = evaluate_waiting_user(
            self.state, sessions, config=self.config, now_ms=now_ms
        )
        return effects


def fired(subject: str, at: int, since: int = T0) -> AlertFired:
    return AlertFired(
        rule="waiting_user",
        subject=subject,
        fired_at_ms=at,
        value=(at - since) / 1000,
        threshold=30.0,
    )


def cleared(subject: str, at: int) -> AlertCleared:
    return AlertCleared(rule="waiting_user", subject=subject, cleared_at_ms=at)


# The rules and their defaults.


def test_the_rules() -> None:
    assert ALERT_RULES == ("waiting_user", "repeated_failures", "session_cost", "daily_cost")
    assert DAY_SUBJECT == "day"


def test_the_maintainers_defaults() -> None:
    config = AlertConfig()
    assert config.waiting_user == WaitingUserRule(enabled=True, threshold_s=30)
    assert config.repeated_failures == RepeatedFailuresRule(enabled=True, count=3, window_s=600)
    assert config.session_cost == SessionCostRule(enabled=False, limit_usd=10.0)
    assert config.daily_cost == DailyCostRule(enabled=False, limit_usd=50.0)
    assert config.cooldown_s == 300
    assert validate_config(config) == ()


def test_the_bounds() -> None:
    assert (WAITING_BOUNDS.minimum, WAITING_BOUNDS.maximum) == (5, 86_400)
    assert (FAILURE_COUNT_BOUNDS.minimum, FAILURE_COUNT_BOUNDS.maximum) == (2, 100)
    assert (FAILURE_WINDOW_BOUNDS.minimum, FAILURE_WINDOW_BOUNDS.maximum) == (60, 86_400)
    assert (COST_BOUNDS.minimum, COST_BOUNDS.maximum) == (0.01, 100_000)
    assert (COOLDOWN_BOUNDS.minimum, COOLDOWN_BOUNDS.maximum) == (0, 86_400)


# Validation.

DEFAULT: Final = AlertConfig()
WAITING_ERROR: Final = FieldError(
    field="waiting_user.threshold_s",
    message="waiting_user.threshold_s must be a whole number of seconds from 5 to 86400",
)
COUNT_ERROR: Final = FieldError(
    field="repeated_failures.count",
    message="repeated_failures.count must be a whole number of failures from 2 to 100",
)
WINDOW_ERROR: Final = FieldError(
    field="repeated_failures.window_s",
    message="repeated_failures.window_s must be a whole number of seconds from 60 to 86400",
)
SESSION_COST_ERROR: Final = FieldError(
    field="session_cost.limit_usd",
    message="session_cost.limit_usd must be an amount in US dollars from 0.01 to 100000",
)
DAILY_COST_ERROR: Final = FieldError(
    field="daily_cost.limit_usd",
    message="daily_cost.limit_usd must be an amount in US dollars from 0.01 to 100000",
)
COOLDOWN_ERROR: Final = FieldError(
    field="cooldown_s",
    message="cooldown_s must be a whole number of seconds from 0 to 86400",
)


# The helpers take values of any type: a settings form may send anything.


# The helpers take any object on purpose: validation must turn a value of the wrong
# type (as a settings form could send) into a FieldError, so the casts only silence the
# dataclasses' annotations for those cases.
def waiting(threshold_s: object) -> AlertConfig:
    return replace(DEFAULT, waiting_user=WaitingUserRule(threshold_s=cast(int, threshold_s)))


def failures(count: object = 3, window_s: object = 600) -> AlertConfig:
    rule = RepeatedFailuresRule(count=cast(int, count), window_s=cast(int, window_s))
    return replace(DEFAULT, repeated_failures=rule)


def costs(session_usd: object = 10.0, daily_usd: object = 50.0) -> AlertConfig:
    return replace(
        DEFAULT,
        session_cost=SessionCostRule(limit_usd=cast(float, session_usd)),
        daily_cost=DailyCostRule(limit_usd=cast(float, daily_usd)),
    )


def cooldown(cooldown_s: object) -> AlertConfig:
    return replace(DEFAULT, cooldown_s=cast(int, cooldown_s))


@pytest.mark.parametrize(
    "config",
    [
        waiting(5),
        waiting(86_400),
        failures(count=2),
        failures(count=100),
        failures(window_s=60),
        failures(window_s=86_400),
        costs(session_usd=0.01, daily_usd=0.01),
        costs(session_usd=100_000, daily_usd=100_000.0),
        cooldown(0),
        cooldown(86_400),
    ],
)
def test_values_at_the_bounds_are_valid(config: AlertConfig) -> None:
    assert validate_config(config) == ()


@pytest.mark.parametrize(
    ("config", "errors"),
    [
        (waiting(4), (WAITING_ERROR,)),
        (waiting(86_401), (WAITING_ERROR,)),
        (waiting(True), (WAITING_ERROR,)),
        (waiting(30.5), (WAITING_ERROR,)),
        (waiting(30.0), (WAITING_ERROR,)),
        (waiting("30"), (WAITING_ERROR,)),
        (waiting(None), (WAITING_ERROR,)),
        (failures(count=1), (COUNT_ERROR,)),
        (failures(count=101), (COUNT_ERROR,)),
        (failures(count=2.5), (COUNT_ERROR,)),
        (failures(window_s=59), (WINDOW_ERROR,)),
        (failures(window_s=86_401), (WINDOW_ERROR,)),
        (failures(window_s="600"), (WINDOW_ERROR,)),
        (costs(session_usd=0.009), (SESSION_COST_ERROR,)),
        (costs(daily_usd=100_000.01), (DAILY_COST_ERROR,)),
        (costs(session_usd=float("nan")), (SESSION_COST_ERROR,)),
        (costs(daily_usd=float("inf")), (DAILY_COST_ERROR,)),
        (costs(session_usd=True), (SESSION_COST_ERROR,)),
        (costs(session_usd="10"), (SESSION_COST_ERROR,)),
        (costs(daily_usd=None), (DAILY_COST_ERROR,)),
        (cooldown(-1), (COOLDOWN_ERROR,)),
        (cooldown(86_401), (COOLDOWN_ERROR,)),
        (cooldown(False), (COOLDOWN_ERROR,)),
        (cooldown(0.5), (COOLDOWN_ERROR,)),
        (cooldown(None), (COOLDOWN_ERROR,)),
    ],
)
def test_values_out_of_bounds_are_errors(
    config: AlertConfig, errors: tuple[FieldError, ...]
) -> None:
    assert validate_config(config) == errors


def test_every_error_is_reported_in_field_order() -> None:
    config = AlertConfig(
        waiting_user=WaitingUserRule(threshold_s=0),
        repeated_failures=RepeatedFailuresRule(count=0, window_s=0),
        session_cost=SessionCostRule(limit_usd=0),
        daily_cost=DailyCostRule(limit_usd=0),
        cooldown_s=-5,
    )
    assert validate_config(config) == (
        WAITING_ERROR,
        COUNT_ERROR,
        WINDOW_ERROR,
        SESSION_COST_ERROR,
        DAILY_COST_ERROR,
        COOLDOWN_ERROR,
    )


def test_a_disabled_rule_is_still_validated() -> None:
    config = replace(DEFAULT, session_cost=SessionCostRule(enabled=False, limit_usd=-3))
    assert validate_config(config) == (SESSION_COST_ERROR,)


def test_errors_never_echo_the_value() -> None:
    config = AlertConfig(
        waiting_user=WaitingUserRule(threshold_s=987_654_321),
        session_cost=SessionCostRule(limit_usd=123_456_789.5),
    )
    for error in validate_config(config):
        assert "987654321" not in error.message
        assert "123456789" not in error.message


# The episode's start.


def test_waiting_since_is_the_oldest_open_block() -> None:
    assert waiting_since(session(opened=(T0 + 5, T0, T0 + 9))) == T0


@pytest.mark.parametrize("status", ["working", "idle", "ended", "stale"])
def test_a_session_that_is_not_waiting_has_no_episode(status: SessionStatus) -> None:
    assert waiting_since(session(status=status)) is None


def test_a_waiting_status_with_no_open_block_has_no_episode() -> None:
    assert waiting_since(session(opened=())) is None


# Firing.


def test_fires_exactly_at_the_threshold_and_not_before() -> None:
    evaluate = Evaluator()
    assert evaluate(T0 + THRESHOLD_MS - 1, session()) == []
    assert evaluate(T0 + THRESHOLD_MS, session()) == [fired("s1", T0 + THRESHOLD_MS)]
    assert evaluate.state.keys == {key(): KeyState(last_fired_at_ms=T0 + 30_000, firing=True)}


def test_the_value_is_the_wait_in_seconds() -> None:
    (alert,) = Evaluator()(T0 + 42_500, session())
    assert isinstance(alert, AlertFired)
    assert (alert.value, alert.threshold) == (42.5, 30.0)


def test_no_repeat_within_the_same_episode() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    for later in (5_000, 10_000, COOLDOWN_MS, 10 * COOLDOWN_MS):
        assert evaluate(T0 + THRESHOLD_MS + later, session()) == []


def test_a_second_block_in_the_same_episode_does_not_fire_again() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    later = T0 + COOLDOWN_MS + THRESHOLD_MS
    assert evaluate(later, session(opened=(T0 + COOLDOWN_MS,))) == []


def test_clears_when_the_session_leaves_waiting_user() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    assert evaluate(T0 + 40_000, session(status="working")) == [cleared("s1", T0 + 40_000)]
    assert evaluate.state.keys == {key(): KeyState(last_fired_at_ms=T0 + 30_000, firing=False)}
    assert evaluate(T0 + 45_000, session(status="idle")) == []


def test_an_episode_that_never_fired_clears_nothing() -> None:
    evaluate = Evaluator()
    evaluate(T0 + 10_000, session())
    assert evaluate(T0 + 20_000, session(status="idle")) == []
    assert evaluate.state == AlertState()


@pytest.mark.parametrize("status", ["ended", "stale"])
def test_an_ended_or_stale_session_clears(status: SessionStatus) -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    assert evaluate(T0 + 31_000, session(status=status)) == [cleared("s1", T0 + 31_000)]


def test_a_session_that_disappears_clears() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    assert evaluate(T0 + 35_000) == [cleared("s1", T0 + 35_000)]


# Re-arming and the cooldown.


def test_a_new_episode_fires_again_only_after_the_cooldown() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    evaluate(T0 + 40_000, session(status="idle"))
    # A new episode inside the cooldown: past the threshold, but no alert.
    second = T0 + 100_000
    assert evaluate(second + THRESHOLD_MS, session(opened=(second,))) == []
    assert evaluate(T0 + THRESHOLD_MS + COOLDOWN_MS - 1, session(opened=(second,))) == []
    # The cooldown ends during the episode: it fires then, with the whole wait.
    at = T0 + THRESHOLD_MS + COOLDOWN_MS
    assert evaluate(at, session(opened=(second,))) == [fired("s1", at, since=second)]


def test_a_new_episode_after_the_cooldown_fires_at_its_threshold() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    evaluate(T0 + 40_000, session(status="idle"))
    third = T0 + 2 * COOLDOWN_MS
    assert evaluate(third + THRESHOLD_MS - 1, session(opened=(third,))) == []
    assert evaluate(third + THRESHOLD_MS, session(opened=(third,))) == [
        fired("s1", third + THRESHOLD_MS, since=third)
    ]


def test_cooldown_zero_fires_every_episode() -> None:
    evaluate = Evaluator(cooldown(0))
    for episode in range(3):
        start = T0 + episode * 100_000
        assert evaluate(start + THRESHOLD_MS, session(opened=(start,))) == [
            fired("s1", start + THRESHOLD_MS, since=start)
        ]
        assert evaluate(start + 50_000, session(status="idle")) == [cleared("s1", start + 50_000)]
    # Nothing to remember: with no cooldown, a cleared key leaves the state.
    assert evaluate.state == AlertState()


def test_cooldown_is_per_key() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session("a"))
    assert evaluate(T0 + 31_000, session("a"), session("b", opened=(T0 + 1_000,))) == [
        fired("b", T0 + 31_000, since=T0 + 1_000)
    ]


def test_a_key_is_forgotten_once_its_cooldown_is_over() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    evaluate(T0 + 40_000)  # the session left memory: cleared, cooldown still running
    assert evaluate.state.keys == {key(): KeyState(last_fired_at_ms=T0 + 30_000, firing=False)}
    evaluate(T0 + THRESHOLD_MS + COOLDOWN_MS - 1)
    assert key() in evaluate.state.keys
    evaluate(T0 + THRESHOLD_MS + COOLDOWN_MS)
    assert evaluate.state == AlertState()


# A clock that goes backwards.


def test_a_clock_that_goes_backwards_never_fires_twice() -> None:
    evaluate = Evaluator(cooldown(0))
    evaluate(T0 + THRESHOLD_MS, session())
    assert evaluate(T0 + 10_000, session()) == []
    assert evaluate(T0 - 50_000, session()) == []
    assert evaluate(T0 + THRESHOLD_MS, session()) == []


def test_a_clear_with_the_clock_behind_is_clamped_to_the_fire() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session())
    assert evaluate(T0, session(status="idle")) == [cleared("s1", T0 + THRESHOLD_MS)]


def test_a_new_episode_with_the_clock_behind_the_last_fire_waits_for_the_cooldown() -> None:
    evaluate = Evaluator()
    evaluate(T0 + 100_000, session())
    evaluate(T0 + 100_001, session(status="idle"))
    earlier = T0 - 100_000
    # Past its own threshold, but before the last fire: the cooldown is not over.
    assert evaluate(earlier + THRESHOLD_MS, session(opened=(earlier,))) == []
    assert evaluate(T0 + 100_000 + COOLDOWN_MS - 1, session(opened=(earlier,))) == []
    at = T0 + 100_000 + COOLDOWN_MS
    assert evaluate(at, session(opened=(earlier,))) == [fired("s1", at, since=earlier)]


def test_a_block_opened_in_the_future_does_not_fire() -> None:
    assert Evaluator()(T0, session(opened=(T0 + 60_000,))) == []


# Disabled.


def test_a_disabled_rule_fires_nothing() -> None:
    evaluate = Evaluator(replace(DEFAULT, waiting_user=WaitingUserRule(enabled=False)))
    assert evaluate(T0 + 10 * THRESHOLD_MS, session()) == []
    assert evaluate.state == AlertState()


def test_disabling_the_rule_clears_what_it_fired() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session("a"))
    evaluate(T0 + THRESHOLD_MS + 1_000, session("a"), session("b"))
    evaluate.config = replace(DEFAULT, waiting_user=WaitingUserRule(enabled=False))
    assert evaluate(T0 + 40_000, session("a"), session("b")) == [
        cleared("a", T0 + 40_000),
        cleared("b", T0 + 40_000),
    ]
    assert evaluate(T0 + 45_000, session("a"), session("b")) == []
    # Re-enabled inside the cooldown: the episodes still running do not fire again.
    evaluate.config = DEFAULT
    assert evaluate(T0 + 50_000, session("a"), session("b")) == []


def test_a_custom_threshold() -> None:
    evaluate = Evaluator(waiting(5))
    assert evaluate(T0 + 4_999, session()) == []
    (alert,) = evaluate(T0 + 5_000, session())
    assert isinstance(alert, AlertFired)
    assert (alert.value, alert.threshold) == (5.0, 5.0)


# The state of other rules and determinism.


def test_keys_of_other_rules_pass_through_untouched() -> None:
    other = {
        AlertKey(rule="daily_cost", subject=DAY_SUBJECT): KeyState(
            last_fired_at_ms=T0 - 10 * COOLDOWN_MS, firing=True
        ),
        AlertKey(rule="repeated_failures", subject="s1"): KeyState(
            last_fired_at_ms=T0 - 10 * COOLDOWN_MS, firing=False
        ),
    }
    state, effects = evaluate_waiting_user(
        AlertState(keys=other), [session()], config=DEFAULT, now_ms=T0 + THRESHOLD_MS
    )
    assert effects == [fired("s1", T0 + THRESHOLD_MS)]
    assert state.keys == {
        **other,
        key(): KeyState(last_fired_at_ms=T0 + THRESHOLD_MS, firing=True),
    }


def test_the_input_state_is_not_changed() -> None:
    before = AlertState()
    evaluate_waiting_user(before, [session()], config=DEFAULT, now_ms=T0 + THRESHOLD_MS)
    assert before == AlertState()


def test_effects_are_sorted_by_subject() -> None:
    evaluate = Evaluator()
    evaluate(T0 + THRESHOLD_MS, session("m"), session("y"))
    effects = evaluate(T0 + THRESHOLD_MS + 5_000, session("z"), session("y"), session("a"))
    assert effects == [
        fired("a", T0 + 35_000),
        cleared("m", T0 + 35_000),
        fired("z", T0 + 35_000),
    ]


def test_alert_keys_order_by_rule_then_subject() -> None:
    keys = [AlertKey(rule="waiting_user", subject="a"), AlertKey(rule="daily_cost", subject="b")]
    assert sorted(keys) == list(reversed(keys))


# A real session: the fixture's dialog denied with Esc waits 102.8 s.


def test_replay_interactive_permission_compact_fires_once_at_30_s() -> None:
    name = "interactive-permission-compact"
    _, denied = replay(name).closed()
    assert waited(denied) == 102_840

    lines = (HOOK_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    timeline = [(BASE + json.loads(line)["offset_ms"], json.loads(line)) for line in lines]
    end = timeline[-1][0] + 10_000
    state: SessionState | None = None
    alerts = AlertState()
    effects: list[AlertEffect] = []
    applied = 0
    # The writer evaluates every 5 s over the sessions it holds; an ended session leaves.
    for now in range(BASE, end, 5_000):
        while applied < len(timeline) and timeline[applied][0] <= now:
            at, document = timeline[applied]
            applied += 1
            state, _ = apply(state, parse_hook(document["payload"]), at, applied)
        assert state is not None
        held = [state] if state.session.status != "ended" else []
        alerts, new = evaluate_waiting_user(alerts, held, config=DEFAULT, now_ms=now)
        effects.extend(new)

    alert, clear = effects
    assert isinstance(alert, AlertFired)
    assert isinstance(clear, AlertCleared)
    # The first evaluation 30 s or more after the dialog opened, and the first one after
    # /compact superseded it.
    assert denied.opened_at_ms + THRESHOLD_MS <= alert.fired_at_ms < denied.opened_at_ms + 35_000
    assert (alert.fired_at_ms, alert.value) == (BASE + 70_000, 33.974)
    assert denied.closed_at_ms is not None
    assert denied.closed_at_ms <= clear.cleared_at_ms < denied.closed_at_ms + 5_000
    assert state is not None
    assert alert.subject == clear.subject == state.session.session_id
