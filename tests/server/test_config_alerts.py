"""The alert settings and the Discord webhook, read from ``CCOBS_*`` variables.

The alert settings map onto ``ccobs.domain.alerts.AlertConfig``, with its defaults and
its bounds (ADR 0041). The webhook URL is a secret (ADR 0007): no error, repr, dump or
log line may hold it, or any part of it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Final, cast

import pytest
from pydantic import SecretStr

from ccobs.domain.alerts import (
    COOLDOWN_BOUNDS,
    COST_BOUNDS,
    FAILURE_COUNT_BOUNDS,
    FAILURE_WINDOW_BOUNDS,
    WAITING_BOUNDS,
    AlertConfig,
    Bounds,
    DailyCostRule,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
)
from ccobs.server import __main__ as server_main
from ccobs.server.config import Settings, SettingsError, load_settings

# A fake webhook URL of the right shape: if it shows up anywhere, the secret leaked.
CANARY_TAIL: Final = "CANARY-not-a-real-token"
CANARY_ID: Final = "123456789012345678"
CANARY_URL: Final = f"https://discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}"
WEBHOOK: Final = "CCOBS_DISCORD_WEBHOOK_URL"

# Each numeric alert variable, its bounds, and a valid value that is not the default.
NUMERIC: Final[dict[str, tuple[Bounds, str]]] = {
    "CCOBS_ALERT_WAITING_THRESHOLD_S": (WAITING_BOUNDS, "45"),
    "CCOBS_ALERT_FAILURES_COUNT": (FAILURE_COUNT_BOUNDS, "5"),
    "CCOBS_ALERT_FAILURES_WINDOW_S": (FAILURE_WINDOW_BOUNDS, "900"),
    "CCOBS_ALERT_SESSION_COST_USD": (COST_BOUNDS, "2.5"),
    "CCOBS_ALERT_DAILY_COST_USD": (COST_BOUNDS, "75"),
    "CCOBS_ALERT_COOLDOWN_S": (COOLDOWN_BOUNDS, "0"),
}
SWITCHES: Final = (
    "CCOBS_ALERT_WAITING_ENABLED",
    "CCOBS_ALERT_FAILURES_ENABLED",
    "CCOBS_ALERT_SESSION_COST_ENABLED",
    "CCOBS_ALERT_DAILY_COST_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No CCOBS_* variable from the developer's shell, and a cwd with no .env in it.
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def _refused(monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> str:
    """The error message when ``variable`` is ``value``."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(SettingsError) as caught:
        load_settings()
    return str(caught.value)


# Alert defaults and parsing.


def test_alert_defaults_are_the_domain_defaults() -> None:
    settings = load_settings()

    assert settings.alert_config == AlertConfig()


def test_alert_field_defaults_come_from_the_domain() -> None:
    # Each default is declared once, in ccobs.domain.alerts.
    defaults = AlertConfig()
    fields = Settings.model_fields
    assert fields["alert_waiting_enabled"].default is defaults.waiting_user.enabled
    assert fields["alert_waiting_threshold_s"].default == defaults.waiting_user.threshold_s
    assert fields["alert_failures_enabled"].default is defaults.repeated_failures.enabled
    assert fields["alert_failures_count"].default == defaults.repeated_failures.count
    assert fields["alert_failures_window_s"].default == defaults.repeated_failures.window_s
    assert fields["alert_session_cost_enabled"].default is defaults.session_cost.enabled
    assert fields["alert_session_cost_usd"].default == defaults.session_cost.limit_usd
    assert fields["alert_daily_cost_enabled"].default is defaults.daily_cost.enabled
    assert fields["alert_daily_cost_usd"].default == defaults.daily_cost.limit_usd
    assert fields["alert_cooldown_s"].default == defaults.cooldown_s


def test_every_alert_variable_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, (_, value) in NUMERIC.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CCOBS_ALERT_WAITING_ENABLED", "false")
    monkeypatch.setenv("CCOBS_ALERT_FAILURES_ENABLED", "false")
    monkeypatch.setenv("CCOBS_ALERT_SESSION_COST_ENABLED", "true")
    monkeypatch.setenv("CCOBS_ALERT_DAILY_COST_ENABLED", "true")

    config = load_settings().alert_config

    assert config == AlertConfig(
        waiting_user=WaitingUserRule(enabled=False, threshold_s=45),
        repeated_failures=RepeatedFailuresRule(enabled=False, count=5, window_s=900),
        session_cost=SessionCostRule(enabled=True, limit_usd=2.5),
        daily_cost=DailyCostRule(enabled=True, limit_usd=75.0),
        cooldown_s=0,
    )
    # Whole numbers stay ints, amounts are floats: the domain's types.
    assert type(config.waiting_user.threshold_s) is int
    assert type(config.daily_cost.limit_usd) is float


