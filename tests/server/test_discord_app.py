"""The Discord sender in the app (ADR 0046): its lifespan, the settings screen's "Send
test message" button behind the state-changing routes' guard (ADR 0045), and the canary
webhook URL kept out of every log, page, API answer, metric and database row.

The app runs with a canary webhook URL and an ``httpx.MockTransport`` for Discord; the
tests drive the sender's rounds from the test thread (its loop is off with
``sweeps=False``) against the app's real writer thread.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import SecretStr
from sqlalchemy import insert

from ccobs.domain.alerts import DAY_SUBJECT, AlertFired, AlertRule
from ccobs.domain.app_metrics import ALERT_DELIVERIES
from ccobs.domain.doctor import HttpAnswer, check_alerts
from ccobs.server import app as server_app
from ccobs.server.alert_settings import DISCORD_TEST_PATH
from ccobs.server.config import Settings
from ccobs.server.costs import CostReader
from ccobs.server.db.models import SessionModel
from ccobs.server.db.repository import Repository
from ccobs.server.discord import DiscordSender
from ccobs.server.metrics import AppMetrics

create_app: Final = functools.partial(
    server_app.create_app, serve_metrics=False, sweeps=False, reconcile=False
)
GOOD_HOST: Final = "127.0.0.1:4300"
ORIGIN: Final = f"http://{GOOD_HOST}"
HOST: Final = "test-host"
NOW: Final = 1_800_000_000_000
SECOND: Final = 1000
SESSION: Final = "5e551011-aaaa-4bbb-8ccc-000000000001"
CWD: Final = "/home/canaryuser/work/project"
CANARY_ID: Final = "123456789012345678"
CANARY_TAIL: Final = "CANARY-not-a-real-token"
CANARY_URL: Final = f"https://discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}"
HTMX: Final = {
    "origin": ORIGIN,
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "hx-request": "true",
    "content-type": "application/x-www-form-urlencoded",
}
INLINE_CODE: Final = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>|<style\b|\sstyle\s*=|\son[a-z]+\s*=|\shx-on",
    re.IGNORECASE,
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


class Discord:
    """A MockTransport for Discord: answers from a script (the last item repeats; an
    exception is raised), records the requests and whether it was closed."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script: list[httpx.Response | Exception] = list(script) or [httpx.Response(204)]
        self.requests: list[httpx.Request] = []
        self.closed = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def transport(self) -> httpx.MockTransport:
        discord = self

        class Transport(httpx.MockTransport):
            def close(self) -> None:
                discord.closed = True

        return Transport(self.handle)


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    clean_env(monkeypatch)
    return Settings(
        data_dir=tmp_path / "data",
        host_label=HOST,
        discord_webhook_url=SecretStr(CANARY_URL),
        discord_include_project=True,
    )


@pytest.fixture
def unconfigured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    clean_env(monkeypatch)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


def start(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"http://{GOOD_HOST}")


@pytest.fixture
def discord() -> Discord:
    return Discord()


@pytest.fixture
def client(settings: Settings, discord: Discord) -> Iterator[TestClient]:
    app = create_app(settings, clock=Clock(), discord_transport=discord.transport())
    with start(app) as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def sender_of(client: TestClient) -> DiscordSender:
    sender = app_of(client).state.discord
    assert isinstance(sender, DiscordSender)
    return sender


def repository(client: TestClient) -> Repository:
    found = app_of(client).state.repository
    assert isinstance(found, Repository)
    return found


def add_session(client: TestClient) -> None:
    row = {
        "session_id": SESSION,
        "host": HOST,
        "status": "idle",
        "started_at_ms": NOW - 60 * SECOND,
        "last_event_at_ms": NOW - SECOND,
        "last_seq": 1,
        "turn_open": False,
        "compacting": False,
        "cwd": CWD,
    }
    with repository(client).engine.begin() as connection:
        connection.execute(insert(SessionModel), [row])


def add_pending(client: TestClient, at: int) -> None:
    rules: tuple[tuple[str, AlertRule], ...] = (
        (SESSION, "waiting_user"),
        (DAY_SUBJECT, "daily_cost"),
    )
    for subject, rule in rules:
        fired = AlertFired(rule=rule, subject=subject, fired_at_ms=at, value=60.0, threshold=50.0)
        repository(client).insert_alert(HOST, fired, discord_state="pending", discord_next_at_ms=at)


def settle(client: TestClient) -> None:
    """Wait until the writer has taken everything queued before now: the outcomes of
    the last round come first in its queue."""
    writer = app_of(client).state.writer
    writer.submit_discord_round(NOW, expire_before_ms=0, limit=0).result(timeout=5.0)


def click(client: TestClient, headers: dict[str, str] = HTMX) -> httpx2.Response:
    return client.post(DISCORD_TEST_PATH, content=b"", headers=headers)


def status_line(response: httpx2.Response) -> str:
    found = re.search(r'<p class="discord-test__status" role="status">([^<]*)</p>', response.text)
    assert found, response.text
    return found[1]


