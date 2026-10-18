"""The app's ``/metrics`` (``ccobs.server.metrics``, ADR 0028).

The series come from the writer's live counts and the queue's counters, read at scrape
time, plus a histogram of closed blocks fed by the writer. Real hook fixtures go through
the real app as in ``test_api.py``; the exposition is parsed with prometheus_client's
own parser and compared with the database. ``/metrics`` is served only by a listener of
its own on the metrics port, never by the public app.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import socket
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import httpx
import prometheus_client
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.samples import Sample
from sqlalchemy import Engine, func, select

import ccobs
from ccobs.domain.app_metrics import (
    AGENTS_RUNNING,
    APP_METRICS,
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
    OTHER_EVENT,
    PROMETHEUS_QUERIES,
    PROMETHEUS_RESULTS,
    RECEIVED_EVENTS,
    SESSION_STATES,
    SESSIONS,
    SESSIONS_ENDED,
    UI_STREAMS,
    UI_STREAMS_REFUSED,
    AppMetric,
)
from ccobs.domain.denials import OpenBlock
from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server import app as server_app
from ccobs.server.config import Settings
from ccobs.server.db.models import AgentModel, BlockModel, HookEventModel, SessionModel
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import DROP_REASONS, HookQueue
from ccobs.server.metrics import (
    EXCLUSIVE_BIND_OPTION,
    SERVER_NAME,
    AppMetrics,
    MetricsListener,
    address_family,
)
from ccobs.server.writer import Writer, WriterCounts, WriterObservers

# The app without its sweeper and reconciler: tests/server/test_sweeps.py and
# tests/server/test_reconcile.py cover them.
create_app: Final = functools.partial(server_app.create_app, sweeps=False, reconcile=False)
HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
SERVER_SOURCES: Final = Path(__file__).parents[2] / "src" / "ccobs" / "server"
# Agents and a permission block; an elicitation block and idle; compaction and a
# permission; a StopFailure (as in test_api.py).
REPLAYED: Final = tuple(
    HOOK_FIXTURES / name
    for name in (
        "headless-tools-subagent.jsonl",
        "interactive-elicitation-idle.jsonl",
        "interactive-permission-compact.jsonl",
        "headless-api-error.jsonl",
    )
)
# The tools fixture's first PermissionRequest is its 12th line (tests/server/test_writer.py).
PERMISSION_PREFIX: Final = 12
WAITING: Final = "00000000-0000-4000-8000-0000000000aa"
SESSION: Final = "00000000-0000-4000-8000-000000000000"
HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
HOUR_MS: Final = 3_600_000
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}

type Payload = dict[str, Any]
type Families = dict[str, list[Sample]]


def fixture_payloads(path: Path) -> list[tuple[int, Payload]]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        lines.append((document["offset_ms"], document["payload"]))
    return lines


def replay_lines() -> list[tuple[int, Payload]]:
    """Every replayed fixture an hour apart, then a session left waiting on a permission."""
    lines = []
    for index, path in enumerate(REPLAYED):
        lines += [(index * HOUR_MS + offset, doc) for offset, doc in fixture_payloads(path)]
    waiting = fixture_payloads(REPLAYED[0])[:PERMISSION_PREFIX]
    start = len(REPLAYED) * HOUR_MS
    lines += [(start + offset, {**doc, "session_id": WAITING}) for offset, doc in waiting]
    return lines


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


def manual(queue: HookQueue, repository: Repository, observers: WriterObservers) -> ManualWriter:
    return ManualWriter(
        queue,
        repository,
        HOST,
        stale_after_ms=12 * HOUR_MS,
        batch_wait_s=0,
        on_block_closed=observers.on_block_closed,
        on_committed=observers.on_committed,
    )


class Clock:
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
def queue(settings: Settings) -> HookQueue:
    return HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)


@pytest.fixture
def client(settings: Settings, clock: Clock, queue: HookQueue) -> Iterator[TestClient]:
    app = create_app(settings, queue=queue, clock=clock, writer_factory=manual, serve_metrics=False)
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def post(client: TestClient, clock: Clock, lines: Sequence[tuple[int, Payload]]) -> None:
    for offset, document in lines:
        clock.now = BASE + offset
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204


def persist(client: TestClient) -> None:
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    while writer.run_once(0.0):
        pass


@pytest.fixture
def replayed(client: TestClient, clock: Clock) -> TestClient:
    post(client, clock, replay_lines())
    persist(client)
    return client


def engine_of(client: TestClient) -> Engine:
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    return repository.engine


def exposition(client: TestClient) -> str:
    metrics = app_of(client).state.metrics
    assert isinstance(metrics, AppMetrics)
    return generate_latest(metrics.registry).decode()


def parse(text: str) -> tuple[dict[str, str], Families]:
    """The type and the samples of each family, keyed by the stored metric name."""
    types: dict[str, str] = {}
    samples: Families = {}
    for family in text_string_to_metric_families(text):
        # The parser strips a counter's _total from the family's name.
        name = family.name + "_total" if family.type == "counter" else family.name
        types[name] = family.type
        samples[name] = list(family.samples)
    return types, samples


def scrape(client: TestClient) -> Families:
    return parse(exposition(client))[1]


def by_label(samples: Families, metric: AppMetric, label: str) -> dict[str, float]:
    """The value of each series of ``metric``, keyed by ``label``."""
    found = [sample for sample in samples[metric.name] if sample.name == metric.name]
    values = {sample.labels[label]: sample.value for sample in found}
    assert len(values) == len(found)
    return values


def single(samples: Families, metric: AppMetric) -> float:
    (sample,) = samples[metric.name]
    assert sample.name == metric.name
    return sample.value


# The exposition's shape.


def test_the_exposition_has_exactly_the_documented_series(client: TestClient) -> None:
    text = exposition(client)
    types, samples = parse(text)

    assert types == {metric.name: metric.type for metric in APP_METRICS}
    assert "_created" not in text
    # The fixed total of ADR 0028, plus the two Loki query series of ADR 0029, the two
    # live stream series of ADR 0032 and the four Prometheus query series of ADR 0035.
    assert sum(len(found) for found in samples.values()) == 71
    for metric in APP_METRICS:
        assert samples[metric.name], metric.name
        for sample in samples[metric.name]:
            extra = {"le"} if sample.name == metric.name + "_bucket" else set()
            assert set(sample.labels) == set(metric.labels) | extra, sample
            assert sample.labels["host"] == HOST
            assert "session_id" not in sample.labels


def test_states_kinds_reasons_and_events_are_exported_with_zeros(client: TestClient) -> None:
    samples = scrape(client)

    assert by_label(samples, SESSIONS, "state") == dict.fromkeys(SESSION_STATES, 0)
    assert by_label(samples, BLOCKS_OPEN, "kind") == dict.fromkeys(BLOCK_KINDS, 0)
    assert by_label(samples, HOOK_EVENTS_DROPPED, "reason") == dict.fromkeys(DROP_REASONS, 0)
    assert by_label(samples, HOOK_EVENTS_RECEIVED, "event") == dict.fromkeys(EVENT_LABELS, 0)
    assert by_label(samples, LOKI_QUERIES, "result") == dict.fromkeys(LOKI_RESULTS, 0)
    assert by_label(samples, PROMETHEUS_QUERIES, "result") == dict.fromkeys(PROMETHEUS_RESULTS, 0)
    for metric in (
        AGENTS_RUNNING,
        SESSIONS_ENDED,
        HOOK_EVENTS_PROCESSED,
        HOOK_QUEUE_DEPTH,
        HOOK_QUEUE_BYTES,
        UI_STREAMS,
        UI_STREAMS_REFUSED,
    ):
        assert single(samples, metric) == 0, metric.name
    histogram = samples[BLOCK_DURATION.name]
    buckets = [s for s in histogram if s.name == BLOCK_DURATION.name + "_bucket"]
    assert {s.labels["kind"] for s in histogram} == set(BLOCK_KINDS)
    assert len(buckets) == len(BLOCK_KINDS) * (len(BLOCK_DURATION_BUCKETS) + 1)
    assert all(sample.value == 0 for sample in histogram)


def test_build_info_carries_the_version(client: TestClient) -> None:
    (sample,) = scrape(client)[BUILD_INFO.name]

    assert sample.value == 1
    assert sample.labels == {"host": HOST, "version": ccobs.__version__}


def test_the_database_runs_in_wal_mode(client: TestClient) -> None:
    assert single(scrape(client), DATABASE_WAL) == 1


def test_a_database_without_wal_shows_up(
    settings: Settings, queue: HookQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ccobs.server.app.wal_enabled", lambda engine: False)
    app = create_app(settings, queue=queue, writer_factory=manual, serve_metrics=False)
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        assert single(scrape(test_client), DATABASE_WAL) == 0


# The values match the database.


def test_the_gauges_and_counters_match_the_database(replayed: TestClient) -> None:
    samples = scrape(replayed)
    with engine_of(replayed).connect() as connection:
        statuses = dict(
            connection.execute(
                select(SessionModel.status, func.count()).group_by(SessionModel.status)
            ).all()
        )
        open_blocks = dict(
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
        events = connection.execute(select(func.count()).select_from(HookEventModel)).scalar_one()

    assert by_label(samples, SESSIONS, "state") == {
        state: statuses.get(state, 0) for state in SESSION_STATES
    }
    assert by_label(samples, BLOCKS_OPEN, "kind") == {
        kind: open_blocks.get(kind, 0) for kind in BLOCK_KINDS
    }
    assert single(samples, AGENTS_RUNNING) == running
    assert single(samples, HOOK_EVENTS_PROCESSED) == events
    # Each replayed session ends once; the waiting one is still open.
    assert single(samples, SESSIONS_ENDED) == statuses["ended"]
    # The fixtures exercise the series: a waiting session, an open permission block.
    assert statuses["waiting_user"] >= 1
    assert open_blocks["permission"] >= 1
    assert events == len(replay_lines())
    assert single(samples, HOOK_QUEUE_DEPTH) == 0
    assert single(samples, HOOK_QUEUE_BYTES) == 0


def test_the_histogram_matches_the_stored_block_durations(replayed: TestClient) -> None:
    samples = scrape(replayed)[BLOCK_DURATION.name]
    with engine_of(replayed).connect() as connection:
        closed = connection.execute(
            select(BlockModel.kind, BlockModel.opened_at_ms, BlockModel.closed_at_ms).where(
                BlockModel.closed_at_ms.is_not(None)
            )
        ).all()
    durations: dict[str, list[float]] = {kind: [] for kind in BLOCK_KINDS}
    for kind, opened_at, closed_at in closed:
        assert closed_at is not None
        durations[kind].append((closed_at - opened_at) / 1000)

    for kind in BLOCK_KINDS:
        of_kind = [sample for sample in samples if sample.labels["kind"] == kind]
        values = {
            (sample.name.removeprefix(BLOCK_DURATION.name), sample.labels.get("le")): sample.value
            for sample in of_kind
        }
        assert values[("_count", None)] == len(durations[kind])
        assert values[("_sum", None)] == pytest.approx(sum(durations[kind]))
        for bound in BLOCK_DURATION_BUCKETS:
            expected = sum(1 for seconds in durations[kind] if seconds <= bound)
            assert values[("_bucket", str(float(bound)))] == expected
        assert values[("_bucket", "+Inf")] == len(durations[kind])
    # Both kinds closed at least once in the fixtures.
    assert all(durations[kind] for kind in BLOCK_KINDS)


def test_the_queue_gauges_follow_the_queue(client: TestClient, clock: Clock) -> None:
    body = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"}
    post(client, clock, [(0, body), (1, body)])

    samples = scrape(client)
    assert single(samples, HOOK_QUEUE_DEPTH) == 2
    assert single(samples, HOOK_QUEUE_BYTES) == 2 * len(json.dumps(body).encode())

    persist(client)
    samples = scrape(client)
    assert single(samples, HOOK_QUEUE_DEPTH) == 0
    assert single(samples, HOOK_QUEUE_BYTES) == 0


# Received and dropped hooks.


def test_received_hooks_are_counted_by_event_with_other_for_the_rest(
    client: TestClient, clock: Clock
) -> None:
    # Parsed but not registered by ccobs setup, and unknown to the parser.
    unregistered = ("InstructionsLoaded", "TaskCreated", "MadeUpEvent")
    names = [*RECEIVED_EVENTS, RECEIVED_EVENTS[0], *unregistered]
    lines = [
        (index, {"session_id": SESSION, "hook_event_name": name})
        for index, name in enumerate(names)
    ]
    post(client, clock, lines)
    # Not a hook payload: dropped as invalid, never received.
    client.post(HOOK_PATH, content=b"{not json", headers=JSON)
    persist(client)

    received = by_label(scrape(client), HOOK_EVENTS_RECEIVED, "event")
    expected = dict.fromkeys(RECEIVED_EVENTS, 1.0)
    expected[RECEIVED_EVENTS[0]] = 2
    expected[OTHER_EVENT] = len(unregistered)
    assert received == expected
    assert set(received) == set(EVENT_LABELS)


def test_each_drop_reason_is_counted(
    settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One item fits: the second valid hook finds the queue full.
    queue = HookQueue(1, settings.hook_queue_max_bytes)
    app = create_app(settings, queue=queue, clock=clock, writer_factory=manual, serve_metrics=False)
    body = json.dumps({"session_id": SESSION, "hook_event_name": "Stop"}).encode()
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        assert client.post(HOOK_PATH, content=b"{not json", headers=JSON).status_code == 204
        client.post(HOOK_PATH, content=body, headers=JSON)  # full
        persist(client)  # invalid
        too_large = b" " * (settings.hook_body_max_bytes + 1)
        client.post(HOOK_PATH, content=too_large, headers=JSON)
        client.post(HOOK_PATH, content=body, headers={"content-type": "text/plain"})
        client.post(HOOK_PATH, content=body, headers={**JSON, "host": "evil.example"})
        client.get(HOOK_PATH)  # bad_request

        def broken(document: object) -> object:
            raise RuntimeError("a parser bug")

        monkeypatch.setattr("ccobs.server.writer.parse_hook", broken)
        client.post(HOOK_PATH, content=body, headers=JSON)
        persist(client)  # internal

        dropped = by_label(scrape(client), HOOK_EVENTS_DROPPED, "reason")

    assert dropped == dict.fromkeys(DROP_REASONS, 1)


# The listener.


def port_is_free(port: int) -> bool:
    """Whether a new socket can bind ``port`` on loopback.

    SO_REUSEADDR on POSIX only, to ignore the TIME_WAIT of closed connections; it still
    refuses a port someone listens on. On Windows it would let the bind share a live
    port, so the bind there goes without it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def listener_of(app: FastAPI) -> MetricsListener:
    listener = app.state.metrics_listener
    assert isinstance(listener, MetricsListener)
    return listener


