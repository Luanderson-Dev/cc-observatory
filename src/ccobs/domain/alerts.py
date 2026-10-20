"""Alert rules, their defaults and bounds, and their evaluators (ADR 0041).

ADR 0007 names four rules; this module holds their configuration with the maintainer's
defaults, the bounds a value must fall in, and their evaluation: ``waiting_user`` and
``repeated_failures`` from the sessions and hook events the writer holds, and the cost
rules (``session_cost``, ``daily_cost``) from a ``CostSample`` of Prometheus's answers to
``session_cost_query`` and ``daily_cost_query``, parsed by ``parse_cost_sample``.

An alert has a key, ``(rule, subject)``: the subject is a ``session_id``, or
``DAY_SUBJECT`` for ``daily_cost``. For each key the evaluators remember when it last
fired and whether the current episode fired. An episode fires once when its value
reaches the threshold and clears when the episode ends, if it fired. A new episode fires
only once ``cooldown_s`` has passed since the key's last fire.

An episode of ``waiting_user`` runs while the session is ``waiting_user``, from its
oldest open block. An episode of ``repeated_failures`` runs while ``count`` or more
``StopFailure`` events of one session fall within the last ``window_s``:
``record_failure_event`` keeps those times per session, from the hook events, and
``evaluate_repeated_failures`` counts them. An episode of a cost rule runs while a
session's total, or the last 24 hours' total, is at or above its limit, and once fired
lasts until the total falls below ``COST_CLEAR_RATIO`` of the limit; a sample older than
``COST_STALE_MS`` neither fires nor clears. Every evaluator shares one ``AlertState``:
the writer records every hook event and calls them on each tick.

There is no wall clock here: the caller passes ``now_ms``. A clock that goes backwards
never fires an episode twice, never raises, and a clear is never earlier than its fire.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from ccobs.domain.grafana_links import SESSION_ID_KEY
from ccobs.domain.hooks import HookEvent, SessionEnd, Stop, StopFailure
from ccobs.domain.sessions import SessionState
from ccobs.domain.telemetry import COST_USAGE, HOST_LABEL, total_query
from ccobs.domain.usage import parse_vector, usage_window

__all__ = [
    "ALERT_RULES",
    "ALERT_SETTINGS",
    "COOLDOWN_BOUNDS",
    "COST_BOUNDS",
    "COST_CLEAR_RATIO",
    "COST_STALE_MS",
    "DAILY_COST_WINDOW",
    "DAY_SUBJECT",
    "FAILURE_COUNT_BOUNDS",
    "FAILURE_WINDOW_BOUNDS",
    "WAITING_BOUNDS",
    "AlertCleared",
    "AlertConfig",
    "AlertEffect",
    "AlertFired",
    "AlertKey",
    "AlertRule",
    "AlertState",
    "Bounds",
    "CostSample",
    "DailyCostRule",
    "FieldError",
    "KeyState",
    "RepeatedFailuresRule",
    "SessionCostRule",
    "WaitingUserRule",
    "apply_saved_settings",
    "daily_cost_query",
    "evaluate_daily_cost",
    "evaluate_repeated_failures",
    "evaluate_session_cost",
    "evaluate_waiting_user",
    "forget_sessions",
    "parse_cost_sample",
    "record_failure_event",
    "session_cost_query",
    "session_cost_since",
    "validate_config",
    "waiting_since",
]

type AlertRule = Literal["waiting_user", "repeated_failures", "session_cost", "daily_cost"]

ALERT_RULES: Final[tuple[AlertRule, ...]] = (
    "waiting_user",
    "repeated_failures",
    "session_cost",
    "daily_cost",
)
# The subject of daily_cost, which is about the whole machine, not one session.
DAY_SUBJECT: Final = "day"

_MS_PER_S: Final = 1000


# Configuration, with the maintainer's defaults (ADR 0041).


@dataclass(frozen=True, kw_only=True)
class WaitingUserRule:
    """A session ``waiting_user`` for ``threshold_s`` seconds or more."""

    enabled: bool = True
    threshold_s: int = 30


@dataclass(frozen=True, kw_only=True)
class RepeatedFailuresRule:
    """``count`` ``StopFailure`` events in one session within ``window_s`` seconds."""

    enabled: bool = True
    count: int = 3
    window_s: int = 600


@dataclass(frozen=True, kw_only=True)
class SessionCostRule:
    """A session's estimated cost at or above ``limit_usd`` US dollars."""

    enabled: bool = False
    limit_usd: float = 10.0


