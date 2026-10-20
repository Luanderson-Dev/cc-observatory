"""The app's read-only JSON API: sessions, their agents and blocks, their hook events, and
the alerts.

Every route reads the database through the readers' ``Repository``, on connections of
the query-only readers' engine (a WAL snapshot per read), and never the writer's memory:
the database is the source of truth, so a hook shows up once the writer commits its
batch, a few tens of milliseconds after it was received. The routes and their
dependencies are ``async def``, so FastAPI never puts them on its default threadpool:
each read runs on a worker thread of the read gate (``ccobs.server.reads``, ADR 0033),
which caps reads at the readers' connections and answers 503 when a read waits too long
for one. A read never blocks the event loop.

The responses are explicit models of stored metadata only (ADR 0023): every field is a
database column, times are integer UTC milliseconds, and nothing else is served.
``cwd`` is personal data (ADR 0006); it is served only to localhost, since every route
sits behind the ``Host`` guard and the app sends no CORS header (ADR 0027).

Request values never come back: a validation error lists where and why, without the
value FastAPI would echo, and an invalid or unknown session ID gets the same 404.

Each read is bounded (ADR 0033): the session detail holds every running agent and open
block but only the latest stopped and closed ones, with totals, and the lists that grow
with a session page by keyset, never by offset. Agents page by ``before`` (an
``agent_id``), blocks by ``before`` (a ``block_id``), and events by ``before_seq`` or
``after_seq``, never both; every cursor is exclusive and every page is in the lists'
usual order, oldest first.

Alerts (ADR 0044) page by ``id``, newest first like ADR 0042's ``list_alerts``: the
latest, or the ones just below ``before``. ``after`` returns the ones just above it,
oldest first, so a browser that keeps the last ``id`` it saw catches up in order, page
by page, without skipping one. An alert never carries ``cwd``: the session's project
name stands in for it, and its Discord delivery stays internal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum
from typing import Annotated, Final, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from ccobs.domain.hooks import is_valid_session_id
from ccobs.domain.presentation import project_name
from ccobs.domain.sessions import Agent, Block, SessionStatus
from ccobs.server.db.repository import (
    AgentPage,
    AlertView,
    HookEventRow,
    Repository,
    SessionDetail,
    SessionView,
)
from ccobs.server.reads import ReadGate

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_CURSOR",
    "MAX_LIMIT",
    "SESSION_STATUSES",
    "AgentOut",
    "AlertOut",
    "BlockOut",
    "EventOut",
    "SessionDetailOut",
    "SessionOut",
    "router",
    "validation_error",
]

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500
# The largest seq or block_id cursor: a larger integer would overflow SQLite's.
MAX_CURSOR: Final = 2**63 - 1
_MAX_ID_LENGTH: Final = 256
SESSION_STATUSES: Final[tuple[str, ...]] = get_args(SessionStatus.__value__)
_NOT_FOUND: Final = "session not found"
# What an alert is about (ADR 0044): a session, or the day (daily_cost).
_SESSION: Final = "session"
_DAY: Final = "day"


class _Out(BaseModel):
    # A field that is not declared here fails, so a response holds only these fields. A
    # validation error never quotes the value (it may be cwd or an ID), so uvicorn's
    # error log cannot carry it.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SessionOut(_Out):
    """A ``sessions`` row: the reducer's ``Session`` and the ``host`` label."""

    session_id: str
    host: str
    status: str
    started_at_ms: int
    last_event_at_ms: int
    last_seq: int
    turn_open: bool
    compacting: bool
    source: str | None
    model: str | None
    permission_mode: str | None
    effort_level: str | None
    last_prompt_id: str | None
    last_error_code: str | None
    idle_notified_at_ms: int | None
    ended_at_ms: int | None
    end_reason: str | None
    # Personal data (ADR 0006), served to localhost only.
    cwd: str | None

    @classmethod
    def of(cls, view: SessionView | SessionDetail) -> SessionOut:
        return cls(host=view.host, **asdict(view.session))


class AgentOut(_Out):
    """An ``agents`` row, running or stopped, and ``parent_agent_id``: the scope of the
    stored ``PreToolUse`` of the Agent call that launched it. ``None`` for the main
    thread, and also when the launch is unknown (``tool_use_id`` is ``None``, or that
    call's ``PreToolUse`` was not stored)."""

    agent_id: str
    status: str
    agent_type: str | None
    internal: bool
    background: bool
    tool_use_id: str | None
    started_at_ms: int | None
    stopped_at_ms: int | None
    last_event_at_ms: int
    stop_reason: str | None
    parent_agent_id: str | None

    @classmethod
    def of(cls, agent: Agent, parents: Mapping[str, str | None]) -> AgentOut:
        return cls(**asdict(agent), parent_agent_id=parents.get(agent.agent_id))


