"""The app's SQLite database: engine pragmas, migrations and the schema (ADRs 0005, 0026).

Every test uses a database file under ``tmp_path``, not ``:memory:``, so that WAL applies
as it does in the app.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import zipfile
from collections.abc import Iterable, Iterator
from importlib import resources
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import QueuePool

from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import apply
from ccobs.server import migrations
from ccobs.server.db import engine as db_engine
from ccobs.server.db.engine import (
    BUSY_TIMEOUT_MS,
    READER_POOL_SIZE,
    create_engine_for,
    wal_enabled,
)
from ccobs.server.db.models import Base
from ccobs.server.db.repository import Repository, hook_event_row
from ccobs.server.migrations import HEAD, MigrationError, alembic_config, upgrade_to_head

ROOT: Final = Path(__file__).parents[2]

# Every column of every table. Adding a column fails this test until the test and an
# ADR are updated: a column is a decision about what the app stores (ADRs 0023, 0026
# and, for the alert tables, 0042).
COLUMNS: Final[dict[str, set[str]]] = {
    "sessions": {
        "session_id",
        "host",
        "status",
        "started_at_ms",
        "last_event_at_ms",
        "last_seq",
        "turn_open",
        "compacting",
        "source",
        "model",
        "permission_mode",
        "effort_level",
        "last_prompt_id",
        "last_error_code",
        "idle_notified_at_ms",
        "ended_at_ms",
        "end_reason",
        "cwd",
    },
    "agents": {
        "session_id",
        "agent_id",
        "host",
        "status",
        "agent_type",
        "internal",
        "background",
        "tool_use_id",
        "started_at_ms",
        "stopped_at_ms",
        "last_event_at_ms",
        "stop_reason",
    },
    "blocks": {
        "session_id",
        "block_id",
        "host",
        "kind",
        "scope_agent_id",
        "tool_name",
        "tool_use_id",
        "mcp_server_name",
        "elicitation_id",
        "opened_at_ms",
        "notified_at_ms",
        "closed_at_ms",
        "close_reason",
        "close_source",
    },
    "hook_events": {
        "seq",
        "host",
        "session_id",
        "received_at_ms",
        "hook_event_name",
        "agent_id",
        "agent_type",
        "prompt_id",
        "tool_name",
        "tool_use_id",
        "mcp_server_name",
        "notification_type",
        "elicitation_id",
        "elicitation_action",
        "compact_trigger",
        "start_source",
        "end_reason",
        "error_code",
        "tool_response_status",
        "duration_ms",
        "is_interrupt",
        "prompt_length",
        "tool_input_bytes",
        "tool_response_bytes",
    },
    "session_state": {
        "session_id",
        "host",
        "agent_order",
        "pending_tools",
        "completed_tool_use_ids",
        "stopped_agent_ids",
        "async_launches",
    },
    "alerts": {
        "id",
        "host",
        "rule",
        "session_id",
        "subject",
        "fired_at_ms",
        "cleared_at_ms",
        "value",
        "threshold",
        "discord_state",
        "discord_attempts",
        "discord_next_at_ms",
    },
    "alert_settings": {"host", "field", "value", "updated_at_ms"},
}

# Parts of a column name that would mean content or a path other than cwd (ADR 0023).
CONTENT_WORDS: Final = (
    "prompt_text",
    "tool_input",
    "tool_response",
    "message",
    "transcript",
    "path",
    "summary",
    "instructions",
    "title",
    "description",
    "subject",
    "schema",
    "content",
    "body",
    "payload",
    "command",
    "output",
)
# Exact names of payload fields that are free text (ADR 0023): PermissionDenied's reason,
# PostToolUseFailure's error and the prompt itself.
CONTENT_NAMES: Final = frozenset(
    {"prompt", "error", "reason", "tool_calls", "last_assistant_message", "error_details"}
)
# The size and status columns that name content without holding it.
CONTENT_METADATA: Final = frozenset(
    {"tool_input_bytes", "tool_response_bytes", "tool_response_status"}
)
# An alert's subject is its session's ID or "day", which a CHECK constraint enforces
# (ADR 0042), never a task's or a message's subject.
CONTENT_METADATA_OF: Final = frozenset({("alerts", "subject")})


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return create_engine_for(tmp_path / "ccobs.db", role="writer")


@pytest.fixture
def reader(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="reader")
    yield engine
    engine.dispose()


@pytest.fixture
def migrated(engine: Engine) -> Engine:
    upgrade_to_head(engine)
    return engine


def _pragma(engine: Engine, name: str) -> object:
    with engine.connect() as connection:
        return connection.execute(text(f"PRAGMA {name}")).scalar_one()


# Engine.


def test_the_engine_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data" / "ccobs.db"
    engine = create_engine_for(path, role="writer")
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    assert path.is_file()
    assert engine.dialect.name == "sqlite"
    assert engine.dialect.driver == "pysqlite"


def test_the_engine_sets_the_pragmas_on_every_connection(engine: Engine) -> None:
    assert _pragma(engine, "journal_mode") == "wal"
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "busy_timeout") == BUSY_TIMEOUT_MS
    # NORMAL is 1: safe in WAL mode, and a commit does not wait on a disk sync.
    assert _pragma(engine, "synchronous") == 1
    engine.dispose()
    # A new connection gets them again: the per-connection pragmas are not persistent.
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "busy_timeout") == BUSY_TIMEOUT_MS
    assert wal_enabled(engine)


def test_the_driver_leaves_transactions_to_sqlalchemy(engine: Engine) -> None:
    # SQLAlchemy's pysqlite recipe: the driver never begins or commits on its own, and
    # the engine's "begin" listener sends BEGIN for every transaction.
    raw = engine.raw_connection()
    try:
        assert raw.driver_connection is not None
        assert raw.driver_connection.isolation_level is None
    finally:
        raw.close()


def test_a_journal_mode_other_than_wal_warns_without_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Where WAL cannot work, SQLite answers with another mode; DELETE stands in for it.
    monkeypatch.setattr(db_engine, "_JOURNAL_MODE", "PRAGMA journal_mode=DELETE")
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    with caplog.at_level(logging.WARNING, logger="ccobs.server.db.engine"):
        assert not wal_enabled(engine)
    (record,) = caplog.records
    assert "'delete', not 'wal'" in record.getMessage()
    assert str(tmp_path) not in record.getMessage()
    assert "ccobs.db" not in record.getMessage()


def test_ddl_rolls_back_with_its_transaction(engine: Engine) -> None:
    def create_then_fail() -> None:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE scratch (x INTEGER)"))
            raise RuntimeError

    with pytest.raises(RuntimeError):
        create_then_fail()
    assert "scratch" not in inspect(engine).get_table_names()


def test_a_read_transaction_sees_one_snapshot(engine: Engine, reader: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scratch (x INTEGER)"))
    with reader.connect() as reading:
        before = reading.execute(text("SELECT count(*) FROM scratch")).scalar_one()
        with engine.begin() as writer:
            writer.execute(text("INSERT INTO scratch VALUES (1)"))
        during = reading.execute(text("SELECT count(*) FROM scratch")).scalar_one()
    with reader.connect() as reading:
        after = reading.execute(text("SELECT count(*) FROM scratch")).scalar_one()
    assert (before, during, after) == (0, 0, 1)


# Roles (ADR 0033): the writer's engine and the readers' engine.


def test_the_writer_engine_holds_one_connection(engine: Engine) -> None:
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    assert (pool.size(), pool._max_overflow) == (1, 0)


def test_the_reader_engine_holds_four_connections(reader: Engine) -> None:
    pool = reader.pool
    assert isinstance(pool, QueuePool)
    assert (pool.size(), pool._max_overflow) == (READER_POOL_SIZE, 0)
    assert READER_POOL_SIZE == 4


def test_the_reader_engine_sets_the_same_pragmas_and_query_only(
    engine: Engine, reader: Engine
) -> None:
    assert _pragma(reader, "journal_mode") == "wal"
    assert _pragma(reader, "foreign_keys") == 1
    assert _pragma(reader, "busy_timeout") == BUSY_TIMEOUT_MS
    assert _pragma(reader, "synchronous") == 1
    assert _pragma(reader, "query_only") == 1
    assert _pragma(engine, "query_only") == 0
    reader.dispose()
    # On every new connection, not once.
    assert _pragma(reader, "query_only") == 1
    assert wal_enabled(reader)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO scratch VALUES ('b')",
        "UPDATE scratch SET x = 'c'",
        "DELETE FROM scratch",
        "CREATE TABLE other (y INTEGER)",
        "DROP TABLE scratch",
    ],
)
def test_a_reader_connection_cannot_write(engine: Engine, reader: Engine, statement: str) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scratch (x TEXT)"))
        connection.execute(text("INSERT INTO scratch VALUES ('a')"))

    with pytest.raises(OperationalError, match="readonly"), reader.begin() as connection:
        connection.execute(text(statement))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT x FROM scratch")).scalars().all() == ["a"]


def test_the_reader_keeps_transactions_and_hidden_parameters(reader: Engine) -> None:
    raw = reader.raw_connection()
    try:
        assert raw.driver_connection is not None
        assert raw.driver_connection.isolation_level is None
    finally:
        raw.close()
    assert reader.hide_parameters


def test_errors_never_show_bound_values(engine: Engine) -> None:
    canary = "/home/CANARY-1b2c/project"
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scratch (x TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO scratch VALUES (:x)"), {"x": canary})
    with pytest.raises(IntegrityError) as error, engine.begin() as connection:
        connection.execute(text("INSERT INTO scratch VALUES (:x)"), {"x": canary})
    assert "CANARY" not in str(error.value)


# Migrations.

REVISION_TEST: Final = '''"""A test revision."""

import sqlalchemy as sa
from alembic import op

revision = "9999"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''


def _with_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Point the migrations at a copy of the package's scripts plus a revision 9999,
    after the head."""
    location = tmp_path / "migrations"
    shutil.copytree(
        Path(str(resources.files("ccobs.server.migrations"))),
        location,
        ignore=shutil.ignore_patterns("__pycache__", "__init__.py"),
    )
    revision = REVISION_TEST.format(body=body, head=HEAD)
    (location / "versions" / "9999_test.py").write_text(revision, encoding="utf-8")
    monkeypatch.setattr(migrations, "_SCRIPT_LOCATION", location)


