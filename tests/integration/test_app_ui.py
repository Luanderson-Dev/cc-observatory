"""The web UI on the real app container, in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. Four hook fixtures are replayed
so that the overview has a session in each live bay and one that ended (ADRs 0024 and
0031): the ended one with its OTLP metrics and events under the same session ID, so that
its page can show tokens and cost (ADR 0035) and its Grafana links can be followed. The
tests then check, against the container that compose builds:

- the overview's bays and the session page (state, the path with ``~``, never the full
  ``cwd``, blocks, agents and timeline) against the API, the source of their state;
- the response headers of ADR 0034 (CSP, ``no-store``, the static cache policy), the
  Fetch Metadata 403 for a request from another site, and the hook route's neutral empty
  204 under those middlewares (ADR 0027);
- the live updates stream (ADR 0032): an ``event: changed`` soon after a hook, the 503
  past ``MAX_STREAMS`` open streams, and the stream gauge on the metrics listener;
- the "Tokens and cost" fragment against the totals the replayed metrics add up to, once
  Prometheus holds them, and the query counter;
- each Grafana link: the page Grafana serves for it, the dashboard UID it names, and
  every query of its Explore panes run through Grafana's datasources, which must return
  the replayed session's events, and per-interval tokens and cost that add up to its
  totals;
- last, since it stops the app: ``docker compose stop app`` with a stream open ends
  within a few seconds, without uvicorn cancelling the stream at its graceful timeout.

The ended session's ID starts with ``LINKED_SESSION_PREFIX``: following its Logs link
puts it in Loki's query log, which test_app_privacy.py checks for the app's own queries.
"""

from __future__ import annotations

import html
import http.client
import json
import math
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from html.parser import HTMLParser
from typing import Final
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from app_support import (
    APP,
    HTTP_TIMEOUT,
    POLL_INTERVAL,
    Samples,
    detail,
    events,
    expected_shape,
    get,
    get_json,
    items,
    linked_session_id,
    mapping,
    poll,
    samples,
)

from ccobs.domain.app_metrics import PROMETHEUS_QUERIES, UI_STREAMS, UI_STREAMS_REFUSED
from ccobs.domain.denials import CATEGORIZE_LABELS, ENCODING_FLAGS_HEADER
from ccobs.domain.grafana_links import DASHBOARD_UID, LOGS_MAX_LINES
from ccobs.domain.presentation import bay_of, display_path
from ccobs.domain.telemetry import COST_USAGE, DEFAULT_HOST, HOST_LABEL, TOKEN_USAGE
from ccobs.domain.usage import (
    SessionUsage,
    Vector,
    format_tokens,
    format_usd,
    model_rows,
    parse_vector,
    session_usage,
    token_type_label,
    usage_queries,
    usage_window,
)
from ccobs.server.guards import CONTENT_SECURITY_POLICY
from ccobs.server.live import MAX_STREAMS, RETRY_MS
from ccobs.server.ui import IMMUTABLE, REVALIDATE
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import (
    HOOKS_PATH,
    HookLine,
    Prepared,
    PreparedHooks,
    load_scenario,
    prepare,
    prepare_hooks,
    send,
    send_hooks,
)

pytestmark = pytest.mark.integration

LOOPBACK: Final = "127.0.0.1"
# The ended session: permission blocks, a subagent, metrics and events.
FINISHED_SCENARIO: Final = "headless-tools-subagent"
# Cut right after its elicitation request: a session waiting on you.
WAITING_SCENARIO: Final = "interactive-elicitation-idle"
WAITING_CUT: Final = "Elicitation"
# Cut right after its first tool call starts: a working session.
WORKING_SCENARIO: Final = "headless-tools-subagent"
WORKING_CUT: Final = "PreToolUse"
# Ends on Stop without SessionEnd: an idle session.
IDLE_SCENARIO: Final = "headless-async-command-transport"
# The first lines of this one start a session for the live updates tests.
LIVE_SCENARIO: Final = "hooks-endpoint-timeout"
LIVE_LINES: Final = 3
# The words the page shows for each state (ccobs.server.ui).
STATE_LABELS: Final = {
    "waiting_user": "Waiting on you",
    "working": "Working",
    "idle": "Idle",
    "ended": "Ended",
}
# Seconds for the writer to store what the route queued.
STORED_DEADLINE: Final = 15.0
# Seconds for the collector, Prometheus and Loki to store the replayed telemetry.
INGEST_DEADLINE: Final = 60.0
# Seconds the usage fragment is polled once Prometheus holds the totals: past the
# usage reader's cache of a live window (30 s), in case the app's clock lags this
# machine's and its first read missed the newest samples.
USAGE_DEADLINE: Final = 40.0
# The live event comes within about 0.3 s of a hook (a 50 ms writer batch and the 0.25 s
# coalescing, ADR 0032); the limit is generous for a loaded CI runner.
LIVE_LIMIT_S: Final = 5.0
# Seconds for a stream's first line, and for the app to free a closed stream's slot.
STREAM_START_DEADLINE: Final = 10.0
# Seconds for the metrics listener to count streams the app accepted.
STREAMS_COUNTED_DEADLINE: Final = 10.0
STREAM_FREED_DEADLINE: Final = 10.0
# `docker compose stop app` with a stream open: well under uvicorn's graceful timeout
# (5 s, ccobs.server.__main__), which a stream still open at shutdown would wait out.
STOP_LIMIT_S: Final = 3.0
STOP_TIMEOUT_S: Final = 60.0
RESTART_DEADLINE: Final = 60.0
# uvicorn's line when its graceful timeout runs out with requests still open.
CANCELLED_LINE: Final = "running task(s)"
# Grafana's frontend asks for the Explore panes with the interval it computes: the
# session's few minutes over maxDataPoints, raised to the queries' 1m minimum.
EXPLORE_INTERVAL_MS: Final = 60_000
# Loki's entries per query, far above one session's.
LOKI_LIMIT: Final = 1000
REL_TOL: Final = 1e-9
# A Windows or POSIX path with a user directory, or a drive (test_app_privacy.py).
PATH_PATTERN: Final = re.compile(r"\\Users\\|/home/|/Users/|[A-Za-z]:\\", re.IGNORECASE)
HOST_SAMPLE: Final = frozenset({(HOST_LABEL, DEFAULT_HOST)})
STREAMS: Final = (UI_STREAMS.name, HOST_SAMPLE)
REFUSED: Final = (UI_STREAMS_REFUSED.name, HOST_SAMPLE)
QUERIES_OK: Final = (
    PROMETHEUS_QUERIES.name,
    frozenset({(HOST_LABEL, DEFAULT_HOST), ("result", "ok")}),
)
CHANGED: Final = re.compile(rb"event: changed\ndata: ([^\n]*)\n\n")
RETRY: Final = re.compile(rb"retry: (\d+)\n\n")