def test_the_listener_serves_only_metrics(settings: Settings, queue: HookQueue) -> None:
    app = create_app(settings, queue=queue, writer_factory=manual, metrics_port=0)
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        port = listener_of(app).port
        assert port != 0
        base = f"http://127.0.0.1:{port}"

        response = httpx.get(base + METRICS_PATH)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        types, _ = parse(response.text)
        assert set(types) == {metric.name for metric in APP_METRICS}
        # No Host check there: any Host gets the counts (ADR 0028).
        assert httpx.get(base + METRICS_PATH, headers={"host": "ccobs-app:9464"}).is_success

        for path in ("/", "/metrics/", "/metricsx", HOOK_PATH, "/healthz", "/api/v1/sessions"):
            missing = httpx.get(base + path)
            assert missing.status_code == 404, path
            assert "ccobs_" not in missing.text

        # The public app never serves /metrics.
        assert client.get(METRICS_PATH).status_code == 404


def test_the_listener_binds_the_listen_host_and_frees_its_port_on_shutdown(
    settings: Settings, queue: HookQueue
) -> None:
    app = create_app(settings, queue=queue, writer_factory=manual, metrics_port=0)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        listener = listener_of(app)
        assert listener.host == settings.listen_host == "127.0.0.1"
        port = listener.port
        assert not port_is_free(port)
        assert httpx.get(f"http://127.0.0.1:{port}{METRICS_PATH}").is_success

    assert port_is_free(port)
    assert app.state.metrics_listener is None


