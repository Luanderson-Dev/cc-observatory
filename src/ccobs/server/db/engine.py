"""The SQLAlchemy engines over the app's SQLite file (ADRs 0005, 0026 and 0033).

The app opens two engines on the same file (ADR 0033): the writer's, with exactly one
connection, for the writer thread (and the start-up reads before it runs), and the
readers', with ``READER_POOL_SIZE`` connections that cannot write (``query_only``), for
the API. Readers can then take every reader connection without ever holding one the
writer needs.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from sqlalchemy import URL, Connection, Engine, create_engine, event, text
from sqlalchemy.pool import ConnectionPoolEntry

__all__ = [
    "BUSY_TIMEOUT_MS",
    "READER_POOL_SIZE",
    "EngineRole",
    "create_engine_for",
    "wal_enabled",
]

_log = logging.getLogger(__name__)

# How long a connection waits for a lock before failing with "database is locked". The
# app has one writer (ADR 0026), so only readers (the API) and a checkpoint can contend.
BUSY_TIMEOUT_MS: Final = 5000

# The writer's engine and the readers' engine (ADR 0033).
type EngineRole = Literal["writer", "reader"]
# Connections of the readers' engine; the read gate (ccobs.server.reads) runs at most
# this many reads at once, so a read never waits for a connection.
READER_POOL_SIZE: Final = 4
# No overflow on either engine: the pool size is the hard cap.
_POOL_SIZES: Final[dict[EngineRole, int]] = {"writer": 1, "reader": READER_POOL_SIZE}
# Set last on a reader connection, after the pragmas above: SQLite then refuses any
# statement that writes (INSERT, UPDATE, DELETE, DDL) with "readonly database".
_QUERY_ONLY: Final = "PRAGMA query_only=ON"

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
    foreign_keys: bool, *, query_only: bool
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
            if query_only:
                cursor.execute(_QUERY_ONLY)
        finally:
            cursor.close()

    return listener


def _on_begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")


def create_engine_for(path: Path, *, role: EngineRole, foreign_keys: bool = True) -> Engine:
    """A sync engine over the SQLite file at ``path``, creating its directory if missing.

    Every transaction is a real SQLite transaction, reads and DDL included. Bound values
    never appear in error messages (``hide_parameters``): a row holds ``cwd``, which is
    personal data. ``role`` sizes the pool (ADR 0033): ``"writer"`` holds one connection,
    ``"reader"`` holds ``READER_POOL_SIZE`` connections set to ``query_only``; neither
    overflows. ``foreign_keys=False`` is for migrations only (``upgrade_to_head``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        hide_parameters=True,
        pool_size=_POOL_SIZES[role],
        max_overflow=0,
    )
    event.listen(engine, "connect", _on_connect(foreign_keys, query_only=role == "reader"))
    event.listen(engine, "begin", _on_begin)
    return engine


def wal_enabled(engine: Engine) -> bool:
    """Whether the database runs in WAL mode, for the healthcheck and the metrics."""
    with engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    return str(mode).lower() == _WAL
