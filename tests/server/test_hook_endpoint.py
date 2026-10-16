"""The hook route and its queue (ADRs 0011 and 0027).

``POST /hooks`` validates the request, stamps it with ``received_at_ms`` and ``seq`` and
offers the raw body to a bounded queue; the writer thread parses and persists it
(tests/server/test_writer.py). Here the app runs with an idle writer and the tests take
the queue's items themselves. Whatever happens, the route answers
an empty 204 with no content type: any other answer would show up as a hook error in the
user's transcript (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import statistics
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import apply
from ccobs.server import app as server_app
from ccobs.server.config import Settings
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import Repository, hook_event_row
from ccobs.server.ingest import (
    DROP_REASONS,
    HookQueue,
    RawHook,
    SeqAllocator,
    wall_clock_ms,
)
from ccobs.server.migrations import HEAD, upgrade_to_head
from ccobs.server.writer import BlockClosedCallback, WriterCounts

# The app without its metrics listener: only tests/server/test_metrics.py opens sockets.
create_app: Final = functools.partial(server_app.create_app, serve_metrics=False)
KIB: Final = 1024
MIB: Final = 1024 * KIB
BODY_MAX: Final = 64 * KIB
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "hooks" / "headless-tools-subagent.jsonl"
# Real payloads, as Claude Code posts them (one JSON object per request).
BODIES: Final = [
    json.dumps(json.loads(line)["payload"]).encode()
    for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
CANARY: Final = "CANARY-7f3a9c"
PRIVATE_PATH: Final = "C:\\Users\\someone-private\\projects\\secret"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", hook_body_max_bytes=BODY_MAX)


class IdleWriter:
    """A writer that never takes from the queue, so the tests can inspect it."""

    def start(self) -> None:
        pass

    def stop(self, timeout_s: float) -> None:
        pass

    def counts(self) -> WriterCounts:
        return WriterCounts(
            sessions_by_status={},
            open_blocks_by_kind={},
            running_agents=0,
            events_processed=0,
            sessions_ended=0,
        )


def idle_writer(
    queue: HookQueue, repository: Repository, on_block_closed: BlockClosedCallback
) -> IdleWriter:
    return IdleWriter()


@pytest.fixture
def queue(settings: Settings) -> HookQueue:
    return HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)


@pytest.fixture
def app(settings: Settings, queue: HookQueue) -> FastAPI:
    return create_app(settings, writer_factory=idle_writer, queue=queue)


def start(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"http://{GOOD_HOST}")


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with start(app) as test_client:
        yield test_client


def drain(queue: HookQueue) -> list[RawHook]:
    """The writer's test double: take everything the queue holds."""
    items = []
    while (item := queue.get(timeout=0)) is not None:
        items.append(item)
        queue.task_done(item)
    return items


def assert_neutral(response: httpx.Response) -> None:
    assert response.status_code == 204
    assert response.content == b""
    assert "content-type" not in response.headers
    assert [name for name in response.headers if name.startswith("access-control-")] == []


def only_drop(queue: HookQueue, reason: str) -> None:
    assert queue.drops == {name: int(name == reason) for name in DROP_REASONS}


def no_drops(queue: HookQueue) -> None:
    assert queue.drops == dict.fromkeys(DROP_REASONS, 0)


async def _exchange(
    app: FastAPI, headers: Sequence[tuple[bytes, bytes]], messages: list[Message]
) -> tuple[int, bytes, int]:
    calls = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": HOOK_PATH,
        "raw_path": HOOK_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", GOOD_HOST.encode()), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4300),
    }
    await app(scope, receive, send)
    status = sent[0]["status"]
    assert sent[0].get("headers", []) == []
    return status, b"".join(m.get("body", b"") for m in sent[1:]), calls


def raw_post(
    app: FastAPI,
    chunks: Sequence[bytes],
    headers: Sequence[tuple[bytes, bytes]] = ((b"content-type", b"application/json"),),
) -> tuple[int, bytes, int]:
    """POST through the ASGI interface directly: ``(status, body, receive calls)``."""
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    return asyncio.run(_exchange(app, headers, messages))