# HTTP with headers.


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def _fetch(
    base: str,
    method: str,
    path: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> Reply:
    """One request on a connection of its own, to the host and port of ``base``; the
    header names of the answer in lower case (a repeated one keeps its last value)."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(
        parts.hostname or LOOPBACK, parts.port, timeout=HTTP_TIMEOUT
    )
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        found = {key.lower(): value for key, value in response.getheaders()}
        return Reply(response.status, found, response.read())
    finally:
        connection.close()


def _page(app_url: str, path: str) -> str:
    reply = _fetch(app_url, "GET", path)
    assert reply.status == 200, f"GET {path}: {reply.status} {reply.body[:300]!r}"
    return reply.text


# A server-sent events stream on a raw socket: http.client and httpx read a stream only
# by blocking until its next bytes, and the tests must post hooks, watch the deadline and
# close it while it is open.


class EventStream:
    """``GET path`` held open on a socket of its own; the body decoded from HTTP/1.1
    chunks when the answer is chunked."""

    def __init__(self, base: str, path: str, headers: Mapping[str, str] | None = None) -> None:
        parts = urlsplit(base)
        host, port = parts.hostname or LOOPBACK, parts.port or 80
        self._socket = socket.create_connection((host, port), timeout=HTTP_TIMEOUT)
        self._socket.settimeout(POLL_INTERVAL)
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Accept: text/event-stream",
            *(f"{name}: {value}" for name, value in (headers or {}).items()),
        ]
        self._socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self._raw = b""
        self.body = b""
        self._seen = 0
        self.closed = False
        self.finished = False
        self.status, self.headers = self._head(time.monotonic() + STREAM_START_DEADLINE)
        self._chunked = self.headers.get("transfer-encoding", "").lower() == "chunked"
        self._decode()

    def _receive(self, deadline: float) -> None:
        """Read what arrives before ``deadline`` or the next poll interval."""
        if self.closed:
            return
        self._socket.settimeout(max(0.01, min(POLL_INTERVAL, deadline - time.monotonic())))
        try:
            chunk = self._socket.recv(65536)
        except TimeoutError:
            return
        except OSError:
            self.closed = True
            return
        if not chunk:
            self.closed = True
        self._raw += chunk

    def _head(self, deadline: float) -> tuple[int, dict[str, str]]:
        while b"\r\n\r\n" not in self._raw:
            assert not self.closed, f"the stream closed before its headers: {self._raw!r}"
            assert time.monotonic() < deadline, f"no headers in time: {self._raw!r}"
            self._receive(deadline)
        head, self._raw = self._raw.split(b"\r\n\r\n", 1)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return int(status_line.split(" ", 2)[1]), headers

    def _decode(self) -> None:
        if not self._chunked:
            self.body += self._raw
            self._raw = b""
            return
        while not self.finished:
            end = self._raw.find(b"\r\n")
            if end < 0:
                return
            size = int(self._raw[:end].split(b";", 1)[0], 16)
            if size == 0:
                self.finished = True
                return
            start = end + 2
            if len(self._raw) < start + size + 2:
                return
            self.body += self._raw[start : start + size]
            self._raw = self._raw[start + size + 2 :]

    def next_match(self, pattern: re.Pattern[bytes], seconds: float) -> re.Match[bytes] | None:
        """The next match of ``pattern`` in the body after the last one returned, waiting
        up to ``seconds``; ``None`` if none came."""
        deadline = time.monotonic() + seconds
        while True:
            found = pattern.search(self.body, self._seen)
            if found is not None:
                self._seen = found.end()
                return found
            if self.closed or self.finished or time.monotonic() >= deadline:
                return None
            self._receive(deadline)
            self._decode()

    def read_body(self, size: int, seconds: float) -> bytes:
        """At least ``size`` bytes of the body, or what came before ``seconds``."""
        deadline = time.monotonic() + seconds
        while len(self.body) < size and not self.closed and time.monotonic() < deadline:
            self._receive(deadline)
            self._decode()
        return self.body

    def ends_within(self, seconds: float) -> bool:
        """Whether the server ends the stream (its last chunk, or the connection) within
        ``seconds``."""
        deadline = time.monotonic() + seconds
        while not (self.closed or self.finished) and time.monotonic() < deadline:
            self._receive(deadline)
            self._decode()
        return self.closed or self.finished

    def close(self) -> None:
        self._socket.close()


def _open_stream(app_url: str, path: str) -> EventStream:
    """A live stream that the app accepted: a 200 event stream whose retry line came."""
    stream = EventStream(app_url, path)
    try:
        assert stream.status == 200, (stream.status, stream.body[:300])
        assert stream.headers.get("content-type", "").startswith("text/event-stream")
        retry = stream.next_match(RETRY, STREAM_START_DEADLINE)
        assert retry is not None, f"no retry line: {stream.body[:300]!r}"
        assert int(retry[1]) == RETRY_MS
    except BaseException:
        stream.close()
        raise
    return stream


def _changed_for(stream: EventStream, session_id: str, seconds: float) -> float | None:
    """When (``time.perf_counter``) the first ``changed`` event that names ``session_id``,
    or says ``all``, arrived within ``seconds``; events about other sessions (a hook
    another test sent, say) are skipped."""
    deadline = time.perf_counter() + seconds
    while (left := deadline - time.perf_counter()) > 0:
        changed = stream.next_match(CHANGED, left)
        if changed is None:
            return None
        data = changed[1].decode()
        if data == "all" or session_id in json.loads(data):
            return time.perf_counter()
    return None


def _stream_path(session_id: str | None = None) -> str:
    return "/ui/stream" if session_id is None else f"/ui/stream?session={quote(session_id)}"


def _listener(app_metrics_url: str) -> Samples:
    return samples(get(app_metrics_url, "/metrics").decode())


def _open_streams(app_metrics_url: str) -> float:
    return _listener(app_metrics_url).get(STREAMS, 0.0)


# HTML.


class _Board(HTMLParser):
    """The session IDs of the overview's strips, by bay (``data-bay`` or ``bay--<key>``)."""

    def __init__(self) -> None:
        super().__init__()
        self._bays: list[str | None] = []
        self.found: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag in ("section", "details"):
            bay = next(
                (name.removeprefix("bay--") for name in classes if name.startswith("bay--")),
                None,
            )
            self._bays.append(bay if "bay" in classes else None)
        elif tag == "a" and "strip__project" in classes:
            bay = next((name for name in reversed(self._bays) if name), None)
            href = values.get("href") or ""
            assert bay is not None, f"a strip outside a bay: {href}"
            assert href.startswith("/sessions/"), href
            self.found.setdefault(bay, []).append(unquote(href.removeprefix("/sessions/")))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("section", "details") and self._bays:
            self._bays.pop()


def _bays(page: str) -> dict[str, list[str]]:
    parser = _Board()
    parser.feed(page)
    parser.close()
    return parser.found


def _one(pattern: str, page: str) -> str:
    found = re.findall(pattern, page, re.DOTALL)
    assert len(found) == 1, f"{pattern}: {found}"
    return html.unescape(str(found[0]))


def _links(page: str) -> dict[str, str]:
    """The session page's Grafana links, by their text."""
    nav = _one(r'<nav id="grafana-links"[^>]*>(.*?)</nav>', page)
    return {
        text: html.unescape(href)
        for href, text in re.findall(r'<a href="([^"]*)"[^>]*>([^<]*)</a>', nav)
    }


# Expected usage, from the replayed metrics.


def _vector(run: Prepared, metric: str, label: str) -> Vector:
    totals: dict[str, float] = {}
    for key, total in run.expected_series(metric).items():
        name = key.label(label) or ""
        totals[name] = totals.get(name, 0.0) + float(total)
    return tuple(sorted(totals.items()))


def _expected_usage(run: Prepared) -> SessionUsage:
    return session_usage(
        _vector(run, TOKEN_USAGE.otel_name, "type"),
        _vector(run, TOKEN_USAGE.otel_name, "model"),
        _vector(run, COST_USAGE.otel_name, "model"),
    )


def _close(found: Mapping[str, float], expected: Mapping[str, float]) -> bool:
    """Same keys and values, ignoring keys that are zero on one side only."""
    keys = {key for key, value in {**found, **expected}.items() if value}
    return all(
        math.isclose(found.get(key, 0.0), expected.get(key, 0.0), rel_tol=REL_TOL) for key in keys
    )


@dataclass(frozen=True)
class UsageText:
    """What the "Tokens and cost" fragment shows."""

    total_cost: str | None
    total_tokens: str | None
    types: list[tuple[str, ...]] = field(default_factory=list)
    models: list[tuple[str, ...]] = field(default_factory=list)


_ROW: Final = re.compile(
    r'<tr><th scope="row">(?:<code>)?([^<]*)(?:</code>)?</th>'
    r'((?:<td class="usage__number">[^<]*</td>)+)</tr>'
)
_CELL: Final = re.compile(r'<td class="usage__number">([^<]*)</td>')


def _rows(table: str) -> list[tuple[str, ...]]:
    return [
        (html.unescape(name), *(html.unescape(cell) for cell in _CELL.findall(cells)))
        for name, cells in _ROW.findall(table)
    ]


def _usage_text(fragment: str) -> UsageText:
    cost = re.search(r"<dt>Total cost</dt><dd>([^<]*)</dd>", fragment)
    tokens = re.search(r"<dt>Tokens</dt><dd>([^<]*)</dd>", fragment)
    by_type, _, by_model = fragment.partition(">By model</caption>")
    return UsageText(
        total_cost=None if cost is None else html.unescape(cost[1]),
        total_tokens=None if tokens is None else html.unescape(tokens[1]),
        types=_rows(by_type),
        models=sorted(_rows(by_model)),
    )


def _expected_text(usage: SessionUsage) -> UsageText:
    return UsageText(
        total_cost=format_usd(usage.total_cost),
        total_tokens=format_tokens(usage.total_tokens),
        types=[
            (token_type_label(name), format_tokens(value)) for name, value in usage.tokens_by_type
        ],
        models=sorted(
            (
                row.model or "Unknown model",
                "-" if row.tokens is None else format_tokens(row.tokens),
                "-" if row.cost is None else format_usd(row.cost),
            )
            for row in model_rows(usage)
        ),
    )


# Grafana, reached on loopback with the Host of its root URL (ADR 0017).


@dataclass(frozen=True)
class Grafana:
    base: str

    @property
    def host(self) -> str:
        return urlsplit(self.base).netloc

    def request(self, method: str, path: str, document: JSON | None = None) -> Reply:
        headers = {"Host": self.host}
        body = None
        if document is not None:
            # A same-origin JSON request, as Grafana's own pages send it.
            headers |= {"Content-Type": "application/json", "Origin": self.base}
            body = json.dumps(document).encode()
        port = urlsplit(self.base).port
        return _fetch(f"http://{LOOPBACK}:{port}", method, path, headers, body)

    def frames(self, pane: JSONObject, query: JSONObject) -> list[JSONObject]:
        """The frames of one of a pane's queries through ``/api/ds/query``, over the
        pane's range."""
        time_range = _object(pane.get("range"))
        document: JSONObject = {
            "from": time_range.get("from"),
            "to": time_range.get("to"),
            "queries": [query],
        }
        reply = self.request("POST", "/api/ds/query", document)
        assert reply.status == 200, f"{query.get('expr')}: {reply.status} {reply.body[:500]!r}"
        answer = _object(json.loads(reply.body))
        result = _object(_object(answer.get("results")).get(str(query.get("refId"))))
        assert "error" not in result, f"{query.get('expr')}: {result.get('error')}"
        return _objects(result.get("frames"))


def _object(value: JSON | None) -> JSONObject:
    assert isinstance(value, dict), f"expected a JSON object, got {value!r}"
    return value


def _objects(value: JSON | None) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _fields(frame: JSONObject) -> Iterator[tuple[JSONObject, list[JSON]]]:
    fields = _objects(_object(frame.get("schema", {})).get("fields"))
    values = _object(frame.get("data", {})).get("values", [])
    assert isinstance(values, list), frame.get("schema")
    for spec, column in zip(fields, values, strict=True):
        if isinstance(column, list):
            yield spec, column


def _sums_by(frames: list[JSONObject], label: str) -> dict[str, float]:
    """The sum of every number field's values, by the value of ``label`` on the field."""
    sums: dict[str, float] = {}
    for frame in frames:
        for spec, column in _fields(frame):
            if spec.get("type") != "number":
                continue
            labels = spec.get("labels")
            name = str(labels.get(label, "")) if isinstance(labels, dict) else ""
            values = [float(value) for value in column if isinstance(value, int | float)]
            sums[name] = sums.get(name, 0.0) + math.fsum(values)
    return sums


def _entry_labels(frames: list[JSONObject]) -> list[dict[str, str]]:
    """The labels of every log entry, structured metadata among them."""
    found: list[dict[str, str]] = []
    for frame in frames:
        for spec, column in _fields(frame):
            if spec.get("name") == "labels":
                found += [
                    {key: str(value) for key, value in entry.items()}
                    for entry in column
                    if isinstance(entry, dict)
                ]
    return found


def _pane_sums(
    grafana: Grafana, pane: JSONObject, query: JSONObject, label: str
) -> dict[str, float]:
    return _sums_by(grafana.frames(pane, query), label)


def _panes(href: str) -> dict[str, JSONObject]:
    query = parse_qs(urlsplit(href).query, strict_parsing=True)
    assert query.get("schemaVersion") == ["1"], query
    assert query.get("orgId") == ["1"], query
    (panes,) = query["panes"]
    return {name: _object(pane) for name, pane in _object(json.loads(panes)).items()}


def _queries(pane: JSONObject) -> list[JSONObject]:
    found = _objects(pane.get("queries"))
    assert found, pane
    return found


def _session_records(run: Prepared) -> int:
    """How many of ``run``'s log records carry its session ID, which the Logs link's
    LogQL selects on."""
    count = 0
    for request in run.logs:
        for resource in _objects(request.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                for record in _objects(scope.get("logRecords")):
                    for attribute in _objects(record.get("attributes")):
                        value = attribute.get("value")
                        if (
                            attribute.get("key") == "session.id"
                            and isinstance(value, dict)
                            and value.get("stringValue") == run.session_id
                        ):
                            count += 1
    return count


def _loki_count(loki_url: str, logql: str, pane: JSONObject) -> int:
    """How many entries ``logql`` selects straight from Loki, over the pane's range."""
    time_range = _object(pane.get("range"))
    start, end = str(time_range.get("from")), str(time_range.get("to"))
    params: dict[str, str | int] = {
        "query": logql,
        "start": f"{start}000000",
        "end": str(time.time_ns()) if end == "now" else f"{end}000000",
        "limit": LOKI_LIMIT,
        "direction": "forward",
    }
    document = mapping(
        get_json(
            loki_url,
            "/loki/api/v1/query_range",
            params,
            {ENCODING_FLAGS_HEADER: CATEGORIZE_LABELS},
        ),
        "Loki answer",
    )
    assert document.get("status") == "success", document
    data = mapping(document.get("data"), "data")
    count = sum(
        len(items(mapping(stream, "stream").get("values"), "values"))
        for stream in items(data.get("result"), "result")
    )
    assert count < LOKI_LIMIT, f"{logql} hit the query limit"
    return count


# The replay.


@dataclass(frozen=True)
class Replay:
    """The replayed sessions: the ended one's hooks and telemetry, and one session in
    each live bay; ``status`` is what the reducer gives each (ADR 0024)."""

    finished: PreparedHooks
    telemetry: Prepared
    waiting: PreparedHooks
    working: PreparedHooks
    idle: PreparedHooks
    status: dict[str, str]

    @property
    def runs(self) -> tuple[PreparedHooks, ...]:
        return (self.finished, self.waiting, self.working, self.idle)


def _cut(run: PreparedHooks, event: str) -> tuple[HookLine, ...]:
    """``run``'s lines up to and including the first ``event``."""
    names = [line.payload.get("hook_event_name") for line in run.lines]
    return run.lines[: names.index(event) + 1]


def _stored(app_url: str, runs: Mapping[str, tuple[str, int]]) -> dict[str, tuple[str, int]]:
    """Each session's status and stored event count."""
    found: dict[str, tuple[str, int]] = {}
    for session_id in runs:
        current = detail(app_url, session_id)
        status = "" if current is None else str(current["status"])
        found[session_id] = (status, len(events(app_url, session_id)))
    return found


@pytest.fixture(scope="module")
def replay(app_url: str, otlp_http_url: str) -> Replay:
    """The four sessions, stored; then the ended one's metrics and events, timed so that
    their newest point is just after its hooks by this machine's clock."""
    session_id = linked_session_id()
    finished = prepare_hooks(load_scenario(FINISHED_SCENARIO), session_id=session_id)
    waiting_run = prepare_hooks(load_scenario(WAITING_SCENARIO))
    working_run = prepare_hooks(load_scenario(WORKING_SCENARIO))
    waiting = PreparedHooks(
        waiting_run.scenario, waiting_run.session_id, _cut(waiting_run, WAITING_CUT)
    )
    working = PreparedHooks(
        working_run.scenario, working_run.session_id, _cut(working_run, WORKING_CUT)
    )
    idle = prepare_hooks(load_scenario(IDLE_SCENARIO))
    runs = (finished, waiting, working, idle)
    status = {run.session_id: str(expected_shape(run.lines).status) for run in runs}
    # The fixtures cover every bay the board shows for a live stack.
    assert sorted(status.values()) == sorted(STATE_LABELS), status
    for run in runs:
        send_hooks(app_url, run.lines)
    expected = {run.session_id: (status[run.session_id], len(run.lines)) for run in runs}
    stored = poll(lambda: _stored(app_url, expected), lambda got: got == expected, STORED_DEADLINE)
    assert stored == expected
    telemetry = prepare(
        load_scenario(FINISHED_SCENARIO), now_ns=time.time_ns(), session_id=session_id
    )
    send(otlp_http_url, telemetry, signals=("metrics", "logs"))
    return Replay(finished, telemetry, waiting, working, idle, status)


@pytest.fixture(scope="module")
def grafana(grafana_url: str) -> Grafana:
    return Grafana(grafana_url)


# The overview and the session page.


def test_the_overview_shows_each_session_in_its_bay(app_url: str, replay: Replay) -> None:
    page = _page(app_url, "/")
    bays = _bays(page)
    for run in replay.runs:
        bay = bay_of(replay.status[run.session_id]).value
        assert run.session_id in bays.get(bay, []), (run.scenario, bay, bays.keys())
        others = [key for key, ids in bays.items() if key != bay and run.session_id in ids]
        assert not others, (run.scenario, others)
        cwd = mapping(detail(app_url, run.session_id), "session")["cwd"]
        assert isinstance(cwd, str)
        assert cwd not in page
    assert not PATH_PATTERN.search(page), PATH_PATTERN.search(page)


def test_the_session_page_shows_the_state_the_api_serves(app_url: str, replay: Replay) -> None:
    for run in replay.runs:
        found = detail(app_url, run.session_id)
        assert found is not None
        page = _page(app_url, f"/sessions/{quote(run.session_id)}")
        status = str(found["status"])
        assert _one(r'class="session-head__state">([^<]*)<', page) == STATE_LABELS[status]
        assert _one(r'class="session-head__id">([^<]*)<', page) == run.session_id
        # The path with ~ for the home directory; never the full cwd or the username.
        cwd = str(found["cwd"])
        shown = display_path(cwd)
        assert shown is not None
        assert shown.startswith("~"), shown
        assert _one(r'class="session-head__path">([^<]*)<', page) == shown
        assert cwd not in page
        assert not PATH_PATTERN.search(page), (run.scenario, PATH_PATTERN.search(page))
        # Blocks: every one, open or closed as the API says.
        blocks = {
            str(mapping(block, "block")["block_id"]): mapping(block, "block")["closed_at_ms"]
            for block in items(found["blocks"], "blocks")
        }
        shown_blocks = {
            block_id: state
            for state, block_id in re.findall(
                r'<li class="block block--(open|closed)" data-block-id="(\d+)">', page
            )
        }
        assert shown_blocks == {
            block_id: "open" if closed is None else "closed" for block_id, closed in blocks.items()
        }, run.scenario
        # Agents: the tree holds every one.
        agents = {
            str(mapping(agent, "agent")["agent_id"]) for agent in items(found["agents"], "agents")
        }
        shown_agents = {
            html.unescape(agent) for agent in re.findall(r'data-agent-id="([^"]*)"', page)
        }
        assert shown_agents == agents, run.scenario
        # The timeline: the newest events first, every one of these short sessions.
        seqs = sorted(
            (int(str(event["seq"])) for event in events(app_url, run.session_id)), reverse=True
        )
        shown_seqs = [int(seq) for seq in re.findall(r'<li class="event" data-seq="(\d+)">', page)]
        assert shown_seqs == seqs, run.scenario
    # The fixtures exercise what the page draws: closed and open blocks, and subagents.
    finished = mapping(detail(app_url, replay.finished.session_id), "session")
    assert items(finished["blocks"], "blocks")
    assert items(finished["agents"], "agents")
    waiting = mapping(detail(app_url, replay.waiting.session_id), "session")
    assert any(
        mapping(block, "block")["closed_at_ms"] is None
        for block in items(waiting["blocks"], "blocks")
    )


# Headers and browser defences (ADR 0034) on the real container.


def test_pages_and_fragments_carry_the_security_headers(app_url: str, replay: Replay) -> None:
    # Not the ended session: its usage fragment is first read by the usage test, which
    # counts the Prometheus queries of that read (the app caches what it read).
    session = quote(replay.idle.session_id)
    for path in (
        "/",
        "/sessions",
        f"/sessions/{session}",
        f"/ui/sessions/{session}/usage",
        "/ui/board",
    ):
        reply = _fetch(app_url, "GET", path, {"Origin": "http://evil.example"})
        assert reply.status == 200, (path, reply.status)
        assert reply.headers.get("content-security-policy") == CONTENT_SECURITY_POLICY, path
        assert reply.headers.get("x-content-type-options") == "nosniff", path
        assert reply.headers.get("referrer-policy") == "no-referrer", path
        assert reply.headers.get("cross-origin-resource-policy") == "same-origin", path
        assert reply.headers.get("cross-origin-opener-policy") == "same-origin", path
        assert reply.headers.get("cache-control") == "no-store", path
        assert not [name for name in reply.headers if name.startswith("access-control-")], path
    # Static files: the same headers, and a cache policy instead of no-store.
    for path, policy, media_type in (
        ("/static/css/app.css", REVALIDATE, "text/css"),
        ("/static/js/live.js", REVALIDATE, "text/javascript"),
        ("/static/vendor/htmx-2.0.11.min.js", IMMUTABLE, "text/javascript"),
    ):
        reply = _fetch(app_url, "GET", path)
        assert reply.status == 200, (path, reply.status)
        assert reply.headers.get("content-security-policy") == CONTENT_SECURITY_POLICY, path
        assert reply.headers.get("cache-control") == policy, path
        assert reply.headers.get("content-type", "").startswith(media_type), path


def test_fetch_metadata_refuses_requests_from_other_sites(
    app_url: str, grafana_url: str, replay: Replay
) -> None:
    session = quote(replay.idle.session_id)
    fetch = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    for site in ("cross-site", "same-site"):
        for path in (
            "/ui/board",
            f"/ui/sessions/{session}/usage",
            "/ui/stream",
            "/api/v1/sessions",
        ):
            reply = _fetch(app_url, "GET", path, {"Sec-Fetch-Site": site, **fetch})
            assert reply.status == 403, (site, path, reply.status)
            assert reply.body == b"forbidden\n", (site, path)
    # A link followed from Grafana (another port, so same-site) still opens the page.
    navigate = {
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": grafana_url + "/",
    }
    assert _fetch(app_url, "GET", f"/sessions/{session}", navigate).status == 200
    # Our own pages' requests pass.
    own = {"Sec-Fetch-Site": "same-origin", **fetch}
    assert _fetch(app_url, "GET", "/ui/board", own).status == 200


def test_the_hook_route_still_answers_an_empty_204(app_url: str) -> None:
    line = prepare_hooks(load_scenario(LIVE_SCENARIO)).lines[0]
    body = json.dumps(line.payload).encode()
    for headers in (
        {"Content-Type": "application/json"},
        # From another site: Fetch Metadata and the security headers leave it alone.
        {
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "no-cors",
            "Origin": "http://evil.example",
        },
        # Not JSON: ignored, still the neutral answer (ADR 0027).
        {"Content-Type": "text/plain"},
    ):
        reply = _fetch(app_url, "POST", HOOKS_PATH, headers, body)
        assert reply.status == 204, (headers, reply.status)
        assert reply.body == b"", headers
        assert "content-security-policy" not in reply.headers, headers
        assert "cache-control" not in reply.headers, headers
        assert not [name for name in reply.headers if name.startswith("access-control-")]


# Live updates (ADR 0032).


def test_a_hook_reaches_an_open_stream_as_a_changed_event(
    app_url: str, app_metrics_url: str
) -> None:
    run = prepare_hooks(load_scenario(LIVE_SCENARIO))
    board = _open_stream(app_url, _stream_path())
    try:
        page = _open_stream(app_url, _stream_path(run.session_id))
        try:
            opened = poll(
                lambda: _open_streams(app_metrics_url),
                lambda count: count >= 2,
                STREAMS_COUNTED_DEADLINE,
            )
            assert opened >= 2, opened
            sent = time.perf_counter()
            send_hooks(app_url, run.lines[:LIVE_LINES])
            for stream, what in ((page, "session page"), (board, "overview")):
                took = _changed_for(
                    stream, run.session_id, sent + LIVE_LIMIT_S - time.perf_counter()
                )
                assert took is not None, f"no changed event for the session on the {what}'s stream"
                print(f"{what}: changed event {took - sent:.3f} s after the POST")
            assert page.headers.get("cache-control") == "no-store"
        finally:
            page.close()
    finally:
        board.close()


def test_the_stream_past_the_cap_gets_a_503(app_url: str, app_metrics_url: str) -> None:
    # Streams the earlier tests closed free their slots once the app sees them go.
    freed = poll(
        lambda: _open_streams(app_metrics_url), lambda count: count == 0, STREAM_FREED_DEADLINE
    )
    assert freed == 0, f"{freed} streams still open"
    refused_before = _listener(app_metrics_url).get(REFUSED, 0.0)
    streams: list[EventStream] = []
    try:
        for _ in range(MAX_STREAMS):
            streams.append(_open_stream(app_url, _stream_path()))
        assert _open_streams(app_metrics_url) == MAX_STREAMS
        extra = EventStream(app_url, _stream_path())
        try:
            assert extra.status == 503, extra.status
            assert extra.headers.get("retry-after") == "5"
            length = int(extra.headers.get("content-length", "0"))
            assert extra.read_body(length, HTTP_TIMEOUT) == b"too many live streams\n"
        finally:
            extra.close()
        found = _listener(app_metrics_url)
        assert found.get(REFUSED, 0.0) == refused_before + 1
        assert found.get(STREAMS) == MAX_STREAMS
    finally:
        for stream in streams:
            stream.close()
    left = poll(
        lambda: _open_streams(app_metrics_url), lambda count: count == 0, STREAM_FREED_DEADLINE
    )
    assert left == 0, f"{left} streams still open after closing them all"


# Tokens and cost (ADR 0035).


def _prometheus_usage(
    prometheus_url: str, app_url: str, replay: Replay
) -> dict[str, dict[str, float]]:
    """The session's totals as the app's queries read them from Prometheus now."""
    found = mapping(detail(app_url, replay.finished.session_id), "session")
    ended = found["ended_at_ms"]
    window = usage_window(
        int(str(found["started_at_ms"])),
        None if ended is None else int(str(ended)),
        int(time.time() * 1000),
    )
    queries = usage_queries(DEFAULT_HOST, replay.finished.session_id, window)
    vectors: dict[str, dict[str, float]] = {}
    for name, query, label in (
        ("tokens_by_type", queries.tokens_by_type, "type"),
        ("tokens_by_model", queries.tokens_by_model, "model"),
        ("cost_by_model", queries.cost_by_model, "model"),
    ):
        answer = get_json(prometheus_url, "/api/v1/query", {"query": query, "time": window.time_s})
        vector = parse_vector(answer, label)
        assert vector is not None, answer
        vectors[name] = dict(vector)
    return vectors


def test_the_usage_fragment_shows_the_replayed_totals(
    app_url: str, app_metrics_url: str, prometheus_url: str, replay: Replay
) -> None:
    usage = _expected_usage(replay.telemetry)
    assert not usage.empty, "the scenario has no tokens or cost"
    expected = {
        "tokens_by_type": dict(usage.tokens_by_type),
        "tokens_by_model": dict(usage.tokens_by_model),
        "cost_by_model": dict(usage.cost_by_model),
    }
    # First wait for Prometheus, so that the app's first read is the whole total: the
    # app caches what it read (ccobs.server.usage). No other test reads this session's
    # usage fragment, so the read below is the first one and asks Prometheus.
    stored = poll(
        lambda: _prometheus_usage(prometheus_url, app_url, replay),
        lambda found: all(_close(found[key], expected[key]) for key in expected),
        INGEST_DEADLINE,
    )
    assert all(_close(stored[key], expected[key]) for key in expected), (stored, expected)
    queries_before = _listener(app_metrics_url).get(QUERIES_OK, 0.0)
    path = f"/ui/sessions/{quote(replay.finished.session_id)}/usage"
    want = _expected_text(usage)
    shown = poll(
        lambda: _usage_text(_page(app_url, path)), lambda text: text == want, USAGE_DEADLINE
    )
    assert shown == want
    # The first read asked Prometheus its three queries.
    assert _listener(app_metrics_url).get(QUERIES_OK, 0.0) >= queries_before + 3
    # An anchor independent of the app's formatting code: every token series of the
    # replay summed, and the cost summed, at the precision the page shows.
    run = replay.telemetry
    tokens = math.fsum(map(float, run.expected_series(TOKEN_USAGE.otel_name).values()))
    cost = math.fsum(map(float, run.expected_series(COST_USAGE.otel_name).values()))
    assert shown.total_tokens is not None
    assert int(shown.total_tokens.replace(",", "")) == round(tokens), (shown, tokens)
    assert shown.total_cost is not None
    digits = re.fullmatch(r"\$([0-9,]+)\.([0-9]+)", shown.total_cost)
    assert digits is not None, shown.total_cost
    displayed = float(f"{digits[1].replace(',', '')}.{digits[2]}")
    assert abs(displayed - cost) <= 0.5 * 10 ** -len(digits[2]) + 1e-12, (shown, cost)


# Grafana links.


def test_the_grafana_links_point_at_the_stacks_grafana(
    app_url: str, grafana: Grafana, replay: Replay
) -> None:
    page = _page(app_url, f"/sessions/{quote(replay.finished.session_id)}")
    links = _links(page)
    assert set(links) == {"Logs", "Tokens and cost", "Dashboard"}, links
    for text in ("Logs", "Tokens and cost"):
        assert links[text].startswith(f"{grafana.base}/explore?"), links[text]
    dashboard = links["Dashboard"]
    assert dashboard.startswith(f"{grafana.base}/d/{DASHBOARD_UID}?"), dashboard
    assert parse_qs(urlsplit(dashboard).query).get(f"var-{HOST_LABEL}") == [DEFAULT_HOST]
    # Explore and the dashboard are routes of Grafana's single-page app: it answers each
    # with its page, on the Host it serves (no redirect to another one).
    for text, href in links.items():
        parts = urlsplit(href)
        reply = grafana.request("GET", f"{parts.path}?{parts.query}")
        assert reply.status == 200, (text, reply.status, reply.headers.get("location"))
        assert reply.headers.get("content-type", "").startswith("text/html"), text
    # The dashboard UID the link names is provisioned.
    reply = grafana.request("GET", f"/api/dashboards/uid/{DASHBOARD_UID}")
    assert reply.status == 200, reply.status
    assert _object(_object(json.loads(reply.body)).get("dashboard")).get("uid") == DASHBOARD_UID


def test_the_logs_link_finds_the_sessions_events(
    app_url: str, grafana: Grafana, loki_url: str, replay: Replay
) -> None:
    links = _links(_page(app_url, f"/sessions/{quote(replay.finished.session_id)}"))
    panes = _panes(links["Logs"])
    assert list(panes) == ["logs"], panes
    pane = panes["logs"]
    (query,) = _queries(pane)
    logql = str(query.get("expr"))
    expected = _session_records(replay.telemetry)
    assert expected, "the scenario has no event of the session"
    # The link names its line limit, so Grafana's backend does not cut the session's
    # events at its own default (100) when the query runs outside Explore's frontend.
    assert query.get("maxLines") == LOGS_MAX_LINES, query
    assert expected < LOGS_MAX_LINES
    # Straight from Loki first, until every record is stored.
    count = poll(
        lambda: _loki_count(loki_url, logql, pane), lambda found: found == expected, INGEST_DEADLINE
    )
    assert count == expected, logql
    # Then the pane's own query through Grafana's Loki datasource, as Explore runs it.
    entries = _entry_labels(grafana.frames(pane, query))
    assert len(entries) == expected, logql
    assert {entry.get("session_id") for entry in entries} == {replay.finished.session_id}


def test_the_tokens_and_cost_link_adds_up_to_the_totals(
    app_url: str, grafana: Grafana, replay: Replay
) -> None:
    links = _links(_page(app_url, f"/sessions/{quote(replay.finished.session_id)}"))
    panes = _panes(links["Tokens and cost"])
    assert sorted(panes) == ["cost", "tokens"], panes
    usage = _expected_usage(replay.telemetry)
    wanted = {
        "tokens": ("type", dict(usage.tokens_by_type)),
        "cost": ("model", dict(usage.cost_by_model)),
    }
    for name, (label, expected) in wanted.items():
        pane = panes[name]
        (query,) = _queries(pane)
        # The per-interval bars over the session's range add up to its totals (anchored
        # makes each interval exact, ADR 0013), as in the overview dashboard.
        sent: JSONObject = {**query, "intervalMs": EXPLORE_INTERVAL_MS}
        found = poll(
            partial(_pane_sums, grafana, pane, sent, label),
            partial(_close, expected=expected),
            INGEST_DEADLINE,
        )
        assert _close(found, expected), (name, query.get("expr"), found, expected)


# Last in this module: it stops the app and starts it again.


def test_stopping_the_app_with_a_stream_open_is_quick(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_logs: Callable[[str], str],
    service_health: Callable[[str], str],
    app_url: str,
) -> None:
    cancelled_before = service_logs(APP).count(CANCELLED_LINE)
    stream = _open_stream(app_url, _stream_path())
    try:
        started = time.perf_counter()
        compose("stop", APP, timeout=STOP_TIMEOUT_S)
        took = time.perf_counter() - started
        print(f"docker compose stop app with a stream open: {took:.2f} s")
        assert stream.ends_within(HTTP_TIMEOUT), "the stream did not end"
    finally:
        stream.close()
        compose("start", APP)
        poll(lambda: _healthy(app_url, service_health), lambda ok: ok, RESTART_DEADLINE)
    assert took < STOP_LIMIT_S, f"stopping took {took:.2f} s"
    cancelled = service_logs(APP).count(CANCELLED_LINE)
    assert cancelled == cancelled_before, "uvicorn cancelled a request at its graceful timeout"
    assert _healthy(app_url, service_health), "the app is not healthy after its restart"


def _healthy(app_url: str, health: Callable[[str], str]) -> bool:
    try:
        answer = get_json(app_url, "/healthz")
    except (OSError, AssertionError, ValueError):
        return False
    return answer == {"status": "ok"} and health(APP) == "healthy"
