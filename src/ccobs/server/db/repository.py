"""Reads and writes of the app's database (ADR 0026).

The writer persists what ``ccobs.domain.sessions`` returns: for each hook event, its
metadata row, the records its effects carry and the new ``SessionState``, all in one
transaction. A batch of events is one transaction too, with each record coalesced so the
last version wins. After a restart, ``load_open_states`` rebuilds the exact states the
writer had in memory: the session, its running agents and open blocks come from their
tables, and the bookkeeping no table holds comes from ``session_state``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    RowMapping,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

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
    AgentModel,
    Base,
    BlockModel,
    HookEventModel,
    SessionModel,
    SessionStateModel,
)

__all__ = [
    "Applied",
    "HookEventRow",
    "Repository",
    "SessionDetail",
    "SessionView",
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
class SessionView:
    host: str
    session: Session


@dataclass(frozen=True, kw_only=True)
class SessionDetail:
    """A session with every agent and block it ever had, stopped and closed ones too."""

    host: str
    session: Session
    agents: list[Agent]
    blocks: list[Block]


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
        with their agents, blocks, events and state (cascade). Returns how many went.

        A session still open is never deleted, so the writer's in-memory state cannot
        outlive its rows. Retention is never shorter than the stale threshold, so the
        sweep has made every session that old stale by then.
        """
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(SessionModel).where(
                    SessionModel.last_event_at_ms < cutoff_ms,
                    SessionModel.status.in_(_CLOSED),
                )
            )
            return result.rowcount

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

    def get_session(self, session_id: str) -> SessionDetail | None:
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
            agents = connection.execute(
                select(AgentModel.__table__)
                .where(AgentModel.session_id == session_id)
                .order_by(AgentModel.last_event_at_ms, AgentModel.agent_id)
            ).mappings()
            blocks = connection.execute(
                select(BlockModel.__table__)
                .where(BlockModel.session_id == session_id)
                .order_by(BlockModel.block_id)
            ).mappings()
            return SessionDetail(
                host=row["host"],
                session=_session(row),
                agents=[_agent(agent) for agent in agents],
                blocks=[_block(block) for block in blocks],
            )

    def list_events(self, session_id: str, limit: int) -> list[HookEventRow]:
        """The latest ``limit`` events of a session, in ``seq`` order."""
        statement = (
            select(HookEventModel.__table__)
            .where(HookEventModel.session_id == session_id)
            .order_by(HookEventModel.seq.desc())
            .limit(limit)
        )
        names = tuple(f.name for f in fields(HookEventRow))
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [HookEventRow(**{name: row[name] for name in names}) for row in reversed(rows)]


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
