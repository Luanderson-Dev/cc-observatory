"""Alembic's environment: runs the migrations on the connection that
``ccobs.server.migrations.alembic_config`` passes in. Online mode only."""

from __future__ import annotations

from alembic import context

from ccobs.server.db.models import Base

context.configure(
    connection=context.config.attributes["connection"],
    target_metadata=Base.metadata,
    # SQLite alters most table definitions by copying the table (ADR 0005).
    render_as_batch=True,
)
with context.begin_transaction():
    context.run_migrations()