class BlockOut(_Out):
    """A ``blocks`` row, open or closed. ``scope_agent_id`` is ``None`` for the main
    thread."""

    block_id: int
    kind: str
    scope_agent_id: str | None
    tool_name: str | None
    tool_use_id: str | None
    mcp_server_name: str | None
    elicitation_id: str | None
    opened_at_ms: int
    notified_at_ms: int | None
    closed_at_ms: int | None
    close_reason: str | None
    close_source: str | None

    @classmethod
    def of(cls, block: Block) -> BlockOut:
        values = asdict(block)
        return cls(scope_agent_id=values.pop("scope"), **values)


class SessionDetailOut(SessionOut):
    """A session, bounded: every running agent and open block, the latest
    ``DETAIL_CLOSED_LIMIT`` stopped agents and closed blocks, and how many agents and
    blocks the session has in all. The others are on the paged routes."""

    agents: list[AgentOut]
    blocks: list[BlockOut]
    agents_total: int
    blocks_total: int


class EventOut(_Out):
    """A ``hook_events`` row: the metadata ADR 0023 keeps for one hook."""

    seq: int
    session_id: str
    received_at_ms: int
    hook_event_name: str
    agent_id: str | None
    agent_type: str | None
    prompt_id: str | None
    tool_name: str | None
    tool_use_id: str | None
    mcp_server_name: str | None
    notification_type: str | None
    elicitation_id: str | None
    elicitation_action: str | None
    compact_trigger: str | None
    start_source: str | None
    end_reason: str | None
    error_code: str | None
    tool_response_status: str | None
    duration_ms: int | None
    is_interrupt: bool | None
    prompt_length: int | None
    tool_input_bytes: int | None
    tool_response_bytes: int | None

    @classmethod
    def of(cls, row: HookEventRow) -> EventOut:
        return cls(**asdict(row))


class AlertOut(_Out):
    """An ``alerts`` row as the browser needs it (ADR 0044): what fired, about what and
    when, never its Discord delivery, which is internal. ``subject_kind`` is ``session``
    or ``day`` (``daily_cost``, with no session). ``project`` is the session's project
    name (``presentation.project_name``), never its ``cwd``; ``None`` for a daily alert
    or a session with no ``cwd``."""

    id: int
    rule: str
    subject_kind: str
    session_id: str | None
    project: str | None
    fired_at_ms: int
    cleared_at_ms: int | None
    value: float
    threshold: float

    @classmethod
    def of(cls, view: AlertView) -> AlertOut:
        alert = view.alert
        return cls(
            id=alert.id,
            rule=alert.rule,
            subject_kind=_DAY if alert.session_id is None else _SESSION,
            session_id=alert.session_id,
            project=project_name(view.cwd),
            fired_at_ms=alert.fired_at_ms,
            cleared_at_ms=alert.cleared_at_ms,
            value=alert.value,
            threshold=alert.threshold,
        )


# Async, like the routes: FastAPI runs a sync dependency on its default threadpool.
async def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.read_repository
    return repository


async def _reads(request: Request) -> ReadGate:
    reads: ReadGate = request.app.state.reads
    return reads


# The reads of the routes that need more than one query. Each runs as one call on the
# gate, so a request holds one slot, once: anyio refuses a second acquire from the task
# that holds a slot. None means the session is unknown.


def _blocks(
    repository: Repository, session_id: str, limit: int, before: int | None
) -> list[Block] | None:
    if not repository.session_exists(session_id):
        return None
    return repository.list_blocks(session_id, limit, before)


class _Unknown(Enum):
    SESSION = "session"
    CURSOR = "cursor"


def _agents(
    repository: Repository, session_id: str, limit: int, before: str | None
) -> AgentPage | _Unknown:
    if not repository.session_exists(session_id):
        return _Unknown.SESSION
    page = repository.list_agents(session_id, limit, before)
    return _Unknown.CURSOR if page is None else page


def _events(
    repository: Repository,
    session_id: str,
    limit: int,
    before_seq: int | None,
    after_seq: int | None,
) -> list[HookEventRow] | None:
    if not repository.session_exists(session_id):
        return None
    return repository.list_events(session_id, limit, before_seq=before_seq, after_seq=after_seq)


