"""The web UI's pages and fragments, and its static files (ADR 0031).

- ``GET /``: the overview, a strip board of sessions stacked in bays by state
  (``ccobs.domain.presentation``).
- ``GET /ui/board``: the board alone, for htmx to refetch (one response shape per URL).
- ``GET /sessions``: every stored session, the latest activity first, paged by a keyset
  cursor (``?before=<last_event_at_ms>-<session_id>``), never by offset.
- ``GET /sessions/{session_id}``: one session: its header, its blocks (open ones first,
  then closed ones newest first), its agents as a tree and its hook timeline, newest
  first. ``/ui/sessions/{session_id}/blocks``, ``.../agents`` and ``.../events`` return
  the next page of each list for the "Show older" controls, ``.../events?after=`` the
  events newer than a cursor, for the timeline's tail, and ``.../live`` the header,
  blocks and agents again, for the live refresh (see "Session page" and "Live
  refresh"). ``.../usage`` returns the "Tokens and cost" section (see "Tokens and
  cost").
- ``/static``: the package's ``static/`` directory.

Every page's header links the Grafana overview dashboard for the app's host label, and
the session page links the session's events, tokens and cost in Grafana's Explore and
the dashboard over its range (``ccobs.domain.grafana_links``). They are plain links that
open a new tab, from ``CCOBS_GRAFANA_URL``; Jinja2 escapes them in the ``href``.

Each request reads the database once, through the read gate (``ccobs.server.reads``,
ADR 0033). A busy gate or a failed read renders a page or fragment that says what failed,
with status 503; nothing from the error reaches it. The templates get view objects built
here, never a session row: ``cwd`` becomes ``presentation.project_name`` first, so the
full path never reaches the HTML (product decision P2). Jinja2 escapes every value
(autoescape), and an undefined name fails (``StrictUndefined``).

Session page. Every list pages by the repository's keysets, never by offset, and each
"Show older" control is an htmx button that appends the next page to its list and
replaces itself out of band (``hx-swap-oob``) with the control for the page after, or
with nothing at the end. The detail already holds every open block and running agent,
so the older pages leave them out: they show only closed blocks and stopped agents, and
nothing appears twice. Agents are a tree (``ccobs.domain.agent_tree``), newest first
among siblings, so older agents belong at the bottom; an older page is a tree of its own
agents, whose launchers may be on another page. The timeline's tail is an element that
refetches ``events?after=<the newest seq shown>`` on the ``ccobs:changed`` event (live
updates dispatch it, ADR 0032) and swaps itself: the response prepends the newer rows to
the timeline out of band and carries a new tail with the new cursor. A trigger during
a request replaces it (``hx-sync``), so the last change of a burst is never lost, and
the rows are the same for the same cursor; a tail more than a page behind triggers
itself again on ``load`` until it catches up. The first rows of a page that had none
also delete its empty message, out of band. The fragments' error responses are not
swapped (htmx 2's default ``responseHandling`` leaves 4xx and 5xx out), so a failed page
or tail request leaves the page as it was. ``app.js`` drops a row an older page repeats
(a block open, or an agent running, when the page loaded) and keeps the focus on the
pager. Only the
metadata ADR 0023 stores reaches the page, and only through the view objects.

Live refresh (ADR 0032). The overview's board refetches ``/ui/board`` on
``ccobs:changed`` and replaces itself. On the session page, a hidden element refetches
``.../live`` and replaces itself, while the header, the blocks and the agents come out
of band: one read of the session detail per change, besides the tail's. "Show older"
pages land in lists of their own (``#closed-blocks-older``, ``#agents-older``) that the
refresh leaves alone, so the pages a user loaded stay. To keep the two parts contiguous,
the refresh does not take the latest closed blocks and stopped agents (that window
slides, and rows would fall between it and the older pages): the page sends its
boundaries, the oldest closed block and stopped agent it showed, which are the cursors
of its "Show older" controls, its render time, and the IDs of the blocks open and agents
running below a boundary (at most ``LIVE_KEEP_LIMIT`` of each). The refresh holds every
open block and running agent, every closed and stopped one from the boundaries up, those
the page named, whatever their close time reads, and those that closed or stopped after
the page loaded. So a row the page showed open or running stays on it once it closes,
unless the page had more than ``LIVE_KEEP_LIMIT`` of them below a boundary; nothing
shows twice (``app.js`` drops from the older lists a row the refresh now holds). The
window only grows; past ``LIVE_CLOSED_LIMIT`` rows it starts over: the latest page, the
older list emptied and a "Show older" control from the new boundary, all out of band,
with the time of that refresh as its new start and the rows then open below the new
boundary named, so the next refresh does not start over again.

Tokens and cost (ADR 0035). The session page loads its "Tokens and cost" section from
``.../usage`` with htmx on ``load`` and every 60 s, not on live updates: the totals come
from Claude Code's metrics in Prometheus, which Claude Code exports about once a minute,
not from the hooks that drive the change hub. The fragment reads the session once
through the read gate, then, with no read slot held, asks the usage reader
(``ccobs.server.usage``) for its totals over the session's window
(``ccobs.domain.usage``). Every state it can show is a 200, so htmx swaps it in place of
the loading text or the last totals: the values, no usage yet, Prometheus unavailable,
values it can't read, or a database it could not read. Only an unknown session is a 404
(left unswapped, like the other fragments' errors).

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ccobs.domain.agent_tree import AgentRef, build_agent_tree
from ccobs.domain.grafana_links import SessionLinks, dashboard_url, session_links, session_range
from ccobs.domain.hook_config import DEFAULT_APP_PORT, HOOK_PATH, hook_settings
from ccobs.domain.hooks import is_valid_session_id
from ccobs.domain.presentation import (
    RECENT_END_WINDOW_MS,
    Bay,
    StripFacts,
    board_sort_key,
    describe_activity,
    describe_agent_stop,
    describe_block,
    describe_block_close,
    display_path,
    format_duration,
    project_name,
)
from ccobs.domain.sessions import Agent, Block, Session
from ccobs.domain.usage import (
    SessionUsage,
    format_tokens,
    format_usd,
    model_rows,
    token_type_label,
    usage_window,
)
from ccobs.server.db.repository import (
    AgentPage,
    BoardRows,
    HookEventRow,
    Repository,
    SessionDetail,
    StripRow,
)
from ccobs.server.live import STREAM_PATH
from ccobs.server.reads import ReadGate
from ccobs.server.usage import UsageReader, UsageUnavailable

__all__ = [
    "AGENTS_PAGE_SIZE",
    "BLOCKS_PAGE_SIZE",
    "CLOSED_BAY_LIMIT",
    "EVENTS_PAGE_SIZE",
    "IMMUTABLE",
    "LIVE_CLOSED_LIMIT",
    "LIVE_KEEP_ID_MAX",
    "LIVE_KEEP_LIMIT",
    "REVALIDATE",
    "SESSIONS_PAGE_SIZE",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "ErrorView",
    "StaticAssets",
    "render",
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
# The session page's lists: how many rows each "Show older" page adds, and how many
# events the page starts with.
BLOCKS_PAGE_SIZE: Final = 50
AGENTS_PAGE_SIZE: Final = 50
EVENTS_PAGE_SIZE: Final = 50
# How many closed blocks (and stopped agents) the live refresh of a session page holds
# before its window starts over at the latest page (see "Live refresh").
LIVE_CLOSED_LIMIT: Final = 200
# How many open blocks (and running agents) below its boundary a live link names; a
# session rarely has more than one or two, and the URL must stay short.
LIVE_KEEP_LIMIT: Final = 8
# The longest agent ID a live link carries (Claude Code's are short; a stored ID may
# reach 256 characters, three times that once quoted).
LIVE_KEEP_ID_MAX: Final = 128
# An agent tree deeper than this is drawn at this depth (the CSS has a class per depth).
_MAX_DEPTH: Final = 6
_SEQ: Final = re.compile(r"[0-9]{1,19}")
_NOT_FOUND: Final = 404
# The largest last_event_at_ms a cursor may hold: SQLite's signed 64-bit integers.
_MAX_CURSOR: Final = 2**63 - 1
_CURSOR: Final = re.compile(r"(?P<ms>[0-9]{1,19})-(?P<session_id>.+)", re.DOTALL)
_SHORT_ID: Final = 8
_SECOND_MS: Final = 1000
_SERVICE_UNAVAILABLE: Final = 503
_BAD_REQUEST: Final = 400
# How the empty state tells a user to connect Claude Code (the CLI ships in the package).
_SETUP: Final = "uvx cc-observatory setup"
# A URL without a port: the app speaks plain http (ADR 0018).
_HTTP_DEFAULT_PORT: Final = 80

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
# The stream of a page that names none of its own: every page streams (ADR 0044).
_environment.globals["stream_path"] = STREAM_PATH


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
_BAD_LINK: Final = ErrorView(
    title="This page link is not valid",
    detail="Reload the session page.",
)
_NO_SESSION: Final = ErrorView(
    title="Session not found",
    detail="No stored session has this ID. Retention may have deleted it, or the link "
    "is incomplete.",
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


def _setup_command(request: Request) -> str:
    """The setup command for this app as the browser reaches it: ``--app-port`` names
    the request's port when it is not the default, so setup writes this app's URL. No
    port in the request means http's 80, which the manual hook JSON uses too."""
    port = _HTTP_DEFAULT_PORT if request.url.port is None else request.url.port
    flag = "" if port == DEFAULT_APP_PORT else f" --app-port {port}"
    return f"{_SETUP}{flag}"


