"""The initial schema: sessions, agents, blocks, hook events and session state (ADR 0026).

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def _session_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["session_id"],
        ["sessions.session_id"],
        name=f"fk_{table}_session_id_sessions",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at_ms", sa.Integer(), nullable=False),
        sa.Column("last_event_at_ms", sa.Integer(), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("turn_open", sa.Boolean(), nullable=False),
        sa.Column("compacting", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("permission_mode", sa.String(), nullable=True),
        sa.Column("effort_level", sa.String(), nullable=True),
        sa.Column("last_prompt_id", sa.String(), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("idle_notified_at_ms", sa.Integer(), nullable=True),
        sa.Column("ended_at_ms", sa.Integer(), nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("cwd", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_last_event_at_ms", "sessions", ["last_event_at_ms"])

    op.create_table(
        "agents",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("agent_type", sa.String(), nullable=True),
        sa.Column("internal", sa.Boolean(), nullable=False),
        sa.Column("background", sa.Boolean(), nullable=False),
        sa.Column("tool_use_id", sa.String(), nullable=True),
        sa.Column("started_at_ms", sa.Integer(), nullable=True),
        sa.Column("stopped_at_ms", sa.Integer(), nullable=True),
        sa.Column("last_event_at_ms", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.String(), nullable=True),
        _session_fk("agents"),
        sa.PrimaryKeyConstraint("session_id", "agent_id", name="pk_agents"),
    )

    op.create_table(
        "blocks",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("block_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope_agent_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_use_id", sa.String(), nullable=True),
        sa.Column("mcp_server_name", sa.String(), nullable=True),
        sa.Column("elicitation_id", sa.String(), nullable=True),
        sa.Column("opened_at_ms", sa.Integer(), nullable=False),
        sa.Column("notified_at_ms", sa.Integer(), nullable=True),
        sa.Column("closed_at_ms", sa.Integer(), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.Column("close_source", sa.String(), nullable=True),
        _session_fk("blocks"),
        sa.PrimaryKeyConstraint("session_id", "block_id", name="pk_blocks"),
    )
    op.create_index(
        "ix_blocks_open",
        "blocks",
        ["session_id"],
        sqlite_where=sa.text("closed_at_ms IS NULL"),
    )

    op.create_table(
        "hook_events",
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("received_at_ms", sa.Integer(), nullable=False),
        sa.Column("hook_event_name", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("agent_type", sa.String(), nullable=True),
        sa.Column("prompt_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_use_id", sa.String(), nullable=True),
        sa.Column("mcp_server_name", sa.String(), nullable=True),
        sa.Column("notification_type", sa.String(), nullable=True),
        sa.Column("elicitation_id", sa.String(), nullable=True),
        sa.Column("elicitation_action", sa.String(), nullable=True),
        sa.Column("compact_trigger", sa.String(), nullable=True),
        sa.Column("start_source", sa.String(), nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("tool_response_status", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("is_interrupt", sa.Boolean(), nullable=True),
        sa.Column("prompt_length", sa.Integer(), nullable=True),
        sa.Column("tool_input_bytes", sa.Integer(), nullable=True),
        sa.Column("tool_response_bytes", sa.Integer(), nullable=True),
        _session_fk("hook_events"),
        sa.PrimaryKeyConstraint("seq", name="pk_hook_events"),
    )
    op.create_index("ix_hook_events_session_id_seq", "hook_events", ["session_id", "seq"])
    op.create_index("ix_hook_events_received_at_ms", "hook_events", ["received_at_ms"])

    op.create_table(
        "session_state",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("agent_order", sa.JSON(), nullable=False),
        sa.Column("pending_tools", sa.JSON(), nullable=False),
        sa.Column("completed_tool_use_ids", sa.JSON(), nullable=False),
        sa.Column("stopped_agent_ids", sa.JSON(), nullable=False),
        sa.Column("async_launches", sa.JSON(), nullable=False),
        _session_fk("session_state"),
        sa.PrimaryKeyConstraint("session_id", name="pk_session_state"),
    )


def downgrade() -> None:
    op.drop_table("session_state")
    op.drop_index("ix_hook_events_received_at_ms", table_name="hook_events")
    op.drop_index("ix_hook_events_session_id_seq", table_name="hook_events")
    op.drop_table("hook_events")
    op.drop_index("ix_blocks_open", table_name="blocks")
    op.drop_table("blocks")
    op.drop_table("agents")
    op.drop_index("ix_sessions_last_event_at_ms", table_name="sessions")
    op.drop_index("ix_sessions_status", table_name="sessions")
    op.drop_table("sessions")
