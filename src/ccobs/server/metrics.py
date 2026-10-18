"""The app's own metrics and the listener that serves them (ADR 0028).

``AppMetrics`` holds one ``CollectorRegistry`` per app, never prometheus_client's global
one, so that several apps (in tests) never share series. Live state is read at scrape
time, not pushed: a collector asks the writer for its counts, the queue for its depth,
bytes and drop counters and the live updates hub (ADR 0032) for its open and refused
streams, so a gauge never goes stale. Only the block-duration histogram
and the Loki and Prometheus query counters keep state of their own, fed by the writer's
``on_block_closed`` callback, the reconciler's ``on_query`` (ADR 0029) and the usage
reader's ``on_query`` (ADR 0035). Every label
value comes from a closed list (``ccobs.domain.app_metrics``), states, kinds, reasons and
events are exported with zeros, and there is no ``_created`` series.

``MetricsListener`` serves the registry on ``CCOBS_METRICS_PORT`` from a thread of its
own: ``/metrics`` only, and a 404 for anything else. It has no Host check: compose never
publishes the port, so only containers on the compose network reach it (ADR 0028). The
public app (``ccobs.server.app``) has no ``/metrics`` route at all.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from socketserver import TCPServer, ThreadingMixIn
from typing import Any, Final, Protocol
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from prometheus_client import (
    CollectorRegistry,
    Histogram,
    disable_created_metrics,
    make_wsgi_app,
)
from prometheus_client.metrics_core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

import ccobs
from ccobs.domain.app_metrics import (
    AGENTS_RUNNING,
    BLOCK_DURATION,
    BLOCK_DURATION_BUCKETS,
    BLOCK_KINDS,
    BLOCKS_OPEN,
    BUILD_INFO,
    DATABASE_WAL,
    EVENT_LABELS,
    HOOK_EVENTS_DROPPED,
    HOOK_EVENTS_PROCESSED,
    HOOK_EVENTS_RECEIVED,
    HOOK_QUEUE_BYTES,
    HOOK_QUEUE_DEPTH,
    LOKI_QUERIES,
    LOKI_RESULTS,
    METRICS_PATH,
    PROMETHEUS_QUERIES,
    PROMETHEUS_RESULTS,
    SESSION_STATES,
    SESSIONS,
    SESSIONS_ENDED,
    UI_STREAMS,
    UI_STREAMS_REFUSED,
    AppMetric,
    LokiQueryResult,
    PrometheusQueryResult,
)
from ccobs.domain.sessions import BlockKind
from ccobs.server.ingest import DROP_REASONS, HookQueue
from ccobs.server.writer import WriterCounts

__all__ = [
    "EXCLUSIVE_BIND_OPTION",
    "SERVER_NAME",
    "AppMetrics",
    "CountsSource",
    "MetricsListener",
    "StreamCounts",
    "address_family",
]

_log = logging.getLogger(__name__)

# How often the listener's loop checks for shutdown, in seconds: shutdown waits up to
# this long, once per app stop.
_POLL_S: Final = 0.05
_NOT_FOUND: Final = b"not found\n"
# The listener's Server header: no software version.
SERVER_NAME: Final = "ccobs"
# On Windows, SO_EXCLUSIVEADDRUSE keeps any other socket from binding the listener's
# port, even one that sets SO_REUSEADDR (which Windows would otherwise let share it).
# Other platforms do not have the option: a bound port is never shared there without
# SO_REUSEPORT.
EXCLUSIVE_BIND_OPTION: Final[int | None] = (
    getattr(socket, "SO_EXCLUSIVEADDRUSE", None) if sys.platform == "win32" else None
)

type _WsgiApp = Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]

# prometheus_client leaves this function unannotated; it takes nothing, returns None.
_disable_created_metrics: Final[Callable[[], None]] = disable_created_metrics


class CountsSource(Protocol):
    """What the collector needs of the writer: ``Writer``, or a test double."""

    def counts(self) -> WriterCounts: ...


class StreamCounts(Protocol):
    """What the collector needs of the live updates hub: ``ChangeHub``, or a test double."""

    @property
    def open_streams(self) -> int: ...

    @property
    def refused_streams(self) -> int: ...


def _gauge(metric: AppMetric) -> GaugeMetricFamily:
    return GaugeMetricFamily(metric.name, metric.help, labels=metric.labels)


def _counter(metric: AppMetric) -> CounterMetricFamily:
    return CounterMetricFamily(metric.name, metric.help, labels=metric.labels)


class _StateCollector(Collector):
    """Reads the queue, and the writer once attached, at every scrape."""

    def __init__(
        self, host: str, queue: HookQueue, version: str, streams: StreamCounts | None
    ) -> None:
        self._host = host
        self._queue = queue
        self._version = version
        self._streams = streams
        self.writer: CountsSource | None = None
        self.wal = False
        # The query counters: fed from the reconciler's thread and the event loop, read at
        # scrape time on the listener's thread.
        self._queries_lock = threading.Lock()
        self._loki_queries: Counter[str] = Counter()
        self._prometheus_queries: Counter[str] = Counter()

    def count_loki_query(self, result: LokiQueryResult) -> None:
        with self._queries_lock:
            self._loki_queries[result] += 1

    def count_prometheus_query(self, result: PrometheusQueryResult) -> None:
        with self._queries_lock:
            self._prometheus_queries[result] += 1

    def collect(self) -> Iterator[Metric]:
        host = self._host
        yield from self._from_queue()
        build = _gauge(BUILD_INFO)
        build.add_metric([host, self._version], 1)
        yield build
        with self._queries_lock:
            queries = dict(self._loki_queries)
            prometheus_queries = dict(self._prometheus_queries)
        loki = _counter(LOKI_QUERIES)
        for result in LOKI_RESULTS:
            loki.add_metric([host, result], queries.get(result, 0))
        yield loki
        prometheus = _counter(PROMETHEUS_QUERIES)
        for outcome in PROMETHEUS_RESULTS:
            prometheus.add_metric([host, outcome], prometheus_queries.get(outcome, 0))
        yield prometheus
        yield from self._from_streams()
        writer = self.writer
        if writer is None:
            return
        yield from self._from_writer(writer.counts())
        wal = _gauge(DATABASE_WAL)
        wal.add_metric([host], 1 if self.wal else 0)
        yield wal

    def _from_queue(self) -> Iterator[Metric]:
        host = self._host
        drops = self._queue.drops
        dropped = _counter(HOOK_EVENTS_DROPPED)
        for reason in DROP_REASONS:
            dropped.add_metric([host, reason], drops[reason])
        yield dropped
        depth = _gauge(HOOK_QUEUE_DEPTH)
        depth.add_metric([host], self._queue.depth)
        yield depth
        size = _gauge(HOOK_QUEUE_BYTES)
        size.add_metric([host], self._queue.bytes)
        yield size

    def _from_streams(self) -> Iterator[Metric]:
        streams = self._streams
        opened = _gauge(UI_STREAMS)
        opened.add_metric([self._host], 0 if streams is None else streams.open_streams)
        yield opened
        refused = _counter(UI_STREAMS_REFUSED)
        refused.add_metric([self._host], 0 if streams is None else streams.refused_streams)
        yield refused

    def _from_writer(self, counts: WriterCounts) -> Iterator[Metric]:
        host = self._host
        sessions = _gauge(SESSIONS)
        for state in SESSION_STATES:
            sessions.add_metric([host, state], counts.sessions_by_status.get(state, 0))
        yield sessions
        blocks = _gauge(BLOCKS_OPEN)
        for kind in BLOCK_KINDS:
            blocks.add_metric([host, kind], counts.open_blocks_by_kind.get(kind, 0))
        yield blocks
        agents = _gauge(AGENTS_RUNNING)
        agents.add_metric([host], counts.running_agents)
        yield agents
        ended = _counter(SESSIONS_ENDED)
        ended.add_metric([host], counts.sessions_ended)
        yield ended
        processed = _counter(HOOK_EVENTS_PROCESSED)
        processed.add_metric([host], counts.events_processed)
        yield processed
        received = _counter(HOOK_EVENTS_RECEIVED)
        for event in EVENT_LABELS:
            received.add_metric([host, event], counts.events_received.get(event, 0))
        yield received


class AppMetrics:
    """The registry of one app: the state collector and the block-duration histogram.

    The writer's series (and the WAL flag) appear once ``attach`` is called, after the
    writer exists; the queue's series, the live stream series (zeros without
    ``streams``) and build info from the start.
    """

    def __init__(
        self,
        host: str,
        queue: HookQueue,
        *,
        streams: StreamCounts | None = None,
        version: str = ccobs.__version__,
    ) -> None:
        # The flag is process-wide: no _created series on any registry (ADR 0013's note
        # on created-timestamp-zero-ingestion, ADR 0028). Set on every construction, in
        # case something turned it back on.
        _disable_created_metrics()
        self.registry: Final = CollectorRegistry(auto_describe=False)
        self._state = _StateCollector(host, queue, version, streams)
        self.registry.register(self._state)
        durations = Histogram(
            BLOCK_DURATION.name,
            BLOCK_DURATION.help,
            labelnames=BLOCK_DURATION.labels,
            buckets=BLOCK_DURATION_BUCKETS,
            registry=self.registry,
        )
        # One child per kind, created now so that both kinds are exported with zeros.
        self._durations: Final = {kind: durations.labels(host, kind) for kind in BLOCK_KINDS}

    def attach(self, writer: CountsSource, *, wal: bool) -> None:
        """Export the writer's counts and whether the database runs in WAL mode."""
        self._state.wal = wal
        self._state.writer = writer

    def count_loki_query(self, result: LokiQueryResult) -> None:
        """The reconciler's ``on_query``: one query sent to Loki, with its ``result``."""
        self._state.count_loki_query(result)

    def count_prometheus_query(self, result: PrometheusQueryResult) -> None:
        """The usage reader's ``on_query``: one query sent to Prometheus, with its
        ``result``."""
        self._state.count_prometheus_query(result)

    def observe_block_closed(self, kind: BlockKind, seconds: float) -> None:
        """The writer's ``on_block_closed``: one closed block of ``kind``, open ``seconds``."""
        self._durations[kind].observe(seconds)


