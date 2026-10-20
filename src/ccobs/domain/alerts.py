"""Alert rules, their defaults and bounds, and their evaluators (ADR 0041).

ADR 0007 names four rules; this module holds their configuration with the maintainer's
defaults, the bounds a value must fall in, and the evaluation of ``waiting_user`` and
``repeated_failures``. The cost rules (``session_cost``, ``daily_cost``) have
configuration and validation here and are evaluated by later stages.

An alert has a key, ``(rule, subject)``: the subject is a ``session_id``, or
``DAY_SUBJECT`` for ``daily_cost``. For each key the evaluators remember when it last
fired and whether the current episode fired. An episode fires once when its value
reaches the threshold and clears when the episode ends, if it fired. A new episode fires
only once ``cooldown_s`` has passed since the key's last fire.

An episode of ``waiting_user`` runs while the session is ``waiting_user``, from its
oldest open block. An episode of ``repeated_failures`` runs while ``count`` or more
``StopFailure`` events of one session fall within the last ``window_s``:
``record_failure_event`` keeps those times per session, from the hook events, and
``evaluate_repeated_failures`` counts them. Both evaluators share one ``AlertState``:
the writer records every hook event and calls both on each tick.

There is no wall clock here: the caller passes ``now_ms``. A clock that goes backwards
never fires an episode twice, never raises, and a clear is never earlier than its fire.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from ccobs.domain.hooks import HookEvent, SessionEnd, Stop, StopFailure
from ccobs.domain.sessions import SessionState

__all__ = [
    "ALERT_RULES",
    "COOLDOWN_BOUNDS",
    "COST_BOUNDS",
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
    "DailyCostRule",
    "FieldError",
    "KeyState",
    "RepeatedFailuresRule",
    "SessionCostRule",
    "WaitingUserRule",
    "evaluate_repeated_failures",
    "evaluate_waiting_user",
    "record_failure_event",
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
