"""The read-only JSON API (``ccobs.server.api``): sessions, their agents and blocks, and
their hook events, read from the database.

Real hook fixtures go through the real app: posted to ``/hooks`` with a fake clock, then
persisted by a writer that the tests drive with ``run_once`` (its thread never starts).
The responses must match the database exactly, hold only stored metadata (ADR 0023) and
never echo a request value; every route sits behind the Host guard and sends no CORS
header (ADR 0027).
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, insert, select
from sqlalchemy.pool import QueuePool

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server import api
from ccobs.server import app as app_module
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db import engine as db_engine
from ccobs.server.db.engine import EngineRole
from ccobs.server.db.models import AgentModel, Base, BlockModel, HookEventModel, SessionModel
from ccobs.server.db.repository import Repository, SessionView
from ccobs.server.ingest import HookQueue
from ccobs.server.migrations import MigrationError
from ccobs.server.reads import READ_SLOTS, ReadGate
from ccobs.server.writer import Writer, WriterObservers

HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
# Agents and a permission block; an elicitation block and idle; compaction and a
# permission; a StopFailure.
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
SESSIONS: Final = len(REPLAYED) + 1
HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
HOUR_MS: Final = 3_600_000
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
SESSION: Final = "00000000-0000-4000-8000-000000000000"
UNKNOWN: Final = "00000000-0000-4000-8000-0000000000ff"
CANARY: Final = "CANARY-5d1e8b"
NOT_FOUND: Final = {"detail": "session not found"}
API: Final = "/api/v1/sessions"
# The routes of one session, after its ID.
SUFFIXES: Final = ("", "/agents", "/blocks", "/events")
# Upper bound for any wait on another thread: far above what a pass needs.
PATIENCE_S: Final = 10.0
# As many reads as Starlette's default threadpool has threads.
CONCURRENT_READS: Final = 40
WRITE_DEADLINE_S: Final = 1.0

# Payload fields that ADR 0023 never keeps: a canary goes in each one a payload has.
CONTENT_FIELDS: Final = (
    "prompt",
    "tool_input",
    "tool_response",
    "tool_calls",
    "last_assistant_message",
    "message",
    "title",
    "requested_schema",
    "permission_suggestions",
    "custom_instructions",
    "compact_summary",
    "task_subject",
    "task_description",
    "transcript_path",
    "agent_transcript_path",
    "scratchpad_dir",
    "file_path",
)

# The API's order of sessions.
NEWEST_FIRST: Final = (SessionModel.last_event_at_ms.desc(), SessionModel.session_id)

type Payload = dict[str, Any]


def fixture_payloads(path: Path) -> list[tuple[int, Payload]]:
    """``(offset_ms, payload)`` for each line of a hook fixture."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        lines.append((document["offset_ms"], document["payload"]))
    return lines


def replay_lines() -> list[tuple[int, Payload]]:
    """Every replayed fixture, an hour apart, so the sessions' last activity differs, then
    the tools fixture up to its first PermissionRequest under another session ID: a
    session still waiting on its permission block."""
    lines = []
    for index, path in enumerate(REPLAYED):
        lines += [(index * HOUR_MS + offset, doc) for offset, doc in fixture_payloads(path)]
    waiting = fixture_payloads(REPLAYED[0])[:PERMISSION_PREFIX]
    start = len(REPLAYED) * HOUR_MS
    lines += [(start + offset, {**doc, "session_id": WAITING}) for offset, doc in waiting]
    return lines


def with_canaries(document: Payload) -> Payload:
    """``document`` with a canary in every content field it has and in an unknown key."""
    planted = dict(document, unreviewed_field=CANARY)
    for key in CONTENT_FIELDS:
        if key in planted:
            value = planted[key]
            if isinstance(value, dict):
                planted[key] = {**value, "canary": CANARY}
            elif isinstance(value, list):
                planted[key] = [*value, CANARY]
            else:
                planted[key] = CANARY
    if document["hook_event_name"] == "PostToolUseFailure":
        # Free text there, unlike StopFailure's error code.
        planted["error"] = CANARY
    return planted


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


class Clock:
    """The receive time of each hook, set by the test before it posts."""

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


