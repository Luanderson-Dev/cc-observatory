"""Run the app: ``python -m ccobs.server`` (the container's command, ADR 0027).

Settings come from ``CCOBS_*`` variables; invalid ones stop the app with exit code 2
and one line per bad variable on stderr.

The server is uvicorn's, with one change (``AppServer``): on shutdown it closes the live
updates hub before uvicorn waits for open connections. uvicorn runs the lifespan's
shutdown only after every connection has closed (or ``timeout_graceful_shutdown`` has
run out and it cancels them), so a stream ended by the lifespan would hold every stop
for the whole timeout (ADR 0032).
"""

from __future__ import annotations

import logging
import socket
import sys
from collections.abc import Callable
from typing import Final

import uvicorn
from fastapi import FastAPI

from ccobs.server.app import create_app
from ccobs.server.config import Settings, SettingsError, load_settings
from ccobs.server.discord import WEBHOOK_URL_FILTER

__all__ = ["AppServer", "build_server", "configure_logging", "load_settings", "main"]

_EXIT_BAD_SETTINGS: Final = 2
_FORMAT: Final = "%(levelname)s:%(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Give the app's loggers (``ccobs.*``) and Alembic's a handler on stderr.

    The root logger is left alone, so no other library logs below its default WARNING:
    SQLAlchemy's engine logs statements and rows at INFO and DEBUG, and rows hold
    ``cwd`` (ADR 0006), so even ``CCOBS_LOG_LEVEL=debug`` must not reach them. Alembic
    is capped at WARNING too, since its INFO lines add nothing an operator needs. uvicorn
    configures its own loggers. The handler drops every record that names a Discord
    webhook path (``WEBHOOK_URL_FILTER``, ADR 0046), whichever logger it comes from: the
    filters on httpx's and httpcore's loggers are the first line, this one catches a
    logger nobody named.
    """
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(WEBHOOK_URL_FILTER)
    for name, logger_level in (("ccobs", level), ("alembic", max(level, logging.WARNING))):
        logger = logging.getLogger(name)
        logger.setLevel(logger_level)
        logger.addHandler(handler)
        # Not up to the root, which has no handler of ours: no line twice.
        logger.propagate = False
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


class AppServer(uvicorn.Server):
    """uvicorn's server, which runs ``before_shutdown`` (on the event loop) before it
    stops accepting connections and waits for the open ones."""

    def __init__(self, config: uvicorn.Config, *, before_shutdown: Callable[[], None]) -> None:
        super().__init__(config)
        self._before_shutdown = before_shutdown

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._before_shutdown()
        await super().shutdown(sockets)


def build_server(settings: Settings, app: FastAPI | None = None) -> AppServer:
    """The server for ``app`` (``create_app(settings)`` by default), configured from the
    settings, which closes the app's live updates hub first when it stops."""
    if app is None:
        app = create_app(settings)
    config = uvicorn.Config(
        app,
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
        # After SIGTERM (and once AppServer has ended the live streams), wait at most 5 s
        # for open requests before the lifespan's shutdown (the background threads
        # together, 5 s, then the writer, 5 s), so the whole stop, 15 s at worst, stays
        # well inside compose's 30 s stop_grace_period (ADRs 0030 and 0046). Without it
        # uvicorn waits for every open connection, however long.
        timeout_graceful_shutdown=5,
    )
    return AppServer(config, before_shutdown=app.state.live.close)


def main() -> None:
    """Read the settings and serve the app until stopped."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        raise SystemExit(_EXIT_BAD_SETTINGS) from None
    configure_logging(settings)
    build_server(settings).run()


if __name__ == "__main__":
    main()
