"""Run the app: ``python -m ccobs.server`` (the container's command, ADR 0027).

Settings come from ``CCOBS_*`` variables; invalid ones stop the app with exit code 2
and one line per bad variable on stderr.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

import uvicorn

from ccobs.server.app import create_app
from ccobs.server.config import Settings, SettingsError, load_settings

__all__ = ["configure_logging", "main"]

_EXIT_BAD_SETTINGS: Final = 2
_FORMAT: Final = "%(levelname)s:%(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Give the app's loggers (``ccobs.*``) and Alembic's a handler on stderr.

    The root logger is left alone, so no other library logs below its default WARNING:
    SQLAlchemy's engine logs statements and rows at INFO and DEBUG, and rows hold
    ``cwd`` (ADR 0006), so even ``CCOBS_LOG_LEVEL=debug`` must not reach them. Alembic
    is capped at WARNING too, since its INFO lines add nothing an operator needs. uvicorn
    configures its own loggers.
    """
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    for name, logger_level in (("ccobs", level), ("alembic", max(level, logging.WARNING))):
        logger = logging.getLogger(name)
        logger.setLevel(logger_level)
        logger.addHandler(handler)
        # Not up to the root, which has no handler of ours: no line twice.
        logger.propagate = False
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


def main() -> None:
    """Read the settings and serve the app until stopped."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        raise SystemExit(_EXIT_BAD_SETTINGS) from None
    configure_logging(settings)
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.port,
        # One process: the hook queue and the writer live in it (ADR 0027).
        workers=1,
        # The access log would record client addresses and request lines.
        access_log=False,
        log_level=settings.log_level,
        # No proxy sits in front of the app; never trust X-Forwarded-* headers.
        proxy_headers=False,
        # Do not advertise the server software and version.
        server_header=False,
        # RFC 9110 (section 6.6.1) asks an origin server with a clock for a Date header;
        # it reveals nothing.
        date_header=True,
    )


if __name__ == "__main__":
    main()
