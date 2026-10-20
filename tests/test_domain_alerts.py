"""Unit tests of ``ccobs.domain.alerts``: the alert rules, their defaults and bounds, and
the waiting-on-you and repeated-failures evaluators (ADRs 0007 and 0041).

Time is a plain integer in milliseconds (a fake clock): the evaluators never read the
clock. Replays feed hook fixtures (``interactive-permission-compact`` through
``ccobs.domain.sessions.apply``, ``headless-api-error`` through
``record_failure_event``) and evaluate every 5 s, as the writer does.
"""

from __future__ import annotations

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
    evaluate_repeated_failures,
    evaluate_waiting_user,
    forget_sessions,
    record_failure_event,
    validate_config,
    waiting_since,
)
from ccobs.domain.hooks import SessionEnd, StopFailure
from ccobs.domain.sessions import Block, Session, SessionState, SessionStatus, apply
from tools.stack.session_replay import BASE, SESSION, event, replay, timeline, waited

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

    events = timeline(name)
    end = events[-1][0] + 10_000
    state: SessionState | None = None
    alerts = AlertState()
    effects: list[AlertEffect] = []
    applied = 0
    # The writer evaluates every 5 s over the sessions it holds; an ended session leaves.
    for now in range(BASE, end, 5_000):
        while applied < len(events) and events[applied][0] <= now:
            at, hook = events[applied]
            applied += 1
            state, _ = apply(state, hook, at, applied)
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


# Repeated API failures: StopFailure events of one session within a sliding window.

WINDOW_MS: Final = 600_000
MINUTE: Final = 60_000


class Failures:
    """Records hook events and evaluates ``repeated_failures``, keeping the state."""

    def __init__(self, config: AlertConfig | None = None) -> None:
        self.state = AlertState()
        self.config = AlertConfig() if config is None else config

    def record(self, at: int, name: str = "StopFailure", **fields: object) -> None:
        if name == "StopFailure":
            fields.setdefault("error", "model_not_found")
        hook = event(name, **fields)
        self.state = record_failure_event(self.state, hook, at, config=self.config)

    def fail(self, *times: int, session_id: str = SESSION) -> None:
        for at in times:
            self.record(at, session_id=session_id)

    def __call__(self, now_ms: int) -> list[AlertEffect]:
        self.state, effects = evaluate_repeated_failures(
            self.state, config=self.config, now_ms=now_ms
        )
        return effects

    def window(self, session_id: str = SESSION) -> tuple[int, ...]:
        return self.state.failures.get(session_id, ())


def failure_fired(at: int, count: int = 3, subject: str = SESSION) -> AlertFired:
    return AlertFired(
        rule="repeated_failures",
        subject=subject,
        fired_at_ms=at,
        value=float(count),
        threshold=3.0,
    )


def failure_cleared(at: int, subject: str = SESSION) -> AlertCleared:
    return AlertCleared(rule="repeated_failures", subject=subject, cleared_at_ms=at)


# Retention deleted some sessions: the writer forgets them.


def test_forget_sessions_drops_their_windows_and_keys_only() -> None:
    firing = KeyState(last_fired_at_ms=T0, firing=True)
    state = AlertState(
        keys={
            AlertKey(rule="repeated_failures", subject="gone"): firing,
            AlertKey(rule="waiting_user", subject="gone"): firing,
            AlertKey(rule="session_cost", subject="kept"): firing,
            AlertKey(rule="daily_cost", subject=DAY_SUBJECT): firing,
        },
        failures={"gone": (T0, T0 + 1), "kept": (T0,)},
    )

    # Even a session_id equal to the day's subject never drops daily_cost's key.
    forgotten = forget_sessions(state, frozenset({"gone", DAY_SUBJECT}))

    assert forgotten == AlertState(
        keys={
            AlertKey(rule="session_cost", subject="kept"): firing,
            AlertKey(rule="daily_cost", subject=DAY_SUBJECT): firing,
        },
        failures={"kept": (T0,)},
    )
    assert state.failures.keys() == {"gone", "kept"}  # the given state is unchanged


