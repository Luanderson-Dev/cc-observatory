"""The writer thread: parse, reduce and persist the hook queue (ADRs 0023, 0024, 0026, 0027).

The writer takes ``RawHook`` items off the ``HookQueue``, parses them (``parse_hook``),
folds them into the in-memory session states (``ccobs.domain.sessions.apply``) and
persists each micro-batch in one transaction. The sweep (``Tick``), the telemetry join
(``TelemetryClose``) and retention go through the same thread, so every write is
serialized. ``run_once`` drives it synchronously; a few tests run the real thread.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import OperationalError

from ccobs.domain.app_metrics import EVENT_LABELS, event_label
from ccobs.domain.denials import OpenBlock
from ccobs.domain.hook_config import HOOK_PATH
from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import SessionState, apply
from ccobs.server import app as server_app
from ccobs.server.changes import ALL, Change
from ccobs.server.config import Settings
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.models import (
    AgentModel,
    BlockModel,
    HookEventModel,
    SessionModel,
    SessionStateModel,
)
from ccobs.server.db.repository import Applied, HookEventRow, Repository, hook_event_row
from ccobs.server.ingest import DROP_REASONS, HookQueue, RawHook
from ccobs.server.live import ChangeHub
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.writer import Writer, WriterCounts, WriterObservers

# The app without its metrics listener (only tests/server/test_metrics.py opens sockets)
# and without its sweeper (tests/server/test_sweeps.py covers it).
create_app: Final = functools.partial(
    server_app.create_app, serve_metrics=False, sweeps=False, reconcile=False
)
HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
FIXTURES: Final = sorted(HOOK_FIXTURES.glob("*.jsonl"))
TOOLS: Final = HOOK_FIXTURES / "headless-tools-subagent.jsonl"
HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
HOUR_MS: Final = 3_600_000
STALE_AFTER_MS: Final = 12 * HOUR_MS
MIB: Final = 1024 * 1024
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
SESSION: Final = "00000000-0000-4000-8000-000000000000"
OTHER: Final = "00000000-0000-4000-8000-000000000001"
THIRD: Final = "00000000-0000-4000-8000-000000000002"
CANARY: Final = "CANARY-7f3a9c"
PRIVATE_PATH: Final = "C:\\Users\\someone-private\\projects\\secret"
TABLES: Final = (SessionModel, AgentModel, BlockModel, HookEventModel, SessionStateModel)

type Line = tuple[int, dict[str, Any]]


def fixture_lines(path: Path) -> list[Line]:
    """``(offset_ms, payload)`` for each line of a hook fixture."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        lines.append((document["offset_ms"], document["payload"]))
    return lines


# The real headless-tools-subagent session up to its first PermissionRequest (Bash, linked
# to the PreToolUse before it): one open permission block, as seq 12.
PERMISSION_PREFIX: Final = fixture_lines(TOOLS)[:12]
PERMISSION_TOOL_USE_ID: Final = "toolu_01EXAMPLE000000000000004"
PERMISSION_BLOCK_ID: Final = 12


def body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def with_session(lines: Sequence[Line], session_id: str) -> list[Line]:
    return [(offset, {**payload, "session_id": session_id}) for offset, payload in lines]


def payload(name: str, session_id: str = SESSION, **fields: Any) -> dict[str, Any]:
    return {"session_id": session_id, "hook_event_name": name, **fields}


def make_repository(path: Path) -> Repository:
    engine = create_engine_for(path, role="writer")
    upgrade_to_head(engine)
    return Repository(engine)


