"""The sweeper (``ccobs.server.sweeps``): the stale sweep and retention, on a schedule.

The unit tests drive ``Sweeper.run`` on the test thread with a fake wait that advances a
fake clock and a fake writer that records what was submitted. The end-to-end tests run
the real app with a writer the test drives (``run_once``) and a sweeper on small
intervals, then move the app's clock past the stale threshold (ADR 0024, 12 h) and past
the retention period (ADR 0026, 90 days).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue
from ccobs.server.sweeps import RETENTION_INTERVAL_S, TICK_INTERVAL_S, Sweeper
from ccobs.server.writer import BlockClosedCallback, Writer

HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
SECOND_MS: Final = 1000
HOUR_MS: Final = 3_600_000
DAY_MS: Final = 24 * HOUR_MS
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
API: Final = "/api/v1/sessions"
FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "hooks" / "headless-tools-subagent.jsonl"
# Up to the subagent's PermissionRequest (its 19th line): a running agent and open blocks.
PREFIX: Final = 19
SILENT: Final = "00000000-0000-4000-8000-0000000000a1"
RECENT: Final = "00000000-0000-4000-8000-0000000000a2"
# How long the end-to-end tests wait for the sweeper thread before they give up.
PATIENCE_S: Final = 10.0
# The most writer rounds one persist runs: far more than the controls a 20 ms sweeper
# submits in PATIENCE_S, each round taking up to 256 of them.
PERSIST_ROUNDS: Final = 1000

type Payload = dict[str, Any]


# Unit tests: a fake writer, a fake clock, a fake wait.


class Recorder:
    """A writer that records what the sweeper submits; ``fail`` makes the next calls raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail: dict[str, Exception] = {}

    def submit_tick(self, now_ms: int) -> None:
        self.calls.append(("tick", now_ms))
        if "tick" in self.fail:
            raise self.fail["tick"]

    def submit_retention(self, cutoff_ms: int) -> None:
        self.calls.append(("retention", cutoff_ms))
        if "retention" in self.fail:
            raise self.fail["retention"]


class FakeTime:
    """A wall clock that only the fake wait moves, and a wait that stops after ``rounds``."""

    def __init__(self, rounds: int) -> None:
        self.now = BASE
        self.rounds = rounds
        self.waits: list[float] = []

    def clock(self) -> int:
        return self.now

    def wait(self, timeout_s: float) -> bool:
        if len(self.waits) == self.rounds:
            return True
        self.waits.append(timeout_s)
        self.now += int(timeout_s * SECOND_MS)
        return False


def make_sweeper(
    writer: Recorder, fake: FakeTime, *, retention: timedelta = timedelta(days=90), **kwargs: Any
) -> Sweeper:
    return Sweeper(writer, retention=retention, clock=fake.clock, wait=fake.wait, **kwargs)


def test_the_default_intervals() -> None:
    # Staleness is measured in hours (ADR 0024): a minute of granularity is plenty.
    assert TICK_INTERVAL_S == 60.0
    assert RETENTION_INTERVAL_S == 3600.0
    sweeper = Sweeper(Recorder(), retention=timedelta(days=90))
    assert sweeper.tick_interval_s == TICK_INTERVAL_S
    assert sweeper.retention_interval_s == RETENTION_INTERVAL_S


def test_it_ticks_at_its_interval_with_the_current_time() -> None:
    writer, fake = Recorder(), FakeTime(rounds=5)

    make_sweeper(writer, fake, tick_interval_s=60.0, retention_interval_s=3600.0).run()

    assert fake.waits == [60.0] * 5
    ticks = [now for name, now in writer.calls if name == "tick"]
    assert ticks == [BASE + n * 60 * SECOND_MS for n in range(1, 6)]


def test_retention_runs_after_the_first_tick_then_at_its_interval() -> None:
    writer, fake = Recorder(), FakeTime(rounds=7)
    retention = timedelta(days=3)

    make_sweeper(
        writer, fake, retention=retention, tick_interval_s=10.0, retention_interval_s=30.0
    ).run()

    now = [BASE + n * 10 * SECOND_MS for n in range(1, 8)]
    cutoff = [at - 3 * DAY_MS for at in now]
    assert writer.calls == [
        ("tick", now[0]),
        ("retention", cutoff[0]),
        ("tick", now[1]),
        ("tick", now[2]),
        ("tick", now[3]),
        ("retention", cutoff[3]),
        ("tick", now[4]),
        ("tick", now[5]),
        ("tick", now[6]),
        ("retention", cutoff[6]),
    ]


