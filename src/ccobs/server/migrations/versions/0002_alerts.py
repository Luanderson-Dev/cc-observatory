"""The alerts and the alert settings saved on the settings screen (ADR 0042).

Two new tables, no change to the others: nothing is copied, so the upgrade is quick on
any database.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen here, as every revision must be: never edit them in place. Changing the set of
# rules (models' ALERT_RULES) or of delivery states (DISCORD_STATES) needs a new revision
# that recreates these CHECK constraints; test_db's DDL comparison with the models fails
# until it exists.
_RULES = "'waiting_user', 'repeated_failures', 'session_cost', 'daily_cost'"
_DISCORD_STATES = "'none', 'pending', 'sent', 'failed', 'expired'"


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("rule", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("fired_at_ms", sa.Integer(), nullable=False),
        sa.Column("cleared_at_ms", sa.Integer(), nullable=True),
        sa.Column("value", sa.REAL(), nullable=False),
        sa.Column("threshold", sa.REAL(), nullable=False),
        sa.Column("discord_state", sa.String(), nullable=False),
        sa.Column("discord_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("discord_next_at_ms", sa.Integer(), nullable=True),
        # op.f: final names, which the naming convention must not prefix again.
        sa.CheckConstraint(f"rule IN ({_RULES})", name=op.f("ck_alerts_rule")),
        sa.CheckConstraint(
            f"discord_state IN ({_DISCORD_STATES})", name=op.f("ck_alerts_discord_state")
        ),
        sa.CheckConstraint(
            "(rule = 'daily_cost' AND session_id IS NULL AND subject = 'day')"
            " OR (rule != 'daily_cost' AND session_id IS NOT NULL AND subject = session_id)",
            name=op.f("ck_alerts_subject"),
        ),
        sa.CheckConstraint(
            "discord_state != 'pending' OR discord_next_at_ms IS NOT NULL",
            name=op.f("ck_alerts_discord_next"),
        ),
        sa.CheckConstraint("discord_attempts >= 0", name=op.f("ck_alerts_discord_attempts")),
        sa.CheckConstraint(
            "cleared_at_ms IS NULL OR cleared_at_ms >= fired_at_ms", name=op.f("ck_alerts_cleared")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_alerts_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_alerts_open",
        "alerts",
        ["host", "rule", "subject"],
        sqlite_where=sa.text("cleared_at_ms IS NULL"),
    )
    op.create_index(
        "ix_alerts_discord_pending",
        "alerts",
        ["host", "discord_next_at_ms", "id"],
        sqlite_where=sa.text("discord_state = 'pending'"),
    )
    op.create_index("ix_alerts_fired_at_ms", "alerts", ["fired_at_ms"])
    op.create_index("ix_alerts_session_id", "alerts", ["session_id"])

    op.create_table(
        "alert_settings",
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("updated_at_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("host", "field", name="pk_alert_settings"),
    )


def downgrade() -> None:
    op.drop_table("alert_settings")
    op.drop_index("ix_alerts_session_id", table_name="alerts")
    op.drop_index("ix_alerts_fired_at_ms", table_name="alerts")
    op.drop_index("ix_alerts_discord_pending", table_name="alerts")
    op.drop_index("ix_alerts_open", table_name="alerts")
    op.drop_table("alerts")
