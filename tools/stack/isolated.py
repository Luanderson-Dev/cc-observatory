"""Settings of the isolated compose project that the integration tests run.

The project ``ccobs-it`` runs ``compose.yaml`` plus ``compose.debug.yaml`` on a port
block of its own (24xxx, ADR 0018), so it never touches the user's ``cc-observatory``
stack. It is kept apart from the developer's environment:

- every call passes ``--env-file .env.example``, so Compose never loads the
  repository's ``.env``;
- inherited ``CCOBS_*`` and ``COMPOSE_*`` variables are dropped from the subprocess
  environment (a ``COMPOSE_PROJECT_NAME``, ``COMPOSE_FILE`` or a port set in the shell
  would otherwise leak in), and the project's ports are added;
- each port can move through ``CCOBS_IT_<NAME>``, for example
  ``CCOBS_IT_OTLP_HTTP_PORT=34318`` for ``CCOBS_OTLP_HTTP_PORT``, when a 24xxx port is
  taken or falls in a Windows excluded port range.

``started_since`` tells whether a container started after the test session did, by the
Docker daemon's timestamps, so that its start-up lines are among the logs read since.

Developer tool, standard library only; not part of the published package.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

PROJECT: Final = "ccobs-it"
COMPOSE_FILES: Final = ("compose.yaml", "compose.debug.yaml")
# Profiles of optional services: traces runs Tempo (ADR 0003).
PROFILES: Final = ("traces",)
ENV_FILE: Final = ".env.example"
DEFAULT_PORTS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "CCOBS_OTLP_GRPC_PORT": 24317,
        "CCOBS_OTLP_HTTP_PORT": 24318,
        "CCOBS_GRAFANA_PORT": 24330,
        "CCOBS_PROMETHEUS_PORT": 24390,
        "CCOBS_LOKI_PORT": 24310,
        "CCOBS_TEMPO_PORT": 24320,
        "CCOBS_APP_PORT": 24300,
        # Published only by compose.debug.yaml, which the project always applies.
        "CCOBS_APP_METRICS_PORT": 24301,
    }
)
OVERRIDE_PREFIX: Final = "CCOBS_IT_"
KEEP_VARIABLE: Final = "CCOBS_IT_KEEP"
# Inherited variables that would change what Compose runs.
DROPPED_PREFIXES: Final = ("CCOBS_", "COMPOSE_")
CLEANUP_COMMAND: Final = f"docker compose -p {PROJECT} down -v --remove-orphans"
_MAX_PORT: Final = 65535
# A timestamp as the Docker daemon writes it (`docker info`'s SystemTime, `docker
# inspect`'s State.StartedAt) and `docker compose logs --since` reads it: RFC 3339, with
# up to nanoseconds.
RFC3339: Final = re.compile(
    r"(?P<whole>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})"
)
_MICROSECOND_DIGITS: Final = 6


def override_variable(name: str) -> str:
    """The variable that moves the isolated port of ``name``
    (``CCOBS_OTLP_HTTP_PORT`` -> ``CCOBS_IT_OTLP_HTTP_PORT``)."""
    return OVERRIDE_PREFIX + name.removeprefix("CCOBS_")


def ports(environ: Mapping[str, str]) -> dict[str, int]:
    """The host port of each port variable: the 24xxx default or its ``CCOBS_IT_*``
    override. Raises ``ValueError`` on an override that is not a port number, or when two
    variables would share a port."""
    chosen: dict[str, int] = {}
    for name, default in DEFAULT_PORTS.items():
        variable = override_variable(name)
        raw = environ.get(variable)
        if raw is None:
            chosen[name] = default
            continue
        text = raw.strip()
        if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= _MAX_PORT:
            raise ValueError(f"{variable}={raw!r} is not a port number (1 to {_MAX_PORT})")
        chosen[name] = int(text)
    seen: dict[int, str] = {}
    for name, port in chosen.items():
        if port in seen:
            raise ValueError(f"{seen[port]} and {name} would both use port {port}")
        seen[port] = name
    return chosen


def compose_environment(environ: Mapping[str, str], chosen: Mapping[str, int]) -> dict[str, str]:
    """``environ`` without ``CCOBS_*`` and ``COMPOSE_*`` variables (in any case), plus
    the project's ports."""
    kept = {
        key: value for key, value in environ.items() if not key.upper().startswith(DROPPED_PREFIXES)
    }
    return {**kept, **{name: str(port) for name, port in chosen.items()}}


def compose_command(docker: str, root: Path, *args: str) -> list[str]:
    """The ``docker compose`` command line for the project, followed by ``args``."""
    command = [docker, "compose", "--project-name", PROJECT, "--env-file", str(root / ENV_FILE)]
    for name in COMPOSE_FILES:
        command += ["--file", str(root / name)]
    for profile in PROFILES:
        command += ["--profile", profile]
    return [*command, *args]


def _instant(text: str) -> datetime:
    """An RFC 3339 timestamp as an aware datetime, truncated to microseconds."""
    match = RFC3339.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not an RFC 3339 timestamp")
    zone = "+00:00" if match["zone"] == "Z" else match["zone"]
    fraction = (match["fraction"] or "")[:_MICROSECOND_DIGITS].ljust(_MICROSECOND_DIGITS, "0")
    return datetime.fromisoformat(match["whole"] + zone) + timedelta(microseconds=int(fraction))


def started_since(started_at: str, since: str) -> bool:
    """Whether a container that started at ``started_at`` (``docker inspect``'s
    ``State.StartedAt``) started at or after ``since``. Both are RFC 3339 timestamps by
    the Docker daemon's clock, with any zone; a container that never started has
    ``0001-01-01T00:00:00Z``. Raises ``ValueError`` on a timestamp that is not RFC 3339."""
    return _instant(started_at) >= _instant(since)