def _render(name: str, *, status_code: int = 200, **context: object) -> HTMLResponse:
    body = _environment.get_template(name).render(**context)
    return HTMLResponse(body, status_code=status_code)


def render(name: str, *, status_code: int = 200, **context: object) -> HTMLResponse:
    """The template ``name`` rendered with the UI's environment (autoescape, an undefined
    name fails), for the UI's other routers (``ccobs.server.alert_settings``)."""
    return _render(name, status_code=status_code, **context)


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


def _grafana_home(request: Request) -> str:
    """The header's link: the overview dashboard for the app's host label, over the
    dashboard's own default range."""
    return dashboard_url(request.app.state.grafana_url, host=request.app.state.host_label)


def _last_ms(session: Session) -> int | None:
    """The session's last moment: its end, its last event once stale (it gets no more),
    else ``None``: it is live."""
    if session.ended_at_ms is None and session.status == "stale":
        return session.last_event_at_ms
    return session.ended_at_ms


def _grafana_session(request: Request, session: Session) -> SessionLinks:
    """The session page's links to Grafana, over the session's range: up to its last
    moment (``_last_ms``), else up to now."""
    return session_links(
        request.app.state.grafana_url,
        host=request.app.state.host_label,
        session_id=session.session_id,
        time_range=session_range(session.started_at_ms, _last_ms(session)),
    )


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]

