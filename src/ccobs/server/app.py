"""The app's HTTP surface: the app factory, its lifespan and the hook route (ADR 0027).

``POST /hooks`` validates, stamps and enqueues, then answers an empty 204; it never
parses the body, which the writer thread (``ccobs.server.writer``) does in the
background, so the route stays O(1) and far inside Claude Code's hook timeout. Every
request on the hook route, whatever happens to it, gets the same neutral 204 (CLAUDE.md,
ADR 0011). The lifespan starts the writer once the database is migrated, then the
metrics listener (``ccobs.server.metrics``, ADR 0028), which serves ``/metrics`` on a
port of its own (this app has no ``/metrics`` route), then the sweeper
(``ccobs.server.sweeps``), which submits the stale sweep and retention to the writer,
then the reconciler (``ccobs.server.reconcile``, ADR 0029), which closes permission
blocks denied with Esc from Loki. The live updates hub (``ccobs.server.live``, ADR 0032)
opens on the event loop before the writer starts, and the writer tells it what each
commit changed. At shutdown it closes the hub, which ends the UI's streams, then stops
the listener, then the reconciler, then the sweeper, then the writer, draining the
queue, before it disposes the engines. There are two (ADR 0033): the writer's, with one
connection, and the readers', query-only, which the API reads through its read gate
(``ccobs.server.reads``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final, Protocol

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ccobs.server import api, live, ui
from ccobs.server.config import Settings
from ccobs.server.db.engine import create_engine_for, wal_enabled
from ccobs.server.db.repository import Repository
from ccobs.server.guards import (
    HOOK_PATHS,
    ClientDisconnectedError,
    FetchMetadataGuard,
    HostGuard,
    SecurityHeaders,
    declared_length,
    header_values,
    is_json_media_type,
    read_body,
    send_empty,
)
from ccobs.server.ingest import Clock, DropReason, HookQueue, RawHook, SeqAllocator, wall_clock_ms
from ccobs.server.live import ChangeHub, StreamTiming
from ccobs.server.metrics import AppMetrics, MetricsListener
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.reads import READ_WAIT_TIMEOUT_S, ReadGate
from ccobs.server.reconcile import POLL_INTERVAL_S, Reconciler, ReconcileTarget
from ccobs.server.sweeps import RETENTION_INTERVAL_S, TICK_INTERVAL_S, Sweeper, SweepTarget
from ccobs.server.writer import Writer, WriterCounts, WriterObservers

__all__ = ["BackgroundWriter", "HookEndpoint", "WriterFactory", "create_app"]

_log = logging.getLogger("ccobs.server")

_POST: Final = "POST"
_MS_PER_S: Final = 1000
# How long shutdown waits for the writer to drain the queue before it counts the rest
# as dropped; well inside the 30 s stop_grace_period that compose.yaml gives the app.
WRITER_STOP_TIMEOUT_S: Final = 5.0
# How long shutdown waits for the sweeper thread; its stop interrupts its wait at once.
SWEEPER_STOP_TIMEOUT_S: Final = 5.0
# How long shutdown waits for the reconciler thread: its stop interrupts its wait at
# once. A query in flight is not cut short: its 5 s timeout applies to each phase
# (connect, each read or write), not to the whole request, and shutdown moves on (with
# a warning) when this runs out.
RECONCILER_STOP_TIMEOUT_S: Final = 5.0


class BackgroundWriter(SweepTarget, ReconcileTarget, Protocol):
    """What the app needs of the writer: ``Writer``, or a test double. The sweeper
    submits its ticks and retention through ``SweepTarget``'s methods, and the
    reconciler reads open blocks and submits closes through ``ReconcileTarget``'s."""

    def start(self) -> None: ...

    def stop(self, timeout_s: float) -> None: ...

    def counts(self) -> WriterCounts: ...


