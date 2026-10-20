"""Reads and writes of the app's database (ADR 0026).

The writer persists what ``ccobs.domain.sessions`` returns: for each hook event, its
metadata row, the records its effects carry and the new ``SessionState``, all in one
transaction. A batch of events is one transaction too, with each record coalesced so the
last version wins. After a restart, ``load_open_states`` rebuilds the exact states the
writer had in memory: the session, its running agents and open blocks come from their
tables, and the bookkeeping no table holds comes from ``session_state``.

The alerts and the alert settings saved on the settings screen have their own reads and
writes (ADR 0042). Every write goes through the writer's engine; the API's reads
(``list_alerts``, ``alert_settings``) run on the readers' engine (ADR 0033).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    RowMapping,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import InstrumentedAttribute, aliased

from ccobs.domain.alerts import (
    ALERT_RULES,
    AlertCleared,
    AlertFired,
    AlertKey,
    AlertRule,
    KeyState,
)
from ccobs.domain.hooks import (
    Elicitation,
    ElicitationResult,
    HookEvent,
    Notification,
    PostCompact,
    PostToolUse,
    PostToolUseFailure,
    PreCompact,
    SessionEnd,
    SessionStart,
    StopFailure,
    ToolEvent,
    UserPromptSubmit,
)
from ccobs.domain.sessions import (
    Agent,
    AgentChanged,
    Block,
    BlockClosed,
    BlockNotified,
    BlockOpened,
    Effect,
    PendingTool,
    Session,
    SessionState,
)
from ccobs.server.db.models import (
    DISCORD_STATES,
    AgentModel,
    AlertModel,
    AlertSettingModel,
    Base,
    BlockModel,
    DiscordState,
    HookEventModel,
    SessionModel,
    SessionStateModel,
)

__all__ = [
    "ALERT_PAGE_MAX",
    "ALERT_SETTING_TYPES",
    "DELIVERY_BATCH_MAX",
    "DETAIL_CLOSED_LIMIT",
    "LIVE_STATUSES",
    "AgentPage",
    "AlertRecord",
    "AlertSettingValue",
    "Applied",
    "BoardRows",
    "HookEventRow",
    "Repository",
    "SessionCounts",
    "SessionDetail",
    "SessionView",
    "StripRow",
    "hook_event_row",
]

type _Values = dict[str, Any]

_log = logging.getLogger(__name__)

_ENDED: Final = "ended"
# The statuses whose sessions retention may delete.
_CLOSED: Final = ("ended", "stale")
_NO_BOOKKEEPING: Final[Mapping[str, list[Any]]] = {
    "agent_order": [],
    "pending_tools": [],
    "completed_tool_use_ids": [],
    "stopped_agent_ids": [],
    "async_launches": [],
}
_RUNNING: Final = "running"
_PRE_TOOL_USE: Final = "PreToolUse"
# How many stopped agents and closed blocks a session detail holds, the latest ones; the
# running agents and the open blocks are always all there (ADR 0033).
DETAIL_CLOSED_LIMIT: Final = 50
# The agents' paging key: when the agent was first seen. Agents have no monotonic column,
# and this value is set when the row is created: a running agent always has its start
# time, and an internal one is stored already stopped, with no start time. The last event
# time is only a non-null fallback. agent_id breaks ties. One exception: a late event of a
# stopped agent the reducer no longer remembers (it keeps the last 64 stopped IDs) builds
# a fresh Agent, whose upsert replaces started_at_ms and tool_use_id, so that agent moves
# to the newest page and may lose its parent (ADR 0033).
_AGENT_FIRST_SEEN: Final = func.coalesce(
    AgentModel.started_at_ms, AgentModel.stopped_at_ms, AgentModel.last_event_at_ms
)
# The statuses of the overview's live bays, which it always shows in full: the stale
# sweep moves a session out of them after hours of silence (ADR 0024).
LIVE_STATUSES: Final = ("waiting_user", "working", "idle")
_STALE: Final = "stale"
# The alerts (ADR 0042). A page of alerts and a batch of Discord deliveries hold at
# most this many rows, whatever the caller asks (ADR 0033).
ALERT_PAGE_MAX: Final = 500
DELIVERY_BATCH_MAX: Final = 100
_DAILY_COST: Final = "daily_cost"
_PENDING: Final = "pending"
# The values the CHECK constraints allow, typed: a row's rule and delivery state.
_RULES: Final[Mapping[str, AlertRule]] = {rule: rule for rule in ALERT_RULES}
_DISCORD_STATES: Final[Mapping[str, DiscordState]] = {state: state for state in DISCORD_STATES}
# The hook events that end a streak of failures (ADR 0041): a main-thread Stop (no
# agent_id) and SessionEnd.
_STOP: Final = "Stop"
_SESSION_END: Final = "SessionEnd"
_STOP_FAILURE: Final = "StopFailure"

type AlertSettingValue = bool | int | float
# The alert settings the settings screen may save, by their flat name (the Settings
# field, CCOBS_<FIELD> in lower case), with the type each takes: a bool, an int, or a
# dollar amount, an int or a finite float (ADR 0041). Anything else is refused on write
# and ignored on read.
ALERT_SETTING_TYPES: Final[Mapping[str, type[bool] | type[int] | type[float]]] = {
    "alert_waiting_enabled": bool,
    "alert_waiting_threshold_s": int,
    "alert_failures_enabled": bool,
    "alert_failures_count": int,
    "alert_failures_window_s": int,
    "alert_session_cost_enabled": bool,
    "alert_session_cost_usd": float,
    "alert_daily_cost_enabled": bool,
    "alert_daily_cost_usd": float,
    "alert_cooldown_s": int,
}
_SESSION_FIELDS: Final = tuple(f.name for f in fields(Session))
_AGENT_FIELDS: Final = tuple(f.name for f in fields(Agent))
# Block.scope is stored as scope_agent_id.
_BLOCK_COLUMNS: Final = {
    f.name: "scope_agent_id" if f.name == "scope" else f.name for f in fields(Block)
}


@dataclass(frozen=True, kw_only=True)
class HookEventRow:
    """The ``hook_events`` row of one event: its metadata (ADR 0023), never content, and
    neither ``cwd`` nor ``model``, which the session row holds."""

    seq: int
    session_id: str
    received_at_ms: int
    hook_event_name: str
    agent_id: str | None = None
    agent_type: str | None = None
    prompt_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    mcp_server_name: str | None = None
    notification_type: str | None = None
    elicitation_id: str | None = None
    elicitation_action: str | None = None
    compact_trigger: str | None = None
    start_source: str | None = None
    end_reason: str | None = None
    error_code: str | None = None
    tool_response_status: str | None = None
    duration_ms: int | None = None
    is_interrupt: bool | None = None
    prompt_length: int | None = None
    tool_input_bytes: int | None = None
    tool_response_bytes: int | None = None


def hook_event_row(event: HookEvent, *, seq: int, received_at_ms: int) -> HookEventRow:
    """The row to store for ``event``, received at ``received_at_ms`` as number ``seq``."""
    row = HookEventRow(
        seq=seq,
        session_id=event.session_id,
        received_at_ms=received_at_ms,
        hook_event_name=event.hook_event_name,
        agent_id=event.agent_id,
        agent_type=event.agent_type,
        prompt_id=event.prompt_id,
    )
    match event:
        case ToolEvent():
            return _tool_row(row, event)
        case UserPromptSubmit():
            return replace(row, prompt_length=event.prompt_length)
        case SessionStart():
            return replace(row, start_source=event.source)
        case SessionEnd():
            return replace(row, end_reason=event.reason)
        case StopFailure():
            return replace(row, error_code=event.error)
        case PreCompact() | PostCompact():
            return replace(row, compact_trigger=event.trigger)
        case Notification():
            return replace(row, notification_type=event.notification_type)
        case Elicitation():
            return replace(
                row, mcp_server_name=event.mcp_server_name, elicitation_id=event.elicitation_id
            )
        case ElicitationResult():
            return replace(
                row,
                mcp_server_name=event.mcp_server_name,
                elicitation_id=event.elicitation_id,
                elicitation_action=event.action,
            )
        case _:
            return row


def _tool_row(row: HookEventRow, event: ToolEvent) -> HookEventRow:
    row = replace(
        row,
        tool_name=event.tool_name,
        tool_use_id=event.tool_use_id,
        mcp_server_name=event.mcp_server_name,
        tool_input_bytes=event.tool_input_bytes,
    )
    match event:
        case PostToolUse():
            return replace(
                row,
                duration_ms=event.duration_ms,
                tool_response_bytes=event.tool_response_bytes,
                tool_response_status=event.tool_response_status,
            )
        case PostToolUseFailure():
            return replace(row, duration_ms=event.duration_ms, is_interrupt=event.is_interrupt)
        case _:
            return row


@dataclass(frozen=True, kw_only=True)
class Applied:
    """One reducer call to persist: the event's row (``None`` for the sweep and the
    telemetry join, which have no hook event), its effects and the new state."""

    event_row: HookEventRow | None
    effects: Sequence[Effect]
    state: SessionState


@dataclass(frozen=True, kw_only=True)
class AlertRecord:
    """An ``alerts`` row (ADR 0042): an alert that fired, ``session_id`` ``None`` for
    ``daily_cost``, and its Discord delivery."""

    id: int
    rule: AlertRule
    subject: str
    session_id: str | None
    fired_at_ms: int
    cleared_at_ms: int | None
    value: float
    threshold: float
    discord_state: DiscordState
    discord_attempts: int
    discord_next_at_ms: int | None


@dataclass(frozen=True, kw_only=True)
class SessionView:
    host: str
    session: Session


@dataclass(frozen=True, kw_only=True)
class SessionDetail:
    """A session, bounded (ADR 0033): all its running agents and open blocks, plus its
    latest ``DETAIL_CLOSED_LIMIT`` stopped agents and closed blocks. Agents are in the
    order they were first seen, blocks in ``block_id`` order, as in their pages.
    ``agents_total`` and ``blocks_total`` count every one stored for the session;
    ``parents`` maps each agent listed to the agent whose call launched it (``_parents``).
    """

    host: str
    session: Session
    agents: list[Agent]
    blocks: list[Block]
    agents_total: int
    blocks_total: int
    parents: Mapping[str, str | None]


@dataclass(frozen=True, kw_only=True)
class AgentPage:
    """A page of agents, in the order they were first seen, with their ``parents`` as in
    ``SessionDetail``."""

    agents: list[Agent]
    parents: Mapping[str, str | None]


@dataclass(frozen=True, kw_only=True)
class SessionCounts:
    """How many sessions each stored status has (statuses with none are absent), and how
    many blocks are open, in every session: what ``ccobs status`` shows (ADR 0038)."""

    by_state: dict[str, int]
    open_blocks: int


@dataclass(frozen=True, kw_only=True)
class StripRow:
    """What a strip of the web UI needs of a session: the session, how many of its agents
    are running and not internal, and its oldest open block (by opening time), if any."""

    session: Session
    running_subagents: int
    oldest_open_block: Block | None


@dataclass(frozen=True, kw_only=True)
class BoardRows:
    """The overview's sessions: every live one (``LIVE_STATUSES``), and the latest ended
    and stale ones, capped, with how many there are in all. ``any_session`` says whether
    the database holds a session at all, old ones included."""

    live: list[StripRow]
    ended: list[StripRow]
    ended_total: int
    stale: list[StripRow]
    stale_total: int
    any_session: bool


# Record <-> row.


def _session_values(host: str, session: Session) -> _Values:
    return {**asdict(session), "host": host}


def _agent_values(host: str, session_id: str, agent: Agent) -> _Values:
    return {**asdict(agent), "session_id": session_id, "host": host}


def _block_values(host: str, session_id: str, block: Block) -> _Values:
    values = {_BLOCK_COLUMNS[key]: value for key, value in asdict(block).items()}
    return {**values, "session_id": session_id, "host": host}


def _state_values(host: str, state: SessionState) -> _Values:
    return {
        "session_id": state.session.session_id,
        "host": host,
        "agent_order": [agent.agent_id for agent in state.agents],
        "pending_tools": [asdict(pending) for pending in state.pending_tools],
        "completed_tool_use_ids": list(state.completed_tool_use_ids),
        "stopped_agent_ids": list(state.stopped_agent_ids),
        "async_launches": list(state.async_launches),
    }


# The rows were written from the reducer's typed records, so the values have their types.


def _session(row: RowMapping) -> Session:
    return Session(**{name: row[name] for name in _SESSION_FIELDS})


def _agent(row: RowMapping) -> Agent:
    return Agent(**{name: row[name] for name in _AGENT_FIELDS})


def _block(row: RowMapping) -> Block:
    return Block(**{name: row[column] for name, column in _BLOCK_COLUMNS.items()})


def _upsert(
    connection: Connection, model: type[Base], keys: tuple[str, ...], rows: Iterable[_Values]
) -> None:
    rows = list(rows)
    if not rows:
        return
    statement = sqlite_insert(model)
    statement = statement.on_conflict_do_update(
        index_elements=list(keys),
        set_={
            column.name: statement.excluded[column.name]
            for column in model.__table__.columns
            if column.name not in keys
        },
    )
    connection.execute(statement, rows)


class Repository:
    """The app's database. One writer (the app's background task) calls the ``apply_*``
    methods; readers may run concurrently (WAL)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        # Rows a load could not use: a running agent missing from its table or from
        # agent_order, or a session without its session_state row. Never expected; the
        # writer may log or expose the count (it holds no values).
        self.skipped_rows = 0

    # Writes.

    def apply_effects(
        self,
        host: str,
        event_row: HookEventRow | None,
        effects: Sequence[Effect],
        session_state: SessionState,
    ) -> None:
        """Persist one reducer call in one transaction."""
        self.apply_batch(host, [Applied(event_row=event_row, effects=effects, state=session_state)])

    def apply_batch(self, host: str, items: Sequence[Applied]) -> None:
        """Persist several reducer calls in one transaction: all of it or nothing.

        Each session, agent, block and state is written once, in its last version. The
        session row comes from the state, which always holds the latest ``Session``.
        """
        sessions: dict[str, _Values] = {}
        states: dict[str, _Values] = {}
        agents: dict[tuple[str, str], _Values] = {}
        blocks: dict[tuple[str, int], _Values] = {}
        events: list[_Values] = []
        for item in items:
            session_id = item.state.session.session_id
            sessions[session_id] = _session_values(host, item.state.session)
            states[session_id] = _state_values(host, item.state)
            if item.event_row is not None:
                events.append({**asdict(item.event_row), "host": host})
            for effect in item.effects:
                if isinstance(effect, AgentChanged):
                    key = (session_id, effect.agent.agent_id)
                    agents[key] = _agent_values(host, session_id, effect.agent)
                elif isinstance(effect, BlockOpened | BlockNotified | BlockClosed):
                    blocks[session_id, effect.block.block_id] = _block_values(
                        host, session_id, effect.block
                    )
        if not sessions:
            return
        with self.engine.begin() as connection:
            # Sessions first: every other table refers to them.
            _upsert(connection, SessionModel, ("session_id",), sessions.values())
            if events:
                connection.execute(insert(HookEventModel), events)
            _upsert(connection, AgentModel, ("session_id", "agent_id"), agents.values())
            _upsert(connection, BlockModel, ("session_id", "block_id"), blocks.values())
            _upsert(connection, SessionStateModel, ("session_id",), states.values())

    def delete_older_than(self, cutoff_ms: int) -> int:
        """Delete the ended and stale sessions whose last event is before ``cutoff_ms``,
        with their agents, blocks, events, state and alerts (cascade), and the cleared
        ``daily_cost`` alerts that fired before it, which have no session (ADR 0042).
        Returns how many sessions went.

        A session still open is never deleted, so the writer's in-memory state cannot
        outlive its rows. Retention is never shorter than the stale threshold, so the
        sweep has made every session that old stale by then. A daily alert still open is
        kept for the same reason: the evaluator rebuilds its key from it.
        """
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(SessionModel).where(
                    SessionModel.last_event_at_ms < cutoff_ms,
                    SessionModel.status.in_(_CLOSED),
                )
            )
            connection.execute(
                delete(AlertModel).where(
                    AlertModel.session_id.is_(None),
                    AlertModel.fired_at_ms < cutoff_ms,
                    AlertModel.cleared_at_ms.is_not(None),
                )
            )
            return result.rowcount

    # Alerts (ADR 0042). Writes on the writer's engine only.

    def insert_alert(
        self,
        host: str,
        fired: AlertFired,
        *,
        discord_state: DiscordState,
        discord_next_at_ms: int | None = None,
    ) -> int:
        """Store an alert that fired and return its ``id``, which grows with every alert
        and is never reused. A ``daily_cost`` alert has no session; any other refers to
        its session, which must be stored (``IntegrityError`` otherwise), and goes with
        it. ``discord_next_at_ms`` is required for a ``pending`` delivery."""
        values = {
            "host": host,
            "rule": fired.rule,
            "session_id": None if fired.rule == _DAILY_COST else fired.subject,
            "subject": fired.subject,
            "fired_at_ms": fired.fired_at_ms,
            "value": fired.value,
            "threshold": fired.threshold,
            "discord_state": discord_state,
            "discord_next_at_ms": discord_next_at_ms,
        }
        with self.engine.begin() as connection:
            statement = insert(AlertModel).values(values).returning(AlertModel.id)
            return connection.execute(statement).scalar_one()

    def clear_alert(self, host: str, cleared: AlertCleared) -> int:
        """Clear the open alerts of ``cleared``'s key at its time, never earlier than
        their fire; returns how many (0 or 1 when every fire went through the
        evaluator, which never fires a key twice before it clears)."""
        statement = (
            update(AlertModel)
            .where(*_open_key(host, cleared.rule, cleared.subject))
            .values(cleared_at_ms=func.max(cleared.cleared_at_ms, AlertModel.fired_at_ms))
        )
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount

    def set_delivery(
        self,
        host: str,
        alert_id: int,
        *,
        state: DiscordState,
        attempts: int,
        next_at_ms: int | None = None,
    ) -> bool:
        """Record where an alert's Discord delivery stands: its state, how many attempts
        were made and, while ``pending``, when the next is due (``IntegrityError``
        without it). False when the host has no such alert."""
        statement = (
            update(AlertModel)
            .where(AlertModel.host == host, AlertModel.id == alert_id)
            .values(discord_state=state, discord_attempts=attempts, discord_next_at_ms=next_at_ms)
        )
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount == 1

    def list_alerts(self, host: str, limit: int, before_id: int | None = None) -> list[AlertRecord]:
        """The host's ``limit`` latest alerts, at most ``ALERT_PAGE_MAX``, newest first,
        starting just below ``before_id`` (exclusive) when given: a keyset page of the
        primary key, the same however many alerts fire later."""
        statement = select(AlertModel.__table__).where(AlertModel.host == host)
        if before_id is not None:
            statement = statement.where(AlertModel.id < before_id)
        statement = statement.order_by(AlertModel.id.desc()).limit(_capped(limit, ALERT_PAGE_MAX))
        with self.engine.connect() as connection:
            return [_alert(row) for row in connection.execute(statement).mappings()]

    def due_deliveries(self, host: str, now_ms: int, limit: int) -> list[AlertRecord]:
        """The host's ``pending`` Discord deliveries due at ``now_ms``, the earliest due
        first (then by ``id``), at most ``limit`` and ``DELIVERY_BATCH_MAX``: a range of
        the partial index on pending deliveries."""
        statement = (
            select(AlertModel.__table__)
            .where(
                AlertModel.host == host,
                AlertModel.discord_state == _PENDING,
                AlertModel.discord_next_at_ms <= now_ms,
            )
            .order_by(AlertModel.discord_next_at_ms, AlertModel.id)
            .limit(_capped(limit, DELIVERY_BATCH_MAX))
        )
        with self.engine.connect() as connection:
            return [_alert(row) for row in connection.execute(statement).mappings()]

    def alert_keys(self, host: str, *, cooldown_since_ms: int) -> dict[AlertKey, KeyState]:
        """The evaluators' ``AlertState.keys`` after a restart (ADR 0041): every key with
        an alert still open, firing, and every key that fired at or after
        ``cooldown_since_ms`` (now minus the cooldown), whose cooldown still runs. Each
        with its latest fire."""
        open_now = case((AlertModel.cleared_at_ms.is_(None), 1), else_=0)
        statement = (
            select(
                AlertModel.rule,
                AlertModel.subject,
                func.max(AlertModel.fired_at_ms),
                func.max(open_now),
            )
            .where(
                AlertModel.host == host,
                or_(
                    AlertModel.cleared_at_ms.is_(None),
                    AlertModel.fired_at_ms >= cooldown_since_ms,
                ),
            )
            .group_by(AlertModel.rule, AlertModel.subject)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return {
            AlertKey(rule=_RULES[rule], subject=subject): KeyState(
                last_fired_at_ms=int(last_fired), firing=bool(firing)
            )
            for rule, subject, last_fired, firing in rows
        }

    def failure_windows(
        self, host: str, *, since_ms: int, count: int
    ) -> dict[str, tuple[int, ...]]:
        """The ``repeated_failures`` windows after a restart, as
        ``record_failure_event`` and ``evaluate_repeated_failures`` would hold them
        (ADR 0041), from the stored hook events.

        For each session that has not ended: the receive times of its ``StopFailure``
        events after (in ``seq``, the order the writer processed them) its last
        main-thread ``Stop`` or ``SessionEnd``, at or after ``since_ms`` (now minus the
        window), the newest ``count`` of them, sorted. Sessions with none are absent.

        It rebuilds them whether or not ``repeated_failures`` is enabled, unlike
        ``record_failure_event``, which records nothing while the rule is off: the caller
        (the writer at start-up) drops the rebuilt windows when the rule is disabled.

        A ``StopFailure`` counts when no reset follows it in its session: ``NOT EXISTS``
        on the ``(session_id, seq)`` index reads only the events after the failure and
        stops at the first reset, where a ``max(seq)`` of the resets read the session's
        whole history for every failure.
        """
        reset = aliased(HookEventModel)
        reset_after = (
            select(1)
            .where(
                reset.session_id == HookEventModel.session_id,
                reset.seq > HookEventModel.seq,
                or_(
                    and_(reset.hook_event_name == _STOP, reset.agent_id.is_(None)),
                    reset.hook_event_name == _SESSION_END,
                ),
            )
            .exists()
        )
        rank = (
            func.row_number()
            .over(
                partition_by=HookEventModel.session_id,
                order_by=(HookEventModel.received_at_ms.desc(), HookEventModel.seq.desc()),
            )
            .label("rank")
        )
        ranked = (
            select(HookEventModel.session_id, HookEventModel.received_at_ms, rank)
            .join(SessionModel, SessionModel.session_id == HookEventModel.session_id)
            .where(
                HookEventModel.host == host,
                SessionModel.status != _ENDED,
                HookEventModel.hook_event_name == _STOP_FAILURE,
                HookEventModel.received_at_ms >= since_ms,
                ~reset_after,
            )
            .subquery()
        )
        statement = (
            select(ranked.c.session_id, ranked.c.received_at_ms)
            .where(ranked.c.rank <= count)
            .order_by(ranked.c.session_id, ranked.c.received_at_ms)
        )
        windows: dict[str, list[int]] = {}
        with self.engine.connect() as connection:
            for session_id, received_at_ms in connection.execute(statement):
                windows.setdefault(session_id, []).append(received_at_ms)
        return {session_id: tuple(times) for session_id, times in windows.items()}

    # Alert settings (ADR 0042).

    def alert_settings(self, host: str) -> dict[str, AlertSettingValue]:
        """The host's saved overrides, by flat setting name, each with the type it was
        saved with. A row that is not an allowlisted setting of the right type (a file
        edited by hand) is ignored, and counted in a log line without names or values.
        Bounds are the caller's to check, with ``validate_config`` (ADR 0042)."""
        statement = select(AlertSettingModel.field, AlertSettingModel.value).where(
            AlertSettingModel.host == host
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        settings: dict[str, AlertSettingValue] = {}
        for field, stored in rows:
            value = _decoded_setting(field, stored)
            if value is not None:
                settings[field] = value
        if ignored := len(rows) - len(settings):
            _log.warning("ignored %d invalid saved alert settings", ignored)
        return settings

    def save_alert_setting(
        self, host: str, field: str, value: AlertSettingValue, *, updated_at_ms: int
    ) -> None:
        """Save one override, replacing the one saved before. ``ValueError`` for a name
        outside ``ALERT_SETTING_TYPES`` or a value of the wrong type; the message never
        holds the value, nor an unknown name."""
        kind = ALERT_SETTING_TYPES.get(field)
        if kind is None:
            raise ValueError("not an alert setting")
        if not _typed(kind, value):
            raise ValueError(f"{field} must be {_KIND_NAMES[kind]}")
        row = {
            "host": host,
            "field": field,
            "value": json.dumps(value, allow_nan=False),
            "updated_at_ms": updated_at_ms,
        }
        with self.engine.begin() as connection:
            _upsert(connection, AlertSettingModel, ("host", "field"), [row])

    def reset_alert_setting(self, host: str, field: str) -> bool:
        """Delete one override (Reset): the setting falls back to the environment's
        value, then the built-in default. False when none was saved."""
        statement = delete(AlertSettingModel).where(
            AlertSettingModel.host == host, AlertSettingModel.field == field
        )
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount == 1

    def reset_alert_settings(self, host: str) -> int:
        """Delete every override of the host; returns how many."""
        statement = delete(AlertSettingModel).where(AlertSettingModel.host == host)
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount

    # State after a restart.

    def load_open_states(self) -> dict[str, SessionState]:
        """The exact state of every session that has not ended."""
        with self.engine.connect() as connection:
            states, skipped = _load_states(connection, SessionModel.status != _ENDED)
        self._skipped(skipped)
        return states

    def load_state(self, session_id: str) -> SessionState | None:
        """The exact state of one session, ended or not; ``None`` if it is not stored."""
        with self.engine.connect() as connection:
            states, skipped = _load_states(connection, SessionModel.session_id == session_id)
        self._skipped(skipped)
        return states.get(session_id)

    def _skipped(self, count: int) -> None:
        if count:
            self.skipped_rows += count
            _log.warning("rebuilt session state without %d inconsistent rows", count)

    def max_seq(self) -> int:
        """The highest ``seq`` stored, 0 for an empty database."""
        with self.engine.connect() as connection:
            statement = select(func.coalesce(func.max(HookEventModel.seq), 0))
            return int(connection.execute(statement).scalar_one())

    # Reads for the API.

    def list_sessions(self, limit: int, status: str | None = None) -> list[SessionView]:
        """The sessions with the latest activity first, optionally of one status."""
        statement = select(SessionModel.__table__).order_by(
            SessionModel.last_event_at_ms.desc(), SessionModel.session_id
        )
        if status is not None:
            statement = statement.where(SessionModel.status == status)
        with self.engine.connect() as connection:
            rows = connection.execute(statement.limit(limit)).mappings()
            return [SessionView(host=row["host"], session=_session(row)) for row in rows]

    def session_exists(self, session_id: str) -> bool:
        with self.engine.connect() as connection:
            statement = select(SessionModel.session_id).where(SessionModel.session_id == session_id)
            return connection.execute(statement).first() is not None

    def find_session(self, session_id: str) -> Session | None:
        """The session's row alone, without its agents, blocks or counts; ``None`` if it
        is not stored (the session page's tokens and cost need only its times)."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(SessionModel.__table__).where(SessionModel.session_id == session_id)
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else _session(row)

    def get_session(
        self,
        session_id: str,
        *,
        closed_limit: int = DETAIL_CLOSED_LIMIT,
        blocks_from: int | None = None,
        agents_from: str | None = None,
        blocks_since_ms: int | None = None,
        agents_since_ms: int | None = None,
        keep_blocks: Collection[int] = (),
        keep_agents: Collection[str] = (),
    ) -> SessionDetail | None:
        """The bounded detail of a session (``SessionDetail``); ``None`` if it is not
        stored. One connection, so every part comes from the same read transaction.

        By default the detail holds the latest ``closed_limit`` closed blocks and stopped
        agents. The live refresh of a session page (ADR 0032) narrows them to a window:
        with ``blocks_from``, only the closed blocks from that ``block_id`` up, plus those
        closed at or after ``blocks_since_ms`` and those in ``keep_blocks``; with
        ``agents_from``, only the stopped agents from that agent's position up (first
        seen, ``agent_id``, as ``list_agents`` pages), plus those stopped at or after
        ``agents_since_ms`` and those in ``keep_agents``. An ``agents_from`` that names no
        agent of the session narrows nothing. Either way, at most the latest
        ``closed_limit`` of them, by ``block_id`` and by position.
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(SessionModel.__table__).where(SessionModel.session_id == session_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            of_session = AgentModel.session_id == session_id
            stopped_filter = [of_session, AgentModel.status != _RUNNING]
            if agents_from is not None:
                position = connection.execute(
                    select(_AGENT_FIRST_SEEN).where(of_session, AgentModel.agent_id == agents_from)
                ).one_or_none()
                if position is not None:
                    window = tuple_(_AGENT_FIRST_SEEN, AgentModel.agent_id) >= tuple_(
                        position[0], agents_from
                    )
                    stopped_filter.append(
                        _widened(
                            window,
                            AgentModel.stopped_at_ms,
                            agents_since_ms,
                            AgentModel.agent_id,
                            keep_agents,
                        )
                    )
            latest_stopped = (
                select(AgentModel.agent_id)
                .where(*stopped_filter)
                .order_by(_AGENT_FIRST_SEEN.desc(), AgentModel.agent_id.desc())
                .limit(closed_limit)
            )
            agents = [
                _agent(agent)
                for agent in connection.execute(
                    select(AgentModel.__table__)
                    .where(
                        of_session,
                        or_(AgentModel.status == _RUNNING, AgentModel.agent_id.in_(latest_stopped)),
                    )
                    .order_by(_AGENT_FIRST_SEEN, AgentModel.agent_id)
                ).mappings()
            ]
            in_session = BlockModel.session_id == session_id
            closed_filter = [in_session, BlockModel.closed_at_ms.is_not(None)]
            if blocks_from is not None:
                closed_filter.append(
                    _widened(
                        BlockModel.block_id >= blocks_from,
                        BlockModel.closed_at_ms,
                        blocks_since_ms,
                        BlockModel.block_id,
                        keep_blocks,
                    )
                )
            latest_closed = (
                select(BlockModel.block_id)
                .where(*closed_filter)
                .order_by(BlockModel.block_id.desc())
                .limit(closed_limit)
            )
            blocks = connection.execute(
                select(BlockModel.__table__)
                .where(
                    in_session,
                    or_(BlockModel.closed_at_ms.is_(None), BlockModel.block_id.in_(latest_closed)),
                )
                .order_by(BlockModel.block_id)
            ).mappings()
            agents_total = connection.execute(
                select(func.count()).select_from(AgentModel).where(of_session)
            ).scalar_one()
            blocks_total = connection.execute(
                select(func.count()).select_from(BlockModel).where(in_session)
            ).scalar_one()
            return SessionDetail(
                host=row["host"],
                session=_session(row),
                agents=agents,
                blocks=[_block(block) for block in blocks],
                agents_total=agents_total,
                blocks_total=blocks_total,
                parents=_parents(connection, session_id, agents),
            )

    def list_blocks(self, session_id: str, limit: int, before: int | None = None) -> list[Block]:
        """The ``limit`` blocks of a session just below ``block_id`` ``before``
        (exclusive), or its latest without it, open or closed, in ``block_id`` order.

        ``block_id`` is the ``seq`` of the event that opened the block: it never changes
        and grows with every new block, so a page read from the primary key stays the same
        however many blocks open later.
        """
        statement = select(BlockModel.__table__).where(BlockModel.session_id == session_id)
        if before is not None:
            statement = statement.where(BlockModel.block_id < before)
        with self.engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(BlockModel.block_id.desc()).limit(limit)
            ).mappings()
            return [_block(row) for row in reversed(rows.all())]

    def list_agents(
        self, session_id: str, limit: int, before: str | None = None
    ) -> AgentPage | None:
        """The ``limit`` agents of a session first seen just before the agent ``before``
        (exclusive), or its latest without it, running or stopped, in the order they were
        first seen. ``None`` if ``before`` names no agent of the session.

        Agents have no monotonic column, so the cursor is an ``agent_id`` and the page
        starts below its position, (first seen, ``agent_id``), which is set when the row is
        created (see ``_AGENT_FIRST_SEEN`` for the one exception): pages stay the same
        however many agents start later.
        """
        of_session = AgentModel.session_id == session_id
        statement = select(AgentModel.__table__).where(of_session)
        with self.engine.connect() as connection:
            if before is not None:
                position = connection.execute(
                    select(_AGENT_FIRST_SEEN).where(of_session, AgentModel.agent_id == before)
                ).one_or_none()
                if position is None:
                    return None
                statement = statement.where(
                    tuple_(_AGENT_FIRST_SEEN, AgentModel.agent_id) < tuple_(position[0], before)
                )
            newest_first = statement.order_by(
                _AGENT_FIRST_SEEN.desc(), AgentModel.agent_id.desc()
            ).limit(limit)
            rows = connection.execute(newest_first).mappings().all()
            agents = [_agent(row) for row in reversed(rows)]
            return AgentPage(agents=agents, parents=_parents(connection, session_id, agents))

    def launch_scopes(self, session_id: str, tool_use_ids: Iterable[str]) -> dict[str, str | None]:
        """For each of ``tool_use_ids`` with a stored ``PreToolUse`` in the session, the
        scope that made the call: an ``agent_id``, or ``None`` for the main thread."""
        with self.engine.connect() as connection:
            return _launch_scopes(connection, session_id, tool_use_ids)

    def list_events(
        self,
        session_id: str,
        limit: int,
        *,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[HookEventRow]:
        """``limit`` events of a session, always in ``seq`` order: its latest, the ones just
        below ``before_seq`` or the ones just above ``after_seq`` (both exclusive, never
        together). Every page is a range of the ``(session_id, seq)`` index."""
        if before_seq is not None and after_seq is not None:
            raise ValueError("page before a seq or after one, not both")
        statement = select(HookEventModel.__table__).where(HookEventModel.session_id == session_id)
        if after_seq is not None:
            statement = statement.where(HookEventModel.seq > after_seq)
            statement = statement.order_by(HookEventModel.seq)
        else:
            if before_seq is not None:
                statement = statement.where(HookEventModel.seq < before_seq)
            statement = statement.order_by(HookEventModel.seq.desc())
        names = tuple(f.name for f in fields(HookEventRow))
        with self.engine.connect() as connection:
            rows = connection.execute(statement.limit(limit)).mappings().all()
        ordered = rows if after_seq is not None else reversed(rows)
        return [HookEventRow(**{name: row[name] for name in names}) for row in ordered]

    # Reads for the diagnostics route (ADR 0038).

    def session_counts(self) -> SessionCounts:
        """The sessions by status and the open blocks: two aggregates on one connection,
        so one read transaction, bounded by the number of statuses."""
        with self.engine.connect() as connection:
            by_state = connection.execute(
                select(SessionModel.status, func.count()).group_by(SessionModel.status)
            ).all()
            open_blocks = connection.execute(
                select(func.count())
                .select_from(BlockModel)
                .where(BlockModel.closed_at_ms.is_(None))
            ).scalar_one()
        return SessionCounts(
            by_state={str(status): int(count) for status, count in by_state},
            open_blocks=int(open_blocks),
        )

    # Reads for the web UI.

    def board(self, *, ended_since_ms: int, closed_limit: int) -> BoardRows:
        """The overview's sessions (``BoardRows``): the live ones, the ``closed_limit``
        latest that ended at or after ``ended_since_ms`` and the ``closed_limit`` stale
        ones with the latest activity. One connection, so one read transaction."""
        table = SessionModel.__table__
        ended_filter = (
            SessionModel.status == _ENDED,
            SessionModel.ended_at_ms >= ended_since_ms,
        )
        stale_filter = SessionModel.status == _STALE
        # Ties go by session ID, as on the board (presentation.board_sort_key).
        latest = (SessionModel.last_event_at_ms.desc(), SessionModel.session_id)
        with self.engine.connect() as connection:
            live = connection.execute(
                select(table).where(SessionModel.status.in_(LIVE_STATUSES))
            ).mappings()
            live_rows = list(live)
            ended = connection.execute(
                select(table)
                .where(*ended_filter)
                .order_by(SessionModel.ended_at_ms.desc(), SessionModel.session_id)
                .limit(closed_limit)
            ).mappings()
            ended_rows = list(ended)
            stale = connection.execute(
                select(table).where(stale_filter).order_by(*latest).limit(closed_limit)
            ).mappings()
            stale_rows = list(stale)
            ended_total = connection.execute(
                select(func.count()).select_from(SessionModel).where(*ended_filter)
            ).scalar_one()
            stale_total = connection.execute(
                select(func.count()).select_from(SessionModel).where(stale_filter)
            ).scalar_one()
            any_session = bool(live_rows or ended_rows or stale_rows) or (
                connection.execute(select(SessionModel.session_id).limit(1)).first() is not None
            )
            strips = _strip_rows(connection, [*live_rows, *ended_rows, *stale_rows])
        live_count, ended_count = len(live_rows), len(ended_rows)
        return BoardRows(
            live=strips[:live_count],
            ended=strips[live_count : live_count + ended_count],
            ended_total=ended_total,
            stale=strips[live_count + ended_count :],
            stale_total=stale_total,
            any_session=any_session,
        )

    def session_page(self, limit: int, before: tuple[int, str] | None = None) -> list[StripRow]:
        """``limit`` sessions with the latest activity first, then the highest
        ``session_id``, starting just below ``before`` (``(last_event_at_ms,
        session_id)``, exclusive) when given. The cursor is a position, not a row: a
        session deleted since still pages, and one that moved up stays out of later pages.
        """
        statement = select(SessionModel.__table__)
        if before is not None:
            statement = statement.where(
                tuple_(SessionModel.last_event_at_ms, SessionModel.session_id)
                < tuple_(before[0], before[1])
            )
        statement = statement.order_by(
            SessionModel.last_event_at_ms.desc(), SessionModel.session_id.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return _strip_rows(connection, rows)


def _capped(limit: int, maximum: int) -> int:
    """``limit`` within 0 and ``maximum``: a read never returns more than its cap."""
    return max(0, min(limit, maximum))


def _open_key(host: str, rule: str, subject: str) -> tuple[ColumnElement[bool], ...]:
    """The open alerts of a key: a range of the partial index ``ix_alerts_open``."""
    return (
        AlertModel.host == host,
        AlertModel.rule == rule,
        AlertModel.subject == subject,
        AlertModel.cleared_at_ms.is_(None),
    )


def _alert(row: RowMapping) -> AlertRecord:
    # The CHECK constraints keep rule and discord_state to the typed values.
    return AlertRecord(
        id=row["id"],
        rule=_RULES[row["rule"]],
        subject=row["subject"],
        session_id=row["session_id"],
        fired_at_ms=row["fired_at_ms"],
        cleared_at_ms=row["cleared_at_ms"],
        value=row["value"],
        threshold=row["threshold"],
        discord_state=_DISCORD_STATES[row["discord_state"]],
        discord_attempts=row["discord_attempts"],
        discord_next_at_ms=row["discord_next_at_ms"],
    )


_KIND_NAMES: Final[Mapping[type, str]] = {
    bool: "true or false",
    int: "a whole number",
    float: "a finite number",
}


def _typed(kind: type, value: object) -> bool:
    """Whether ``value`` has the type of a setting of ``kind``: a bool for a switch, an
    int (never a bool) for a whole number, an int or a finite float (never a bool) for a
    dollar amount."""
    if kind is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if kind is int:
        return isinstance(value, int)
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _decoded_setting(field: str, stored: str) -> AlertSettingValue | None:
    """The value of a saved setting, or ``None`` when the row is not a valid one."""
    kind = ALERT_SETTING_TYPES.get(field)
    if kind is None:
        return None
    try:
        value: object = json.loads(stored)
    except ValueError:
        return None
    if not _typed(kind, value) or not isinstance(value, bool | int | float):
        return None
    return value


def _widened[K](
    window: ColumnElement[bool],
    ended_at: InstrumentedAttribute[int | None],
    since_ms: int | None,
    key: InstrumentedAttribute[K],
    keep: Collection[K],
) -> ColumnElement[bool]:
    """``window``, or closed (stopped) at or after ``since_ms`` when it is given, or one
    of ``keep``."""
    clauses = [window]
    if since_ms is not None:
        clauses.append(ended_at >= since_ms)
    if keep:
        clauses.append(key.in_(list(keep)))
    return or_(*clauses)


def _strip_rows(connection: Connection, rows: Sequence[RowMapping]) -> list[StripRow]:
    """The ``StripRow`` of each session row, in the same order: two more queries, one for
    the running non-internal agents and one for the open blocks of these sessions."""
    session_ids = [row["session_id"] for row in rows]
    if not session_ids:
        return []
    running: dict[str, int] = {}
    for session_id, count in connection.execute(
        select(AgentModel.session_id, func.count())
        .where(
            AgentModel.session_id.in_(session_ids),
            AgentModel.status == _RUNNING,
            AgentModel.internal.is_(False),
        )
        .group_by(AgentModel.session_id)
    ):
        running[session_id] = count
    oldest: dict[str, Block] = {}
    for row in connection.execute(
        select(BlockModel.__table__)
        .where(BlockModel.session_id.in_(session_ids), BlockModel.closed_at_ms.is_(None))
        .order_by(BlockModel.opened_at_ms, BlockModel.block_id)
    ).mappings():
        oldest.setdefault(row["session_id"], _block(row))
    return [
        StripRow(
            session=_session(row),
            running_subagents=running.get(row["session_id"], 0),
            oldest_open_block=oldest.get(row["session_id"]),
        )
        for row in rows
    ]


def _launch_scopes(
    connection: Connection, session_id: str, tool_use_ids: Iterable[str]
) -> dict[str, str | None]:
    """``Repository.launch_scopes`` on an open connection. A ``PreToolUse`` row keeps the
    call's ``tool_use_id`` and the ``agent_id`` of its scope (ADR 0023); if a call has
    more than one row, the first one counts."""
    wanted = set(tool_use_ids)
    if not wanted:
        return {}
    rows = connection.execute(
        select(HookEventModel.tool_use_id, HookEventModel.agent_id)
        .where(
            HookEventModel.session_id == session_id,
            HookEventModel.hook_event_name == _PRE_TOOL_USE,
            HookEventModel.tool_use_id.in_(wanted),
        )
        .order_by(HookEventModel.seq.desc())
    ).all()
    # Latest first, so the first row of a call is the one that stays. The filter on
    # wanted already excludes a NULL tool_use_id; the check only narrows the type.
    return {tool_use_id: agent_id for tool_use_id, agent_id in rows if tool_use_id is not None}


def _parents(
    connection: Connection, session_id: str, agents: Sequence[Agent]
) -> dict[str, str | None]:
    """Each agent's parent: the scope of the ``PreToolUse`` of the Agent call that
    launched it (``Agent.tool_use_id``, which the reducer links). ``None`` for the main
    thread, and also when the launch is unknown: no linked call, or no stored
    ``PreToolUse`` for it."""
    scopes = _launch_scopes(
        connection, session_id, (a.tool_use_id for a in agents if a.tool_use_id is not None)
    )
    return {
        agent.agent_id: None if agent.tool_use_id is None else scopes.get(agent.tool_use_id)
        for agent in agents
    }


def _load_states(
    connection: Connection, which: ColumnElement[bool]
) -> tuple[dict[str, SessionState], int]:
    """The states of the sessions ``which`` selects, and how many rows were skipped.

    One read transaction, so the tables are read at one point in time. A state is exact
    when the rows are consistent, as ``apply_batch`` writes them. Otherwise it is rebuilt
    as well as possible and the inconsistent rows are counted: a session without its
    ``session_state`` row gets empty bookkeeping, an ID in ``agent_order`` without a
    running agent row is dropped, and a running agent missing from ``agent_order`` joins
    at the end (in order of last event, as every one does without bookkeeping).
    """
    sessions = connection.execute(select(SessionModel.__table__).where(which)).mappings().all()
    if not sessions:
        return {}, 0
    chosen = select(SessionModel.session_id).where(which)
    bookkeeping = {
        row["session_id"]: row
        for row in connection.execute(
            select(SessionStateModel.__table__).where(SessionStateModel.session_id.in_(chosen))
        ).mappings()
    }
    running: dict[str, dict[str, Agent]] = {}
    for row in connection.execute(
        select(AgentModel.__table__)
        .where(AgentModel.session_id.in_(chosen), AgentModel.status == _RUNNING)
        .order_by(AgentModel.last_event_at_ms, AgentModel.agent_id)
    ).mappings():
        running.setdefault(row["session_id"], {})[row["agent_id"]] = _agent(row)
    open_blocks: dict[str, list[Block]] = {}
    for row in connection.execute(
        select(BlockModel.__table__)
        .where(BlockModel.session_id.in_(chosen), BlockModel.closed_at_ms.is_(None))
        .order_by(BlockModel.block_id)
    ).mappings():
        open_blocks.setdefault(row["session_id"], []).append(_block(row))

    skipped = 0
    states: dict[str, SessionState] = {}
    for row in sessions:
        session_id = row["session_id"]
        found = bookkeeping.get(session_id)
        extra = _NO_BOOKKEEPING if found is None else {key: found[key] for key in _NO_BOOKKEEPING}
        skipped += found is None
        agents = running.get(session_id, {})
        order = [agent_id for agent_id in extra["agent_order"] if agent_id in agents]
        unlisted = [agent_id for agent_id in agents if agent_id not in order]
        # Without bookkeeping every agent is unlisted, and the missing row counts once.
        skipped += len(extra["agent_order"]) - len(order) + (len(unlisted) if found else 0)
        states[session_id] = SessionState(
            session=_session(row),
            agents=tuple(agents[agent_id] for agent_id in [*order, *unlisted]),
            stopped_agent_ids=tuple(extra["stopped_agent_ids"]),
            open_blocks=tuple(open_blocks.get(session_id, ())),
            pending_tools=tuple(PendingTool(**pending) for pending in extra["pending_tools"]),
            completed_tool_use_ids=tuple(extra["completed_tool_use_ids"]),
            async_launches=tuple(extra["async_launches"]),
        )
    return states, skipped
