"""The web UI's pages and fragments, and its static files (ADR 0031).

- ``GET /``: the overview, a strip board of sessions stacked in bays by state
  (``ccobs.domain.presentation``).
- ``GET /ui/board``: the board alone, for htmx to refetch (one response shape per URL).
- ``GET /sessions``: every stored session, the latest activity first, paged by a keyset
  cursor (``?before=<last_event_at_ms>-<session_id>``), never by offset.
- ``/static``: the package's ``static/`` directory.

Each request reads the database once, through the read gate (``ccobs.server.reads``,
ADR 0033). A busy gate or a failed read renders a page or fragment that says what failed,
with status 503; nothing from the error reaches it. The templates get view objects built
here, never a session row: ``cwd`` becomes ``presentation.project_name`` first, so the
full path never reaches the HTML (product decision P2). Jinja2 escapes every value
(autoescape), and an undefined name fails (``StrictUndefined``).

Static cache policy: files under ``static/vendor/`` carry their version in their name
(``tests/server/test_static.py`` checks htmx's), so a new version is a new URL and they
are cached for a year as immutable. Every other static file (our CSS and JavaScript, the
fonts, the icon) keeps its name across releases, so it is ``no-cache``: the browser
stores it but revalidates it with its ``ETag`` on each use, a 304 on localhost, and an
upgrade never leaves a stale stylesheet. ``SecurityHeaders`` adds no ``no-store`` under
``/static/`` (ADR 0034).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.hooks import is_valid_session_id
from ccobs.domain.presentation import (
    RECENT_END_WINDOW_MS,
    Bay,
    StripFacts,
    board_sort_key,
    describe_activity,
    describe_block,
    format_duration,
    project_name,
)
from ccobs.server.db.repository import BoardRows, Repository, StripRow
from ccobs.server.reads import ReadGate

__all__ = [
    "CLOSED_BAY_LIMIT",
    "IMMUTABLE",
    "REVALIDATE",
    "SESSIONS_PAGE_SIZE",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "StaticAssets",
    "router",
]

_log = logging.getLogger(__name__)

TEMPLATES_DIR: Final = Path(__file__).with_name("templates")
STATIC_DIR: Final = Path(__file__).with_name("static")
# Versioned by name: a new version is a new URL.
_VERSIONED_DIR: Final = "vendor"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
REVALIDATE: Final = "no-cache"

# How many strips each closed bay (ended, stale) shows; the rest are on All sessions.
CLOSED_BAY_LIMIT: Final = 20
SESSIONS_PAGE_SIZE: Final = 50
# The largest last_event_at_ms a cursor may hold: SQLite's signed 64-bit integers.
_MAX_CURSOR: Final = 2**63 - 1
_CURSOR: Final = re.compile(r"(?P<ms>[0-9]{1,19})-(?P<session_id>.+)", re.DOTALL)
_SHORT_ID: Final = 8
_SERVICE_UNAVAILABLE: Final = 503
_BAD_REQUEST: Final = 400

_STATE_LABELS: Final = {
    "waiting_user": "Waiting on you",
    "working": "Working",
    "idle": "Idle",
    "ended": "Ended",
    "stale": "Stale",
}
_LIVE_BAYS: Final = (Bay.WAITING_USER, Bay.WORKING, Bay.IDLE)

# StaticFiles takes media types from mimetypes, which on Windows reads the registry
# (.js can be text/plain there) and may not know .woff2 at all; with nosniff the browser
# then refuses the file. The standard types, whatever the OS says (RFC 9239, RFC 8081).
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/css", ".css")

_environment: Final = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


# View objects: what the templates see.


@dataclass(frozen=True, kw_only=True)
class Moment:
    """A past moment shown as the time elapsed since it: ``text`` from the server, which
    app.js ticks from ``since_ms``; ``iso`` is the moment in UTC, for ``datetime``."""

    iso: str
    since_ms: int
    text: str


@dataclass(frozen=True, kw_only=True)
class Strip:
    href: str
    short_id: str
    status: str
    state_label: str
    project: str | None
    doing: str | None
    agents: str | None
    error_code: str | None
    elapsed: Moment
    elapsed_kind: Literal["wait", "ended", "activity"]


@dataclass(frozen=True, kw_only=True)
class BayView:
    key: str
    heading: str
    strips: list[Strip]
    total: int
    collapsible: bool

    @property
    def more(self) -> int:
        return self.total - len(self.strips)


@dataclass(frozen=True, kw_only=True)
class BoardView:
    bays: list[BayView]
    waiting: int
    live: int
    any_session: bool


@dataclass(frozen=True, kw_only=True)
class ErrorView:
    title: str
    detail: str


_BUSY: Final = ErrorView(
    title="The database is busy",
    detail="Too many reads are waiting for the database. Reload the page in a few seconds.",
)
_BROKEN: Final = ErrorView(
    title="The app could not read its database",
    detail="Reading the sessions failed. The app's log (docker compose logs app) says why; "
    "reload the page once it is fixed.",
)
_BAD_CURSOR: Final = ErrorView(
    title="This page link is not valid",
    detail="Start again from the newest sessions.",
)


def _moment(since_ms: int, now_ms: int) -> Moment:
    at = datetime.fromtimestamp(since_ms / 1000, UTC)
    return Moment(
        iso=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        since_ms=since_ms,
        text=format_duration(now_ms - since_ms),
    )


def _strip(row: StripRow, now_ms: int) -> Strip:
    session = row.session
    block = row.oldest_open_block
    running = row.running_subagents
    elapsed_kind: Literal["wait", "ended", "activity"]
    if session.status == "waiting_user":
        doing = (
            None
            if block is None
            else describe_block(block.kind, block.tool_name, block.mcp_server_name)
        )
        since = session.last_event_at_ms if block is None else block.opened_at_ms
        elapsed_kind = "wait"
    else:
        doing = describe_activity(
            session.status,
            compacting=session.compacting,
            running_subagents=running,
            idle_notified=session.idle_notified_at_ms is not None,
            prompted=session.last_prompt_id is not None,
            end_reason=session.end_reason,
        )
        if session.status == "ended":
            since = session.ended_at_ms or session.last_event_at_ms
            elapsed_kind = "ended"
        else:
            since = session.last_event_at_ms
            elapsed_kind = "activity"
    state_label = _STATE_LABELS[session.status]
    if doing == state_label:
        # Ended for no known reason: "Ended" once, not twice.
        doing = None
    # The subagents, unless the activity already says how many run.
    agents = describe_activity("working", running_subagents=running) if running else None
    return Strip(
        href="/sessions/" + quote(session.session_id, safe=""),
        short_id=session.session_id[:_SHORT_ID],
        status=session.status,
        state_label=state_label,
        project=project_name(session.cwd),
        doing=doing,
        agents=None if agents == doing else agents,
        error_code=session.last_error_code,
        elapsed=_moment(since, now_ms),
        elapsed_kind=elapsed_kind,
    )


def _facts(row: StripRow) -> StripFacts:
    session = row.session
    block = row.oldest_open_block
    return StripFacts(
        session_id=session.session_id,
        status=session.status,
        last_event_at_ms=session.last_event_at_ms,
        ended_at_ms=session.ended_at_ms,
        oldest_open_block_at_ms=None if block is None else block.opened_at_ms,
    )


def _board_view(rows: BoardRows, now_ms: int) -> BoardView:
    live = sorted(rows.live, key=lambda row: board_sort_key(_facts(row)))
    by_bay: dict[Bay, list[StripRow]] = {bay: [] for bay in _LIVE_BAYS}
    for row in live:
        by_bay[Bay(row.session.status)].append(row)
    bays = [
        BayView(
            key=bay.value,
            heading=bay.heading,
            strips=[_strip(row, now_ms) for row in by_bay[bay]],
            total=len(by_bay[bay]),
            collapsible=False,
        )
        for bay in _LIVE_BAYS
        # The waiting bay always shows: who waits on you is the board's first question.
        if by_bay[bay] or bay is Bay.WAITING_USER
    ]
    for bay, closed, total in (
        (Bay.ENDED, rows.ended, rows.ended_total),
        (Bay.STALE, rows.stale, rows.stale_total),
    ):
        if total:
            ordered = sorted(closed, key=lambda row: board_sort_key(_facts(row)))
            bays.append(
                BayView(
                    key=bay.value,
                    heading=bay.heading,
                    strips=[_strip(row, now_ms) for row in ordered],
                    total=total,
                    collapsible=True,
                )
            )
    return BoardView(
        bays=bays,
        waiting=len(by_bay[Bay.WAITING_USER]),
        live=len(live),
        any_session=rows.any_session,
    )


def _hook_json(request: Request) -> str:
    """The hook settings for this app as the browser reaches it (the request's port),
    with the null device of the browser's OS: Claude Code runs on the same machine."""
    port = request.url.port
    url = f"http://127.0.0.1{'' if port is None else f':{port}'}{HOOK_PATH}"
    windows = "Windows" in request.headers.get("user-agent", "")
    return json.dumps(hook_settings(url, windows=windows), indent=2)


def _render(name: str, *, status_code: int = 200, **context: object) -> HTMLResponse:
    body = _environment.get_template(name).render(**context)
    return HTMLResponse(body, status_code=status_code)


# Reads: one call on the gate per request.


def _board_read(repository: Repository, now_ms: int) -> BoardRows:
    return repository.board(
        ended_since_ms=now_ms - RECENT_END_WINDOW_MS, closed_limit=CLOSED_BAY_LIMIT
    )


async def _read[T, *Ts](reads: ReadGate, read: Callable[[*Ts], T], *args: *Ts) -> T | ErrorView:
    """``read(*args)`` through the gate, or the error to show: a busy gate (its 503) or
    a failed query. Only the error's type is logged; its message may quote a value."""
    try:
        return await reads.run(read, *args)
    except HTTPException as error:
        if error.status_code != _SERVICE_UNAVAILABLE:
            raise
        _log.warning("UI read refused: database busy")
        return _BUSY
    except SQLAlchemyError as error:
        _log.error("UI read failed (%s)", type(error).__name__)
        return _BROKEN


async def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.read_repository
    return repository


async def _reads(request: Request) -> ReadGate:
    reads: ReadGate = request.app.state.reads
    return reads


def _now_ms(request: Request) -> int:
    clock: Callable[[], int] = request.app.state.clock
    return clock()


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]