def assert_no_canary(text: str) -> None:
    for forbidden in (CANARY_ID, CANARY_TAIL, "/webhooks/", "CANARY"):
        assert forbidden not in text, forbidden


# The button.


def test_the_settings_show_the_button_only_when_discord_is_configured(
    client: TestClient, unconfigured: Settings
) -> None:
    body = client.get("/alerts").text

    assert body.count('id="discord-test"') == 1
    assert f'hx-post="{DISCORD_TEST_PATH}"' in body
    assert ">Send test message</button>" in body
    assert "discord-test__status" not in body
    assert not INLINE_CODE.search(body)
    assert_no_canary(body)
    with start(create_app(unconfigured)) as other:
        assert "discord-test" not in other.get("/alerts").text


def test_a_click_sends_the_test_message_and_says_so(client: TestClient, discord: Discord) -> None:
    response = click(client)

    assert response.status_code == 200
    assert status_line(response) == "Test message sent."
    assert 'id="discord-test"' in response.text
    assert f'hx-post="{DISCORD_TEST_PATH}"' in response.text
    assert not INLINE_CODE.search(response.text)
    (request,) = discord.requests
    assert b"cc-observatory test message from host test-host." in request.content
    # Not an alert: nothing stored.
    assert repository(client).list_alerts(HOST, 10) == []


def test_a_second_click_within_ten_seconds_sends_nothing(
    client: TestClient, discord: Discord
) -> None:
    assert status_line(click(client)) == "Test message sent."

    response = click(client)

    assert response.status_code == 200
    assert status_line(response) == "Wait a few seconds before sending another test."
    assert len(discord.requests) == 1


@pytest.mark.parametrize(
    ("answer", "line"),
    [
        (
            httpx.Response(404, json={"message": "Unknown Webhook"}),
            "Discord did not accept the test message (status 404)."
            " Check CCOBS_DISCORD_WEBHOOK_URL.",
        ),
        (httpx.ReadTimeout(CANARY_URL), "Could not reach Discord (timeout)."),
        (httpx.ConnectError(CANARY_URL), "Could not reach Discord (connection error)."),
    ],
    ids=["refused", "timeout", "no-connection"],
)
def test_a_failed_test_says_why_without_the_url(
    settings: Settings, answer: httpx.Response | Exception, line: str
) -> None:
    discord = Discord(answer)
    with start(create_app(settings, discord_transport=discord.transport())) as client:
        response = click(client)
        assert sender_of(client).suspended_status is None

    assert response.status_code == 200
    assert status_line(response) == line
    assert_no_canary(response.text)


def test_without_discord_the_route_sends_nothing_and_says_so(unconfigured: Settings) -> None:
    with start(create_app(unconfigured)) as client:
        response = click(client)

    assert response.status_code == 200
    assert status_line(response) == (
        "Discord is off. Set CCOBS_DISCORD_WEBHOOK_URL and restart the app to send a test."
    )


@pytest.mark.parametrize(
    "headers",
    [
        {**HTMX, "sec-fetch-site": "cross-site", "origin": "http://evil.example"},
        {**HTMX, "sec-fetch-site": "same-site", "origin": "http://localhost:4330"},
        {**HTMX, "origin": "null"},
        {key: value for key, value in HTMX.items() if key != "origin"},
        {key: value for key, value in HTMX.items() if key != "hx-request"},
        {**HTMX, "content-type": "text/plain"},
    ],
    ids=["cross-site", "same-site", "origin-null", "no-origin", "no-hx-request", "text-plain"],
)
def test_a_click_that_is_not_a_same_origin_htmx_post_is_refused(
    client: TestClient, discord: Discord, headers: dict[str, str]
) -> None:
    response = click(client, headers)

    assert response.status_code in (403, 415)
    assert discord.requests == []


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_other_methods_get_405(client: TestClient, discord: Discord, method: str) -> None:
    assert client.request(method, DISCORD_TEST_PATH, headers=HTMX).status_code == 405
    assert discord.requests == []


# The lifespan.


def test_the_sender_runs_only_when_discord_is_configured(
    settings: Settings, unconfigured: Settings
) -> None:
    discord = Discord()
    app = server_app.create_app(
        settings, serve_metrics=False, reconcile=False, discord_transport=discord.transport()
    )
    with start(app) as client:
        sender = sender_of(client)
        assert sender.is_alive()
        assert sender.poll_interval_s == 5.0
        assert sender.host == HOST
        assert sender.app_url == "http://localhost:4300"
        assert sender.include_project is True
    assert app.state.discord is None
    assert not sender.is_alive()
    assert discord.closed

    app = server_app.create_app(unconfigured, serve_metrics=False, reconcile=False)
    with start(app) as client:
        assert app_of(client).state.discord is None