def _store_a_session(engine: Engine) -> None:
    repo = Repository(engine)
    state = None
    payloads: list[dict[str, Any]] = [
        {"hook_event_name": "UserPromptSubmit", "prompt": "x"},
        {"hook_event_name": "SubagentStart", "agent_id": "a1"},
        {"hook_event_name": "PermissionRequest", "agent_id": "a1", "tool_name": "Bash"},
    ]
    for seq, payload in enumerate(payloads, start=1):
        hook_event = parse_hook({"session_id": "s1", **payload})
        state, effects = apply(state, hook_event, seq, seq)
        repo.apply_effects(
            "h", hook_event_row(hook_event, seq=seq, received_at_ms=seq), effects, state
        )


# The tables of revision 0001.
OLD_TABLES: Final = ("sessions", "agents", "blocks", "hook_events", "session_state")


def _counts_of(engine: Engine, tables: Iterable[str]) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608 - fixed names
            for table in tables
        }


def _counts(engine: Engine) -> dict[str, int]:
    return _counts_of(engine, COLUMNS)


def test_a_failing_revision_leaves_nothing_behind(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every revision runs in one transaction: 9999 fails after its own DDL, and the
    # tables of the earlier revisions go too.
    body = (
        '    op.create_table("extra", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    raise RuntimeError("boom")'
    )
    _with_revision(tmp_path, monkeypatch, body)
    with pytest.raises(RuntimeError, match="boom"):
        upgrade_to_head(engine)
    assert inspect(engine).get_table_names() == []


def test_migrations_run_with_foreign_keys_off(
    migrated: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A batch migration copies sessions and drops the original. With foreign keys on,
    # the drop would cascade-delete every agent, block, event, state and alert row.
    _store_a_session(migrated)
    _insert_alert(migrated)
    with migrated.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO alert_settings (host, field, value, updated_at_ms)"
                " VALUES ('h', 'alert_cooldown_s', '60', 1)"
            )
        )
    before = _counts(migrated)
    body = (
        '    with op.batch_alter_table("sessions", recreate="always") as batch:\n'
        '        batch.alter_column("source", existing_type=sa.String(), nullable=True)'
    )
    _with_revision(tmp_path, monkeypatch, body)
    upgrade_to_head(migrated)
    assert _counts(migrated) == before
    assert all(before.values())
    # The app's own connections keep foreign keys on.
    assert _pragma(migrated, "foreign_keys") == 1