def test_the_listener_stops_before_the_writer(settings: Settings, queue: HookQueue) -> None:
    seen: list[bool] = []
    ports: list[int] = []

    class Recorder:
        def start(self) -> None:
            pass

        def stop(self, timeout_s: float) -> None:
            seen.append(port_is_free(ports[0]))

        def submit_tick(self, now_ms: int) -> None:
            pass

        def submit_retention(self, cutoff_ms: int) -> None:
            pass

        def submit_telemetry_close(self, session_id: str, tool_use_id: str, at_ms: int) -> None:
            pass

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

    def factory(hooks: HookQueue, repository: Repository, observers: WriterObservers) -> Recorder:
        return Recorder()

    app = create_app(settings, queue=queue, writer_factory=factory, metrics_port=0)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        ports.append(listener_of(app).port)

    assert seen == [True]


def test_the_listener_listens_on_the_configured_port_by_default(
    settings: Settings, queue: HookQueue
) -> None:
    with socket.socket() as finder:
        finder.bind(("127.0.0.1", 0))
        free = finder.getsockname()[1]
    configured = settings.model_copy(update={"metrics_port": free})
    app = create_app(configured, queue=queue, writer_factory=manual)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert listener_of(app).port == free


def test_a_listener_never_started_still_frees_its_port() -> None:
    listener = MetricsListener(AppMetrics(HOST, HookQueue(1, 1)).registry, "127.0.0.1", 0)
    port = listener.port
    assert not port_is_free(port)

    listener.stop()

    assert port_is_free(port)


