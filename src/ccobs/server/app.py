"""The app's HTTP surface: the app factory, its lifespan and the hook route (ADR 0027).

``POST /hooks`` validates, stamps and enqueues, then answers an empty 204; it never
parses the body, which the writer thread (``ccobs.server.writer``) does in the
background, so the route stays O(1) and far inside Claude Code's hook timeout. Every
request on the hook route, whatever happens to it, gets the same neutral 204 (CLAUDE.md,
ADR 0011). The lifespan starts the writer once the database is migrated and stops it,
draining the queue, before it disposes the engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final, Protocol

from fastapi import FastAPI
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ccobs.server.config import Settings
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import Repository
from ccobs.server.guards import (
    HOOK_PATHS,
    ClientDisconnectedError,
    HostGuard,
    declared_length,
    header_values,
    is_json_media_type,
    read_body,
    send_empty,
)
from ccobs.server.ingest import Clock, DropReason, HookQueue, RawHook, SeqAllocator, wall_clock_ms
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.writer import Writer, WriterCounts

__all__ = ["BackgroundWriter", "HookEndpoint", "WriterFactory", "create_app"]

_log = logging.getLogger("ccobs.server")

_POST: Final = "POST"
_MS_PER_S: Final = 1000
# How long shutdown waits for the writer to drain the queue before it counts the rest
# as dropped; well inside the 30 s stop_grace_period that compose.yaml gives the app.
WRITER_STOP_TIMEOUT_S: Final = 5.0


class BackgroundWriter(Protocol):
    """What the app needs of the writer: ``Writer``, or a test double."""

    def start(self) -> None: ...

    def stop(self, timeout_s: float) -> None: ...

    def counts(self) -> WriterCounts: ...


# Builds the writer once the database is migrated, from the queue and the repository.
type WriterFactory = Callable[[HookQueue, Repository], BackgroundWriter]


class HookEndpoint:
    """The hook route, as a bare ASGI app so that it answers every method.

    ``seq`` is set by the lifespan, from the database; until then every hook is dropped
    as ``internal`` (uvicorn runs the lifespan before it accepts a connection).
    ``received_at_ms`` is taken once the body is in, right before the seq and the offer.
    """

    def __init__(self, queue: HookQueue, *, body_max_bytes: int, clock: Clock) -> None:
        self.queue = queue
        self.body_max_bytes = body_max_bytes
        self.clock = clock
        self.seq: SeqAllocator | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            reason = await self._ingest(scope, receive)
            if reason is not None:
                self._drop(reason)
        except Exception as error:
            # The type only: an exception's message or traceback may quote the body.
            self.queue.count_drop("internal")
            _log.error("hook dropped: internal error (%s)", type(error).__name__)
        await send_empty(send)

    async def _ingest(self, scope: Scope, receive: Receive) -> DropReason | None:
        """Enqueue the hook; the reason it was dropped before the queue, if it was."""
        if scope["method"] != _POST:
            return "bad_request"
        headers = scope["headers"]
        content_types = header_values(headers, b"content-type")
        if len(content_types) != 1 or not is_json_media_type(content_types[0]):
            return "bad_content_type"
        try:
            length = declared_length(headers)
        except ValueError:
            return "bad_request"
        if length is not None and length > self.body_max_bytes:
            return "too_large"
        try:
            body = await read_body(receive, limit=self.body_max_bytes)
        except ClientDisconnectedError:
            return "bad_request"
        if body is None:
            return "too_large"
        if self.seq is None:
            raise RuntimeError("the app has not started")
        # The clock, the seq and the offer with no await in between: on the one event
        # loop, queue order is seq order, which the writer relies on, and receive times
        # follow seq unless the wall clock steps back (ADR 0027).
        received_at_ms = self.clock()
        if not self.queue.offer(RawHook(body, received_at_ms, self.seq.next())):
            _log.debug("hook dropped: queue full")
        return None

    def _drop(self, reason: DropReason) -> None:
        self.queue.count_drop(reason)
        _log.debug("hook dropped: %s", reason)


def create_app(
    settings: Settings,
    *,
    repository: Repository | None = None,
    queue: HookQueue | None = None,
    clock: Clock | None = None,
    writer_factory: WriterFactory | None = None,
) -> FastAPI:
    """The app, not started: its lifespan migrates the database, seeds ``seq`` and runs
    the writer thread.

    ``repository`` is for tests and later stages that own their engine (the app neither
    creates nor disposes one then); ``queue`` and ``clock`` default to a queue bounded by
    the settings and the wall clock; ``writer_factory`` to a ``Writer`` with the
    settings' host label and stale threshold.
    """
    hook_queue = queue or HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    endpoint = HookEndpoint(
        hook_queue, body_max_bytes=settings.hook_body_max_bytes, clock=clock or wall_clock_ms
    )

    def default_writer(hooks: HookQueue, repo: Repository) -> Writer:
        stale_after_ms = int(settings.session_stale_after.total_seconds() * _MS_PER_S)
        return Writer(hooks, repo, settings.host_label, stale_after_ms=stale_after_ms)

    make_writer = writer_factory or default_writer

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = repository is None
        if repository is None:
            repo = Repository(create_engine_for(settings.database_path))
        else:
            repo = repository
        try:
            # Before the first request: a failed migration stops the app (ADR 0026).
            upgrade_to_head(repo.engine)
            endpoint.seq = SeqAllocator(repo.max_seq() + 1)
            app.state.repository = repo
            writer = make_writer(hook_queue, repo)
            writer.start()
            app.state.writer = writer
            try:
                yield
            finally:
                # No new hook enters the queue while the writer drains it.
                endpoint.seq = None
                # Off the event loop: the drain can take up to the timeout.
                await asyncio.to_thread(writer.stop, WRITER_STOP_TIMEOUT_S)
        finally:
            endpoint.seq = None
            # After the writer has stopped: it writes until then.
            if owned:
                repo.engine.dispose()

    app = FastAPI(
        title="cc-observatory",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # No 307 from /hooks/ to /hooks (or anywhere): the hook route answers only 204,
        # and HOOK_PATHS serves it with and without the slash.
        redirect_slashes=False,
    )
    app.state.queue = hook_queue
    for path in sorted(HOOK_PATHS):
        app.router.routes.append(Route(path, endpoint, include_in_schema=False))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(HostGuard, queue=hook_queue)
    return app
