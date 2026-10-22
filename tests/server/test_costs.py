"""The cost reader (``ccobs.server.costs``): the cost rules' samples from Prometheus
(ADRs 0041 and 0043).

The unit tests drive ``CostReader.poll_once`` and ``run`` on the test thread with a
fake writer, a fake clock, a fake wait and an ``httpx.MockTransport`` that stands in for
Prometheus. The end-to-end tests read a sample into a real writer, which fires the
alert at its next tick, and run the reader in the app's lifespan.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from ccobs.domain.alerts import (
    AlertConfig,
    CostSample,
    DailyCostRule,
    SessionCostRule,
    daily_cost_query,
    session_cost_query,
)
from ccobs.domain.app_metrics import PROMETHEUS_QUERIES, PrometheusQueryResult
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.costs import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    HTTP_TIMEOUT_S,
    MAX_ANSWER_BYTES,
    MIN_POLL_INTERVAL_S,
    POLL_INTERVAL_S,
    QUERY_PATH,
    CostReader,
)
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue, RawHook
from ccobs.server.metrics import AppMetrics
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.sweeps import AlertTicker
from ccobs.server.writer import Writer, WriterObservers

HOST: Final = "test-host"
PROMETHEUS_URL: Final = "http://prometheus.test:9090"
NOW_MS: Final = 1_800_000_000_000
SECOND: Final = 1000
SESSION: Final = "00000000-0000-4000-8000-0000000000a1"
OTHER: Final = "00000000-0000-4000-8000-0000000000a2"
SINCE_MS: Final = NOW_MS - 3_600_000
GOOD_HOST: Final = "127.0.0.1:4300"
PATIENCE_S: Final = 10.0
HOUR_MS: Final = 3_600_000
SESSION_ONLY: Final = AlertConfig(session_cost=SessionCostRule(enabled=True))
DAILY_ONLY: Final = AlertConfig(daily_cost=DailyCostRule(enabled=True))
BOTH: Final = AlertConfig(
    session_cost=SessionCostRule(enabled=True), daily_cost=DailyCostRule(enabled=True)
)

type Document = dict[str, object]


def vector(*series: tuple[dict[str, str], str]) -> Document:
    """Prometheus's answer to an instant query: ``(labels, value)`` per series."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [NOW_MS / SECOND, value]} for labels, value in series
            ],
        },
    }


SESSIONS_ANSWER: Final = vector(({"session_id": SESSION}, "12.5"), ({"session_id": OTHER}, "0.25"))
DAILY_ANSWER: Final = vector(({}, "60"))


class Prometheus:
    """A MockTransport that answers each query by its kind (session or daily) from a
    script per kind: each item is a response, or an exception to raise; the last one
    repeats. It records every request."""

    def __init__(
        self,
        *,
        sessions: list[httpx.Response | Exception] | None = None,
        daily: list[httpx.Response | Exception] | None = None,
    ) -> None:
        self.scripts: dict[str, list[httpx.Response | Exception]] = {
            "sessions": sessions or [httpx.Response(200, json=SESSIONS_ANSWER)],
            "daily": daily or [httpx.Response(200, json=DAILY_ANSWER)],
        }
        self.requests: list[httpx.Request] = []
        self.closed = False
        self.seen = threading.Event()

    @property
    def queries(self) -> list[str]:
        return [request.url.params["query"] for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.seen.set()
        kind = "sessions" if "by (session_id)" in request.url.params["query"] else "daily"
        script = self.scripts[kind]
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        try:
            content = item.content
        except httpx.ResponseNotRead:
            return item  # a streamed body, used once
        # A fresh response each time: the client closes the one it reads.
        return httpx.Response(item.status_code, headers=item.headers, content=content)

    def transport(self) -> httpx.MockTransport:
        prometheus = self

        class Transport(httpx.MockTransport):
            def close(self) -> None:
                prometheus.closed = True

        return Transport(self.handle)


class Costs:
    """The writer as the cost reader sees it: its rules, its oldest open session, and
    the samples submitted."""

    def __init__(self, config: AlertConfig = BOTH, since: int | None = SINCE_MS) -> None:
        self.config = config
        self.since = since
        self.since_calls = 0
        self.samples: list[CostSample] = []
        self.fail: Exception | None = None

    def alert_config(self) -> AlertConfig:
        if self.fail is not None:
            raise self.fail
        return self.config

    def session_cost_since(self) -> int | None:
        self.since_calls += 1
        return self.since

    def submit_cost_sample(self, sample: CostSample) -> None:
        self.samples.append(sample)


class FakeTime:
    """A wall clock only the fake wait moves; the wait stops after ``rounds``."""

    def __init__(self, rounds: int, now: int = NOW_MS) -> None:
        self.now = now
        self.rounds = rounds
        self.waits: list[float] = []

    def clock(self) -> int:
        return self.now

    def wait(self, timeout_s: float) -> bool:
        if len(self.waits) == self.rounds:
            return True
        self.waits.append(timeout_s)
        self.now += int(timeout_s * SECOND)
        return False


class Results:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, result: PrometheusQueryResult) -> None:
        self.counts[result] += 1


