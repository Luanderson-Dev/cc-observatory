"""The alert settings fragment and its state-changing routes (ADRs 0042 and 0045).

- ``GET /ui/alerts/settings``: the fragment, a form with each alert setting's effective
  value and where it comes from: saved here, the ``CCOBS_ALERT_*`` environment, or the
  built-in default (ADR 0042's precedence). The settings are grouped by rule in plain
  words, each value with its unit; no setting or variable name shows as text. It reads
  the saved values through the read gate, like every UI read (ADR 0033). The alerts page
  (``ccobs.server.alerts_page``) renders the same form from ``settings_context``.
- ``POST /ui/alerts/settings``: save the settings the form holds whose value differs
  from the one in force; the others keep their values and their sources, so a setting
  submitted unchanged stays saved, or keeps following the environment (ADR 0045). Each
  is parsed by its type (``true`` or ``false`` for a switch, digits for a whole number,
  digits with an optional decimal part for a dollar amount; a name may repeat, the last
  value wins, so a hidden ``false`` before a checkbox's ``true`` sends an unchecked box)
  and checked against its bounds (``apply_settings``), every submitted one, changed or
  not. On any error nothing is saved, and the fragment comes back with each bad field's
  error, which names the field and its range but never the value typed, and each
  submitted field refilled with the text typed, so the user sees what to fix. With no
  change, nothing is written and the fragment says so.
- ``POST /ui/alerts/settings/reset``: drop the saved value of ``field``, or every saved
  value without it; the settings fall back to the environment, then the defaults.

A save or reset that took answers with ``HX-Trigger: ccobs:alerts``, the event live.js
dispatches on each alerts ping (ADR 0044): it bubbles to body, so the alerts page's
history refetches and its empty state names the rules now on. Errors, refusals and a
save with no change send no event.

Only a same-origin htmx request reaches the POST routes (``guards.UnsafeMethodGuard``,
ADR 0045). The writer applies each change in one transaction and reloads the effective
configuration, which the next alert tick uses; the route waits for it on the event loop
(``asyncio.wrap_future``) for up to ``app.state.settings_timeout_s`` (5 s by default).
Without an answer in time, or when the writer cannot take the change, it answers 503;
a change the writer had not started by then is cancelled and never applied. The body of
a form is read up to ``FORM_MAX_BYTES``. Errors and answers hold the setting names and
ranges only: no submitted value reaches an error message or a log line; the only place
one shows is the user's own input, refilled after an error (autoescaped). Validation
errors answer 200, since htmx swaps only 2xx answers by default.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Protocol
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from ccobs.domain.alerts import (
    ALERT_SETTINGS,
    AlertConfig,
    FieldError,
    apply_saved_settings,
    apply_settings,
    setting_error,
    setting_value,
)
from ccobs.server.db.repository import ALERT_SETTING_TYPES, AlertSettingValue, Repository
from ccobs.server.reads import ReadGate
from ccobs.server.ui import ErrorView, render
from ccobs.server.writer import AlertSettingsResult, AlertSettingsUnavailableError

__all__ = [
    "FORM_MAX_BYTES",
    "POST_PATHS",
    "RESET_PATH",
    "SETTINGS_PATH",
    "SETTINGS_WRITE_TIMEOUT_S",
    "AlertSettingsTarget",
    "SettingGroup",
    "SettingView",
    "router",
    "settings_context",
]

_log = logging.getLogger(__name__)

SETTINGS_PATH: Final = "/ui/alerts/settings"
RESET_PATH: Final = "/ui/alerts/settings/reset"
# The only routes besides the hook route that take a POST (ADR 0045).
POST_PATHS: Final = frozenset({SETTINGS_PATH, RESET_PATH})
# How long a POST waits for the writer to apply its change (ADR 0045).
SETTINGS_WRITE_TIMEOUT_S: Final = 5.0
# The form holds ten short fields; anything much larger is not ours.
FORM_MAX_BYTES: Final = 4096
_FORM_MAX_FIELDS: Final = 64
_RESET_FIELD: Final = "field"
_TEMPLATE: Final = "_alert_settings.html"
_TRIGGER_HEADER: Final = "HX-Trigger"
# The event live.js dispatches on each alerts ping (ADR 0044).
_ALERTS_EVENT: Final = "ccobs:alerts"

_BAD_REQUEST: Final = 400
_TOO_LARGE: Final = 413
_SERVICE_UNAVAILABLE: Final = 503

_WHOLE: Final = re.compile(r"[0-9]{1,15}")
_AMOUNT: Final = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,15})?")
_SWITCH: Final = {"true": True, "false": False}

type Kind = Literal["switch", "whole", "amount"]
type Source = Literal["saved", "env", "default"]

_KINDS: Final[Mapping[type, Kind]] = {bool: "switch", int: "whole", float: "amount"}
_SWITCH_LABEL: Final = "Warn me"
_SECONDS: Final = "seconds"
_DOLLARS: Final = "US dollars"
# Each value's label and unit; a switch is labelled _SWITCH_LABEL in its rule's group.
_VALUE_LABELS: Final[Mapping[str, tuple[str, str]]] = {
    "alert_waiting_threshold_s": ("After", _SECONDS),
    "alert_failures_count": ("After", "failures"),
    "alert_failures_window_s": ("Within", _SECONDS),
    "alert_session_cost_usd": ("Limit", _DOLLARS),
    "alert_daily_cost_usd": ("Limit", _DOLLARS),
    "alert_cooldown_s": ("Wait at least", _SECONDS),
}
_COST_NOTE: Final = "Cost is Claude Code's estimate, not your bill."
# The form's groups, one per rule and its values, in ALERT_SETTINGS order; the note
# shows under its group.
_GROUPS: Final[tuple[tuple[str, tuple[str, ...], str | None], ...]] = (
    ("When a session waits on you", ("alert_waiting_enabled", "alert_waiting_threshold_s"), None),
    (
        "When API requests keep failing",
        ("alert_failures_enabled", "alert_failures_count", "alert_failures_window_s"),
        None,
    ),
    (
        "When a session costs more than",
        ("alert_session_cost_enabled", "alert_session_cost_usd"),
        None,
    ),
    (
        "When the last 24 hours cost more than",
        ("alert_daily_cost_enabled", "alert_daily_cost_usd"),
        _COST_NOTE,
    ),
    ("Between two alerts of the same kind", ("alert_cooldown_s",), None),
)
_SOURCE_LABELS: Final[Mapping[str, str]] = {
    "saved": "Saved here",
    "env": "From the environment",
    "default": "Default",
}

_SAVED_MESSAGE: Final = "Saved."
_NO_CHANGES_MESSAGE: Final = "No changes to save."
_RESET_MESSAGE: Final = "Reset."
_NOT_SAVED_MESSAGE: Final = "Nothing was saved: fix the fields marked below."
_BUSY: Final = ErrorView(
    title="The database is busy",
    detail="Too many reads are waiting for the database. Reload in a few seconds.",
)
_BROKEN: Final = ErrorView(
    title="The app could not read its database",
    detail="Reading the alert settings failed. The app's log (docker compose logs app) says why.",
)
_NOT_SAVED: Final = ErrorView(
    title="The settings were not saved",
    detail="The app did not save them in time. Reload to see the settings in force.",
)
_BAD_FORM: Final = ErrorView(
    title="The settings were not saved",
    detail="The form could not be read. Reload the page and try again.",
)
_TOO_LARGE_FORM: Final = ErrorView(
    title="The settings were not saved",
    detail="The form was too large. Reload the page and try again.",
)
_UNKNOWN_SETTING: Final = ErrorView(
    title="The settings were not reset",
    detail="That is not an alert setting. Reload the page and try again.",
)


class AlertSettingsTarget(Protocol):
    """What the routes need of the writer (``Writer``)."""

    def submit_alert_settings_save(
        self, values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> Future[AlertSettingsResult]: ...

    def submit_alert_settings_reset(self, field: str | None) -> Future[AlertSettingsResult]: ...


@dataclass(frozen=True, kw_only=True)
class SettingView:
    """One setting as the fragment shows it: its value in force as text (``on`` for a
    switch), or the text submitted after an error, and where the value in force comes
    from."""

    name: str
    label: str
    unit: str | None
    kind: Kind
    value: str
    on: bool
    source: Source
    source_label: str
    error: str | None


@dataclass(frozen=True, kw_only=True)
class SettingGroup:
    """One rule's settings, under its legend, with an optional note below them."""

    legend: str
    settings: list[SettingView]
    note: str | None


