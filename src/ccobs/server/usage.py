"""The usage reader: a session's tokens and cost, from Prometheus (ADR 0035).

The session page loads its "Tokens and cost" section from ``/ui/sessions/{id}/usage``,
which reads the session from the database first (through the read gate, ADR 0033) and
then calls ``UsageReader.read`` with the session's window (``ccobs.domain.usage``), so no
read slot is held while Prometheus answers. ``read`` sends the three instant queries of
``ccobs.domain.usage.usage_queries`` at once, as ``GET /api/v1/query`` with the window's
evaluation ``time``, on an ``httpx.AsyncClient`` of its own: on the event loop, never on
a thread, so a slow Prometheus holds no worker. The app's lifespan creates the reader on
its loop and closes it at shutdown.

Each query is bounded twice: httpx's timeout per phase, and ``timeout_s`` for the whole
query, so a Prometheus that trickles its answer still gives up in time. The answer is
read as a stream and given up past ``max_answer_bytes``. Each query counts its result
(``on_query``, ``AppMetrics.count_prometheus_query``): ``ok``, ``error`` (no connection,
a non-2xx status, a redirect, which is never followed), ``timeout``, or ``invalid`` (too
large, not JSON, or not a vector of finite, non-negative values). If any of the three
fails, the usage is unavailable, with the first failure's reason: the page never shows
part of a total.

A successful read is cached, at most ``cache_size`` sessions, the least recently used
evicted first, keyed by the session, its window's start and, once the window is final,
its end. A live session's totals are kept ``live_ttl_s`` (the page refreshes them every
minute, and Claude Code exports about as often), a final window's ``final_ttl_s``. A
failure is not cached: the next refresh asks again. Logs hold exception type names,
status codes and reasons only: never the URL (with its query), a session ID or a body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

import httpx

from ccobs.domain.app_metrics import PrometheusQueryResult
from ccobs.domain.usage import (
    SessionUsage,
    UsageWindow,
    Vector,
    parse_vector,
    session_usage,
    usage_queries,
)

__all__ = [
    "CACHE_SIZE",
    "FINAL_TTL_S",
    "HTTP_TIMEOUT_S",
    "LIVE_TTL_S",
    "MAX_ANSWER_BYTES",
    "QUERY_PATH",
    "UsageReader",
    "UsageUnavailable",
]

_log = logging.getLogger(__name__)

# The whole query, and each phase of it: a Prometheus on the same machine answers these
# in milliseconds, and the page must not wait long for a section it can do without.
HTTP_TIMEOUT_S: Final = 3.0
# An answer has one series per type or model: a few hundred bytes. 1 MiB is far beyond
# any real one.
MAX_ANSWER_BYTES: Final = 1024 * 1024
# Sessions whose totals are kept: more than the session pages one person keeps open.
CACHE_SIZE: Final = 64
# How long totals are kept: a live session's for about half the page's refresh interval
# (60 s), a final window's longer, since they no longer change.
LIVE_TTL_S: Final = 30.0
FINAL_TTL_S: Final = 300.0
QUERY_PATH: Final = "/api/v1/query"

type _Failure = Literal["error", "timeout", "invalid"]
# The session, its window's start, and its end once final (None while it can grow).
type _Key = tuple[str, int, int | None]


@dataclass(frozen=True, slots=True)
class UsageUnavailable:
    """Prometheus gave no usable answer; ``reason`` is the first failure's result."""

    reason: _Failure


class _QueryError(Exception):
    """A query that got no usable answer; its message is safe to log."""

    def __init__(self, reason: _Failure, detail: str) -> None:
        super().__init__(detail)
        self.reason: Final = reason


