"""The effective alert configuration: the environment's, with saved overrides on top.

``apply_saved_settings`` (ADR 0042) applies each saved setting, by its flat name, over
the configuration the environment gives, and ignores (by name, never by value) a saved
value of the wrong type or out of bounds, so the environment's value stays in force.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Final

import pytest

from ccobs.domain.alerts import (
    ALERT_SETTINGS,
    AlertConfig,
    DailyCostRule,
    FieldError,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
    apply_saved_settings,
    apply_settings,
    setting_error,
    setting_value,
    validate_config,
)
from ccobs.server.config import Settings
from ccobs.server.db.repository import ALERT_SETTING_TYPES

# An environment that differs from the defaults in every setting.
ENV: Final = AlertConfig(
    waiting_user=WaitingUserRule(enabled=False, threshold_s=45),
    repeated_failures=RepeatedFailuresRule(enabled=False, count=4, window_s=900),
    session_cost=SessionCostRule(enabled=True, limit_usd=12.5),
    daily_cost=DailyCostRule(enabled=True, limit_usd=60.0),
    cooldown_s=120,
)


def test_without_overrides_the_environment_is_the_configuration() -> None:
    assert apply_saved_settings(ENV, {}) == (ENV, ())


def test_every_override_replaces_its_setting() -> None:
    saved: dict[str, object] = {
        "alert_waiting_enabled": True,
        "alert_waiting_threshold_s": 90,
        "alert_failures_enabled": True,
        "alert_failures_count": 5,
        "alert_failures_window_s": 1200,
        "alert_session_cost_enabled": False,
        "alert_session_cost_usd": 3,
        "alert_daily_cost_enabled": False,
        "alert_daily_cost_usd": 70.25,
        "alert_cooldown_s": 0,
    }

    config, ignored = apply_saved_settings(ENV, saved)

    assert ignored == ()
    assert config == AlertConfig(
        waiting_user=WaitingUserRule(enabled=True, threshold_s=90),
        repeated_failures=RepeatedFailuresRule(enabled=True, count=5, window_s=1200),
        session_cost=SessionCostRule(enabled=False, limit_usd=3.0),
        daily_cost=DailyCostRule(enabled=False, limit_usd=70.25),
        cooldown_s=0,
    )
    assert isinstance(config.session_cost.limit_usd, float)


def test_a_partial_override_keeps_the_other_settings_from_the_environment() -> None:
    config, ignored = apply_saved_settings(ENV, {"alert_failures_count": 7})

    assert ignored == ()
    assert config.repeated_failures == RepeatedFailuresRule(enabled=False, count=7, window_s=900)
    assert config.waiting_user == ENV.waiting_user
    assert config.cooldown_s == ENV.cooldown_s


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # Out of bounds (ADR 0041).
        ("alert_waiting_threshold_s", 4),
        ("alert_failures_count", 101),
        ("alert_failures_window_s", 59),
        ("alert_session_cost_usd", 0.0),
        ("alert_daily_cost_usd", float("nan")),
        ("alert_daily_cost_usd", float("inf")),
        ("alert_cooldown_s", -1),
        # The wrong type: a bool is never a number, a number never a switch.
        ("alert_waiting_threshold_s", True),
        ("alert_waiting_threshold_s", 30.0),
        ("alert_session_cost_usd", False),
        ("alert_session_cost_usd", "10"),
        ("alert_waiting_enabled", 1),
        ("alert_failures_enabled", "true"),
        ("alert_cooldown_s", None),
    ],
)
def test_an_invalid_override_is_ignored_by_name(name: str, value: object) -> None:
    # A valid override next to the bad one still applies.
    good = "alert_failures_count" if name == "alert_cooldown_s" else "alert_cooldown_s"
    expected, _ = apply_saved_settings(ENV, {good: 7})

    config, ignored = apply_saved_settings(ENV, {name: value, good: 7})

    assert expected != ENV
    assert config == expected
    assert ignored == (name,)
    assert validate_config(config) == ()


def test_ignored_names_come_in_settings_order_and_unknown_names_are_never_reported() -> None:
    saved: dict[str, object] = {
        "alert_cooldown_s": -5,
        "not_a_setting C:\\Users\\someone": 3,
        "alert_waiting_threshold_s": 2,
        "alert_daily_cost_usd": 80,
    }

    config, ignored = apply_saved_settings(ENV, saved)

    assert ignored == ("alert_waiting_threshold_s", "alert_cooldown_s")
    assert config.daily_cost.limit_usd == 80.0
    assert config.waiting_user.threshold_s == ENV.waiting_user.threshold_s


def test_the_settings_are_the_repositorys_and_the_environments() -> None:
    # One list of names: the rows the repository stores and the CCOBS_ALERT_* fields.
    assert set(ALERT_SETTINGS) == set(ALERT_SETTING_TYPES)
    assert len(set(ALERT_SETTINGS)) == len(ALERT_SETTINGS)
    env_fields = {field for field in Settings.model_fields if field.startswith("alert_")}
    assert set(ALERT_SETTINGS) == env_fields
    assert len(fields(AlertConfig)) == 5


# The settings screen's changes (ADR 0045): typed values by flat name, every error by
# flat name with the range, never the value.

ALL_SET: Final[dict[str, bool | int | float]] = {
    "alert_waiting_enabled": True,
    "alert_waiting_threshold_s": 90,
    "alert_failures_enabled": True,
    "alert_failures_count": 5,
    "alert_failures_window_s": 1200,
    "alert_session_cost_enabled": False,
    "alert_session_cost_usd": 3.5,
    "alert_daily_cost_enabled": False,
    "alert_daily_cost_usd": 70.25,
    "alert_cooldown_s": 0,
}


def test_setting_value_reads_every_setting_by_its_flat_name() -> None:
    values = {name: setting_value(ENV, name) for name in ALERT_SETTINGS}

    assert values == {
        "alert_waiting_enabled": False,
        "alert_waiting_threshold_s": 45,
        "alert_failures_enabled": False,
        "alert_failures_count": 4,
        "alert_failures_window_s": 900,
        "alert_session_cost_enabled": True,
        "alert_session_cost_usd": 12.5,
        "alert_daily_cost_enabled": True,
        "alert_daily_cost_usd": 60.0,
        "alert_cooldown_s": 120,
    }


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("alert_waiting_enabled", "alert_waiting_enabled must be true or false"),
        (
            "alert_waiting_threshold_s",
            "alert_waiting_threshold_s must be a whole number of seconds from 5 to 86400",
        ),
        (
            "alert_failures_count",
            "alert_failures_count must be a whole number of failures from 2 to 100",
        ),
        (
            "alert_failures_window_s",
            "alert_failures_window_s must be a whole number of seconds from 60 to 86400",
        ),
        (
            "alert_session_cost_usd",
            "alert_session_cost_usd must be an amount in US dollars from 0.01 to 100000",
        ),
        (
            "alert_daily_cost_usd",
            "alert_daily_cost_usd must be an amount in US dollars from 0.01 to 100000",
        ),
        ("alert_cooldown_s", "alert_cooldown_s must be a whole number of seconds from 0 to 86400"),
    ],
)
def test_setting_error_names_the_field_and_its_range(name: str, message: str) -> None:
    assert setting_error(name) == FieldError(field=name, message=message)


def test_apply_settings_replaces_every_setting_given() -> None:
    config, errors = apply_settings(ENV, ALL_SET)

    assert errors == ()
    assert config == AlertConfig(
        waiting_user=WaitingUserRule(enabled=True, threshold_s=90),
        repeated_failures=RepeatedFailuresRule(enabled=True, count=5, window_s=1200),
        session_cost=SessionCostRule(enabled=False, limit_usd=3.5),
        daily_cost=DailyCostRule(enabled=False, limit_usd=70.25),
        cooldown_s=0,
    )


def test_apply_settings_keeps_the_settings_not_given_and_ignores_unknown_names() -> None:
    config, errors = apply_settings(ENV, {"alert_failures_count": 7, "other": "x"})

    assert errors == ()
    assert config == AlertConfig(
        waiting_user=ENV.waiting_user,
        repeated_failures=RepeatedFailuresRule(enabled=False, count=7, window_s=900),
        session_cost=ENV.session_cost,
        daily_cost=ENV.daily_cost,
        cooldown_s=ENV.cooldown_s,
    )
    assert apply_settings(ENV, {}) == (ENV, ())


def test_an_int_amount_becomes_a_float() -> None:
    config, _ = apply_settings(ENV, {"alert_daily_cost_usd": 80})

    assert config.daily_cost.limit_usd == 80.0
    assert isinstance(config.daily_cost.limit_usd, float)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("alert_waiting_threshold_s", 4),
        ("alert_failures_count", 101),
        ("alert_failures_window_s", 59),
        ("alert_session_cost_usd", 0.0),
        ("alert_daily_cost_usd", float("nan")),
        ("alert_cooldown_s", -1),
        ("alert_waiting_threshold_s", True),
        ("alert_waiting_threshold_s", 30.0),
        ("alert_session_cost_usd", "10"),
        ("alert_waiting_enabled", 1),
        ("alert_failures_enabled", "true"),
        ("alert_cooldown_s", None),
    ],
)
def test_apply_settings_refuses_a_bad_value_by_name_and_changes_nothing(
    name: str, value: object
) -> None:
    # A valid change next to the bad one is not applied either.
    good = "alert_failures_count" if name == "alert_cooldown_s" else "alert_cooldown_s"
    config, errors = apply_settings(ENV, {name: value, good: 7})

    assert config == ENV
    # The message is the setting's own, so it never holds the value.
    assert errors == (setting_error(name),)


def test_apply_settings_reports_every_bad_setting_in_settings_order() -> None:
    values: dict[str, object] = {
        "alert_cooldown_s": -5,
        "alert_waiting_enabled": "yes",
        "alert_daily_cost_usd": 0,
        "alert_waiting_threshold_s": 2,
    }

    config, errors = apply_settings(ENV, values)

    assert config == ENV
    assert [error.field for error in errors] == [
        "alert_waiting_enabled",
        "alert_waiting_threshold_s",
        "alert_daily_cost_usd",
        "alert_cooldown_s",
    ]