@contextmanager
def started(settings: Settings, clock: Clock, **options: Any) -> Iterator[TestClient]:
    """The app with a manual writer, started, behind a good ``Host``."""

    def manual(
        queue: HookQueue, repository: Repository, observers: WriterObservers
    ) -> ManualWriter:
        return ManualWriter(queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0)

    queue = HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    app = create_app(
        settings,
        queue=queue,
        clock=clock,
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        **options,
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    with started(settings, clock) as test_client:
        yield test_client


def post(client: TestClient, clock: Clock, lines: Sequence[tuple[int, Payload]]) -> None:
    for offset, document in lines:
        clock.now = BASE + offset
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def persist(client: TestClient) -> None:
    """Run the writer until the queue is empty: every posted hook is committed."""
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


def rows(
    client: TestClient,
    model: type[Base],
    *order: Any,
    session_id: str | None = None,
    without: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The rows of ``model`` as dicts, minus the ``without`` columns."""
    table = model.__table__
    statement = select(table).order_by(*order)
    if session_id is not None:
        statement = statement.where(table.c.session_id == session_id)
    with engine_of(client).connect() as connection:
        found = connection.execute(statement).mappings().all()
    return [{k: v for k, v in row.items() if k not in without} for row in found]


def session_ids(client: TestClient) -> list[str]:
    return [row["session_id"] for row in rows(client, SessionModel, SessionModel.session_id)]


def ok(client: TestClient, url: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    return response.json()


# Responses match the database.


def test_the_session_list_matches_the_database(replayed: TestClient) -> None:
    expected = rows(replayed, SessionModel, *NEWEST_FIRST)

    assert len(expected) == SESSIONS
    assert ok(replayed, API) == expected
    # Newest first: the fixtures were replayed an hour apart, in order.
    times = [session["last_event_at_ms"] for session in expected]
    assert times == sorted(times, reverse=True)


def first_seen_key(agent: dict[str, Any]) -> tuple[int, str]:
    """The agents' order: when each was first seen, then ``agent_id`` (ADR 0033)."""
    for name in ("started_at_ms", "stopped_at_ms", "last_event_at_ms"):
        if agent[name] is not None:
            return agent[name], agent["agent_id"]
    raise AssertionError("an agent row always has last_event_at_ms")


def expected_agents(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    """Every agent of the session as the API serves it, in the API's order, with its
    parent worked out here from the stored PreToolUse rows."""
    launches: dict[str, str | None] = {}
    for event in rows(client, HookEventModel, HookEventModel.seq, session_id=session_id):
        if event["hook_event_name"] == "PreToolUse" and event["tool_use_id"] is not None:
            launches.setdefault(event["tool_use_id"], event["agent_id"])
    agents = rows(client, AgentModel, session_id=session_id, without=("session_id", "host"))
    return [
        {**agent, "parent_agent_id": launches.get(agent["tool_use_id"] or "")}
        for agent in sorted(agents, key=first_seen_key)
    ]


def test_each_session_detail_matches_the_database(replayed: TestClient) -> None:
    with_agents = with_blocks = linked = 0
    for session_id in session_ids(replayed):
        (session,) = rows(replayed, SessionModel, session_id=session_id)
        agents = expected_agents(replayed, session_id)
        blocks = rows(
            replayed,
            BlockModel,
            BlockModel.block_id,
            session_id=session_id,
            without=("session_id", "host"),
        )

        assert ok(replayed, f"{API}/{session_id}") == {
            **session,
            "agents": agents,
            "blocks": blocks,
            "agents_total": len(agents),
            "blocks_total": len(blocks),
        }
        assert ok(replayed, f"{API}/{session_id}/agents?limit=500") == agents
        assert ok(replayed, f"{API}/{session_id}/blocks?limit=500") == blocks
        with_agents += bool(agents)
        with_blocks += bool(blocks)
        linked += len([a for a in agents if a["tool_use_id"] is not None])

    # The fixtures exercise both lists, and an agent linked to the main thread's call.
    assert with_agents >= 1
    assert with_blocks >= 2
    assert linked >= 1


def test_the_events_match_the_database(replayed: TestClient) -> None:
    total = 0
    for session_id in session_ids(replayed):
        expected = rows(
            replayed,
            HookEventModel,
            HookEventModel.seq,
            session_id=session_id,
            without=("host",),
        )

        assert ok(replayed, f"{API}/{session_id}/events?limit=500") == expected
        assert ok(replayed, f"{API}/{session_id}/events?limit=3") == expected[-3:]
        total += len(expected)

    assert total == len(replay_lines())


def test_a_hook_shows_up_once_its_batch_commits(client: TestClient, clock: Clock) -> None:
    post(client, clock, [(0, {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"})])

    # Queued, not yet written: the database is the API's source of truth.
    assert ok(client, API) == []
    assert client.get(f"{API}/{SESSION}").status_code == 404

    persist(client)

    (session,) = ok(client, API)
    assert (session["session_id"], session["status"]) == (SESSION, "working")
    assert [event["seq"] for event in ok(client, f"{API}/{SESSION}/events")] == [1]


# Filters and limits.


def test_the_status_filter_matches_the_database(replayed: TestClient) -> None:
    statuses = {row["status"] for row in rows(replayed, SessionModel, SessionModel.session_id)}
    assert {"ended", "waiting_user"} <= statuses
    for status in [*statuses, *(set(api.SESSION_STATUSES) - statuses)]:
        listed = ok(replayed, f"{API}?status={status}")
        expected = [
            row for row in rows(replayed, SessionModel, *NEWEST_FIRST) if row["status"] == status
        ]
        assert listed == expected


def test_the_statuses_are_the_reducers() -> None:
    assert api.SESSION_STATUSES == ("working", "waiting_user", "idle", "ended", "stale")


def test_the_session_list_is_limited(client: TestClient, clock: Clock) -> None:
    sessions = [f"00000000-0000-4000-8000-{index:012d}" for index in range(55)]
    post(
        client,
        clock,
        [
            (index, {"session_id": session_id, "hook_event_name": "UserPromptSubmit"})
            for index, session_id in enumerate(sessions)
        ],
    )
    persist(client)
    newest_first = sessions[::-1]

    assert [s["session_id"] for s in ok(client, API)] == newest_first[:50]
    assert [s["session_id"] for s in ok(client, f"{API}?limit=500")] == newest_first
    assert [s["session_id"] for s in ok(client, f"{API}?limit=1")] == newest_first[:1]
    assert [s["session_id"] for s in ok(client, f"{API}?limit=2&status=working")] == (
        newest_first[:2]
    )


def test_the_events_are_limited(client: TestClient, clock: Clock) -> None:
    prompt = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"}
    post(client, clock, [(index, prompt) for index in range(60)])
    persist(client)

    assert [e["seq"] for e in ok(client, f"{API}/{SESSION}/events")] == list(range(11, 61))
    assert [e["seq"] for e in ok(client, f"{API}/{SESSION}/events?limit=1")] == [60]
    assert len(ok(client, f"{API}/{SESSION}/events?limit=500")) == 60


@pytest.mark.parametrize("limit", ["0", "-1", "501", "10" * 20, "1.5", "abc", ""])
@pytest.mark.parametrize("path", [API, *(f"{API}/{SESSION}{s}" for s in SUFFIXES[1:])])
def test_a_limit_outside_1_to_500_is_a_validation_error(
    replayed: TestClient, path: str, limit: str
) -> None:
    response = replayed.get(path, params={"limit": limit})

    assert response.status_code == 422
    assert_validation_body(response.json(), "limit")


@pytest.mark.parametrize("status", ["busy", "WORKING", "", "working "])
def test_an_unknown_status_is_a_validation_error(replayed: TestClient, status: str) -> None:
    response = replayed.get(API, params={"status": status})

    assert response.status_code == 422
    assert_validation_body(response.json(), "status")


def assert_validation_body(body: Any, field: str) -> None:
    """FastAPI's 422 body without the ``input`` and ``ctx`` that would echo the value."""
    assert set(body) == {"detail"}
    assert body["detail"]
    for error in body["detail"]:
        assert set(error) == {"loc", "msg", "type"}
        assert error["loc"] == ["query", field]


@pytest.mark.parametrize("field", ["limit", "status"])
def test_a_validation_error_does_not_echo_the_value(replayed: TestClient, field: str) -> None:
    response = replayed.get(API, params={field: CANARY})

    assert response.status_code == 422
    assert CANARY not in response.text


# Bounded detail and keyset paging (ADR 0033).

HISTORY_BLOCKS: Final = 500
HISTORY_AGENTS: Final = 120


def seed_history(client: TestClient, clock: Clock) -> None:
    """``SESSION`` with a long history written straight to its tables: every seventh
    block open, every fifth agent running, agent start times out of the order of their
    IDs (with ties), and every eleventh stopped agent internal (no start time)."""
    post(client, clock, [(0, {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"})])
    persist(client)
    blocks = []
    for index in range(HISTORY_BLOCKS):
        closed = None if index % 7 == 0 else BASE + 10 * index + 5
        blocks.append(
            {
                "session_id": SESSION,
                "host": HOST,
                "block_id": 1_000 + index,
                "kind": "permission",
                "scope_agent_id": None,
                "opened_at_ms": BASE + 10 * index,
                "closed_at_ms": closed,
                "close_reason": None if closed is None else "tool_completed",
                "close_source": None if closed is None else "hook",
            }
        )
    agents = []
    for index in range(HISTORY_AGENTS):
        running = index % 5 == 0
        internal = not running and index % 11 == 0
        started = BASE + ((index * 37) % HISTORY_AGENTS) // 2
        agents.append(
            {
                "session_id": SESSION,
                "host": HOST,
                "agent_id": f"a{index:04d}",
                "status": "running" if running else "stopped",
                "internal": internal,
                "background": False,
                "started_at_ms": None if internal else started,
                "stopped_at_ms": None if running else started + 7,
                "last_event_at_ms": started + 9,
                "stop_reason": None if running else "stopped",
            }
        )
    with engine_of(client).begin() as connection:
        connection.execute(insert(BlockModel), blocks)
        connection.execute(insert(AgentModel), agents)


def all_blocks(client: TestClient) -> list[dict[str, Any]]:
    return rows(
        client, BlockModel, BlockModel.block_id, session_id=SESSION, without=("session_id", "host")
    )


def test_the_detail_holds_what_is_open_and_the_latest_closed(
    client: TestClient, clock: Clock
) -> None:
    seed_history(client, clock)
    agents = expected_agents(client, SESSION)
    blocks = all_blocks(client)
    stopped = [a for a in agents if a["status"] == "stopped"]
    closed = [b for b in blocks if b["closed_at_ms"] is not None]
    latest_stopped = {a["agent_id"] for a in stopped[-50:]}
    latest_closed = {b["block_id"] for b in closed[-50:]}

    detail = ok(client, f"{API}/{SESSION}")

    assert (detail["agents_total"], detail["blocks_total"]) == (HISTORY_AGENTS, HISTORY_BLOCKS)
    assert detail["blocks"] == [
        b for b in blocks if b["closed_at_ms"] is None or b["block_id"] in latest_closed
    ]
    assert detail["agents"] == [
        a for a in agents if a["status"] == "running" or a["agent_id"] in latest_stopped
    ]
    open_blocks = [b for b in detail["blocks"] if b["closed_at_ms"] is None]
    assert len(open_blocks) == len(range(0, HISTORY_BLOCKS, 7))
    assert len(detail["blocks"]) == len(open_blocks) + 50
    assert len(detail["agents"]) == HISTORY_AGENTS // 5 + 50


@pytest.mark.parametrize("limit", [1, 7, 50, 500])
@pytest.mark.parametrize(("suffix", "key"), [("/blocks", "block_id"), ("/agents", "agent_id")])
def test_pages_cover_every_agent_and_block_once(
    client: TestClient, clock: Clock, suffix: str, key: str, limit: int
) -> None:
    seed_history(client, clock)
    expected = all_blocks(client) if key == "block_id" else expected_agents(client, SESSION)
    url = f"{API}/{SESSION}{suffix}"

    pages = [ok(client, f"{url}?limit={limit}")]
    while pages[-1]:
        assert len(pages[-1]) <= limit
        pages.append(ok(client, url, params={"before": pages[-1][0][key], "limit": limit}))

    # Newest page first, each page oldest first: together, every row once, in order.
    assert pages[0] == expected[-limit:]
    assert [row for page in reversed(pages) for row in page] == expected
    assert len(pages) - 1 == -(-len(expected) // limit)
    assert ok(client, url) == expected[-api.DEFAULT_LIMIT :]


def test_event_pages_cover_every_event_once_and_after_seq_only_newer(
    client: TestClient, clock: Clock
) -> None:
    prompt = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"}
    post(client, clock, [(index, prompt) for index in range(120)])
    persist(client)
    url = f"{API}/{SESSION}/events"
    seqs = [row["seq"] for row in rows(client, HookEventModel, HookEventModel.seq)]

    back = [ok(client, url, params={"limit": 7})]
    while back[-1]:
        back.append(ok(client, url, params={"before_seq": back[-1][0]["seq"], "limit": 7}))
    forward = [ok(client, url, params={"after_seq": 0, "limit": 7})]
    while forward[-1]:
        forward.append(ok(client, url, params={"after_seq": forward[-1][-1]["seq"], "limit": 7}))

    assert [e["seq"] for page in reversed(back) for e in page] == seqs
    assert [e["seq"] for page in forward for e in page] == seqs
    assert [e["seq"] for e in ok(client, url, params={"before_seq": 50, "limit": 3})] == [
        47,
        48,
        49,
    ]
    assert [e["seq"] for e in ok(client, url, params={"after_seq": 50, "limit": 3})] == [
        51,
        52,
        53,
    ]
    # The tail: nothing past the last one, then only what came after it.
    assert ok(client, url, params={"after_seq": seqs[-1]}) == []
    post(client, clock, [(200 + index, prompt) for index in range(3)])
    persist(client)
    newer = ok(client, url, params={"after_seq": seqs[-1]})
    assert [e["seq"] for e in newer] == [seqs[-1] + 1, seqs[-1] + 2, seqs[-1] + 3]


def test_before_seq_and_after_seq_together_are_a_validation_error(replayed: TestClient) -> None:
    response = replayed.get(
        f"{API}/{SESSION}/events", params={"before_seq": 987_654, "after_seq": 123_457}
    )

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"}
    ((error),) = body["detail"]
    assert set(error) == {"loc", "msg", "type"}
    assert error["loc"] == ["query"]
    assert "987654" not in response.text
    assert "123457" not in response.text


@pytest.mark.parametrize(
    ("suffix", "name", "value"),
    [
        *(
            (suffix, name, value)
            for suffix, name in (
                ("/blocks", "before"),
                ("/events", "before_seq"),
                ("/events", "after_seq"),
            )
            for value in ("-1", "1.5", "abc", "", str(2**63), CANARY)
        ),
        *(
            ("/agents", "before", value)
            for value in ("", CANARY, "x" * 257 + CANARY, f"{CANARY}\x00", f"{CANARY}\x1b[31m")
        ),
    ],
)
def test_an_invalid_cursor_is_a_validation_error_without_echo(
    client: TestClient, clock: Clock, suffix: str, name: str, value: str
) -> None:
    seed_history(client, clock)

    response = client.get(f"{API}/{SESSION}{suffix}", params={name: value})

    assert response.status_code == 422
    assert_validation_body(response.json(), name)
    assert CANARY not in response.text
    assert "x" * 64 not in response.text


def test_an_agent_cursor_of_another_session_is_a_validation_error(
    client: TestClient, clock: Clock
) -> None:
    seed_history(client, clock)
    start = {"session_id": WAITING, "hook_event_name": "SubagentStart", "agent_id": "elsewhere"}
    post(client, clock, [(0, start)])
    persist(client)
    assert [a["agent_id"] for a in ok(client, f"{API}/{WAITING}/agents")] == ["elsewhere"]

    response = client.get(f"{API}/{SESSION}/agents", params={"before": "elsewhere"})

    assert response.status_code == 422
    assert_validation_body(response.json(), "before")
    assert "elsewhere" not in response.text


def test_the_largest_cursor_is_accepted(client: TestClient, clock: Clock) -> None:
    seed_history(client, clock)
    largest = str(2**63 - 1)
    assert len(ok(client, f"{API}/{SESSION}/blocks?before={largest}")) == api.DEFAULT_LIMIT
    assert len(ok(client, f"{API}/{SESSION}/events?before_seq={largest}")) == 1
    assert ok(client, f"{API}/{SESSION}/events?after_seq={largest}") == []


@pytest.mark.parametrize(
    ("suffix", "params"),
    [
        ("/blocks", {"before": "5"}),
        ("/agents", {"before": "a0001"}),
        ("/events", {"before_seq": "5"}),
        ("/events", {"after_seq": "5"}),
    ],
)
def test_a_cursor_on_an_unknown_session_is_a_404(
    client: TestClient, clock: Clock, suffix: str, params: dict[str, str]
) -> None:
    seed_history(client, clock)

    response = client.get(f"{API}/{UNKNOWN}{suffix}", params=params)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_an_agent_names_the_agent_that_launched_it(client: TestClient, clock: Clock) -> None:
    def hook(name: str, **fields: Any) -> Payload:
        return {"session_id": SESSION, "hook_event_name": name, **fields}

    post(
        client,
        clock,
        [
            (0, hook("PreToolUse", tool_name="Agent", tool_use_id="t1")),
            (1, hook("SubagentStart", agent_id="a1", agent_type="general-purpose")),
            (2, hook("PreToolUse", agent_id="a1", tool_name="Agent", tool_use_id="t2")),
            (3, hook("SubagentStart", agent_id="a2", agent_type="Explore")),
            (4, hook("SubagentStop", agent_id="internal")),
        ],
    )
    persist(client)

    parents = {"a1": None, "a2": "a1", "internal": None}
    detail = ok(client, f"{API}/{SESSION}")
    assert {a["agent_id"]: a["parent_agent_id"] for a in detail["agents"]} == parents
    page = ok(client, f"{API}/{SESSION}/agents")
    assert {a["agent_id"]: a["parent_agent_id"] for a in page} == parents


class CountingGate(ReadGate):
    """A read gate that counts its reads."""

    def __init__(self, *, slots: int) -> None:
        super().__init__(slots=slots)
        self.runs = 0

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        self.runs += 1
        return await super().run(read, *args)


@pytest.mark.parametrize(
    ("path", "status"),
    [
        (API, 200),
        (f"{API}/{SESSION}", 200),
        (f"{API}/{SESSION}/agents", 200),
        (f"{API}/{SESSION}/agents?before=a0050", 200),
        (f"{API}/{SESSION}/blocks", 200),
        (f"{API}/{SESSION}/blocks?before=1100", 200),
        (f"{API}/{SESSION}/events", 200),
        (f"{API}/{SESSION}/events?before_seq=1", 200),
        (f"{API}/{SESSION}/events?after_seq=0", 200),
        (f"{API}/{UNKNOWN}", 404),
        (f"{API}/{UNKNOWN}/agents?before=a0050", 404),
        (f"{API}/{UNKNOWN}/blocks", 404),
        (f"{API}/{UNKNOWN}/events", 404),
        (f"{API}/{SESSION}/agents?before=unknown", 422),
    ],
)
def test_each_request_takes_one_read_slot(
    client: TestClient, clock: Clock, path: str, status: int
) -> None:
    seed_history(client, clock)
    portal = client.portal
    assert portal is not None
    # Built on the app's event loop, like the lifespan's gate. One slot, so a request
    # that asked for a second one while holding the first would fail.
    gate = portal.call(lambda: CountingGate(slots=1))
    app_of(client).state.reads = gate

    response = client.get(path)

    assert response.status_code == status
    assert gate.runs == 1


# Unknown and malicious session IDs.


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_an_unknown_session_is_a_404(replayed: TestClient, suffix: str) -> None:
    response = replayed.get(f"{API}/{UNKNOWN}{suffix}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


# Raw path segments, already percent-encoded where needed.
MALICIOUS_IDS: Final = [
    CANARY + "x" * 300,
    "x" * 256 + CANARY,
    f"{CANARY}%00",
    f"{CANARY}%0A",
    f"{CANARY}%1B%5B31m",
    f"{CANARY}%C2%85",
    f"{CANARY}%ED%A0%80",
    f"..%2F..%2F{CANARY}",
    f"..%5C..%5C{CANARY}",
    f"%2E%2E%2F{CANARY}",
    "..",
    "%2E%2E",
    f"{CANARY}%2Fevents",
]


@pytest.mark.parametrize("suffix", SUFFIXES)
@pytest.mark.parametrize("segment", MALICIOUS_IDS)
def test_a_malicious_session_id_is_refused_without_echo(
    replayed: TestClient, segment: str, suffix: str
) -> None:
    response = replayed.get(f"{API}/{segment}{suffix}")

    assert response.status_code in (404, 422)
    assert CANARY not in response.text
    assert "x" * 64 not in response.text


def test_a_session_id_at_the_bounds_is_looked_up(client: TestClient, clock: Clock) -> None:
    long_id = "s" * 256
    post(client, clock, [(0, {"session_id": long_id, "hook_event_name": "UserPromptSubmit"})])
    persist(client)

    assert ok(client, f"{API}/{long_id}")["session_id"] == long_id
    assert len(ok(client, f"{API}/{long_id}/events")) == 1
    assert client.get(f"{API}/{long_id}s").status_code == 404


# The Host guard and CORS.

ROUTES: Final = (API, *(f"{API}/{SESSION}{suffix}" for suffix in SUFFIXES))


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "127.0.0.1.nip.io"])
def test_every_route_refuses_a_foreign_host(replayed: TestClient, path: str, host: str) -> None:
    response = replayed.get(path, headers={"host": host})

    assert response.status_code == 400
    assert SESSION not in response.text


@pytest.mark.parametrize("path", ROUTES)
def test_no_route_sends_cors_headers(replayed: TestClient, path: str) -> None:
    origin = {"origin": "https://evil.example"}
    preflight = replayed.options(
        path,
        headers={
            **origin,
            "access-control-request-method": "GET",
            "access-control-request-headers": "content-type",
        },
    )
    answers = [replayed.get(path, headers=origin), preflight]

    # No CORS middleware: a preflight meets the router's 405, so the browser blocks it.
    assert preflight.status_code == 405
    for response in answers:
        assert not [name for name in response.headers if name.lower().startswith("access-control")]


def test_the_routes_stay_off_the_default_threadpool_and_out_of_the_schema(
    client: TestClient,
) -> None:
    routes = [route for route in api.router.routes if isinstance(route, APIRoute)]

    assert sorted(route.path for route in routes) == [
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/agents",
        "/api/v1/sessions/{session_id}/blocks",
        "/api/v1/sessions/{session_id}/events",
    ]
    for route in routes:
        # Async def, dependencies included: FastAPI would run a sync def in its default
        # threadpool; the database reads go through the read gate instead (ADR 0033).
        assert inspect.iscoroutinefunction(route.endpoint)
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert calls
        assert all(inspect.iscoroutinefunction(call) for call in calls)
        assert route.methods == {"GET"}
    assert client.get("/openapi.json").status_code == 404


# Readers and the writer (ADR 0033).


class BlockedReads:
    """Holds every session-list read on a reader connection until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self._started = 0
        self._changed = threading.Condition()

    def started(self) -> None:
        with self._changed:
            self._started += 1
            self._changed.notify_all()

    def wait_started(self, count: int) -> None:
        with self._changed:
            assert self._changed.wait_for(lambda: self._started >= count, timeout=PATIENCE_S)


@pytest.fixture
def blocked_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[BlockedReads]:
    blocked = BlockedReads()
    original = Repository.list_sessions

    def list_sessions(self: Repository, limit: int, status: str | None = None) -> list[SessionView]:
        # Holds a connection of the engine it reads through while it waits.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            blocked.started()
            blocked.release.wait(PATIENCE_S)
        return original(self, limit, status)

    monkeypatch.setattr(Repository, "list_sessions", list_sessions)
    try:
        yield blocked
    finally:
        blocked.release.set()


def test_the_api_reads_through_a_query_only_engine_of_its_own(client: TestClient) -> None:
    reader = app_of(client).state.read_repository
    assert isinstance(reader, Repository)
    writer_pool = engine_of(client).pool
    reader_pool = reader.engine.pool
    assert isinstance(writer_pool, QueuePool)
    assert isinstance(reader_pool, QueuePool)
    assert (writer_pool.size(), writer_pool._max_overflow) == (1, 0)
    assert (reader_pool.size(), reader_pool._max_overflow) == (READ_SLOTS, 0)
    with reader.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    assert isinstance(app_of(client).state.reads, ReadGate)


def checked_in(engine: Engine) -> int:
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedin()


def test_shutdown_disposes_both_engines(settings: Settings, clock: Clock) -> None:
    with started(settings, clock) as test_client:
        assert ok(test_client, API) == []
        writer = engine_of(test_client)
        reader = app_of(test_client).state.read_repository.engine
        # A connection each, back in its pool.
        assert (checked_in(reader), checked_in(writer)) == (1, 1)
    # dispose() swaps in an empty pool.
    assert (checked_in(reader), checked_in(writer)) == (0, 0)


def test_a_failed_migration_opens_no_reader_engine(
    settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles: list[str] = []
    real_create = db_engine.create_engine_for

    def create_engine_for(path: Path, *, role: EngineRole) -> Engine:
        roles.append(role)
        return real_create(path, role=role)

    def upgrade_to_head(_engine: Engine) -> None:
        raise MigrationError("the migration left 1 foreign key violations")

    monkeypatch.setattr(app_module, "create_engine_for", create_engine_for)
    monkeypatch.setattr(app_module, "upgrade_to_head", upgrade_to_head)

    with pytest.raises(MigrationError), started(settings, clock):
        pass
    assert roles == ["writer"]


def test_the_writer_commits_while_reads_hold_every_reader_connection(
    client: TestClient, clock: Clock, blocked_reads: BlockedReads
) -> None:
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    # Up to the first prompt: a session opens.
    first = fixture_payloads(REPLAYED[0])[:3]
    committed = threading.Event()

    def write() -> None:
        writer.run_once(0.0)
        committed.set()

    with ThreadPoolExecutor(max_workers=CONCURRENT_READS) as pool:
        reads = [pool.submit(client.get, API) for _ in range(CONCURRENT_READS)]
        # Every reader connection is held; the other reads queue for a slot.
        blocked_reads.wait_started(READ_SLOTS)
        post(client, clock, first)
        thread = threading.Thread(target=write)
        thread.start()
        in_time = committed.wait(WRITE_DEADLINE_S)
        blocked_reads.release.set()
        thread.join(PATIENCE_S)
        statuses = [read.result(timeout=PATIENCE_S).status_code for read in reads]

    assert in_time, "the writer waited on the readers"
    assert statuses == [200] * CONCURRENT_READS
    assert session_ids(client) == [first[0][1]["session_id"]]


def test_a_read_that_finds_no_free_slot_gets_a_neutral_503(
    settings: Settings, clock: Clock, blocked_reads: BlockedReads
) -> None:
    wait_s = 0.2
    with (
        started(settings, clock, read_wait_timeout_s=wait_s) as test_client,
        ThreadPoolExecutor(max_workers=READ_SLOTS) as pool,
    ):
        held = [pool.submit(test_client.get, API) for _ in range(READ_SLOTS)]
        blocked_reads.wait_started(READ_SLOTS)
        begin = time.monotonic()
        # The other routes: every read shares the same slots.
        busy = [
            test_client.get(f"{API}/{SESSION}{suffix}", params={"limit": 7}) for suffix in SUFFIXES
        ]
        elapsed = time.monotonic() - begin
        blocked_reads.release.set()
        statuses = [read.result(timeout=PATIENCE_S).status_code for read in held]
        after = test_client.get(API)

    for response in busy:
        assert response.status_code == 503
        assert response.json() == {"detail": "database busy"}
        assert response.headers["content-type"] == "application/json"
        assert "Traceback" not in response.text
        assert SESSION not in response.text
    # One wait per route; loose, since Windows timers can fire early.
    assert wait_s <= elapsed < PATIENCE_S
    assert statuses == [200] * READ_SLOTS
    assert after.status_code == 200


# Privacy: stored metadata only.


def test_no_content_reaches_any_response(client: TestClient, clock: Clock) -> None:
    post(client, clock, [(offset, with_canaries(doc)) for offset, doc in replay_lines()])
    persist(client)

    bodies = [client.get(API).text, client.get(f"{API}?limit=500&status=idle").text]
    for session_id in session_ids(client):
        for suffix in SUFFIXES:
            bodies.append(client.get(f"{API}/{session_id}{suffix}?limit=500").text)

    assert len(bodies) == 2 + len(SUFFIXES) * SESSIONS
    for text in bodies:
        assert text.startswith(("[", "{"))
        assert CANARY not in text


# Every field the API serves; a new one fails here until it is reviewed (ADR 0023).
SESSION_FIELDS: Final = (
    "session_id",
    "host",
    "status",
    "started_at_ms",
    "last_event_at_ms",
    "last_seq",
    "turn_open",
    "compacting",
    "source",
    "model",
    "permission_mode",
    "effort_level",
    "last_prompt_id",
    "last_error_code",
    "idle_notified_at_ms",
    "ended_at_ms",
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
    "started_at_ms",
    "stopped_at_ms",
    "last_event_at_ms",
    "stop_reason",
    "parent_agent_id",
)
BLOCK_FIELDS: Final = (
    "block_id",
    "kind",
    "scope_agent_id",
    "tool_name",
    "tool_use_id",
    "mcp_server_name",
    "elicitation_id",
    "opened_at_ms",
    "notified_at_ms",
    "closed_at_ms",
    "close_reason",
    "close_source",
)
EVENT_FIELDS: Final = (
    "seq",
    "session_id",
    "received_at_ms",
    "hook_event_name",
    "agent_id",
    "agent_type",
    "prompt_id",
    "tool_name",
    "tool_use_id",
    "mcp_server_name",
    "notification_type",
    "elicitation_id",
    "elicitation_action",
    "compact_trigger",
    "start_source",
    "end_reason",
    "error_code",
    "tool_response_status",
    "duration_ms",
    "is_interrupt",
    "prompt_length",
    "tool_input_bytes",
    "tool_response_bytes",
)
DETAIL_FIELDS: Final = ("agents", "blocks", "agents_total", "blocks_total")
# Not columns of the model's table: the detail's lists and their counts of stored rows,
# and an agent's parent, the agent_id of a stored hook_events row.
DERIVED_FIELDS: Final = (*DETAIL_FIELDS, "parent_agent_id")


@pytest.mark.parametrize(
    ("model", "table", "expected"),
    [
        (api.SessionOut, SessionModel, SESSION_FIELDS),
        (api.AgentOut, AgentModel, AGENT_FIELDS),
        (api.BlockOut, BlockModel, BLOCK_FIELDS),
        (api.EventOut, HookEventModel, EVENT_FIELDS),
        (api.SessionDetailOut, SessionModel, (*SESSION_FIELDS, *DETAIL_FIELDS)),
    ],
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_the_response_fields_are_exactly_these(
    model: type[BaseModel], table: type[Base], expected: tuple[str, ...]
) -> None:
    assert tuple(model.model_fields) == expected
    stored = set(table.__table__.columns.keys())
    # Only stored columns, or values worked out from them: nothing outside the
    # database's allowlist.
    assert set(expected) - set(DERIVED_FIELDS) <= stored
    assert model.model_config.get("extra") == "forbid"


def test_a_response_validation_error_does_not_quote_the_value() -> None:
    # A value that fails a response model must not reach uvicorn's error log: it may be
    # cwd or an ID.
    values: dict[str, Any] = dict.fromkeys(AGENT_FIELDS)
    values |= {"agent_id": "a", "status": "running", "internal": False, "background": False}
    values |= {"last_event_at_ms": BASE, "agent_type": [CANARY], "tool_use_id": {"x": CANARY}}
    with pytest.raises(ValidationError) as raised:
        api.AgentOut(**values)

    assert "agent_type" in str(raised.value)
    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