router = APIRouter(include_in_schema=False)


async def _board_context(
    request: Request, repository: Repository, reads: ReadGate
) -> dict[str, object]:
    now_ms = _now_ms(request)
    rows = await _read(reads, _board_read, repository, now_ms)
    if isinstance(rows, ErrorView):
        return {"board": None, "error": rows, "hook_json": "", "setup_command": ""}
    board = _board_view(rows, now_ms)
    empty = not board.any_session
    return {
        "board": board,
        "error": None,
        "hook_json": _hook_json(request) if empty else "",
        "setup_command": _setup_command(request) if empty else "",
    }


def _status(context: dict[str, object]) -> int:
    return 200 if context["error"] is None else _SERVICE_UNAVAILABLE


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, repository: _Repo, reads: _Reads) -> Response:
    """The strip board, in a full page."""
    context = await _board_context(request, repository, reads)
    return _render(
        "overview.html",
        status_code=_status(context),
        nav="overview",
        grafana_home=_grafana_home(request),
        live_stream=STREAM_PATH,
        **context,
    )


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
            return _sessions_page(request, status_code=_BAD_REQUEST, paged=True, error=_BAD_CURSOR)
    now_ms = _now_ms(request)
    # One more than a page: whether an older page exists.
    rows = await _read(reads, _page_read, repository, SESSIONS_PAGE_SIZE + 1, cursor)
    if isinstance(rows, ErrorView):
        return _sessions_page(
            request, status_code=_SERVICE_UNAVAILABLE, paged=cursor is not None, error=rows
        )
    shown = rows[:SESSIONS_PAGE_SIZE]
    empty = not rows and cursor is None
    return _sessions_page(
        request,
        paged=cursor is not None,
        strips=[_strip(row, now_ms) for row in shown],
        next_href=_next_href(shown) if len(rows) > SESSIONS_PAGE_SIZE else None,
        hook_json=_hook_json(request) if empty else "",
        setup_command=_setup_command(request) if empty else "",
    )


def _sessions_page(
    request: Request,
    *,
    status_code: int = 200,
    paged: bool,
    error: ErrorView | None = None,
    strips: Sequence[Strip] = (),
    next_href: str | None = None,
    hook_json: str = "",
    setup_command: str = "",
) -> HTMLResponse:
    """The All sessions page; ``paged`` when it is not the first page."""
    return _render(
        "sessions.html",
        status_code=status_code,
        nav="sessions",
        grafana_home=_grafana_home(request),
        before=paged,
        error=error,
        strips=strips,
        next_href=next_href,
        hook_json=hook_json,
        setup_command=setup_command,
    )


def _next_href(shown: Sequence[StripRow]) -> str:
    last = shown[-1].session
    return "/sessions?" + urlencode({"before": f"{last.last_event_at_ms}-{last.session_id}"})


# The session page.


@dataclass(frozen=True, kw_only=True)
class SessionHead:
    session_id: str
    short_id: str
    status: str
    state_label: str
    project: str | None
    path: str | None
    doing: str | None
    error_code: str | None
    waiting: Moment | None
    started: Moment
    last_event: Moment
    ended: Moment | None


@dataclass(frozen=True, kw_only=True)
class BlockView:
    block_id: int
    is_open: bool
    what: str
    scope: str | None
    opened: Moment
    waited: str | None
    closed: Moment | None
    close: str | None


@dataclass(frozen=True, kw_only=True)
class AgentView:
    agent_id: str
    short_id: str
    agent_type: str | None
    depth: int
    launched_by: str | None
    running: bool
    status_text: str
    internal: bool
    background: bool
    started: Moment | None
    stopped: Moment | None
    ran: str | None


@dataclass(frozen=True, kw_only=True)
class Detail:
    """One field of a timeline row: a label, a value and whether the value is an
    identifier (shown in mono)."""

    label: str
    value: str
    mono: bool = False


@dataclass(frozen=True, kw_only=True)
class EventView:
    seq: int
    name: str
    at: Moment
    details: list[Detail]


@dataclass(frozen=True, kw_only=True)
class More:
    """A "Show older" control: the URL of the next page, or ``None`` at the end."""

    href: str | None


@dataclass(frozen=True, kw_only=True)
class Tail:
    """The timeline's tail: the URL of the events after the newest one shown, and
    whether to fetch it at once (more than a page was new)."""

    href: str
    now: bool


@dataclass(frozen=True, kw_only=True)
class BlocksRegion:
    """The blocks the live refresh replaces: every open one, then the closed ones,
    newest first; ``total`` counts every block of the session."""

    open: list[BlockView]
    closed: list[BlockView]
    total: int


@dataclass(frozen=True, kw_only=True)
class AgentsRegion:
    """The agents the live refresh replaces, as a tree; ``total`` counts every agent."""

    rows: list[AgentView]
    total: int


@dataclass(frozen=True, kw_only=True)
class LiveLink:
    """The live refresh's URL: the page's boundaries and render time."""

    href: str


