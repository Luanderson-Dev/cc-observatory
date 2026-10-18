"""The usage reader: a session's tokens and cost from Prometheus (``ccobs.server.usage``).

Prometheus is an ``httpx.MockTransport``: the tests check the three instant queries
(``ccobs.domain.usage``) and their evaluation time, each failure mode and its result on
the ``ccobs_prometheus_queries_total`` counter (ok, error, timeout, invalid), the answer
size cap, the cache (TTL, LRU, final windows) and that logs never hold a session ID or a
URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Final, Literal, TypedDict, Unpack

import httpx
import pytest

from ccobs.domain.app_metrics import PrometheusQueryResult
from ccobs.domain.usage import SessionUsage, UsageWindow, usage_queries, usage_window
from ccobs.server.usage import (
    CACHE_SIZE,
    FINAL_TTL_S,
    HTTP_TIMEOUT_S,
    LIVE_TTL_S,
    MAX_ANSWER_BYTES,
    QUERY_PATH,
    UsageReader,
    UsageUnavailable,
)

PROMETHEUS: Final = "http://prometheus:9090"
HOST: Final = "test-host"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
OTHER: Final = "5e551011-aaaa-bbbb-cccc-000000000002"
STARTED: Final = 1_800_000_000_000
HOUR_MS: Final = 3_600_000
LIVE: Final = usage_window(STARTED, None, STARTED + HOUR_MS)
FINAL: Final = usage_window(STARTED, STARTED + HOUR_MS, STARTED + 2 * HOUR_MS)

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
type Reason = Literal["error", "timeout", "invalid"]


def vector(label: str, items: dict[str, str]) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {label: name}, "value": [1_800_003_600.0, value]}
                for name, value in items.items()
            ],
        },
    }


TOKENS_BY_TYPE: Final = vector("type", {"input": "100", "output": "20", "cacheRead": "3000"})
TOKENS_BY_MODEL: Final = vector("model", {"claude-haiku-5-5": "3120"})
COST_BY_MODEL: Final = vector("model", {"claude-haiku-5-5": "0.0123"})


class Prometheus:
    """A fake Prometheus: answers each query from ``answers``, keyed by which of the
    three it is, and records every request."""

    def __init__(self, respond: Callable[[httpx.Request, str], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self.respond = respond or self.success

    @staticmethod
    def kind(request: httpx.Request) -> str:
        query = request.url.params["query"]
        if query.startswith("sum by (type)"):
            return "tokens_by_type"
        if "cost" in query:
            return "cost_by_model"
        return "tokens_by_model"

    @staticmethod
    def success(request: httpx.Request, kind: str) -> httpx.Response:
        body = {
            "tokens_by_type": TOKENS_BY_TYPE,
            "tokens_by_model": TOKENS_BY_MODEL,
            "cost_by_model": COST_BY_MODEL,
        }[kind]
        return httpx.Response(200, json=body)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request, self.kind(request))


class Options(TypedDict, total=False):
    """The reader's tunable keyword arguments that the tests set."""

    timeout_s: float
    max_answer_bytes: int
    cache_size: int
    live_ttl_s: float
    final_ttl_s: float
    monotonic: Callable[[], float]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def run[T](
    prometheus: Prometheus | Handler,
    body: Callable[[UsageReader], Awaitable[T]],
    **options: Unpack[Options],
) -> tuple[T, list[PrometheusQueryResult]]:
    """``body(reader)`` on a fresh loop, with the reader closed after; the results
    counted."""
    counted: list[PrometheusQueryResult] = []
    handler = prometheus.handle if isinstance(prometheus, Prometheus) else prometheus

    async def main() -> T:
        reader = UsageReader(
            prometheus_url=PROMETHEUS,
            host=HOST,
            transport=httpx.MockTransport(handler),
            on_query=counted.append,
            **options,
        )
        try:
            return await body(reader)
        finally:
            await reader.aclose()

    return asyncio.run(main()), counted


def read_once(
    prometheus: Prometheus | Handler, window: UsageWindow = LIVE, **options: Unpack[Options]
) -> tuple[SessionUsage | UsageUnavailable, list[PrometheusQueryResult]]:
    return run(prometheus, lambda reader: reader.read(SID, window), **options)