# Builds the writer once the database is migrated, from the queue, the repository and
# the observers: the block-duration histogram (ADR 0028) and the live updates hub (ADR
# 0032).
type WriterFactory = Callable[[HookQueue, Repository, WriterObservers], BackgroundWriter]


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
    serve_metrics: bool = True,
    metrics_port: int | None = None,
    sweeps: bool = True,
    sweep_intervals: tuple[float, float] | None = None,
    reconcile: bool = True,
    reconcile_interval_s: float | None = None,
    loki_transport: httpx.BaseTransport | None = None,
    read_wait_timeout_s: float = READ_WAIT_TIMEOUT_S,
    stream_timing: StreamTiming | None = None,
) -> FastAPI:
    """The app, not started: its lifespan migrates the database, seeds ``seq``, runs the
    writer thread and the metrics listener.

    ``repository`` is for tests and later stages that own their engine (the app neither
    creates nor disposes one then); ``queue`` and ``clock`` default to a queue bounded by
    the settings and the wall clock; ``writer_factory`` to a ``Writer`` with the
    settings' host label and stale threshold. The metrics listener (ADR 0028) binds
    ``settings.listen_host`` on ``metrics_port``, ``settings.metrics_port`` by default
    (0 picks a free port, for tests); ``serve_metrics=False`` leaves it off, the registry
    still in ``app.state.metrics``. The sweeper (``ccobs.server.sweeps``) submits the
    stale sweep and retention to the writer on ``sweep_intervals``, ``(tick, retention)``
    in seconds (60 s and 1 h by default), with the same clock as the hook route;
    ``sweeps=False`` leaves it off. The reconciler (``ccobs.server.reconcile``, ADR
    0029) polls ``settings.loki_url`` every ``reconcile_interval_s`` (2 s by default)
    while permission blocks are open, with the same clock, through ``loki_transport``
    (httpx's by default; tests pass an ``httpx.MockTransport``); it runs only when both
    ``reconcile`` and ``settings.telemetry_reconcile`` are true. The API reads through
    an engine of its own, query-only, on the same database file, which the app always
    creates and disposes (ADR 0033); a read that waits ``read_wait_timeout_s`` for a free
    connection answers 503. The live updates hub is ``app.state.live``, built here so
    that the server can close it before uvicorn waits for open connections
    (``ccobs.server.__main__``); ``stream_timing`` sets its streams' intervals (tests).
    """
    hook_queue = queue or HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    endpoint = HookEndpoint(
        hook_queue, body_max_bytes=settings.hook_body_max_bytes, clock=clock or wall_clock_ms
    )

    hub = ChangeHub()

    def default_writer(hooks: HookQueue, repo: Repository, observers: WriterObservers) -> Writer:
        stale_after_ms = int(settings.session_stale_after.total_seconds() * _MS_PER_S)
        return Writer(
            hooks,
            repo,
            settings.host_label,
            stale_after_ms=stale_after_ms,
            on_block_closed=observers.on_block_closed,
            on_committed=observers.on_committed,
        )

    make_writer = writer_factory or default_writer
    tick_interval_s, retention_interval_s = sweep_intervals or (
        TICK_INTERVAL_S,
        RETENTION_INTERVAL_S,
    )
    listen_port = settings.metrics_port if metrics_port is None else metrics_port

    def start_listener(metrics: AppMetrics) -> MetricsListener | None:
        """The metrics listener, started; None (logged) if its port cannot be bound:
        hooks matter more than metrics, so the app runs on without it."""
        try:
            listener = MetricsListener(metrics.registry, settings.listen_host, listen_port)
        except OSError as error:
            _log.error(
                "metrics listener could not start on port %d (%s)",
                listen_port,
                type(error).__name__,
            )
            return None
        listener.start()
        return listener

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = repository is None
        if repository is None:
            repo = Repository(create_engine_for(settings.database_path, role="writer"))
        else:
            repo = repository
        reader: Repository | None = None
        try:
            # Before the first request: a failed migration stops the app (ADR 0026).
            upgrade_to_head(repo.engine)
            # The same file as the writer's engine; upgrade_to_head has checked that it
            # names one. Its connections open lazily, after the migration.
            database = Path(str(repo.engine.url.database))
            reader = Repository(create_engine_for(database, role="reader"))
            app.state.read_repository = reader
            app.state.reads = ReadGate(wait_timeout_s=read_wait_timeout_s)
            # WAL is set on every connection (ADR 0005), so one check at start-up holds;
            # read before the writer starts, so nothing that can raise sits between
            # writer.start() and the try block that stops it.
            wal = wal_enabled(repo.engine)
            endpoint.seq = SeqAllocator(repo.max_seq() + 1)
            app.state.repository = repo
            # Before the writer: the histogram must exist for the first closed block, and
            # the hub must be open for the first commit.
            metrics = AppMetrics(settings.host_label, hook_queue, streams=hub)
            hub.open(asyncio.get_running_loop())
            observers = WriterObservers(
                on_block_closed=metrics.observe_block_closed,
                on_committed=hub.notify_threadsafe,
            )
            writer = make_writer(hook_queue, repo, observers)
            writer.start()
            app.state.writer = writer
            metrics.attach(writer, wal=wal)
            app.state.metrics = metrics
            listener = start_listener(metrics) if serve_metrics else None
            app.state.metrics_listener = listener
            sweeper: Sweeper | None = None
            app.state.sweeper = None
            reconciler: Reconciler | None = None
            app.state.reconciler = None
            try:
                if sweeps:
                    # After the writer starts, inside the try that stops it.
                    sweeper = Sweeper(
                        writer,
                        retention=settings.retention,
                        tick_interval_s=tick_interval_s,
                        retention_interval_s=retention_interval_s,
                        clock=endpoint.clock,
                    )
                    sweeper.start()
                    app.state.sweeper = sweeper
                if reconcile and settings.telemetry_reconcile:
                    reconciler = Reconciler(
                        writer,
                        loki_url=settings.loki_url,
                        host=settings.host_label,
                        poll_interval_s=reconcile_interval_s or POLL_INTERVAL_S,
                        clock=endpoint.clock,
                        transport=loki_transport,
                        on_query=metrics.count_loki_query,
                    )
                    reconciler.start()
                    app.state.reconciler = reconciler
                yield
            finally:
                # First: the UI's streams end (the server has usually closed it already).
                hub.close()
                # Before the writer stops, so no new scrape reads a writer going away.
                if listener is not None:
                    await asyncio.to_thread(listener.stop)
                    app.state.metrics_listener = None
                # Before the sweeper and the writer: nothing is submitted to a stopping
                # writer, and no query starts during shutdown.
                if reconciler is not None:
                    await asyncio.to_thread(reconciler.stop, RECONCILER_STOP_TIMEOUT_S)
                    app.state.reconciler = None
                # Before the writer stops, so nothing is submitted to a stopping writer.
                if sweeper is not None:
                    await asyncio.to_thread(sweeper.stop, SWEEPER_STOP_TIMEOUT_S)
                    app.state.sweeper = None
                # No new hook enters the queue while the writer drains it.
                endpoint.seq = None
                # Off the event loop: the drain can take up to the timeout.
                await asyncio.to_thread(writer.stop, WRITER_STOP_TIMEOUT_S)
        finally:
            hub.close()
            endpoint.seq = None
            # A read can outlive its request (uvicorn cancels requests after its grace
            # period, the read gate does not stop a running read): dispose() leaves a
            # checked-out connection alone, and it closes when the read returns it.
            if reader is not None:
                reader.engine.dispose()
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
        # A 422 that echoes no request value (the API's query parameters).
        exception_handlers={RequestValidationError: api.validation_error},
    )
    app.state.queue = hook_queue
    # The same clock as the hook route: the UI measures elapsed times and the window of
    # recently ended sessions from it.
    app.state.clock = endpoint.clock
    app.state.live = hub
    app.state.stream_timing = stream_timing or StreamTiming()
    for path in sorted(HOOK_PATHS):
        app.router.routes.append(Route(path, endpoint, include_in_schema=False))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # The read-only JSON API, behind the same guards.
    app.include_router(api.router)
    # The web UI's pages and fragments, and its static files (ADR 0031).
    app.include_router(ui.router)
    # The UI's live updates stream (ADR 0032).
    app.include_router(live.router)
    app.mount("/static", ui.StaticAssets(), name="static")
    # The last one added is the outermost (ADR 0034): the Host guard, then the response
    # headers (so that a 403 carries them), then Fetch Metadata. The hook route passes
    # through the inner two untouched.
    app.add_middleware(FetchMetadataGuard)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(HostGuard, queue=hook_queue)
    return app