@dataclass(frozen=True, kw_only=True)
class SessionRead:
    detail: SessionDetail
    # Newest first, one more than a page when an older page exists.
    events: list[HookEventRow]


def _session_href(session_id: str, suffix: str, **query: object) -> str:
    return (
        "/ui/sessions/"
        + quote(session_id, safe="")
        + "/"
        + suffix
        + "?"
        + urlencode({key: str(value) for key, value in query.items()})
    )


def _session_head(detail: SessionDetail, now_ms: int) -> SessionHead:
    session = detail.session
    waiting = None
    if session.status == "waiting_user":
        open_blocks = [block for block in detail.blocks if block.closed_at_ms is None]
        oldest = min(open_blocks, key=lambda block: block.opened_at_ms, default=None)
        doing = (
            None
            if oldest is None
            else describe_block(oldest.kind, oldest.tool_name, oldest.mcp_server_name)
        )
        since = session.last_event_at_ms if oldest is None else oldest.opened_at_ms
        waiting = _moment(since, now_ms)
    else:
        running = sum(
            1 for agent in detail.agents if agent.status == "running" and not agent.internal
        )
        doing = describe_activity(
            session.status,
            compacting=session.compacting,
            running_subagents=running,
            idle_notified=session.idle_notified_at_ms is not None,
            prompted=session.last_prompt_id is not None,
            end_reason=session.end_reason,
        )
    state_label = _STATE_LABELS[session.status]
    ended = session.ended_at_ms
    return SessionHead(
        session_id=session.session_id,
        short_id=session.session_id[:_SHORT_ID],
        status=session.status,
        state_label=state_label,
        project=project_name(session.cwd),
        path=display_path(session.cwd),
        # Ended for no known reason: "Ended" once, not twice.
        doing=None if doing == state_label else doing,
        error_code=session.last_error_code,
        waiting=waiting,
        started=_moment(session.started_at_ms, now_ms),
        last_event=_moment(session.last_event_at_ms, now_ms),
        ended=None if ended is None else _moment(ended, now_ms),
    )


def _block_view(block: Block, now_ms: int) -> BlockView:
    closed_at = block.closed_at_ms
    return BlockView(
        block_id=block.block_id,
        is_open=closed_at is None,
        what=describe_block(block.kind, block.tool_name, block.mcp_server_name),
        scope=None if block.scope is None else block.scope[:_SHORT_ID],
        opened=_moment(block.opened_at_ms, now_ms),
        waited=None if closed_at is None else format_duration(closed_at - block.opened_at_ms),
        closed=None if closed_at is None else _moment(closed_at, now_ms),
        close=None if closed_at is None else describe_block_close(block.kind, block.close_reason),
    )


def _agent_views(
    agents: Sequence[Agent], parents: Mapping[str, str | None], now_ms: int
) -> list[AgentView]:
    """The agents as a tree, in pre-order: newest first among siblings (``agents`` is in
    first-seen order), each subagent right under the agent that launched it. The tree
    gives only the depth: "Launched by" is the stored parent, which may be on another
    page (or cut from a cycle) while the agent hangs from the main thread."""
    by_id = {agent.agent_id: agent for agent in agents}
    tree = build_agent_tree(
        AgentRef(
            agent_id=agent.agent_id,
            parent_agent_id=parents.get(agent.agent_id),
            order=-position,
        )
        for position, agent in enumerate(agents)
    )
    views = []
    for node in tree.rows:
        # Every row of the tree comes from an agent: only the root has no ID.
        agent = by_id[str(node.agent_id)]
        started, stopped = agent.started_at_ms, agent.stopped_at_ms
        running = agent.status == "running"
        parent = parents.get(agent.agent_id)
        views.append(
            AgentView(
                agent_id=agent.agent_id,
                short_id=agent.agent_id[:_SHORT_ID],
                agent_type=agent.agent_type,
                depth=min(node.depth, _MAX_DEPTH),
                launched_by=(
                    None if parent is None or parent == agent.agent_id else parent[:_SHORT_ID]
                ),
                running=running,
                status_text="Running" if running else describe_agent_stop(agent.stop_reason),
                internal=agent.internal,
                background=agent.background,
                started=None if started is None else _moment(started, now_ms),
                stopped=None if stopped is None else _moment(stopped, now_ms),
                ran=(
                    None
                    if started is None or stopped is None
                    else format_duration(stopped - started)
                ),
            )
        )
    return views


def _took(duration_ms: int) -> str:
    """A tool call's duration: milliseconds under a second, else ``format_duration``."""
    return f"{duration_ms} ms" if duration_ms < _SECOND_MS else format_duration(duration_ms)


def _count(value: int | None, unit: str) -> str | None:
    return None if value is None else f"{value} {unit}"