class _FormError(Exception):
    def __init__(self, status_code: int, view: ErrorView) -> None:
        super().__init__(view.title)
        self.status_code = status_code
        self.view = view


# Views.


def _text(value: bool | int | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # Up to 15 significant digits: what was saved, without float noise (0.1 stays 0.1).
    return format(value, ".15g")


def _plain_error(error: FieldError) -> str:
    """An error as the form shows it under its field: without the setting's name, which
    the field's label stands for (``Must be a whole number of seconds from 5 to 86400.``)."""
    rest = error.message.removeprefix(f"{error.field} ")
    return f"{rest[:1].upper()}{rest[1:]}."


def _groups(views: list[SettingView]) -> list[SettingGroup]:
    by_name = {view.name: view for view in views}
    return [
        SettingGroup(legend=legend, settings=[by_name[name] for name in names], note=note)
        for legend, names, note in _GROUPS
    ]


def _views(
    env: AlertConfig,
    env_fields: frozenset[str],
    saved: Mapping[str, AlertSettingValue],
    errors: tuple[FieldError, ...],
    submitted: Mapping[str, str],
) -> list[SettingView]:
    """Each setting in ``ALERT_SETTINGS`` order, with its ``submitted`` text in place of
    the value in force. A saved value the configuration ignores (out of bounds, ADR 0042)
    is not the one in force, so it is not shown as saved."""
    config, ignored = apply_saved_settings(env, saved)
    messages = {error.field: _plain_error(error) for error in errors}
    views = []
    for name in ALERT_SETTINGS:
        source: Source
        if name in saved and name not in ignored:
            source = "saved"
        elif name in env_fields:
            source = "env"
        else:
            source = "default"
        value = setting_value(config, name)
        text = submitted.get(name)
        label, unit = _VALUE_LABELS.get(name, (_SWITCH_LABEL, None))
        views.append(
            SettingView(
                name=name,
                label=label,
                unit=unit,
                kind=_KINDS[ALERT_SETTING_TYPES[name]],
                value=_text(value) if text is None else text,
                on=value is True if text is None else text.strip() == "true",
                source=source,
                source_label=_SOURCE_LABELS[source],
                error=messages.get(name),
            )
        )
    return views


def settings_context(
    request: Request,
    saved: Mapping[str, AlertSettingValue],
    *,
    errors: tuple[FieldError, ...] = (),
    message: str | None = None,
    submitted: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """What ``_alert_settings.html`` needs: the settings in force with ``saved`` over the
    environment (or the ``submitted`` text, after an error), grouped by rule, a status
    message, and the form's routes."""
    env: AlertConfig = request.app.state.alert_env_config
    env_fields: frozenset[str] = request.app.state.alert_env_fields
    return {
        "groups": _groups(_views(env, env_fields, saved, errors, submitted or {})),
        "message": message,
        "settings_path": SETTINGS_PATH,
        "reset_path": RESET_PATH,
    }


def _fragment(
    request: Request,
    saved: Mapping[str, AlertSettingValue],
    *,
    errors: tuple[FieldError, ...] = (),
    message: str | None = None,
    submitted: Mapping[str, str] | None = None,
) -> HTMLResponse:
    context = settings_context(request, saved, errors=errors, message=message, submitted=submitted)
    return render(_TEMPLATE, status_code=200, **context)


def _changed(response: HTMLResponse) -> HTMLResponse:
    """``response`` to a change that took: htmx dispatches ``ccobs:alerts`` on the form,
    which bubbles to body, so the history refetches and its empty state names the
    rules now on."""
    response.headers[_TRIGGER_HEADER] = _ALERTS_EVENT
    return response


def _error(status_code: int, view: ErrorView) -> HTMLResponse:
    return render("_error.html", status_code=status_code, error=view)


# Reads and writes.


async def _repository(request: Request) -> Repository:
    repository: Repository = request.app.state.read_repository
    return repository


async def _reads(request: Request) -> ReadGate:
    reads: ReadGate = request.app.state.reads
    return reads


_Repo = Annotated[Repository, Depends(_repository)]
_Reads = Annotated[ReadGate, Depends(_reads)]


async def _saved(
    request: Request, repository: Repository, reads: ReadGate
) -> dict[str, AlertSettingValue] | ErrorView:
    """The host's saved values through the read gate, or the error to show: a busy gate
    (the only HTTPException it raises) or a failed query, logged by type only."""
    try:
        return await reads.run(repository.alert_settings, request.app.state.host_label)
    except HTTPException:
        _log.warning("alert settings read refused: database busy")
        return _BUSY
    except SQLAlchemyError as error:
        _log.error("alert settings read failed (%s)", type(error).__name__)
        return _BROKEN


async def _form(request: Request) -> dict[str, str]:
    """The urlencoded form, the last value of each name; ``_FormError`` when it is
    larger than ``FORM_MAX_BYTES``, not ASCII, or holds too many fields."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > FORM_MAX_BYTES:
            raise _FormError(_TOO_LARGE, _TOO_LARGE_FORM)
    try:
        pairs = parse_qsl(
            body.decode("ascii"), keep_blank_values=True, max_num_fields=_FORM_MAX_FIELDS
        )
    except ValueError:
        raise _FormError(_BAD_REQUEST, _BAD_FORM) from None
    return dict(pairs)


def _changes(
    request: Request,
    saved: Mapping[str, AlertSettingValue],
    values: Mapping[str, AlertSettingValue],
) -> dict[str, AlertSettingValue]:
    """The ``values`` that differ from the ones in force (``saved`` over the
    environment), compared as typed values: ``060`` seconds is no change from 60."""
    env: AlertConfig = request.app.state.alert_env_config
    config, _ = apply_saved_settings(env, saved)
    return {name: value for name, value in values.items() if value != setting_value(config, name)}


def _parse(form: Mapping[str, str]) -> tuple[dict[str, AlertSettingValue], set[str]]:
    """The settings the form holds, by type, and the names of those that do not parse.
    Other names are ignored."""
    values: dict[str, AlertSettingValue] = {}
    bad: set[str] = set()
    for name in ALERT_SETTINGS:
        if name not in form:
            continue
        text = form[name].strip()
        kind = ALERT_SETTING_TYPES[name]
        if kind is bool and text in _SWITCH:
            values[name] = _SWITCH[text]
        elif kind is int and _WHOLE.fullmatch(text):
            values[name] = int(text)
        elif kind is float and _AMOUNT.fullmatch(text):
            values[name] = float(text)
        else:
            bad.add(name)
    return values, bad


async def _apply(
    request: Request, submit: Callable[[AlertSettingsTarget], Future[AlertSettingsResult]]
) -> AlertSettingsResult | None:
    """Hand a change to the writer and wait for its outcome without blocking the event
    loop; None (logged) when the writer cannot take it, fails it, or does not answer in
    time, in which case a change it has not started is cancelled."""
    writer: AlertSettingsTarget = request.app.state.writer
    timeout_s: float = request.app.state.settings_timeout_s
    try:
        return await asyncio.wait_for(asyncio.wrap_future(submit(writer)), timeout_s)
    except AlertSettingsUnavailableError:
        _log.warning("alert settings not changed: the writer could not apply the change")
    except TimeoutError:
        _log.warning("alert settings not changed: no answer from the writer in time")
    return None


def _now_ms(request: Request) -> int:
    clock: Callable[[], int] = request.app.state.clock
    return clock()


# Routes.

router = APIRouter(include_in_schema=False)


@router.get(SETTINGS_PATH, response_class=HTMLResponse)
async def settings_fragment(request: Request, repository: _Repo, reads: _Reads) -> Response:
    """The settings form, with the values in force."""
    saved = await _saved(request, repository, reads)
    if isinstance(saved, ErrorView):
        return _error(_SERVICE_UNAVAILABLE, saved)
    return _fragment(request, saved)


@router.post(SETTINGS_PATH, response_class=HTMLResponse)
async def save_settings(request: Request, repository: _Repo, reads: _Reads) -> Response:
    """Save the settings the form holds (see the module's docstring)."""
    try:
        form = await _form(request)
    except _FormError as error:
        return _error(error.status_code, error.view)
    values, bad = _parse(form)
    # Bounds hold whatever the other settings are: every submitted field is checked,
    # changed or not, and reported with the parse errors.
    _, out_of_bounds = apply_settings(AlertConfig(), values)
    bad.update(error.field for error in out_of_bounds)
    saved = await _saved(request, repository, reads)
    if isinstance(saved, ErrorView):
        return _error(_SERVICE_UNAVAILABLE, saved)
    if bad:
        errors = tuple(setting_error(name) for name in ALERT_SETTINGS if name in bad)
        return _fragment(request, saved, errors=errors, message=_NOT_SAVED_MESSAGE, submitted=form)
    changes = _changes(request, saved, values)
    if not changes:
        return _fragment(request, saved, message=_NO_CHANGES_MESSAGE)
    now_ms = _now_ms(request)
    result = await _apply(
        request, lambda writer: writer.submit_alert_settings_save(changes, updated_at_ms=now_ms)
    )
    if result is None:
        return _error(_SERVICE_UNAVAILABLE, _NOT_SAVED)
    if result.errors:
        return _fragment(
            request,
            result.saved,
            errors=result.errors,
            message=_NOT_SAVED_MESSAGE,
            submitted=form,
        )
    return _changed(_fragment(request, result.saved, message=_SAVED_MESSAGE))


@router.post(RESET_PATH, response_class=HTMLResponse)
async def reset_settings(request: Request) -> Response:
    """Reset the setting ``field`` names, or every setting without it."""
    try:
        form = await _form(request)
    except _FormError as error:
        return _error(error.status_code, error.view)
    field = form.get(_RESET_FIELD)
    if field is not None and field not in ALERT_SETTINGS:
        return _error(_BAD_REQUEST, _UNKNOWN_SETTING)
    result = await _apply(request, lambda writer: writer.submit_alert_settings_reset(field))
    if result is None:
        return _error(_SERVICE_UNAVAILABLE, _NOT_SAVED)
    return _changed(_fragment(request, result.saved, message=_RESET_MESSAGE))