def test_the_listener_can_be_turned_off(client: TestClient) -> None:
    assert app_of(client).state.metrics_listener is None
    # The registry is still there for tests and later stages.
    assert isinstance(app_of(client).state.metrics, AppMetrics)


def test_a_busy_metrics_port_leaves_the_app_running(
    settings: Settings,
    queue: HookQueue,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        app = create_app(
            settings, queue=queue, clock=clock, writer_factory=manual, metrics_port=port
        )
        with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
            assert app.state.metrics_listener is None
            post(client, clock, [(0, {"session_id": SESSION, "hook_event_name": "Stop"})])
            assert client.get("/healthz").status_code == 200

    assert f"metrics listener could not start on port {port}" in caplog.text


def test_the_listener_names_no_software_version(settings: Settings, queue: HookQueue) -> None:
    app = create_app(settings, queue=queue, writer_factory=manual, metrics_port=0)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        port = listener_of(app).port
        for path in (METRICS_PATH, "/nope"):
            server = httpx.get(f"http://127.0.0.1:{port}{path}").headers.get_list("server")
            assert server == [SERVER_NAME], path
        # A request the handler refuses itself (over 100 headers: 431), before any app
        # code runs.
        with socket.create_connection(("127.0.0.1", port)) as raw:
            raw.sendall(b"GET /metrics HTTP/1.0\r\n" + b"X-A: b\r\n" * 101 + b"\r\n")
            answer = b""
            while chunk := raw.recv(4096):
                answer += chunk
    head = answer.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    assert head.startswith("HTTP/")
    assert f"Server: {SERVER_NAME}" in head
    assert "Python" not in head
    assert "WSGIServer" not in head


def registry() -> prometheus_client.CollectorRegistry:
    return AppMetrics(HOST, HookQueue(1, 1)).registry


def test_the_exclusive_bind_option_is_set_before_the_bind() -> None:
    # SO_REUSEADDR stands in for SO_EXCLUSIVEADDRUSE, which only Windows has: what is
    # checked here is that the listener sets the option it is given on its socket.
    listener = MetricsListener(registry(), "127.0.0.1", 0, exclusive_option=socket.SO_REUSEADDR)
    try:
        option = listener.listening_socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        assert option != 0
    finally:
        listener.stop()


def test_without_an_exclusive_option_the_listener_still_binds() -> None:
    listener = MetricsListener(registry(), "127.0.0.1", 0, exclusive_option=None)
    try:
        assert not port_is_free(listener.port)
    finally:
        listener.stop()


def test_the_exclusive_bind_option_is_windows_only() -> None:
    if sys.platform == "win32":
        assert EXCLUSIVE_BIND_OPTION == socket.SO_EXCLUSIVEADDRUSE
    else:
        assert EXCLUSIVE_BIND_OPTION is None


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is Windows only")
def test_on_windows_no_socket_can_share_the_listeners_port() -> None:
    listener = MetricsListener(registry(), "127.0.0.1", 0)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as intruder:
            # SO_REUSEADDR would let it share a port bound without the exclusive option.
            intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):  # noqa: PT011 - the error code varies
                intruder.bind(("127.0.0.1", listener.port))
    finally:
        listener.stop()