class UsageReader:
    """Reads sessions' tokens and cost from Prometheus, with a small cache.

    ``prometheus_url`` is ``CCOBS_PROMETHEUS_URL`` (no trailing slash) and ``host`` the
    host label the queries filter on. ``monotonic`` is the cache's clock; ``transport``
    replaces httpx's (tests pass an ``httpx.MockTransport``), and ``on_query`` receives
    the result of every query sent. Build it on the event loop that uses it.
    """

    def __init__(
        self,
        *,
        prometheus_url: str,
        host: str,
        timeout_s: float = HTTP_TIMEOUT_S,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        cache_size: int = CACHE_SIZE,
        live_ttl_s: float = LIVE_TTL_S,
        final_ttl_s: float = FINAL_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
        on_query: Callable[[PrometheusQueryResult], None] | None = None,
    ) -> None:
        for name, value in (
            ("timeout_s", timeout_s),
            ("max_answer_bytes", max_answer_bytes),
            ("cache_size", cache_size),
            ("live_ttl_s", live_ttl_s),
            ("final_ttl_s", final_ttl_s),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        self.host: Final = host
        self._url: Final = f"{prometheus_url}{QUERY_PATH}"
        self._timeout_s: Final = timeout_s
        self._max_answer_bytes: Final = max_answer_bytes
        self._cache_size: Final = cache_size
        self._live_ttl_s: Final = live_ttl_s
        self._final_ttl_s: Final = final_ttl_s
        self._monotonic = monotonic
        self._on_query = on_query
        self._cache: OrderedDict[_Key, tuple[float, SessionUsage]] = OrderedDict()
        # No proxy from the environment: Prometheus is on the compose network.
        self._client: Final = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, trust_env=False, follow_redirects=False
        )

    @property
    def closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def read(self, session_id: str, window: UsageWindow) -> SessionUsage | UsageUnavailable:
        """The session's usage over ``window``, from the cache or from Prometheus."""
        key: _Key = (session_id, window.start_ms, window.time_ms if window.final else None)
        now = self._monotonic()
        cached = self._cache.get(key)
        if cached is not None:
            expires, usage = cached
            if now < expires:
                self._cache.move_to_end(key)
                return usage
            del self._cache[key]
        queries = usage_queries(self.host, session_id, window)
        answers = await asyncio.gather(
            self._query(queries.tokens_by_type, "type", window),
            self._query(queries.tokens_by_model, "model", window),
            self._query(queries.cost_by_model, "model", window),
        )
        vectors: list[Vector] = []
        for answer in answers:
            if isinstance(answer, UsageUnavailable):
                return answer
            vectors.append(answer)
        tokens_by_type, tokens_by_model, cost_by_model = vectors
        usage = session_usage(tokens_by_type, tokens_by_model, cost_by_model)
        ttl = self._final_ttl_s if window.final else self._live_ttl_s
        self._cache[key] = (self._monotonic() + ttl, usage)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return usage

    async def _query(
        self, query: str, label: str, window: UsageWindow
    ) -> Vector | UsageUnavailable:
        """One instant query's vector, or why there is none; counted either way."""
        try:
            async with asyncio.timeout(self._timeout_s):
                document = await self._get(query, window)
            parsed = parse_vector(document, label)
            if parsed is None:
                raise _QueryError("invalid", "unexpected answer")
        except _QueryError as error:
            return self._failed(error.reason, str(error))
        except (TimeoutError, httpx.TimeoutException) as error:
            return self._failed("timeout", type(error).__name__)
        except Exception as error:
            # httpx's other errors: no connection, a broken exchange.
            return self._failed("error", type(error).__name__)
        self._count("ok")
        return parsed

    async def _get(self, query: str, window: UsageWindow) -> object:
        """The decoded answer, read up to ``max_answer_bytes``."""
        params = {"query": query, "time": window.time_s}
        async with self._client.stream("GET", self._url, params=params) as response:
            if not response.is_success:
                raise _QueryError("error", f"status {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_answer_bytes:
                    raise _QueryError("invalid", "answer too large")
        try:
            document: object = json.loads(body)
        except (ValueError, RecursionError) as error:
            # Not JSON: a JSONDecodeError, a UnicodeDecodeError (bytes that are no UTF
            # encoding json.loads detects), or nesting too deep to decode.
            raise _QueryError("invalid", type(error).__name__) from None
        return document

    def _failed(self, reason: _Failure, detail: str) -> UsageUnavailable:
        self._count(reason)
        _log.warning("prometheus query failed (%s)", detail)
        return UsageUnavailable(reason=reason)

    def _count(self, result: PrometheusQueryResult) -> None:
        if self._on_query is None:
            return
        try:
            self._on_query(result)
        except Exception as error:
            _log.error("prometheus query not counted (%s)", type(error).__name__)
