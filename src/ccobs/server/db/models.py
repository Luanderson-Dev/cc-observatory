"""The app's tables (ADRs 0026 and 0042): sessions, agents, blocks, hook events, session
state, alerts and alert settings.

Metadata only (ADR 0023): no column holds a prompt, a tool input or output, a message,
error text or a path, except ``sessions.cwd``. ``sessions.cwd`` and ``sessions.model``
(an inference-profile ARN with the AWS account ID on Bedrock) are personal data at rest.
Every table has ``host`` (ADR 0022), and every time is an integer of UTC milliseconds,
the receive time the reducer works with (ADR 0024).

``tests/server/test_db.py`` lists every column; a new one needs that test and an ADR.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

from sqlalchemy import JSON, REAL, CheckConstraint, ForeignKey, Index, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ccobs.domain.alerts import ALERT_RULES, DAY_SUBJECT

__all__ = [
    "DISCORD_STATES",
    "AgentModel",
    "AlertModel",
    "AlertSettingModel",
    "Base",
    "BlockModel",
    "DiscordState",
    "HookEventModel",
    "SessionModel",
    "SessionStateModel",
]

# Where an alert's Discord delivery stands (ADR 0042). The transitions belong to the
# Discord stage of milestone 5; revision 0002 freezes this set in a CHECK, so changing it
# needs a new revision.
# - none: Discord was not configured when the alert fired; it is never sent.
# - pending: waiting to be sent; the next attempt is due at discord_next_at_ms.
# - sent: Discord accepted it.
# - failed: given up, the attempts exhausted or the webhook refused (401, 403 or 404).
# - expired: still pending an hour after it fired, and dropped.
type DiscordState = Literal["none", "pending", "sent", "failed", "expired"]
DISCORD_STATES: Final[tuple[DiscordState, ...]] = ("none", "pending", "sent", "failed", "expired")
_DAILY: Final = "daily_cost"

# Named constraints, so that later migrations (batch mode on SQLite) can refer to them.
NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _session_fk() -> ForeignKey:
    return ForeignKey("sessions.session_id", ondelete="CASCADE")


class SessionModel(Base):
    """One row per session: the reducer's ``Session`` plus ``host``."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_status", "status"),
        Index("ix_sessions_last_event_at_ms", "last_event_at_ms"),
    )

    session_id: Mapped[str] = mapped_column(primary_key=True)
    host: Mapped[str]
    status: Mapped[str]
    started_at_ms: Mapped[int]
    last_event_at_ms: Mapped[int]
    last_seq: Mapped[int]
    turn_open: Mapped[bool]
    compacting: Mapped[bool]
    source: Mapped[str | None]
    model: Mapped[str | None]
    permission_mode: Mapped[str | None]
    effort_level: Mapped[str | None]
    last_prompt_id: Mapped[str | None]
    last_error_code: Mapped[str | None]
    idle_notified_at_ms: Mapped[int | None]
    ended_at_ms: Mapped[int | None]
    end_reason: Mapped[str | None]
    cwd: Mapped[str | None]


class AgentModel(Base):
    """Every agent a session ever had, running or stopped: the reducer's ``Agent``."""

    __tablename__ = "agents"

    session_id: Mapped[str] = mapped_column(_session_fk(), primary_key=True)
    agent_id: Mapped[str] = mapped_column(primary_key=True)
    host: Mapped[str]
    status: Mapped[str]
    agent_type: Mapped[str | None]
    internal: Mapped[bool]
    background: Mapped[bool]
    tool_use_id: Mapped[str | None]
    started_at_ms: Mapped[int | None]
    stopped_at_ms: Mapped[int | None]
    last_event_at_ms: Mapped[int]
    stop_reason: Mapped[str | None]


class BlockModel(Base):
    """Every block, open or closed: the reducer's ``Block``. ``block_id`` is the ``seq`` of
    the event that opened it; ``scope_agent_id`` is ``Block.scope``."""

    __tablename__ = "blocks"
    __table_args__ = (
        Index(
            "ix_blocks_open",
            "session_id",
            sqlite_where=text("closed_at_ms IS NULL"),
        ),
    )

    session_id: Mapped[str] = mapped_column(_session_fk(), primary_key=True)
    block_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    host: Mapped[str]
    kind: Mapped[str]
    scope_agent_id: Mapped[str | None]
    tool_name: Mapped[str | None]
    tool_use_id: Mapped[str | None]
    mcp_server_name: Mapped[str | None]
    elicitation_id: Mapped[str | None]
    opened_at_ms: Mapped[int]
    notified_at_ms: Mapped[int | None]
    closed_at_ms: Mapped[int | None]
    close_reason: Mapped[str | None]
    close_source: Mapped[str | None]


