"""The alerts page (``ccobs.server.alerts_page``, ADR 0044): the alerts the app sent, as
strips, newest first and paged by ``id``, live on the stream's alerts ping, and the alert
settings grouped by rule, with whether Discord gets them too.

Alerts and sessions are written straight to the database. The page reads through the
read gate, shows the project name only (never the ``cwd``, product decision P2), never
any part of the Discord webhook URL, and holds nothing the strict CSP refuses
(ADR 0034).
"""

from __future__ import annotations

import functools
import html
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.exc import OperationalError

from ccobs.domain.alerts import DAY_SUBJECT, AlertCleared, AlertFired, AlertRule
from ccobs.domain.presentation import format_duration
from ccobs.server import alerts_page
from ccobs.server import app as server_app
from ccobs.server.alert_settings import RESET_PATH, SETTINGS_PATH
from ccobs.server.config import Settings
from ccobs.server.db.models import SessionModel
from ccobs.server.db.repository import Repository
from ccobs.server.reads import ReadGate

create_app: Final = functools.partial(
    server_app.create_app, serve_metrics=False, sweeps=False, reconcile=False
)
GOOD_HOST: Final = "127.0.0.1:4300"
HOST: Final = "test-host"
NOW: Final = 1_800_000_000_000
MINUTE_MS: Final = 60_000
USERNAME: Final = "canaryuser"
SESSION: Final = "5e551011-aaaa-4bbb-8ccc-000000000001"
NO_CWD: Final = "5e551011-aaaa-4bbb-8ccc-000000000002"
WEBHOOK: Final = (
    "https://discord.com/api/webhooks/123456789012345678/Tok3nCanary-abcdefghijklmnopqrstuvwxyz"
)
INLINE_CODE: Final = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>|<style\b|\sstyle\s*=|\son[a-z]+\s*=|\shx-on",
    re.IGNORECASE,
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    clean_env(monkeypatch)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, clock=Clock()), base_url=f"http://{GOOD_HOST}") as test:
        yield test


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def repository(client: TestClient) -> Repository:
    found = app_of(client).state.repository
    assert isinstance(found, Repository)
    return found


def add_session(client: TestClient, session_id: str, cwd: str | None) -> None:
    row = {
        "session_id": session_id,
        "host": HOST,
        "status": "idle",
        "started_at_ms": NOW - 60 * MINUTE_MS,
        "last_event_at_ms": NOW - MINUTE_MS,
        "last_seq": 1,
        "turn_open": False,
        "compacting": False,
        "cwd": cwd,
    }
    with repository(client).engine.begin() as connection:
        connection.execute(insert(SessionModel), [row])


def fire(
    client: TestClient,
    rule: AlertRule,
    subject: str,
    at: int,
    value: float,
    threshold: float,
    *,
    host: str = HOST,
) -> int:
    fired = AlertFired(rule=rule, subject=subject, fired_at_ms=at, value=value, threshold=threshold)
    return repository(client).insert_alert(host, fired, discord_state="none")


def clear(client: TestClient, rule: AlertRule, subject: str, at: int) -> None:
    cleared = AlertCleared(rule=rule, subject=subject, cleared_at_ms=at)
    repository(client).clear_alert(HOST, cleared)


def page(client: TestClient, url: str, *, status: int = 200) -> str:
    response = client.get(url)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    return response.text


def items(body: str) -> list[str]:
    """The markup of each alert strip, in page order."""
    return re.findall(r'<li class="strip alert[^"]*".*?</li>', body, re.DOTALL)


