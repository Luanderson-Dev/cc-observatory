"""The alerts page (ADR 0044): what the app warns you about, and the warnings it sent.

- ``GET /alerts``: the history, newest first, then the alert settings. The history is a
  list of strips, one per alert: what happened in words
  (``presentation.describe_alert``), the session's project name and short ID with a
  link to the session (a daily alert is about every session, so it has neither), when
  it fired, and whether it is still on or cleared, and after how long. It pages by
  alert ``id`` like ``/api/v1/alerts``: ``?before=<id>`` shows the next older alerts.
  The settings are the form of ``ccobs.server.alert_settings`` with the same saved
  values, a line that says whether alerts also go to Discord, and an empty slot for
  the browser notifications control.
- ``GET /ui/alerts/history``: the history alone, with the same ``before``. The history
  element refetches it on ``ccobs:alerts``, which live.js dispatches on each alerts ping
  of the stream and each time the stream opens (ADR 0044), and replaces itself.

Each request reads once through the read gate (ADR 0033): the page of alerts with their
sessions' ``cwd`` (``Repository.alert_views``, the API's query) and the host's saved
settings, in one call, so the empty history's invitation names the values in force, the
saved ones over the environment's. ``cwd`` becomes ``presentation.project_name`` before
any template sees it (product decision P2). Of Discord the page knows only whether a
webhook is configured and whether messages name the project, never its URL (P7). A busy
gate or a failed read is a 503 that says what failed; a cursor that is not an alert ID
is a 400 that does not echo it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from ccobs.domain.alerts import AlertConfig, apply_saved_settings
from ccobs.domain.presentation import (
    alerts_invitation,
    describe_alert,
    format_duration,
    project_name,
)
from ccobs.server.alert_settings import settings_context
from ccobs.server.db.repository import AlertSettingValue, AlertView, Repository
from ccobs.server.reads import ReadGate
from ccobs.server.ui import ErrorView, Moment, gated_read, grafana_home, moment, render

__all__ = [
    "ALERTS_PAGE_SIZE",
    "HISTORY_PATH",
    "PAGE_PATH",
    "AlertItem",
    "DiscordView",
    "HistoryView",
    "router",
]

PAGE_PATH: Final = "/alerts"
HISTORY_PATH: Final = "/ui/alerts/history"
ALERTS_PAGE_SIZE: Final = 50
_ID: Final = re.compile(r"[0-9]{1,19}")
_MAX_ID: Final = 2**63 - 1
_SHORT_ID: Final = 8
_BAD_REQUEST: Final = 400
_SERVICE_UNAVAILABLE: Final = 503
_BAD_CURSOR: Final = ErrorView(
    title="This page link is not valid",
    detail="Start again from the newest alerts.",
)


@dataclass(frozen=True, kw_only=True)
class AlertItem:
    """One alert as a strip. ``session_href`` and ``short_id`` are ``None`` for a daily
    alert; ``project`` too, and for a session with no ``cwd``."""

    on: bool
    what: str
    state: str
    session_href: str | None
    short_id: str | None
    project: str | None
    fired: Moment


@dataclass(frozen=True, kw_only=True)
class HistoryView:
    """A page of the history: its alerts, whether it is an older page, the next older
    page's URL, the URL that refetches it, and what an empty first page says."""

    items: list[AlertItem]
    paged: bool
    next_href: str | None
    refresh_href: str
    invitation: str


@dataclass(frozen=True, kw_only=True)
class DiscordView:
    configured: bool
    names_project: bool


@dataclass(frozen=True, kw_only=True)
class _Read:
    # Newest first, one more than a page when an older page exists.
    alerts: list[AlertView]
    saved: dict[str, AlertSettingValue]


def _read(repository: Repository, host: str, limit: int, before: int | None) -> _Read:
    return _Read(
        alerts=repository.alert_views(host, limit, before_id=before),
        saved=repository.alert_settings(host),
    )


def _cursor(before: str | None) -> int | None:
    """The alert ID of ``before``; ``None`` when it is not one."""
    if before is None or _ID.fullmatch(before) is None:
        return None
    value = int(before)
    return value if value <= _MAX_ID else None