def test_forget_sessions_it_does_not_hold_returns_the_state_itself() -> None:
    state = AlertState(failures={"kept": (T0,)})
    assert forget_sessions(state, frozenset({"gone"})) is state
    assert forget_sessions(state, frozenset()) is state


def failure_key(subject: str = SESSION) -> AlertKey:
    return AlertKey(rule="repeated_failures", subject=subject)


def test_three_failures_within_the_window_fire_once() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 5 * MINUTE)
    assert evaluate(T0 + 5 * MINUTE) == []
    # The first failure is exactly one window old: it still counts.
    evaluate.fail(T0 + WINDOW_MS)
    assert evaluate(T0 + WINDOW_MS) == [failure_fired(T0 + WINDOW_MS)]
    assert evaluate.state.keys == {
        failure_key(): KeyState(last_fired_at_ms=T0 + WINDOW_MS, firing=True)
    }
    # More failures and evaluations in the same episode: no repeat.
    evaluate.fail(T0 + WINDOW_MS + 1_000)
    assert evaluate(T0 + WINDOW_MS + 5_000) == []
    assert evaluate(T0 + WINDOW_MS + 10_000) == []


def test_three_failures_spread_over_eleven_minutes_do_not_fire() -> None:
    evaluate = Failures()
    for at in (T0, T0 + 5 * MINUTE + 30_000, T0 + 11 * MINUTE):
        evaluate.fail(at)
        assert evaluate(at) == []
        assert evaluate(at + 5_000) == []


def test_two_failures_are_not_enough() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000)
    assert evaluate(T0 + 1_000) == []
    assert evaluate.state.keys == {}


def test_a_custom_count_and_window() -> None:
    config = replace(DEFAULT, repeated_failures=RepeatedFailuresRule(count=2, window_s=60))
    evaluate = Failures(config)
    evaluate.fail(T0, T0 + 60_000)
    (alert,) = evaluate(T0 + 60_000)
    assert isinstance(alert, AlertFired)
    assert (alert.value, alert.threshold) == (2.0, 2.0)


def test_every_error_code_counts_and_only_times_are_kept() -> None:
    evaluate = Failures()
    evaluate.record(T0, error="rate_limit")
    evaluate.record(T0 + 1_000, error="model_not_found")
    evaluate.record(T0 + 2_000, error="Some free text")  # not a code: error is None
    assert evaluate.window() == (T0, T0 + 1_000, T0 + 2_000)
    assert evaluate(T0 + 2_000) == [failure_fired(T0 + 2_000)]


def test_a_stop_between_failures_resets_the_window() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + MINUTE)
    evaluate.record(T0 + 2 * MINUTE, "Stop")
    assert evaluate.window() == ()
    assert evaluate.state.failures == {}
    evaluate.fail(T0 + 3 * MINUTE)
    assert evaluate(T0 + 3 * MINUTE) == []
    assert evaluate.window() == (T0 + 3 * MINUTE,)


def test_a_stop_clears_a_firing_alert() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + MINUTE, T0 + 2 * MINUTE)
    evaluate(T0 + 2 * MINUTE)
    evaluate.record(T0 + 3 * MINUTE, "Stop")
    assert evaluate(T0 + 3 * MINUTE + 1_000) == [failure_cleared(T0 + 3 * MINUTE + 1_000)]
    assert evaluate.state.keys == {
        failure_key(): KeyState(last_fired_at_ms=T0 + 2 * MINUTE, firing=False)
    }


def test_failures_age_out_and_clear() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + MINUTE, T0 + 2 * MINUTE)
    assert evaluate(T0 + 2 * MINUTE) == [failure_fired(T0 + 2 * MINUTE)]
    assert evaluate(T0 + WINDOW_MS) == []
    # One millisecond later the first failure is out of the window: 2 < 3.
    assert evaluate(T0 + WINDOW_MS + 1) == [failure_cleared(T0 + WINDOW_MS + 1)]
    assert evaluate.window() == (T0 + MINUTE, T0 + 2 * MINUTE)
    # Every failure aged out: the session leaves the windows.
    assert evaluate(T0 + 2 * MINUTE + WINDOW_MS + 1) == []
    assert evaluate.state.failures == {}