def text_of(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def seed_every_rule(client: TestClient) -> None:
    """One alert of each rule, oldest first: a cleared wait, failures and a session cost
    still on, then a daily cost still on."""
    add_session(client, SESSION, f"/home/{USERNAME}/work/billing-api")
    add_session(client, NO_CWD, None)
    fire(client, "waiting_user", SESSION, NOW - 10 * MINUTE_MS, 134.4, 30.0)
    clear(client, "waiting_user", SESSION, NOW - 10 * MINUTE_MS + 95_000)
    fire(client, "repeated_failures", NO_CWD, NOW - 5 * MINUTE_MS, 3.0, 3.0)
    fire(client, "session_cost", SESSION, NOW - 2 * MINUTE_MS, 12.4, 10.0)
    fire(client, "daily_cost", DAY_SUBJECT, NOW - MINUTE_MS, 52.1, 50.0)


# The page and its place in the masthead.


def test_the_masthead_links_the_alerts_page_between_sessions_and_grafana(
    client: TestClient,
) -> None:
    for url, current in (("/", None), ("/sessions", None), ("/alerts", "/alerts")):
        body = page(client, url)
        nav = body.split('<nav class="masthead__nav"', 1)[1].split("</nav>", 1)[0]
        links = re.findall(r"<a [^>]*>([^<]+)</a>", nav)
        assert links == ["Overview", "All sessions", "Alerts", "Grafana"]
        assert ('<a href="/alerts" aria-current="page">Alerts</a>' in nav) == (current is not None)
        assert ('<a href="/alerts">Alerts</a>' in nav) == (current is None)


def test_the_page_has_a_title_a_lede_and_two_sections_history_first(client: TestClient) -> None:
    body = page(client, "/alerts")

    assert "<title>Alerts - cc-observatory</title>" in body
    assert '<h1 class="page-title">Alerts</h1>' in body
    assert (
        '<p class="page-lede">What the app warns you about, and the warnings it sent.</p>' in body
    )
    headings = re.findall(r'<h2 class="alerts-section__title"[^>]*>([^<]+)</h2>', body)
    assert headings == ["Recent alerts", "Settings"]
    # The settings form of stage 9, grouped by rule.
    assert 'id="alert-settings"' in body
    assert f'hx-post="{SETTINGS_PATH}"' in body
    assert f'hx-post="{RESET_PATH}"' in body
    assert body.index('id="alert-history"') < body.index('id="alert-settings"')
    # The slot for the browser notifications control (stage 11): empty, no placeholder.
    assert '<div id="alert-notifications" class="alert-notifications"></div>' in body
    # The page streams unfiltered, for the alerts ping (ADR 0044).
    assert 'data-stream="/ui/stream"' in body
    assert not INLINE_CODE.search(body)


# History.


def test_each_alert_says_what_happened_by_rule_newest_first(client: TestClient) -> None:
    seed_every_rule(client)

    rows = [text_of(row) for row in items(page(client, "/alerts"))]

    assert len(rows) == 4
    assert "The last 24 hours cost $52.10 (limit $50)" in rows[0]
    assert "This session cost $12.40 (limit $10)" in rows[1]
    assert "API requests failed 3 times" in rows[2]
    assert "Waited on you for 2m 14s" in rows[3]
    # When each fired, in the app's duration format, ticked by app.js.
    assert f"Fired {format_duration(MINUTE_MS)} ago" in rows[0]
    assert f"Fired {format_duration(10 * MINUTE_MS)} ago" in rows[3]


def test_an_alert_still_on_is_amber_and_a_cleared_one_says_after_how_long(
    client: TestClient,
) -> None:
    seed_every_rule(client)

    rows = items(page(client, "/alerts"))

    assert rows[1].startswith('<li class="strip alert alert--on">')
    assert "Still on" in text_of(rows[1])
    assert rows[3].startswith('<li class="strip alert alert--cleared">')
    assert f"Cleared after {format_duration(95_000)}" in text_of(rows[3])
    assert "Still on" not in text_of(rows[3])
    fired = NOW - 10 * MINUTE_MS
    assert f'data-since-ms="{fired}"' in rows[3]
    assert 'datetime="2027-01-15T07:50:00Z"' in rows[3]


def test_a_wait_still_on_says_it_is_not_over(client: TestClient) -> None:
    add_session(client, SESSION, f"/home/{USERNAME}/work/billing-api")
    fire(client, "waiting_user", SESSION, NOW - MINUTE_MS, 134.4, 30.0)

    (row,) = (text_of(row) for row in items(page(client, "/alerts")))

    assert "Waiting on you for over 2m 14s" in row
    assert "Waited" not in row
    assert "Still on" in row


def test_a_session_alert_names_the_project_and_links_the_session(client: TestClient) -> None:
    seed_every_rule(client)

    body = page(client, "/alerts")
    rows = items(body)

    assert f'<a class="strip__project" href="/sessions/{SESSION}">billing-api</a>' in rows[1]
    assert f'<code class="strip__id">{SESSION[:8]}</code>' in rows[1]
    # A session with no cwd: the placeholder, still linked.
    assert f'href="/sessions/{NO_CWD}"' in rows[2]
    assert "Unknown project" in rows[2]
    # The daily alert is about every session: no link, no ID.
    assert "/sessions/" not in rows[0]
    assert "All sessions" in text_of(rows[0])
    # Never the cwd or the username.
    assert USERNAME not in body
    assert "/home/" not in body


def test_another_hosts_alerts_are_not_shown(client: TestClient) -> None:
    add_session(client, SESSION, None)
    fire(client, "waiting_user", SESSION, NOW - MINUTE_MS, 40.0, 30.0, host="other-host")

    body = page(client, "/alerts")

    assert items(body) == []


def test_the_history_pages_with_older_alerts(client: TestClient) -> None:
    add_session(client, SESSION, None)
    total = alerts_page.ALERTS_PAGE_SIZE + 3
    ids = [
        fire(client, "repeated_failures", SESSION, NOW - total + index, 3.0, 3.0)
        for index in range(total)
    ]

    first = page(client, "/alerts")
    assert len(items(first)) == alerts_page.ALERTS_PAGE_SIZE
    oldest_shown = ids[-alerts_page.ALERTS_PAGE_SIZE]
    pager = re.search(r'<a class="pager__next" href="([^"]+)">Older alerts</a>', first)
    assert pager is not None
    assert html.unescape(pager.group(1)) == f"/alerts?before={oldest_shown}"
    assert "pager__first" not in first

    second = page(client, f"/alerts?before={oldest_shown}")
    assert len(items(second)) == 3
    assert "pager__next" not in second
    assert '<a class="pager__first" href="/alerts">Newest alerts</a>' in second
    # The older page refetches itself at the same cursor.
    assert f'hx-get="{alerts_page.HISTORY_PATH}?before={oldest_shown}"' in second

    past = page(client, f"/alerts?before={ids[0]}")
    assert items(past) == []
    assert "No older alerts." in past
    assert "No alerts yet" not in past


@pytest.mark.parametrize(
    "before", ["x", "-1", "1.5", "", str(2**63), "99999999999999999999", "1 2"]
)
def test_an_invalid_cursor_is_a_400_without_echo(client: TestClient, before: str) -> None:
    for url in ("/alerts", alerts_page.HISTORY_PATH):
        response = client.get(url, params={"before": before})

        assert response.status_code == 400
        assert "This page link is not valid" in response.text
        if len(before) > 2:
            assert before not in response.text
    body = client.get("/alerts", params={"before": "x"}).text
    assert '<a href="/alerts">Newest alerts</a>' in body
    assert 'id="alert-settings"' not in body


# Live: the history refetches itself on the alerts ping.


def test_the_history_refetches_itself_on_the_alerts_ping(client: TestClient) -> None:
    seed_every_rule(client)

    body = page(client, "/alerts")
    fragment = page(client, alerts_page.HISTORY_PATH)

    element = (
        f'<div id="alert-history" class="alert-history" hx-get="{alerts_page.HISTORY_PATH}"'
        ' hx-trigger="ccobs:alerts from:body" hx-swap="outerHTML" hx-sync="this:replace">'
    )
    assert element in body
    # The fragment is the same element, alone, with the same rows.
    assert "<html" not in fragment
    assert element in fragment
    assert [text_of(row) for row in items(fragment)] == [text_of(row) for row in items(body)]
    assert not INLINE_CODE.search(fragment)


# The empty history: an invitation with the values in force.


def test_the_empty_history_invites_with_the_values_in_force(client: TestClient) -> None:
    repository(client).save_alert_setting(HOST, "alert_waiting_threshold_s", 90, updated_at_ms=1)

    body = page(client, "/alerts")

    assert items(body) == []
    assert (
        "No alerts yet. The app warns you here when a session waits on you longer than"
        " 1m 30s, or when API requests fail 3 times within 10m 00s."
    ) in text_of(body)
    assert "cost more" not in body.split('id="alert-settings"', 1)[0]
    assert "pager__" not in body


def test_the_empty_history_names_the_cost_rules_when_they_are_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("CCOBS_ALERT_DAILY_COST_ENABLED", "true")
    settings = Settings(data_dir=tmp_path / "data", host_label=HOST)
    with TestClient(create_app(settings), base_url=f"http://{GOOD_HOST}") as test:
        body = page(test, alerts_page.HISTORY_PATH)

    assert "or when the last 24 hours cost more than $50." in text_of(body)
    assert "a session costs more" not in body


# Discord: configured or not, never the URL.


def discord_line(body: str) -> str:
    found = re.search(r'<p class="alerts-discord">(.*?)</p>', body, re.DOTALL)
    assert found is not None
    return text_of(found.group(1))


def test_without_discord_the_page_says_how_to_turn_it_on(client: TestClient) -> None:
    line = discord_line(page(client, "/alerts"))

    assert line == (
        "Discord is off. Set CCOBS_DISCORD_WEBHOOK_URL in the stack's .env and restart"
        " the app to send alerts there too."
    )


@pytest.mark.parametrize(
    ("include_project", "expected"),
    [
        ("false", "Alerts also go to your Discord webhook."),
        ("true", "Alerts also go to your Discord webhook. Messages name the project."),
    ],
)
def test_with_discord_the_page_says_so_and_never_shows_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_project: str, expected: str
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("CCOBS_DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("CCOBS_DISCORD_INCLUDE_PROJECT", include_project)
    settings = Settings(data_dir=tmp_path / "data", host_label=HOST)
    with TestClient(create_app(settings), base_url=f"http://{GOOD_HOST}") as test:
        body = page(test, "/alerts")

    assert discord_line(body) == expected
    for fragment in ("discord.com", "api/webhooks", "123456789012345678", "Tok3nCanary"):
        assert fragment not in body


# Reads: one call on the read gate per request, and its failures.


class CountingGate(ReadGate):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        self.calls += 1
        return await super().run(read, *args)


class FailingGate(ReadGate):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        raise self.error


BUSY: Final = HTTPException(status_code=503, detail="database busy")
BROKEN: Final = OperationalError("SELECT secret", {}, Exception("disk I/O error"))


@pytest.mark.parametrize("url", ["/alerts", alerts_page.HISTORY_PATH])
def test_each_request_reads_once_through_the_read_gate(client: TestClient, url: str) -> None:
    seed_every_rule(client)
    gate = CountingGate()
    app_of(client).state.reads = gate

    page(client, url)

    assert gate.calls == 1


@pytest.mark.parametrize(
    ("error", "message"),
    [(BUSY, "The database is busy"), (BROKEN, "The app could not read its database")],
    ids=["busy", "broken"],
)
@pytest.mark.parametrize("url", ["/alerts", alerts_page.HISTORY_PATH])
def test_a_failed_read_says_what_failed_with_503(
    client: TestClient, error: Exception, message: str, url: str
) -> None:
    app_of(client).state.reads = FailingGate(error)

    body = page(client, url, status=503)

    assert message in body
    assert "secret" not in body
    assert 'class="error-state"' in body
    assert ("<html" in body) == (url == "/alerts")
    assert not INLINE_CODE.search(body)