def test_a_foreign_key_violation_fails_the_whole_upgrade(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "    op.execute(\n"
        '        "INSERT INTO agents (session_id, agent_id, host, status, internal,"\n'
        "        \" background, last_event_at_ms) VALUES ('none', 'a', 'h', 'running', 0, 0, 1)\"\n"
        "    )"
    )
    _with_revision(tmp_path, monkeypatch, body)
    with pytest.raises(MigrationError, match="1 foreign key violations"):
        upgrade_to_head(engine)
    assert inspect(engine).get_table_names() == []


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_migrations_need_a_database_file(url: str) -> None:
    with pytest.raises(ValueError, match="database file"):
        upgrade_to_head(create_engine(url))


def test_upgrade_to_head_matches_the_models(migrated: Engine) -> None:
    with migrated.connect() as connection:
        context = MigrationContext.configure(connection)
        assert context.get_current_revision() == HEAD
        assert compare_metadata(context, Base.metadata) == []


def test_upgrade_to_head_twice_is_a_no_op(migrated: Engine) -> None:
    before = _schema(migrated)
    upgrade_to_head(migrated)
    assert _schema(migrated) == before
    with migrated.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == HEAD


def test_the_initial_migration_downgrades_to_an_empty_database(migrated: Engine) -> None:
    with migrated.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
    assert set(inspect(migrated).get_table_names()) == {"alembic_version"}