def test_a_new_streak_fires_again_only_after_the_cooldown() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + MINUTE, T0 + 2 * MINUTE)
    evaluate(T0 + 2 * MINUTE)
    evaluate.record(T0 + 3 * MINUTE, "Stop")
    evaluate(T0 + 3 * MINUTE)
    # A new streak inside the cooldown: three failures, no alert.
    evaluate.fail(T0 + 4 * MINUTE, T0 + 5 * MINUTE, T0 + 6 * MINUTE)
    assert evaluate(T0 + 6 * MINUTE) == []
    assert evaluate(T0 + 2 * MINUTE + COOLDOWN_MS - 1) == []
    # The cooldown ends while the streak still holds: it fires then.
    at = T0 + 2 * MINUTE + COOLDOWN_MS
    assert evaluate(at) == [failure_fired(at)]


def test_cooldown_zero_fires_every_streak() -> None:
    evaluate = Failures(cooldown(0))
    for streak in range(3):
        start = T0 + streak * 10 * MINUTE
        evaluate.fail(start, start + 1_000, start + 2_000)
        assert evaluate(start + 2_000) == [failure_fired(start + 2_000)]
        evaluate.record(start + 3_000, "Stop")
        assert evaluate(start + 4_000) == [failure_cleared(start + 4_000)]
    assert evaluate.state == AlertState()


def test_the_cooldown_of_another_rule_does_not_apply() -> None:
    evaluate = Failures()
    evaluate.state = AlertState(keys={key(SESSION): KeyState(last_fired_at_ms=T0, firing=True)})
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    assert evaluate(T0 + 2_000) == [failure_fired(T0 + 2_000)]


def test_a_disabled_rule_records_and_fires_nothing() -> None:
    off = replace(DEFAULT, repeated_failures=RepeatedFailuresRule(enabled=False))
    evaluate = Failures(off)
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    assert evaluate.state == AlertState()
    assert evaluate(T0 + 2_000) == []
    assert evaluate.state == AlertState()


def test_disabling_the_rule_clears_what_it_fired_and_forgets_the_windows() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000, session_id="a")
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000, session_id="b")
    evaluate(T0 + 2_000)
    evaluate.fail(T0 + 3_000, session_id="c")
    evaluate.config = replace(DEFAULT, repeated_failures=RepeatedFailuresRule(enabled=False))
    assert evaluate(T0 + 5_000) == [
        failure_cleared(T0 + 5_000, "a"),
        failure_cleared(T0 + 5_000, "b"),
    ]
    assert evaluate.state.failures == {}
    assert evaluate(T0 + 10_000) == []


def test_session_end_drops_the_window_and_clears_the_alert() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    evaluate(T0 + 2_000)
    evaluate.record(T0 + 3_000, "SessionEnd", reason="other")
    assert evaluate.state.failures == {}
    assert evaluate(T0 + 5_000) == [failure_cleared(T0 + 5_000)]


def test_a_session_end_with_no_window_changes_nothing() -> None:
    evaluate = Failures()
    evaluate.fail(T0, session_id="other")
    before = evaluate.state
    evaluate.record(T0 + 1_000, "SessionEnd")
    evaluate.record(T0 + 1_000, "Stop")
    assert evaluate.state == before


def test_out_of_order_failures_are_inserted_in_order() -> None:
    evaluate = Failures()
    evaluate.fail(T0 + 2 * MINUTE, T0, T0 + MINUTE)
    assert evaluate.window() == (T0, T0 + MINUTE, T0 + 2 * MINUTE)
    assert evaluate(T0 + 2 * MINUTE) == [failure_fired(T0 + 2 * MINUTE)]


def test_a_late_failure_older_than_a_full_window_is_dropped() -> None:
    evaluate = Failures()
    evaluate.fail(T0 + MINUTE, T0 + 2 * MINUTE, T0 + 3 * MINUTE)
    evaluate.fail(T0)
    assert evaluate.window() == (T0 + MINUTE, T0 + 2 * MINUTE, T0 + 3 * MINUTE)