def _alerts(
    repository: Repository, host: str, limit: int, before: int | None, after: int | None
) -> list[AlertView]:
    return repository.alert_views(host, limit, before_id=before, after_id=after)


def _invalid(loc: tuple[str, ...], msg: str) -> RequestValidationError:
    """A 422 through ``validation_error``, like FastAPI's own: where and why, no value."""
    return RequestValidationError([{"loc": loc, "msg": msg, "type": "value_error"}])


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]
_Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
# seq and block_id cursors: anything SQLite's signed 64-bit integers hold.
_Seq = Annotated[int | None, Query(ge=0, le=MAX_CURSOR)]
# An agent_id cursor: what the hook parser accepts as an ID (ADR 0023).
_AgentCursor = Annotated[str | None, Query(min_length=1, max_length=_MAX_ID_LENGTH)]

router = APIRouter(prefix="/api/v1")


@router.get("/sessions")
async def list_sessions(
    repository: _Repo,
    reads: _Reads,
    status: SessionStatus | None = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[SessionOut]:
    """The sessions with the latest activity first, optionally of one status."""
    views = await reads.run(repository.list_sessions, limit, status)
    return [SessionOut.of(view) for view in views]


@router.get("/sessions/{session_id}")
async def get_session(repository: _Repo, reads: _Reads, session_id: str) -> SessionDetailOut:
    """One session, bounded: its running agents and open blocks, the latest stopped and
    closed ones, and their totals."""
    detail = None
    if is_valid_session_id(session_id):
        detail = await reads.run(repository.get_session, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return SessionDetailOut(
        **SessionOut.of(detail).model_dump(),
        agents=[AgentOut.of(agent, detail.parents) for agent in detail.agents],
        blocks=[BlockOut.of(block) for block in detail.blocks],
        agents_total=detail.agents_total,
        blocks_total=detail.blocks_total,
    )


@router.get("/sessions/{session_id}/agents")
async def list_agents(
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before: _AgentCursor = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[AgentOut]:
    """The session's ``limit`` agents first seen just before the agent ``before``, or its
    latest, oldest first."""
    if before is not None and not is_valid_session_id(before):
        raise _invalid(("query", "before"), "not an agent ID")
    page: AgentPage | _Unknown = _Unknown.SESSION
    if is_valid_session_id(session_id):
        page = await reads.run(_agents, repository, session_id, limit, before)
    if page is _Unknown.SESSION:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if page is _Unknown.CURSOR:
        raise _invalid(("query", "before"), "no agent of this session has this ID")
    return [AgentOut.of(agent, page.parents) for agent in page.agents]


@router.get("/sessions/{session_id}/blocks")
async def list_blocks(
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before: _Seq = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[BlockOut]:
    """The session's ``limit`` blocks just below ``block_id`` ``before``, or its latest,
    oldest first."""
    blocks = None
    if is_valid_session_id(session_id):
        blocks = await reads.run(_blocks, repository, session_id, limit, before)
    if blocks is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return [BlockOut.of(block) for block in blocks]


@router.get("/sessions/{session_id}/events")
async def list_events(
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before_seq: _Seq = None,
    after_seq: _Seq = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[EventOut]:
    """The session's ``limit`` hook events in ``seq`` order: its latest, the ones just
    below ``before_seq`` or the ones just above ``after_seq``."""
    if before_seq is not None and after_seq is not None:
        raise _invalid(("query",), "before_seq and after_seq cannot be used together")
    rows = None
    if is_valid_session_id(session_id):
        rows = await reads.run(_events, repository, session_id, limit, before_seq, after_seq)
    if rows is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return [EventOut.of(row) for row in rows]


@router.get("/alerts")
async def list_alerts(
    request: Request,
    repository: _Repo,
    reads: _Reads,
    before: _Seq = None,
    after: _Seq = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[AlertOut]:
    """The host's ``limit`` alerts by ``id``: its latest, or the ones just below
    ``before``, newest first; or the ones just above ``after``, oldest first."""
    if before is not None and after is not None:
        raise _invalid(("query",), "before and after cannot be used together")
    host: str = request.app.state.host_label
    views = await reads.run(_alerts, repository, host, limit, before, after)
    return [AlertOut.of(view) for view in views]


async def validation_error(_request: Request, error: RequestValidationError) -> Response:
    """FastAPI's 422 without the ``input`` and ``ctx`` of each error, which would echo the
    request's values."""
    detail = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in error.errors()]
    return JSONResponse({"detail": detail}, status_code=422)