router = APIRouter(include_in_schema=False)


async def _board_context(
    request: Request, repository: Repository, reads: ReadGate
) -> dict[str, object]:
    now_ms = _now_ms(request)
    rows = await _read(reads, _board_read, repository, now_ms)
    if isinstance(rows, ErrorView):
        return {"board": None, "error": rows, "hook_json": ""}
    board = _board_view(rows, now_ms)
    hook_json = "" if board.any_session else _hook_json(request)
    return {"board": board, "error": None, "hook_json": hook_json}


def _status(context: dict[str, object]) -> int:
    return 200 if context["error"] is None else _SERVICE_UNAVAILABLE


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, repository: _Repo, reads: _Reads) -> Response:
    """The strip board, in a full page."""
    context = await _board_context(request, repository, reads)
    return _render("overview.html", status_code=_status(context), nav="overview", **context)


@router.get("/ui/board", response_class=HTMLResponse)
async def board_fragment(request: Request, repository: _Repo, reads: _Reads) -> Response:
    """The strip board alone, for htmx to swap in place of ``#board``."""
    context = await _board_context(request, repository, reads)
    return _render("_board.html", status_code=_status(context), **context)


def _parse_cursor(before: str) -> tuple[int, str] | None:
    match = _CURSOR.fullmatch(before)
    if match is None:
        return None
    last_event_at_ms = int(match["ms"])
    session_id = match["session_id"]
    if last_event_at_ms > _MAX_CURSOR or not is_valid_session_id(session_id):
        return None
    return last_event_at_ms, session_id


