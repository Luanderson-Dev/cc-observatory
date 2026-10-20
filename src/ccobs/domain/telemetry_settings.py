"""The telemetry ``env`` block that points Claude Code at the stack.

``telemetry_env`` builds the variables that ``ccobs setup`` (milestone 4) writes into
the ``env`` block of Claude Code's user settings, ``~/.claude/settings.json``:

- telemetry on, metrics and events exported over OTLP/HTTP (``http/protobuf``: Claude
  Code has no default protocol) to the collector on IPv4 loopback (ADR 0018);
- cumulative temporality, which the Prometheus OTLP receiver expects (ADR 0013);
- with ``traces=True``, the beta tracing variables too (README.md, "For traces").

Three more groups are not written by the default block; ``ccobs doctor`` evaluates
each variable's value, not its presence, when one is set elsewhere:

- ``CONTENT_VARIABLES`` make Claude Code export session content: the ``OTEL_LOG_*``
  flags and the detailed beta tracing pair (ADR 0006). ADR 0006's ``--capture-content``
  opt-in, deferred to a later milestone, is the only planned exception;
- ``SESSION_ID_VARIABLE``, whose default (``true``) keeps the series of concurrent
  sessions apart (ADR 0016); only ``false`` is a problem;
- ``PER_SIGNAL_ENDPOINT_VARIABLES`` take precedence over the generic endpoint and
  protocol the block writes (ADR 0036).

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "CONTENT_VARIABLES",
    "DEFAULT_OTLP_ENDPOINT",
    "PER_SIGNAL_ENDPOINT_VARIABLES",
    "SESSION_ID_VARIABLE",
    "TELEMETRY_VARIABLES",
    "TRACES_VARIABLES",
    "telemetry_env",
]

# The collector's OTLP/HTTP port in the stack's port block (ADR 0018). A base endpoint:
# the SDK appends /v1/metrics, /v1/logs and /v1/traces itself.
DEFAULT_OTLP_ENDPOINT: Final = "http://127.0.0.1:4318"

_ENDPOINT_VARIABLE: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
# Claude Code only accepts string values in the env block, so every value is a str.
_BASE: Final[tuple[tuple[str, str | None], ...]] = (
    ("CLAUDE_CODE_ENABLE_TELEMETRY", "1"),
    ("OTEL_METRICS_EXPORTER", "otlp"),
    ("OTEL_LOGS_EXPORTER", "otlp"),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    (_ENDPOINT_VARIABLE, None),  # the endpoint argument
    ("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "cumulative"),
)
_TRACES: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "1"),
    ("OTEL_TRACES_EXPORTER", "otlp"),
)

# The names telemetry_env writes, in order: the base set, then the traces set.
TELEMETRY_VARIABLES: Final = tuple(name for name, _ in _BASE)
TRACES_VARIABLES: Final = tuple(name for name, _ in _TRACES)
# Variables that export session content (Claude Code's settings reference and
# monitoring docs): never written by the block; doctor flags an enabling value.
CONTENT_VARIABLES: Final = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_ASSISTANT_RESPONSES",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_RAW_API_BODIES",
    "ENABLE_BETA_TRACING_DETAILED",
    "BETA_TRACING_ENDPOINT",
)
# Never written by the block: its default (true) keeps concurrent sessions' series apart
# (ADR 0016). Doctor flags it only when it is "false".
SESSION_ID_VARIABLE: Final = "OTEL_METRICS_INCLUDE_SESSION_ID"
# Per-signal OTLP variables override the generic endpoint and protocol the block writes.
# Setup does not write them; doctor warns when one is set elsewhere (ADR 0036).
PER_SIGNAL_ENDPOINT_VARIABLES: Final = (
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
)

_MAX_PORT: Final = 65535
# http to 127.0.0.1 and an optional port without leading zeros; no user info, path
# (not even "/"), query or fragment. Only 127.0.0.1: the stack publishes its ports there
# alone (ADR 0018), localhost may resolve to ::1, and [::1] can never connect.
_ENDPOINT: Final = re.compile(r"http://127\.0\.0\.1(?::(?P<port>[1-9][0-9]{0,4}))?")


def _check_endpoint(endpoint: str) -> str:
    match = _ENDPOINT.fullmatch(endpoint)
    port = match["port"] if match else None
    if match is None or (port is not None and int(port) > _MAX_PORT):
        # The endpoint is not echoed: it came from the user's input.
        raise ValueError(
            "the OTLP endpoint must be http to 127.0.0.1 (not localhost, which may resolve "
            "to ::1) with an optional port, and no path"
        )
    return endpoint


def telemetry_env(endpoint: str = DEFAULT_OTLP_ENDPOINT, *, traces: bool = False) -> dict[str, str]:
    """The ``env`` variables of Claude Code's user settings that export to ``endpoint``.

    ``traces`` appends ``TRACES_VARIABLES``. Raises ``ValueError`` unless ``endpoint``
    is ``http://127.0.0.1`` with an optional port and nothing else. Each call returns a
    new dict, in a fixed order; it never holds a name from ``CONTENT_VARIABLES``,
    ``PER_SIGNAL_ENDPOINT_VARIABLES`` or ``SESSION_ID_VARIABLE``.
    """
    _check_endpoint(endpoint)
    env = {name: endpoint if value is None else value for name, value in _BASE}
    if traces:
        env.update(_TRACES)
    return env