class HookEventModel(Base):
    """One row per hook event received, with the metadata ADR 0023 keeps for it. ``seq`` is
    assigned by the app, in receive order."""

    __tablename__ = "hook_events"
    __table_args__ = (
        Index("ix_hook_events_session_id_seq", "session_id", "seq"),
        Index("ix_hook_events_received_at_ms", "received_at_ms"),
    )

    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    host: Mapped[str]
    session_id: Mapped[str] = mapped_column(_session_fk())
    received_at_ms: Mapped[int]
    hook_event_name: Mapped[str]
    agent_id: Mapped[str | None]
    agent_type: Mapped[str | None]
    prompt_id: Mapped[str | None]
    tool_name: Mapped[str | None]
    tool_use_id: Mapped[str | None]
    mcp_server_name: Mapped[str | None]
    notification_type: Mapped[str | None]
    elicitation_id: Mapped[str | None]
    # ElicitationResult's action, PreCompact/PostCompact's trigger, SessionStart's
    # source, SessionEnd's reason and StopFailure's error code.
    elicitation_action: Mapped[str | None]
    compact_trigger: Mapped[str | None]
    start_source: Mapped[str | None]
    end_reason: Mapped[str | None]
    error_code: Mapped[str | None]
    tool_response_status: Mapped[str | None]
    duration_ms: Mapped[int | None]
    is_interrupt: Mapped[bool | None]
    prompt_length: Mapped[int | None]
    tool_input_bytes: Mapped[int | None]
    tool_response_bytes: Mapped[int | None]


class SessionStateModel(Base):
    """The reducer's working state that no other table holds, so that a restart rebuilds
    an exact ``SessionState`` (ADR 0026). Identifiers and tool names only.

    ``agent_order`` lists the running agents in the order the state holds them; the agents
    themselves are the ``agents`` rows. ``pending_tools`` holds ``PendingTool`` objects.
    """

    __tablename__ = "session_state"

    session_id: Mapped[str] = mapped_column(_session_fk(), primary_key=True)
    host: Mapped[str]
    agent_order: Mapped[list[str]] = mapped_column(JSON)
    pending_tools: Mapped[list[dict[str, str | None]]] = mapped_column(JSON)
    completed_tool_use_ids: Mapped[list[str]] = mapped_column(JSON)
    stopped_agent_ids: Mapped[list[str]] = mapped_column(JSON)
    async_launches: Mapped[list[str]] = mapped_column(JSON)


def _one_of(column: str, values: Iterable[str]) -> str:
    """A CHECK clause that ``column`` holds one of ``values`` (fixed identifiers)."""
    return f"{column} IN ({', '.join(f"'{value}'" for value in values)})"


class AlertModel(Base):
    """One row per alert that fired (ADR 0042): its key, when it fired and cleared, its
    value and threshold in the rule's unit, and the state of its Discord delivery.

    ``subject`` is the key's subject: the session's ID, which ``session_id`` repeats so
    that the alert goes with its session (cascade), or ``"day"`` for ``daily_cost``,
    whose ``session_id`` is null; a CHECK keeps it to those, never free text. No
    project name, ``cwd`` or content: pages and messages derive the project from the
    session at read time. ``id`` is ``AUTOINCREMENT``, so an ID is never reused and a
    page cursor never meets another alert under an old ID.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(_one_of("rule", ALERT_RULES), name="rule"),
        CheckConstraint(_one_of("discord_state", DISCORD_STATES), name="discord_state"),
        CheckConstraint(
            f"(rule = '{_DAILY}' AND session_id IS NULL AND subject = '{DAY_SUBJECT}')"
            f" OR (rule != '{_DAILY}' AND session_id IS NOT NULL AND subject = session_id)",
            name="subject",
        ),
        CheckConstraint(
            "discord_state != 'pending' OR discord_next_at_ms IS NOT NULL", name="discord_next"
        ),
        CheckConstraint("discord_attempts >= 0", name="discord_attempts"),
        CheckConstraint("cleared_at_ms IS NULL OR cleared_at_ms >= fired_at_ms", name="cleared"),
        # The open alerts of a key, which a clear looks up.
        Index(
            "ix_alerts_open",
            "host",
            "rule",
            "subject",
            sqlite_where=text("cleared_at_ms IS NULL"),
        ),
        # The Discord deliveries due, in the order they are sent.
        Index(
            "ix_alerts_discord_pending",
            "host",
            "discord_next_at_ms",
            "id",
            sqlite_where=text("discord_state = 'pending'"),
        ),
        # Retention of the daily alerts, and the cooldowns rebuilt at start-up.
        Index("ix_alerts_fired_at_ms", "fired_at_ms"),
        # The cascade from sessions looks alerts up by session.
        Index("ix_alerts_session_id", "session_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str]
    rule: Mapped[str]
    session_id: Mapped[str | None] = mapped_column(_session_fk())
    subject: Mapped[str]
    fired_at_ms: Mapped[int]
    cleared_at_ms: Mapped[int | None]
    value: Mapped[float] = mapped_column(REAL)
    threshold: Mapped[float] = mapped_column(REAL)
    discord_state: Mapped[str]
    discord_attempts: Mapped[int] = mapped_column(server_default=text("0"))
    discord_next_at_ms: Mapped[int | None]


class AlertSettingModel(Base):
    """One row per alert setting saved on the settings screen (ADR 0042), which overrides
    the environment's value. ``field`` is a flat setting name (``CCOBS_<FIELD>`` lower
    case, such as ``alert_cooldown_s``), checked against an allowlist in code;
    ``value`` is its JSON text (``true``, ``30``, ``10.5``), which keeps a bool, an int
    and a float apart exactly."""

    __tablename__ = "alert_settings"

    host: Mapped[str] = mapped_column(primary_key=True)
    field: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]
    updated_at_ms: Mapped[int]
