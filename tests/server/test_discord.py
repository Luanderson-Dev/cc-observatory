"""The Discord sender (``ccobs.server.discord``, ADR 0046).

The unit tests drive ``DiscordSender.send_once`` and ``run`` on the test thread with a
fake writer (rounds answered from a script), a fake clock and wait, and an
``httpx.MockTransport`` that stands in for Discord. Each status path is covered: 2xx,
429 (body, header, both, cap), 5xx and transport errors with back-off and the attempts
limit, 401/403/404 with the suspension, other statuses, and the expiry. The canary tests
run every path with every logger at DEBUG and check that no part of the webhook URL
reaches a log line.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import Future
from typing import Final

import httpx
import pytest
from pydantic import SecretStr

from ccobs.domain.alerts import AlertRule
from ccobs.domain.app_metrics import DeliveryResult
from ccobs.domain.discord_message import DiscordAlert, alerts_payload, connection_test_payload
from ccobs.server.db.repository import AlertRecord, AlertView, DeliveryUpdate, DueDeliveries
from ccobs.server.discord import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    EXPIRE_AFTER_MS,
    HTTP_TIMEOUT_S,
    MAX_ATTEMPTS,
    POLL_INTERVAL_S,
    REFUSED_STATUSES,
    RETRY_AFTER_MAX_S,
    ROUND_TIMEOUT_S,
    TEST_INTERVAL_S,
    ConnectionTestResult,
    DiscordSender,
    _backoff_s,
    connection_test_status,
    hide_webhook_urls,
)
from ccobs.server.writer import DiscordUnavailableError

HOST: Final = "test-host"
APP_URL: Final = "http://localhost:4300"
NOW_MS: Final = 1_800_000_000_000
SECOND: Final = 1000
SESSION: Final = "00000000-0000-4000-8000-0000000000a1"
CWD: Final = "/home/someone/work/project"
CANARY_ID: Final = "123456789012345678"
CANARY_TAIL: Final = "CANARY-not-a-real-token"
CANARY_URL: Final = f"https://discord.com/api/webhooks/{CANARY_ID}/{CANARY_TAIL}"


def view(
    alert_id: int = 1,
    *,
    attempts: int = 0,
    rule: AlertRule = "waiting_user",
    session_id: str | None = SESSION,
    cwd: str | None = CWD,
) -> AlertView:
    record = AlertRecord(
        id=alert_id,
        rule=rule,
        subject=session_id or "day",
        session_id=session_id,
        fired_at_ms=NOW_MS - 10 * SECOND,
        cleared_at_ms=None,
        value=45.0,
        threshold=30.0,
        discord_state="pending",
        discord_attempts=attempts,
        discord_next_at_ms=NOW_MS - 10 * SECOND,
    )
    return AlertView(alert=record, cwd=cwd)


class Outbox:
    """The writer as the sender sees it: each round answered from a script (the last
    one repeats), and the results submitted."""

    def __init__(self, *rounds: DueDeliveries | Exception | None) -> None:
        self.script: list[DueDeliveries | Exception | None] = list(rounds) or [
            DueDeliveries(expired=0, alerts=[])
        ]
        self.rounds: list[tuple[int, int, int]] = []
        self.futures: list[Future[DueDeliveries]] = []
        self.results: list[list[DeliveryUpdate]] = []

    def submit_discord_round(
        self, now_ms: int, *, expire_before_ms: int, limit: int
    ) -> Future[DueDeliveries]:
        self.rounds.append((now_ms, expire_before_ms, limit))
        answer = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        future: Future[DueDeliveries] = Future()
        self.futures.append(future)
        if isinstance(answer, Exception):
            future.set_exception(answer)
        elif answer is not None:  # None: the writer never answers
            future.set_result(answer)
        return future

    def submit_discord_results(self, updates: Sequence[DeliveryUpdate]) -> None:
        self.results.append(list(updates))

    @property
    def limits(self) -> list[int]:
        return [limit for _, _, limit in self.rounds]


def due(*views: AlertView, expired: int = 0) -> DueDeliveries:
    return DueDeliveries(expired=expired, alerts=list(views))


class Discord:
    """A MockTransport that answers from a script (each item a response or an exception
    to raise; the last one repeats) and records every request."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script: list[httpx.Response | Exception] = list(script) or [
            httpx.Response(200, json={"id": "1"})
        ]
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

    def payloads(self) -> list[object]:
        return [json.loads(request.content) for request in self.requests]

    def contents(self) -> list[str]:
        found = []
        for payload in self.payloads():
            assert isinstance(payload, dict)
            content = payload["content"]
            assert isinstance(content, str)
            found.append(content)
        return found


