"""The Discord sender's reads and writes on the writer thread (ADR 0046).

The sender never touches the database: each round it hands the writer a
``DiscordRound`` with a future, which the writer settles with the deliveries due (after
expiring the old ones, in one transaction), and it hands back each attempt's outcome as
``DiscordResults``, which the writer records in one transaction. A round whose request
gave up (its future cancelled) is skipped; a stop or a failed read settles it with
``DiscordUnavailableError``. The writer counts the deliveries it expires
(``on_discord_expired``), so the count never depends on the sender reading the answer.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from ccobs.domain.alerts import AlertFired
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import (
    DeliveryUpdate,
    DueDeliveries,
    Repository,
)
from ccobs.server.discord import EXPIRE_AFTER_MS, DiscordSender
from ccobs.server.ingest import HookQueue, RawHook
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.writer import DiscordUnavailableError, Writer

HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
SESSION: Final = "00000000-0000-4000-8000-000000000000"
CWD: Final = "/home/someone/work/project"
PRIVATE: Final = "C:\\Users\\someone-private\\secret"
MIB: Final = 1024 * 1024


class FailingRepository(Repository):
    """A repository whose Discord reads and writes fail on demand."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.fail = False
        # Set, a read waits for ``release`` once ``entered`` is set: a slow writer.
        self.slow = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def take_due_deliveries(
        self, host: str, *, now_ms: int, expire_before_ms: int, limit: int
    ) -> DueDeliveries:
        if self.fail:
            raise OperationalError("UPDATE alerts", None, Exception(PRIVATE))
        if self.slow:
            self.entered.set()
            assert self.release.wait(10.0)
        return super().take_due_deliveries(
            host, now_ms=now_ms, expire_before_ms=expire_before_ms, limit=limit
        )

    def record_deliveries(self, host: str, updates: Sequence[DeliveryUpdate]) -> int:
        if self.fail:
            raise OperationalError("UPDATE alerts", None, Exception(PRIVATE))
        return super().record_deliveries(host, updates)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[FailingRepository]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    yield FailingRepository(engine)
    engine.dispose()


class Expired:
    """The writer's ``on_discord_expired``: each count it reports."""

    def __init__(self) -> None:
        self.counts: list[int] = []

    def __call__(self, count: int) -> None:
        self.counts.append(count)


@pytest.fixture
def expired() -> Expired:
    return Expired()


@pytest.fixture
def writer(repo: FailingRepository, expired: Expired) -> Writer:
    queue = HookQueue(max_items=100, max_bytes=MIB)
    body = json.dumps(
        {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "cwd": CWD}
    ).encode()
    assert queue.offer(RawHook(body, BASE, 1))
    made = Writer(
        queue,
        repo,
        HOST,
        stale_after_ms=3_600_000,
        batch_wait_s=0.0,
        discord_configured=True,
        on_discord_expired=expired,
        sleep=lambda seconds: None,
    )
    run_all(made)
    return made


def run_all(writer: Writer) -> None:
    while writer.run_once(0.0):
        pass


def pending(repo: Repository, at: int = BASE) -> int:
    fired = AlertFired(
        rule="waiting_user", subject=SESSION, fired_at_ms=at, value=40.0, threshold=30.0
    )
    return repo.insert_alert(HOST, fired, discord_state="pending", discord_next_at_ms=at)


def taken(future: Future[DueDeliveries]) -> DueDeliveries:
    assert future.done()
    return future.result(timeout=0)


def test_a_round_expires_then_reads_the_deliveries_due(
    writer: Writer, repo: FailingRepository, expired: Expired
) -> None:
    old = pending(repo, BASE - 10)
    due = pending(repo, BASE)

    future = writer.submit_discord_round(BASE + 5, expire_before_ms=BASE - 5, limit=10)
    assert not future.done()
    run_all(writer)

    result = taken(future)
    assert result.expired == 1
    assert [(view.alert.id, view.cwd) for view in result.alerts] == [(due, CWD)]
    states = {alert.id: alert.discord_state for alert in repo.list_alerts(HOST, 10)}
    assert states == {old: "expired", due: "pending"}
    assert expired.counts == [1]


def test_the_writer_counts_what_it_expires_even_if_nobody_reads_the_answer(
    writer: Writer, repo: FailingRepository, expired: Expired
) -> None:
    for at in (BASE - 30, BASE - 20, BASE - 10):
        pending(repo, at)

    writer.submit_discord_round(BASE, expire_before_ms=BASE - 5, limit=0)
    run_all(writer)
    # Nothing left to expire: no count of zero.
    writer.submit_discord_round(BASE, expire_before_ms=BASE - 5, limit=0)
    run_all(writer)

    assert expired.counts == [3]


def test_a_writer_without_an_expiry_counter_still_expires(
    writer: Writer, repo: FailingRepository
) -> None:
    # writer: its session holds the alerts.
    made = Writer(
        HookQueue(max_items=10, max_bytes=MIB),
        repo,
        HOST,
        stale_after_ms=3_600_000,
        discord_configured=True,
        sleep=lambda seconds: None,
    )
    pending(repo, BASE - 10)

    future = made.submit_discord_round(BASE, expire_before_ms=BASE, limit=10)
    run_all(made)

    assert taken(future).expired == 1