# The defaults.


def test_the_defaults() -> None:
    assert HTTP_TIMEOUT_S == 3.0
    assert MAX_ANSWER_BYTES == 1024 * 1024
    assert CACHE_SIZE == 64
    assert LIVE_TTL_S == 30.0
    assert FINAL_TTL_S == 300.0
    assert QUERY_PATH == "/api/v1/query"


@pytest.mark.parametrize(
    "bounds",
    [
        Options(timeout_s=0),
        Options(max_answer_bytes=0),
        Options(cache_size=0),
        Options(live_ttl_s=0),
        Options(final_ttl_s=0),
    ],
    ids=lambda bounds: next(iter(bounds)),
)
def test_every_bound_must_be_positive(bounds: Options) -> None:
    with pytest.raises(ValueError, match=next(iter(bounds))):
        UsageReader(prometheus_url=PROMETHEUS, host=HOST, **bounds)


# A successful read.


def test_three_instant_queries_give_the_sessions_usage() -> None:
    prometheus = Prometheus()

    usage, counted = read_once(prometheus)

    assert isinstance(usage, SessionUsage)
    assert usage.tokens_by_type == (("input", 100.0), ("output", 20.0), ("cacheRead", 3000.0))
    assert usage.tokens_by_model == (("claude-haiku-5-5", 3120.0),)
    assert usage.cost_by_model == (("claude-haiku-5-5", 0.0123),)
    assert counted == ["ok", "ok", "ok"]


def test_the_queries_are_the_domains_at_the_windows_time() -> None:
    prometheus = Prometheus()

    read_once(prometheus, FINAL)

    queries = usage_queries(HOST, SID, FINAL)
    expected = {queries.tokens_by_type, queries.tokens_by_model, queries.cost_by_model}
    assert {request.url.params["query"] for request in prometheus.requests} == expected
    for request in prometheus.requests:
        assert request.method == "GET"
        assert f"{request.url.scheme}://{request.url.netloc.decode()}" == PROMETHEUS
        assert request.url.path == QUERY_PATH
        assert dict(request.url.params) == {
            "query": request.url.params["query"],
            "time": FINAL.time_s,
        }


def test_a_url_with_a_path_prefix_keeps_it() -> None:
    prometheus = Prometheus()

    async def main() -> None:
        reader = UsageReader(
            prometheus_url="http://proxy/prometheus",
            host=HOST,
            transport=httpx.MockTransport(prometheus.handle),
        )
        await reader.read(SID, LIVE)
        await reader.aclose()

    asyncio.run(main())

    assert {request.url.path for request in prometheus.requests} == {"/prometheus/api/v1/query"}


def test_an_empty_answer_is_an_empty_usage() -> None:
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    prometheus = Prometheus(lambda request, kind: httpx.Response(200, json=empty))

    usage, counted = read_once(prometheus)

    assert isinstance(usage, SessionUsage)
    assert usage.empty
    assert counted == ["ok", "ok", "ok"]


# Failures.


def answer(status: int = 200, content: bytes = b"", **headers: str) -> Prometheus:
    def respond(request: httpx.Request, kind: str) -> httpx.Response:
        return httpx.Response(status, content=content, headers=headers)

    return Prometheus(respond)


MATRIX: Final = b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
NAN: Final = (
    b'{"status":"success","data":{"resultType":"vector",'
    b'"result":[{"metric":{},"value":[1,"NaN"]}]}}'
)


