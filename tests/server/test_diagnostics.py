"""The diagnostics route, ``GET /api/v1/diagnostics`` (``ccobs.server.diagnostics``, ADR
0038), and its Loki reader.

``ccobs doctor`` and ``ccobs status`` read it: the session counts by state and the open
blocks from SQLite (through the read gate, ADR 0033), the hook counters that feed the
app's metrics (ADR 0028), and the hook failure counts from Loki (an
``httpx.MockTransport`` here), which degrade to ``available: false`` with a reason while
the route still answers 200. Nothing in the answer names a session, a path or content.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import insert
from sqlalchemy.exc import OperationalError

import ccobs
from ccobs.domain.alerts import (
    DAY_SUBJECT,
    AlertConfig,
    DailyCostRule,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
)
from ccobs.domain.app_metrics import EVENT_LABELS, LOKI_QUERIES, event_label
from ccobs.domain.diagnostics import (
    CANCELLED,
    EXECUTIONS,
    FIELD_LABEL,
    HOOK_FAILURES_WINDOW_S,
    NON_BLOCKING_ERROR,
    HookFailures,
    ReportedAlerts,
    ReportedDiscord,
    hook_failures_query,
    parse_diagnostics,
)
from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server import app as app_module
from ccobs.server import diagnostics
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.models import AlertModel, BlockModel, SessionModel
from ccobs.server.db.repository import AlertCounts, Repository, SessionCounts
from ccobs.server.diagnostics import (
    HTTP_TIMEOUT_S,
    MAX_ANSWER_BYTES,
    QUERY_PATH,
    HookFailuresReader,
    HookFailuresUnavailable,
)
from ccobs.server.discord import DiscordSender
from ccobs.server.ingest import DROP_REASONS, HookQueue
from ccobs.server.reads import ReadGate
from ccobs.server.writer import Writer, WriterObservers

HOST: Final = "test-host"
LOKI: Final = "http://loki:3100"
NOW: Final = 1_800_000_000_000
HOUR_MS: Final = 3_600_000
GOOD_HOST: Final = "127.0.0.1:4300"
URL: Final = "/api/v1/diagnostics"
JSON: Final = {"content-type": "application/json"}
STATUSES: Final = ("working", "waiting_user", "idle", "ended", "stale")
CANARY: Final = "CANARY-9f2c41"
HOOK_FIXTURE: Final = (
    Path(__file__).parents[1] / "fixtures" / "hooks" / "headless-tools-subagent.jsonl"
)
GOOD_ANSWER: Final = {EXECUTIONS: "12", NON_BLOCKING_ERROR: "5", CANCELLED: "4"}


def vector(values: dict[str, str]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {FIELD_LABEL: name}, "value": [1_800_000_000.0, value]}
                for name, value in values.items()
            ],
        },
    }


class Loki:
    """A fake Loki behind an ``httpx.MockTransport``: ``respond`` builds each answer."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda _request: httpx.Response(
            200, json=vector(GOOD_ANSWER)
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def loki() -> Loki:
    return Loki()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST, loki_url=LOKI)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings: Settings, clock: Clock, loki: Loki) -> Iterator[TestClient]:
    def manual(
        queue: HookQueue, repository: Repository, observers: WriterObservers
    ) -> ManualWriter:
        return ManualWriter(queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0)

    app = create_app(
        settings,
        clock=clock,
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        diagnostics_transport=httpx.MockTransport(loki.handle),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def ok(client: TestClient) -> dict[str, Any]:
    response = client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    document: dict[str, Any] = response.json()
    return document


def persist(client: TestClient) -> None:
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    while writer.run_once(0.0):
        pass


def post(client: TestClient, body: bytes, headers: dict[str, str] = JSON) -> None:
    response = client.post(HOOK_PATH, content=body, headers=headers)
    assert response.status_code == 204


def session_row(session_id: str, status: str, **values: object) -> dict[str, object]:
    return {
        "session_id": session_id,
        "host": HOST,
        "status": status,
        "started_at_ms": NOW - HOUR_MS,
        "last_event_at_ms": NOW,
        "last_seq": 1,
        "turn_open": False,
        "compacting": False,
        "cwd": f"/home/{CANARY}/project",
        "last_prompt_id": None,
        "last_error_code": None,
        "idle_notified_at_ms": None,
        "ended_at_ms": None,
        "end_reason": None,
        **values,
    }


def block_row(session_id: str, block_id: int, *, closed: bool) -> dict[str, object]:
    return {
        "session_id": session_id,
        "host": HOST,
        "block_id": block_id,
        "kind": "permission",
        "scope_agent_id": None,
        "tool_name": "Bash",
        "tool_use_id": f"toolu_{CANARY}_{block_id}",
        "opened_at_ms": NOW - 1000,
        "closed_at_ms": NOW if closed else None,
        "close_reason": "tool_completed" if closed else None,
        "close_source": "hook" if closed else None,
    }


def seed(client: TestClient, by_state: dict[str, int], open_blocks: int, closed: int) -> None:
    """Sessions with the given counts by state; the blocks go on the first session."""
    sessions = [
        session_row(f"{CANARY}-{status}-{index}", status)
        for status, count in by_state.items()
        for index in range(count)
    ]
    blocks = [
        block_row(str(sessions[0]["session_id"]), n, closed=False) for n in range(open_blocks)
    ]
    blocks += [
        block_row(str(sessions[0]["session_id"]), 100 + n, closed=True) for n in range(closed)
    ]
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    with repository.engine.begin() as connection:
        connection.execute(insert(SessionModel), sessions)
        if blocks:
            connection.execute(insert(BlockModel), blocks)


def loki_count(client: TestClient, result: str) -> float | None:
    registry = app_of(client).state.metrics.registry
    value: float | None = registry.get_sample_value(
        LOKI_QUERIES.name, {"host": HOST, "result": result}
    )
    return value


# The answer's shape.


def test_an_empty_database_answers_every_field_with_zeros(client: TestClient) -> None:
    assert ok(client) == {
        "version": ccobs.__version__,
        "host": HOST,
        "started_at_ms": app_of(client).state.started_at_ms,
        "sessions": {"by_state": dict.fromkeys(STATUSES, 0), "open_blocks": 0},
        "hooks": {
            "received": dict.fromkeys(EVENT_LABELS, 0),
            "dropped": dict.fromkeys(DROP_REASONS, 0),
        },
        "hook_failures": {
            "available": True,
            "window_s": HOOK_FAILURES_WINDOW_S,
            "executions": 12,
            "non_blocking_error": 5,
            "cancelled": 4,
        },
        "alerts": {
            "rules": ["waiting_user", "repeated_failures"],
            "open": 0,
            "fired_24h": 0,
            "discord": {
                "configured": False,
                "suspended_status": None,
                "pending": 0,
                "failed_24h": 0,
                "expired_24h": 0,
            },
        },
    }


def test_started_at_is_when_the_app_started(client: TestClient, clock: Clock) -> None:
    started = ok(client)["started_at_ms"]
    clock.now = NOW + HOUR_MS

    # The wall clock at start-up, not the hook route's clock, and it stays put.
    assert started == app_of(client).state.started_at_ms
    assert started > NOW // 2
    assert ok(client)["started_at_ms"] == started


def test_started_at_is_read_from_the_wall_clock(
    settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "wall_clock_ms", lambda: 1_234)
    app = create_app(settings, clock=clock, serve_metrics=False, sweeps=False, reconcile=False)

    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        assert app_of(test_client).state.started_at_ms == 1_234


def test_sessions_are_counted_by_state_with_open_blocks(client: TestClient) -> None:
    seed(client, {"working": 2, "waiting_user": 1, "ended": 3}, open_blocks=2, closed=5)

    assert ok(client)["sessions"] == {
        "by_state": {"working": 2, "waiting_user": 1, "idle": 0, "ended": 3, "stale": 0},
        "open_blocks": 2,
    }


def test_the_repository_counts_in_one_bounded_read(client: TestClient) -> None:
    seed(client, {"idle": 1, "stale": 2}, open_blocks=1, closed=1)
    reader = app_of(client).state.read_repository
    assert isinstance(reader, Repository)

    assert reader.session_counts() == SessionCounts(by_state={"idle": 1, "stale": 2}, open_blocks=1)


def fixture_payloads() -> list[dict[str, Any]]:
    return [
        json.loads(line)["payload"]
        for line in HOOK_FIXTURE.read_text(encoding="utf-8").splitlines()
    ]


def test_hooks_received_and_dropped_come_from_the_counters(client: TestClient) -> None:
    payloads = fixture_payloads()
    for payload in payloads:
        post(client, json.dumps(payload).encode())
    # ccobs doctor's own probe: an empty object, which the writer drops as invalid.
    post(client, b"{}")
    post(client, b"{}", headers={"content-type": "text/plain"})
    persist(client)
    expected = dict.fromkeys(EVENT_LABELS, 0)
    for payload in payloads:
        expected[event_label(payload["hook_event_name"])] += 1

    hooks = ok(client)["hooks"]

    assert hooks["received"] == expected
    assert sum(hooks["received"].values()) == len(payloads)
    assert hooks["dropped"] == {
        **dict.fromkeys(DROP_REASONS, 0),
        "invalid": 1,
        "bad_content_type": 1,
    }


# Loki.


def test_loki_gets_one_instant_query_of_the_hosts_counts(client: TestClient, loki: Loki) -> None:
    ok(client)

    (request,) = loki.requests
    assert request.method == "GET"
    assert f"{request.url.scheme}://{request.url.host}:{request.url.port}" == LOKI
    assert request.url.path == QUERY_PATH
    assert dict(request.url.params) == {"query": hook_failures_query(HOST)}
    assert loki_count(client, "ok") == 1
    assert loki_count(client, "error") == 0


def raise_(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise error

    return respond


@pytest.mark.parametrize(
    ("respond", "reason"),
    [
        (raise_(httpx.ConnectError("refused")), "unreachable"),
        (raise_(httpx.RemoteProtocolError("broken")), "unreachable"),
        (raise_(httpx.ConnectTimeout("slow")), "timeout"),
        (raise_(httpx.ReadTimeout("slow")), "timeout"),
        (lambda _r: httpx.Response(500, text=f"boom {CANARY}"), "status"),
        (lambda _r: httpx.Response(302, headers={"location": "http://elsewhere/"}), "status"),
        (lambda _r: httpx.Response(200, text=f"garbage {CANARY}"), "shape"),
        (lambda _r: httpx.Response(200, json={"status": "error"}), "shape"),
        (lambda _r: httpx.Response(200, json=vector({EXECUTIONS: "1.5"})), "shape"),
        (lambda _r: httpx.Response(200, content=b"[" * 100_000), "shape"),
        (lambda _r: httpx.Response(200, content=b" " * (MAX_ANSWER_BYTES + 1)), "shape"),
    ],
)
def test_a_failed_loki_query_degrades_to_unavailable_with_a_reason(
    client: TestClient,
    loki: Loki,
    respond: Callable[[httpx.Request], httpx.Response],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed(client, {"working": 1}, open_blocks=1, closed=0)
    loki.respond = respond

    with caplog.at_level(logging.DEBUG):
        document = ok(client)

    assert document["hook_failures"] == {
        "available": False,
        "reason": reason,
        "window_s": HOOK_FAILURES_WINDOW_S,
    }
    # The SQLite part is still there.
    assert document["sessions"]["by_state"]["working"] == 1
    assert document["sessions"]["open_blocks"] == 1
    assert loki_count(client, "error") == 1
    assert loki_count(client, "ok") == 0
    # Logs hold type names and status codes only: no URL, query or body. (httpx logs each
    # request's URL at INFO, below the WARNING the server keeps for libraries.)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("ccobs"))
    assert "loki diagnostics query failed" in ours
    for secret in (CANARY, "query=", LOKI, "hook_execution_complete"):
        assert secret not in ours


# The reader on its own.


def read_once(
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]], **options: Any
) -> HookFailures | HookFailuresUnavailable:
    """One read on a reader of its own, closed afterwards."""

    async def main() -> HookFailures | HookFailuresUnavailable:
        reader = HookFailuresReader(
            loki_url=LOKI, host=HOST, transport=httpx.MockTransport(handler), **options
        )
        try:
            async with asyncio.timeout(5):
                return await reader.read()
        finally:
            await reader.aclose()
            assert reader.closed

    return asyncio.run(main())


def test_the_whole_query_is_bounded_by_the_timeout() -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    results: list[str] = []

    answer = read_once(slow, timeout_s=0.05, on_query=results.append)

    assert answer == HookFailuresUnavailable(reason="timeout")
    assert results == ["error"]


def test_a_failing_counter_does_not_fail_the_read(caplog: pytest.LogCaptureFixture) -> None:
    def count(_result: str) -> None:
        raise RuntimeError(CANARY)

    async def good(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector(GOOD_ANSWER))

    assert read_once(good, on_query=count) == HookFailures(12, 5, 4)
    assert "loki query not counted (RuntimeError)" in caplog.text
    assert CANARY not in caplog.text


def test_without_a_counter_the_read_still_works() -> None:
    async def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert read_once(down) == HookFailuresUnavailable(reason="unreachable")


def test_the_reader_refuses_bounds_that_are_not_positive() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        HookFailuresReader(loki_url=LOKI, host=HOST, timeout_s=0)
    with pytest.raises(ValueError, match="max_answer_bytes"):
        HookFailuresReader(loki_url=LOKI, host=HOST, max_answer_bytes=0)


def test_the_timeout_is_short() -> None:
    assert HTTP_TIMEOUT_S <= 2.0


# The read gate (ADR 0033).


class CountingGate(ReadGate):
    def __init__(self, *, slots: int, wait_timeout_s: float = 5.0) -> None:
        super().__init__(slots=slots, wait_timeout_s=wait_timeout_s)
        self.runs = 0

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        self.runs += 1
        return await super().run(read, *args)


def test_the_counts_take_one_read_slot(client: TestClient) -> None:
    portal = client.portal
    assert portal is not None
    gate = portal.call(lambda: CountingGate(slots=1))
    app_of(client).state.reads = gate

    document = ok(client)

    # The alert counts share the session counts' slot: one read, one budget.
    assert gate.runs == 1
    assert document["alerts"] is not None


def test_a_saturated_read_gate_answers_503_without_asking_loki(
    client: TestClient, loki: Loki
) -> None:
    portal = client.portal
    assert portal is not None
    gate = portal.call(lambda: ReadGate(slots=1, wait_timeout_s=0.05))
    holder = object()
    # Every slot taken by a read that does not end.
    portal.call(gate._slots.acquire_on_behalf_of, holder)
    app_of(client).state.reads = gate

    response = client.get(URL)

    assert response.status_code == 503
    assert response.json() == {"detail": "database busy"}
    assert loki.requests == []
    portal.call(gate._slots.release_on_behalf_of, holder)


# The guards and privacy.


def test_the_route_is_get_only_async_and_out_of_the_schema(client: TestClient) -> None:
    routes = [route for route in diagnostics.router.routes if isinstance(route, APIRoute)]

    assert [(route.path, route.methods) for route in routes] == [(URL, {"GET"})]
    for route in routes:
        # Async def, dependencies included: no sync code on FastAPI's default threadpool.
        assert inspect.iscoroutinefunction(route.endpoint)
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert calls
        assert all(inspect.iscoroutinefunction(call) for call in calls)
    assert client.post(URL).status_code == 405
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example"])
def test_a_foreign_host_is_refused(client: TestClient, loki: Loki, host: str) -> None:
    response = client.get(URL, headers={"host": host})

    assert response.status_code == 400
    assert loki.requests == []


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_cross_site_fetch_is_refused(client: TestClient, loki: Loki, site: str) -> None:
    headers = {"sec-fetch-site": site, "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}

    response = client.get(URL, headers=headers)

    assert response.status_code == 403
    assert loki.requests == []


def test_the_answer_carries_the_security_headers(client: TestClient) -> None:
    response = client.get(URL)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert not [name for name in response.headers if name.lower().startswith("access-control")]


def test_no_session_id_path_or_content_reaches_the_answer(client: TestClient) -> None:
    seed(client, {"working": 1, "idle": 1}, open_blocks=1, closed=1)
    for payload in fixture_payloads():
        planted = {**payload, "session_id": f"{CANARY}-hooked", "cwd": f"/home/{CANARY}/repo"}
        if "prompt" in planted:
            planted["prompt"] = CANARY
        post(client, json.dumps(planted).encode())
    persist(client)

    text = client.get(URL).text

    assert CANARY not in text
    assert "/home/" not in text
    assert "session_id" not in text
    assert "cwd" not in text


# Alerts (ADR 0046): the rules on, the counts, and Discord's delivery, never its URL.

DAY_MS: Final = 24 * HOUR_MS
CANARY_URL: Final = f"https://discord.com/api/webhooks/123456789012345678/{CANARY}"
URL_PARTS: Final = (CANARY, "123456789012345678", "/webhooks/", "discord.com")


def alert_row(
    *,
    fired_at_ms: int,
    cleared: bool = False,
    discord_state: str = "none",
    host: str = HOST,
) -> dict[str, object]:
    """A daily_cost alert: it needs no session row."""
    return {
        "host": host,
        "rule": "daily_cost",
        "subject": DAY_SUBJECT,
        "session_id": None,
        "fired_at_ms": fired_at_ms,
        "cleared_at_ms": fired_at_ms if cleared else None,
        "value": 60.0,
        "threshold": 50.0,
        "discord_state": discord_state,
        "discord_attempts": 0,
        "discord_next_at_ms": fired_at_ms if discord_state == "pending" else None,
    }


def seed_alerts(client: TestClient, rows: list[dict[str, object]]) -> None:
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    with repository.engine.begin() as connection:
        connection.execute(insert(AlertModel), rows)


def test_alerts_are_counted_over_the_last_24_hours(client: TestClient) -> None:
    recent, old = NOW - HOUR_MS, NOW - DAY_MS - 1
    seed_alerts(
        client,
        [
            # Still on: two recent, one older than a day (open counts every age).
            alert_row(fired_at_ms=recent),
            alert_row(fired_at_ms=recent, discord_state="pending"),
            alert_row(fired_at_ms=old, discord_state="pending"),
            # Cleared, fired within the day (its first millisecond included).
            alert_row(fired_at_ms=recent, cleared=True, discord_state="sent"),
            alert_row(fired_at_ms=recent, cleared=True, discord_state="failed"),
            alert_row(fired_at_ms=recent, cleared=True, discord_state="failed"),
            alert_row(fired_at_ms=NOW - DAY_MS, cleared=True, discord_state="expired"),
            # Older than the day: out of every 24 h count.
            alert_row(fired_at_ms=old, cleared=True, discord_state="failed"),
            alert_row(fired_at_ms=old, cleared=True, discord_state="expired"),
            # Another host's alerts are not this app's.
            alert_row(fired_at_ms=recent, discord_state="failed", host="other-host"),
        ],
    )

    alerts = ok(client)["alerts"]

    assert alerts["open"] == 3
    assert alerts["fired_24h"] == 6
    assert alerts["discord"] == {
        "configured": False,
        "suspended_status": None,
        "pending": 2,
        "failed_24h": 2,
        "expired_24h": 1,
    }


def test_the_window_follows_the_app_clock(client: TestClient, clock: Clock) -> None:
    seed_alerts(client, [alert_row(fired_at_ms=NOW, discord_state="failed")])
    assert ok(client)["alerts"]["fired_24h"] == 1

    clock.now = NOW + DAY_MS + 1

    alerts = ok(client)["alerts"]
    assert alerts["fired_24h"] == 0
    assert alerts["discord"]["failed_24h"] == 0
    assert alerts["open"] == 1


def test_the_rules_are_those_in_force(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = app_of(client).state.writer
    some = AlertConfig(
        waiting_user=WaitingUserRule(enabled=False),
        repeated_failures=RepeatedFailuresRule(enabled=True),
        session_cost=SessionCostRule(enabled=False),
        daily_cost=DailyCostRule(enabled=True),
    )
    none = AlertConfig(
        waiting_user=WaitingUserRule(enabled=False),
        repeated_failures=RepeatedFailuresRule(enabled=False),
    )

    monkeypatch.setattr(writer, "alert_config", lambda: some)
    assert ok(client)["alerts"]["rules"] == ["repeated_failures", "daily_cost"]

    monkeypatch.setattr(writer, "alert_config", lambda: none)
    assert ok(client)["alerts"]["rules"] == []


@pytest.fixture
def discord_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock, loki: Loki
) -> Iterator[TestClient]:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    settings = Settings(
        data_dir=tmp_path / "data",
        host_label=HOST,
        loki_url=LOKI,
        discord_webhook_url=SecretStr(CANARY_URL),
    )
    app = create_app(
        settings,
        clock=clock,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        diagnostics_transport=httpx.MockTransport(loki.handle),
        discord_transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def test_a_configured_webhook_says_so_and_never_shows_its_url(
    discord_client: TestClient,
) -> None:
    response = discord_client.get(URL)

    assert response.status_code == 200
    discord = response.json()["alerts"]["discord"]
    assert discord["configured"] is True
    assert discord["suspended_status"] is None
    for forbidden in URL_PARTS:
        assert forbidden not in response.text


def test_a_suspended_sender_reports_its_status_only(discord_client: TestClient) -> None:
    seed_alerts(discord_client, [alert_row(fired_at_ms=NOW, discord_state="pending")])
    sender = app_of(discord_client).state.discord
    assert isinstance(sender, DiscordSender)

    assert sender.send_once() == "suspended"
    # The outcome reaches the database through the writer's queue, in order.
    writer = app_of(discord_client).state.writer
    writer.submit_discord_round(NOW, expire_before_ms=0, limit=0).result(timeout=5.0)
    response = discord_client.get(URL)

    assert response.json()["alerts"]["discord"] == {
        "configured": True,
        "suspended_status": 404,
        "pending": 0,
        "failed_24h": 1,
        "expired_24h": 0,
    }
    for forbidden in URL_PARTS:
        assert forbidden not in response.text


def test_a_failed_alerts_read_answers_null_and_keeps_the_rest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed(client, {"working": 1}, open_blocks=0, closed=0)
    reader = app_of(client).state.read_repository

    def broken(*_args: object, **_kwargs: object) -> object:
        raise OperationalError(f"SELECT {CANARY}", None, RuntimeError(CANARY))

    monkeypatch.setattr(reader, "alert_counts", broken)

    with caplog.at_level(logging.DEBUG):
        document = ok(client)

    assert document["alerts"] is None
    assert document["sessions"]["by_state"]["working"] == 1
    assert document["hook_failures"]["available"] is True
    assert "alerts diagnostics read failed (OperationalError)" in caplog.text
    assert CANARY not in caplog.text


def test_a_failed_session_read_still_fails_the_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = app_of(client).state.read_repository

    def broken() -> object:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(reader, "session_counts", broken)
    failing = TestClient(
        app_of(client), base_url=f"http://{GOOD_HOST}", raise_server_exceptions=False
    )

    # Only the alerts part degrades: the sessions are the route's core (ADR 0038).
    assert failing.get(URL).status_code == 500


def test_the_answer_is_what_the_cli_parses(client: TestClient) -> None:
    seed_alerts(client, [alert_row(fired_at_ms=NOW, discord_state="pending")])

    parsed = parse_diagnostics(client.get(URL).content)

    assert parsed is not None
    assert parsed.alerts == ReportedAlerts(
        rules=("waiting_user", "repeated_failures"),
        open=1,
        fired_24h=1,
        discord=ReportedDiscord(
            configured=False, suspended_status=None, pending=1, failed_24h=0, expired_24h=0
        ),
    )


def test_the_repository_counts_alerts_in_one_aggregate(client: TestClient) -> None:
    seed_alerts(
        client,
        [
            alert_row(fired_at_ms=NOW, discord_state="failed"),
            alert_row(fired_at_ms=NOW - DAY_MS - 1, cleared=True, discord_state="expired"),
        ],
    )
    reader = app_of(client).state.read_repository
    assert isinstance(reader, Repository)

    assert reader.alert_counts(HOST, since_ms=NOW - DAY_MS) == AlertCounts(
        open=1, fired=1, pending=0, failed=1, expired=0
    )
    assert reader.alert_counts("nobody", since_ms=0) == AlertCounts(
        open=0, fired=0, pending=0, failed=0, expired=0
    )