def test_a_failing_expiry_counter_is_logged_by_type_and_the_round_answered(
    writer: Writer, repo: FailingRepository, caplog: pytest.LogCaptureFixture
) -> None:
    # writer: its session holds the alerts.
    def broken(count: int) -> None:
        raise RuntimeError(PRIVATE)

    made = Writer(
        HookQueue(max_items=10, max_bytes=MIB),
        repo,
        HOST,
        stale_after_ms=3_600_000,
        discord_configured=True,
        on_discord_expired=broken,
        sleep=lambda seconds: None,
    )
    pending(repo, BASE - 10)

    future = made.submit_discord_round(BASE, expire_before_ms=BASE, limit=10)
    run_all(made)

    assert taken(future).expired == 1
    assert "discord expiries not counted (RuntimeError)" in caplog.text
    assert PRIVATE not in caplog.text


def test_expiries_are_counted_when_the_sender_gave_up_on_a_round_already_running(
    writer: Writer, repo: FailingRepository
) -> None:
    # The race: the sender's wait for the round runs out while the writer is reading, so
    # its cancel() fails, the writer still expires the deliveries and nobody reads the
    # answer. The writer counts them anyway.
    counted: list[tuple[str, int]] = []
    expired = Expired()
    real = Writer(
        HookQueue(max_items=10, max_bytes=MIB),
        repo,
        HOST,
        stale_after_ms=3_600_000,
        discord_configured=True,
        on_discord_expired=expired,
        poll_s=0.01,
    )
    now = BASE + EXPIRE_AFTER_MS + 1
    for at in (BASE - 2, BASE - 1):
        pending(repo, at)
    sender = DiscordSender(
        real,
        webhook_url=SecretStr("https://discord.invalid/api/webhooks/1/not-a-real-token"),
        host=HOST,
        app_url="http://localhost:4300",
        include_project=False,
        # Longer than the writer takes to pick the round up, shorter than its read.
        round_timeout_s=1.0,
        clock=lambda: now,
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        on_delivery=lambda result, count: counted.append((result, count)),
    )
    repo.slow = True
    real.start()
    try:
        assert sender.send_once() == "unavailable"
        assert repo.entered.is_set()
        repo.release.set()
    finally:
        real.stop(5.0)
        sender.stop(1.0)

    assert {alert.discord_state for alert in repo.list_alerts(HOST, 10)} == {"expired"}
    assert expired.counts == [2]
    assert counted == []


def test_results_are_recorded(writer: Writer, repo: FailingRepository) -> None:
    alert_id = pending(repo)

    writer.submit_discord_results([DeliveryUpdate(alert_id=alert_id, state="sent", attempts=1)])
    run_all(writer)

    (alert,) = repo.list_alerts(HOST, 10)
    assert (alert.discord_state, alert.discord_attempts) == ("sent", 1)


def test_results_are_recorded_before_the_next_round_reads(
    writer: Writer, repo: FailingRepository
) -> None:
    # Queue order: the next round never sees a delivery the last one already sent.
    alert_id = pending(repo)
    writer.submit_discord_results([DeliveryUpdate(alert_id=alert_id, state="sent", attempts=1)])
    future = writer.submit_discord_round(BASE + 5, expire_before_ms=0, limit=10)
    run_all(writer)

    assert taken(future).alerts == []


def test_a_round_that_gave_up_is_skipped(writer: Writer, repo: FailingRepository) -> None:
    pending(repo, BASE - 10)
    future = writer.submit_discord_round(BASE, expire_before_ms=BASE, limit=10)
    assert future.cancel()

    run_all(writer)

    (alert,) = repo.list_alerts(HOST, 10)
    assert alert.discord_state == "pending"


def test_a_failed_round_or_record_is_logged_by_type_only(
    writer: Writer, repo: FailingRepository, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    alert_id = pending(repo)
    repo.fail = True

    future = writer.submit_discord_round(BASE, expire_before_ms=0, limit=10)
    writer.submit_discord_results([DeliveryUpdate(alert_id=alert_id, state="sent", attempts=1)])
    run_all(writer)

    with pytest.raises(DiscordUnavailableError):
        future.result(timeout=0)
    assert "discord deliveries not read (OperationalError)" in caplog.text
    assert "discord deliveries not recorded (OperationalError)" in caplog.text
    assert PRIVATE not in caplog.text
    assert "UPDATE" not in caplog.text
    repo.fail = False
    (alert,) = repo.list_alerts(HOST, 10)
    assert alert.discord_state == "pending"
    # The writer goes on.
    writer.submit_discord_results([DeliveryUpdate(alert_id=alert_id, state="sent", attempts=1)])
    run_all(writer)
    (alert,) = repo.list_alerts(HOST, 10)
    assert alert.discord_state == "sent"


def test_a_round_still_queued_at_stop_is_settled(writer: Writer) -> None:
    future = writer.submit_discord_round(BASE, expire_before_ms=0, limit=10)
    gave_up = writer.submit_discord_round(BASE, expire_before_ms=0, limit=10)
    assert gave_up.cancel()
    writer.submit_discord_results([])

    writer.stop(0.0)

    with pytest.raises(DiscordUnavailableError, match="the writer stopped"):
        future.result(timeout=0)
