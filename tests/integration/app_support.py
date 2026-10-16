"""Helpers shared by the app's integration tests (``test_app_*.py``).

The app runs in the ``ccobs-it`` project on its published port, behind its ``Host``
allowlist (ADR 0027): ``http.client`` sends ``Host: 127.0.0.1:<port>``, which it allows.
Its metrics listener is published by compose.debug.yaml only (ADR 0028). Expectations
come from the reducer itself: a hook fixture replayed in process through ``parse_hook``
and ``sessions.apply`` (ADR 0024) gives the state the app must serve once the same
payloads went through HTTP. Times differ (the app stamps its own receive times, and its
``seq`` is global), so the comparison leaves them out, keeping the order of blocks.

Not a test module: pytest collects only ``test_*.py``.
"""

from __future__ import annotations

import http.client
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit

from prometheus_client.parser import text_string_to_metric_families

from ccobs.domain.hooks import parse_hook
from ccobs.domain.sessions import (
    Agent,
    AgentChanged,
    Block,
    BlockClosed,
    BlockNotified,
    BlockOpened,
    SessionState,
    apply,
)
from tools.stack.replay import HookLine

APP: Final = "app"
APP_PORT: Final = "CCOBS_APP_PORT"
APP_METRICS_PORT: Final = "CCOBS_APP_METRICS_PORT"
API: Final = "/api/v1/sessions"
HTTP_TIMEOUT: Final = 10.0
POLL_INTERVAL: Final = 0.2
# The API's largest page (ccobs.server.api.MAX_LIMIT).
PAGE: Final = 500
# Statuses the session gauge counts: every one but ended (ADR 0028).
CLOSED_STATUSES: Final = frozenset({"ended", "stale"})


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes

    def json(self) -> object:
        document: object = json.loads(self.body)
        return document


def request(
    base: str,
    method: str,
    path: str,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> Answer:
    """One request on a connection of its own."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        target = f"{path}?{urlencode(params)}" if params else path
        connection.request(method, target, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        return Answer(response.status, response.read())
    finally:
        connection.close()


def get(
    base: str,
    path: str,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """The body of a 200 answer to ``GET {base}{path}?{params}``."""
    answer = request(base, "GET", path, params, headers)
    assert answer.status == 200, f"GET {path} answered {answer.status}: {answer.body[:300]!r}"
    return answer.body


def get_json(
    base: str,
    path: str,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
) -> object:
    document: object = json.loads(get(base, path, params, headers))
    return document


def mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def items(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


def percentile(values: Iterable[float], fraction: float) -> float:
    """The nearest-rank percentile: the smallest value with ``fraction`` of the values at
    or below it."""
    ordered = sorted(values)
    assert ordered, "no values"
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


# The app's API.


def sessions(app_url: str) -> list[dict[str, object]]:
    """Every session the app has (one page of the largest size)."""
    found = [mapping(item, "session") for item in items(_list(app_url), "sessions")]
    assert len(found) < PAGE, "more sessions than one page holds"
    return found


def _list(app_url: str) -> object:
    return get_json(app_url, API, {"limit": PAGE})


def detail(app_url: str, session_id: str) -> dict[str, object] | None:
    """One session with its agents and blocks; ``None`` while the app has not stored it."""
    answer = request(app_url, "GET", f"{API}/{session_id}")
    if answer.status == 404:
        return None
    assert answer.status == 200, f"{session_id}: {answer.status} {answer.body[:300]!r}"
    return mapping(answer.json(), "session")


def events(app_url: str, session_id: str) -> list[dict[str, object]]:
    found = get_json(app_url, f"{API}/{session_id}/events", {"limit": PAGE})
    return [mapping(item, "event") for item in items(found, "events")]


# The state to compare.

SESSION_FIELDS: Final = (
    "status",
    "turn_open",
    "compacting",
    "source",
    "model",
    "permission_mode",
    "effort_level",
    "last_prompt_id",
    "last_error_code",
    "end_reason",
    "cwd",
)
AGENT_FIELDS: Final = (
    "agent_id",
    "status",
    "agent_type",
    "internal",
    "background",
    "tool_use_id",
    "stop_reason",
)
BLOCK_FIELDS: Final = (
    "kind",
    "scope_agent_id",
    "tool_name",
    "tool_use_id",
    "mcp_server_name",
    "elicitation_id",
    "close_reason",
    "close_source",
)

type Row = tuple[object, ...]


@dataclass(frozen=True)
class SessionShape:
    """A session without its times and ``seq`` numbers: its fields, its agents sorted by
    ID, and its blocks in opening order, each with whether it was notified, closed, and
    whether the session's idle notice and end were recorded."""

    session: Row
    agents: tuple[Row, ...]
    blocks: tuple[Row, ...]

    @property
    def status(self) -> object:
        return self.session[0]


def _row(values: Mapping[str, object], names: Iterable[str]) -> Row:
    return tuple(values[name] for name in names)


def shape_of_detail(found: Mapping[str, object]) -> SessionShape:
    """The shape of an API session detail."""
    session = (
        *_row(found, SESSION_FIELDS),
        found["idle_notified_at_ms"] is not None,
        found["ended_at_ms"] is not None,
    )
    agents = sorted(
        (_row(mapping(agent, "agent"), AGENT_FIELDS) for agent in items(found["agents"], "agents")),
        key=lambda row: str(row[0]),
    )
    blocks = []
    for item in items(found["blocks"], "blocks"):
        block = mapping(item, "block")
        blocks.append(
            (
                *_row(block, BLOCK_FIELDS),
                block["notified_at_ms"] is not None,
                block["closed_at_ms"] is not None,
            )
        )
    return SessionShape(session, tuple(agents), tuple(blocks))


def _agent_values(agent: Agent) -> dict[str, object]:
    return {name: getattr(agent, name) for name in AGENT_FIELDS}


def _block_values(block: Block) -> dict[str, object]:
    values = {name: getattr(block, name) for name in BLOCK_FIELDS if name != "scope_agent_id"}
    return {**values, "scope_agent_id": block.scope}


def expected_shape(lines: Iterable[HookLine]) -> SessionShape:
    """The shape the reducer gives ``lines``, folded in order (ADR 0024)."""
    state: SessionState | None = None
    agents: dict[str, Agent] = {}
    blocks: dict[int, Block] = {}
    for seq, line in enumerate(lines, start=1):
        state, effects = apply(state, parse_hook(line.payload), 1_000_000 + line.offset_ms, seq)
        for effect in effects:
            if isinstance(effect, AgentChanged):
                agents[effect.agent.agent_id] = effect.agent
            elif isinstance(effect, BlockOpened | BlockNotified | BlockClosed):
                blocks[effect.block.block_id] = effect.block
    assert state is not None, "no hook line"
    session = state.session
    values = {name: getattr(session, name) for name in SESSION_FIELDS}
    return SessionShape(
        (
            *_row(values, SESSION_FIELDS),
            session.idle_notified_at_ms is not None,
            session.ended_at_ms is not None,
        ),
        tuple(_row(_agent_values(agents[key]), AGENT_FIELDS) for key in sorted(agents)),
        tuple(
            (
                *_row(_block_values(blocks[key]), BLOCK_FIELDS),
                blocks[key].notified_at_ms is not None,
                blocks[key].closed_at_ms is not None,
            )
            for key in sorted(blocks)
        ),
    )


def shapes(app_url: str, session_ids: Iterable[str]) -> dict[str, SessionShape | None]:
    """The shape of each session in the API, ``None`` for one not stored yet."""
    found: dict[str, SessionShape | None] = {}
    for session_id in session_ids:
        current = detail(app_url, session_id)
        found[session_id] = None if current is None else shape_of_detail(current)
    return found


# The app's metrics.

type Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]