# HookQueue.


def hook(body: bytes = b"{}", seq: int = 1) -> RawHook:
    return RawHook(body=body, received_at_ms=1, seq=seq)


def test_raw_hook_is_frozen() -> None:
    item = hook()
    attribute = "seq"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(item, attribute, 2)


def test_queue_offers_and_gets_in_order() -> None:
    queue = HookQueue(max_items=10, max_bytes=MIB)

    assert queue.offer(hook(b"ab", 1))
    assert queue.offer(hook(b"cde", 2))
    assert (queue.depth, queue.bytes) == (2, 5)

    first = queue.get(timeout=0)
    assert first == hook(b"ab", 1)
    # Held until the writer is done with it.
    assert (queue.depth, queue.bytes) == (2, 5)
    queue.task_done(first)
    assert (queue.depth, queue.bytes) == (1, 3)
    second = queue.get(timeout=0)
    assert second == hook(b"cde", 2)
    queue.task_done(second)
    assert (queue.depth, queue.bytes) == (0, 0)


def test_queue_get_times_out_with_none() -> None:
    queue = HookQueue(max_items=1, max_bytes=MIB)
    started = time.monotonic()

    assert queue.get(timeout=0.05) is None
    assert time.monotonic() - started >= 0.04


def test_queue_get_waits_for_an_offer() -> None:
    queue = HookQueue(max_items=1, max_bytes=MIB)
    timer = threading.Timer(0.05, queue.offer, args=(hook(),))
    timer.start()
    try:
        assert queue.get(timeout=5) == hook()
    finally:
        timer.join()


def test_queue_is_bounded_by_count() -> None:
    queue = HookQueue(max_items=2, max_bytes=MIB)

    assert queue.offer(hook(seq=1))
    assert queue.offer(hook(seq=2))
    assert not queue.offer(hook(seq=3))
    assert queue.depth == 2
    only_drop(queue, "full")


def test_queue_is_bounded_by_bytes() -> None:
    queue = HookQueue(max_items=10, max_bytes=10)

    assert queue.offer(hook(b"x" * 6, 1))
    assert not queue.offer(hook(b"x" * 5, 2))
    assert queue.offer(hook(b"x" * 4, 3))
    assert queue.bytes == 10
    only_drop(queue, "full")


def test_queue_frees_room_on_task_done() -> None:
    queue = HookQueue(max_items=1, max_bytes=MIB)
    queue.offer(hook(seq=1))
    item = queue.get(timeout=0)
    assert item is not None

    assert not queue.offer(hook(seq=2))
    queue.task_done(item)
    assert queue.offer(hook(seq=3))


def test_queue_refuses_bad_bounds() -> None:
    with pytest.raises(ValueError, match="max_items"):
        HookQueue(max_items=0, max_bytes=1)
    with pytest.raises(ValueError, match="max_bytes"):
        HookQueue(max_items=1, max_bytes=0)


def test_queue_counts_drops_by_reason() -> None:
    queue = HookQueue(max_items=1, max_bytes=MIB)
    no_drops(queue)

    for reason in DROP_REASONS:
        queue.count_drop(reason)
    queue.count_drop("internal")

    assert queue.drops == {**dict.fromkeys(DROP_REASONS, 1), "internal": 2}
    # A snapshot of plain ints, not a live view.
    snapshot = queue.drops
    queue.count_drop("full")
    assert snapshot["full"] == 1
    assert all(type(value) is int for value in queue.drops.values())


def test_drop_reasons() -> None:
    assert DROP_REASONS == (
        "full",
        "too_large",
        "bad_content_type",
        "bad_host",
        "bad_request",
        "invalid",
        "internal",
    )