def test_migrations_need_no_alembic_ini() -> None:
    package = resources.files("ccobs.server.migrations")
    assert not (package / "alembic.ini").is_file()
    config = alembic_config(None)
    assert config.config_file_name is None
    assert Path(config.get_main_option("script_location") or "") == Path(str(package))


# The schema.


def _schema(engine: Engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def test_every_column_is_in_the_allowlist(migrated: Engine) -> None:
    assert _schema(migrated) == COLUMNS


def test_no_column_holds_content() -> None:
    for table, columns in COLUMNS.items():
        for column in columns - CONTENT_METADATA:
            if (table, column) in CONTENT_METADATA_OF:
                continue
            assert not any(word in column for word in CONTENT_WORDS), (table, column)
            assert column not in CONTENT_NAMES, (table, column)
    assert "cwd" in COLUMNS["sessions"]
    assert all("cwd" not in columns for table, columns in COLUMNS.items() if table != "sessions")


def test_every_table_has_a_host_column(migrated: Engine) -> None:
    for table, columns in _schema(migrated).items():
        assert "host" in columns, table


def test_child_tables_cascade_from_sessions(migrated: Engine) -> None:
    inspector = inspect(migrated)
    for table in ("agents", "blocks", "hook_events", "session_state", "alerts"):
        (foreign_key,) = inspector.get_foreign_keys(table)
        assert foreign_key["referred_table"] == "sessions"
        assert foreign_key["constrained_columns"] == ["session_id"]
        assert foreign_key["options"].get("ondelete") == "CASCADE"


def test_the_indexes(migrated: Engine) -> None:
    inspector = inspect(migrated)
    indexes = {
        table: {tuple(index["column_names"]) for index in inspector.get_indexes(table)}
        for table in ("sessions", "blocks", "hook_events")
    }
    assert {("status",), ("last_event_at_ms",)} <= indexes["sessions"]
    assert ("session_id", "seq") in indexes["hook_events"]
    assert ("received_at_ms",) in indexes["hook_events"]
    # A partial index over the open blocks only.
    assert ("session_id",) in indexes["blocks"]
    with migrated.connect() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'ix_blocks_open'")
        ).scalar_one()
    assert "WHERE closed_at_ms IS NULL" in sql


# The alert tables (ADR 0042).

SESSION_ID: Final = "s1"


def _index_sql(engine: Engine, name: str) -> str:
    with engine.connect() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": name},
        ).scalar_one()
    return str(sql)