def address_family(host: str) -> socket.AddressFamily:
    """IPv6 for an IPv6 literal (``::``, ``::1``), IPv4 for anything else."""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _only_metrics(app: _WsgiApp) -> _WsgiApp:
    """``app`` on ``METRICS_PATH``, a plain 404 on every other path; every answer with
    ``Server: ccobs``."""

    def with_server(start_response: StartResponse) -> StartResponse:
        def start(status: str, headers: list[tuple[str, str]], *rest: Any) -> Any:
            # wsgiref adds its own Server header (with the Python version) only when the
            # app sent none.
            return start_response(status, [*headers, ("Server", SERVER_NAME)], *rest)

        return start

    def wrapped(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if environ.get("PATH_INFO") == METRICS_PATH:
            return app(environ, with_server(start_response))
        with_server(start_response)(
            "404 Not Found",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(_NOT_FOUND))),
            ],
        )
        return [_NOT_FOUND]

    return wrapped


class _QuietHandler(WSGIRequestHandler):
    """No access log: it would record client addresses and request lines. The errors
    the handler answers itself (a malformed request line) name no software version."""

    server_version = SERVER_NAME
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _server_class(family: socket.AddressFamily, exclusive_option: int | None) -> type[WSGIServer]:
    class _Server(ThreadingMixIn, WSGIServer):
        address_family = family
        daemon_threads = True
        # On Windows, SO_REUSEADDR lets another socket bind a port that is in use;
        # elsewhere it only skips the TIME_WAIT of closed connections on a restart.
        allow_reuse_address = sys.platform != "win32"

        def server_bind(self) -> None:
            if exclusive_option is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
            # HTTPServer.server_bind resolves the bound address with socket.getfqdn,
            # a reverse DNS lookup that can stall and yields the machine's name.
            TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = str(host)
            self.server_port = int(port)
            self.setup_environ()

    return _Server


class MetricsListener:
    """Serves a registry's ``/metrics`` on ``host:port`` from a daemon thread.

    The socket is bound in the constructor, so a busy port raises ``OSError`` there;
    ``port`` 0 picks a free one, which ``port`` then reports. ``exclusive_option`` is
    the socket option set before the bind to keep the port exclusive
    (``EXCLUSIVE_BIND_OPTION``, None outside Windows); injectable for tests.
    """

    def __init__(
        self,
        registry: CollectorRegistry,
        host: str,
        port: int,
        *,
        exclusive_option: int | None = EXCLUSIVE_BIND_OPTION,
    ) -> None:
        self.host: Final = host
        app = _only_metrics(make_wsgi_app(registry))
        self._server = make_server(
            host,
            port,
            app,
            server_class=_server_class(address_family(host), exclusive_option),
            handler_class=_QuietHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _POLL_S},
            name="ccobs-metrics",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def listening_socket(self) -> socket.socket:
        """The listening socket."""
        return self._server.socket

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the socket, so the port is free on return."""
        # shutdown() waits for serve_forever to return: it would wait forever on a
        # listener that never started.
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()