class FakeTime:
    """A wall clock only the fake wait moves; the wait stops after ``rounds``."""

    def __init__(self, rounds: int = 0, now: int = NOW_MS) -> None:
        self.now = now
        self.rounds = rounds
        self.waits: list[float] = []
        self.monotonic_now = 100.0

    def clock(self) -> int:
        return self.now

    def monotonic(self) -> float:
        return self.monotonic_now

    def advance(self, seconds: float) -> None:
        """Both clocks move on together."""
        self.now += round(seconds * SECOND)
        self.monotonic_now += seconds

    def wait(self, timeout_s: float) -> bool:
        if len(self.waits) == self.rounds:
            return True
        self.waits.append(timeout_s)
        self.now += int(timeout_s * SECOND)
        return False


class Deliveries:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, result: DeliveryResult, count: int) -> None:
        self.counts[result] += count


def make(
    outbox: Outbox,
    discord: Discord,
    *,
    fake: FakeTime | None = None,
    deliveries: Deliveries | None = None,
    include_project: bool = False,
    round_timeout_s: float = ROUND_TIMEOUT_S,
) -> DiscordSender:
    fake = fake or FakeTime()
    return DiscordSender(
        outbox,
        webhook_url=SecretStr(CANARY_URL),
        host=HOST,
        app_url=APP_URL,
        include_project=include_project,
        round_timeout_s=round_timeout_s,
        clock=fake.clock,
        monotonic=fake.monotonic,
        wait=fake.wait,
        transport=discord.transport(),
        on_delivery=deliveries,
    )


def updates(outbox: Outbox) -> list[tuple[int, str, int, int | None]]:
    (last,) = outbox.results[-1:]
    return [(item.alert_id, item.state, item.attempts, item.next_at_ms) for item in last]


def assert_no_canary(text: str) -> None:
    for forbidden in (CANARY_ID, CANARY_TAIL, "/webhooks/", "CANARY"):
        assert forbidden not in text, forbidden


def test_the_defaults() -> None:
    assert POLL_INTERVAL_S == 5.0
    assert HTTP_TIMEOUT_S == ROUND_TIMEOUT_S == 5.0
    assert (BACKOFF_INITIAL_S, BACKOFF_MAX_S) == (5.0, 300.0)
    assert MAX_ATTEMPTS == 5
    assert RETRY_AFTER_MAX_S == 15 * 60
    assert EXPIRE_AFTER_MS == 60 * 60 * SECOND
    assert TEST_INTERVAL_S == 10.0
    assert frozenset({401, 403, 404}) == REFUSED_STATUSES


def _sender_with(name: str, value: float) -> DiscordSender:
    poll, timeout, round_timeout = (
        value if name == field else default
        for field, default in (
            ("poll_interval_s", POLL_INTERVAL_S),
            ("timeout_s", HTTP_TIMEOUT_S),
            ("round_timeout_s", ROUND_TIMEOUT_S),
        )
    )
    return DiscordSender(
        Outbox(),
        webhook_url=SecretStr(CANARY_URL),
        host=HOST,
        app_url=APP_URL,
        include_project=False,
        poll_interval_s=poll,
        timeout_s=timeout,
        round_timeout_s=round_timeout,
    )


@pytest.mark.parametrize("name", ["poll_interval_s", "timeout_s", "round_timeout_s"])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan")])
def test_invalid_intervals_are_refused(name: str, value: float) -> None:
    with pytest.raises(ValueError, match=name):
        _sender_with(name, value)


def test_the_client_trusts_the_environment_and_never_follows_redirects() -> None:
    # The only client that leaves the machine, so the only one that takes HTTPS_PROXY.
    sender = make(Outbox(), Discord())
    try:
        assert sender._client.trust_env is True
        assert sender._client.follow_redirects is False
        assert sender._client.timeout == httpx.Timeout(HTTP_TIMEOUT_S)
    finally:
        sender.stop(1.0)


