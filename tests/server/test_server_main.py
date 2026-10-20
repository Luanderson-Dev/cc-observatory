"""``python -m ccobs.server``: read the settings, then run uvicorn (ADR 0027)."""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import os
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from ccobs.server import __main__ as server_main
from ccobs.server import app as server_app
from ccobs.server.discord import WEBHOOK_URL_FILTER, hide_webhook_urls


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CCOBS_DATA_DIR", str(tmp_path / "data"))


_LOGGERS = ("", "ccobs", "ccobs.server", "alembic", "sqlalchemy", "sqlalchemy.engine.Engine")


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    """A fresh process's logging state (root at WARNING, no handlers, nothing set on the
    named loggers), restored afterwards so that main() leaves nothing to other tests."""
    saved = []
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, list(logger.handlers), logger.propagate))
        logger.handlers.clear()
        logger.setLevel(logging.WARNING if name == "" else logging.NOTSET)
        logger.propagate = True
    yield
    for logger, level, handlers, propagate in saved:
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate


# The uvicorn settings main() passes, read back from the server's config.
CONFIG_FIELDS = (
    "host",
    "port",
    "workers",
    "access_log",
    "log_level",
    "proxy_headers",
    "server_header",
    "date_header",
    "timeout_graceful_shutdown",
)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(self: uvicorn.Server, sockets: Any = None) -> None:
        assert isinstance(self, server_main.AppServer)
        config = self.config
        calls.append((config.app, {name: getattr(config, name) for name in CONFIG_FIELDS}))

    monkeypatch.setattr(server_main.AppServer, "run", fake_run)
    return calls


def test_bad_settings_exit_2_with_the_message_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runs: list[tuple[Any, dict[str, Any]]],
) -> None:
    monkeypatch.setenv("CCOBS_PORT", "0")

    with pytest.raises(SystemExit) as raised:
        server_main.main()

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "invalid cc-observatory settings" in captured.err
    assert "CCOBS_PORT" in captured.err
    assert captured.out == ""
    assert runs == []


def test_good_settings_run_one_uvicorn_worker(
    monkeypatch: pytest.MonkeyPatch, runs: list[tuple[Any, dict[str, Any]]], tmp_path: Path
) -> None:
    monkeypatch.setenv("CCOBS_PORT", "4301")
    monkeypatch.setenv("CCOBS_LOG_LEVEL", "warning")

    server_main.main()

    ((app, kwargs),) = runs
    assert isinstance(app, FastAPI)
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 4301,
        "workers": 1,
        "access_log": False,
        "log_level": "warning",
        "proxy_headers": False,
        "server_header": False,
        "date_header": True,
        "timeout_graceful_shutdown": 5,
    }
    # The app is built, not started: no database yet.
    assert not (tmp_path / "data" / "ccobs.db").exists()


def test_graceful_shutdown_fits_the_compose_stop_grace_period(
    runs: list[tuple[Any, dict[str, Any]]],
) -> None:
    # After SIGTERM the server ends the UI's live streams, then uvicorn waits at most this
    # long for open requests, then runs the lifespan's shutdown: the background threads
    # (reconciler, Discord sender, cost reader, alert ticker, sweeper) are stopped
    # together against one deadline, then the writer drains the queue with a budget of
    # its own. The worst case (every thread stuck, say a Discord POST and a cost query at
    # once) stays well inside compose's 30 s stop_grace_period (ADR 0030, ADR 0046), so
    # Docker never has to kill the app (exit 137) with hooks still queued.
    server_main.main()

    ((_, kwargs),) = runs
    timeouts = {
        name: value for name, value in vars(server_app).items() if name.endswith("_STOP_TIMEOUT_S")
    }
    writer = timeouts.pop("WRITER_STOP_TIMEOUT_S")
    # Every other stop timeout belongs to a thread of the group stopped together
    # (test_app_shutdown checks that the lifespan signals them all before it joins any);
    # a new one fails here until it is placed in the group or the budget.
    together = timeouts
    assert set(together) == {
        "RECONCILER_STOP_TIMEOUT_S",
        "DISCORD_SENDER_STOP_TIMEOUT_S",
        "COST_READER_STOP_TIMEOUT_S",
        "ALERT_TICKER_STOP_TIMEOUT_S",
        "SWEEPER_STOP_TIMEOUT_S",
    }
    worst_case = kwargs["timeout_graceful_shutdown"] + max(together.values()) + writer
    assert kwargs["timeout_graceful_shutdown"] == 5
    assert worst_case == 15
    assert worst_case <= 30 * 2 / 3