def test_a_retention_interval_shorter_than_a_tick_runs_on_every_tick() -> None:
    writer, fake = Recorder(), FakeTime(rounds=2)

    make_sweeper(writer, fake, tick_interval_s=60.0, retention_interval_s=1.0).run()

    assert [name for name, _ in writer.calls] == ["tick", "retention"] * 2


def test_a_retention_interval_that_is_not_a_multiple_rounds_up() -> None:
    writer, fake = Recorder(), FakeTime(rounds=4)

    make_sweeper(writer, fake, tick_interval_s=10.0, retention_interval_s=25.0).run()

    # Every third tick: never more often than the interval asks.
    assert [name for name, _ in writer.calls] == [
        "tick",
        "retention",
        "tick",
        "tick",
        "tick",
        "retention",
    ]


@pytest.mark.parametrize("name", ["tick_interval_s", "retention_interval_s"])
@pytest.mark.parametrize("value", [0.0, -1.0])
def test_intervals_must_be_positive(name: str, value: float) -> None:
    intervals: dict[str, Any] = {name: value}
    with pytest.raises(ValueError, match=name):
        Sweeper(Recorder(), retention=timedelta(days=1), **intervals)


@pytest.mark.parametrize("failing", ["tick", "retention"])
def test_a_failing_submit_is_logged_by_its_type_and_the_loop_goes_on(
    failing: str, caplog: pytest.LogCaptureFixture
) -> None:
    writer, fake = Recorder(), FakeTime(rounds=3)
    writer.fail[failing] = RuntimeError("secret detail C:\\Users\\someone")
    caplog.set_level(logging.DEBUG, logger="ccobs.server.sweeps")

    make_sweeper(writer, fake, tick_interval_s=1.0, retention_interval_s=1.0).run()

    assert [name for name, _ in writer.calls] == ["tick", "retention"] * 3
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 3
    for record in errors:
        message = record.getMessage()
        assert "RuntimeError" in message
        assert "secret" not in message
        assert "someone" not in message
        assert record.exc_info is None


def test_a_failing_clock_is_logged_and_the_loop_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    writer, fake = Recorder(), FakeTime(rounds=2)
    times: Iterator[int | Exception] = iter([OSError("clock"), BASE])

    def clock() -> int:
        value = next(times)
        if isinstance(value, Exception):
            raise value
        return value

    caplog.set_level(logging.ERROR, logger="ccobs.server.sweeps")
    sweeper = Sweeper(
        writer,
        retention=timedelta(days=1),
        tick_interval_s=1.0,
        retention_interval_s=1.0,
        clock=clock,
        wait=fake.wait,
    )

    sweeper.run()

    assert writer.calls == [("tick", BASE), ("retention", BASE - DAY_MS)]
    assert ["OSError" in record.getMessage() for record in caplog.records] == [True]


def test_a_retention_on_a_round_the_clock_fails_moves_to_the_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer, fake = Recorder(), FakeTime(rounds=4)
    failures = iter([True])

    def clock() -> int:
        if next(failures, False):
            raise OSError("clock")
        return fake.clock()

    caplog.set_level(logging.ERROR, logger="ccobs.server.sweeps")
    sweeper = Sweeper(
        writer,
        retention=timedelta(days=1),
        tick_interval_s=10.0,
        retention_interval_s=30.0,
        clock=clock,
        wait=fake.wait,
    )

    sweeper.run()

    # Round 1 (a retention round) fails; round 2 runs its retention; round 4 is the next
    # scheduled one.
    now = [BASE + n * 10 * SECOND_MS for n in range(1, 5)]
    assert writer.calls == [
        ("tick", now[1]),
        ("retention", now[1] - DAY_MS),
        ("tick", now[2]),
        ("tick", now[3]),
        ("retention", now[3] - DAY_MS),
    ]
    assert ["OSError" in record.getMessage() for record in caplog.records] == [True]