# Rounds.


def test_nothing_due_sends_nothing_and_asks_for_the_expiry_and_ten_alerts() -> None:
    outbox, discord, deliveries = Outbox(), Discord(), Deliveries()
    sender = make(outbox, discord, deliveries=deliveries)

    assert sender.send_once() == "idle"

    assert outbox.rounds == [(NOW_MS, NOW_MS - EXPIRE_AFTER_MS, 10)]
    assert discord.requests == []
    assert outbox.results == []
    assert deliveries.counts == Counter()


def test_a_due_alert_is_posted_with_wait_and_marked_sent() -> None:
    outbox, discord, deliveries = Outbox(due(view(7, attempts=1))), Discord(), Deliveries()
    sender = make(outbox, discord, deliveries=deliveries)

    assert sender.send_once() == "sent"

    (request,) = discord.requests
    assert request.method == "POST"
    assert request.url.path == f"/api/webhooks/{CANARY_ID}/{CANARY_TAIL}"
    assert dict(request.url.params) == {"wait": "true"}
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("cc-observatory/")
    alert = DiscordAlert(
        rule="waiting_user", value=45.0, threshold=30.0, session_id=SESSION, cwd=CWD
    )
    expected = alerts_payload([alert], host=HOST, app_url=APP_URL, include_project=False)
    assert discord.payloads() == [expected]
    assert updates(outbox) == [(7, "sent", 2, None)]
    assert deliveries.counts == Counter(sent=1)


def test_the_project_is_named_only_when_configured() -> None:
    discord = Discord()
    make(Outbox(due(view())), discord, include_project=True).send_once()
    make(Outbox(due(view())), discord, include_project=False).send_once()

    named, unnamed = discord.contents()
    assert "project project" in named
    assert "project" not in unnamed.replace(f"/sessions/{SESSION}", "")
    assert CWD not in named


def test_due_alerts_are_coalesced_into_one_message_with_one_result() -> None:
    views = [view(n, attempts=n % 3) for n in range(1, 11)]
    outbox, discord, deliveries = Outbox(due(*views)), Discord(), Deliveries()

    assert make(outbox, discord, deliveries=deliveries).send_once() == "sent"

    assert len(discord.requests) == 1
    (content,) = discord.contents()
    assert len(content.split("\n")) == 10
    assert updates(outbox) == [(n, "sent", n % 3 + 1, None) for n in range(1, 11)]
    assert deliveries.counts == Counter(sent=10)


@pytest.mark.parametrize(
    ("headers", "body", "wait_s"),
    [
        ({}, {"retry_after": 2.5, "global": False}, 2.5),
        ({"retry-after": "7"}, None, 7.0),
        ({"retry-after": "3"}, {"retry_after": 9.25}, 9.25),
        ({"retry-after": "12"}, {"retry_after": 1}, 12.0),
        ({"retry-after": "86400"}, None, RETRY_AFTER_MAX_S),
        ({}, {"retry_after": 10**9}, RETRY_AFTER_MAX_S),
        ({"retry-after": "soon"}, {"retry_after": "x"}, BACKOFF_INITIAL_S),
        ({"retry-after": "-3"}, {"retry_after": float("inf")}, BACKOFF_INITIAL_S),
        ({}, {"retry_after": True}, BACKOFF_INITIAL_S),
        ({}, ["retry_after", 30], BACKOFF_INITIAL_S),
        ({"retry-after": "nan"}, None, BACKOFF_INITIAL_S),
    ],
)
def test_a_429_keeps_the_alerts_pending_for_discords_wait(
    headers: dict[str, str], body: object, wait_s: float
) -> None:
    content = b"not json" if body is None else json.dumps(body).encode()
    discord = Discord(httpx.Response(429, headers=headers, content=content))
    outbox, deliveries = Outbox(due(view(1, attempts=2), view(2))), Deliveries()

    assert make(outbox, discord, deliveries=deliveries).send_once() == "rate_limited"

    next_at = NOW_MS + round(wait_s * SECOND)
    # Attempts unchanged: Discord asked to wait.
    assert updates(outbox) == [(1, "pending", 2, next_at), (2, "pending", 0, next_at)]
    assert deliveries.counts == Counter(rate_limited=2)