def test_queue_is_thread_safe() -> None:
    queue = HookQueue(max_items=500, max_bytes=MIB)
    accepted: list[bool] = []
    lock = threading.Lock()

    def offer_many() -> None:
        results = [queue.offer(hook(b"x" * 10)) for _ in range(200)]
        with lock:
            accepted.extend(results)

    threads = [threading.Thread(target=offer_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert accepted.count(True) == 500
    assert queue.drops["full"] == 1600 - 500
    assert (queue.depth, queue.bytes) == (500, 5000)
    assert len(drain(queue)) == 500
    assert (queue.depth, queue.bytes) == (0, 0)


# SeqAllocator and the clock.


def test_seq_allocator_counts_from_its_start() -> None:
    seq = SeqAllocator(42)

    assert [seq.next() for _ in range(3)] == [42, 43, 44]


def test_seq_allocator_refuses_a_start_below_one() -> None:
    with pytest.raises(ValueError, match="seq"):
        SeqAllocator(0)


def test_seq_allocator_is_thread_safe() -> None:
    seq = SeqAllocator(1)
    seen: list[int] = []
    lock = threading.Lock()

    def take() -> None:
        values = [seq.next() for _ in range(1000)]
        with lock:
            seen.extend(values)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(seen) == list(range(1, 8001))


def test_wall_clock_is_utc_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    now = wall_clock_ms()
    after = time.time_ns() // 1_000_000

    assert before <= now <= after


# The neutral answer, for every request.


def test_a_valid_hook_is_enqueued(client: TestClient, queue: HookQueue) -> None:
    response = client.post(HOOK_PATH, content=BODIES[0], headers=JSON)

    assert_neutral(response)
    (item,) = drain(queue)
    assert item.body == BODIES[0]
    no_drops(queue)


def test_invalid_json_is_still_enqueued(client: TestClient, queue: HookQueue) -> None:
    # The writer parses it (and counts it invalid); the route stays O(1).
    response = client.post(HOOK_PATH, content=b"{not json", headers=JSON)

    assert_neutral(response)
    assert [item.body for item in drain(queue)] == [b"{not json"]
    no_drops(queue)


def test_an_empty_body_is_still_enqueued(client: TestClient, queue: HookQueue) -> None:
    assert_neutral(client.post(HOOK_PATH, content=b"", headers=JSON))

    assert [item.body for item in drain(queue)] == [b""]


@pytest.mark.parametrize(
    "content_type",
    ["application/json; charset=utf-8", "Application/JSON", "application/json;charset=UTF-8"],
)
def test_json_with_parameters_or_any_case_is_accepted(
    client: TestClient, queue: HookQueue, content_type: str
) -> None:
    response = client.post(HOOK_PATH, content=b"{}", headers={"content-type": content_type})

    assert_neutral(response)
    assert queue.depth == 1


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""],
)
def test_another_content_type_is_dropped(
    client: TestClient, queue: HookQueue, content_type: str
) -> None:
    response = client.post(HOOK_PATH, content=b"{}", headers={"content-type": content_type})

    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "bad_content_type")


def test_a_missing_content_type_is_dropped(client: TestClient, queue: HookQueue) -> None:
    response = client.post(HOOK_PATH, content=b"{}")

    assert "content-type" not in response.request.headers
    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "bad_content_type")


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example"])
def test_a_disallowed_host_is_dropped(client: TestClient, queue: HookQueue, host: str) -> None:
    response = client.post(HOOK_PATH, content=b"{}", headers={**JSON, "host": host})

    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "bad_host")


def test_a_missing_host_is_dropped(client: TestClient, app: FastAPI, queue: HookQueue) -> None:
    messages: list[Message] = [{"type": "http.request", "body": b"{}"}]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.0",
        "method": "POST",
        "scheme": "http",
        "path": HOOK_PATH,
        "raw_path": HOOK_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4300),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))

    assert sent[0]["status"] == 204
    assert messages != []  # the body was never read
    assert queue.depth == 0
    only_drop(queue, "bad_host")