def _event_details(row: HookEventRow) -> list[Detail]:
    """The fields of a timeline row: stored metadata only (ADR 0023), never content.
    IDs other than the agent's (prompt, tool use, elicitation) are left out: they only
    link rows to each other."""
    fields: list[tuple[str, str | None, bool]] = [
        ("Tool", row.tool_name, True),
        ("MCP server", row.mcp_server_name, True),
        ("Agent", None if row.agent_id is None else row.agent_id[:_SHORT_ID], True),
        ("Agent type", row.agent_type, False),
        ("Notification", row.notification_type, True),
        ("Source", row.start_source, False),
        ("Trigger", row.compact_trigger, False),
        ("Reason", row.end_reason, False),
        ("Action", row.elicitation_action, False),
        ("Status", row.tool_response_status, False),
        ("Error", row.error_code, True),
        ("Took", None if row.duration_ms is None else _took(row.duration_ms), False),
        ("Outcome", "Interrupted" if row.is_interrupt else None, False),
        ("Prompt", _count(row.prompt_length, "characters"), False),
        ("Input", _count(row.tool_input_bytes, "bytes"), False),
        ("Output", _count(row.tool_response_bytes, "bytes"), False),
    ]
    return [
        Detail(label=label, value=value, mono=mono)
        for label, value, mono in fields
        if value is not None
    ]


def _event_view(row: HookEventRow, now_ms: int) -> EventView:
    return EventView(
        seq=row.seq,
        name=row.hook_event_name,
        at=_moment(row.received_at_ms, now_ms),
        details=_event_details(row),
    )


def _tail(session_id: str, after: int, *, now: bool) -> Tail:
    return Tail(href=_session_href(session_id, "events", after=after), now=now)


def _blocks_region(detail: SessionDetail, now_ms: int, keep: int | None = None) -> BlocksRegion:
    """The detail's blocks: open ones in ``block_id`` order, closed ones newest first, at
    most ``keep`` of those."""
    views = [_block_view(block, now_ms) for block in detail.blocks]
    closed = [block for block in reversed(views) if not block.is_open]
    return BlocksRegion(
        open=[block for block in views if block.is_open],
        closed=closed if keep is None else closed[:keep],
        total=detail.blocks_total,
    )


@dataclass(frozen=True, kw_only=True)
class _AgentsShown:
    region: AgentsRegion
    # The stopped agents shown, and every agent shown, in first-seen order.
    stopped: list[Agent]
    agents: list[Agent]

    @property
    def boundary(self) -> str | None:
        """The oldest stopped agent shown: the "Show older" cursor and the boundary."""
        return self.stopped[0].agent_id if self.stopped else None

    def kept(self) -> tuple[str, ...]:
        """The running agents below the boundary (see ``_Window``)."""
        if not self.stopped:
            return ()
        # The boundary is the oldest stopped agent shown, so every agent shown before it
        # (in first-seen order) is running.
        below = self.agents[: self.agents.index(self.stopped[0])]
        # An ID too long to carry is left out rather than making the link too long.
        ids = [agent.agent_id for agent in below if len(agent.agent_id) <= LIVE_KEEP_ID_MAX]
        return tuple(ids[:LIVE_KEEP_LIMIT])


def _agents_region(detail: SessionDetail, now_ms: int, keep: int | None = None) -> _AgentsShown:
    """The detail's agents as a tree, with the latest ``keep`` stopped ones at most."""
    stopped = [agent for agent in detail.agents if agent.status != "running"]
    if keep is not None:
        stopped = stopped[-keep:]
    kept = {agent.agent_id for agent in stopped}
    agents = [
        agent for agent in detail.agents if agent.status == "running" or agent.agent_id in kept
    ]
    region = AgentsRegion(
        rows=_agent_views(agents, detail.parents, now_ms), total=detail.agents_total
    )
    return _AgentsShown(region=region, stopped=stopped, agents=agents)


def _kept_blocks(blocks: BlocksRegion, boundary: int | None) -> tuple[int, ...]:
    """The open blocks below the boundary (see ``_Window``)."""
    if boundary is None:
        return ()
    return tuple(block.block_id for block in blocks.open if block.block_id < boundary)[
        :LIVE_KEEP_LIMIT
    ]


@dataclass(frozen=True, kw_only=True)
class _Window[K]:
    """One section's live window (see "Live refresh"): its boundary (``None``: no
    closed or stopped row was shown, so everything), the time from which a row that
    closes joins it, and the rows open or running below the boundary when it was set,
    which join it however their close time reads (a sweep stops an agent at its last
    event, a late hook closes a block at its receive time)."""

    start: K | None
    since_ms: int
    keep: tuple[K, ...]


def _live_link(session_id: str, blocks: _Window[int], agents: _Window[str]) -> LiveLink:
    query: list[tuple[str, object]] = []
    for prefix, window in (("blocks", blocks), ("agents", agents)):
        if window.start is not None:
            query.append((f"{prefix}_from", window.start))
        query.append((f"{prefix}_since", window.since_ms))
    query.extend(("keep_block", block_id) for block_id in blocks.keep)
    query.extend(("keep_agent", agent_id) for agent_id in agents.keep)
    href = "/ui/sessions/" + quote(session_id, safe="") + "/live?" + urlencode(query)
    return LiveLink(href=href)


def _usage_href(session_id: str) -> str:
    return "/ui/sessions/" + quote(session_id, safe="") + "/usage"


def _live_stream(session_id: str) -> str:
    return STREAM_PATH + "?" + urlencode({"session": session_id})


def _session_read(repository: Repository, session_id: str, limit: int) -> SessionRead | None:
    detail = repository.get_session(session_id)
    if detail is None:
        return None
    events = repository.list_events(session_id, limit)
    return SessionRead(detail=detail, events=events[::-1])


