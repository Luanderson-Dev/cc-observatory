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
from collections.abc import Collection, Iterable, Mapping, Sequence
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
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import InstrumentedAttribute

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
    "DETAIL_CLOSED_LIMIT",
    "LIVE_STATUSES",
    "AgentPage",
    "Applied",
    "BoardRows",
    "HookEventRow",
    "Repository",
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