def test_a_body_over_the_cap_by_content_length_is_dropped(
    client: TestClient, queue: HookQueue
) -> None:
    response = client.post(HOOK_PATH, content=b"x" * (BODY_MAX + 1), headers=JSON)

    assert response.request.headers["content-length"] == str(BODY_MAX + 1)
    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "too_large")


def test_a_declared_length_over_the_cap_is_refused_before_reading(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", b"104857601")]

    status, body, calls = raw_post(app, [b"{}"], headers)

    assert (status, body, calls) == (204, b"", 0)
    only_drop(queue, "too_large")


def test_a_body_at_the_cap_is_accepted(client: TestClient, queue: HookQueue) -> None:
    assert_neutral(client.post(HOOK_PATH, content=b"x" * BODY_MAX, headers=JSON))

    assert queue.depth == 1
    no_drops(queue)


def test_a_chunked_body_over_the_cap_is_dropped(client: TestClient, queue: HookQueue) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b"x" * (20 * KIB)

    response = client.post(HOOK_PATH, content=chunks(), headers=JSON)

    assert "content-length" not in response.request.headers
    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "too_large")


def test_a_chunked_body_over_the_cap_stops_being_read(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    status, body, calls = raw_post(app, [b"x" * (20 * KIB)] * 5)

    assert (status, body) == (204, b"")
    # 20, 40, 60 KiB fit; the fourth chunk passes 64 KiB and the fifth is never read.
    assert calls == 4
    only_drop(queue, "too_large")


def test_a_chunked_body_under_the_cap_is_enqueued(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    status, _, _ = raw_post(app, [b'{"a":', b"", b"1}"])

    assert status == 204
    assert [item.body for item in drain(queue)] == [b'{"a":1}']


@pytest.mark.parametrize("value", [b"abc", b"-1", b"1, 2"])
def test_a_malformed_content_length_is_dropped(
    client: TestClient, app: FastAPI, queue: HookQueue, value: bytes
) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", value)]

    status, body, calls = raw_post(app, [b"{}"], headers)

    assert (status, body, calls) == (204, b"", 0)
    only_drop(queue, "bad_request")


def test_a_client_that_disconnects_mid_body_is_dropped(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    messages: list[Message] = [
        {"type": "http.request", "body": b"{", "more_body": True},
        {"type": "http.disconnect"},
    ]

    status, _, _ = asyncio.run(_exchange(app, [(b"content-type", b"application/json")], messages))

    assert status == 204
    assert queue.depth == 0
    only_drop(queue, "bad_request")


def test_a_full_queue_by_count_drops_the_hook(settings: Settings) -> None:
    queue = HookQueue(max_items=2, max_bytes=MIB)
    with start(create_app(settings, writer_factory=idle_writer, queue=queue)) as client:
        responses = [client.post(HOOK_PATH, content=body, headers=JSON) for body in BODIES[:3]]

    for response in responses:
        assert_neutral(response)
    assert [item.body for item in drain(queue)] == BODIES[:2]
    only_drop(queue, "full")


def test_a_full_queue_by_bytes_drops_the_hook(settings: Settings) -> None:
    queue = HookQueue(max_items=100, max_bytes=100)
    with start(create_app(settings, writer_factory=idle_writer, queue=queue)) as client:
        responses = [client.post(HOOK_PATH, content=b"x" * 60, headers=JSON) for _ in range(2)]

    for response in responses:
        assert_neutral(response)
    assert queue.depth == 1
    only_drop(queue, "full")


class ExplodingQueue(HookQueue):
    def offer(self, item: RawHook) -> bool:
        raise RuntimeError(f"{CANARY} {item.body!r} {PRIVATE_PATH}")


def test_a_handler_exception_still_answers_204(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    queue = ExplodingQueue(max_items=10, max_bytes=MIB)
    caplog.set_level(logging.DEBUG)
    with start(create_app(settings, writer_factory=idle_writer, queue=queue)) as client:
        response = client.post(HOOK_PATH, content=BODIES[0], headers=JSON)

    assert_neutral(response)
    only_drop(queue, "internal")
    assert "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
    assert "someone-private" not in caplog.text


def test_a_failing_clock_still_answers_204(settings: Settings, queue: HookQueue) -> None:
    def broken_clock() -> int:
        raise OSError("no clock")

    with start(
        create_app(settings, writer_factory=idle_writer, queue=queue, clock=broken_clock)
    ) as client:
        assert_neutral(client.post(HOOK_PATH, content=b"{}", headers=JSON))

    only_drop(queue, "internal")


def test_hooks_before_startup_are_dropped(app: FastAPI, queue: HookQueue) -> None:
    # Without the lifespan there is no seq to continue from; uvicorn always runs it first.
    response = TestClient(app, base_url=f"http://{GOOD_HOST}").post(
        HOOK_PATH, content=b"{}", headers=JSON
    )

    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "internal")


@pytest.mark.parametrize("method", ["GET", "PUT", "OPTIONS", "DELETE", "PATCH", "HEAD", "TRACE"])
def test_other_methods_get_204(client: TestClient, queue: HookQueue, method: str) -> None:
    response = client.request(method, HOOK_PATH, content=b"{}", headers=JSON)

    assert_neutral(response)
    assert queue.depth == 0
    only_drop(queue, "bad_request")


# The trailing slash: the same hook route, never a redirect.


def test_the_hook_route_with_a_slash_enqueues(client: TestClient, queue: HookQueue) -> None:
    response = client.post(HOOK_PATH + "/", content=b"{}", headers=JSON, follow_redirects=False)

    assert_neutral(response)
    assert [item.body for item in drain(queue)] == [b"{}"]


def test_the_hook_route_with_a_slash_drops_a_bad_host(client: TestClient, queue: HookQueue) -> None:
    response = client.post(
        HOOK_PATH + "/",
        content=b"{}",
        headers={**JSON, "host": "evil.example"},
        follow_redirects=False,
    )

    assert_neutral(response)
    only_drop(queue, "bad_host")


@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
def test_the_hook_route_with_a_slash_answers_every_method(
    client: TestClient, queue: HookQueue, method: str
) -> None:
    response = client.request(method, HOOK_PATH + "/", follow_redirects=False)

    assert_neutral(response)
    only_drop(queue, "bad_request")


def test_other_routes_with_a_slash_are_not_redirected(client: TestClient) -> None:
    response = client.get("/healthz/", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers


# received_at_ms and seq.


def test_received_at_comes_from_the_injected_clock(settings: Settings, queue: HookQueue) -> None:
    ticks = iter([1_800_000_000_123, 1_800_000_000_456])
    with start(
        create_app(settings, writer_factory=idle_writer, queue=queue, clock=lambda: next(ticks))
    ) as client:
        client.post(HOOK_PATH, content=b"{}", headers=JSON)
        client.post(HOOK_PATH, content=b"{}", headers=JSON)

    assert [item.received_at_ms for item in drain(queue)] == [
        1_800_000_000_123,
        1_800_000_000_456,
    ]


def test_received_at_defaults_to_the_wall_clock(client: TestClient, queue: HookQueue) -> None:
    before = wall_clock_ms()
    client.post(HOOK_PATH, content=b"{}", headers=JSON)
    after = wall_clock_ms()

    (item,) = drain(queue)
    assert before <= item.received_at_ms <= after


def test_received_at_is_taken_once_the_body_is_in(settings: Settings, queue: HookQueue) -> None:
    messages: list[Message] = [
        {"type": "http.request", "body": b"{", "more_body": True},
        {"type": "http.request", "body": b"}"},
    ]
    unread_at_clock: list[int] = []

    def clock() -> int:
        unread_at_clock.append(len(messages))
        return 1_800_000_000_000

    app = create_app(settings, writer_factory=idle_writer, queue=queue, clock=clock)
    with start(app):
        status, _, _ = asyncio.run(
            _exchange(app, [(b"content-type", b"application/json")], messages)
        )

    assert status == 204
    assert unread_at_clock == [0]
    assert [(item.body, item.received_at_ms) for item in drain(queue)] == [
        (b"{}", 1_800_000_000_000)
    ]


def test_the_clock_is_not_read_for_a_dropped_hook(settings: Settings, queue: HookQueue) -> None:
    reads: list[int] = []

    def clock() -> int:
        reads.append(1)
        return 1

    with start(
        create_app(settings, writer_factory=idle_writer, queue=queue, clock=clock)
    ) as client:
        client.post(HOOK_PATH, content=b"{}", headers={"content-type": "text/plain"})

    assert reads == []


def test_seq_is_monotonic_from_one_on_an_empty_database(
    client: TestClient, queue: HookQueue
) -> None:
    for body in BODIES[:5]:
        client.post(HOOK_PATH, content=body, headers=JSON)

    assert [item.seq for item in drain(queue)] == [1, 2, 3, 4, 5]


def test_seq_skips_no_number_for_requests_dropped_before_the_queue(
    client: TestClient, queue: HookQueue
) -> None:
    client.post(HOOK_PATH, content=b"{}", headers=JSON)
    client.post(HOOK_PATH, content=b"{}", headers={"content-type": "text/plain"})
    client.post(HOOK_PATH, content=b"x" * (BODY_MAX + 1), headers=JSON)
    client.post(HOOK_PATH, content=b"{}", headers=JSON)

    assert [item.seq for item in drain(queue)] == [1, 2]


def _store_event(settings: Settings, seq: int) -> None:
    engine = create_engine_for(settings.database_path)
    try:
        upgrade_to_head(engine)
        repository = Repository(engine)
        event = parse_hook(json.loads(BODIES[2]))
        state, effects = apply(None, event, 1_800_000_000_000, seq)
        row = hook_event_row(event, seq=seq, received_at_ms=1_800_000_000_000)
        repository.apply_effects(settings.host_label, row, effects, state)
        assert repository.max_seq() == seq
    finally:
        engine.dispose()


def test_seq_continues_after_the_stored_max(settings: Settings, queue: HookQueue) -> None:
    _store_event(settings, 41)

    with start(create_app(settings, writer_factory=idle_writer, queue=queue)) as client:
        client.post(HOOK_PATH, content=b"{}", headers=JSON)
        client.post(HOOK_PATH, content=b"{}", headers=JSON)

    assert [item.seq for item in drain(queue)] == [42, 43]


def test_seq_continues_after_a_restart(settings: Settings) -> None:
    first = HookQueue(max_items=10, max_bytes=MIB)
    with start(create_app(settings, writer_factory=idle_writer, queue=first)) as client:
        client.post(HOOK_PATH, content=BODIES[2], headers=JSON)
    (item,) = drain(first)
    # The test persists it, as the writer would.
    _store_event(settings, item.seq)

    second = HookQueue(max_items=10, max_bytes=MIB)
    with start(create_app(settings, writer_factory=idle_writer, queue=second)) as client:
        client.post(HOOK_PATH, content=b"{}", headers=JSON)

    assert [hook.seq for hook in drain(second)] == [item.seq + 1]


def test_an_injected_repository_seeds_seq(settings: Settings, queue: HookQueue) -> None:
    _store_event(settings, 7)
    engine = create_engine_for(settings.database_path)
    try:
        with start(
            create_app(
                settings, writer_factory=idle_writer, repository=Repository(engine), queue=queue
            )
        ) as client:
            client.post(HOOK_PATH, content=b"{}", headers=JSON)
        # The caller owns the engine: the app does not dispose it.
        assert Repository(engine).max_seq() == 7
    finally:
        engine.dispose()

    assert [item.seq for item in drain(queue)] == [8]


# The lifespan.


def test_create_app_touches_no_database(settings: Settings) -> None:
    create_app(settings)

    assert not settings.database_path.exists()


def test_the_lifespan_migrates_the_database_to_head(settings: Settings) -> None:
    with start(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        engine = create_engine_for(settings.database_path)
        try:
            with engine.connect() as connection:
                version = connection.execute(text("SELECT version_num FROM alembic_version"))
                assert version.scalar_one() == HEAD
        finally:
            engine.dispose()


def test_the_app_exposes_its_queue_and_repository(settings: Settings, queue: HookQueue) -> None:
    app = create_app(settings, writer_factory=idle_writer, queue=queue)
    assert app.state.queue is queue

    with start(app):
        assert isinstance(app.state.repository, Repository)
        assert app.state.repository.max_seq() == 0
        assert isinstance(app.state.writer, IdleWriter)
        assert app.state.writer.counts().events_processed == 0


def test_the_default_queue_follows_the_settings(settings: Settings) -> None:
    app = create_app(settings)

    queue: HookQueue = app.state.queue
    assert (queue.max_items, queue.max_bytes) == (
        settings.hook_queue_max_items,
        settings.hook_queue_max_bytes,
    )


# Speed: validate, enqueue, respond.


class Timing:
    """Times every HTTP request inside the app, around the guard and the route."""

    def __init__(self, app: ASGIApp, samples: list[float]) -> None:
        self.app = app
        self.samples = samples

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        await self.app(scope, receive, send)
        self.samples.append(time.perf_counter() - started)


def test_the_hook_route_answers_within_50_ms_at_p99(settings: Settings) -> None:
    queue = HookQueue(max_items=1000, max_bytes=64 * MIB)
    app = create_app(settings, writer_factory=idle_writer, queue=queue)
    samples: list[float] = []
    app.add_middleware(Timing, samples=samples)

    with start(app) as client:
        for index in range(500):
            assert_neutral(
                client.post(HOOK_PATH, content=BODIES[index % len(BODIES)], headers=JSON)
            )

    assert queue.depth == 500
    assert len(samples) == 500
    p99 = statistics.quantiles(samples, n=100)[98]
    print(f"hook route p99: {p99 * 1000:.3f} ms, median {statistics.median(samples) * 1000:.3f} ms")
    assert p99 < 0.050


def test_the_hook_route_never_parses_json(
    client: TestClient, queue: HookQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the hook route parsed JSON")

    monkeypatch.setattr(json, "loads", forbidden)
    monkeypatch.setattr(json, "load", forbidden)

    assert_neutral(client.post(HOOK_PATH, content=BODIES[0], headers=JSON))
    assert queue.depth == 1
    no_drops(queue)


# Logs.


def test_logs_never_hold_a_body_or_a_path(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = json.dumps(
        {
            "session_id": "00000000-0000-4000-8000-000000000000",
            "hook_event_name": "UserPromptSubmit",
            "cwd": PRIVATE_PATH,
            "prompt": CANARY,
        }
    ).encode()
    queue = HookQueue(max_items=1, max_bytes=MIB)
    with start(create_app(settings, writer_factory=idle_writer, queue=queue)) as client:
        client.post(HOOK_PATH, content=body, headers=JSON)
        client.post(HOOK_PATH, content=body, headers=JSON)  # full
        client.post(HOOK_PATH, content=body, headers={"content-type": "text/plain"})
        client.post(HOOK_PATH, content=body, headers={**JSON, "host": "evil.example"})
        client.post(HOOK_PATH, content=body + b"x" * BODY_MAX, headers=JSON)
        client.put(HOOK_PATH, content=body, headers=JSON)
        client.get("/healthz", headers={"host": "evil.example"})

    assert queue.drops == {
        "full": 1,
        "too_large": 1,
        "bad_content_type": 1,
        "bad_host": 1,
        "bad_request": 1,
        "invalid": 0,
        "internal": 0,
    }
    assert caplog.records
    for forbidden in (CANARY, "someone-private", "Users", HOOK_PATH + "?", "evil.example"):
        assert forbidden not in caplog.text