def test_a_429_with_a_huge_body_reads_only_its_start() -> None:
    body = b'{"retry_after": 4, "pad": "' + b"x" * (1024 * 1024) + b'"}'
    outbox = Outbox(due(view()))

    make(outbox, Discord(httpx.Response(429, content=body))).send_once()

    # Cut before its end: not JSON any more, so the default wait.
    assert updates(outbox) == [(1, "pending", 0, NOW_MS + round(BACKOFF_INITIAL_S * SECOND))]


def test_while_rate_limited_no_message_goes_out_but_the_expiry_runs() -> None:
    fake = FakeTime()
    discord = Discord(httpx.Response(429, json={"retry_after": 12}), httpx.Response(204))
    outbox = Outbox(due(view()), due(view(), expired=1), due(view()))
    deliveries = Deliveries()
    sender = make(outbox, discord, fake=fake, deliveries=deliveries)

    assert sender.send_once() == "rate_limited"
    fake.advance(11)
    assert sender.send_once() == "paused"
    fake.advance(1)
    assert sender.send_once() == "sent"

    assert outbox.limits == [10, 0, 10]
    assert len(discord.requests) == 2
    # The writer counts the expired deliveries, not the sender (see test_writer_discord).
    assert deliveries.counts == Counter(rate_limited=1, sent=1)


@pytest.mark.parametrize("step_s", [-3 * 3600, 3 * 3600])
def test_the_429_pause_follows_the_monotonic_clock_whatever_the_wall_clock_does(
    step_s: int,
) -> None:
    # A wall clock stepped back hours must not keep sending paused past Discord's wait
    # (nor one stepped forward end it early).
    fake = FakeTime()
    discord = Discord(httpx.Response(429, json={"retry_after": 12}), httpx.Response(204))
    sender = make(Outbox(due(view())), discord, fake=fake)

    assert sender.send_once() == "rate_limited"
    fake.now += step_s * SECOND
    fake.monotonic_now += 11
    assert sender.send_once() == "paused"
    fake.monotonic_now += 1
    assert sender.send_once() == "sent"

    assert len(discord.requests) == 2


def test_the_longest_429_pause_ends_after_fifteen_minutes_of_monotonic_time() -> None:
    fake = FakeTime()
    discord = Discord(httpx.Response(429, json={"retry_after": 10**9}), httpx.Response(204))
    sender = make(Outbox(due(view())), discord, fake=fake)

    assert sender.send_once() == "rate_limited"
    fake.now -= 24 * 3600 * SECOND
    fake.monotonic_now += RETRY_AFTER_MAX_S - 0.001
    assert sender.send_once() == "paused"
    fake.monotonic_now += 0.001
    assert sender.send_once() == "sent"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(503, json={"message": "unavailable"}),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("no route"),
        httpx.RemoteProtocolError("broken"),
    ],
    ids=lambda failure: str(getattr(failure, "status_code", type(failure).__name__)),
)
def test_a_server_error_or_no_answer_backs_off_per_attempt(
    failure: httpx.Response | Exception,
) -> None:
    views = [view(n, attempts=n) for n in range(4)]
    outbox, deliveries = Outbox(due(*views)), Deliveries()

    assert make(outbox, Discord(failure), deliveries=deliveries).send_once() == "retried"

    # 5 s after the first failed attempt, doubling.
    assert updates(outbox) == [
        (0, "pending", 1, NOW_MS + 5 * SECOND),
        (1, "pending", 2, NOW_MS + 10 * SECOND),
        (2, "pending", 3, NOW_MS + 20 * SECOND),
        (3, "pending", 4, NOW_MS + 40 * SECOND),
    ]
    assert deliveries.counts == Counter(retried=4)


