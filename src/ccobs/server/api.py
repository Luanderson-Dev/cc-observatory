"""The app's read-only JSON API: sessions, their agents and blocks, and their hook events.

Every route reads the database through the ``Repository``, on connections of its own
(a WAL snapshot per read), and never the writer's memory: the database is the source of
truth, so a hook shows up once the writer commits its batch, a few tens of milliseconds
after it was received. The routes are sync ``def``, so FastAPI runs them in its
threadpool and a read never blocks the event loop.

The responses are explicit models of stored metadata only (ADR 0023): every field is a
database column, times are integer UTC milliseconds, and nothing else is served.
``cwd`` is personal data (ADR 0006); it is served only to localhost, since every route
sits behind the ``Host`` guard and the app sends no CORS header (ADR 0027).

Request values never come back: a validation error lists where and why, without the
value FastAPI would echo, and an invalid or unknown session ID gets the same 404.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Final, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from ccobs.domain.hooks import is_valid_session_id
from ccobs.domain.sessions import Agent, Block, SessionStatus
from ccobs.server.db.repository import HookEventRow, Repository, SessionDetail, SessionView

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SESSION_STATUSES",
    "AgentOut",
    "BlockOut",
    "EventOut",
    "SessionDetailOut",
    "SessionOut",
    "router",
    "validation_error",
]

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500
SESSION_STATUSES: Final[tuple[str, ...]] = get_args(SessionStatus.__value__)
_NOT_FOUND: Final = "session not found"


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
    """An ``agents`` row, running or stopped."""

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

    @classmethod
    def of(cls, agent: Agent) -> AgentOut:
        return cls(**asdict(agent))


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
    """A session with every agent and block it ever had."""

    agents: list[AgentOut]
    blocks: list[BlockOut]


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


def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.repository
    return repository


_Repo = Annotated[Repository, Depends(_repository)]
_Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]

router = APIRouter(prefix="/api/v1")


@router.get("/sessions")
def list_sessions(
    repository: _Repo,
    status: SessionStatus | None = None,
    limit: _Limit = DEFAULT_LIMIT,
) -> list[SessionOut]:
    """The sessions with the latest activity first, optionally of one status."""
    views = repository.list_sessions(limit, status)
    return [SessionOut.of(view) for view in views]


@router.get("/sessions/{session_id}")
def get_session(repository: _Repo, session_id: str) -> SessionDetailOut:
    """One session with its agents and blocks."""
    detail = repository.get_session(session_id) if is_valid_session_id(session_id) else None
    if detail is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return SessionDetailOut(
        **SessionOut.of(detail).model_dump(),
        agents=[AgentOut.of(agent) for agent in detail.agents],
        blocks=[BlockOut.of(block) for block in detail.blocks],
    )


@router.get("/sessions/{session_id}/events")
def list_events(
    repository: _Repo, session_id: str, limit: _Limit = DEFAULT_LIMIT
) -> list[EventOut]:
    """The session's latest ``limit`` hook events, in ``seq`` order."""
    if not is_valid_session_id(session_id) or not repository.session_exists(session_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return [EventOut.of(row) for row in repository.list_events(session_id, limit)]


async def validation_error(_request: Request, error: RequestValidationError) -> Response:
    """FastAPI's 422 without the ``input`` and ``ctx`` of each error, which would echo the
    request's values."""
    detail = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in error.errors()]
    return JSONResponse({"detail": detail}, status_code=422)
