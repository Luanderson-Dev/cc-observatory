"""The read gate: API reads on worker threads of their own, capped and bounded (ADR 0033).

The API's routes are ``async def``, so FastAPI never runs them on Starlette's default
threadpool (40 threads shared by every sync route and file response). Each database read
goes through ``ReadGate.run`` instead, which runs the sync function on a worker thread,
at most ``READ_SLOTS`` at once: one per connection of the readers' engine, so a read that
holds a slot never waits for a connection. With the writer on an engine of its own, no
number of readers can take the connection the writer needs.

The timeout bounds the wait for a slot, never the read itself. ``run`` first waits for
a slot, for up to ``wait_timeout_s``; a read that gets none answers 503 with a short
neutral detail (no traceback, nothing from the request), and a client can retry. Once
the slot is held, the read runs to its end even if the request is cancelled (the client
went away, or the server shuts down): anyio shields a worker thread it cannot stop
(``abandon_on_cancel=False``), and the slot is released only when the thread returns. So
the slots in use always match the reader connections in use, and a cancelled request
cannot let a new read start on a connection that is still busy. A read cannot run
forever: SQLite's busy timeout (``BUSY_TIMEOUT_MS``) bounds each lock wait, and the
queries are bounded by their limits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import anyio
import anyio.to_thread
from fastapi import HTTPException

from ccobs.server.db.engine import READER_POOL_SIZE

__all__ = ["BUSY_DETAIL", "READ_SLOTS", "READ_WAIT_TIMEOUT_S", "ReadGate"]

# One slot per connection of the readers' engine.
READ_SLOTS: Final = READER_POOL_SIZE
# How long a read waits for a slot before it answers 503.
READ_WAIT_TIMEOUT_S: Final = 5.0
BUSY_DETAIL: Final = "database busy"
_SERVICE_UNAVAILABLE: Final = 503


class ReadGate:
    """Runs sync database reads on worker threads, at most ``slots`` at once.

    Build it on the event loop that serves the requests (the app's lifespan does):
    its limiters belong to that loop.
    """

    def __init__(
        self, *, slots: int = READ_SLOTS, wait_timeout_s: float = READ_WAIT_TIMEOUT_S
    ) -> None:
        if slots < 1:
            raise ValueError("a read gate needs at least one slot")
        if wait_timeout_s <= 0:
            raise ValueError("the wait for a slot must be positive")
        self.wait_timeout_s: Final = wait_timeout_s
        # Who may read: waited on with the timeout.
        self._slots = anyio.CapacityLimiter(slots)
        # The worker threads, as many as the slots. Only a slot holder asks for one, so
        # it never waits here; it keeps reads off the default limiter, whose tokens
        # Starlette's sync routes and file responses share.
        self._threads = anyio.CapacityLimiter(slots)

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        """``read(*args)`` on a worker thread, once a slot is free; a 503 if none frees
        up within ``wait_timeout_s``."""
        acquired = False
        # anyio's acquire either takes the slot or raises: a cancellation by the deadline
        # never leaves a slot taken without ``acquired`` set.
        with anyio.move_on_after(self.wait_timeout_s):
            await self._slots.acquire()
            acquired = True
        if not acquired:
            raise HTTPException(status_code=_SERVICE_UNAVAILABLE, detail=BUSY_DETAIL)
        try:
            return await anyio.to_thread.run_sync(
                read, *args, abandon_on_cancel=False, limiter=self._threads
            )
        finally:
            self._slots.release()