def test_the_fifth_failed_attempt_gives_up() -> None:
    outbox, deliveries = Outbox(due(view(1, attempts=4), view(2, attempts=1))), Deliveries()

    assert make(outbox, Discord(httpx.Response(500)), deliveries=deliveries).send_once() == (
        "retried"
    )
    assert updates(outbox) == [(1, "failed", 5, None), (2, "pending", 2, NOW_MS + 10 * SECOND)]
    assert deliveries.counts == Counter(failed=1, retried=1)

    alone = Outbox(due(view(1, attempts=MAX_ATTEMPTS - 1)))
    assert make(alone, Discord(httpx.Response(500))).send_once() == "failed"


def test_the_back_off_is_capped() -> None:
    # Five attempts never reach the cap; a longer streak would stop at five minutes.
    assert _backoff_s(1) == BACKOFF_INITIAL_S
    assert _backoff_s(MAX_ATTEMPTS - 1) == 40.0
    assert _backoff_s(30) == BACKOFF_MAX_S


@pytest.mark.parametrize("status", sorted(REFUSED_STATUSES))
def test_a_refused_webhook_fails_the_alerts_and_suspends_sending(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    discord = Discord(httpx.Response(status, json={"message": "Unknown Webhook"}))
    outbox = Outbox(due(view(1), view(2, attempts=3)), due(view(3), expired=2))
    deliveries = Deliveries()
    sender = make(outbox, discord, deliveries=deliveries)
    assert sender.suspended_status is None

    assert sender.send_once() == "suspended"
    assert updates(outbox) == [(1, "failed", 1, None), (2, "failed", 4, None)]
    assert sender.suspended_status == status
    for _ in range(3):
        assert sender.send_once() == "suspended"

    # No request after the refused one: the expiry alone runs.
    assert len(discord.requests) == 1
    assert outbox.limits == [10, 0, 0, 0]
    assert len(outbox.results) == 1
    assert deliveries.counts == Counter(suspended=2)
    refusals = [r for r in caplog.records if "refused the webhook" in r.getMessage()]
    assert len(refusals) == 1
    assert f"status {status}" in refusals[0].getMessage()
    assert_no_canary(caplog.text)


@pytest.mark.parametrize("status", [301, 302, 307, 400, 405, 413, 422])
def test_another_status_fails_the_alerts_without_suspending(status: int) -> None:
    headers = {"location": "https://example.com/elsewhere"} if status < 400 else {}
    discord = Discord(httpx.Response(status, headers=headers))
    outbox, deliveries = Outbox(due(view(1, attempts=1))), Deliveries()
    sender = make(outbox, discord, deliveries=deliveries)

    assert sender.send_once() == "failed"

    assert updates(outbox) == [(1, "failed", 2, None)]
    assert sender.suspended_status is None
    # Never followed.
    assert len(discord.requests) == 1
    assert deliveries.counts == Counter(failed=1)


def test_expired_deliveries_are_left_to_the_writer_to_count() -> None:
    # The writer counts them when it marks them (on_discord_expired): a round whose answer
    # the sender never reads still expires its deliveries, so the sender cannot count them.
    deliveries = Deliveries()
    sender = make(Outbox(due(expired=3)), Discord(), deliveries=deliveries)

    assert sender.send_once() == "idle"
    assert deliveries.counts == Counter()


def test_a_writer_that_does_not_answer_in_time_skips_the_round(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbox = Outbox(None)
    sender = make(outbox, Discord(), round_timeout_s=0.01)

    assert sender.send_once() == "unavailable"

    (future,) = outbox.futures
    assert future.cancelled()
    assert "no answer from the writer in time" in caplog.text


def test_a_writer_that_cannot_read_skips_the_round() -> None:
    discord = Discord()
    sender = make(Outbox(DiscordUnavailableError("the read failed")), discord)

    assert sender.send_once() == "unavailable"
    assert discord.requests == []


def test_a_failing_counter_does_not_stop_a_round(caplog: pytest.LogCaptureFixture) -> None:
    def broken(result: DeliveryResult, count: int) -> None:
        raise RuntimeError(CANARY_URL)

    outbox = Outbox(due(view()))
    sender = DiscordSender(
        outbox,
        webhook_url=SecretStr(CANARY_URL),
        host=HOST,
        app_url=APP_URL,
        include_project=False,
        clock=lambda: NOW_MS,
        transport=Discord().transport(),
        on_delivery=broken,
    )
    try:
        assert sender.send_once() == "sent"
    finally:
        sender.stop(1.0)
    assert updates(outbox) == [(1, "sent", 1, None)]
    assert "discord delivery not counted (RuntimeError)" in caplog.text
    assert_no_canary(caplog.text)


# The loop.


def test_the_loop_runs_a_round_every_five_seconds() -> None:
    fake = FakeTime(rounds=3)
    outbox = Outbox()
    make(outbox, Discord(), fake=fake).run()

    assert fake.waits == [POLL_INTERVAL_S] * 3
    assert [now for now, _, _ in outbox.rounds] == [NOW_MS + n * 5 * SECOND for n in (1, 2, 3)]


def test_a_failing_round_is_logged_by_type_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken(Outbox):
        def submit_discord_round(
            self, now_ms: int, *, expire_before_ms: int, limit: int
        ) -> Future[DueDeliveries]:
            self.rounds.append((now_ms, expire_before_ms, limit))
            raise RuntimeError(CANARY_URL)

    fake = FakeTime(rounds=2)
    outbox = Broken()
    make(outbox, Discord(), fake=fake).run()

    assert len(outbox.rounds) == 2
    assert caplog.text.count("discord round failed (RuntimeError)") == 2
    assert_no_canary(caplog.text)


def test_start_runs_the_loop_and_stop_is_prompt_and_closes_the_client() -> None:
    outbox, discord = Outbox(), Discord()
    sender = DiscordSender(
        outbox,
        webhook_url=SecretStr(CANARY_URL),
        host=HOST,
        app_url=APP_URL,
        include_project=False,
        poll_interval_s=0.01,
        transport=discord.transport(),
    )
    sender.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            sender.start()
        for _ in range(500):
            if outbox.rounds:
                break
            threading.Event().wait(0.01)
        assert outbox.rounds
        assert sender.is_alive()
    finally:
        sender.stop(5.0)
    assert not sender.is_alive()
    assert discord.closed


def test_stop_without_start_closes_the_client() -> None:
    discord = Discord()
    sender = make(Outbox(), discord)
    assert not sender.is_alive()

    sender.stop(1.0)

    assert discord.closed


def test_a_stop_that_times_out_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    release = threading.Event()

    class Stuck(Outbox):
        def submit_discord_round(
            self, now_ms: int, *, expire_before_ms: int, limit: int
        ) -> Future[DueDeliveries]:
            release.wait(5.0)
            return super().submit_discord_round(
                now_ms, expire_before_ms=expire_before_ms, limit=limit
            )

    sender = DiscordSender(
        Stuck(),
        webhook_url=SecretStr(CANARY_URL),
        host=HOST,
        app_url=APP_URL,
        include_project=False,
        poll_interval_s=0.01,
        transport=Discord().transport(),
    )
    sender.start()
    threading.Event().wait(0.1)
    sender.stop(0.2)
    release.set()

    assert "discord sender did not stop within 0.2 s" in caplog.text


# The settings screen's test button.


def test_the_test_message_is_sent_and_stores_nothing() -> None:
    outbox, discord = Outbox(), Discord(httpx.Response(200, json={"id": "1"}))
    sender = make(outbox, discord)

    result = sender.send_connection_test()

    assert result == ConnectionTestResult(kind="sent", status=200)
    assert connection_test_status(result) == "Test message sent."
    assert discord.payloads() == [connection_test_payload(HOST)]
    assert dict(discord.requests[0].url.params) == {"wait": "true"}
    assert outbox.rounds == []
    assert outbox.results == []


def test_the_test_button_sends_at_most_one_message_per_ten_seconds() -> None:
    fake, discord = FakeTime(), Discord()
    sender = make(Outbox(), discord, fake=fake)

    assert sender.send_connection_test().kind == "sent"
    fake.monotonic_now += TEST_INTERVAL_S - 0.1
    too_soon = sender.send_connection_test()
    assert too_soon == ConnectionTestResult(kind="too_soon")
    assert connection_test_status(too_soon) == "Wait a few seconds before sending another test."
    fake.monotonic_now += 0.1
    assert sender.send_connection_test().kind == "sent"

    assert len(discord.requests) == 2


@pytest.mark.parametrize(
    ("status", "line"),
    [
        *(
            (
                status,
                f"Discord did not accept the test message (status {status})."
                " Check CCOBS_DISCORD_WEBHOOK_URL.",
            )
            for status in (401, 403, 404)
        ),
        (429, "Discord is rate limiting the webhook. Try again in a minute."),
        *(
            (status, f"Discord had an error (status {status}). Try again later.")
            for status in (500, 502, 503, 504)
        ),
        *(
            (status, f"Discord did not accept the test message (status {status}).")
            for status in (302, 400, 405, 413)
        ),
    ],
)
def test_a_refused_test_says_what_the_status_means_and_never_suspends(
    status: int, line: str
) -> None:
    sender = make(Outbox(), Discord(httpx.Response(status)))

    result = sender.send_connection_test()

    assert result == ConnectionTestResult(kind="refused", status=status)
    assert connection_test_status(result) == line
    assert sender.suspended_status is None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout(CANARY_URL), "timeout"),
        (httpx.ConnectError(CANARY_URL), "connection error"),
        (httpx.RemoteProtocolError(CANARY_URL), "error"),
        (httpx.UnsupportedProtocol(CANARY_URL), "error"),
    ],
)
def test_an_unreachable_discord_says_why_in_one_word(error: Exception, reason: str) -> None:
    result = make(Outbox(), Discord(error)).send_connection_test()

    assert result == ConnectionTestResult(kind="unreachable", reason=reason)
    assert connection_test_status(result) == f"Could not reach Discord ({reason})."
    assert_no_canary(connection_test_status(result))


