"""The SQLAlchemy engine over the app's SQLite file (ADRs 0005 and 0026)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final

from sqlalchemy import URL, Connection, Engine, create_engine, event, text
from sqlalchemy.pool import ConnectionPoolEntry

__all__ = ["BUSY_TIMEOUT_MS", "create_engine_for", "wal_enabled"]

_log = logging.getLogger(__name__)

# How long a connection waits for a lock before failing with "database is locked". The
# app has one writer (ADR 0026), so only readers (the API) and a checkpoint can contend.
BUSY_TIMEOUT_MS: Final = 5000

# journal_mode=WAL persists in the file, but setting it on every connection also
# converts a database created elsewhere. SQLite answers with the mode it ended up in,
# which is not "wal" where WAL cannot work (some network or shared filesystems).
_JOURNAL_MODE: Final = "PRAGMA journal_mode=WAL"
_WAL: Final = "wal"
# Per connection. synchronous=NORMAL is durable against an app crash in WAL mode; a power
# loss can lose the last commits, never corrupt the file. SQLite leaves foreign keys off
# by default, and the cascades of ADR 0026 need them on.
_PRAGMAS: Final = ("PRAGMA synchronous=NORMAL", f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def _on_connect(
    foreign_keys: bool,
) -> Callable[[sqlite3.Connection, ConnectionPoolEntry], None]:
    def listener(dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
        # pysqlite would send BEGIN only before DML, leaving DDL and reads outside any
        # transaction. Autocommit at the driver level, and _on_begin sends BEGIN itself
        # (SQLAlchemy's pysqlite recipe). It also lets the pragmas below take effect:
        # foreign_keys is a no-op inside a transaction.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            (mode,) = cursor.execute(_JOURNAL_MODE).fetchone()
            if str(mode).lower() != _WAL:
                # No path here: it holds the OS username (ADR 0006).
                _log.warning(
                    "SQLite journal mode is %r, not 'wal': readers will block the writer;"
                    " keep the data directory on a local filesystem (ADR 0005)",
                    mode,
                )
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
            cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        finally:
            cursor.close()

    return listener


def _on_begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")


def create_engine_for(path: Path, *, foreign_keys: bool = True) -> Engine:
    """A sync engine over the SQLite file at ``path``, creating its directory if missing.

    Every transaction is a real SQLite transaction, reads and DDL included. Bound values
    never appear in error messages (``hide_parameters``): a row holds ``cwd``, which is
    personal data. ``foreign_keys=False`` is for migrations only (``upgrade_to_head``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)), hide_parameters=True)
    event.listen(engine, "connect", _on_connect(foreign_keys))
    event.listen(engine, "begin", _on_begin)
    return engine


def wal_enabled(engine: Engine) -> bool:
    """Whether the database runs in WAL mode, for the healthcheck and the metrics."""
    with engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    return str(mode).lower() == _WAL