@dataclass(frozen=True, kw_only=True)
class DailyCostRule:
    """The estimated cost of the last 24 hours (rolling) at or above ``limit_usd``."""

    enabled: bool = False
    limit_usd: float = 50.0


@dataclass(frozen=True, kw_only=True)
class AlertConfig:
    """Every rule, plus ``cooldown_s``: the minimum interval between two alerts with the
    same key."""

    waiting_user: WaitingUserRule = field(default_factory=WaitingUserRule)
    repeated_failures: RepeatedFailuresRule = field(default_factory=RepeatedFailuresRule)
    session_cost: SessionCostRule = field(default_factory=SessionCostRule)
    daily_cost: DailyCostRule = field(default_factory=DailyCostRule)
    cooldown_s: int = 300


# Validation.


@dataclass(frozen=True, kw_only=True)
class Bounds:
    """The inclusive range of a setting, and how its error message describes it.

    ``integer`` settings take an ``int`` only; the others an ``int`` or a finite ``float``.
    A ``bool`` is never a number here, though Python takes it as one.
    """

    minimum: float
    maximum: float
    what: str
    integer: bool


@dataclass(frozen=True, kw_only=True)
class FieldError:
    """A setting out of its bounds. The message names the field and the allowed range,
    never the value, which may be anything a user typed."""

    field: str
    message: str


_DAY_S: Final = 86_400
_SECONDS: Final = "a whole number of seconds"
WAITING_BOUNDS: Final = Bounds(minimum=5, maximum=_DAY_S, what=_SECONDS, integer=True)
FAILURE_COUNT_BOUNDS: Final = Bounds(
    minimum=2, maximum=100, what="a whole number of failures", integer=True
)
FAILURE_WINDOW_BOUNDS: Final = Bounds(minimum=60, maximum=_DAY_S, what=_SECONDS, integer=True)
COST_BOUNDS: Final = Bounds(
    minimum=0.01, maximum=100_000, what="an amount in US dollars", integer=False
)
COOLDOWN_BOUNDS: Final = Bounds(minimum=0, maximum=_DAY_S, what=_SECONDS, integer=True)


def _error(name: str, bounds: Bounds) -> FieldError:
    return FieldError(
        field=name,
        message=f"{name} must be {bounds.what} from {bounds.minimum:g} to {bounds.maximum:g}",
    )