def test_listen_host_comes_from_the_settings(
    monkeypatch: pytest.MonkeyPatch, runs: list[tuple[Any, dict[str, Any]]]
) -> None:
    monkeypatch.setenv("CCOBS_LISTEN_HOST", "::1")

    server_main.main()

    ((_, kwargs),) = runs
    assert kwargs["host"] == "::1"


# Logging: the app's loggers only, never SQLAlchemy's statements and rows.


@pytest.mark.parametrize("level", ["debug", "info"])
def test_sqlalchemy_never_logs_below_warning(
    monkeypatch: pytest.MonkeyPatch, runs: list[tuple[Any, dict[str, Any]]], level: str
) -> None:
    monkeypatch.setenv("CCOBS_LOG_LEVEL", level)
    root = logging.getLogger()
    root_handlers = list(root.handlers)

    server_main.main()

    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("alembic").getEffectiveLevel() >= logging.WARNING
    # The root logger is left as it was.
    assert root.level == logging.WARNING
    assert root.handlers == root_handlers


@pytest.mark.parametrize(
    ("level", "expected"), [("debug", logging.DEBUG), ("error", logging.ERROR)]
)
def test_the_app_loggers_follow_the_log_level(
    monkeypatch: pytest.MonkeyPatch,
    runs: list[tuple[Any, dict[str, Any]]],
    level: str,
    expected: int,
) -> None:
    monkeypatch.setenv("CCOBS_LOG_LEVEL", level)

    server_main.main()

    app_logger = logging.getLogger("ccobs")
    assert logging.getLogger("ccobs.server").getEffectiveLevel() == expected
    assert len(app_logger.handlers) == 1
    assert not app_logger.propagate
    assert logging.getLogger("alembic").level == max(expected, logging.WARNING)


def test_the_server_closes_the_live_streams_before_uvicorn_waits_for_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    app = FastAPI()

    class Hub:
        def close(self) -> None:
            order.append("close streams")

    app.state.live = Hub()

    async def shutdown(self: uvicorn.Server, sockets: Any = None) -> None:
        order.append("uvicorn shutdown")

    monkeypatch.setattr(uvicorn.Server, "shutdown", shutdown)
    settings = server_main.load_settings()
    server = server_main.build_server(settings, app)

    asyncio.run(server.shutdown())

    assert order == ["close streams", "uvicorn shutdown"]


# The webhook URL never reaches a log line (ADR 0046).

WEBHOOK_URL = "https://discord.invalid/api/webhooks/123/CANARY-not-a-real-token"


def _import_every_module(package: str) -> None:
    """Import ``package`` and every submodule whose dependencies are installed, so each
    logger they define exists."""
    root = importlib.import_module(package)
    for module in pkgutil.walk_packages(root.__path__, f"{package}."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            continue  # an optional dependency (h2, socksio, trio, click) is missing


def test_every_httpx_and_httpcore_logger_drops_webhook_records() -> None:
    # A future httpx or httpcore that adds a logger fails here until it is named.
    _import_every_module("httpx")
    _import_every_module("httpcore")
    hide_webhook_urls()

    names = [
        name
        for name in logging.root.manager.loggerDict
        if name.split(".")[0] in ("httpx", "httpcore")
    ]
    assert {"httpx", "httpcore", "httpcore.connection", "httpcore.http11"} <= set(names)
    for name in names:
        assert WEBHOOK_URL_FILTER in logging.getLogger(name).filters, name


def test_the_app_handler_drops_webhook_records_from_any_logger(
    runs: list[tuple[Any, dict[str, Any]]],
) -> None:
    server_main.main()
    (handler,) = logging.getLogger("ccobs").handlers
    assert isinstance(handler, logging.StreamHandler)
    assert WEBHOOK_URL_FILTER in handler.filters
    stream = io.StringIO()
    handler.setStream(stream)

    # A logger nobody put a filter on, whose records reach the app's handler.
    fresh = logging.getLogger("ccobs.some.future.module")
    fresh.warning("POST %s", WEBHOOK_URL)
    fresh.warning("kept line")
    # Any logger's record, handed to the handler directly.
    record = logging.LogRecord(
        "somelib.client", logging.ERROR, __file__, 1, "GET %s", (WEBHOOK_URL,), None
    )
    handler.handle(record)

    assert "CANARY" not in stream.getvalue()
    assert "/webhooks/" not in stream.getvalue()
    assert "kept line" in stream.getvalue()
