"""Discord messages for alerts (ADR 0046): the JSON body of a webhook execution.

One line per alert, in plain words: what happened, in the alerts page's words
(``presentation.describe_alert``; only fired alerts are sent, so never the cleared
wording), the host label, the short session ID (8 characters) and a local link to the
session page, ``<app_url>/sessions/<id>``, or to the alerts page for a daily alert, which
has no session. The link is in angle brackets, so Discord shows no preview of it. The
project name (``presentation.project_name``) follows the session only when
``include_project`` is set (``CCOBS_DISCORD_INCLUDE_PROJECT``, decision P6), on one
line and at most ``PROJECT_MAX`` characters. A message never holds the ``cwd``, a path,
content or error text: an alert row holds none (ADR 0042).

Text that does not come from the app (the project name, the host label, the session ID)
is cleaned first: every character of Unicode category Cc (controls, line breaks), Cf
(format: bidi overrides such as U+202E, zero-width characters such as U+200B), Zl or Zp
(U+2028, U+2029), and any other whitespace but the space, becomes a space, so a value
can neither break its line nor reorder or hide text. It is then escaped for Discord's
markdown: a backslash before each of ``\\ * _ ~ ` | > [ ] ( )``,
``<`` (mention and timestamp syntax) and ``@`` (``@everyone``, ``@here``). Escaping only
changes what Discord shows; ``allowed_mentions`` with an empty ``parse`` list is what
stops every ping, whatever the content says.

Up to ``ALERTS_PER_MESSAGE`` (10) alerts go in one message. Discord refuses a ``content``
over ``CONTENT_MAX`` (2000) characters, so a longer one keeps as many whole lines as
fit and ends with ``…and N more``.

Pure: no I/O. The Discord sender (``ccobs.server.discord``) posts what this builds.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, TypedDict
from urllib.parse import quote

from ccobs.domain.alerts import AlertRule
from ccobs.domain.presentation import describe_alert, project_name

__all__ = [
    "ALERTS_PER_MESSAGE",
    "CONTENT_MAX",
    "PROJECT_MAX",
    "USERNAME",
    "AllowedMentions",
    "DiscordAlert",
    "Payload",
    "alert_line",
    "alerts_payload",
    "connection_test_payload",
    "escape_markdown",
]

# The name the messages show as their author, instead of the webhook's own.
USERNAME: Final = "cc-observatory"
# Discord's limit on a message's content, in characters.
CONTENT_MAX: Final = 2000
ALERTS_PER_MESSAGE: Final = 10
PROJECT_MAX: Final = 100
_SHORT_ID: Final = 8
_MARKDOWN: Final = re.compile(r"([\\*_~`|>\[\]()<@])")
# Characters that break a line, or reorder or hide text, become a space: these Unicode
# categories, and any whitespace but the space itself.
_INVISIBLE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
_ELLIPSIS: Final = "…"


class AllowedMentions(TypedDict):
    parse: list[str]


class Payload(TypedDict):
    """The body of Discord's Execute Webhook request."""

    username: str
    content: str
    allowed_mentions: AllowedMentions


@dataclass(frozen=True, kw_only=True)
class DiscordAlert:
    """What a message says of one alert: its rule, value and threshold in the rule's
    unit, its session (``None`` for ``daily_cost``) and that session's ``cwd``, which
    only ever becomes a project name."""

    rule: AlertRule
    value: float
    threshold: float
    session_id: str | None
    cwd: str | None


def escape_markdown(text: str) -> str:
    """``text`` with a backslash before each character Discord's markdown or mentions
    would read."""
    return _MARKDOWN.sub(r"\\\1", text)


def _visible(char: str) -> str:
    if char == " ":
        return char
    if char.isspace() or unicodedata.category(char) in _INVISIBLE_CATEGORIES:
        return " "
    return char


def _clean(text: str) -> str:
    """``text`` on one line, with nothing that reorders or hides what it shows."""
    return "".join(_visible(char) for char in text)


def _project(cwd: str | None) -> str | None:
    name = project_name(cwd)
    if name is None:
        return None
    name = _clean(name)
    if len(name) > PROJECT_MAX:
        name = name[: PROJECT_MAX - 1] + _ELLIPSIS
    return escape_markdown(name)


def alert_line(alert: DiscordAlert, *, host: str, app_url: str, include_project: bool) -> str:
    """One alert as a line of the message (see the module's docstring)."""
    what = describe_alert(alert.rule, alert.value, alert.threshold, cleared=False)
    where = f"Host {escape_markdown(_clean(host))}"
    session_id = alert.session_id
    if session_id is None:
        return f"{what}. {where}: <{app_url}/alerts>"
    where += f", session {escape_markdown(_clean(session_id[:_SHORT_ID]))}"
    project = _project(alert.cwd) if include_project else None
    if project is not None:
        where += f", project {project}"
    return f"{what}. {where}: <{app_url}/sessions/{quote(session_id, safe='')}>"


def _payload(content: str) -> Payload:
    return Payload(username=USERNAME, content=content, allowed_mentions=AllowedMentions(parse=[]))


def _fit(lines: Sequence[str]) -> str:
    """The lines joined, or as many whole ones as fit in ``CONTENT_MAX`` with a count of
    the rest."""
    whole = "\n".join(lines)
    if len(whole) <= CONTENT_MAX:
        return whole
    for kept in range(len(lines) - 1, 0, -1):
        cut = "\n".join(lines[:kept]) + f"\n{_ELLIPSIS}and {len(lines) - kept} more"
        if len(cut) <= CONTENT_MAX:
            return cut
    return f"{_ELLIPSIS}and {len(lines)} more"


def alerts_payload(
    alerts: Sequence[DiscordAlert], *, host: str, app_url: str, include_project: bool
) -> Payload:
    """One message for one to ``ALERTS_PER_MESSAGE`` alerts, a line each, in order."""
    if not alerts:
        raise ValueError("a message needs at least one alert")
    if len(alerts) > ALERTS_PER_MESSAGE:
        raise ValueError(f"a message holds at most {ALERTS_PER_MESSAGE} alerts")
    lines = [
        alert_line(alert, host=host, app_url=app_url, include_project=include_project)
        for alert in alerts
    ]
    return _payload(_fit(lines))


def connection_test_payload(host: str) -> Payload:
    """The fixed message of the settings screen's \"Send test message\" button."""
    return _payload(
        f"cc-observatory test message from host {escape_markdown(_clean(host))}."
        " Alerts will show up here."
    )