def test_stop_interrupts_the_wait_at_once() -> None:
    writer = Recorder()
    sweeper = Sweeper(writer, retention=timedelta(days=1), tick_interval_s=60.0)
    sweeper.start()
    assert sweeper.is_alive()

    started = time.monotonic()
    sweeper.stop(timeout_s=5.0)

    assert time.monotonic() - started < 2.0
    assert not sweeper.is_alive()
    # Stopped before the first tick was due.
    assert writer.calls == []


def test_the_thread_submits_on_its_own() -> None:
    writer = Recorder()
    sweeper = Sweeper(
        writer, retention=timedelta(days=1), tick_interval_s=0.001, retention_interval_s=0.001
    )
    sweeper.start()
    deadline = time.monotonic() + PATIENCE_S
    while len(writer.calls) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    sweeper.stop(timeout_s=5.0)

    assert {name for name, _ in writer.calls} == {"tick", "retention"}
    assert not sweeper.is_alive()


def test_a_sweeper_starts_once_and_stops_without_a_thread() -> None:
    sweeper = Sweeper(Recorder(), retention=timedelta(days=1))
    sweeper.stop(timeout_s=1.0)  # never started: nothing to join
    sweeper = Sweeper(Recorder(), retention=timedelta(days=1))
    sweeper.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            sweeper.start()
    finally:
        sweeper.stop(timeout_s=5.0)


def test_a_thread_that_does_not_stop_in_time_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    entered = threading.Event()

    def stuck(timeout_s: float) -> bool:
        entered.set()
        release.wait(PATIENCE_S)
        return True

    sweeper = Sweeper(Recorder(), retention=timedelta(days=1), wait=stuck)
    caplog.set_level(logging.WARNING, logger="ccobs.server.sweeps")
    sweeper.start()
    assert entered.wait(PATIENCE_S)

    sweeper.stop(timeout_s=0.01)

    assert any("did not stop" in record.getMessage() for record in caplog.records)
    release.set()