def test_the_alert_indexes(migrated: Engine) -> None:
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspect(migrated).get_indexes("alerts")
    }
    assert indexes == {
        "ix_alerts_open": ("host", "rule", "subject"),
        "ix_alerts_discord_pending": ("host", "discord_next_at_ms", "id"),
        "ix_alerts_fired_at_ms": ("fired_at_ms",),
        "ix_alerts_session_id": ("session_id",),
    }
    assert "WHERE cleared_at_ms IS NULL" in _index_sql(migrated, "ix_alerts_open")
    assert "WHERE discord_state = 'pending'" in _index_sql(migrated, "ix_alerts_discord_pending")


def test_alert_ids_are_never_reused(migrated: Engine) -> None:
    # AUTOINCREMENT: an ID deleted with the newest row is not handed out again, so a
    # page cursor (keyset by id) never meets a different alert under an old ID.
    with migrated.connect() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'alerts'")
        ).scalar_one()
    assert "AUTOINCREMENT" in str(sql)


def test_alert_settings_has_one_row_per_host_and_field(migrated: Engine) -> None:
    inspector = inspect(migrated)
    assert inspector.get_pk_constraint("alert_settings")["constrained_columns"] == [
        "host",
        "field",
    ]
    assert inspector.get_foreign_keys("alert_settings") == []


_ALERT_COLUMNS: Final = (
    "host, rule, session_id, subject, fired_at_ms, cleared_at_ms, value, threshold,"
    " discord_state, discord_attempts, discord_next_at_ms"
)
_VALID_ALERT: Final = {
    "host": "'h'",
    "rule": "'waiting_user'",
    "session_id": f"'{SESSION_ID}'",
    "subject": f"'{SESSION_ID}'",
    "fired_at_ms": "10",
    "cleared_at_ms": "NULL",
    "value": "31.5",
    "threshold": "30",
    "discord_state": "'pending'",
    "discord_attempts": "0",
    "discord_next_at_ms": "10",
}


def _insert_alert(engine: Engine, **overrides: str) -> None:
    values = {**_VALID_ALERT, **overrides}
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO alerts ({_ALERT_COLUMNS}) VALUES ("  # noqa: S608 - test literals
                + ", ".join(values[name.strip()] for name in _ALERT_COLUMNS.split(","))
                + ")"
            )
        )


def _alert_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM alerts")).scalar_one())


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"discord_state": "'none'", "discord_next_at_ms": "NULL"},
        {"discord_state": "'sent'", "discord_attempts": "1", "cleared_at_ms": "10"},
        {"discord_state": "'failed'", "discord_attempts": "5"},
        {"discord_state": "'expired'"},
        {"rule": "'repeated_failures'"},
        {"rule": "'session_cost'"},
        {"rule": "'daily_cost'", "session_id": "NULL", "subject": "'day'"},
    ],
)
def test_the_alert_checks_accept_valid_rows(migrated: Engine, overrides: dict[str, str]) -> None:
    _store_a_session(migrated)
    _insert_alert(migrated, **overrides)
    assert _alert_count(migrated) == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"rule": "'cost'"}, "ck_alerts_rule"),
        ({"discord_state": "'delivered'"}, "ck_alerts_discord_state"),
        # No rule or channel can be off for one alert: a disabled rule fires nothing.
        ({"discord_state": "'skipped'"}, "ck_alerts_discord_state"),
        ({"discord_state": "'pending'", "discord_next_at_ms": "NULL"}, "ck_alerts_discord_next"),
        ({"discord_attempts": "-1"}, "ck_alerts_discord_attempts"),
        ({"cleared_at_ms": "9"}, "ck_alerts_cleared"),
        # The subject is the session's ID, or "day" for daily_cost alone: never free text.
        ({"subject": "'a prompt'"}, "ck_alerts_subject"),
        ({"subject": "'day'"}, "ck_alerts_subject"),
        ({"session_id": "NULL"}, "ck_alerts_subject"),
        ({"rule": "'daily_cost'"}, "ck_alerts_subject"),
        ({"rule": "'daily_cost'", "session_id": "NULL"}, "ck_alerts_subject"),
        ({"rule": "'waiting_user'", "session_id": "NULL", "subject": "'day'"}, "ck_alerts_subject"),
    ],
)
def test_the_alert_checks_reject_bad_rows(
    migrated: Engine, overrides: dict[str, str], constraint: str
) -> None:
    _store_a_session(migrated)
    # The whole name: a doubled prefix (ck_alerts_ck_alerts_rule) must not pass.
    with pytest.raises(IntegrityError, match=rf"CHECK constraint failed: {constraint}\b"):
        _insert_alert(migrated, **overrides)
    assert _alert_count(migrated) == 0