def dump(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, in primary-key order."""
    with engine.connect() as connection:
        return {
            model.__tablename__: [
                tuple(row)
                for row in connection.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key)
                )
            ]
            for model in TABLES
        }


def direct_replay(repo: Repository, lines: Sequence[Line]) -> dict[str, SessionState]:
    """The reference: reduce and persist one event at a time, in-process."""
    states: dict[str, SessionState] = {}
    for seq, (offset, document) in enumerate(lines, start=1):
        event = parse_hook(document)
        at = BASE + offset
        state, effects = apply(states.get(event.session_id), event, at, seq)
        row = hook_event_row(event, seq=seq, received_at_ms=at)
        repo.apply_effects(HOST, row, effects, state)
        states[event.session_id] = state
    return states


def count(engine: Engine, model: type[Any]) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(model)).scalar_one())


def event_seqs(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return list(
            connection.execute(select(HookEventModel.seq).order_by(HookEventModel.seq)).scalars()
        )


def drops(**expected: int) -> dict[str, int]:
    return {reason: expected.get(reason, 0) for reason in DROP_REASONS}


class Sleeps:
    """An injected sleep that records its durations instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FlakyRepository(Repository):
    """A repository whose writes fail on demand, as a full disk or a locked file would."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.fail_seqs: set[int] = set()
        self.fail_all = False
        self.fail_retention = False
        self.batches: list[list[int | None]] = []
        self.loads: list[str] = []
        self.open_states: dict[str, SessionState] | None = None
        # When set, each write waits for it (a writer stuck on a slow disk).
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def apply_batch(self, host: str, items: Sequence[Applied]) -> None:
        seqs = [None if item.event_row is None else item.event_row.seq for item in items]
        self.batches.append(seqs)
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(10)
        if self.fail_all or self.fail_seqs & {seq for seq in seqs if seq is not None}:
            raise OperationalError(
                "INSERT INTO hook_events (cwd) VALUES (?)",
                None,
                Exception(f"database or disk is full {PRIVATE_PATH}"),
            )
        super().apply_batch(host, items)

    def delete_older_than(self, cutoff_ms: int) -> frozenset[str]:
        if self.fail_retention:
            raise OperationalError("DELETE FROM sessions", None, Exception(PRIVATE_PATH))
        return super().delete_older_than(cutoff_ms)

    def load_open_states(self) -> dict[str, SessionState]:
        if self.open_states is not None:
            return self.open_states
        return super().load_open_states()

    def load_state(self, session_id: str) -> SessionState | None:
        self.loads.append(session_id)
        return super().load_state(session_id)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[FlakyRepository]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    yield FlakyRepository(engine)
    engine.dispose()


@pytest.fixture
def queue() -> HookQueue:
    return HookQueue(max_items=10_000, max_bytes=64 * MIB)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_writer(repo: FlakyRepository, queue: HookQueue, sleeps: Sleeps) -> Callable[..., Writer]:
    def make(**kwargs: Any) -> Writer:
        options: dict[str, Any] = {
            "stale_after_ms": STALE_AFTER_MS,
            "batch_wait_s": 0.0,
            "sleep": sleeps,
            **kwargs,
        }
        return Writer(queue, repo, HOST, **options)

    return make


class Feeder:
    """Offers hooks to the queue with increasing seq values, as the hook route does."""

    def __init__(self, queue: HookQueue, first_seq: int = 1) -> None:
        self.queue = queue
        self.seq = first_seq - 1

    def raw(self, raw_body: bytes, at: int) -> RawHook:
        self.seq += 1
        item = RawHook(raw_body, at, self.seq)
        assert self.queue.offer(item)
        return item

    def __call__(self, document: dict[str, Any], at: int = BASE) -> RawHook:
        return self.raw(body(document), at)

    def lines(self, lines: Sequence[Line]) -> None:
        for offset, document in lines:
            self(document, BASE + offset)


@pytest.fixture
def feed(queue: HookQueue) -> Feeder:
    return Feeder(queue)


def run_all(writer: Writer) -> None:
    """Run the writer synchronously until both of its inputs are empty."""
    while writer.run_once(0.0):
        pass


def wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


# The app, end to end.


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


def http_replay(settings: Settings, lines: Sequence[Line]) -> HookQueue:
    """POST each payload to a fresh app, with the receive time the fixture recorded, then
    stop the app, which drains the queue."""
    queue = HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    times = iter([BASE + offset for offset, _ in lines])
    app = create_app(settings, queue=queue, clock=lambda: next(times))
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        assert isinstance(app.state.writer, Writer)
        assert app.state.writer.is_alive()
        for _, document in lines:
            response = client.post(HOOK_PATH, content=body(document), headers=JSON)
            assert response.status_code == 204
    assert not app.state.writer.is_alive()
    return queue


@contextmanager
def database(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_for(settings.database_path, role="writer")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_an_http_replay_stores_what_a_direct_replay_does(
    settings: Settings, tmp_path: Path, path: Path
) -> None:
    lines = fixture_lines(path)

    queue = http_replay(settings, lines)

    reference = make_repository(tmp_path / "reference.db")
    direct_replay(reference, lines)
    with database(settings) as engine:
        assert dump(engine) == dump(reference.engine)
        assert count(engine, HookEventModel) == len(lines)
    reference.engine.dispose()
    assert queue.drops == drops()
    assert (queue.depth, queue.bytes) == (0, 0)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_a_restart_mid_session_stores_what_an_uninterrupted_run_does(
    settings: Settings, tmp_path: Path, path: Path
) -> None:
    lines = fixture_lines(path)
    half = len(lines) // 2

    http_replay(settings, lines[:half])
    http_replay(settings, lines[half:])

    reference = make_repository(tmp_path / "reference.db")
    direct_replay(reference, lines)
    with database(settings) as engine:
        assert dump(engine) == dump(reference.engine)
        # seq continued after the restart: 1..n with no gap and no repeat.
        assert event_seqs(engine) == list(range(1, len(lines) + 1))
    reference.engine.dispose()


def test_the_lifespan_stops_the_writer_before_disposing_the_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    original_dispose = Engine.dispose
    original_stop = Writer.stop

    def dispose(self: Engine, close: bool = True) -> None:
        order.append("dispose")
        original_dispose(self, close)

    def stop(self: Writer, timeout_s: float) -> None:
        order.append("stop")
        original_stop(self, timeout_s)

    monkeypatch.setattr(Engine, "dispose", dispose)
    monkeypatch.setattr(Writer, "stop", stop)
    app = create_app(settings)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        # The migrations dispose an engine of their own at start-up.
        started = list(order)
        assert "stop" not in started

    # Both engines, the readers' and the writer's (ADR 0033), once the writer is stopped.
    assert order == [*started, "stop", "dispose", "dispose"]


def test_the_lifespan_uses_an_injected_writer_factory(settings: Settings) -> None:
    calls: list[tuple[str, object]] = []
    threads: dict[str, threading.Thread] = {}

    class Recorder:
        def start(self) -> None:
            calls.append(("start", None))
            threads["start"] = threading.current_thread()

        def stop(self, timeout_s: float) -> None:
            calls.append(("stop", timeout_s))
            threads["stop"] = threading.current_thread()

        def submit_tick(self, now_ms: int) -> None:
            calls.append(("tick", now_ms))

        def submit_retention(self, cutoff_ms: int) -> None:
            calls.append(("retention", cutoff_ms))

        def submit_alert_tick(self, now_ms: int) -> None:
            calls.append(("alert tick", now_ms))

        def submit_telemetry_close(self, session_id: str, tool_use_id: str, at_ms: int) -> None:
            calls.append(("close", at_ms))

        def open_permission_blocks(self) -> list[OpenBlock]:
            return []

        def counts(self) -> WriterCounts:
            return WriterCounts(
                sessions_by_status={},
                open_blocks_by_kind={},
                running_agents=0,
                events_processed=0,
                sessions_ended=0,
            )

    recorder = Recorder()

    def factory(queue: HookQueue, repository: Repository, observers: WriterObservers) -> Recorder:
        calls.append(("factory", repository.max_seq()))
        return recorder

    app = create_app(settings, writer_factory=factory)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert app.state.writer is recorder
        assert app.state.writer.counts().sessions_ended == 0
        assert [name for name, _ in calls] == ["factory", "start"]

    assert [name for name, _ in calls] == ["factory", "start", "stop"]
    assert isinstance(calls[-1][1], float)
    assert calls[-1][1] > 0
    # The lifespan runs on the event loop's thread; the blocking stop runs off it.
    assert threads["stop"] is not threads["start"]


def test_the_default_writer_follows_the_settings(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"session_stale_after": timedelta(hours=2)}))
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        writer: Writer = app.state.writer
        assert writer.host == HOST
        assert writer.stale_after_ms == 2 * HOUR_MS


# Parsing: what the writer cannot use is counted and skipped.


def test_poison_hooks_are_counted_invalid_and_the_writer_goes_on(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: Repository
) -> None:
    writer = make_writer()
    feed.raw(b"{not json", BASE)
    feed.raw(b"[1, 2]", BASE)
    feed.raw(b'"a string"', BASE)
    feed.raw(body({"hook_event_name": "Stop"}), BASE)  # no session_id
    feed.raw(b"\xff\xfe\x00", BASE)  # not UTF-8
    feed.raw(b"[" * 100_000, BASE)  # nested too deep for the parser
    feed.raw(b"", BASE)
    good = feed(payload("UserPromptSubmit", prompt="hi"), BASE + 1)

    assert writer.run_once(0.0) == 8

    assert queue.drops == drops(invalid=7)
    assert event_seqs(repo.engine) == [good.seq]
    assert (queue.depth, queue.bytes) == (0, 0)
    state = writer.state(SESSION)
    assert state is not None
    assert state.session.status == "working"


def test_a_parser_bug_costs_only_its_hook(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    real_parse_hook = parse_hook

    def buggy(obj: object) -> Any:
        if isinstance(obj, dict) and obj.get("prompt") == CANARY:
            raise TypeError(CANARY)
        return real_parse_hook(obj)

    monkeypatch.setattr("ccobs.server.writer.parse_hook", buggy)
    writer = make_writer()
    first = feed(payload("UserPromptSubmit"), BASE)
    feed(payload("UserPromptSubmit", prompt=CANARY), BASE + 1)
    last = feed(payload("Stop"), BASE + 2)

    assert writer.run_once(0.0) == 3

    assert queue.drops == drops(internal=1)
    assert event_seqs(repo.engine) == [first.seq, last.seq]
    assert sleeps.calls == []
    assert "TypeError" in caplog.text
    assert CANARY not in caplog.text
    assert queue.depth == 0


def test_a_failed_session_load_is_counted_internal_and_backs_off(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = make_writer()

    def broken(session_id: str) -> SessionState | None:
        raise OperationalError("SELECT * FROM sessions", None, Exception(PRIVATE_PATH))

    monkeypatch.setattr(repo, "load_state", broken)
    feed(payload("UserPromptSubmit"), BASE)
    writer.submit_telemetry_close(OTHER, PERMISSION_TOOL_USE_ID, BASE)

    writer.run_once(0.0)

    assert queue.drops == drops(internal=1)
    assert count(repo.engine, HookEventModel) == 0
    assert sleeps.calls == [0.1]
    assert "could not load a session (OperationalError)" in caplog.text
    assert "TelemetryClose skipped: could not load a session" in caplog.text
    assert PRIVATE_PATH not in caplog.text
    assert "SELECT" not in caplog.text

    monkeypatch.undo()
    recovered = feed(payload("UserPromptSubmit"), BASE + 1)
    writer.run_once(0.0)
    assert event_seqs(repo.engine) == [recovered.seq]
    assert sleeps.calls == [0.1]


def test_sessions_ended_counts_each_move_to_ended_once_committed(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer = make_writer()
    feed(payload("UserPromptSubmit"), BASE)
    feed(payload("SessionEnd", reason="other"), BASE + 1)
    feed(payload("SessionEnd", reason="other"), BASE + 2)  # already ended: not again
    feed(payload("SessionEnd", OTHER, reason="other"), BASE + 3)  # a new session's first
    run_all(writer)
    assert writer.counts().sessions_ended == 2

    feed(payload("UserPromptSubmit"), BASE + 4)  # reopens SESSION
    run_all(writer)
    repo.fail_all = True
    feed(payload("SessionEnd", reason="other"), BASE + 5)  # not persisted: not counted
    run_all(writer)
    assert writer.counts().sessions_ended == 2

    repo.fail_all = False
    feed(payload("SessionEnd", reason="other"), BASE + 6)
    run_all(writer)
    assert writer.counts().sessions_ended == 3


def test_a_batch_of_only_poison_writes_nothing(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer()
    feed.raw(b"nope", BASE)

    writer.run_once(0.0)

    assert repo.batches == []
    assert queue.drops == drops(invalid=1)


def test_an_already_applied_seq_writes_nothing(
    make_writer: Callable[..., Writer], queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer()
    late = Feeder(queue, first_seq=5)
    late(payload("UserPromptSubmit"))
    run_all(writer)
    early = Feeder(queue, first_seq=3)
    early(payload("Stop"))  # seq 3, below the session's last_seq of 5

    run_all(writer)

    assert event_seqs(repo.engine) == [5]
    assert repo.batches == [[5]]
    assert queue.drops == drops()
    assert queue.depth == 0


def test_a_session_mismatch_is_counted_internal(
    make_writer: Callable[..., Writer], queue: HookQueue, repo: FlakyRepository
) -> None:
    # A corrupted in-memory state: SESSION's slot holds OTHER's state.
    other, _ = apply(None, parse_hook(payload("UserPromptSubmit", OTHER)), BASE, 1)
    repo.open_states = {SESSION: other}
    writer = make_writer()
    feed = Feeder(queue, first_seq=2)
    feed(payload("Stop", SESSION))
    third = feed(payload("UserPromptSubmit", THIRD))

    run_all(writer)

    assert queue.drops == drops(internal=1)
    assert event_seqs(repo.engine) == [third.seq]
    assert queue.depth == 0


# Micro-batches.


def test_one_batch_is_one_write(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer = make_writer()
    feed.lines(PERMISSION_PREFIX)

    assert writer.run_once(0.0) == len(PERMISSION_PREFIX)

    assert repo.batches == [list(range(1, len(PERMISSION_PREFIX) + 1))]


def test_batches_hold_at_most_batch_max_items(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer = make_writer(batch_max=5)
    feed.lines(PERMISSION_PREFIX)

    run_all(writer)

    assert [len(batch) for batch in repo.batches] == [5, 5, 2]


def test_a_batch_waits_for_more_items_up_to_batch_wait(
    repo: FlakyRepository, queue: HookQueue, feed: Feeder
) -> None:
    # A wide wait, cut short by batch_max: the second hook, 50 ms later, fills the batch.
    writer = Writer(
        queue, repo, HOST, stale_after_ms=STALE_AFTER_MS, batch_wait_s=30.0, batch_max=2
    )
    feed(payload("UserPromptSubmit"))
    later = threading.Timer(0.05, lambda: feed(payload("Stop")))
    later.start()

    started = time.monotonic()
    assert writer.run_once(0.0) == 2
    elapsed = time.monotonic() - started
    later.join()

    assert repo.batches == [[1, 2]]
    assert elapsed < 15.0


def test_run_once_returns_zero_when_idle(make_writer: Callable[..., Writer]) -> None:
    writer = make_writer()

    assert writer.run_once(0.0) == 0


def test_a_failing_item_is_dropped_and_the_rest_of_its_batch_persists(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
) -> None:
    writer = make_writer()
    feed.lines(PERMISSION_PREFIX[:5])
    repo.fail_seqs = {3}

    run_all(writer)

    # The batch failed whole, then each item was retried on its own.
    assert repo.batches == [[1, 2, 3, 4, 5], [1], [2], [3], [4], [5]]
    assert event_seqs(repo.engine) == [1, 2, 4, 5]
    assert queue.drops == drops(internal=1)
    assert writer.state(PERMISSION_PREFIX[0][1]["session_id"]) == repo.load_state(
        PERMISSION_PREFIX[0][1]["session_id"]
    )
    assert sleeps.calls == []  # some items persisted: no back-off
    assert queue.depth == 0


def test_memory_rolls_back_to_the_last_committed_state(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer()
    feed(payload("UserPromptSubmit"))
    run_all(writer)
    committed = writer.state(SESSION)
    repo.fail_all = True
    feed(payload("Stop"))

    run_all(writer)

    assert writer.state(SESSION) == committed == repo.load_state(SESSION)
    repo.fail_all = False
    feed(payload("Stop"))
    run_all(writer)
    state = writer.state(SESSION)
    assert state is not None
    assert state.session.status == "idle"
    assert state == repo.load_state(SESSION)


def test_a_persistent_failure_backs_off_and_recovers(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
) -> None:
    writer = make_writer(backoff_initial_s=0.5, backoff_max_s=3.0)
    repo.fail_all = True
    for attempt in range(5):
        feed(payload("UserPromptSubmit"), BASE + attempt)
        assert writer.run_once(0.0) == 1

    assert sleeps.calls == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert queue.drops == drops(internal=5)
    assert count(repo.engine, HookEventModel) == 0

    repo.fail_all = False
    recovered = feed(payload("UserPromptSubmit"), BASE + 10)
    writer.run_once(0.0)
    assert event_seqs(repo.engine) == [recovered.seq]
    assert sleeps.calls == [0.5, 1.0, 2.0, 3.0, 3.0]  # no sleep after a success

    # The back-off starts over after a success.
    repo.fail_all = True
    feed(payload("Stop"), BASE + 11)
    writer.run_once(0.0)
    assert sleeps.calls[-1] == 0.5


def test_the_default_back_off_waits_on_the_stop_event(
    repo: FlakyRepository, queue: HookQueue, feed: Feeder
) -> None:
    writer = Writer(
        queue, repo, HOST, stale_after_ms=STALE_AFTER_MS, batch_wait_s=0.0, backoff_initial_s=30.0
    )
    repo.fail_all = True
    writer.start()
    feed(payload("UserPromptSubmit"))
    wait_until(lambda: queue.drops["internal"] == 1)

    started = time.monotonic()
    writer.stop(5.0)

    # The 30 s back-off ended as soon as the writer was asked to stop.
    assert time.monotonic() - started < 5.0
    assert not writer.is_alive()


def test_an_unexpected_error_never_kills_the_writer(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = make_writer()
    calls = 0

    def explode(*args: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"boom {CANARY}")

    monkeypatch.setattr(writer, "_commit", explode)
    feed.raw(b"not json", BASE)  # counted invalid before the error
    feed(payload("UserPromptSubmit"))
    writer.run_once(0.0)

    # The hook the error lost is counted once; the invalid one is not counted twice.
    assert calls == 1
    assert queue.drops == drops(invalid=1, internal=1)
    assert queue.depth == 0
    assert sleeps.calls == [0.1]

    monkeypatch.undo()
    feed(payload("Stop"))
    writer.run_once(0.0)
    assert queue.drops == drops(invalid=1, internal=1)
    assert len(event_seqs(repo.engine)) == 2  # the lost hook's write had gone through


def test_an_ended_session_leaves_memory_and_reopens_from_the_database(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer = make_writer()
    feed(payload("UserPromptSubmit"), BASE)
    feed(payload("SessionEnd", reason="other"), BASE + 1)
    run_all(writer)
    assert writer.state(SESSION) is None
    assert writer.counts().sessions_by_status == {}
    assert repo.loads == [SESSION]  # once, for the first event of an unknown session

    feed(payload("UserPromptSubmit"), BASE + 2)
    feed(payload("Stop"), BASE + 3)
    run_all(writer)

    state = writer.state(SESSION)
    assert state is not None
    assert state.session.status == "idle"
    assert state.session.started_at_ms == BASE  # the same session, reopened
    assert repo.loads == [SESSION, SESSION]


def test_the_writer_starts_from_the_open_states_in_the_database(
    repo: FlakyRepository, queue: HookQueue, make_writer: Callable[..., Writer]
) -> None:
    states = direct_replay(repo, PERMISSION_PREFIX)
    (session_id,) = states

    writer = make_writer()

    assert writer.state(session_id) == states[session_id]
    assert writer.counts().open_blocks_by_kind == {"permission": 1}
    assert repo.loads == []


# The sweep, the telemetry join and retention.


def _blocked(make_writer: Callable[..., Writer], feed: Feeder) -> tuple[Writer, str]:
    writer = make_writer()
    feed.lines(PERMISSION_PREFIX)
    run_all(writer)
    session_id = PERMISSION_PREFIX[0][1]["session_id"]
    state = writer.state(session_id)
    assert state is not None
    assert state.session.status == "waiting_user"
    return writer, session_id


def test_a_tick_marks_silent_sessions_stale_and_persists_it(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer, session_id = _blocked(make_writer, feed)
    last = BASE + PERMISSION_PREFIX[-1][0]
    batches = len(repo.batches)

    writer.submit_tick(last + STALE_AFTER_MS - 1)  # not silent long enough
    run_all(writer)
    assert len(repo.batches) == batches
    state = writer.state(session_id)
    assert state is not None
    assert state.session.status == "waiting_user"

    writer.submit_tick(last + STALE_AFTER_MS)
    run_all(writer)

    assert repo.batches[-1] == [None]  # no hook event row
    detail = repo.get_session(session_id)
    assert detail is not None
    assert detail.session.status == "stale"
    (block,) = [b for b in detail.blocks if b.block_id == PERMISSION_BLOCK_ID]
    assert (block.close_reason, block.close_source) == ("stale", "sweep")
    assert block.closed_at_ms == last + STALE_AFTER_MS
    assert writer.state(session_id) == repo.load_state(session_id)
    assert writer.counts().sessions_by_status == {"stale": 1}


def test_a_telemetry_close_closes_the_block_as_denied(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer, session_id = _blocked(make_writer, feed)
    at = BASE + PERMISSION_PREFIX[-1][0] + 500
    batches = len(repo.batches)

    writer.submit_telemetry_close(session_id, "toolu_no_such_call", at)  # no match
    writer.submit_telemetry_close(OTHER, PERMISSION_TOOL_USE_ID, at)  # unknown session
    run_all(writer)
    assert len(repo.batches) == batches

    writer.submit_telemetry_close(session_id, PERMISSION_TOOL_USE_ID, at)
    run_all(writer)

    detail = repo.get_session(session_id)
    assert detail is not None
    (block,) = [b for b in detail.blocks if b.block_id == PERMISSION_BLOCK_ID]
    assert (block.close_reason, block.close_source, block.closed_at_ms) == (
        "denied",
        "telemetry",
        at,
    )
    assert detail.session.status == "working"
    assert writer.state(session_id) == repo.load_state(session_id)


def test_retention_deletes_old_closed_sessions_and_forgets_them(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer, session_id = _blocked(make_writer, feed)
    last = BASE + PERMISSION_PREFIX[-1][0]
    feed(payload("UserPromptSubmit", OTHER), last + STALE_AFTER_MS)  # open: kept
    writer.submit_tick(last + STALE_AFTER_MS)
    run_all(writer)

    writer.submit_retention(last + 1)
    run_all(writer)

    assert repo.get_session(session_id) is None
    assert writer.state(session_id) is None
    assert repo.get_session(OTHER) is not None
    assert writer.state(OTHER) is not None

    later = last + 2 * STALE_AFTER_MS
    feed(PERMISSION_PREFIX[2][1], later)  # the same session_id starts afresh
    run_all(writer)
    state = writer.state(session_id)
    assert state is not None
    assert state.session.started_at_ms == later
    assert state.session.status == "working"


def test_retention_runs_after_the_writes_queued_before_it(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer, session_id = _blocked(make_writer, feed)
    last = BASE + PERMISSION_PREFIX[-1][0]
    writer.submit_tick(last + STALE_AFTER_MS)
    writer.submit_retention(last + 1)
    writer.submit_tick(last + 2 * STALE_AFTER_MS)

    run_all(writer)

    assert repo.get_session(session_id) is None
    assert writer.state(session_id) is None


def test_a_failed_retention_is_logged_and_the_writer_goes_on(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: FlakyRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, session_id = _blocked(make_writer, feed)
    last = BASE + PERMISSION_PREFIX[-1][0]
    writer.submit_tick(last + STALE_AFTER_MS)
    run_all(writer)
    repo.fail_retention = True

    writer.submit_retention(last + 1)
    run_all(writer)

    assert writer.state(session_id) is not None
    assert "OperationalError" in caplog.text
    repo.fail_retention = False
    writer.submit_retention(last + 1)
    run_all(writer)
    assert writer.state(session_id) is None


def test_a_step_takes_at_most_batch_max_scheduled_tasks(
    make_writer: Callable[..., Writer],
) -> None:
    writer = make_writer(batch_max=2)
    for now in range(3):
        writer.submit_tick(BASE + now)

    assert [writer.run_once(0.0) for _ in range(3)] == [2, 1, 0]


def test_a_retry_skips_what_no_longer_applies_without_the_failed_item(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    queue: HookQueue,
    repo: FlakyRepository,
    sleeps: Sleeps,
) -> None:
    writer = make_writer()
    # The tick, in the same batch, marks the session the hook creates stale; on its own,
    # once the hook has failed, it finds nothing to sweep.
    created = feed(payload("UserPromptSubmit"), BASE)
    repo.fail_seqs = {created.seq}
    writer.submit_tick(BASE + STALE_AFTER_MS)

    writer.run_once(0.0)

    assert repo.batches == [[created.seq, None], [created.seq]]
    assert queue.drops == drops(internal=1)
    assert writer.state(SESSION) is None
    assert sleeps.calls == [0.1]


def test_a_failed_tick_write_is_logged_not_counted_as_a_hook(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer, session_id = _blocked(make_writer, feed)
    repo.fail_all = True

    writer.submit_tick(BASE + PERMISSION_PREFIX[-1][0] + STALE_AFTER_MS)
    run_all(writer)

    assert queue.drops == drops()
    state = writer.state(session_id)
    assert state is not None
    assert state.session.status == "waiting_user"


# What stage 10 exposes.


def test_counts_match_the_database(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    closed: list[tuple[str, float]] = []
    writer = make_writer(on_block_closed=lambda kind, seconds: closed.append((kind, seconds)))
    fed: list[Line] = []
    for path in FIXTURES:
        fed += fixture_lines(path)
    fed += with_session(PERMISSION_PREFIX, SESSION)  # one block left open
    feed.lines(fed)
    run_all(writer)
    # Every fed line parses; registered events by name, the rest as other.
    received = Counter(event_label(document["hook_event_name"]) for _, document in fed)

    with repo.engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(SessionModel.status, func.count())
                .where(SessionModel.status != "ended")
                .group_by(SessionModel.status)
            ).all()
        )
        # Each fixture session ends once at most.
        ended = connection.execute(
            select(func.count()).select_from(SessionModel).where(SessionModel.status == "ended")
        ).scalar_one()
        blocks = dict(
            connection.execute(
                select(BlockModel.kind, func.count())
                .where(BlockModel.closed_at_ms.is_(None))
                .group_by(BlockModel.kind)
            ).all()
        )
        running = connection.execute(
            select(func.count())
            .select_from(AgentModel)
            .join(SessionModel, AgentModel.session_id == SessionModel.session_id)
            .where(
                AgentModel.status == "running",
                AgentModel.internal.is_(False),
                SessionModel.status.not_in(("ended", "stale")),
            )
        ).scalar_one()
        durations = sorted(
            (kind, (closed_at - opened_at) / 1000)
            for kind, closed_at, opened_at in connection.execute(
                select(BlockModel.kind, BlockModel.closed_at_ms, BlockModel.opened_at_ms)
            )
            if closed_at is not None
        )
    assert writer.counts() == WriterCounts(
        sessions_by_status=statuses,
        open_blocks_by_kind=blocks,
        running_agents=running,
        events_processed=count(repo.engine, HookEventModel),
        sessions_ended=ended,
        events_received=dict(received),
    )
    assert set(received) <= set(EVENT_LABELS)
    assert writer.counts().open_blocks_by_kind == {"permission": 1}
    assert sorted(closed) == durations
    assert len(closed) >= 5


def test_running_agents_ignore_internal_agents_and_stale_sessions(
    make_writer: Callable[..., Writer], feed: Feeder
) -> None:
    writer = make_writer()
    lines = fixture_lines(TOOLS)[:18]  # the Explore subagent is running
    feed.lines(lines)
    run_all(writer)
    assert writer.counts().running_agents == 1

    writer.submit_tick(BASE + lines[-1][0] + STALE_AFTER_MS)
    run_all(writer)
    assert writer.counts().running_agents == 0


def test_a_failing_block_callback_is_logged_and_ignored(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: FlakyRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")

    def callback(kind: str, seconds: float) -> None:
        raise ValueError(CANARY)

    writer = make_writer(on_block_closed=callback)
    feed.lines(fixture_lines(TOOLS))
    run_all(writer)

    assert count(repo.engine, HookEventModel) == len(fixture_lines(TOOLS))
    assert "ValueError" in caplog.text
    assert CANARY not in caplog.text


def test_counts_are_safe_to_read_from_another_thread(
    make_writer: Callable[..., Writer], feed: Feeder
) -> None:
    writer = make_writer(batch_max=1)
    lines = fixture_lines(TOOLS)
    feed.lines(lines)
    seen: list[WriterCounts] = []
    done = threading.Event()

    def read() -> None:
        while not done.is_set():
            seen.append(writer.counts())
            time.sleep(0)  # let the writer run

    reader = threading.Thread(target=read)
    reader.start()
    run_all(writer)
    done.set()
    reader.join()

    assert seen
    assert writer.counts().events_processed == len(lines)


# Live updates (ADR 0032): the writer names the sessions each commit changed.


class Commits:
    """An ``on_committed`` that records each change and, at that moment, how many hook
    events the database holds: the callback must come after the commit."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.calls: list[tuple[Change, int]] = []

    def __call__(self, change: Change) -> None:
        self.calls.append((change, count(self.engine, HookEventModel)))

    @property
    def changes(self) -> list[Change]:
        return [change for change, _ in self.calls]


def test_each_commit_names_the_sessions_it_changed_once_committed(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    commits = Commits(repo.engine)
    writer = make_writer(on_committed=commits)
    feed(payload("UserPromptSubmit"))
    feed(payload("UserPromptSubmit", OTHER))

    run_all(writer)

    # One batch, one commit, one call: both sessions, with their rows already stored.
    assert commits.calls == [(frozenset({SESSION, OTHER}), 2)]


def test_a_batch_that_writes_nothing_names_nothing(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    commits = Commits(repo.engine)
    writer = make_writer(on_committed=commits)
    feed.raw(b"not json", BASE)

    run_all(writer)

    assert commits.calls == []


def test_a_failed_batch_names_only_what_its_retries_committed(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    commits = Commits(repo.engine)
    writer = make_writer(on_committed=commits)
    feed(payload("UserPromptSubmit"))
    failing = feed(payload("UserPromptSubmit", OTHER))
    repo.fail_seqs = {failing.seq}

    run_all(writer)

    assert commits.calls == [(frozenset({SESSION}), 1)]
    repo.fail_seqs = set()
    repo.fail_all = True
    feed(payload("Stop"))
    run_all(writer)
    assert commits.calls == [(frozenset({SESSION}), 1)]


def test_ticks_and_telemetry_closes_name_their_sessions(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    commits = Commits(repo.engine)
    writer, session_id = _blocked(lambda **kwargs: make_writer(on_committed=commits), feed)
    at = BASE + PERMISSION_PREFIX[-1][0] + 500
    commits.calls.clear()

    writer.submit_telemetry_close(session_id, PERMISSION_TOOL_USE_ID, at)
    run_all(writer)
    writer.submit_tick(at + STALE_AFTER_MS)
    run_all(writer)

    assert commits.changes == [frozenset({session_id}), frozenset({session_id})]


def test_retention_that_deletes_sessions_names_every_session(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    commits = Commits(repo.engine)
    writer, _ = _blocked(lambda **kwargs: make_writer(on_committed=commits), feed)
    last = BASE + PERMISSION_PREFIX[-1][0]
    writer.submit_tick(last + STALE_AFTER_MS)
    run_all(writer)
    commits.calls.clear()

    writer.submit_retention(last - 1)  # nothing that old
    run_all(writer)
    assert commits.calls == []
    repo.fail_retention = True
    writer.submit_retention(last + 1)
    run_all(writer)
    assert commits.calls == []
    repo.fail_retention = False
    writer.submit_retention(last + 1)
    run_all(writer)

    assert commits.changes == [ALL]


def test_a_failing_commit_callback_is_logged_and_ignored(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: FlakyRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")

    def callback(change: Change) -> None:
        raise ValueError(CANARY)

    writer = make_writer(on_committed=callback)
    feed.lines(fixture_lines(TOOLS))
    run_all(writer)

    assert count(repo.engine, HookEventModel) == len(fixture_lines(TOOLS))
    assert writer.counts().events_processed == len(fixture_lines(TOOLS))
    assert "ValueError" in caplog.text
    assert CANARY not in caplog.text
    assert SESSION not in caplog.text


def test_the_writer_does_not_import_the_web_framework() -> None:
    # The writer names changes with ccobs.server.changes, which needs only the standard
    # library; the hub, which needs Starlette, lives in ccobs.server.live.
    code = (
        "import sys, ccobs.server.writer;"
        "names = ('starlette', 'fastapi', 'ccobs.server.live');"
        "print(sorted(m for m in names if m in sys.modules))"
    )
    completed = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )

    assert completed.stdout.strip() == "[]"


def test_the_app_feeds_the_hub_from_the_writer(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        hub: ChangeHub = app.state.live
        portal = client.portal
        assert portal is not None
        subscription = portal.call(hub.subscribe, None)
        assert subscription is not None
        response = client.post(HOOK_PATH, content=body(payload("UserPromptSubmit")), headers=JSON)
        assert response.status_code == 204
        wait_until(lambda: portal.call(subscription.take) == frozenset({SESSION}))


# The thread and shutdown.


def test_start_twice_is_refused(make_writer: Callable[..., Writer]) -> None:
    writer = make_writer()
    writer.start()
    try:
        with pytest.raises(RuntimeError):
            writer.start()
    finally:
        writer.stop(5.0)


def test_the_thread_persists_hooks_and_controls(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer(poll_s=0.01)
    writer.start()
    feed.lines(PERMISSION_PREFIX)
    wait_until(lambda: queue.depth == 0)
    session_id = PERMISSION_PREFIX[0][1]["session_id"]

    writer.submit_tick(BASE + PERMISSION_PREFIX[-1][0] + STALE_AFTER_MS)
    wait_until(lambda: writer.counts().sessions_by_status == {"stale": 1})
    writer.stop(5.0)

    assert not writer.is_alive()
    detail = repo.get_session(session_id)
    assert detail is not None
    assert detail.session.status == "stale"


def test_stop_drains_the_queue(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer(batch_max=4)
    writer.start()
    lines = fixture_lines(TOOLS)
    feed.lines(lines)

    writer.stop(5.0)

    assert count(repo.engine, HookEventModel) == len(lines)
    assert queue.drops == drops()
    assert (queue.depth, queue.bytes) == (0, 0)


def test_stop_without_the_thread_drains_synchronously(
    make_writer: Callable[..., Writer], feed: Feeder, queue: HookQueue, repo: FlakyRepository
) -> None:
    writer = make_writer(batch_max=2)
    lines = fixture_lines(TOOLS)
    feed.lines(lines)

    writer.stop(5.0)

    assert count(repo.engine, HookEventModel) == len(lines)
    assert queue.depth == 0


def test_stop_counts_what_it_could_not_drain_in_time(
    repo: FlakyRepository, queue: HookQueue, feed: Feeder, make_writer: Callable[..., Writer]
) -> None:
    release = threading.Event()
    repo.gate = release
    writer = make_writer(batch_max=1)
    writer.start()
    for index in range(5):
        feed(payload("UserPromptSubmit"), BASE + index)
    writer.submit_tick(BASE + STALE_AFTER_MS)
    assert repo.entered.wait(5)

    writer.stop(0.05)

    assert queue.drops == drops(internal=4)
    release.set()
    wait_until(lambda: not writer.is_alive())
    assert event_seqs(repo.engine) == [1]
    assert (queue.depth, queue.bytes) == (0, 0)


def test_stop_drops_what_arrives_past_the_deadline(
    repo: FlakyRepository, queue: HookQueue, feed: Feeder, sleeps: Sleeps
) -> None:
    now = [0.0]

    def monotonic() -> float:
        now[0] += 1.0
        return now[0]

    writer = Writer(
        queue,
        repo,
        HOST,
        stale_after_ms=STALE_AFTER_MS,
        batch_wait_s=0.0,
        batch_max=1,
        monotonic=monotonic,
        sleep=sleeps,
    )
    for index in range(3):
        feed(payload("UserPromptSubmit"), BASE + index)

    writer.stop(2.5)  # the fake clock runs out after one batch

    assert len(event_seqs(repo.engine)) == 1
    assert queue.drops == drops(internal=2)
    assert queue.depth == 0


# Logs.


def test_logs_never_hold_content_paths_or_session_ids(
    make_writer: Callable[..., Writer],
    queue: HookQueue,
    repo: FlakyRepository,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = payload("UserPromptSubmit", prompt=CANARY, cwd=PRIVATE_PATH)
    # Mismatch: SESSION's slot holds OTHER's state.
    other, _ = apply(None, parse_hook(payload("UserPromptSubmit", OTHER)), BASE, 1)
    repo.open_states = {SESSION: other}
    writer = make_writer()
    feed = Feeder(queue, first_seq=2)
    feed.raw(f'{{"prompt": "{CANARY}", "cwd": "{PRIVATE_PATH}"'.encode(), BASE)
    feed.raw(body({"prompt": CANARY, "cwd": PRIVATE_PATH}), BASE)
    feed(secret)
    feed.lines(fixture_lines(TOOLS))
    repo.fail_seqs = {feed.seq}
    run_all(writer)
    repo.fail_all = True
    feed(payload("Stop", THIRD, cwd=PRIVATE_PATH))
    run_all(writer)
    repo.fail_all = False
    repo.fail_retention = True
    writer.submit_retention(BASE)
    run_all(writer)

    def explode(*args: object) -> None:
        raise RuntimeError(f"{CANARY} {PRIVATE_PATH} {SESSION}")

    monkeypatch.setattr(writer, "_commit", explode)
    feed(payload("UserPromptSubmit", THIRD, prompt=CANARY))
    run_all(writer)
    monkeypatch.undo()
    feed(payload("UserPromptSubmit", THIRD, prompt=CANARY))
    writer.stop(0.0)

    assert queue.drops["invalid"] == 2
    assert queue.drops["internal"] >= 4
    text = caplog.text
    assert caplog.records
    session_ids = {SESSION, OTHER, THIRD, PERMISSION_PREFIX[0][1]["session_id"]}
    for forbidden in (CANARY, PRIVATE_PATH, "\\Users\\", "someone-private", "INSERT", "DELETE"):
        assert forbidden not in text
    for session_id in session_ids:
        assert session_id not in text
    assert all(record.exc_info is None for record in caplog.records)


def test_hook_event_rows_are_what_the_writer_stores(
    make_writer: Callable[..., Writer], feed: Feeder, repo: FlakyRepository
) -> None:
    writer = make_writer()
    lines = fixture_lines(TOOLS)
    feed.lines(lines)
    run_all(writer)

    session_id = lines[0][1]["session_id"]
    stored = repo.list_events(session_id, limit=1000)
    expected: list[HookEventRow] = [
        hook_event_row(parse_hook(document), seq=seq, received_at_ms=BASE + offset)
        for seq, (offset, document) in enumerate(lines, start=1)
    ]
    assert stored == expected