def make(
    writer: Costs,
    prometheus: Prometheus,
    *,
    fake: FakeTime | None = None,
    results: Results | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    max_answer_bytes: int = MAX_ANSWER_BYTES,
) -> CostReader:
    return CostReader(
        writer,
        prometheus_url=PROMETHEUS_URL,
        host=HOST,
        clock=fake.clock if fake else (lambda: NOW_MS),
        wait=fake.wait if fake else None,
        monotonic=monotonic,
        max_answer_bytes=max_answer_bytes,
        transport=prometheus.transport(),
        on_query=results,
    )


def assert_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Logs never hold the URL, a query, a session ID or an answer."""
    for forbidden in ("prometheus.test", "/api/v1", "query=", "increase", SESSION, "12.5"):
        assert forbidden not in caplog.text, forbidden


def test_the_defaults() -> None:
    assert POLL_INTERVAL_S == MIN_POLL_INTERVAL_S == 60.0
    assert HTTP_TIMEOUT_S == 3.0
    assert MAX_ANSWER_BYTES == 8 * 1024 * 1024
    assert (BACKOFF_INITIAL_S, BACKOFF_MAX_S) == (60.0, 600.0)
    assert QUERY_PATH == "/api/v1/query"
    reader = CostReader(Costs(), prometheus_url=PROMETHEUS_URL, host=HOST)
    try:
        assert reader.poll_interval_s == POLL_INTERVAL_S
        assert reader.prometheus_url == PROMETHEUS_URL
        assert reader.host == HOST
    finally:
        reader.stop(1.0)


def _reader_with(name: str, value: float, **more: float) -> CostReader:
    """A reader with one setting (and, for consistency, ``more``) changed."""
    settings = {
        "poll_interval_s": POLL_INTERVAL_S,
        "timeout_s": HTTP_TIMEOUT_S,
        "backoff_initial_s": BACKOFF_INITIAL_S,
        "backoff_max_s": BACKOFF_MAX_S,
    }
    settings[name] = value
    settings.update(more)
    return CostReader(
        Costs(),
        prometheus_url=PROMETHEUS_URL,
        host=HOST,
        poll_interval_s=settings["poll_interval_s"],
        timeout_s=settings["timeout_s"],
        backoff_initial_s=settings["backoff_initial_s"],
        backoff_max_s=settings["backoff_max_s"],
        max_answer_bytes=int(value) if name == "max_answer_bytes" else MAX_ANSWER_BYTES,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("poll_interval_s", 59.999),
        ("poll_interval_s", 0.0),
        ("backoff_initial_s", 59.0),
        ("backoff_max_s", 59.0),
        ("timeout_s", 0.0),
        ("max_answer_bytes", 0),
    ],
)
def test_invalid_settings_are_refused(name: str, value: float) -> None:
    with pytest.raises(ValueError, match=name):
        _reader_with(name, value)


def test_a_slower_cadence_is_accepted() -> None:
    # The first back-off wait is at least the cadence, so it moves with it.
    reader = _reader_with("poll_interval_s", 90.0, backoff_initial_s=90.0)
    try:
        assert reader.poll_interval_s == 90.0
    finally:
        reader.stop(1.0)


# Which queries a round sends.


def test_no_query_while_no_cost_rule_is_enabled() -> None:
    writer, prometheus = Costs(config=AlertConfig()), Prometheus()
    reader = make(writer, prometheus)

    assert reader.poll_once() == "idle"

    assert prometheus.requests == []
    assert writer.samples == []
    assert writer.since_calls == 0


def test_no_query_without_an_open_session_when_only_the_session_rule_is_on() -> None:
    writer, prometheus = Costs(config=SESSION_ONLY, since=None), Prometheus()

    assert make(writer, prometheus).poll_once() == "idle"

    assert prometheus.requests == []
    assert writer.samples == []


def test_the_session_query_with_open_sessions() -> None:
    writer, prometheus, results = Costs(config=SESSION_ONLY), Prometheus(), Results()

    assert make(writer, prometheus, results=results).poll_once() == "ok"

    (request,) = prometheus.requests
    assert request.method == "GET"
    assert str(request.url.copy_with(query=None)) == f"{PROMETHEUS_URL}{QUERY_PATH}"
    assert request.url.params["query"] == session_cost_query(HOST, since_ms=SINCE_MS, now_ms=NOW_MS)
    assert request.url.params["time"] == "1800000000.000"
    assert writer.samples == [
        CostSample(observed_at_ms=NOW_MS, sessions={SESSION: 12.5, OTHER: 0.25}, daily_usd=None)
    ]
    assert results.counts == Counter({"ok": 1})


def test_the_daily_query_alone() -> None:
    writer, prometheus = Costs(config=DAILY_ONLY), Prometheus()

    assert make(writer, prometheus).poll_once() == "ok"

    assert prometheus.queries == [daily_cost_query(HOST)]
    # The session rule is off: its oldest open session is not even asked.
    assert writer.since_calls == 0
    assert writer.samples == [CostSample(observed_at_ms=NOW_MS, sessions={}, daily_usd=60.0)]


def test_the_daily_query_alone_when_no_session_is_open() -> None:
    writer, prometheus = Costs(config=BOTH, since=None), Prometheus()

    assert make(writer, prometheus).poll_once() == "ok"

    assert prometheus.queries == [daily_cost_query(HOST)]
    assert writer.samples == [CostSample(observed_at_ms=NOW_MS, sessions={}, daily_usd=60.0)]


def test_both_queries_make_one_sample() -> None:
    writer, prometheus, results = Costs(config=BOTH), Prometheus(), Results()

    assert make(writer, prometheus, results=results).poll_once() == "ok"

    assert prometheus.queries == [
        session_cost_query(HOST, since_ms=SINCE_MS, now_ms=NOW_MS),
        daily_cost_query(HOST),
    ]
    assert all(r.url.params["time"] == "1800000000.000" for r in prometheus.requests)
    assert writer.samples == [
        CostSample(observed_at_ms=NOW_MS, sessions={SESSION: 12.5, OTHER: 0.25}, daily_usd=60.0)
    ]
    assert results.counts == Counter({"ok": 2})


def test_an_empty_day_is_zero_and_series_without_a_session_are_left_out() -> None:
    writer = Costs(config=BOTH)
    prometheus = Prometheus(
        sessions=[httpx.Response(200, json=vector(({}, "3"), ({"session_id": SESSION}, "1")))],
        daily=[httpx.Response(200, json=vector())],
    )

    assert make(writer, prometheus).poll_once() == "ok"

    assert writer.samples == [
        CostSample(observed_at_ms=NOW_MS, sessions={SESSION: 1.0}, daily_usd=0.0)
    ]


def test_no_query_names_a_session() -> None:
    writer, prometheus = Costs(config=BOTH), Prometheus()

    make(writer, prometheus).poll_once()

    assert len(prometheus.requests) == 2
    for request in prometheus.requests:
        query = request.url.params["query"]
        for matcher in ("session_id=", "session_id!", "session_id =", "session_id!~"):
            assert matcher not in query
        assert SESSION not in str(request.url)
        assert set(request.url.params) == {"query", "time"}


def test_the_rules_are_read_at_every_round() -> None:
    writer, prometheus = Costs(config=AlertConfig()), Prometheus()
    reader = make(writer, prometheus)
    assert reader.poll_once() == "idle"

    writer.config = DAILY_ONLY
    assert reader.poll_once() == "ok"
    writer.config = AlertConfig()
    assert reader.poll_once() == "idle"

    assert len(prometheus.requests) == 1


# Failures: nothing submitted, the result counted, the type logged.


def _streamed(chunks: list[bytes]) -> httpx.Response:
    return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)))


FAILURES: Final[list[tuple[str, httpx.Response | Exception, str, str]]] = [
    ("no connection", httpx.ConnectError("down"), "error", "ConnectError"),
    ("a timeout", httpx.ReadTimeout("slow"), "timeout", "ReadTimeout"),
    ("an error status", httpx.Response(500, text=f"boom {SESSION}"), "error", "status 500"),
    (
        "a redirect, never followed",
        httpx.Response(302, headers={"location": "http://elsewhere.test/"}),
        "error",
        "status 302",
    ),
    ("not JSON", httpx.Response(200, content=b"<html>12.5</html>"), "invalid", "JSONDecodeError"),
    ("not UTF", httpx.Response(200, content=b"\xff\xfe\xfa"), "invalid", "Error"),
    # Nesting too deep for the decoder: RecursionError up to Python 3.13, JSONDecodeError
    # from 3.14; either way the answer is invalid and only the error's type is logged.
    ("too deep", httpx.Response(200, content=b"[" * 100_000), "invalid", "Error"),
    (
        "an error answer",
        httpx.Response(200, json={"status": "error", "error": f"bad {SESSION}"}),
        "invalid",
        "unexpected answer",
    ),
    (
        "a matrix",
        httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix"}}),
        "invalid",
        "unexpected answer",
    ),
    (
        "a NaN",
        httpx.Response(200, json=vector(({"session_id": SESSION}, "NaN"))),
        "invalid",
        "unexpected answer",
    ),
    (
        "a negative total",
        httpx.Response(200, json=vector(({"session_id": SESSION}, "-1"))),
        "invalid",
        "unexpected answer",
    ),
]


@pytest.mark.parametrize(
    ("answer", "result", "logged"),
    [pytest.param(answer, result, logged, id=name) for name, answer, result, logged in FAILURES],
)
def test_a_failed_session_query_submits_nothing(
    answer: httpx.Response | Exception,
    result: str,
    logged: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, results = Costs(config=BOTH), Results()
    prometheus = Prometheus(sessions=[answer])

    assert make(writer, prometheus, results=results).poll_once() == "error"

    assert writer.samples == []
    # The day is not asked once the sessions failed: the sample would be partial.
    assert len(prometheus.requests) == 1
    assert results.counts == Counter({result: 1})
    assert "prometheus cost query failed (" in caplog.text
    assert logged in caplog.text
    assert_quiet(caplog)


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        pytest.param(httpx.ConnectError("down"), "error", id="no connection"),
        pytest.param(httpx.ConnectTimeout("slow"), "timeout", id="a timeout"),
        pytest.param(httpx.Response(503), "error", id="an error status"),
        pytest.param(
            httpx.Response(200, json=vector(({}, "1"), ({"model": "x"}, "2"))),
            "invalid",
            id="a day of two series",
        ),
        pytest.param(
            httpx.Response(200, json=vector(({}, "Inf"))), "invalid", id="an infinite day"
        ),
    ],
)
def test_a_failed_daily_query_submits_nothing(
    answer: httpx.Response | Exception, result: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, results = Costs(config=BOTH), Results()
    prometheus = Prometheus(daily=[answer])

    assert make(writer, prometheus, results=results).poll_once() == "error"

    assert writer.samples == []
    assert len(prometheus.requests) == 2
    assert results.counts == Counter({"ok": 1, result: 1})
    assert_quiet(caplog)


def test_an_answer_past_the_size_cap_is_given_up(caplog: pytest.LogCaptureFixture) -> None:
    writer, results = Costs(config=SESSION_ONLY), Results()
    body = json.dumps(SESSIONS_ANSWER).encode()
    prometheus = Prometheus(sessions=[_streamed([body])])

    reader = make(writer, prometheus, results=results, max_answer_bytes=len(body) - 1)

    assert reader.poll_once() == "error"
    assert writer.samples == []
    assert results.counts == Counter({"invalid": 1})
    assert "answer too large" in caplog.text


def test_an_answer_that_trickles_past_the_whole_query_timeout_is_given_up() -> None:
    writer, results = Costs(config=SESSION_ONLY), Results()
    body = json.dumps(SESSIONS_ANSWER).encode()

    class Trickle(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for index in range(len(body)):
                yield body[index : index + 1]

    readings = iter(range(0, 10_000))

    def monotonic() -> float:
        # One second passes at every reading: the deadline falls mid-answer.
        return float(next(readings))

    prometheus = Prometheus(sessions=[httpx.Response(200, stream=Trickle())])
    reader = make(writer, prometheus, results=results, monotonic=monotonic)

    assert reader.poll_once() == "error"
    assert writer.samples == []
    assert results.counts == Counter({"timeout": 1})


def test_an_answer_inside_the_whole_query_timeout_is_read() -> None:
    writer = Costs(config=SESSION_ONLY)
    body = json.dumps(SESSIONS_ANSWER).encode()
    prometheus = Prometheus(sessions=[_streamed([body])])

    assert make(writer, prometheus, monotonic=lambda: 0.0).poll_once() == "ok"
    assert len(writer.samples) == 1


def test_a_failing_counter_does_not_stop_a_round() -> None:
    def broken(result: PrometheusQueryResult) -> None:
        raise RuntimeError(result)

    writer = Costs(config=BOTH)
    reader = CostReader(
        writer,
        prometheus_url=PROMETHEUS_URL,
        host=HOST,
        clock=lambda: NOW_MS,
        transport=Prometheus().transport(),
        on_query=broken,
    )

    assert reader.poll_once() == "ok"
    assert len(writer.samples) == 1
    reader.stop(1.0)


# The loop: cadence and back-off.


def test_the_loop_polls_every_minute_and_never_faster() -> None:
    writer, prometheus = Costs(config=BOTH), Prometheus()
    fake = FakeTime(rounds=3)

    make(writer, prometheus, fake=fake).run()

    assert fake.waits == [60.0, 60.0, 60.0]
    assert len(prometheus.requests) == 6
    assert [sample.observed_at_ms for sample in writer.samples] == [
        NOW_MS + 60 * SECOND,
        NOW_MS + 120 * SECOND,
        NOW_MS + 180 * SECOND,
    ]


def test_an_idle_loop_waits_a_minute_and_sends_nothing() -> None:
    writer, prometheus = Costs(config=AlertConfig()), Prometheus()
    fake = FakeTime(rounds=3)

    make(writer, prometheus, fake=fake).run()

    assert fake.waits == [60.0, 60.0, 60.0]
    assert prometheus.requests == []


def test_failures_back_off_up_to_the_cap_and_a_success_resets() -> None:
    down = httpx.ConnectError("down")
    ok = httpx.Response(200, json=SESSIONS_ANSWER)
    writer = Costs(config=SESSION_ONLY)
    prometheus = Prometheus(sessions=[down, down, down, down, down, ok, down])
    fake = FakeTime(rounds=8)

    make(writer, prometheus, fake=fake).run()

    # One failure retries at the poll interval, so a transient error doesn't age the
    # sample past the 180 s staleness; only consecutive failures wait longer.
    assert fake.waits == [60.0, 60.0, 120.0, 240.0, 480.0, 600.0, 60.0, 60.0]
    assert min(fake.waits) >= MIN_POLL_INTERVAL_S
    assert len(writer.samples) == 1


def test_a_failing_writer_or_clock_is_logged_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, prometheus = Costs(config=BOTH), Prometheus()
    writer.fail = RuntimeError(f"broken {SESSION}")
    fake = FakeTime(rounds=3)

    make(writer, prometheus, fake=fake).run()

    # Not Prometheus's doing: no back-off.
    assert fake.waits == [60.0, 60.0, 60.0]
    assert prometheus.requests == []
    assert "cost round failed (RuntimeError)" in caplog.text
    assert_quiet(caplog)


# Thread control.


def test_stop_is_prompt_and_closes_the_client() -> None:
    prometheus = Prometheus()
    reader = make(Costs(), prometheus)
    reader.start()
    assert reader.is_alive()
    with pytest.raises(RuntimeError, match="already started"):
        reader.start()

    started = time.monotonic()
    reader.stop(5.0)

    assert time.monotonic() - started < 3.0  # prompt, with room for a slow CI runner
    assert not reader.is_alive()
    assert prometheus.closed
    assert prometheus.requests == []  # stopped within its first minute


def test_stop_without_start_closes_the_client() -> None:
    prometheus = Prometheus()
    reader = make(Costs(), prometheus)

    reader.stop(1.0)

    assert not reader.is_alive()
    assert prometheus.closed


def test_a_stop_that_times_out_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    release = threading.Event()
    entered = threading.Event()

    def stuck(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(PATIENCE_S)
        return httpx.Response(200, json=DAILY_ANSWER)

    waits = iter([False])

    def wait(timeout_s: float) -> bool:
        return next(waits, True)

    reader = CostReader(
        Costs(config=DAILY_ONLY),
        prometheus_url=PROMETHEUS_URL,
        host=HOST,
        clock=lambda: NOW_MS,
        wait=wait,
        transport=httpx.MockTransport(stuck),
    )
    reader.start()
    assert entered.wait(PATIENCE_S)
    reader.stop(0.2)
    release.set()

    assert "cost reader did not stop within 0.2 s" in caplog.text


# End to end: a real writer, and the app's lifespan.


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


def run_all(writer: Writer) -> None:
    while writer.run_once(0.0):
        pass


def test_a_sample_read_into_the_writer_fires_at_its_next_tick(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    repository = Repository(engine)
    queue = HookQueue(max_items=100, max_bytes=1024 * 1024)
    writer = ManualWriter(
        queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0.0, alert_config=BOTH
    )
    started = NOW_MS - 10 * 60 * SECOND
    for seq, session_id in enumerate((SESSION, OTHER), start=1):
        body = json.dumps({"session_id": session_id, "hook_event_name": "UserPromptSubmit"})
        assert queue.offer(RawHook(body.encode(), started + seq, seq))
    run_all(writer)
    prometheus = Prometheus()
    reader = CostReader(
        writer,
        prometheus_url=PROMETHEUS_URL,
        host=HOST,
        clock=lambda: NOW_MS,
        transport=prometheus.transport(),
    )
    try:
        assert reader.poll_once() == "ok"
    finally:
        reader.stop(1.0)
    run_all(writer)
    writer.submit_alert_tick(NOW_MS + 5 * SECOND)
    run_all(writer)

    # The session query's range starts at the oldest open session (SESSION).
    assert prometheus.queries[0] == session_cost_query(HOST, since_ms=started + 1, now_ms=NOW_MS)
    fired = sorted((a.rule, a.subject, a.value) for a in repository.list_alerts(HOST, 10))
    assert fired == [("daily_cost", "day", 60.0), ("session_cost", SESSION, 12.5)]
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(
        data_dir=tmp_path / "data",
        host_label=HOST,
        prometheus_url=PROMETHEUS_URL,
        alert_daily_cost_enabled=True,
    )


def manual(queue: HookQueue, repository: Repository, observers: WriterObservers) -> ManualWriter:
    return ManualWriter(
        queue,
        repository,
        HOST,
        stale_after_ms=12 * HOUR_MS,
        batch_wait_s=0,
        alert_config=AlertConfig(daily_cost=DailyCostRule(enabled=True)),
        on_block_closed=observers.on_block_closed,
        on_committed=observers.on_committed,
    )


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def test_the_app_runs_the_cost_reader_and_counts_its_queries(settings: Settings) -> None:
    prometheus = Prometheus(
        daily=[httpx.ConnectError("down"), httpx.Response(200, json=DAILY_ANSWER)]
    )
    app = create_app(
        settings,
        writer_factory=manual,
        serve_metrics=False,
        reconcile=False,
        cost_transport=prometheus.transport(),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        reader = app_of(client).state.cost_reader
        assert isinstance(reader, CostReader)
        assert reader.is_alive()
        assert reader.poll_interval_s == POLL_INTERVAL_S
        assert reader.prometheus_url == PROMETHEUS_URL
        assert reader.host == HOST
        # Its thread waits a minute first; a round run here goes through the app's
        # writer, transport and counter.
        assert reader.poll_once() == "error"
        assert reader.poll_once() == "ok"
        metrics = app_of(client).state.metrics
        assert isinstance(metrics, AppMetrics)
        text = generate_latest(metrics.registry).decode()
    assert app_of(client).state.cost_reader is None
    assert not reader.is_alive()
    assert prometheus.closed
    assert prometheus.queries == [daily_cost_query(HOST)] * 2
    assert f'{PROMETHEUS_QUERIES.name}{{host="{HOST}",result="error"}} 1.0' in text
    assert f'{PROMETHEUS_QUERIES.name}{{host="{HOST}",result="ok"}} 1.0' in text


def test_create_app_without_sweeps_leaves_the_cost_reader_off(settings: Settings) -> None:
    prometheus = Prometheus()
    app = create_app(
        settings,
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        cost_transport=prometheus.transport(),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as client:
        assert app_of(client).state.cost_reader is None
    assert prometheus.requests == []


def test_the_lifespan_starts_the_cost_reader_after_the_ticker_and_stops_it_first(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def record[T](
        owner: type[T], name: str, start: Callable[[T], None], stop: Callable[[T, float], None]
    ) -> None:
        def started(self: T) -> None:
            order.append(f"{name} start")
            start(self)

        def stopped(self: T, timeout_s: float) -> None:
            order.append(f"{name} stop")
            stop(self, timeout_s)

        monkeypatch.setattr(owner, "start", started)
        monkeypatch.setattr(owner, "stop", stopped)

    record(AlertTicker, "ticker", AlertTicker.start, AlertTicker.stop)
    record(CostReader, "costs", CostReader.start, CostReader.stop)
    app = create_app(settings, writer_factory=manual, serve_metrics=False, reconcile=False)

    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert order == ["ticker start", "costs start"]

    assert order[2:] == ["costs stop", "ticker stop"]