def samples(text: str) -> Samples:
    """Every sample of a Prometheus text exposition, by name and labels."""
    found: Samples = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            found[(sample.name, frozenset(sample.labels.items()))] = float(sample.value)
    return found


def live_gauges(app_url: str, host: str) -> Samples:
    """The values the session, block and agent gauges must have, computed from the API:
    sessions by status (ended left out), open blocks by kind, and running agents that
    are not internal in sessions neither ended nor stale (ADR 0028)."""
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    running = 0
    for session in sessions(app_url):
        status = str(session["status"])
        if status != "ended":
            by_status[status] = by_status.get(status, 0) + 1
        found = detail(app_url, str(session["session_id"]))
        assert found is not None
        for item in items(found["blocks"], "blocks"):
            block = mapping(item, "block")
            if block["closed_at_ms"] is None:
                kind = str(block["kind"])
                by_kind[kind] = by_kind.get(kind, 0) + 1
        if status not in CLOSED_STATUSES:
            running += sum(
                1
                for item in items(found["agents"], "agents")
                if mapping(item, "agent")["status"] == "running"
                and not mapping(item, "agent")["internal"]
            )
    gauges: Samples = {}
    for status, count in by_status.items():
        gauges[("ccobs_sessions", frozenset({("host", host), ("state", status)}))] = count
    for kind, count in by_kind.items():
        gauges[("ccobs_blocks_open", frozenset({("host", host), ("kind", kind)}))] = count
    gauges[("ccobs_agents_running", frozenset({("host", host)}))] = running
    return gauges


def gauges_in(found: Samples, names: Iterable[str]) -> Samples:
    """The non-zero samples of ``names`` (the API cannot name the zero ones), plus the
    running agents, which are a single series."""
    wanted = set(names)
    return {
        key: value
        for key, value in found.items()
        if key[0] in wanted and (value != 0 or key[0] == "ccobs_agents_running")
    }
