"""Alembic migrations of the app's database, packaged inside ``ccobs`` (ADR 0026).

The configuration is built in code: there is no ``alembic.ini`` at runtime, and the
script location is this package's directory, wherever it is installed. The app's
lifespan calls ``upgrade_to_head`` before it accepts requests.

To write a new revision, add ``versions/NNNN_<slug>.py`` by hand (``script.py.mako`` is
the template), with ``down_revision`` set to the current head, and update ``HEAD``.
``tests/server/test_db.py`` checks that the head matches the models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine

from ccobs.server.db.engine import create_engine_for

__all__ = ["HEAD", "MigrationError", "alembic_config", "upgrade_to_head"]

# The latest revision in versions/.
HEAD: Final = "0002"

_SCRIPT_LOCATION: Final = Path(__file__).parent


class MigrationError(RuntimeError):
    """A migration left the database inconsistent; nothing of it was committed."""


def alembic_config(connection: Connection | None) -> Config:
    """An Alembic configuration for this package that runs on ``connection`` (env.py)."""
    config = Config()
    # configparser interpolates %, which a directory name may hold.
    config.set_main_option("script_location", str(_SCRIPT_LOCATION).replace("%", "%%"))
    config.attributes["connection"] = connection
    return config


def upgrade_to_head(engine: Engine) -> None:
    """Bring the database file of ``engine`` to the latest revision, atomically; a no-op
    when it is already there.

    The migration runs on a connection of its own with foreign keys off, set before its
    transaction starts (SQLite ignores the pragma inside one): a batch migration copies a
    table and drops the original, and with foreign keys on, dropping ``sessions`` would
    cascade-delete every child row. Before the commit, ``PRAGMA foreign_key_check`` must
    find no violation, or the whole upgrade is rolled back.
    """
    database = engine.url.database
    if not database or database == ":memory:":
        raise ValueError("migrations need a database file")
    migration_engine = create_engine_for(Path(database), role="writer", foreign_keys=False)
    try:
        with migration_engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                # Counts only: the rows identify data, not the error.
                raise MigrationError(f"the migration left {len(violations)} foreign key violations")
    finally:
        migration_engine.dispose()