def _page_read(
    repository: Repository, limit: int, before: tuple[int, str] | None
) -> list[StripRow]:
    return repository.session_page(limit, before)


@router.get("/sessions", response_class=HTMLResponse)
async def all_sessions(
    request: Request, repository: _Repo, reads: _Reads, before: str | None = None
) -> Response:
    """Every stored session as a strip, the latest activity first, a page at a time."""
    cursor = None
    if before is not None:
        cursor = _parse_cursor(before)
        if cursor is None:
            # The value is not echoed.
            return _sessions_page(status_code=_BAD_REQUEST, paged=True, error=_BAD_CURSOR)
    now_ms = _now_ms(request)
    # One more than a page: whether an older page exists.
    rows = await _read(reads, _page_read, repository, SESSIONS_PAGE_SIZE + 1, cursor)
    if isinstance(rows, ErrorView):
        return _sessions_page(
            status_code=_SERVICE_UNAVAILABLE, paged=cursor is not None, error=rows
        )
    shown = rows[:SESSIONS_PAGE_SIZE]
    return _sessions_page(
        paged=cursor is not None,
        strips=[_strip(row, now_ms) for row in shown],
        next_href=_next_href(shown) if len(rows) > SESSIONS_PAGE_SIZE else None,
        hook_json=_hook_json(request) if not rows and cursor is None else "",
    )


def _sessions_page(
    *,
    status_code: int = 200,
    paged: bool,
    error: ErrorView | None = None,
    strips: Sequence[Strip] = (),
    next_href: str | None = None,
    hook_json: str = "",
) -> HTMLResponse:
    """The All sessions page; ``paged`` when it is not the first page."""
    return _render(
        "sessions.html",
        status_code=status_code,
        nav="sessions",
        before=paged,
        error=error,
        strips=strips,
        next_href=next_href,
        hook_json=hook_json,
    )


def _next_href(shown: Sequence[StripRow]) -> str:
    last = shown[-1].session
    return "/sessions?" + urlencode({"before": f"{last.last_event_at_ms}-{last.session_id}"})


# Static files.


class StaticAssets(StaticFiles):
    """The package's static files, each with its cache policy (see the module's
    docstring); conditional requests still get their 304."""

    def __init__(self, directory: Path = STATIC_DIR) -> None:
        super().__init__(directory=directory)
        self._versioned = os.path.realpath(directory / _VERSIONED_DIR)

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        # A FileResponse, or the 304 built from its headers.
        response = super().file_response(full_path, stat_result, scope, status_code)
        versioned = os.path.dirname(os.path.realpath(full_path)) == self._versioned
        response.headers["cache-control"] = IMMUTABLE if versioned else REVALIDATE
        return response
