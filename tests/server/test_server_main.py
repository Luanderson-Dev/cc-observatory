"""``python -m ccobs.server``: read the settings, then run uvicorn (ADR 0027)."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from ccobs.server import __main__ as server_main
from ccobs.server import app as server_app


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


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)
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
    # After SIGTERM uvicorn waits at most this long for open requests, then runs the
    # lifespan's shutdown: the reconciler, the sweeper and the writer get 5 s each. The
    # sum stays well inside compose's 30 s stop_grace_period (ADR 0030), so Docker never
    # has to kill the app (exit 137) with hooks still queued.
    server_main.main()

    ((_, kwargs),) = runs
    budget = (
        kwargs["timeout_graceful_shutdown"]
        + server_app.RECONCILER_STOP_TIMEOUT_S
        + server_app.SWEEPER_STOP_TIMEOUT_S
        + server_app.WRITER_STOP_TIMEOUT_S
    )
    assert kwargs["timeout_graceful_shutdown"] == 5
    assert budget <= 30 * 2 / 3


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