def test_an_alert_needs_its_session(migrated: Engine) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        _insert_alert(migrated)


def test_session_alerts_go_with_their_session(migrated: Engine) -> None:
    _store_a_session(migrated)
    _insert_alert(migrated)
    _insert_alert(migrated, rule="'daily_cost'", session_id="NULL", subject="'day'")
    with migrated.begin() as connection:
        connection.execute(text("DELETE FROM sessions"))
    with migrated.connect() as connection:
        rules = connection.execute(text("SELECT rule FROM alerts")).scalars().all()
    assert rules == ["daily_cost"]


def _revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def test_the_alerts_revision_upgrades_a_database_with_data(engine: Engine) -> None:
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001")
    _store_a_session(engine)
    before = _counts_of(engine, OLD_TABLES)
    assert all(before.values())

    upgrade_to_head(engine)

    assert _revision(engine) == HEAD == "0002"
    assert _counts_of(engine, OLD_TABLES) == before
    assert _schema(engine) == COLUMNS
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    # The new tables work on the upgraded file.
    _insert_alert(engine)
    assert _alert_count(engine) == 1


def _alert_ddl(engine: Engine) -> dict[str, str]:
    """The ``CREATE`` statements of ``alerts`` and its indexes, whitespace collapsed."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'alerts' AND sql IS NOT NULL"
            )
        ).all()
    return {str(name): " ".join(str(sql).split()) for name, sql in rows}


def test_the_alerts_revision_matches_the_models_exactly(migrated: Engine, tmp_path: Path) -> None:
    # compare_metadata ignores CHECK constraints and partial index WHERE clauses, so the
    # revision's frozen rule and delivery-state sets could drift from the models'
    # unseen. The stored DDL of both must be the same, text for text.
    fresh = create_engine_for(tmp_path / "fresh.db", role="writer")
    Base.metadata.create_all(fresh)
    try:
        expected = _alert_ddl(fresh)
    finally:
        fresh.dispose()
    assert set(expected) == {
        "alerts",
        "ix_alerts_open",
        "ix_alerts_discord_pending",
        "ix_alerts_fired_at_ms",
        "ix_alerts_session_id",
    }
    assert _alert_ddl(migrated) == expected


def test_the_alerts_revision_downgrades_and_keeps_the_rest(migrated: Engine) -> None:
    _store_a_session(migrated)
    _insert_alert(migrated)
    before = _counts_of(migrated, OLD_TABLES)
    with migrated.begin() as connection:
        command.downgrade(alembic_config(connection), "0001")
    assert _revision(migrated) == "0001"
    assert set(_schema(migrated)) == set(OLD_TABLES)
    assert _counts_of(migrated, OLD_TABLES) == before
    # And up again.
    upgrade_to_head(migrated)
    assert _schema(migrated) == COLUMNS


# Packaging.


def test_the_migrations_are_package_resources() -> None:
    package = resources.files("ccobs.server.migrations")
    assert (package / "env.py").is_file()
    assert (package / "script.py.mako").is_file()
    assert (package / "versions" / "0001_initial.py").is_file()
    assert (package / "versions" / "0002_alerts.py").is_file()


def test_the_wheel_ships_the_migrations(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv builds the wheel (pyproject.toml's build backend is uv_build)"
    subprocess.run(  # noqa: S603 - uv from PATH, fixed arguments
        [uv, "build", str(ROOT), "--wheel", "--offline", "--quiet", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for name in (
        "ccobs/server/migrations/__init__.py",
        "ccobs/server/migrations/env.py",
        "ccobs/server/migrations/script.py.mako",
        "ccobs/server/migrations/versions/0001_initial.py",
        "ccobs/server/migrations/versions/0002_alerts.py",
        "ccobs/server/db/models.py",
    ):
        assert name in names
    assert not any("__pycache__" in name for name in names)