# End to end: the real app, a writer the test drives, the sweeper on small intervals.


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``.
    ``ticks`` counts the ticks the sweeper has submitted."""

    ticks = 0

    def start(self) -> None:
        pass

    def submit_tick(self, now_ms: int) -> None:
        super().submit_tick(now_ms)
        # Only the sweeper thread writes it; the test thread only reads it.
        self.ticks += 1


class Clock:
    """The app's wall clock: hook receive times and the sweeper's now."""

    def __init__(self) -> None:
        self.now = BASE

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    def manual(
        queue: HookQueue, repository: Repository, on_block_closed: BlockClosedCallback
    ) -> ManualWriter:
        return ManualWriter(queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0)

    queue = HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    app = create_app(
        settings,
        queue=queue,
        clock=clock,
        writer_factory=manual,
        serve_metrics=False,
        sweep_intervals=(0.02, 0.02),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def session_lines(session_id: str) -> list[tuple[int, Payload]]:
    lines = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines()[:PREFIX]:
        document = json.loads(line)
        lines.append((document["offset_ms"], {**document["payload"], "session_id": session_id}))
    return lines


def post(
    client: TestClient, clock: Clock, start_ms: int, lines: Sequence[tuple[int, Payload]]
) -> None:
    for offset, document in lines:
        clock.now = start_ms + offset
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204


def writer_of(client: TestClient) -> ManualWriter:
    app = client.app
    assert isinstance(app, FastAPI)
    writer = app.state.writer
    assert isinstance(writer, ManualWriter)
    return writer


def persist(client: TestClient, deadline: float | None = None) -> None:
    """Run the writer until its queues are empty, for at most PERSIST_ROUNDS rounds and
    until ``deadline`` (``time.monotonic``): the sweeper keeps submitting meanwhile."""
    writer = writer_of(client)
    end = time.monotonic() + PATIENCE_S if deadline is None else deadline
    for _ in range(PERSIST_ROUNDS):
        if not writer.run_once(0.0) or time.monotonic() >= end:
            return
    pytest.fail("the writer's queues did not empty")


def detail(client: TestClient, session_id: str) -> Any:
    response = client.get(f"{API}/{session_id}")
    assert response.status_code == 200, response.text
    return response.json()


def eventually(client: TestClient, done: Callable[[], bool]) -> None:
    """Persist what the sweeper submits until ``done``, or fail after PATIENCE_S."""
    deadline = time.monotonic() + PATIENCE_S
    while time.monotonic() < deadline:
        persist(client, deadline)
        if done():
            return
        time.sleep(0.01)
    pytest.fail("the sweeper did not get there in time")


def test_a_silent_session_goes_stale_and_is_deleted_after_retention(
    client: TestClient, clock: Clock
) -> None:
    post(client, clock, BASE, session_lines(SILENT))
    persist(client)
    before = detail(client, SILENT)
    open_blocks = [block["block_id"] for block in before["blocks"] if block["closed_at_ms"] is None]
    running = [agent["agent_id"] for agent in before["agents"] if agent["status"] == "running"]
    assert before["status"] != "stale"
    assert open_blocks
    assert running
    # A tick submitted after the last event, within the threshold, leaves it alone.
    writer = writer_of(client)
    ticks = writer.ticks
    eventually(client, lambda: writer.ticks > ticks)
    persist(client)
    assert detail(client, SILENT)["status"] == before["status"]

    swept_at = BASE + 13 * HOUR_MS
    clock.now = swept_at
    eventually(client, lambda: detail(client, SILENT)["status"] == "stale")

    after = detail(client, SILENT)
    blocks = {block["block_id"]: block for block in after["blocks"]}
    for block_id in open_blocks:
        assert blocks[block_id]["close_source"] == "sweep"
        assert blocks[block_id]["close_reason"] == "stale"
        assert blocks[block_id]["closed_at_ms"] is not None
        assert blocks[block_id]["closed_at_ms"] <= swept_at
    agents = {agent["agent_id"]: agent for agent in after["agents"]}
    for agent_id in running:
        assert agents[agent_id]["status"] == "stopped"
        assert agents[agent_id]["stop_reason"] == "stale"
    assert writer_of(client).state(SILENT) is not None  # stale sessions stay in memory

    # Past the retention period (90 days by default): a session that just got events stays.
    later = BASE + 91 * DAY_MS
    post(client, clock, later, session_lines(RECENT))
    persist(client)

    def silent_deleted() -> bool:
        status: int = client.get(f"{API}/{SILENT}").status_code
        return status == 404

    eventually(client, silent_deleted)
    assert client.get(f"{API}/{SILENT}").json() == {"detail": "session not found"}
    recent = detail(client, RECENT)
    assert recent["status"] not in ("stale", "ended")
    assert writer_of(client).state(SILENT) is None
    assert writer_of(client).state(RECENT) is not None


def test_create_app_can_leave_the_sweeper_off(settings: Settings) -> None:
    app = create_app(settings, serve_metrics=False, sweeps=False)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert app.state.sweeper is None


def test_the_default_sweeper_follows_the_settings(settings: Settings) -> None:
    configured = settings.model_copy(update={"retention": timedelta(days=7)})
    app = create_app(configured, serve_metrics=False)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        sweeper = app.state.sweeper
        assert isinstance(sweeper, Sweeper)
        assert sweeper.is_alive()
        assert sweeper.retention_ms == 7 * DAY_MS
        assert sweeper.tick_interval_s == TICK_INTERVAL_S
        assert sweeper.retention_interval_s == RETENTION_INTERVAL_S
    assert not sweeper.is_alive()
    assert app.state.sweeper is None


def test_the_lifespan_stops_the_sweeper_before_the_writer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    original_start = Sweeper.start
    original_sweeper_stop = Sweeper.stop
    original_writer_start = Writer.start
    original_writer_stop = Writer.stop

    def sweeper_start(self: Sweeper) -> None:
        order.append("sweeper start")
        original_start(self)

    def sweeper_stop(self: Sweeper, timeout_s: float) -> None:
        order.append("sweeper stop")
        original_sweeper_stop(self, timeout_s)

    def writer_start(self: Writer) -> None:
        order.append("writer start")
        original_writer_start(self)

    def writer_stop(self: Writer, timeout_s: float) -> None:
        order.append("writer stop")
        original_writer_stop(self, timeout_s)

    monkeypatch.setattr(Sweeper, "start", sweeper_start)
    monkeypatch.setattr(Sweeper, "stop", sweeper_stop)
    monkeypatch.setattr(Writer, "start", writer_start)
    monkeypatch.setattr(Writer, "stop", writer_stop)
    app = create_app(settings, serve_metrics=False)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert order == ["writer start", "sweeper start"]

    assert order == ["writer start", "sweeper start", "sweeper stop", "writer stop"]