@pytest.mark.parametrize("variable", list(NUMERIC))
def test_alert_bounds_are_inclusive(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    bounds, _ = NUMERIC[variable]
    for edge in (bounds.minimum, bounds.maximum):
        monkeypatch.setenv(variable, f"{edge:g}")
        load_settings()


@pytest.mark.parametrize("variable", list(NUMERIC))
def test_alert_values_out_of_bounds_name_the_variable_and_range(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    bounds, _ = NUMERIC[variable]
    below = f"{bounds.minimum - (0.001 if not bounds.integer else 1):g}"
    above = f"{bounds.maximum + 1:g}"
    for value in (below, above):
        message = _refused(monkeypatch, variable, value)
        expected = (
            f"  {variable}: must be {bounds.what} from {bounds.minimum:g} to {bounds.maximum:g}"
        )
        # The whole report: the variable and the range, and nothing of the value.
        assert message.splitlines()[1:] == [expected], message


@pytest.mark.parametrize("variable", list(NUMERIC))
@pytest.mark.parametrize("value", ["abc-4242", "1e999x", "", "true4242"])
def test_alert_values_that_are_not_numbers_are_refused_without_the_value(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    message = _refused(monkeypatch, variable, value)

    assert variable in message
    if value:
        assert value not in message


@pytest.mark.parametrize(
    "variable",
    ["CCOBS_ALERT_WAITING_THRESHOLD_S", "CCOBS_ALERT_FAILURES_COUNT", "CCOBS_ALERT_COOLDOWN_S"],
)
def test_whole_number_alert_values_refuse_fractions(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    assert variable in _refused(monkeypatch, variable, "30.5")


@pytest.mark.parametrize("variable", ["CCOBS_ALERT_SESSION_COST_USD", "CCOBS_ALERT_DAILY_COST_USD"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "infinity"])
def test_cost_limits_refuse_non_finite_numbers(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    message = _refused(monkeypatch, variable, value)

    assert f"  {variable}: must be an amount in US dollars from 0.01 to 100000" in message
    assert value not in message.lower()


@pytest.mark.parametrize("variable", SWITCHES)
def test_alert_switches_refuse_anything_but_a_boolean(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    message = _refused(monkeypatch, variable, "maybe-4242")

    assert variable in message
    assert "4242" not in message


def test_every_invalid_alert_variable_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, (bounds, _) in NUMERIC.items():
        monkeypatch.setenv(name, f"{bounds.maximum + 1:g}")

    with pytest.raises(SettingsError) as caught:
        load_settings()
    lines = str(caught.value).splitlines()
    for name in NUMERIC:
        assert sum(line.startswith(f"  {name}:") for line in lines) == 1, name


def test_alert_config_of_settings_built_in_code() -> None:
    settings = Settings(alert_cooldown_s=60, alert_session_cost_usd=3)

    assert settings.alert_config.cooldown_s == 60
    assert settings.alert_config.session_cost.limit_usd == 3.0


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("alert_cooldown_s", "must be a whole number of seconds from 0 to 86400"),
        ("alert_daily_cost_usd", "must be an amount in US dollars from 0.01 to 100000"),
    ],
)
def test_alert_settings_refuse_a_bool_for_a_number(field: str, message: str) -> None:
    # validate_config never takes a bool as a number; neither do the settings, though
    # pydantic alone would read True as 1.
    with pytest.raises(ValueError, match=field) as caught:
        Settings.model_validate({field: True})
    assert message in str(caught.value)


# Discord.


def test_discord_is_not_configured_by_default() -> None:
    settings = load_settings()

    assert settings.discord_webhook_url is None
    assert settings.discord_configured is False
    assert settings.discord_include_project is False


def test_an_empty_webhook_url_means_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEBHOOK, "")

    settings = load_settings()

    assert settings.discord_webhook_url is None
    assert settings.discord_configured is False


def test_include_project_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_DISCORD_INCLUDE_PROJECT", "true")

    assert load_settings().discord_include_project is True


def test_include_project_refuses_anything_but_a_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "CCOBS_DISCORD_INCLUDE_PROJECT" in _refused(
        monkeypatch, "CCOBS_DISCORD_INCLUDE_PROJECT", "sometimes"
    )


@pytest.mark.parametrize(
    "url",
    [
        CANARY_URL,
        f"https://discordapp.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}",
        f"https://canary.discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}",
        f"https://ptb.discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}",
        f"https://discord.com/api/v10/webhooks/{CANARY_ID}/{CANARY_TAIL}",
        f"https://discord.com/api/v9/webhooks/{CANARY_ID}/{CANARY_TAIL}",
        f"https://discord.com/api/webhooks/1/{CANARY_TAIL}",
        f"https://discord.com/api/webhooks/{CANARY_ID}/a_b-C9",
    ],
)
def test_discord_webhook_urls_are_accepted(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv(WEBHOOK, url)

    settings = load_settings()

    assert settings.discord_configured is True
    assert settings.discord_webhook_url is not None
    assert settings.discord_webhook_url.get_secret_value() == url


_PATH: Final = f"/api/webhooks/{CANARY_ID}/{CANARY_TAIL}"
REFUSED_URLS: Final = [
    f"http://discord.com{_PATH}",
    f"ftp://discord.com{_PATH}",
    f"discord.com{_PATH}",
    f"//discord.com{_PATH}",
    f"https://example.com{_PATH}",
    f"https://discord.com.evil.example{_PATH}",
    f"https://evildiscord.com{_PATH}",
    f"https://discord.co{_PATH}",
    f"https://www.discord.com{_PATH}",
    f"https://canary.discordapp.com.evil.example{_PATH}",
    f"https://discord.com@evil.example{_PATH}",
    f"https://user:CANARY-pass@discord.com{_PATH}",
    f"https://user@discord.com{_PATH}",
    f"https://discord.com:8443{_PATH}",
    f"https://discord.com:443{_PATH}",
    f"https://discord.com:{_PATH}",
    f"https://discord.com{_PATH}?wait=true",
    f"https://discord.com{_PATH}?",
    f"https://discord.com{_PATH}#CANARY-fragment",
    f"https://discord.com{_PATH}/",
    f"https://discord.com{_PATH}/slack",
    f"https://discord.com/api/webhooks/{CANARY_ID}",
    f"https://discord.com/api/webhooks/{CANARY_ID}/",
    "https://discord.com/api/webhooks/",
    f"https://discord.com/api/webhooks/not-a-number/{CANARY_TAIL}",
    f"https://discord.com/api/webhooks/-1/{CANARY_TAIL}",
    f"https://discord.com/api/webhooks/١٢٣/{CANARY_TAIL}",
    f"https://discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}.x",
    f"https://discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}%2F",
    f"https://discord.com/api/vX/webhooks/{CANARY_ID}/{CANARY_TAIL}",
    f"https://discord.com/api/v/webhooks/{CANARY_ID}/{CANARY_TAIL}",
    f"https://discord.com/webhooks/{CANARY_ID}/{CANARY_TAIL}",
    f"https://discord.com/API/webhooks/{CANARY_ID}/{CANARY_TAIL}",
    f"https://discord.com/api/../api/webhooks/{CANARY_ID}/{CANARY_TAIL}",
    f" {CANARY_URL}",
    f"{CANARY_URL} ",
    f"{CANARY_URL}\n",
    f"https://disc\tord.com{_PATH}",
    "not a url CANARY",
]


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_other_webhook_urls_are_refused_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(WEBHOOK, url)

    with pytest.raises(SettingsError) as caught:
        load_settings()

    message = str(caught.value)
    assert f"  {WEBHOOK}: must be a Discord webhook URL" in message.splitlines()[1]
    for leaked in (str(caught.value), repr(caught.value), caplog.text):
        assert "CANARY" not in leaked
        assert CANARY_ID not in leaked
        assert "discord.com/" not in leaked
        assert "evil" not in leaked
    # The original pydantic error (it holds the input) is not attached for a traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_a_bad_webhook_url_stops_the_app_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(WEBHOOK, f"https://discord.com.evil.example{_PATH}")

    def never_run(self: object) -> None:
        raise AssertionError("the server must not start")

    monkeypatch.setattr(server_main.AppServer, "run", never_run)

    with pytest.raises(SystemExit) as raised:
        server_main.main()

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert f"{WEBHOOK}: must be a Discord webhook URL" in captured.err
    for text in (captured.err, captured.out, caplog.text):
        assert CANARY_TAIL not in text
        assert CANARY_ID not in text


def test_webhook_url_is_refused_in_code_too() -> None:
    with pytest.raises(ValueError, match="must be a Discord webhook URL") as caught:
        Settings(discord_webhook_url=SecretStr(f"http://discord.com{_PATH}"))
    assert "CANARY" not in str(caught.value)


def test_webhook_url_may_be_given_as_a_secret_in_code() -> None:
    settings = Settings(discord_webhook_url=SecretStr(CANARY_URL))

    assert settings.discord_configured is True


def test_webhook_url_never_shows_in_repr_str_or_dumps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(WEBHOOK, CANARY_URL)

    settings = load_settings()
    logging.getLogger("ccobs").debug("settings: %s %r", settings, settings)

    dumps = (
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        repr(settings.discord_webhook_url),
        str(settings.discord_webhook_url),
        str(asdict(settings.alert_config)),
        caplog.text,
    )
    for text in dumps:
        assert CANARY_TAIL not in text
        assert CANARY_ID not in text


def test_webhook_url_with_other_invalid_settings_reports_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WEBHOOK, f"http://discord.com{_PATH}")
    monkeypatch.setenv("CCOBS_ALERT_COOLDOWN_S", "-1")

    with pytest.raises(SettingsError) as caught:
        load_settings()
    message = str(caught.value)
    assert WEBHOOK in message
    assert "CCOBS_ALERT_COOLDOWN_S" in message
    assert "CANARY" not in message


def test_a_settings_error_built_directly_never_shows_the_input() -> None:
    # load_settings() already words its own errors; this guards any later code that
    # builds Settings from a plain string: pydantic's own error must not echo it either.
    from pydantic import ValidationError

    # A plain str, as an env source passes it (the cast only quiets the annotation).
    bad = cast(SecretStr, f"https://evil.example/api/webhooks/{CANARY_ID}/{CANARY_TAIL}")
    with pytest.raises(ValidationError) as caught:
        Settings(discord_webhook_url=bad)
    assert CANARY_TAIL not in str(caught.value)
    assert CANARY_TAIL not in repr(caught.value)