# The webhook URL never reaches a log.


def test_httpx_records_naming_a_webhook_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hide_webhook_urls()
    hide_webhook_urls()  # idempotent

    logging.getLogger("httpx").info("HTTP Request: POST %s", httpx.URL(CANARY_URL))
    logging.getLogger("httpcore.http11").debug("request url=%r", CANARY_URL)
    logging.getLogger("httpx").info("HTTP Request: GET %s", "http://loki:3100/ready")
    logging.getLogger("httpx").info("broken %s %s", "one")

    assert_no_canary(caplog.text)
    assert "http://loki:3100/ready" in caplog.text
    assert "broken" not in caplog.text
    assert logging.getLogger("httpx").filters.count(logging.getLogger("httpx").filters[0]) == 1


def every_path() -> list[httpx.Response | Exception]:
    """A response or an error for each path of a round, each holding the URL where an
    exception would."""
    return [
        httpx.Response(204),
        httpx.Response(429, json={"retry_after": 0.001}),
        httpx.Response(500, text=CANARY_URL),
        httpx.ReadTimeout(CANARY_URL),
        httpx.ConnectError(CANARY_URL),
        httpx.RemoteProtocolError(CANARY_URL),
        httpx.Response(400, text=CANARY_URL),
        httpx.Response(302, headers={"location": CANARY_URL}),
        httpx.Response(404, json={"message": "Unknown Webhook"}),
    ]


def test_the_canary_never_reaches_a_log_on_any_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "ccobs"):
        caplog.set_level(logging.DEBUG, logger=name)
    fake = FakeTime()
    script = every_path()
    discord = Discord(*script)
    outbox = Outbox(due(view(1), view(2, rule="daily_cost", session_id=None, cwd=None)))
    sender = make(outbox, discord, fake=fake, include_project=True)
    results: list[str] = []
    for _ in script:
        results.append(sender.send_once())
        fake.advance(60)
    for _ in range(4):
        fake.monotonic_now += TEST_INTERVAL_S
        results.append(connection_test_status(sender.send_connection_test()))
    sender.stop(1.0)

    assert results[:9] == [
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
    # httpx logged every request, and every record of them was dropped.
    assert caplog.records
    assert_no_canary(caplog.text)
    for text in results:
        assert_no_canary(text)
    for batch in outbox.results:
        assert_no_canary(repr(batch))


def test_the_url_never_shows_in_the_senders_repr() -> None:
    sender = make(Outbox(), Discord())
    try:
        assert_no_canary(repr(sender))
        assert_no_canary(repr(vars(sender)))
    finally:
        sender.stop(1.0)