@pytest.mark.parametrize(
    ("prometheus", "reason"),
    [
        (answer(500, b'{"status":"error"}'), "error"),
        (answer(400, b'{"status":"error","errorType":"bad_data"}'), "error"),
        (answer(302, location="http://elsewhere/"), "error"),
        (answer(200, b"not json"), "invalid"),
        # A UTF-16 byte order mark alone decodes to an empty string (JSONDecodeError);
        # with a stray byte after it, json.loads raises UnicodeDecodeError.
        (answer(200, b"\xff\xfe"), "invalid"),
        (answer(200, b"\xff\xfe\x00"), "invalid"),
        # Nesting too deep for the decoder: RecursionError.
        (answer(200, b"[" * 100_000 + b"]" * 100_000), "invalid"),
        (answer(200, MATRIX), "invalid"),
        (answer(200, NAN), "invalid"),
        (answer(200, b" " * (MAX_ANSWER_BYTES + 1)), "invalid"),
    ],
    ids=[
        "500",
        "400",
        "redirect",
        "not-json",
        "bom-only",
        "not-utf",
        "too-deep",
        "matrix",
        "nan",
        "oversized",
    ],
)
def test_a_bad_answer_makes_the_usage_unavailable(prometheus: Prometheus, reason: Reason) -> None:
    usage, counted = read_once(prometheus)

    assert usage == UsageUnavailable(reason=reason)
    assert counted == [reason] * 3


def test_an_endless_body_is_given_up_past_the_cap() -> None:
    chunk = 64 * 1024
    sent: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        while True:
            sent.append(chunk)
            yield b" " * chunk

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless())

    usage, counted = read_once(handler)

    assert usage == UsageUnavailable(reason="invalid")
    assert counted == ["invalid"] * 3
    # Each of the three queries stops one chunk past the cap (a little more if the
    # generator runs ahead of the reader), never reading on.
    per_query = MAX_ANSWER_BYTES // chunk + 1
    assert 3 * per_query <= len(sent) <= 3 * (per_query + 2)


def test_the_size_cap_applies_to_the_body_as_read() -> None:
    body = json.dumps(TOKENS_BY_TYPE).encode()

    usage, counted = read_once(answer(200, body), max_answer_bytes=len(body) - 1)

    assert usage == UsageUnavailable(reason="invalid")
    assert counted == ["invalid"] * 3


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError("refused"), "error"),
        (httpx.RemoteProtocolError("closed"), "error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
    ],
)
def test_a_transport_failure_makes_the_usage_unavailable(error: Exception, reason: Reason) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise error

    usage, counted = read_once(handler)

    assert usage == UsageUnavailable(reason=reason)
    assert counted == [reason] * 3


def test_a_whole_query_is_bounded_by_the_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=TOKENS_BY_TYPE)

    usage, counted = read_once(handler, timeout_s=0.05)

    assert usage == UsageUnavailable(reason="timeout")
    assert counted == ["timeout"] * 3


def test_one_failed_query_of_three_makes_the_usage_unavailable() -> None:
    def respond(request: httpx.Request, kind: str) -> httpx.Response:
        if kind == "cost_by_model":
            return httpx.Response(503)
        return Prometheus.success(request, kind)

    usage, counted = read_once(Prometheus(respond))

    assert usage == UsageUnavailable(reason="error")
    assert sorted(counted) == ["error", "ok", "ok"]


def test_failures_log_no_session_id_url_or_body(caplog: pytest.LogCaptureFixture) -> None:
    # httpx logs each request with its URL at INFO, below the WARNING the server keeps
    # for libraries (ccobs.server.__main__); the app's own records are checked.
    caplog.set_level(logging.DEBUG, logger="ccobs")

    for prometheus in (answer(500, b"secret-body"), answer(200, b"secret-body")):
        read_once(prometheus)

    ours = [record for record in caplog.records if record.name.startswith("ccobs")]
    text = "\n".join(record.getMessage() for record in ours)
    assert "prometheus query failed (status 500)" in text
    assert "prometheus query failed (JSONDecodeError)" in text
    for secret in (SID, "prometheus:9090", "http", "secret-body", "session_id", HOST):
        assert secret not in text
    assert all(record.exc_info is None for record in ours)


def test_a_failing_counter_does_not_break_the_read(caplog: pytest.LogCaptureFixture) -> None:
    def broken(result: PrometheusQueryResult) -> None:
        raise RuntimeError("counter")

    async def main() -> SessionUsage | UsageUnavailable:
        reader = UsageReader(
            prometheus_url=PROMETHEUS,
            host=HOST,
            transport=httpx.MockTransport(Prometheus().handle),
            on_query=broken,
        )
        try:
            return await reader.read(SID, LIVE)
        finally:
            await reader.aclose()

    assert isinstance(asyncio.run(main()), SessionUsage)
    assert "prometheus query not counted (RuntimeError)" in caplog.text


