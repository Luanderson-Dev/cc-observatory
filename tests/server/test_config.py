"""The app's settings: ``CCOBS_*`` environment variables, read once at start-up."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from ccobs.domain.telemetry import DEFAULT_HOST, HOST_VALUE_PATTERN
from ccobs.server.config import Settings, SettingsError, load_settings

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No CCOBS_* variable from the developer's shell, and a cwd with no .env in it.
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


# Defaults.


def test_defaults() -> None:
    settings = load_settings()

    assert settings.host_label == "local"
    # Loopback unless told otherwise; the container sets CCOBS_LISTEN_HOST (stage 13).
    assert settings.listen_host == "127.0.0.1"
    assert settings.port == 4300
    assert settings.metrics_port == 9464
    assert settings.data_dir == Path("/var/lib/ccobs")
    assert settings.retention == timedelta(days=90)
    assert settings.session_stale_after == timedelta(hours=12)
    assert settings.loki_url == "http://loki:3100"
    # Grafana's root URL with the default port (ADR 0017).
    assert settings.grafana_url == "http://localhost:4330"
    assert settings.telemetry_reconcile is True
    assert settings.log_level == "info"
    assert settings.hook_queue_max_items == 10_000
    assert settings.hook_queue_max_bytes == 64 * MIB
    assert settings.hook_body_max_bytes == 8 * MIB


def test_default_host_label_is_the_stack_default() -> None:
    # The app labels its data like the collector does (ADR 0022).
    assert load_settings().host_label == DEFAULT_HOST


def test_database_path_is_in_the_data_dir() -> None:
    assert load_settings().database_path == Path("/var/lib/ccobs/ccobs.db")


def test_database_path_follows_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CCOBS_DATA_DIR", str(tmp_path / "data"))

    assert load_settings().database_path == tmp_path / "data" / "ccobs.db"


# Every field from the environment.


def test_every_field_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = {
        "CCOBS_HOST_LABEL": "work-laptop.01",
        "CCOBS_LISTEN_HOST": "::1",
        "CCOBS_PORT": "8080",
        "CCOBS_METRICS_PORT": "9100",
        "CCOBS_DATA_DIR": str(tmp_path),
        "CCOBS_APP_RETENTION": "30d",
        "CCOBS_SESSION_STALE_AFTER": "2h30m",
        "CCOBS_LOKI_URL": "http://127.0.0.1:4310",
        "CCOBS_PROMETHEUS_URL": "http://127.0.0.1:4390",
        "CCOBS_GRAFANA_URL": "http://localhost:24330",
        "CCOBS_TELEMETRY_RECONCILE": "false",
        "CCOBS_LOG_LEVEL": "debug",
        "CCOBS_HOOK_QUEUE_MAX_ITEMS": "500",
        "CCOBS_HOOK_QUEUE_MAX_BYTES": str(16 * MIB),
        "CCOBS_HOOK_BODY_MAX_BYTES": str(1 * MIB),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = load_settings()

    assert settings.host_label == "work-laptop.01"
    assert settings.listen_host == "::1"
    assert settings.port == 8080
    assert settings.metrics_port == 9100
    assert settings.data_dir == tmp_path
    assert settings.retention == timedelta(days=30)
    assert settings.session_stale_after == timedelta(hours=2, minutes=30)
    assert settings.loki_url == "http://127.0.0.1:4310"
    assert settings.prometheus_url == "http://127.0.0.1:4390"
    assert settings.grafana_url == "http://localhost:24330"
    assert settings.telemetry_reconcile is False
    assert settings.log_level == "debug"
    assert settings.hook_queue_max_items == 500
    assert settings.hook_queue_max_bytes == 16 * MIB
    assert settings.hook_body_max_bytes == 1 * MIB


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_log_levels(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    monkeypatch.setenv("CCOBS_LOG_LEVEL", level)

    assert load_settings().log_level == level


def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_LOG_LEVEL", "verbose")

    with pytest.raises(SettingsError, match="CCOBS_LOG_LEVEL"):
        load_settings()


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("RETENTION", "1d")
    monkeypatch.setenv("APP_RETENTION", "1d")

    settings = load_settings()

    assert settings.port == 4300
    assert settings.retention == timedelta(days=90)


def test_retention_is_read_only_from_its_app_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    # CCOBS_RETENTION would read like a stack-wide setting; the backends have their own.
    monkeypatch.setenv("CCOBS_RETENTION", "1d")

    assert load_settings().retention == timedelta(days=90)


# No implicit .env.


def test_a_dotenv_file_in_the_cwd_is_ignored(tmp_path: Path) -> None:
    # compose passes the environment; the stack's .env holds compose's settings.
    (tmp_path / ".env").write_text(
        "CCOBS_PORT=8080\nCCOBS_HOST_LABEL=from-dotenv\n", encoding="utf-8"
    )

    settings = load_settings()

    assert settings.port == 4300
    assert settings.host_label == "local"


# The host label: the rule of ADR 0022, the same as the collector's.


@pytest.mark.parametrize(
    "value", ["local", "work-laptop", "build_01", "host.example", "A1", "-", "x" * 200]
)
def test_host_label_accepts_letters_digits_dots_underscores_and_dashes(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CCOBS_HOST_LABEL", value)

    assert load_settings().host_label == value


@pytest.mark.parametrize(
    "value",
    ["", " ", "my host", 'a"b', "a\\b", "a\\tb", "host:1", "café", "a/b", "a\nb", "a{b}"],
)
def test_host_label_rejects_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CCOBS_HOST_LABEL", value)

    with pytest.raises(SettingsError, match="CCOBS_HOST_LABEL") as caught:
        load_settings()
    assert "letters, digits" in str(caught.value)


def test_host_label_rule_is_the_domain_pattern() -> None:
    # One pattern for the app, ccobs doctor and the tests of the stack's configs.
    assert HOST_VALUE_PATTERN.pattern == "[A-Za-z0-9._-]+"


# Durations.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1d", timedelta(days=1)),
        ("90d", timedelta(days=90)),
        ("1w", timedelta(weeks=1)),
        ("720h", timedelta(days=30)),
        ("1y", timedelta(days=365)),
        ("100y", timedelta(days=36500)),
    ],
)
def test_retention_accepts_one_day_to_a_hundred_years(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: timedelta
) -> None:
    monkeypatch.setenv("CCOBS_APP_RETENTION", text)

    assert load_settings().retention == expected


def test_retention_under_a_day_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_APP_RETENTION", "23h59m")
    monkeypatch.setenv("CCOBS_SESSION_STALE_AFTER", "1h")

    with pytest.raises(SettingsError, match="CCOBS_APP_RETENTION: must be at least 1d"):
        load_settings()


def test_retention_over_a_hundred_years_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # now - retention would overflow datetime for much longer ones.
    monkeypatch.setenv("CCOBS_APP_RETENTION", "101y")

    with pytest.raises(SettingsError, match="CCOBS_APP_RETENTION: must be at most 100y"):
        load_settings()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1h", timedelta(hours=1)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("12h", timedelta(hours=12)),
        ("2d", timedelta(days=2)),
    ],
)
def test_stale_after_accepts_an_hour_or_more(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: timedelta
) -> None:
    monkeypatch.setenv("CCOBS_SESSION_STALE_AFTER", text)

    assert load_settings().session_stale_after == expected


@pytest.mark.parametrize("text", ["59m59s", "30m", "1s"])
def test_stale_after_under_an_hour_is_refused(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setenv("CCOBS_SESSION_STALE_AFTER", text)

    with pytest.raises(SettingsError) as caught:
        load_settings()
    assert (
        "CCOBS_SESSION_STALE_AFTER: must be at least 1h (ADR 0024):"
        " a shorter one marks live sessions stale"
    ) in str(caught.value)


def test_retention_may_equal_stale_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_APP_RETENTION", "2d")
    monkeypatch.setenv("CCOBS_SESSION_STALE_AFTER", "2d")

    settings = load_settings()

    assert settings.retention == settings.session_stale_after == timedelta(days=2)


def test_retention_must_not_be_shorter_than_stale_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_APP_RETENTION", "1d")
    monkeypatch.setenv("CCOBS_SESSION_STALE_AFTER", "2d")

    with pytest.raises(SettingsError, match="CCOBS_APP_RETENTION") as caught:
        load_settings()
    assert "CCOBS_SESSION_STALE_AFTER" in str(caught.value)


@pytest.mark.parametrize("variable", ["CCOBS_APP_RETENTION", "CCOBS_SESSION_STALE_AFTER"])
@pytest.mark.parametrize("text", ["", "0", "0d", "-1d", "90", "soon", "1.5h", "P90D", "3600"])
def test_durations_are_rejected(monkeypatch: pytest.MonkeyPatch, variable: str, text: str) -> None:
    monkeypatch.setenv(variable, text)

    with pytest.raises(SettingsError, match=variable) as caught:
        load_settings()
    assert "y, w, d, h, m, s or ms" in str(caught.value) or "positive" in str(caught.value)


def test_durations_may_be_given_as_timedeltas() -> None:
    settings = Settings.model_validate(
        {"CCOBS_APP_RETENTION": timedelta(days=7), "session_stale_after": timedelta(hours=1)}
    )

    assert settings.retention == timedelta(days=7)
    assert settings.session_stale_after == timedelta(hours=1)


@pytest.mark.parametrize("key", ["CCOBS_APP_RETENTION", "session_stale_after"])
@pytest.mark.parametrize("value", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_timedeltas_are_rejected(key: str, value: timedelta) -> None:
    with pytest.raises(ValueError, match="must be at least"):
        Settings.model_validate({key: value})


# Loki's URL.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://loki:3100", "http://loki:3100"),
        ("http://127.0.0.1:4310", "http://127.0.0.1:4310"),
        ("https://loki.internal", "https://loki.internal"),
        ("http://[::1]:3100", "http://[::1]:3100"),
        ("HTTP://loki:3100", "HTTP://loki:3100"),
        # A path prefix (a reverse proxy) is kept; trailing slashes are stripped, so that
        # API paths are appended as f"{loki_url}/loki/api/v1/...".
        ("http://loki:3100/", "http://loki:3100"),
        ("http://proxy/loki//", "http://proxy/loki"),
    ],
)
def test_loki_url_accepts_http_and_https(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("CCOBS_LOKI_URL", value)

    assert load_settings().loki_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "loki:3100",
        "//loki:3100",
        "ftp://loki:3100",
        "file:///etc/passwd",
        "http://",
        "http://:3100",
        "http:///loki",
        "http://user:secret@loki:3100",
        "http://user@loki:3100",
        "http://loki:3100?x=1",
        "http://loki:3100/?",
        "http://loki:3100#top",
        "http://loki:99999",
        "http://loki:port",
        "http://lo ki:3100",
        " http://loki:3100",
        "http://loki:3100\n",
    ],
)
def test_loki_url_rejects_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CCOBS_LOKI_URL", value)

    with pytest.raises(SettingsError, match="CCOBS_LOKI_URL") as caught:
        load_settings()
    assert "http or https URL" in str(caught.value)


def test_loki_url_error_does_not_echo_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # The value may hold a password; the error goes to the logs.
    monkeypatch.setenv("CCOBS_LOKI_URL", "http://user:hunter2@loki:3100")

    with pytest.raises(SettingsError, match="CCOBS_LOKI_URL") as caught:
        load_settings()
    assert "hunter2" not in str(caught.value)


# Prometheus's URL, for the session page's tokens and cost (ADR 0035): checked like Loki's.


def test_prometheus_url_defaults_to_the_compose_service() -> None:
    assert Settings.model_fields["prometheus_url"].default == "http://prometheus:9090"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://prometheus:9090", "http://prometheus:9090"),
        ("https://prometheus.internal/", "https://prometheus.internal"),
        ("http://[::1]:9090//", "http://[::1]:9090"),
        ("http://proxy/prometheus/", "http://proxy/prometheus"),
    ],
)
def test_prometheus_url_accepts_http_and_https(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("CCOBS_PROMETHEUS_URL", value)

    assert load_settings().prometheus_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "prometheus:9090",
        "ftp://prometheus:9090",
        "http://",
        "http://user:hunter2@prometheus:9090",
        "http://prometheus:9090?x=1",
        "http://prometheus:9090#top",
        "http://prometheus:99999",
        "http://prometheus:9090\n",
    ],
)
def test_prometheus_url_rejects_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CCOBS_PROMETHEUS_URL", value)

    with pytest.raises(SettingsError, match="CCOBS_PROMETHEUS_URL") as caught:
        load_settings()
    assert "http or https URL" in str(caught.value)
    assert "hunter2" not in str(caught.value)


# Grafana's URL, for the UI's links: the browser opens it, and Grafana answers only on
# localhost (ADR 0017).


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:4330", "http://localhost:4330"),
        ("http://localhost:24330/", "http://localhost:24330"),
        ("https://localhost", "https://localhost"),
        ("HTTP://LOCALHOST:4330", "HTTP://LOCALHOST:4330"),
        ("http://localhost:4330/grafana//", "http://localhost:4330/grafana"),
    ],
)
def test_grafana_url_accepts_http_and_https_on_localhost(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("CCOBS_GRAFANA_URL", value)

    assert load_settings().grafana_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost:4330",
        "//localhost:4330",
        "ftp://localhost:4330",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://",
        "http://:4330",
        # Grafana redirects every other host name to localhost (enforce_domain).
        "http://127.0.0.1:4330",
        "http://[::1]:4330",
        "http://grafana:3000",
        "http://localhost.example.com:4330",
        "http://example.com/localhost",
        "http://user@localhost:4330",
        "http://localhost@example.com",
        "http://localhost:4330?orgId=2",
        "http://localhost:4330#top",
        "http://localhost:99999",
        "http://localhost:65536",
        "http://localhost:0",
        "http://localhost:",
        "http://localhost:/grafana",
        "http://localhost:port",
        " http://localhost:4330",
        "http://localhost:4330\n",
        'http://localhost:4330/"onmouseover=x',
        "http://localhost:4330/<script>",
    ],
)
def test_grafana_url_rejects_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CCOBS_GRAFANA_URL", value)

    with pytest.raises(SettingsError, match="CCOBS_GRAFANA_URL") as caught:
        load_settings()
    assert "localhost" in str(caught.value)


# Ports and sizes: positive and bounded.


@pytest.mark.parametrize("variable", ["CCOBS_PORT", "CCOBS_METRICS_PORT"])
@pytest.mark.parametrize("value", ["0", "-1", "65536", "http", "1.5"])
def test_ports_are_valid_tcp_ports(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(SettingsError, match=variable):
        load_settings()


@pytest.mark.parametrize(
    ("variable", "field"), [("CCOBS_PORT", "port"), ("CCOBS_METRICS_PORT", "metrics_port")]
)
@pytest.mark.parametrize("value", [1, 65535])
def test_port_bounds_are_inclusive(
    monkeypatch: pytest.MonkeyPatch, variable: str, field: str, value: int
) -> None:
    monkeypatch.setenv(variable, str(value))

    assert getattr(load_settings(), field) == value


def test_app_and_metrics_ports_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_METRICS_PORT", "4300")

    with pytest.raises(SettingsError, match="CCOBS_PORT") as caught:
        load_settings()
    assert "CCOBS_METRICS_PORT" in str(caught.value)


@pytest.mark.parametrize(
    ("variable", "low", "high"),
    [
        ("CCOBS_HOOK_QUEUE_MAX_ITEMS", 1, 1_000_000),
        ("CCOBS_HOOK_QUEUE_MAX_BYTES", MIB, 1024 * MIB),
        ("CCOBS_HOOK_BODY_MAX_BYTES", 64 * 1024, 64 * MIB),
    ],
)
def test_queue_and_body_sizes_are_bounded(
    monkeypatch: pytest.MonkeyPatch, variable: str, low: int, high: int
) -> None:
    # The body limit must fit in the queue: keep the queue at its largest while testing.
    if variable != "CCOBS_HOOK_QUEUE_MAX_BYTES":
        monkeypatch.setenv("CCOBS_HOOK_QUEUE_MAX_BYTES", str(1024 * MIB))
    else:
        monkeypatch.setenv("CCOBS_HOOK_BODY_MAX_BYTES", str(64 * 1024))

    for accepted in (low, high):
        monkeypatch.setenv(variable, str(accepted))
        load_settings()

    for rejected in (low - 1, high + 1, 0, -1):
        monkeypatch.setenv(variable, str(rejected))
        with pytest.raises(SettingsError, match=variable) as caught:
            load_settings()
        assert f"between {low} and {high}" in str(caught.value)


def test_a_body_must_fit_in_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_HOOK_QUEUE_MAX_BYTES", str(4 * MIB))
    monkeypatch.setenv("CCOBS_HOOK_BODY_MAX_BYTES", str(8 * MIB))

    with pytest.raises(SettingsError, match="CCOBS_HOOK_BODY_MAX_BYTES") as caught:
        load_settings()
    assert "CCOBS_HOOK_QUEUE_MAX_BYTES" in str(caught.value)


def test_sizes_must_be_integers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_HOOK_BODY_MAX_BYTES", "8MiB")

    with pytest.raises(SettingsError, match="CCOBS_HOOK_BODY_MAX_BYTES"):
        load_settings()


# Errors.


def test_every_invalid_variable_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCOBS_HOST_LABEL", "bad label")
    monkeypatch.setenv("CCOBS_APP_RETENTION", "forever")
    monkeypatch.setenv("CCOBS_PORT", "http")

    with pytest.raises(SettingsError) as caught:
        load_settings()
    message = str(caught.value)
    for variable in ("CCOBS_HOST_LABEL", "CCOBS_APP_RETENTION", "CCOBS_PORT"):
        assert variable in message
    assert message.startswith("invalid cc-observatory settings")
    # One line per variable, named once, and no pydantic boilerplate.
    assert "validation error" not in message
    assert "errors.pydantic.dev" not in message


def test_settings_error_is_a_value_error() -> None:
    assert issubclass(SettingsError, ValueError)


def test_settings_are_frozen() -> None:
    settings = load_settings()

    with pytest.raises(ValueError, match="frozen"):
        settings.port = 1


# The app as the browser opens it, for the links in Discord messages (ADR 0046).


def test_app_url_defaults_to_the_published_port_on_localhost() -> None:
    assert load_settings().app_url == "http://localhost:4300"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:4300", "http://localhost:4300"),
        ("http://localhost:24300/", "http://localhost:24300"),
        ("http://127.0.0.1:4300", "http://127.0.0.1:4300"),
        ("http://[::1]:4300", "http://[::1]:4300"),
        ("http://localhost", "http://localhost"),
        ("http://localhost:65535", "http://localhost:65535"),
    ],
)
def test_app_url_accepts_http_on_the_hosts_the_app_answers(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("CCOBS_APP_URL", value)

    assert load_settings().app_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost:4300",
        "https://localhost:4300",
        "http://example.com:4300",
        "http://localhost.example.com:4300",
        "http://user@localhost:4300",
        "http://localhost:4300/path",
        "http://localhost:4300?x=1",
        "http://localhost:4300#top",
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost:",
        "http://localhost:04300",
        " http://localhost:4300",
        "http://localhost:4300\n",
        # Non-ASCII digits (fullwidth), which a bare \d would take.
        "http://localhost:\uff14\uff13\uff10\uff10",
    ],
)
def test_app_url_rejects_anything_else_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CCOBS_APP_URL", value)

    with pytest.raises(SettingsError, match="CCOBS_APP_URL") as caught:
        load_settings()
    assert "localhost" in str(caught.value)
    if value.strip():
        assert value.strip() not in str(caught.value)