def _item(view: AlertView, now_ms: int) -> AlertItem:
    alert = view.alert
    cleared = alert.cleared_at_ms
    session_id = alert.session_id
    state = "Still on"
    if cleared is not None:
        state = f"Cleared after {format_duration(cleared - alert.fired_at_ms)}"
    return AlertItem(
        on=cleared is None,
        what=describe_alert(alert.rule, alert.value, alert.threshold, cleared=cleared is not None),
        state=state,
        session_href=None if session_id is None else "/sessions/" + quote(session_id, safe=""),
        short_id=None if session_id is None else session_id[:_SHORT_ID],
        project=project_name(view.cwd),
        fired=moment(alert.fired_at_ms, now_ms),
    )


def _with_before(path: str, before: int | None) -> str:
    return path if before is None else path + "?" + urlencode({"before": before})


def _history(request: Request, found: _Read, before: int | None, now_ms: int) -> HistoryView:
    shown = found.alerts[:ALERTS_PAGE_SIZE]
    next_href = None
    if len(found.alerts) > ALERTS_PAGE_SIZE:
        next_href = _with_before(PAGE_PATH, shown[-1].alert.id)
    env: AlertConfig = request.app.state.alert_env_config
    config, _ = apply_saved_settings(env, found.saved)
    return HistoryView(
        items=[_item(view, now_ms) for view in shown],
        paged=before is not None,
        next_href=next_href,
        refresh_href=_with_before(HISTORY_PATH, before),
        invitation=alerts_invitation(config),
    )


def _discord(request: Request) -> DiscordView:
    return DiscordView(
        configured=request.app.state.discord_configured,
        names_project=request.app.state.discord_include_project,
    )


def _page(
    request: Request,
    *,
    status_code: int = 200,
    error: ErrorView | None = None,
    history: HistoryView | None = None,
    saved: Mapping[str, AlertSettingValue] | None = None,
) -> HTMLResponse:
    settings = {} if saved is None else settings_context(request, saved)
    return render(
        "alerts.html",
        status_code=status_code,
        nav="alerts",
        grafana_home=grafana_home(request),
        error=error,
        history=history,
        discord=_discord(request),
        **settings,
    )


def _fragment_error(error: ErrorView, status_code: int) -> HTMLResponse:
    return render("_error.html", status_code=status_code, error=error)


async def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.read_repository
    return repository


async def _reads(request: Request) -> ReadGate:
    reads: ReadGate = request.app.state.reads
    return reads


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]


async def _load(
    request: Request, repository: Repository, reads: ReadGate, before: int | None
) -> _Read | ErrorView:
    host: str = request.app.state.host_label
    # One more than a page: whether an older page exists.
    return await gated_read(reads, _read, repository, host, ALERTS_PAGE_SIZE + 1, before)


def _now_ms(request: Request) -> int:
    now_ms: int = request.app.state.clock()
    return now_ms


router = APIRouter(include_in_schema=False)


@router.get(PAGE_PATH, response_class=HTMLResponse)
async def alerts(
    request: Request, repository: _Repo, reads: _Reads, before: str | None = None
) -> Response:
    """The alerts page: the history, then the settings."""
    cursor = _cursor(before)
    if before is not None and cursor is None:
        # The value is not echoed.
        return _page(request, status_code=_BAD_REQUEST, error=_BAD_CURSOR)
    now_ms = _now_ms(request)
    found = await _load(request, repository, reads, cursor)
    if isinstance(found, ErrorView):
        return _page(request, status_code=_SERVICE_UNAVAILABLE, error=found)
    return _page(request, history=_history(request, found, cursor, now_ms), saved=found.saved)


@router.get(HISTORY_PATH, response_class=HTMLResponse)
async def history(
    request: Request, repository: _Repo, reads: _Reads, before: str | None = None
) -> Response:
    """The history alone, for htmx to swap in place of ``#alert-history``."""
    cursor = _cursor(before)
    if before is not None and cursor is None:
        return _fragment_error(_BAD_CURSOR, _BAD_REQUEST)
    now_ms = _now_ms(request)
    found = await _load(request, repository, reads, cursor)
    if isinstance(found, ErrorView):
        return _fragment_error(found, _SERVICE_UNAVAILABLE)
    return render("_alert_history.html", history=_history(request, found, cursor, now_ms))