def test_without_a_counter_the_reader_still_reads() -> None:
    async def main() -> SessionUsage | UsageUnavailable:
        reader = UsageReader(
            prometheus_url=PROMETHEUS,
            host=HOST,
            transport=httpx.MockTransport(Prometheus().handle),
        )
        try:
            return await reader.read(SID, LIVE)
        finally:
            await reader.aclose()

    assert isinstance(asyncio.run(main()), SessionUsage)


# The cache.


def test_a_live_window_is_cached_for_its_ttl() -> None:
    prometheus = Prometheus()
    clock = Clock()

    async def body(reader: UsageReader) -> None:
        first = await reader.read(SID, LIVE)
        clock.now += LIVE_TTL_S - 1
        # Later, with a later now: the same live session hits the cache.
        later = usage_window(STARTED, None, STARTED + HOUR_MS + 29_000)
        assert await reader.read(SID, later) == first
        assert len(prometheus.requests) == 3
        clock.now += 1
        await reader.read(SID, later)
        assert len(prometheus.requests) == 6

    _, counted = run(prometheus, body, monotonic=clock)
    assert counted == ["ok"] * 6


def test_a_final_window_is_cached_longer() -> None:
    prometheus = Prometheus()
    clock = Clock()

    async def body(reader: UsageReader) -> None:
        await reader.read(SID, FINAL)
        clock.now += FINAL_TTL_S - 1
        await reader.read(SID, FINAL)
        assert len(prometheus.requests) == 3
        clock.now += 1
        await reader.read(SID, FINAL)
        assert len(prometheus.requests) == 6

    run(prometheus, body, monotonic=clock)


def test_a_session_that_turns_final_is_read_again() -> None:
    prometheus = Prometheus()

    async def body(reader: UsageReader) -> None:
        await reader.read(SID, LIVE)
        await reader.read(SID, FINAL)
        assert len(prometheus.requests) == 6

    run(prometheus, body, monotonic=Clock())


def test_sessions_are_cached_apart_and_the_least_recent_is_evicted() -> None:
    prometheus = Prometheus()
    third = "5e551011-aaaa-bbbb-cccc-000000000003"

    async def body(reader: UsageReader) -> None:
        await reader.read(SID, LIVE)
        await reader.read(OTHER, LIVE)
        assert len(prometheus.requests) == 6
        # SID is used again, so OTHER is the least recent when a third comes in.
        await reader.read(SID, LIVE)
        await reader.read(third, LIVE)
        assert len(prometheus.requests) == 9
        await reader.read(SID, LIVE)
        assert len(prometheus.requests) == 9
        await reader.read(OTHER, LIVE)
        assert len(prometheus.requests) == 12

    run(prometheus, body, monotonic=Clock(), cache_size=2)


def test_failures_are_not_cached() -> None:
    calls = {"count": 0}

    def respond(request: httpx.Request, kind: str) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] <= 3:
            return httpx.Response(503)
        return Prometheus.success(request, kind)

    async def body(reader: UsageReader) -> None:
        assert await reader.read(SID, LIVE) == UsageUnavailable(reason="error")
        assert isinstance(await reader.read(SID, LIVE), SessionUsage)

    run(Prometheus(respond), body, monotonic=Clock())


def test_a_hostile_session_id_is_quoted_in_the_query() -> None:
    prometheus = Prometheus()

    run(prometheus, lambda reader: reader.read('x"}) or vector(1) #', LIVE))

    for request in prometheus.requests:
        assert 'session_id="x\\"}) or vector(1) #"' in request.url.params["query"]


def test_aclose_closes_the_client() -> None:
    async def main() -> bool:
        reader = UsageReader(
            prometheus_url=PROMETHEUS,
            host=HOST,
            transport=httpx.MockTransport(Prometheus().handle),
        )
        await reader.aclose()
        return reader.closed

    assert asyncio.run(main()) is True