@pytest.mark.parametrize(
    ("host", "family"),
    [
        ("127.0.0.1", socket.AF_INET),
        ("0.0.0.0", socket.AF_INET),  # noqa: S104 - the value under test
        ("localhost", socket.AF_INET),
        ("::", socket.AF_INET6),
        ("::1", socket.AF_INET6),
    ],
)
def test_the_address_family_follows_the_listen_host(host: str, family: int) -> None:
    assert address_family(host) == family


def test_the_listener_logs_no_access_lines(
    settings: Settings, queue: HookQueue, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(settings, queue=queue, writer_factory=manual, metrics_port=0)
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        port = listener_of(app).port
        httpx.get(f"http://127.0.0.1:{port}{METRICS_PATH}")
        httpx.get(f"http://127.0.0.1:{port}/nope")

    captured = capsys.readouterr()
    assert "GET" not in captured.err + captured.out


# The constants come from the domain.


def test_no_metric_name_is_spelled_out_in_server_code() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in SERVER_SOURCES.rglob("*.py")}
    assert sources
    for path, text in sources.items():
        for metric in APP_METRICS:
            assert metric.name not in text, (path, metric.name)
        assert '"ccobs_' not in text, path
    # And the server does use them.
    assert "app_metrics" in sources[SERVER_SOURCES / "metrics.py"]


