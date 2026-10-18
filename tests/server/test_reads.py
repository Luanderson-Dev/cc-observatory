"""The read gate (``ccobs.server.reads``, ADR 0033): API reads run on worker threads of
their own, at most one per reader connection, and a read that waits too long for a slot
gets a neutral 503.

Each test runs its own event loop with ``anyio.run``. Blocked reads wait on a
``threading.Event`` the test sets, never on a sleep, so the timings do not depend on the
machine; every wait has a generous upper bound so that a failure cannot hang the suite.
"""

from __future__ import annotations

import operator
import threading
import time
from typing import Final

import anyio
import anyio.to_thread
import pytest
from fastapi import HTTPException

from ccobs.server.db.engine import READER_POOL_SIZE
from ccobs.server.reads import BUSY_DETAIL, READ_SLOTS, READ_WAIT_TIMEOUT_S, ReadGate

# Upper bound for any wait on another thread: far above what a pass needs.
PATIENCE_S: Final = 10.0
SHORT_S: Final = 0.1


class Blocked:
    """A sync read that holds its slot until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self) -> str:
        self.started.set()
        self.release.wait(PATIENCE_S)
        self.finished.set()
        return "blocked"

    async def wait_started(self) -> None:
        assert await anyio.to_thread.run_sync(self.started.wait, PATIENCE_S)


def test_the_slots_match_the_reader_pool_and_the_wait_is_bounded() -> None:
    assert READ_SLOTS == READER_POOL_SIZE
    assert READ_WAIT_TIMEOUT_S == 5.0


def test_a_read_runs_on_another_thread_and_returns_its_value() -> None:
    async def main() -> tuple[int, int]:
        gate = ReadGate()
        return await gate.run(threading.get_ident), await gate.run(operator.add, 2, 3)

    thread_id, total = anyio.run(main)

    assert thread_id != threading.get_ident()
    assert total == 5


def test_a_read_never_waits_on_the_default_thread_limiter() -> None:
    async def main() -> str:
        # Every token of the default limiter (Starlette's sync routes and files) taken.
        default = anyio.to_thread.current_default_thread_limiter()
        default.total_tokens = 1
        await default.acquire_on_behalf_of(object())
        with anyio.fail_after(PATIENCE_S):
            return await ReadGate().run(str, "read")

    assert anyio.run(main) == "read"


def test_a_read_that_finds_no_slot_gets_a_503_after_the_timeout() -> None:
    blocked = Blocked()

    async def main() -> tuple[HTTPException, float, str]:
        gate = ReadGate(slots=1, wait_timeout_s=SHORT_S)
        results: list[str] = []

        async def holder() -> None:
            results.append(await gate.run(blocked))

        async with anyio.create_task_group() as group:
            group.start_soon(holder)
            await blocked.wait_started()
            start = time.monotonic()
            with pytest.raises(HTTPException) as error:
                await gate.run(str, "second")
            elapsed = time.monotonic() - start
            blocked.release.set()
        # The slot is free again once the read that held it is over.
        after = await gate.run(str, "third")
        assert results == ["blocked"]
        return error.value, elapsed, after

    error, elapsed, after = anyio.run(main)

    assert error.status_code == 503
    assert error.detail == BUSY_DETAIL == "database busy"
    assert error.headers is None
    # Loose: Windows' monotonic clock ticks every 15.6 ms and timers can fire early.
    assert SHORT_S / 2 <= elapsed < PATIENCE_S
    assert after == "third"


def test_the_timeout_bounds_the_wait_for_a_slot_not_the_read() -> None:
    blocked = Blocked()

    def release_later() -> None:
        blocked.started.wait(PATIENCE_S)
        # Well past the wait timeout: the read is not cut short.
        time.sleep(SHORT_S * 3)
        blocked.release.set()

    async def main() -> str:
        gate = ReadGate(slots=1, wait_timeout_s=SHORT_S)
        releaser = threading.Thread(target=release_later)
        releaser.start()
        try:
            return await gate.run(blocked)
        finally:
            releaser.join(PATIENCE_S)

    assert anyio.run(main) == "blocked"


def test_a_cancelled_request_holds_its_slot_until_its_read_ends() -> None:
    blocked = Blocked()

    async def main() -> None:
        gate = ReadGate(slots=1, wait_timeout_s=SHORT_S)
        request = anyio.CancelScope()

        async def handler() -> None:
            with request:
                await gate.run(blocked)

        async with anyio.create_task_group() as group:
            group.start_soon(handler)
            await blocked.wait_started()
            # A client gone mid-read: its task is cancelled, the thread is not.
            request.cancel()
            await anyio.sleep(0)
            assert not blocked.finished.is_set()
            # The slot stays taken while the connection is in use.
            with pytest.raises(HTTPException):
                await gate.run(str, "other")
            blocked.release.set()
        assert blocked.finished.is_set()
        assert await gate.run(str, "free") == "free"

    anyio.run(main)


def test_a_gate_needs_a_slot_and_a_positive_wait() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ReadGate(slots=0)
    with pytest.raises(ValueError, match="positive"):
        ReadGate(wait_timeout_s=0)