def test_a_stop_drops_every_failure_recorded_before_it_even_if_timed_earlier() -> None:
    evaluate = Failures()
    evaluate.fail(T0 + MINUTE, T0 + 2 * MINUTE, T0 + 3 * MINUTE)
    assert evaluate(T0 + 3 * MINUTE) == [failure_fired(T0 + 3 * MINUTE)]
    # The clock stepped back: the next turn's successful Stop is timed before the
    # failures, but it was processed after them (ADR 0024), so it drops them all.
    evaluate.record(T0, "Stop")
    assert evaluate.window() == ()
    assert evaluate(T0 + 1_000) == [failure_cleared(T0 + 3 * MINUTE)]
    # A failure processed after the Stop counts again.
    evaluate.fail(T0 + 2_000)
    assert evaluate.window() == (T0 + 2_000,)


def test_the_window_keeps_at_most_count_failures() -> None:
    evaluate = Failures()
    evaluate.fail(*(T0 + i for i in range(3 + 50)))
    assert evaluate.window() == (T0 + 50, T0 + 51, T0 + 52)
    assert evaluate(T0 + 52) == [failure_fired(T0 + 52)]


def test_lowering_the_count_fires_on_the_window_and_trims_it_at_the_next_failure() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    evaluate.config = failures(count=2)
    fired = AlertFired(
        rule="repeated_failures", subject=SESSION, fired_at_ms=T0 + 2_000, value=3.0, threshold=2.0
    )
    assert evaluate(T0 + 2_000) == [fired]
    assert evaluate.window() == (T0, T0 + 1_000, T0 + 2_000)
    # The change applies to the next failure recorded: the window keeps the newest 2.
    evaluate.fail(T0 + 3_000)
    assert evaluate.window() == (T0 + 2_000, T0 + 3_000)
    assert evaluate(T0 + 3_000) == []


def test_raising_the_count_grows_the_window_and_fires_on_the_new_count() -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    evaluate.config = failures(count=5)
    assert evaluate(T0 + 2_000) == []
    evaluate.fail(T0 + 3_000)
    assert evaluate.window() == (T0, T0 + 1_000, T0 + 2_000, T0 + 3_000)
    assert evaluate(T0 + 3_000) == []
    evaluate.fail(T0 + 4_000)
    assert evaluate.window() == (T0, T0 + 1_000, T0 + 2_000, T0 + 3_000, T0 + 4_000)
    fired = AlertFired(
        rule="repeated_failures", subject=SESSION, fired_at_ms=T0 + 4_000, value=5.0, threshold=5.0
    )
    assert evaluate(T0 + 4_000) == [fired]
    # The sixth failure keeps the window at five.
    evaluate.fail(T0 + 5_000)
    assert evaluate.window() == (T0 + 1_000, T0 + 2_000, T0 + 3_000, T0 + 4_000, T0 + 5_000)


def test_a_subagent_failure_counts() -> None:
    evaluate = Failures()
    evaluate.fail(T0)
    evaluate.record(T0 + 1_000, agent_id="a1", agent_type="Explore")
    evaluate.fail(T0 + 2_000)
    assert evaluate(T0 + 2_000) == [failure_fired(T0 + 2_000)]


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("SubagentStop", {"agent_id": "a1", "agent_type": "Explore"}),
        ("Stop", {"agent_id": "a1", "agent_type": "Explore"}),
        ("UserPromptSubmit", {"prompt": "again"}),
        ("PostToolUse", {"tool_name": "Read", "tool_use_id": "t1"}),
        ("Notification", {"notification_type": "idle_prompt"}),
    ],
)
def test_only_a_main_agent_stop_resets(name: str, fields: dict[str, object]) -> None:
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000)
    before = evaluate.state
    evaluate.record(T0 + 1_500, name, **fields)
    assert evaluate.state is before
    evaluate.fail(T0 + 2_000)
    assert evaluate(T0 + 2_000) == [failure_fired(T0 + 2_000)]


