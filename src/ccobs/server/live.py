"""Live updates for the web UI: a change hub fed by the writer, and an SSE stream (ADR 0032).

The writer thread calls ``ChangeHub.notify_threadsafe`` after each commit with the IDs
of the sessions it changed (``ALL`` after retention deletes sessions). That call only
schedules ``publish`` on the event loop (``loop.call_soon_threadsafe``): it is O(1),
never blocks the writer, and does nothing when no browser listens. ``publish`` marks
each subscription dirty; a subscription can filter on one session (the session page)
and holds at most ``MAX_PENDING_IDS`` IDs before it collapses to ``ALL``, so a client
costs bounded memory however fast sessions change.

``GET /ui/stream`` (``?session=<id>`` to filter) is a ``text/event-stream``: first a
``retry: 5000`` line, then one ``event: changed`` per burst of changes, coalesced for
``COALESCE_S`` (``data: all`` or a JSON list of session IDs), and a comment every
``HEARTBEAT_S`` while idle. When an alert fires or clears, every stream, filtered or
not, also gets one ``event: alerts`` per burst with a fixed ``data: alerts`` (ADR 0044):
no alert, session or value, only "refetch the alerts". It never reads the database: the
page refetches its own fragments, through the read gate, when ``live.js`` turns the
``changed`` event into a ``ccobs:changed`` event. Every page of the UI opens a stream.
At most ``MAX_STREAMS`` streams are open at once; one more gets a plain 503, which the
metrics count (``UI_STREAMS_REFUSED``).

A stream ends when the client goes away (Starlette cancels it) or when the hub closes.
uvicorn waits for open connections before it runs the lifespan's shutdown, so the
server closes the hub first (``ccobs.server.__main__``); the lifespan closes it too.

FastAPI's own ``EventSourceResponse`` is not used: it sets ``Cache-Control: no-cache``
(the app wants ``no-store``, ADR 0034), its keep-alive interval is a module global, and
a refusal would have to come from a dependency as JSON; a plain ``StreamingResponse``
keeps all three in our hands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Final

from fastapi import APIRouter, Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ccobs.domain.hooks import is_valid_session_id
from ccobs.server.changes import ALL, AlertsChanged, Change, Everything, HubEvent

__all__ = [
    "ALERTS_EVENT",
    "ALL",
    "COALESCE_S",
    "HEARTBEAT_S",
    "MAX_PENDING_IDS",
    "MAX_STREAMS",
    "RETRY_MS",
    "STREAM_PATH",
    "Change",
    "ChangeHub",
    "Everything",
    "StreamTiming",
    "Subscription",
    "encode_change",
    "router",
    "stream_events",
]

_log = logging.getLogger(__name__)

STREAM_PATH: Final = "/ui/stream"
# Open streams at once, for the whole app: one per tab of any page (ADR 0044).
MAX_STREAMS: Final = 16
# Session IDs a subscription keeps between two events before it says "all" instead.
MAX_PENDING_IDS: Final = 64
# How long the browser waits before it reconnects on its own (live.js reconnects itself).
RETRY_MS: Final = 5000
# How long a stream gathers changes after the first one before it sends the event.
COALESCE_S: Final = 0.25
# How often an idle stream sends a comment, so that nothing between drops it as dead.
HEARTBEAT_S: Final = 15.0
_HEARTBEAT: Final = b": keep-alive\n\n"
# The alerts ping: a fixed marker (an event with no data line is never dispatched).
ALERTS_EVENT: Final = b"event: alerts\ndata: alerts\n\n"
_RETRY_AFTER_S: Final = "5"
_BAD_REQUEST: Final = 400
_SERVICE_UNAVAILABLE: Final = 503


@dataclass(frozen=True, kw_only=True)
class StreamTiming:
    """The stream's intervals; the defaults in production, short ones in tests."""

    retry_ms: int = RETRY_MS
    coalesce_s: float = COALESCE_S
    heartbeat_s: float = HEARTBEAT_S