def _not_found_page(request: Request) -> HTMLResponse:
    return _render(
        "not_found.html",
        status_code=_NOT_FOUND,
        nav=None,
        grafana_home=_grafana_home(request),
        error=_NO_SESSION,
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_page(
    request: Request, repository: _Repo, reads: _Reads, session_id: str
) -> Response:
    """One session: its header, blocks, agents and hook timeline."""
    if not is_valid_session_id(session_id):
        return _not_found_page(request)
    now_ms = _now_ms(request)
    found = await _read(reads, _session_read, repository, session_id, EVENTS_PAGE_SIZE + 1)
    if isinstance(found, ErrorView):
        return _render(
            "session.html",
            status_code=_SERVICE_UNAVAILABLE,
            nav=None,
            grafana_home=_grafana_home(request),
            error=found,
        )
    if found is None:
        return _not_found_page(request)
    detail = found.detail
    blocks = _blocks_region(detail, now_ms)
    # The oldest closed block and stopped agent shown: the "Show older" cursors, and the
    # live refresh's boundaries.
    blocks_from = blocks.closed[-1].block_id if blocks.closed else None
    blocks_next = None
    if detail.blocks_total > len(detail.blocks) and blocks_from is not None:
        blocks_next = _session_href(session_id, "blocks", before=blocks_from)
    shown = _agents_region(detail, now_ms)
    agents_from = shown.boundary
    agents_next = None
    if detail.agents_total > len(detail.agents) and agents_from is not None:
        # Every running agent is on this page already.
        agents_next = _session_href(session_id, "agents", before=agents_from)
    events = found.events[:EVENTS_PAGE_SIZE]
    events_next = None
    if len(found.events) > EVENTS_PAGE_SIZE:
        events_next = _session_href(session_id, "events", before=events[-1].seq)
    return _render(
        "session.html",
        nav=None,
        grafana_home=_grafana_home(request),
        error=None,
        grafana=_grafana_session(request, detail.session),
        live_stream=_live_stream(session_id),
        usage_href=_usage_href(session_id),
        live=_live_link(
            session_id,
            _Window(start=blocks_from, since_ms=now_ms, keep=_kept_blocks(blocks, blocks_from)),
            _Window(start=agents_from, since_ms=now_ms, keep=shown.kept()),
        ),
        head=_session_head(detail, now_ms),
        blocks=blocks,
        blocks_more=More(href=blocks_next),
        agents=shown.region,
        agents_more=More(href=agents_next),
        events=[_event_view(row, now_ms) for row in events],
        events_more=More(href=events_next),
        tail=_tail(session_id, events[0].seq if events else 0, now=False),
    )


# The session page's fragments. Each reads once, through one call on the gate; None
# means the session is unknown. Their error responses (400, 404, 503) are fragments too,
# but htmx 2's default responseHandling does not swap a 4xx or 5xx response: the "Show
# older" button or the tail stays as it was, and a later click or change retries.


def _parse_seq(value: str | None) -> int | None:
    if value is None or _SEQ.fullmatch(value) is None:
        return None
    number = int(value)
    return number if number <= _MAX_CURSOR else None


def _fragment_error(error: ErrorView, status_code: int) -> HTMLResponse:
    return _render("_error.html", status_code=status_code, error=error)


def _blocks_read(
    repository: Repository, session_id: str, limit: int, before: int
) -> list[Block] | None:
    if not repository.session_exists(session_id):
        return None
    return repository.list_blocks(session_id, limit, before)


@router.get("/ui/sessions/{session_id}/blocks", response_class=HTMLResponse)
async def blocks_fragment(
    request: Request,
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before: str | None = None,
) -> Response:
    """The closed blocks just older than block ``before``, newest first, and the next
    "Show older blocks" control, out of band."""
    if not is_valid_session_id(session_id):
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    cursor = _parse_seq(before)
    if cursor is None:
        return _fragment_error(_BAD_LINK, _BAD_REQUEST)
    now_ms = _now_ms(request)
    found = await _read(reads, _blocks_read, repository, session_id, BLOCKS_PAGE_SIZE + 1, cursor)
    if isinstance(found, ErrorView):
        return _fragment_error(found, _SERVICE_UNAVAILABLE)
    if found is None:
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    # Oldest first, one more than a page when an older page exists.
    page = found[-BLOCKS_PAGE_SIZE:]
    next_href = None
    if len(found) > BLOCKS_PAGE_SIZE:
        next_href = _session_href(session_id, "blocks", before=page[0].block_id)
    # Open blocks are on the session page already, all of them.
    rows = [
        _block_view(block, now_ms) for block in reversed(page) if block.closed_at_ms is not None
    ]
    return _render("_blocks_page.html", blocks=rows, more=More(href=next_href))


class _UnknownAgent:
    """An agent cursor that names no agent of the session."""


_UNKNOWN_AGENT: Final = _UnknownAgent()


def _agents_read(
    repository: Repository, session_id: str, limit: int, before: str
) -> AgentPage | _UnknownAgent | None:
    if not repository.session_exists(session_id):
        return None
    page = repository.list_agents(session_id, limit, before)
    return _UNKNOWN_AGENT if page is None else page


@router.get("/ui/sessions/{session_id}/agents", response_class=HTMLResponse)
async def agents_fragment(
    request: Request,
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before: str | None = None,
) -> Response:
    """The stopped agents first seen just before agent ``before``, as a tree, and the
    next "Show older agents" control, out of band."""
    if not is_valid_session_id(session_id):
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    if before is None or not is_valid_session_id(before):
        return _fragment_error(_BAD_LINK, _BAD_REQUEST)
    now_ms = _now_ms(request)
    found = await _read(reads, _agents_read, repository, session_id, AGENTS_PAGE_SIZE + 1, before)
    if isinstance(found, ErrorView):
        return _fragment_error(found, _SERVICE_UNAVAILABLE)
    if isinstance(found, _UnknownAgent):
        return _fragment_error(_BAD_LINK, _BAD_REQUEST)
    if found is None:
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    # Oldest first, one more than a page when an older page exists.
    page = found.agents[-AGENTS_PAGE_SIZE:]
    next_href = None
    if len(found.agents) > AGENTS_PAGE_SIZE:
        next_href = _session_href(session_id, "agents", before=page[0].agent_id)
    # Running agents are on the session page already, all of them.
    stopped = [agent for agent in page if agent.status != "running"]
    return _render(
        "_agents_page.html",
        agents=_agent_views(stopped, found.parents, now_ms),
        more=More(href=next_href),
    )


def _events_read(
    repository: Repository,
    session_id: str,
    limit: int,
    before: int | None,
    after: int | None,
) -> list[HookEventRow] | None:
    if not repository.session_exists(session_id):
        return None
    return repository.list_events(session_id, limit, before_seq=before, after_seq=after)


@router.get("/ui/sessions/{session_id}/events", response_class=HTMLResponse)
async def events_fragment(
    request: Request,
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    before: str | None = None,
    after: str | None = None,
) -> Response:
    """With ``before``: the events just older than it, newest first, and the next "Show
    older events" control, out of band. With ``after``: the events newer than it, to
    prepend to the timeline out of band, and a new tail. One of the two, never both."""
    if not is_valid_session_id(session_id):
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    older, newer = _parse_seq(before), _parse_seq(after)
    if (before is None) == (after is None) or (older is None and newer is None):
        return _fragment_error(_BAD_LINK, _BAD_REQUEST)
    now_ms = _now_ms(request)
    found = await _read(
        reads, _events_read, repository, session_id, EVENTS_PAGE_SIZE + 1, older, newer
    )
    if isinstance(found, ErrorView):
        return _fragment_error(found, _SERVICE_UNAVAILABLE)
    if found is None:
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    if newer is not None:
        # Oldest first from the cursor: the page is the next EVENTS_PAGE_SIZE events.
        page = found[:EVENTS_PAGE_SIZE]
        cursor = page[-1].seq if page else newer
        return _render(
            "_events_tail.html",
            events=[_event_view(row, now_ms) for row in reversed(page)],
            tail=_tail(session_id, cursor, now=len(found) > EVENTS_PAGE_SIZE),
            # A tail from 0 belongs to a page that showed no event, and its empty message.
            first=newer == 0,
        )
    page = found[-EVENTS_PAGE_SIZE:]
    next_href = None
    if len(found) > EVENTS_PAGE_SIZE:
        next_href = _session_href(session_id, "events", before=page[0].seq)
    return _render(
        "_events_page.html",
        events=[_event_view(row, now_ms) for row in reversed(page)],
        more=More(href=next_href),
    )


def _live_read(
    repository: Repository, session_id: str, blocks: _Window[int], agents: _Window[str]
) -> SessionDetail | None:
    # One more than the cap: whether the window grew past it.
    return repository.get_session(
        session_id,
        closed_limit=LIVE_CLOSED_LIMIT + 1,
        blocks_from=blocks.start,
        agents_from=agents.start,
        blocks_since_ms=blocks.since_ms,
        agents_since_ms=agents.since_ms,
        keep_blocks=blocks.keep,
        keep_agents=agents.keep,
    )


def _parse_windows(
    query: Mapping[str, str | None], keep_block: Sequence[str], keep_agent: Sequence[str]
) -> tuple[_Window[int], _Window[str]] | None:
    """The two windows of a live link; ``None`` if any part is not valid."""
    blocks_since = _parse_seq(query["blocks_since"])
    agents_since = _parse_seq(query["agents_since"])
    blocks_from, agents_from = query["blocks_from"], query["agents_from"]
    blocks_start = None if blocks_from is None else _parse_seq(blocks_from)
    kept_blocks = [_parse_seq(value) for value in keep_block]
    if (
        blocks_since is None
        or agents_since is None
        or (blocks_from is not None and blocks_start is None)
        or (agents_from is not None and not is_valid_session_id(agents_from))
        or len(keep_block) > LIVE_KEEP_LIMIT
        or len(keep_agent) > LIVE_KEEP_LIMIT
        or not all(is_valid_session_id(value) for value in keep_agent)
    ):
        return None
    blocks_keep = tuple(block_id for block_id in kept_blocks if block_id is not None)
    if len(blocks_keep) != len(kept_blocks):
        return None
    return (
        _Window(start=blocks_start, since_ms=blocks_since, keep=blocks_keep),
        _Window(start=agents_from, since_ms=agents_since, keep=tuple(keep_agent)),
    )


@router.get("/ui/sessions/{session_id}/live", response_class=HTMLResponse)
async def live_fragment(
    request: Request,
    repository: _Repo,
    reads: _Reads,
    session_id: str,
    blocks_since: str | None = None,
    agents_since: str | None = None,
    blocks_from: str | None = None,
    agents_from: str | None = None,
    keep_block: Annotated[list[str] | None, Query()] = None,
    keep_agent: Annotated[list[str] | None, Query()] = None,
) -> Response:
    """The header, blocks and agents again, out of band, and a new live element (see
    "Live refresh"): for each section its boundary (``*_from``, when the page showed a
    closed block or a stopped agent), the time from which closed rows join
    (``*_since``) and the rows open or running below the boundary (``keep_*``)."""
    if not is_valid_session_id(session_id):
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    windows = _parse_windows(
        {
            "blocks_since": blocks_since,
            "agents_since": agents_since,
            "blocks_from": blocks_from,
            "agents_from": agents_from,
        },
        keep_block or [],
        keep_agent or [],
    )
    if windows is None:
        return _fragment_error(_BAD_LINK, _BAD_REQUEST)
    block_window, agent_window = windows
    # Before the read: a window that starts over takes rows closed from here on.
    now_ms = _now_ms(request)
    found = await _read(reads, _live_read, repository, session_id, block_window, agent_window)
    if isinstance(found, ErrorView):
        return _fragment_error(found, _SERVICE_UNAVAILABLE)
    if found is None:
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    blocks = _blocks_region(found, now_ms)
    blocks_more = None
    if len(blocks.closed) > LIVE_CLOSED_LIMIT:
        # The window starts over at the latest page, with a pager below it.
        blocks = _blocks_region(found, now_ms, keep=BLOCKS_PAGE_SIZE)
        start = blocks.closed[-1].block_id
        block_window = _Window(start=start, since_ms=now_ms, keep=_kept_blocks(blocks, start))
        blocks_more = More(href=_session_href(session_id, "blocks", before=start))
    shown = _agents_region(found, now_ms)
    agents_more = None
    if len(shown.stopped) > LIVE_CLOSED_LIMIT:
        shown = _agents_region(found, now_ms, keep=AGENTS_PAGE_SIZE)
        boundary = shown.boundary
        agent_window = _Window(start=boundary, since_ms=now_ms, keep=shown.kept())
        agents_more = More(href=_session_href(session_id, "agents", before=boundary))
    return _render(
        "_session_live.html",
        live=_live_link(session_id, block_window, agent_window),
        head=_session_head(found, now_ms),
        blocks=blocks,
        blocks_more=blocks_more,
        agents=shown.region,
        agents_more=agents_more,
    )


# Tokens and cost.


@dataclass(frozen=True, kw_only=True)
class UsageRow:
    """One row of a usage table: a name (``mono`` for a model's) and its formatted
    values; ``None`` where Prometheus had no series."""

    name: str
    tokens: str | None
    cost: str | None = None
    mono: bool = False


@dataclass(frozen=True, kw_only=True)
class UsageView:
    total_cost: str
    total_tokens: str
    types: list[UsageRow]
    models: list[UsageRow]


_USAGE_MESSAGES: Final = {
    "error": "Prometheus is unavailable, so tokens and cost can't be shown right now.",
    "timeout": "Prometheus is unavailable, so tokens and cost can't be shown right now.",
    "invalid": "Prometheus answered with values the app can't read, so tokens and cost"
    " can't be shown right now.",
}


def _usage_view(usage: SessionUsage) -> UsageView:
    return UsageView(
        total_cost=format_usd(usage.total_cost),
        total_tokens=format_tokens(usage.total_tokens),
        types=[
            UsageRow(name=token_type_label(name), tokens=format_tokens(value))
            for name, value in usage.tokens_by_type
        ],
        models=[
            UsageRow(
                name=row.model or "Unknown model",
                mono=bool(row.model),
                tokens=None if row.tokens is None else format_tokens(row.tokens),
                cost=None if row.cost is None else format_usd(row.cost),
            )
            for row in model_rows(usage)
        ],
    )


def _usage_read(repository: Repository, session_id: str) -> Session | None:
    return repository.find_session(session_id)


def _usage_fragment(*, usage: UsageView | None = None, message: str | None = None) -> Response:
    return _render("_usage.html", usage=usage, message=message)


@router.get("/ui/sessions/{session_id}/usage", response_class=HTMLResponse)
async def usage_fragment(
    request: Request, repository: _Repo, reads: _Reads, session_id: str
) -> Response:
    """The session's "Tokens and cost" section (see "Tokens and cost")."""
    if not is_valid_session_id(session_id):
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    # The read gate's slot is released before Prometheus is asked.
    found = await _read(reads, _usage_read, repository, session_id)
    if isinstance(found, ErrorView):
        return _usage_fragment(message=f"{found.title}. {found.detail}")
    if found is None:
        return _fragment_error(_NO_SESSION, _NOT_FOUND)
    window = usage_window(found.started_at_ms, _last_ms(found), _now_ms(request))
    reader: UsageReader = request.app.state.usage
    usage = await reader.read(session_id, window)
    if isinstance(usage, UsageUnavailable):
        return _usage_fragment(message=_USAGE_MESSAGES[usage.reason])
    return _usage_fragment(usage=None if usage.empty else _usage_view(usage))


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