def _valid(value: object, bounds: Bounds) -> bool:
    """Whether ``value`` has the type ``bounds`` asks for and lies in its range. Any other
    type, from a bool to a string a settings form sent, is invalid, never an exception."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        number: float = value
    elif isinstance(value, float) and not bounds.integer and math.isfinite(value):
        number = value
    else:
        return False
    return bounds.minimum <= number <= bounds.maximum


# Saved settings (ADR 0042): each setting by its flat name, the Settings field behind
# CCOBS_<NAME> and the field of its alert_settings row, with how it replaces its value
# in a config. Switches take a bool, seconds and counts an int, and dollar limits an int
# or a float.
_SWITCHES: Final[Mapping[str, Callable[[AlertConfig, bool], AlertConfig]]] = {
    "alert_waiting_enabled": lambda c, v: replace(
        c, waiting_user=replace(c.waiting_user, enabled=v)
    ),
    "alert_failures_enabled": lambda c, v: replace(
        c, repeated_failures=replace(c.repeated_failures, enabled=v)
    ),
    "alert_session_cost_enabled": lambda c, v: replace(
        c, session_cost=replace(c.session_cost, enabled=v)
    ),
    "alert_daily_cost_enabled": lambda c, v: replace(
        c, daily_cost=replace(c.daily_cost, enabled=v)
    ),
}
_WHOLE: Final[Mapping[str, Callable[[AlertConfig, int], AlertConfig]]] = {
    "alert_waiting_threshold_s": lambda c, v: replace(
        c, waiting_user=replace(c.waiting_user, threshold_s=v)
    ),
    "alert_failures_count": lambda c, v: replace(
        c, repeated_failures=replace(c.repeated_failures, count=v)
    ),
    "alert_failures_window_s": lambda c, v: replace(
        c, repeated_failures=replace(c.repeated_failures, window_s=v)
    ),
    "alert_cooldown_s": lambda c, v: replace(c, cooldown_s=v),
}
_AMOUNTS: Final[Mapping[str, Callable[[AlertConfig, float], AlertConfig]]] = {
    "alert_session_cost_usd": lambda c, v: replace(
        c, session_cost=replace(c.session_cost, limit_usd=v)
    ),
    "alert_daily_cost_usd": lambda c, v: replace(c, daily_cost=replace(c.daily_cost, limit_usd=v)),
}
ALERT_SETTINGS: Final[tuple[str, ...]] = (
    "alert_waiting_enabled",
    "alert_waiting_threshold_s",
    "alert_failures_enabled",
    "alert_failures_count",
    "alert_failures_window_s",
    "alert_session_cost_enabled",
    "alert_session_cost_usd",
    "alert_daily_cost_enabled",
    "alert_daily_cost_usd",
    "alert_cooldown_s",
)


def _with_setting(config: AlertConfig, name: str, value: object) -> AlertConfig | None:
    """``config`` with setting ``name`` set to ``value``; ``None`` for a value of the
    wrong type (a ``bool`` is never a number, a number never a switch)."""
    if name in _SWITCHES:
        return _SWITCHES[name](config, value) if isinstance(value, bool) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if name in _WHOLE:
        return _WHOLE[name](config, value) if isinstance(value, int) else None
    return _AMOUNTS[name](config, float(value))


def apply_saved_settings(
    base: AlertConfig, saved: Mapping[str, object]
) -> tuple[AlertConfig, tuple[str, ...]]:
    """The effective configuration: ``base`` (the environment's, valid) with the saved
    overrides on top, and the names of the overrides it ignored (ADR 0042).

    Each override in ``ALERT_SETTINGS`` order replaces its setting unless its value has
    the wrong type or leaves the configuration invalid (``validate_config``): then it is
    ignored, and the setting keeps ``base``'s value. A name outside ``ALERT_SETTINGS``
    is skipped and never reported, since it may be any text. The ignored names never
    come with their values.
    """
    config = base
    ignored: list[str] = []
    for name in ALERT_SETTINGS:
        if name not in saved:
            continue
        candidate = _with_setting(config, name, saved[name])
        if candidate is None or validate_config(candidate):
            ignored.append(name)
        else:
            config = candidate
    return config, tuple(ignored)


def validate_config(config: AlertConfig) -> tuple[FieldError, ...]:
    """The settings out of their bounds, in field order; empty when all are valid.

    A disabled rule is validated too, so that turning it on never meets a bad value.
    """
    failures = config.repeated_failures
    checks = (
        ("waiting_user.threshold_s", WAITING_BOUNDS, config.waiting_user.threshold_s),
        ("repeated_failures.count", FAILURE_COUNT_BOUNDS, failures.count),
        ("repeated_failures.window_s", FAILURE_WINDOW_BOUNDS, failures.window_s),
        ("session_cost.limit_usd", COST_BOUNDS, config.session_cost.limit_usd),
        ("daily_cost.limit_usd", COST_BOUNDS, config.daily_cost.limit_usd),
        ("cooldown_s", COOLDOWN_BOUNDS, config.cooldown_s),
    )
    return tuple(
        _error(name, bounds) for name, bounds, value in checks if not _valid(value, bounds)
    )


# State and effects.


@dataclass(frozen=True, kw_only=True, order=True)
class AlertKey:
    """What deduplication and the cooldown apply to: one rule about one subject."""

    rule: AlertRule
    subject: str


@dataclass(frozen=True, kw_only=True)
class KeyState:
    """``firing`` is true from a fire until its clear: the current episode fired."""

    last_fired_at_ms: int
    firing: bool


@dataclass(frozen=True, kw_only=True)
class AlertState:
    """The evaluators' memory, shared by every rule. ``keys`` holds only keys that fired,
    kept while they fire or their cooldown runs. ``failures`` holds, per session, the
    times of the ``StopFailure`` events that may still count, sorted, at most ``count``
    of them, and no empty window. Each call returns a new state and never changes the one
    it was given."""

    keys: Mapping[AlertKey, KeyState] = field(default_factory=dict)
    failures: Mapping[str, tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class AlertFired:
    """``value`` and ``threshold`` are in the rule's unit: seconds for ``waiting_user``,
    failures for ``repeated_failures`` and US dollars for the cost rules."""

    rule: AlertRule
    subject: str
    fired_at_ms: int
    value: float
    threshold: float


@dataclass(frozen=True, kw_only=True)
class AlertCleared:
    rule: AlertRule
    subject: str
    cleared_at_ms: int


type AlertEffect = AlertFired | AlertCleared


# The waiting-on-you rule.


def waiting_since(state: SessionState) -> int | None:
    """When the session's current wait began: the opening of its oldest open block, or
    ``None`` when the session is not ``waiting_user``."""
    if state.session.status != "waiting_user" or not state.open_blocks:
        return None
    return min(block.opened_at_ms for block in state.open_blocks)


@dataclass(frozen=True, kw_only=True)
class _Episode:
    """A rule's condition holding for a subject at ``now_ms``. ``reached`` is whether its
    ``value`` reached the ``threshold``, which may be later than the episode's start."""

    reached: bool
    value: float
    threshold: float


def _step(
    key: AlertKey,
    previous: KeyState | None,
    episode: _Episode | None,
    *,
    cooldown_ms: int,
    now_ms: int,
) -> tuple[KeyState | None, AlertEffect | None]:
    """One key of any rule: its new state (``None`` once there is nothing left to
    remember) and its effect, if any. ``episode`` is ``None`` when no episode runs."""
    effect: AlertEffect | None = None
    if previous is not None and previous.firing:
        if episode is not None:
            return previous, None  # this episode already fired
        effect = AlertCleared(
            rule=key.rule,
            subject=key.subject,
            cleared_at_ms=max(now_ms, previous.last_fired_at_ms),
        )
        previous = KeyState(last_fired_at_ms=previous.last_fired_at_ms, firing=False)
    elif (
        episode is not None
        and episode.reached
        and (previous is None or now_ms >= previous.last_fired_at_ms + cooldown_ms)
    ):
        fired = AlertFired(
            rule=key.rule,
            subject=key.subject,
            fired_at_ms=now_ms,
            value=episode.value,
            threshold=episode.threshold,
        )
        return KeyState(last_fired_at_ms=now_ms, firing=True), fired
    # Not firing: the key matters only while its cooldown runs.
    if previous is not None and now_ms < previous.last_fired_at_ms + cooldown_ms:
        return previous, effect
    return None, effect


def _evaluate(
    state: AlertState,
    rule: AlertRule,
    episodes: Mapping[str, _Episode],
    *,
    cooldown_ms: int,
    now_ms: int,
) -> tuple[dict[AlertKey, KeyState], list[AlertEffect]]:
    """Step every key of ``rule`` that is remembered or has an episode, by subject. The
    keys of other rules pass through unchanged."""
    keys = {k: v for k, v in state.keys.items() if k.rule != rule}
    known = {k.subject: v for k, v in state.keys.items() if k.rule == rule}
    effects: list[AlertEffect] = []
    for subject in sorted(known.keys() | episodes.keys()):
        key = AlertKey(rule=rule, subject=subject)
        kept, effect = _step(
            key, known.get(subject), episodes.get(subject), cooldown_ms=cooldown_ms, now_ms=now_ms
        )
        if kept is not None:
            keys[key] = kept
        if effect is not None:
            effects.append(effect)
    return keys, effects


def evaluate_waiting_user(
    state: AlertState,
    sessions: Iterable[SessionState],
    *,
    config: AlertConfig,
    now_ms: int,
) -> tuple[AlertState, list[AlertEffect]]:
    """Evaluate ``waiting_user`` at ``now_ms`` over the sessions the caller holds.

    A session absent from ``sessions`` (ended and dropped, or removed) is not waiting.
    The keys of other rules pass through unchanged. Effects come sorted by subject.
    With the rule disabled nothing fires, and what had fired clears. The work is linear
    in the sessions plus the keys that fire or cool down.
    """
    rule = config.waiting_user
    episodes: dict[str, _Episode] = {}
    if rule.enabled:
        for session_state in sessions:
            since = waiting_since(session_state)
            if since is not None:
                episodes[session_state.session.session_id] = _Episode(
                    reached=now_ms - since >= rule.threshold_s * _MS_PER_S,
                    value=(now_ms - since) / _MS_PER_S,
                    threshold=float(rule.threshold_s),
                )
    keys, effects = _evaluate(
        state,
        "waiting_user",
        episodes,
        cooldown_ms=config.cooldown_s * _MS_PER_S,
        now_ms=now_ms,
    )
    return replace(state, keys=keys), effects


# The repeated-failures rule.


def record_failure_event(
    state: AlertState, event: HookEvent, at_ms: int, *, config: AlertConfig
) -> AlertState:
    """Record a hook event, received at ``at_ms``, in the failure windows.

    - A ``StopFailure`` of any scope and any ``error`` adds its time (only its time) to
      its session's window, in time order: a late one is inserted where it belongs. The
      window keeps the newest ``count`` times, since older ones can never complete a
      streak, so a late failure older than a full window is dropped. With the rule
      disabled nothing is added.
    - A main-agent ``Stop`` (no ``agent_id``) is a successful turn: it drops every
      failure of the session recorded so far, in processing order (ADR 0024), whatever
      their times, so a clock that steps back never keeps an alert firing. A subagent's
      ``Stop`` or ``SubagentStop`` proves nothing about the main turn and changes
      nothing.
    - ``SessionEnd`` drops the session's window.

    Any other event returns ``state`` itself. A window left empty is removed.
    """
    session_id = event.session_id
    times = state.failures.get(session_id, ())
    if isinstance(event, StopFailure):
        rule = config.repeated_failures
        if not rule.enabled:
            return state
        times = tuple(sorted((*times, at_ms))[-rule.count :])
    elif (isinstance(event, Stop) and event.agent_id is None) or isinstance(event, SessionEnd):
        times = ()
    else:
        return state
    failures = {k: v for k, v in state.failures.items() if k != session_id}
    if times:
        failures[session_id] = times
    return replace(state, failures=failures)


def forget_sessions(state: AlertState, session_ids: frozenset[str]) -> AlertState:
    """Forget the sessions retention deleted: their failure windows and the keys whose
    subject they are. Their alerts went with them (cascade), so none can fire or clear
    again; ``daily_cost``, about no session, is kept. Returns ``state`` itself when it
    holds none of them."""
    keys = {
        key: value
        for key, value in state.keys.items()
        if key.rule == "daily_cost" or key.subject not in session_ids
    }
    failures = {k: v for k, v in state.failures.items() if k not in session_ids}
    if len(keys) == len(state.keys) and len(failures) == len(state.failures):
        return state
    return replace(state, keys=keys, failures=failures)


def evaluate_repeated_failures(
    state: AlertState, *, config: AlertConfig, now_ms: int
) -> tuple[AlertState, list[AlertEffect]]:
    """Evaluate ``repeated_failures`` at ``now_ms`` over the recorded failure windows.

    A failure counts while it is at most ``window_s`` old: at ``now_ms - window_s`` it
    still counts, one millisecond later it no longer does. A failure recorded after
    ``now_ms`` (the clock went backwards) counts, since it happened. An episode runs
    while ``count`` or more failures count; it ends, and clears if it fired, when aging
    or a successful ``Stop`` brings the count under ``count``, or ``SessionEnd`` drops the
    window. Failures that no longer count leave the windows, and so do empty windows.

    The keys of other rules pass through unchanged. Effects come sorted by subject. With
    the rule disabled nothing fires, what had fired clears and every window is dropped.
    """
    rule = config.repeated_failures
    oldest = now_ms - rule.window_s * _MS_PER_S
    failures: dict[str, tuple[int, ...]] = {}
    episodes: dict[str, _Episode] = {}
    if rule.enabled:
        for session_id, times in state.failures.items():
            counted = tuple(at for at in times if at >= oldest)
            if counted:
                failures[session_id] = counted
            if len(counted) >= rule.count:
                episodes[session_id] = _Episode(
                    reached=True, value=float(len(counted)), threshold=float(rule.count)
                )
    keys, effects = _evaluate(
        state,
        "repeated_failures",
        episodes,
        cooldown_ms=config.cooldown_s * _MS_PER_S,
        now_ms=now_ms,
    )
    return replace(state, keys=keys, failures=failures), effects


# The cost rules.

# A sample older than this (3 minutes: three of Claude Code's one-minute exports, and
# many of the reader's polls) says nothing about now: it neither fires nor clears.
COST_STALE_MS: Final = 180_000
# Hysteresis: an episode that fired lasts while its total stays at or above this share of
# the limit, so a total that wobbles around the limit (the rolling day losing an old
# request, then gaining a new one) does not clear and fire again.
COST_CLEAR_RATIO: Final = 0.9
# The rolling day of daily_cost (maintainer's decision P2: no time zone).
DAILY_COST_WINDOW: Final = "24h"


@dataclass(frozen=True, kw_only=True)
class CostSample:
    """What the cost queries answered, run at ``observed_at_ms``: each session's total
    in US dollars, by ``session_id``, and the total of the last 24 hours, ``None`` when
    the day was not asked."""

    observed_at_ms: int
    sessions: Mapping[str, float]
    daily_usd: float | None = None


def _open(state: SessionState) -> bool:
    """Whether the session can alert on its cost: neither ``ended`` nor ``stale``."""
    return state.session.status not in ("ended", "stale")


def session_cost_since(sessions: Iterable[SessionState]) -> int | None:
    """The ``since_ms`` of ``session_cost_query``: the earliest ``started_at_ms`` of the
    sessions that can alert (neither ``ended`` nor ``stale``), or ``None`` when there is
    none, and then the reader sends no query.

    The range grows with the oldest open session's age: one idle but alive for days makes
    a range of days, and every query reads all the host's cost samples in it (ADR 0041).
    """
    return min((s.session.started_at_ms for s in sessions if _open(s)), default=None)


def session_cost_query(host: str, *, since_ms: int, now_ms: int) -> str:
    """Every session's estimated cost, in one instant query to run at ``now_ms``:
    ``sum by (session_id) (increase(<cost>{host="<host>"}[<range>] anchored))``.

    It is the session page's query (ADR 0035, ``usage.usage_queries``) without the
    ``session_id`` matcher and grouped by ``session_id`` instead of ``model``, so one
    query covers every session and no session ID reaches Prometheus. ``since_ms`` is the
    start of the oldest session to evaluate (``session_cost_since``), and the range is
    that session's page window (``usage.usage_window`` while live: from
    ``WINDOW_BEFORE_MS`` before its start to now). Each session the range covers from its
    start has nothing before the range, so
    ``anchored`` takes its starting value from the zero sample at its start (ADR 0013)
    whatever the range's length, and its total is the one its page shows. Older series
    inside the range (sessions the caller no longer holds) undercount, and the
    evaluator ignores them. Raises ``ValueError`` for a host that is not valid UTF-8.
    """
    window = usage_window(since_ms, None, now_ms)
    return total_query(
        COST_USAGE,
        window=window.range,
        by=(SESSION_ID_KEY,),
        matchers={HOST_LABEL: host},
    )


def daily_cost_query(host: str) -> str:
    """The estimated cost of the last 24 hours across sessions, in one instant query:
    ``sum(increase(<cost>{host="<host>"}[24h] anchored))``.

    ADR 0013's exact total over a range that ends at the query's time: what the session
    pages show, summed over every session and cut at 24 hours ago. A series whose last
    sample before the range is older than Prometheus's lookback (5 minutes by default)
    takes its starting value from its first sample inside the range instead (ADR 0013),
    so a session idle across the range's start can undercount what it spent before its
    next export. Raises ``ValueError`` for a host that is not valid UTF-8.
    """
    return total_query(COST_USAGE, window=DAILY_COST_WINDOW, matchers={HOST_LABEL: host})


def parse_cost_sample(
    *, observed_at_ms: int, sessions: object, daily: object | None = None
) -> CostSample | None:
    """The sample of the decoded answers to ``session_cost_query`` and, unless ``None``
    (not asked), ``daily_cost_query``; ``None`` when either is not a valid instant
    vector of finite, non-negative values (``usage.parse_vector``), so a rule never acts
    on part of a total.

    A series without ``session_id`` (``OTEL_METRICS_INCLUDE_SESSION_ID=false``) belongs
    to no session and is left out. The day is the value of its one series; an empty
    vector, which ``sum`` gives over no series, is US$ 0, and more than one series is
    invalid.
    """
    vector = parse_vector(sessions, SESSION_ID_KEY)
    if vector is None:
        return None
    daily_usd: float | None = None
    if daily is not None:
        day = parse_vector(daily, SESSION_ID_KEY)
        if day is None or len(day) > 1:
            return None
        daily_usd = day[0][1] if day else 0.0
    return CostSample(
        observed_at_ms=observed_at_ms,
        sessions={session_id: usd for session_id, usd in vector if session_id},
        daily_usd=daily_usd,
    )


def _fresh(sample: CostSample, now_ms: int) -> bool:
    """Whether ``sample`` may fire or clear at ``now_ms``: at most ``COST_STALE_MS`` old.
    A sample from after ``now_ms`` (the clock went backwards) is fresh."""
    return now_ms - sample.observed_at_ms <= COST_STALE_MS


# Holds a firing key while a stale sample says nothing: _step keeps it as it is.
_HOLD: Final = _Episode(reached=False, value=0.0, threshold=0.0)


def _cost_episodes(
    state: AlertState,
    rule: AlertRule,
    totals: Mapping[str, float] | None,
    subjects: Iterable[str],
    *,
    limit: float,
) -> dict[str, _Episode]:
    """The episodes of a cost rule over ``subjects``, the subjects that can alert.

    With ``totals`` (a fresh sample), an episode starts at ``limit`` and, once it fired,
    lasts down to ``limit * COST_CLEAR_RATIO``; a subject without a total has none.
    Without (``None``: stale or missing), every firing subject holds and nothing starts.
    """
    firing = {k.subject for k, v in state.keys.items() if k.rule == rule and v.firing}
    episodes: dict[str, _Episode] = {}
    for subject in subjects:
        if totals is None:
            if subject in firing:
                episodes[subject] = _HOLD
            continue
        total = totals.get(subject)
        if total is None:
            continue
        floor = limit * COST_CLEAR_RATIO if subject in firing else limit
        if total >= floor:
            episodes[subject] = _Episode(reached=total >= limit, value=total, threshold=limit)
    return episodes


def evaluate_session_cost(
    state: AlertState,
    sample: CostSample | None,
    sessions: Iterable[SessionState],
    *,
    config: AlertConfig,
    now_ms: int,
) -> tuple[AlertState, list[AlertEffect]]:
    """Evaluate ``session_cost`` at ``now_ms`` over ``sample`` and the sessions the
    caller holds.

    Only a session of ``sessions`` that is neither ``ended`` nor ``stale`` can alert: a
    session of the sample the caller does not hold is ignored, and one that ends, turns
    stale, leaves ``sessions`` or leaves the sample ends its episode, clearing if it
    fired. Its episode fires when its total reaches ``limit_usd`` (value: the total,
    threshold: the limit) and, once fired, clears only when the total falls below
    ``limit_usd * COST_CLEAR_RATIO``. A stale or missing ``sample`` (``None``, or older
    than ``COST_STALE_MS``) neither fires nor clears a session it still holds.

    The keys of other rules pass through unchanged. Effects come sorted by subject. With
    the rule disabled nothing fires, and what had fired clears.
    """
    rule = config.session_cost
    episodes: dict[str, _Episode] = {}
    if rule.enabled:
        open_ids = (s.session.session_id for s in sessions if _open(s))
        totals = sample.sessions if sample is not None and _fresh(sample, now_ms) else None
        episodes = _cost_episodes(state, "session_cost", totals, open_ids, limit=rule.limit_usd)
    keys, effects = _evaluate(
        state,
        "session_cost",
        episodes,
        cooldown_ms=config.cooldown_s * _MS_PER_S,
        now_ms=now_ms,
    )
    return replace(state, keys=keys), effects


def evaluate_daily_cost(
    state: AlertState, sample: CostSample | None, *, config: AlertConfig, now_ms: int
) -> tuple[AlertState, list[AlertEffect]]:
    """Evaluate ``daily_cost`` at ``now_ms`` over ``sample``'s rolling 24 hours, with the
    subject ``DAY_SUBJECT``.

    The episode fires when the day's total reaches ``limit_usd`` and, once fired, clears
    only when the total falls below ``limit_usd * COST_CLEAR_RATIO``, as old requests
    leave the rolling day. A sample that is stale, missing or without the day neither
    fires nor clears.

    The keys of other rules pass through unchanged. With the rule disabled nothing fires,
    and what had fired clears.
    """
    rule = config.daily_cost
    episodes: dict[str, _Episode] = {}
    if rule.enabled:
        totals = (
            {DAY_SUBJECT: sample.daily_usd}
            if sample is not None and sample.daily_usd is not None and _fresh(sample, now_ms)
            else None
        )
        episodes = _cost_episodes(state, "daily_cost", totals, (DAY_SUBJECT,), limit=rule.limit_usd)
    keys, effects = _evaluate(
        state,
        "daily_cost",
        episodes,
        cooldown_ms=config.cooldown_s * _MS_PER_S,
        now_ms=now_ms,
    )
    return replace(state, keys=keys), effects