class Subscription:
    """One stream's view of the changes: what changed since the stream last sent an
    event, for every session or for ``session_id`` only. Lives on the event loop."""

    def __init__(
        self,
        session_id: str | None,
        *,
        max_ids: int,
        release: Callable[[Subscription], None],
    ) -> None:
        self.session_id: Final = session_id
        self._max_ids = max_ids
        self._release = release
        self._pending: set[str] | Everything = set()
        # Whether an alert fired or cleared since the last alerts ping (ADR 0044).
        self._alerts = False
        # Set by a change, an alerts change and close(); cleared once both are taken.
        self._dirty = asyncio.Event()
        self._closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def offer(self, change: Change) -> None:
        """Add ``change`` to what is pending, if it concerns this subscription."""
        if self.closed:
            return
        pending = self._pending
        if self.session_id is not None:
            if isinstance(change, Everything) or isinstance(pending, Everything):
                self._pending = ALL
            elif self.session_id in change:
                self._pending = {self.session_id}
            else:
                return
        elif isinstance(change, Everything) or isinstance(pending, Everything):
            self._pending = ALL
        else:
            pending.update(change)
            if len(pending) > self._max_ids:
                self._pending = ALL
        self._dirty.set()

    def offer_alerts(self) -> None:
        """Note that the alerts changed: every subscription, whatever its session
        filter, gets the next alerts ping (ADR 0044)."""
        if self.closed:
            return
        self._alerts = True
        self._dirty.set()

    def take_alerts(self) -> bool:
        """Whether the alerts changed since the last call, and start over. Stays dirty
        while a session change is pending, which ``take`` returns."""
        alerts = self._alerts
        self._alerts = False
        if not self._pending:
            self._dirty.clear()
        return alerts

    def take(self) -> Change:
        """What changed since the last call, and start over. Stays dirty while an
        alerts change is pending, which ``take_alerts`` returns."""
        pending = self._pending
        self._pending = set()
        if not self._alerts:
            self._dirty.clear()
        return pending if isinstance(pending, Everything) else frozenset(pending)

    async def wait(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for a change (or close); False if none came."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._dirty.wait()
        except TimeoutError:
            return False
        return True

    async def settle(self, delay_s: float) -> None:
        """Let more changes gather for ``delay_s``; returns at once on close."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay_s):
                await self._closed.wait()

    def close(self) -> None:
        """End the subscription and free its slot; harmless when already closed."""
        if self.closed:
            return
        self._closed.set()
        self._dirty.set()
        self._release(self)


class ChangeHub:
    """The app's live subscriptions, and the writer's way to reach them.

    Built with the app; ``open`` binds it to the event loop at start-up (until then, and
    after ``close``, it refuses streams and ignores changes). Every method but
    ``notify_threadsafe`` runs on the event loop. The counts are plain integers, which
    the metrics listener reads from its own thread.
    """

    def __init__(self, *, max_streams: int = MAX_STREAMS, max_ids: int = MAX_PENDING_IDS) -> None:
        self.max_streams: Final = max_streams
        self._max_ids = max_ids
        self._subscriptions: set[Subscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refused = 0

    @property
    def is_open(self) -> bool:
        return self._loop is not None

    @property
    def open_streams(self) -> int:
        return len(self._subscriptions)

    @property
    def refused_streams(self) -> int:
        return self._refused

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, session_id: str | None) -> Subscription | None:
        """A new subscription; None when the hub is not open or the cap is reached
        (only the cap counts as a refusal)."""
        if self._loop is None:
            return None
        if len(self._subscriptions) >= self.max_streams:
            self._refused += 1
            return None
        subscription = Subscription(
            session_id, max_ids=self._max_ids, release=self._subscriptions.discard
        )
        self._subscriptions.add(subscription)
        return subscription

    def publish(self, change: HubEvent) -> None:
        if isinstance(change, AlertsChanged):
            # No session changed: every stream gets the alerts ping, filtered or not.
            for subscription in list(self._subscriptions):
                subscription.offer_alerts()
            return
        for subscription in list(self._subscriptions):
            subscription.offer(change)

    def notify_threadsafe(self, change: HubEvent) -> None:
        """The writer's ``on_committed``: schedule ``publish`` on the event loop. From
        any thread; never blocks, never raises for a closed loop."""
        loop = self._loop
        if loop is None or not self._subscriptions:
            return
        # The loop is gone: the app is stopping, nobody listens any more.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self.publish, change)

    def close(self) -> None:
        """Refuse new streams and end every open one: each stream's generator returns."""
        self._loop = None
        for subscription in list(self._subscriptions):
            subscription.close()


def encode_change(change: Change) -> bytes:
    """One ``changed`` event: ``all``, or the session IDs as a JSON list. JSON escapes
    every control and non-ASCII character, so the data stays on one line."""
    data = change.value if isinstance(change, Everything) else json.dumps(sorted(change))
    return f"event: changed\ndata: {data}\n\n".encode()


async def stream_events(
    subscription: Subscription, timing: StreamTiming
) -> AsyncGenerator[bytes, None]:
    """The stream's body: the retry line, then coalesced events and heartbeats until the
    subscription closes. Closes the subscription when it ends, however it ends."""
    try:
        yield f"retry: {timing.retry_ms}\n\n".encode()
        while True:
            if not await subscription.wait(timing.heartbeat_s):
                yield _HEARTBEAT
                continue
            if subscription.closed:
                return
            await subscription.settle(timing.coalesce_s)
            if subscription.closed:
                return
            alerts = subscription.take_alerts()
            change = subscription.take()
            if isinstance(change, Everything) or change:
                yield encode_change(change)
            if alerts:
                yield ALERTS_EVENT
    finally:
        subscription.close()


class _LiveResponse(StreamingResponse):
    """The stream; frees its slot even if the body never starts (a client gone before
    the first byte)."""

    def __init__(self, subscription: Subscription, timing: StreamTiming) -> None:
        super().__init__(stream_events(subscription, timing), media_type="text/event-stream")
        self._subscription = subscription

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._subscription.close()


router = APIRouter(include_in_schema=False)


@router.get(STREAM_PATH)
async def stream(request: Request, session: str | None = None) -> Response:
    """Server-sent events that say when sessions change; ``session`` filters on one."""
    if session is not None and not is_valid_session_id(session):
        # The value is not echoed.
        return PlainTextResponse("invalid session\n", status_code=_BAD_REQUEST)
    hub: ChangeHub = request.app.state.live
    if not hub.is_open:
        return PlainTextResponse("the app is stopping\n", status_code=_SERVICE_UNAVAILABLE)
    subscription = hub.subscribe(session)
    if subscription is None:
        _log.warning("live stream refused: %d streams open", hub.open_streams)
        return PlainTextResponse(
            "too many live streams\n",
            status_code=_SERVICE_UNAVAILABLE,
            headers={"retry-after": _RETRY_AFTER_S},
        )
    timing: StreamTiming = request.app.state.stream_timing
    return _LiveResponse(subscription, timing)
