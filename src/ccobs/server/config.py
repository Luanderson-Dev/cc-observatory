"""The app's settings, read once at start-up from ``CCOBS_*`` environment variables.

compose passes the environment to the container; nothing reads a ``.env`` file here. The
stack's ``.env`` holds compose's settings, and reading it from whatever directory the
app starts in would make the result depend on the working directory. An invalid value
stops the app at start-up with a ``SettingsError`` that names every bad variable.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccobs.domain.durations import parse_duration
from ccobs.domain.telemetry import DEFAULT_HOST, HOST_VALUE_PATTERN

__all__ = ["DATABASE_FILE", "Settings", "SettingsError", "load_settings"]

ENV_PREFIX: Final = "CCOBS_"
# The SQLite file inside data_dir (ADR 0005).
DATABASE_FILE: Final = "ccobs.db"

_KIB: Final = 1024
_MIB: Final = 1024 * _KIB

# Inclusive bounds of the integer settings. Each one is positive, and capped so that a
# typo (an extra zero or two) cannot make the app reserve an unreasonable share of a
# laptop's memory.
_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "port": (1, 65535),
    "metrics_port": (1, 65535),
    "hook_queue_max_items": (1, 1_000_000),
    "hook_queue_max_bytes": (_MIB, 1024 * _MIB),
    "hook_body_max_bytes": (64 * _KIB, 64 * _MIB),
}
# Bounds of the durations. Retention: at least a day, and at most 100 years (365-day
# years, as in Prometheus's units), far below the point where now - retention leaves
# datetime's range. Stale sessions: at least an hour (ADR 0024), since a session can sit
# silent that long while a person reads or thinks.
_MIN_RETENTION: Final = timedelta(days=1)
_MAX_RETENTION: Final = timedelta(days=100 * 365)
_MIN_STALE_AFTER: Final = timedelta(hours=1)
# Grafana's only host name (ADR 0017), and the characters its URL may hold: unreserved
# ones, ":" and "/" for the scheme, port and path, and "%" for an encoded path.
_GRAFANA_HOST: Final = "localhost"
_GRAFANA_URL_CHARACTERS: Final = re.compile(r"[A-Za-z0-9._~:/%-]+")
_MAX_PORT: Final = 65535


class SettingsError(ValueError):
    """The environment holds invalid settings; the message names every bad variable."""


class Settings(BaseSettings):
    """The app's configuration. Each field reads ``CCOBS_<FIELD>`` unless noted."""

    # env_file=None: no .env, see the module's docstring. Frozen: settings are read once.
    # No validate_by_name: with it, pydantic-settings would also read CCOBS_RETENTION.
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_file=None, frozen=True)

    # The host label on the app's /metrics series and database rows (ADR 0022): the
    # variable the collector reads, with the same default and the same allowed values.
    host_label: str = DEFAULT_HOST
    # Loopback by default, safe when the app runs directly on a machine. The container
    # image sets CCOBS_LISTEN_HOST to all interfaces (stage 13 of milestone 2, in the
    # Dockerfile or compose's environment): there the app must listen on the container's
    # own network interface, or neither Docker's port publishing nor Prometheus (over the
    # compose network) could reach it. That is not exposure: compose publishes the app
    # on 127.0.0.1 only (ADR 0018), and the metrics port not at all.
    listen_host: str = "127.0.0.1"
    # The app's port inside the container (ADR 0018); compose maps CCOBS_APP_PORT to it.
    port: int = 4300
    # The /metrics listener that Prometheus scrapes over the compose network; never
    # published. 9464 is the usual Prometheus exporter port of OpenTelemetry SDKs.
    metrics_port: int = 9464
    # Holds the SQLite database (database_path); a named volume in compose.
    data_dir: Path = Path("/var/lib/ccobs")
    # How long the app keeps database rows (ADR 0005). Prometheus keeps metrics 90 days
    # by default (ADR 0016), so the app keeps sessions as long as their cost history.
    # Not CCOBS_RETENTION, which would read like a setting of the whole stack. From 1d
    # to 100y (_MIN_RETENTION, _MAX_RETENTION), and never shorter than
    # session_stale_after, or rows of sessions still open would be deleted.
    retention: timedelta = Field(
        default=timedelta(days=90), validation_alias=f"{ENV_PREFIX}APP_RETENTION"
    )
    # A session or agent silent this long becomes stale (ADR 0024, mark_stale): long
    # enough for a session left open over a working day, short enough that the live
    # gauges stop counting a session whose SessionEnd never came. At least 1h.
    session_stale_after: timedelta = timedelta(hours=12)
    # Loki's HTTP API on the compose network, for telemetry reconciliation: http or
    # https, with a host and no credentials, query or fragment. Trailing slashes are
    # stripped, so that API paths are appended as f"{loki_url}/loki/api/v1/...".
    loki_url: str = "http://loki:3100"
    # Grafana as the browser opens it, for the UI's links (ADR 0017): http or https on
    # the host name localhost, since Grafana redirects every other one (127.0.0.1
    # included) to its root URL, with an optional port and path and no user, query or
    # fragment. Trailing slashes are stripped. compose derives it from
    # CCOBS_GRAFANA_PORT, as Grafana's own root URL.
    grafana_url: str = "http://localhost:4330"
    # Whether the app reads Claude Code's events back from Loki to complete hook data.
    telemetry_reconcile: bool = True
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # The hook queue between the endpoint and the background writer: a full queue drops
    # the event (and still answers an empty 2xx), so Claude Code never waits on the
    # database. 10,000 events is minutes of the busiest sessions seen in the fixtures,
    # far more than a writer that is merely slow falls behind by.
    hook_queue_max_items: int = 10_000
    # The same queue's bound in bytes, so that a burst of large payloads cannot grow the
    # app's memory without limit: 64 MiB, room for 8 bodies at the largest size below.
    hook_queue_max_bytes: int = 64 * _MIB
    # The largest hook body read. Bodies carry tool inputs and outputs (a file read, a
    # long command output), which can reach megabytes; 8 MiB keeps every realistic one
    # while refusing pathological ones before they are parsed.
    hook_body_max_bytes: int = 8 * _MIB

    @field_validator("host_label")
    @classmethod
    def _check_host_label(cls, value: str) -> str:
        if not HOST_VALUE_PATTERN.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a host label: use letters, digits, '.', '_' and '-' only"
            )
        return value

    @field_validator("retention", "session_stale_after", mode="before")
    @classmethod
    def _parse_duration(cls, value: object) -> object:
        # The environment gives strings in Prometheus's units; code may pass timedeltas.
        return parse_duration(value) if isinstance(value, str) else value

    @field_validator("retention")
    @classmethod
    def _check_retention(cls, value: timedelta) -> timedelta:
        if value < _MIN_RETENTION:
            raise ValueError("must be at least 1d")
        if value > _MAX_RETENTION:
            # now - retention must stay a valid datetime.
            raise ValueError("must be at most 100y")
        return value

    @field_validator("session_stale_after")
    @classmethod
    def _check_stale_after(cls, value: timedelta) -> timedelta:
        if value < _MIN_STALE_AFTER:
            raise ValueError(
                "must be at least 1h (ADR 0024): a shorter one marks live sessions stale"
            )
        return value

    @field_validator("loki_url")
    @classmethod
    def _check_loki_url(cls, value: str) -> str:
        # The value is never echoed: it may hold a password, and the error is logged.
        # Whitespace is checked first: urlsplit silently drops tabs and newlines.
        error = "must be an http or https URL with a host and no user, query or fragment"
        if any(char.isspace() for char in value) or "?" in value or "#" in value:
            raise ValueError(error)
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError(error)
        if "@" in parts.netloc:
            raise ValueError(error)
        try:
            _ = parts.port
        except ValueError:
            raise ValueError(error) from None
        return value.rstrip("/")

    @field_validator("grafana_url")
    @classmethod
    def _check_grafana_url(cls, value: str) -> str:
        # Only URL characters that need no escaping in HTML, so the value is plain in an
        # href; whitespace, quotes, "<", ">", "?", "#" and "@" are refused before
        # urlsplit, which silently drops tabs and newlines.
        error = (
            "must be an http or https URL on localhost (ADR 0017: Grafana answers no other"
            " host name), with no user, query or fragment"
        )
        if not _GRAFANA_URL_CHARACTERS.fullmatch(value):
            raise ValueError(error)
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or parts.hostname != _GRAFANA_HOST:
            raise ValueError(error)
        try:
            port = parts.port
        except ValueError:
            raise ValueError(error) from None
        # No port, or one from 1 to 65535: urlsplit reads "localhost:" as no port and
        # accepts port 0.
        if parts.netloc.endswith(":") or (port is not None and not 1 <= port <= _MAX_PORT):
            raise ValueError(error)
        return value.rstrip("/")

    @field_validator(*_BOUNDS)
    @classmethod
    def _check_bounds(cls, value: int, info: ValidationInfo) -> int:
        assert info.field_name is not None  # noqa: S101 - always set for field validators
        low, high = _BOUNDS[info.field_name]
        if not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}, got {value}")
        return value

    @model_validator(mode="after")
    def _check_together(self) -> Self:
        if self.port == self.metrics_port:
            raise ValueError(
                f"{_variable('port')} and {_variable('metrics_port')} must differ,"
                f" both are {self.port}"
            )
        if self.retention < self.session_stale_after:
            raise ValueError(
                f"{_variable('retention')} ({self.retention}) must not be shorter than"
                f" {_variable('session_stale_after')} ({self.session_stale_after}):"
                " rows of sessions still open would be deleted"
            )
        if self.hook_body_max_bytes > self.hook_queue_max_bytes:
            raise ValueError(
                f"{_variable('hook_body_max_bytes')} ({self.hook_body_max_bytes}) must not"
                f" exceed {_variable('hook_queue_max_bytes')} ({self.hook_queue_max_bytes}):"
                " the largest body must fit in the queue"
            )
        return self

    @property
    def database_path(self) -> Path:
        """The SQLite database file."""
        return self.data_dir / DATABASE_FILE


# The environment variable of each field: its alias when it has one, else the prefixed
# name.
_VARIABLES: Final = {
    name: field.validation_alias
    if isinstance(field.validation_alias, str)
    else f"{ENV_PREFIX}{name.upper()}"
    for name, field in Settings.model_fields.items()
}


def _variable(key: str | int) -> str:
    """The environment variable behind a field name or alias in an error's location.

    An error on an aliased field is located at its alias, the variable itself.
    """
    return _VARIABLES.get(str(key), str(key))


def load_settings() -> Settings:
    """Read the settings from the environment.

    Raises ``SettingsError`` with one line per invalid variable, named as the user sets
    it, instead of pydantic's report, which names fields.
    """
    try:
        return Settings()
    except ValidationError as error:
        lines = []
        for item in error.errors(include_url=False):
            message = item["msg"].removeprefix("Value error, ")
            location = item["loc"]
            lines.append(f"  {_variable(location[0])}: {message}" if location else f"  {message}")
        raise SettingsError("invalid cc-observatory settings:\n" + "\n".join(lines)) from None