def test_sessions_are_independent_and_effects_sorted_by_subject() -> None:
    evaluate = Failures()
    for subject in ("z", "m", "a"):
        evaluate.fail(T0, T0 + 1_000, session_id=subject)
    evaluate.fail(T0 + 2_000, session_id="z")
    evaluate.fail(T0 + 2_000, session_id="a")
    assert evaluate(T0 + 2_000) == [
        failure_fired(T0 + 2_000, subject="a"),
        failure_fired(T0 + 2_000, subject="z"),
    ]
    evaluate.record(T0 + 3_000, "Stop", session_id="z")
    evaluate.fail(T0 + 3_000, session_id="m")
    assert evaluate(T0 + 4_000) == [
        failure_fired(T0 + 4_000, subject="m"),
        failure_cleared(T0 + 4_000, subject="z"),
    ]


def test_a_failure_after_now_counts_and_a_clear_is_never_before_its_fire() -> None:
    # The clock went backwards: failures received "later" than now still happened.
    evaluate = Failures()
    evaluate.fail(T0, T0 + 1_000, T0 + 2_000)
    assert evaluate(T0 - MINUTE) == [failure_fired(T0 - MINUTE)]
    assert evaluate(T0 - 2 * MINUTE) == []
    evaluate.record(T0 + 3_000, "Stop")
    assert evaluate(T0 - 3 * MINUTE) == [failure_cleared(T0 - MINUTE)]


def test_the_input_state_is_not_changed_by_failures() -> None:
    before = AlertState()
    hook = event("StopFailure", error="model_not_found")
    after = record_failure_event(before, hook, T0, config=DEFAULT)
    assert before == AlertState()
    full = AlertState(failures={SESSION: (T0, T0 + 1, T0 + 2)})
    evaluate_repeated_failures(full, config=DEFAULT, now_ms=T0 + 2)
    assert full == AlertState(failures={SESSION: (T0, T0 + 1, T0 + 2)})
    assert after.failures == {SESSION: (T0,)}


def test_the_two_evaluators_keep_each_others_state() -> None:
    state = AlertState()
    for at in (T0, T0 + 1_000, T0 + 2_000):
        hook = event("StopFailure", error="overloaded")
        state = record_failure_event(state, hook, at, config=DEFAULT)
    now = T0 + THRESHOLD_MS
    state, waits = evaluate_waiting_user(state, [session(SESSION)], config=DEFAULT, now_ms=now)
    state, fails = evaluate_repeated_failures(state, config=DEFAULT, now_ms=now)
    assert waits == [fired(SESSION, now)]
    assert fails == [failure_fired(now)]
    assert state.keys == {
        key(SESSION): KeyState(last_fired_at_ms=now, firing=True),
        failure_key(): KeyState(last_fired_at_ms=now, firing=True),
    }
    # waiting_user leaves the failure windows alone, and the other way round.
    state, _ = evaluate_waiting_user(state, [], config=DEFAULT, now_ms=now + 5_000)
    assert state.failures == {SESSION: (T0, T0 + 1_000, T0 + 2_000)}
    state, effects = evaluate_repeated_failures(state, config=DEFAULT, now_ms=now + 5_000)
    assert effects == []
    assert state.keys[key(SESSION)] == KeyState(last_fired_at_ms=now, firing=False)


# A real session: claude -p with an unknown model fails once (model_not_found).


def test_replay_headless_api_error_does_not_fire_with_the_defaults() -> None:
    events = timeline("headless-api-error")
    failures = [hook for _, hook in events if isinstance(hook, StopFailure)]
    assert [hook.error for hook in failures] == ["model_not_found"]
    assert isinstance(events[-1][1], SessionEnd)

    state = AlertState()
    effects: list[AlertEffect] = []
    windows: list[tuple[int, ...]] = []
    applied = 0
    for now in range(BASE, events[-1][0] + 10_000, 5_000):
        while applied < len(events) and events[applied][0] <= now:
            at, hook = events[applied]
            applied += 1
            state = record_failure_event(state, hook, at, config=DEFAULT)
            windows.append(state.failures.get(hook.session_id, ()))
        state, new = evaluate_repeated_failures(state, config=DEFAULT, now_ms=now)
        effects.extend(new)

    (failed_at,) = [at for at, hook in events if isinstance(hook, StopFailure)]
    assert (failed_at,) in windows
    assert effects == []
    assert state == AlertState()