def test_create_app_without_sweeps_leaves_the_loop_off_but_the_button_on(
    settings: Settings,
) -> None:
    discord = Discord()
    with start(create_app(settings, discord_transport=discord.transport())) as client:
        sender = sender_of(client)
        assert not sender.is_alive()
        assert status_line(click(client)) == "Test message sent."
    assert discord.closed


def test_the_lifespan_starts_the_sender_after_the_cost_reader_and_stops_it_first(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def record[T](
        owner: type[T], name: str, begin: Callable[[T], None], end: Callable[[T, float], None]
    ) -> None:
        def started(self: T) -> None:
            order.append(f"{name} start")
            begin(self)

        def stopped(self: T, timeout_s: float) -> None:
            order.append(f"{name} stop")
            end(self, timeout_s)

        monkeypatch.setattr(owner, "start", started)
        monkeypatch.setattr(owner, "stop", stopped)

    record(CostReader, "costs", CostReader.start, CostReader.stop)
    record(DiscordSender, "discord", DiscordSender.start, DiscordSender.stop)
    app = server_app.create_app(
        settings, serve_metrics=False, reconcile=False, discord_transport=Discord().transport()
    )

    with start(app):
        assert order == ["costs start", "discord start"]

    assert order[2:] == ["discord stop", "costs stop"]


# The canary, end to end.


def test_rounds_through_the_app_record_and_count_each_delivery(
    client: TestClient, discord: Discord
) -> None:
    add_session(client)
    add_pending(client, NOW)

    assert sender_of(client).send_once() == "sent"
    settle(client)

    states = {
        (a.rule, a.discord_state, a.discord_attempts)
        for a in repository(client).list_alerts(HOST, 10)
    }
    assert states == {("waiting_user", "sent", 1), ("daily_cost", "sent", 1)}
    (request,) = discord.requests
    assert b"project project" in request.content
    assert CWD.encode() not in request.content
    metrics = app_of(client).state.metrics
    assert isinstance(metrics, AppMetrics)
    text = generate_latest(metrics.registry).decode()
    assert f'{ALERT_DELIVERIES.name}{{host="{HOST}",result="sent"}} 2.0' in text


def test_the_canary_never_leaves_the_sender(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "ccobs", "uvicorn", "fastapi"):
        caplog.set_level(logging.DEBUG, logger=name)
    script: list[httpx.Response | Exception] = [
        httpx.Response(204),
        # 1 ms on the monotonic clock, waited out below: the pause does not follow the
        # fake wall clock.
        httpx.Response(429, json={"retry_after": 0.001}),
        httpx.Response(500, text=CANARY_URL),
        httpx.ReadTimeout(CANARY_URL),
        httpx.ConnectError(CANARY_URL),
        httpx.RemoteProtocolError(CANARY_URL),
        httpx.Response(400, text=CANARY_URL),
        httpx.Response(302, headers={"location": CANARY_URL}),
        httpx.Response(404, json={"message": "Unknown Webhook"}),
    ]
    discord = Discord(*script)
    clock = Clock()
    loki = httpx.MockTransport(lambda request: httpx.Response(503))
    app = create_app(
        settings,
        clock=clock,
        discord_transport=discord.transport(),
        diagnostics_transport=loki,
    )
    pages: list[str] = []
    with start(app) as client:
        add_session(client)
        sender = sender_of(client)
        results = []
        for _ in script:
            add_pending(client, clock.now)
            results.append(sender.send_once())
            clock.now += 60 * SECOND
            if results[-1] == "rate_limited":
                # Past the clock's tick (about 16 ms on Windows).
                threading.Event().wait(0.05)
        pages.append(click(client).text)
        pages.append(click(client).text)
        for url in ("/alerts", "/ui/alerts/settings", "/api/v1/alerts", "/api/v1/diagnostics"):
            response = client.get(url)
            assert response.status_code == 200, url
            pages.append(response.text)
        metrics = app_of(client).state.metrics
        assert isinstance(metrics, AppMetrics)
        pages.append(generate_latest(metrics.registry).decode())
        assert sender.suspended_status == 404
        # What ccobs doctor makes of the app's answer after the suspension (ADR 0046).
        alerts_check = check_alerts(HttpAnswer(200, client.get("/api/v1/diagnostics").content))
        pages.append(f"{alerts_check.title}\n{alerts_check.detail}\n{alerts_check.fix}")
        database = settings.database_path
    with sqlite3.connect(database) as connection:
        pages.append("\n".join(connection.iterdump()))

    assert results == [
        "sent",
        "rate_limited",
        "retried",
        "retried",
        "retried",
        "retried",
        "failed",
        "failed",
        "suspended",
    ]
    # The test button after the suspension: refused by its status, then too soon.
    assert "(status 404)" in pages[0]
    assert "Wait a few seconds" in pages[1]
    # httpx logged the requests; every record of them was dropped.
    assert caplog.records
    assert_no_canary(caplog.text)
    for page in pages:
        assert_no_canary(page)
    assert "Discord refused the webhook (status 404)" in pages[-2]
    assert "/home/canaryuser" not in pages[2]