def test_before_the_writer_is_attached_only_its_series_are_missing() -> None:
    metrics = AppMetrics(HOST, HookQueue(1, 1))
    types, _ = parse(generate_latest(metrics.registry).decode())

    from_writer = {
        SESSIONS,
        SESSIONS_ENDED,
        BLOCKS_OPEN,
        AGENTS_RUNNING,
        HOOK_EVENTS_PROCESSED,
        HOOK_EVENTS_RECEIVED,
        DATABASE_WAL,
    }
    assert set(types) == {metric.name for metric in APP_METRICS if metric not in from_writer}


def test_the_samples_of_each_counter_are_cumulative(replayed: TestClient, clock: Clock) -> None:
    before = scrape(replayed)
    post(replayed, clock, [(9 * HOUR_MS, {"session_id": SESSION, "hook_event_name": "Stop"})])
    persist(replayed)
    after = scrape(replayed)

    for metric in (HOOK_EVENTS_PROCESSED, SESSIONS_ENDED):
        assert single(after, metric) >= single(before, metric)
    received_before = Counter(by_label(before, HOOK_EVENTS_RECEIVED, "event"))
    received_after = Counter(by_label(after, HOOK_EVENTS_RECEIVED, "event"))
    assert received_after - received_before == Counter({"Stop": 1})


def test_app_metrics_turn_created_series_off() -> None:
    # Even after something re-enabled them, as the flag is process-wide.
    enable: Callable[[], None] = prometheus_client.enable_created_metrics
    enable()
    metrics = AppMetrics(HOST, HookQueue(1, 1))
    metrics.observe_block_closed("permission", 2.0)

    text = generate_latest(metrics.registry).decode()
    assert "_created" not in text
    assert f'{BLOCK_DURATION.name}_count{{host="{HOST}",kind="permission"}} 1.0' in text


def test_prometheus_queries_are_counted_by_result() -> None:
    # The usage reader's on_query (ADR 0035); every result exported, with zeros.
    metrics = AppMetrics(HOST, HookQueue(1, 1))
    for result in ("ok", "ok", "timeout", "invalid"):
        metrics.count_prometheus_query(result)

    _, samples = parse(generate_latest(metrics.registry).decode())

    assert by_label(samples, PROMETHEUS_QUERIES, "result") == {
        "ok": 2,
        "error": 0,
        "timeout": 1,
        "invalid": 1,
    }
