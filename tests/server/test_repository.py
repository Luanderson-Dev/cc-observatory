"""The repository over the app's SQLite database (ADR 0026).

The central test replays every hook fixture (ADR 0012) through ``parse_hook``,
``sessions.apply`` and ``Repository.apply_effects``, and rebuilds the state from the
database before every event, as a restart there would: the rebuilt state must equal the
in-memory one, and the tables must end up the same as after an uninterrupted replay.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from ccobs.domain.hooks import HookEvent, parse_hook
from ccobs.domain.sessions import (
    Agent,
    AgentChanged,
    Block,
    BlockClosed,
    BlockNotified,
    BlockOpened,
    Effect,
    SessionState,
    apply,
    close_block_denied,
    mark_stale,
)
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.models import (
    AgentModel,
    BlockModel,
    HookEventModel,
    SessionModel,
    SessionStateModel,
)
from ccobs.server.db.repository import (
    DETAIL_CLOSED_LIMIT,
    Applied,
    HookEventRow,
    Repository,
    hook_event_row,
)
from ccobs.server.migrations import upgrade_to_head

HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
FIXTURES: Final = sorted(HOOK_FIXTURES.glob("*.jsonl"))
HOST: Final = "test-host"
SESSION: Final = "00000000-0000-4000-8000-000000000000"
OTHER: Final = "00000000-0000-4000-8000-000000000001"
BASE: Final = 1_800_000_000_000
TABLES: Final = (SessionModel, AgentModel, BlockModel, HookEventModel, SessionStateModel)


def make_repository(path: Path) -> Repository:
    engine = create_engine_for(path, role="writer")
    upgrade_to_head(engine)
    return Repository(engine)


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return make_repository(tmp_path / "ccobs.db")


def event(name: str, session_id: str = SESSION, **fields: Any) -> HookEvent:
    return parse_hook({"session_id": session_id, "hook_event_name": name, **fields})


def fixture_events(path: Path) -> Iterator[tuple[int, int, HookEvent]]:
    """``(seq, received_at_ms, event)`` for each line of a hook fixture."""
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        document = json.loads(line)
        yield index + 1, BASE + document["offset_ms"], parse_hook(document["payload"])


def dump(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, in primary-key order."""
    with engine.connect() as connection:
        return {
            model.__tablename__: [
                tuple(row)
                for row in connection.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key)
                )
            ]
            for model in TABLES
        }


def count(repo: Repository, model: type[Any], session_id: str | None = None) -> int:
    statement = select(func.count()).select_from(model)
    if session_id is not None:
        statement = statement.where(model.session_id == session_id)
    with repo.engine.connect() as connection:
        return int(connection.execute(statement).scalar_one())


class Writer:
    """Applies events the way the app's writer does: reduce, then persist."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.states: dict[str, SessionState] = {}
        self.agents: dict[tuple[str, str], Agent] = {}
        self.blocks: dict[tuple[str, int], Block] = {}
        self.seq = 0

    def __call__(self, hook_event: HookEvent, at: int, seq: int | None = None) -> SessionState:
        self.seq = self.seq + 1 if seq is None else seq
        sid = hook_event.session_id
        state, effects = apply(self.states.get(sid), hook_event, at, self.seq)
        self.repo.apply_effects(
            HOST, hook_event_row(hook_event, seq=self.seq, received_at_ms=at), effects, state
        )
        self.record(sid, state, effects)
        return state

    def record(self, sid: str, state: SessionState, effects: list[Effect]) -> None:
        self.states[sid] = state
        for effect in effects:
            if isinstance(effect, AgentChanged):
                self.agents[sid, effect.agent.agent_id] = effect.agent
            elif isinstance(effect, BlockOpened | BlockNotified | BlockClosed):
                self.blocks[sid, effect.block.block_id] = effect.block


# The fixtures.


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_a_fixture_replay_stores_the_reducer_output(repo: Repository, path: Path) -> None:
    write = Writer(repo)
    for seq, at, hook_event in fixture_events(path):
        write(hook_event, at, seq)

    ((sid, state),) = write.states.items()
    detail = repo.get_session(sid)
    assert detail is not None
    assert detail.host == HOST
    assert detail.session == state.session
    assert {a.agent_id: a for a in detail.agents} == {
        agent_id: agent for (_, agent_id), agent in write.agents.items()
    }
    assert {b.block_id: b for b in detail.blocks} == {
        block_id: block for (_, block_id), block in write.blocks.items()
    }
    lines = len(path.read_text(encoding="utf-8").splitlines())
    assert count(repo, HookEventModel) == lines
    assert repo.max_seq() == lines
    assert repo.load_state(sid) == state
    expected_open = {} if state.session.status == "ended" else {sid: state}
    assert repo.load_open_states() == expected_open


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_a_restart_before_any_event_rebuilds_the_exact_state(tmp_path: Path, path: Path) -> None:
    straight = Writer(make_repository(tmp_path / "straight.db"))
    restarted_path = tmp_path / "restarted.db"
    restarted = Writer(make_repository(restarted_path))
    for seq, at, hook_event in fixture_events(path):
        sid = hook_event.session_id
        before = straight.states.get(sid)
        straight(hook_event, at, seq)
        # A restart: a new engine, and the state read back from the database only.
        restarted.repo.engine.dispose()
        restarted.repo = Repository(create_engine_for(restarted_path, role="writer"))
        restarted.states = restarted.repo.load_open_states()
        if sid not in restarted.states and (loaded := restarted.repo.load_state(sid)):
            restarted.states[sid] = loaded  # an ended session, which can reopen
        assert restarted.states.get(sid) == before
        restarted(hook_event, at, seq)
        assert restarted.repo.load_state(sid) == straight.states[sid]
    assert dump(restarted.repo.engine) == dump(straight.repo.engine)


def test_the_sweep_and_telemetry_persist_without_a_hook_event(repo: Repository) -> None:
    write = Writer(repo)
    write(event("UserPromptSubmit", prompt="secret"), BASE)
    write(event("PreToolUse", tool_name="Bash", tool_use_id="t1"), BASE + 1)
    write(event("PermissionRequest", tool_name="Bash"), BASE + 2)
    write(event("PreToolUse", tool_name="Agent", tool_use_id="t2"), BASE + 3)
    write(event("SubagentStart", agent_id="a1", agent_type="Explore"), BASE + 4)
    write(event("PreToolUse", agent_id="a1", tool_name="Read", tool_use_id="t3"), BASE + 5)
    write(event("PermissionRequest", agent_id="a1", tool_name="Read"), BASE + 6)

    state, effects = close_block_denied(write.states[SESSION], tool_use_id="t1", at_ms=BASE + 7)
    repo.apply_effects(HOST, None, effects, state)
    write.record(SESSION, state, effects)
    assert repo.load_state(SESSION) == state

    state, effects = mark_stale(state, BASE + 10_000, 1_000)
    repo.apply_effects(HOST, None, effects, state)
    write.record(SESSION, state, effects)
    assert state.session.status == "stale"
    assert repo.load_state(SESSION) == state
    assert repo.load_open_states() == {SESSION: state}
    detail = repo.get_session(SESSION)
    assert detail is not None
    assert {b.block_id: b for b in detail.blocks} == {k[1]: b for k, b in write.blocks.items()}
    assert count(repo, HookEventModel) == 7


def test_agent_and_block_order_survive_a_reload(repo: Repository) -> None:
    # The running agents keep the order they joined in, which is not the order of their
    # IDs or start times.
    write = Writer(repo)
    write(event("SubagentStart", agent_id="b"), BASE + 10)
    write(event("SubagentStart", agent_id="a"), BASE)
    write(event("SubagentStart", agent_id="c"), BASE + 5)
    write(event("PreToolUse", agent_id="b", tool_name="Read", tool_use_id="t1"), BASE + 20)
    # Open blocks keep their opening order, whatever their scopes.
    write(event("PermissionRequest", agent_id="c", tool_name="Bash"), BASE + 21)
    write(event("PermissionRequest", agent_id="b", tool_name="Read"), BASE + 22)
    write(event("PermissionRequest", agent_id="a", tool_name="Edit"), BASE + 23)
    loaded = repo.load_state(SESSION)
    assert loaded is not None
    assert [a.agent_id for a in loaded.agents] == ["b", "a", "c"]
    assert [b.scope for b in loaded.open_blocks] == ["c", "b", "a"]
    assert loaded == write.states[SESSION]


# Batches and transactions.


def test_a_batch_coalesces_each_record_so_the_last_one_wins(repo: Repository) -> None:
    items: list[Applied] = []
    state: SessionState | None = None
    for seq, hook_event in enumerate(
        [
            event("PreToolUse", tool_name="Bash", tool_use_id="t1"),
            event("PermissionRequest", tool_name="Bash"),
            event("Notification", notification_type="permission_prompt"),
            event("PostToolUse", tool_name="Bash", tool_use_id="t1"),
            event("SubagentStart", agent_id="a1"),
            event("SubagentStop", agent_id="a1"),
            event("UserPromptSubmit", session_id=OTHER),
        ],
        start=1,
    ):
        previous = state if hook_event.session_id == SESSION else None
        new_state, effects = apply(previous, hook_event, BASE + seq, seq)
        if hook_event.session_id == SESSION:
            state = new_state
        items.append(
            Applied(
                event_row=hook_event_row(hook_event, seq=seq, received_at_ms=BASE + seq),
                effects=effects,
                state=new_state,
            )
        )
    repo.apply_batch(HOST, items)

    assert state is not None
    detail = repo.get_session(SESSION)
    assert detail is not None
    assert detail.session == state.session
    (block,) = detail.blocks
    assert (block.notified_at_ms, block.closed_at_ms) == (BASE + 3, BASE + 4)
    assert block.close_reason == "tool_completed"
    (agent,) = detail.agents
    assert (agent.status, agent.stop_reason) == ("stopped", "stopped")
    assert repo.load_state(SESSION) == state
    assert count(repo, HookEventModel) == 7
    assert {s.session.session_id for s in repo.list_sessions(10)} == {SESSION, OTHER}


def test_effects_of_one_event_coalesce_too(repo: Repository) -> None:
    state, effects = apply(None, event("PermissionRequest", tool_name="Bash"), BASE, 1)
    (opened,) = [e for e in effects if isinstance(e, BlockOpened)]
    closed = dataclasses.replace(
        opened.block, closed_at_ms=BASE + 5, close_reason="denied", close_source="telemetry"
    )
    repo.apply_effects(
        HOST,
        hook_event_row(event("PermissionRequest"), seq=1, received_at_ms=BASE),
        [opened, BlockClosed(block=closed), *effects[1:]],
        state,
    )
    detail = repo.get_session(SESSION)
    assert detail is not None
    assert detail.blocks == [closed]


def test_a_failed_batch_persists_nothing(repo: Repository) -> None:
    write = Writer(repo)
    write(event("UserPromptSubmit"), BASE)
    before = dump(repo.engine)

    first, first_effects = apply(None, event("UserPromptSubmit", session_id=OTHER), BASE, 2)
    second, second_effects = apply(write.states[SESSION], event("Stop"), BASE + 1, 3)
    items = [
        Applied(
            event_row=hook_event_row(event("UserPromptSubmit", OTHER), seq=2, received_at_ms=BASE),
            effects=first_effects,
            state=first,
        ),
        # seq 1 is taken: the insert fails after the batch's other writes.
        Applied(
            event_row=hook_event_row(event("Stop"), seq=1, received_at_ms=BASE + 1),
            effects=second_effects,
            state=second,
        ),
    ]
    with pytest.raises(IntegrityError):
        repo.apply_batch(HOST, items)

    assert dump(repo.engine) == before
    assert repo.get_session(OTHER) is None


def test_a_failed_write_shows_no_bound_values(repo: Repository) -> None:
    canary = "CANARY-5d1e"
    write = Writer(repo)
    write(event("UserPromptSubmit", prompt_id=canary), BASE, seq=1)
    # A duplicate seq: the failing insert carries the event's metadata.
    with pytest.raises(IntegrityError) as duplicate:
        write(event("UserPromptSubmit", prompt_id=canary), BASE, seq=1)
    assert canary not in str(duplicate.value)
    # A session insert that fails: its statement carries cwd.
    with repo.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER refuse BEFORE INSERT ON sessions"
                " BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        )
    cwd = f"/home/{canary}/project"
    with pytest.raises(IntegrityError) as refused:
        write(event("UserPromptSubmit", session_id=OTHER, cwd=cwd), BASE + 1)
    assert canary not in str(refused.value)
    assert "refused" in str(refused.value)


def test_an_empty_batch_writes_nothing(repo: Repository) -> None:
    repo.apply_batch(HOST, [])
    assert all(rows == [] for rows in dump(repo.engine).values())


def test_max_seq(repo: Repository) -> None:
    assert repo.max_seq() == 0
    write = Writer(repo)
    write(event("UserPromptSubmit"), BASE, seq=41)
    write(event("UserPromptSubmit", session_id=OTHER), BASE, seq=7)
    assert repo.max_seq() == 41


# Retention.


def test_retention_deletes_old_closed_sessions_and_cascades(repo: Repository) -> None:
    write = Writer(repo)
    write(event("UserPromptSubmit"), BASE)
    write(event("SubagentStart", agent_id="a1"), BASE + 1)
    write(event("PermissionRequest", agent_id="a1", tool_name="Bash"), BASE + 2)
    # The sweep makes it stale; its last event stays at BASE + 2.
    state, effects = mark_stale(write.states[SESSION], BASE + 5_000, 1_000)
    repo.apply_effects(HOST, None, effects, state)
    write.record(SESSION, state, effects)
    ended = "00000000-0000-4000-8000-000000000002"
    write(event("UserPromptSubmit", session_id=ended), BASE)
    write(event("SessionEnd", session_id=ended, reason="other"), BASE + 1)
    # Old but still open: never deleted, whatever its age.
    still_open = "00000000-0000-4000-8000-000000000003"
    write(event("UserPromptSubmit", session_id=still_open), BASE)
    write(event("UserPromptSubmit", session_id=OTHER), BASE + 1_000)
    for model in TABLES:
        assert count(repo, model, SESSION) > 0, model

    # Strictly older: a session whose last event is at the cutoff stays.
    assert repo.delete_older_than(BASE + 1) == 0
    assert repo.delete_older_than(BASE + 3) == 2

    for model in TABLES:
        assert count(repo, model, SESSION) == 0, model
        assert count(repo, model, ended) == 0, model
        assert count(repo, model, OTHER) > 0 or model in (AgentModel, BlockModel), model
    assert repo.load_state(SESSION) is None
    assert repo.load_state(OTHER) == write.states[OTHER]
    assert repo.load_state(still_open) == write.states[still_open]


# Rebuilding from inconsistent rows.


def _skips_one(repo: Repository, caplog: pytest.LogCaptureFixture, statement: str) -> SessionState:
    with repo.engine.begin() as connection:
        connection.execute(text(statement))
    with caplog.at_level(logging.WARNING, logger="ccobs.server.db.repository"):
        loaded = repo.load_state(SESSION)
        assert repo.load_open_states().keys() == {SESSION}
    assert loaded is not None
    assert repo.skipped_rows == 2  # once per load
    assert [r.getMessage() for r in caplog.records] == [
        "rebuilt session state without 1 inconsistent rows"
    ] * 2
    return loaded


def _two_agents(repo: Repository) -> SessionState:
    write = Writer(repo)
    write(event("UserPromptSubmit", cwd="/home/someone"), BASE)
    write(event("PreToolUse", tool_name="Read", tool_use_id="t1"), BASE + 1)
    write(event("SubagentStart", agent_id="a1"), BASE + 2)
    write(event("SubagentStart", agent_id="a2"), BASE + 3)
    assert repo.skipped_rows == 0
    return write.states[SESSION]


def test_a_missing_session_state_row_gives_empty_bookkeeping(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    state = _two_agents(repo)
    loaded = _skips_one(repo, caplog, "DELETE FROM session_state")
    assert loaded == dataclasses.replace(state, pending_tools=())
    assert [a.agent_id for a in loaded.agents] == ["a1", "a2"]


def test_a_missing_agent_row_is_skipped(repo: Repository, caplog: pytest.LogCaptureFixture) -> None:
    state = _two_agents(repo)
    loaded = _skips_one(repo, caplog, "DELETE FROM agents WHERE agent_id = 'a1'")
    assert loaded == dataclasses.replace(state, agents=state.agents[1:])


def test_a_running_agent_missing_from_agent_order_joins_at_the_end(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    state = _two_agents(repo)
    loaded = _skips_one(repo, caplog, """UPDATE session_state SET agent_order = '["a2"]'""")
    assert [a.agent_id for a in loaded.agents] == ["a2", "a1"]
    assert loaded == dataclasses.replace(state, agents=state.agents[::-1])


# Host, events and reads.


def test_host_is_on_every_row(repo: Repository) -> None:
    write = Writer(repo)
    for seq, at, hook_event in fixture_events(HOOK_FIXTURES / "headless-tools-subagent.jsonl"):
        write(hook_event, at, seq)
    with repo.engine.connect() as connection:
        for model in TABLES:
            hosts = set(connection.execute(select(model.host).distinct()).scalars())
            assert hosts == {HOST}, model


def test_hook_event_rows_keep_metadata_only() -> None:
    fields = {f.name for f in dataclasses.fields(HookEventRow)}
    assert {"cwd", "model", "prompt", "tool_input", "tool_response", "host"}.isdisjoint(fields)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"hook_event_name": "UserPromptSubmit", "prompt": "four", "prompt_id": "p1"},
            {"prompt_length": 4, "prompt_id": "p1"},
        ),
        (
            {
                "hook_event_name": "PostToolUse",
                "agent_id": "a1",
                "agent_type": "Explore",
                "tool_name": "mcp__srv__do",
                "tool_use_id": "t1",
                "tool_input": {"a": 1},
                "tool_response": {"status": "completed"},
                "duration_ms": 12,
            },
            {
                "agent_id": "a1",
                "agent_type": "Explore",
                "tool_name": "mcp__srv__do",
                "tool_use_id": "t1",
                "mcp_server_name": "srv",
                "tool_input_bytes": 7,
                "tool_response_bytes": 22,
                "tool_response_status": "completed",
                "duration_ms": 12,
            },
        ),
        (
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "duration_ms": 3,
                "is_interrupt": True,
                "error": "secret",
            },
            {"tool_name": "Bash", "duration_ms": 3, "is_interrupt": True},
        ),
        (
            {"hook_event_name": "SessionStart", "source": "startup", "model": "m"},
            {"start_source": "startup"},
        ),
        ({"hook_event_name": "SessionEnd", "reason": "other"}, {"end_reason": "other"}),
        ({"hook_event_name": "StopFailure", "error": "rate_limit"}, {"error_code": "rate_limit"}),
        ({"hook_event_name": "PreCompact", "trigger": "manual"}, {"compact_trigger": "manual"}),
        ({"hook_event_name": "PostCompact", "trigger": "auto"}, {"compact_trigger": "auto"}),
        (
            {"hook_event_name": "Notification", "notification_type": "idle_prompt"},
            {"notification_type": "idle_prompt"},
        ),
        (
            {"hook_event_name": "Elicitation", "mcp_server_name": "srv", "elicitation_id": "e1"},
            {"mcp_server_name": "srv", "elicitation_id": "e1"},
        ),
        (
            {
                "hook_event_name": "ElicitationResult",
                "mcp_server_name": "srv",
                "elicitation_id": "e1",
                "action": "accept",
            },
            {"mcp_server_name": "srv", "elicitation_id": "e1", "elicitation_action": "accept"},
        ),
        ({"hook_event_name": "Stop", "cwd": "/home/someone"}, {}),
        ({"hook_event_name": "SomethingNew"}, {}),
    ],
)
def test_hook_event_row_maps_each_event(
    repo: Repository, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    hook_event = parse_hook({"session_id": SESSION, **payload})
    row = hook_event_row(hook_event, seq=9, received_at_ms=BASE)
    base = HookEventRow(
        seq=9,
        session_id=SESSION,
        received_at_ms=BASE,
        hook_event_name=payload["hook_event_name"],
    )
    assert row == dataclasses.replace(base, **expected)

    state, effects = apply(None, hook_event, BASE, 9)
    repo.apply_effects(HOST, row, effects, state)
    assert repo.list_events(SESSION, limit=10) == [row]


def test_list_events_is_in_seq_order_and_limited(repo: Repository) -> None:
    write = Writer(repo)
    for index in range(5):
        write(event("UserPromptSubmit"), BASE + index)
    write(event("UserPromptSubmit", session_id=OTHER), BASE)
    assert [row.seq for row in repo.list_events(SESSION, limit=3)] == [3, 4, 5]
    assert repo.list_events("missing", limit=3) == []


def test_list_sessions_orders_by_last_event_and_filters_by_status(repo: Repository) -> None:
    write = Writer(repo)
    write(event("UserPromptSubmit"), BASE + 5)
    write(event("UserPromptSubmit", session_id=OTHER), BASE + 1)
    write(event("Stop", session_id=OTHER), BASE + 9)
    third = "00000000-0000-4000-8000-000000000002"
    write(event("SessionEnd", session_id=third, reason="other"), BASE)

    listed = repo.list_sessions(10)
    assert [view.session.session_id for view in listed] == [OTHER, SESSION, third]
    assert {view.host for view in listed} == {HOST}
    assert [v.session.session_id for v in repo.list_sessions(1)] == [OTHER]
    assert [v.session.session_id for v in repo.list_sessions(10, status="idle")] == [OTHER]
    assert [v.session.session_id for v in repo.list_sessions(10, status="ended")] == [third]
    assert repo.load_open_states().keys() == {SESSION, OTHER}


def test_session_exists(repo: Repository) -> None:
    Writer(repo)(event("UserPromptSubmit"), BASE)

    assert repo.session_exists(SESSION)
    assert not repo.session_exists(OTHER)


def test_unknown_sessions(repo: Repository) -> None:
    assert repo.get_session(SESSION) is None
    assert repo.load_state(SESSION) is None
    assert repo.load_open_states() == {}
    assert repo.list_sessions(10) == []


def test_no_content_reaches_the_database(repo: Repository, tmp_path: Path) -> None:
    canary = "CANARY-7f3a"
    write = Writer(repo)
    write(event("UserPromptSubmit", prompt=canary, transcript_path=canary), BASE)
    write(
        event(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="t1",
            tool_input={"command": canary},
            tool_response={"stdout": canary},
        ),
        BASE + 1,
    )
    write(event("PostToolUseFailure", tool_name="Bash", error=canary), BASE + 2)
    write(event("Stop", last_assistant_message=canary), BASE + 3)
    write(event("Notification", notification_type="permission_prompt", message=canary), BASE + 4)
    write(
        event(
            "Elicitation",
            mcp_server_name="srv",
            message=canary,
            requested_schema={"type": "object", "title": canary},
        ),
        BASE + 5,
    )
    write(event("PermissionDenied", tool_name="Bash", reason=canary), BASE + 6)
    write(event("SubagentStop", agent_id="a1", last_assistant_message=canary), BASE + 7)
    write(event("PreCompact", trigger="manual", custom_instructions=canary), BASE + 8)
    assert count(repo, HookEventModel) == 9
    repo.engine.dispose()
    with repo.engine.connect() as connection:
        connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    for path in tmp_path.glob("ccobs.db*"):
        assert canary.encode() not in path.read_bytes(), path.name


# Bounded detail and keyset paging (ADR 0033).

BLOCKS: Final = 500
AGENTS: Final = 300


def seeded(repo: Repository) -> None:
    """``SESSION`` with ``BLOCKS`` blocks and ``AGENTS`` agents written straight to their
    tables (an ``OTHER`` session gets a few of each, which no read of ``SESSION`` may
    return). Every seventh block is open and every fifth agent is running. Agent start
    times are a permutation of the order of their IDs, with ties, and every eleventh
    stopped agent is internal (no start time), so the agents' key differs from their IDs.
    """
    write = Writer(repo)
    write(event("UserPromptSubmit"), BASE)
    write(event("UserPromptSubmit", session_id=OTHER), BASE)
    blocks: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    for sid, total_blocks, total_agents in ((SESSION, BLOCKS, AGENTS), (OTHER, 5, 5)):
        for index in range(total_blocks):
            closed = None if index % 7 == 0 else BASE + 10 * index + 5
            blocks.append(
                {
                    "session_id": sid,
                    "host": HOST,
                    "block_id": 1_000 + 2 * index + (sid == OTHER),
                    "kind": "permission",
                    "scope_agent_id": None,
                    "opened_at_ms": BASE + 10 * index,
                    "closed_at_ms": closed,
                    "close_reason": None if closed is None else "tool_completed",
                    "close_source": None if closed is None else "hook",
                }
            )
        for index in range(total_agents):
            running = index % 5 == 0
            internal = not running and index % 11 == 0
            started = BASE + ((index * 37) % total_agents) // 2
            agents.append(
                {
                    "session_id": sid,
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
    with repo.engine.begin() as connection:
        connection.execute(insert(BlockModel), blocks)
        connection.execute(insert(AgentModel), agents)


def first_seen(agent: Agent) -> int:
    for value in (agent.started_at_ms, agent.stopped_at_ms):
        if value is not None:
            return value
    return agent.last_event_at_ms


def agent_key(agent: Agent) -> tuple[int, str]:
    return first_seen(agent), agent.agent_id


def all_rows(repo: Repository) -> tuple[list[Agent], list[Block]]:
    """Every agent and block of ``SESSION``, in the orders of the reads."""
    stored = ("session_id", "host", "scope_agent_id")
    with repo.engine.connect() as connection:
        agent_rows = connection.execute(
            select(AgentModel.__table__).where(AgentModel.session_id == SESSION)
        ).mappings()
        agents = [Agent(**{k: v for k, v in row.items() if k not in stored}) for row in agent_rows]
        block_rows = connection.execute(
            select(BlockModel.__table__).where(BlockModel.session_id == SESSION)
        ).mappings()
        blocks = [
            Block(scope=row["scope_agent_id"], **{k: v for k, v in row.items() if k not in stored})
            for row in block_rows
        ]
    return sorted(agents, key=agent_key), sorted(blocks, key=lambda b: b.block_id)


def test_the_detail_holds_what_is_open_and_the_latest_closed(repo: Repository) -> None:
    seeded(repo)
    agents, blocks = all_rows(repo)
    stopped = [a for a in agents if a.status == "stopped"]
    closed = [b for b in blocks if b.closed_at_ms is not None]
    latest_stopped = {a.agent_id for a in stopped[-DETAIL_CLOSED_LIMIT:]}
    latest_closed = {b.block_id for b in closed[-DETAIL_CLOSED_LIMIT:]}

    detail = repo.get_session(SESSION)

    assert detail is not None
    assert DETAIL_CLOSED_LIMIT == 50
    assert (detail.agents_total, detail.blocks_total) == (AGENTS, BLOCKS)
    # In the order of the full lists: the open ones whatever their age, then the latest.
    assert detail.blocks == [
        b for b in blocks if b.closed_at_ms is None or b.block_id in latest_closed
    ]
    assert detail.agents == [
        a for a in agents if a.status == "running" or a.agent_id in latest_stopped
    ]
    assert len([b for b in detail.blocks if b.closed_at_ms is None]) == len(range(0, BLOCKS, 7))
    assert len([a for a in detail.agents if a.status == "running"]) == AGENTS // 5
    assert detail.parents == {a.agent_id: None for a in detail.agents}


def test_a_short_session_detail_holds_everything(repo: Repository) -> None:
    write = Writer(repo)
    write(event("SubagentStart", agent_id="a1"), BASE)
    write(event("PermissionRequest", tool_name="Bash"), BASE + 1)
    detail = repo.get_session(SESSION)
    assert detail is not None
    assert (detail.agents_total, detail.blocks_total) == (1, 1)
    assert [a.agent_id for a in detail.agents] == ["a1"]
    assert len(detail.blocks) == 1


LATE: Final = BASE + 10**7


def _close_late(repo: Repository, block: Block, agent: Agent) -> None:
    """Mark ``block`` and ``agent`` as closed and stopped at ``LATE``, long after the
    others, as if they were open and running when a page loaded."""
    with repo.engine.begin() as connection:
        connection.execute(
            update(BlockModel)
            .where(BlockModel.session_id == SESSION, BlockModel.block_id == block.block_id)
            .values(closed_at_ms=LATE)
        )
        connection.execute(
            update(AgentModel)
            .where(AgentModel.session_id == SESSION, AgentModel.agent_id == agent.agent_id)
            .values(stopped_at_ms=LATE)
        )


def test_a_windowed_detail_holds_everything_from_the_boundaries_or_closed_since(
    repo: Repository,
) -> None:
    # The live refresh of a session page (ADR 0032): every open block and running agent,
    # the closed and stopped ones from the page's boundaries up, and the ones that
    # closed or stopped after the page loaded, whatever their position.
    seeded(repo)
    agents, blocks = all_rows(repo)
    closed = [b for b in blocks if b.closed_at_ms is not None]
    stopped = [a for a in agents if a.status == "stopped"]
    old_block = closed[0]
    old_agent = next(a for a in stopped if a.started_at_ms is not None)
    _close_late(repo, old_block, old_agent)
    block_from, agent_from = closed[-30], stopped[-30]

    detail = repo.get_session(
        SESSION,
        closed_limit=500,
        blocks_from=block_from.block_id,
        agents_from=agent_from.agent_id,
        blocks_since_ms=LATE,
        agents_since_ms=LATE,
    )

    assert detail is not None
    assert [b.block_id for b in detail.blocks] == [
        b.block_id
        for b in blocks
        if b.closed_at_ms is None or b.block_id >= block_from.block_id or b == old_block
    ]
    assert [a.agent_id for a in detail.agents] == [
        a.agent_id
        for a in agents
        if a.status == "running" or agent_key(a) >= agent_key(agent_from) or a == old_agent
    ]
    assert (detail.agents_total, detail.blocks_total) == (AGENTS, BLOCKS)


def test_a_windowed_detail_holds_the_kept_rows_whatever_their_close_time(
    repo: Repository,
) -> None:
    # A block open and an agent running when the page loaded, below its boundaries, may
    # close with a time before the page's (a sweep, a late hook): the page names them.
    seeded(repo)
    agents, blocks = all_rows(repo)
    closed = [b for b in blocks if b.closed_at_ms is not None]
    stopped = [a for a in agents if a.status == "stopped"]
    block_from, agent_from = closed[-30], stopped[-30]
    old_block, old_agent = closed[0], stopped[0]

    detail = repo.get_session(
        SESSION,
        closed_limit=500,
        blocks_from=block_from.block_id,
        agents_from=agent_from.agent_id,
        blocks_since_ms=LATE,
        agents_since_ms=LATE,
        keep_blocks=[old_block.block_id],
        keep_agents=[old_agent.agent_id],
    )

    assert detail is not None
    assert old_block in detail.blocks
    assert old_agent in detail.agents
    assert closed[1] not in detail.blocks
    assert stopped[1] not in detail.agents


def test_a_windowed_detail_keeps_the_latest_up_to_its_limit(repo: Repository) -> None:
    seeded(repo)
    agents, blocks = all_rows(repo)
    closed = [b for b in blocks if b.closed_at_ms is not None]
    stopped = [a for a in agents if a.status == "stopped"]

    detail = repo.get_session(
        SESSION,
        closed_limit=10,
        blocks_from=closed[-30].block_id,
        agents_from=stopped[-30].agent_id,
        blocks_since_ms=LATE,
        agents_since_ms=LATE,
    )

    assert detail is not None
    assert [b for b in detail.blocks if b.closed_at_ms is not None] == closed[-10:]
    assert [a for a in detail.agents if a.status == "stopped"] == stopped[-10:]


def test_a_window_without_a_start_time_holds_only_rows_from_its_boundary(
    repo: Repository,
) -> None:
    seeded(repo)
    _, blocks = all_rows(repo)
    closed = [b for b in blocks if b.closed_at_ms is not None]

    detail = repo.get_session(SESSION, closed_limit=500, blocks_from=closed[-30].block_id)

    assert detail is not None
    assert [b for b in detail.blocks if b.closed_at_ms is not None] == closed[-30:]


def test_an_unknown_agent_boundary_holds_the_latest_stopped_agents(repo: Repository) -> None:
    seeded(repo)
    agents, _ = all_rows(repo)
    stopped = [a for a in agents if a.status == "stopped"]

    detail = repo.get_session(SESSION, agents_from="no-such-agent", agents_since_ms=LATE)

    assert detail is not None
    assert [a for a in detail.agents if a.status == "stopped"] == stopped[-DETAIL_CLOSED_LIMIT:]


@pytest.mark.parametrize("limit", [1, 7, 50, 500])
def test_block_pages_cover_every_block_once(repo: Repository, limit: int) -> None:
    seeded(repo)
    _, blocks = all_rows(repo)
    pages: list[list[Block]] = []
    page = repo.list_blocks(SESSION, limit)
    while page:
        assert len(page) <= limit
        pages.append(page)
        page = repo.list_blocks(SESSION, limit, before=page[0].block_id)

    # The first page is the latest; each page is in block_id order and older than the last.
    assert pages[0] == blocks[-limit:]
    assert [b for p in reversed(pages) for b in p] == blocks
    assert len(pages) == -(-BLOCKS // limit)


def test_a_block_cursor_is_exclusive(repo: Repository) -> None:
    seeded(repo)
    _, blocks = all_rows(repo)
    assert repo.list_blocks(SESSION, 3, before=blocks[100].block_id) == blocks[97:100]
    assert repo.list_blocks(SESSION, 3, before=blocks[0].block_id) == []
    assert repo.list_blocks(SESSION, 3, before=0) == []
    assert repo.list_blocks("missing", 3) == []


@pytest.mark.parametrize("limit", [1, 7, 50, 500])
def test_agent_pages_cover_every_agent_once(repo: Repository, limit: int) -> None:
    seeded(repo)
    agents, _ = all_rows(repo)
    pages: list[list[Agent]] = []
    page = repo.list_agents(SESSION, limit)
    while page is not None and page.agents:
        assert len(page.agents) <= limit
        assert page.parents == {a.agent_id: None for a in page.agents}
        pages.append(page.agents)
        page = repo.list_agents(SESSION, limit, before=page.agents[0].agent_id)

    assert page is not None
    assert pages[0] == agents[-limit:]
    assert [a for p in reversed(pages) for a in p] == agents
    assert len(pages) == -(-AGENTS // limit)


def test_an_agent_cursor_is_exclusive_and_must_name_an_agent_of_the_session(
    repo: Repository,
) -> None:
    seeded(repo)
    agents, _ = all_rows(repo)
    page = repo.list_agents(SESSION, 3, before=agents[100].agent_id)
    assert page is not None
    assert page.agents == agents[97:100]
    first = repo.list_agents(SESSION, 3, before=agents[0].agent_id)
    assert first is not None
    assert first.agents == []
    # Unknown, or an agent of another session: no position to page from.
    assert repo.list_agents(SESSION, 3, before="a9999") is None
    with repo.engine.begin() as connection:
        connection.execute(
            update(AgentModel)
            .where(AgentModel.session_id == OTHER, AgentModel.agent_id == "a0001")
            .values(agent_id="only-other")
        )
    assert repo.list_agents(SESSION, 3, before="only-other") is None


def test_event_pages_before_and_after_a_seq(repo: Repository) -> None:
    write = Writer(repo)
    for index in range(30):
        write(event("UserPromptSubmit", session_id=OTHER if index % 3 == 0 else SESSION), BASE)
    mine = [row.seq for row in repo.list_events(SESSION, 500)]
    assert len(mine) == 20

    # Before: the limit events just below the cursor, in seq order.
    assert [r.seq for r in repo.list_events(SESSION, 3, before_seq=mine[10])] == mine[7:10]
    assert [r.seq for r in repo.list_events(SESSION, 3, before_seq=mine[0])] == []
    # After: the limit events just above the cursor, in seq order.
    assert [r.seq for r in repo.list_events(SESSION, 3, after_seq=mine[10])] == mine[11:14]
    assert [r.seq for r in repo.list_events(SESSION, 3, after_seq=0)] == mine[:3]
    assert [r.seq for r in repo.list_events(SESSION, 3, after_seq=mine[-1])] == []
    # Paging back from the latest page, and tailing forward from nothing, cover each once.
    back: list[int] = []
    page = [r.seq for r in repo.list_events(SESSION, 7)]
    while page:
        back = page + back
        page = [r.seq for r in repo.list_events(SESSION, 7, before_seq=page[0])]
    forward: list[int] = []
    cursor = 0
    while page := [r.seq for r in repo.list_events(SESSION, 7, after_seq=cursor)]:
        forward += page
        cursor = page[-1]
    assert back == forward == mine
    with pytest.raises(ValueError, match="not both"):
        repo.list_events(SESSION, 3, before_seq=5, after_seq=1)


def test_launch_scopes_name_the_scope_of_each_launching_call(repo: Repository) -> None:
    write = Writer(repo)
    write(event("PreToolUse", tool_name="Agent", tool_use_id="t1"), BASE)
    write(event("SubagentStart", agent_id="a1", agent_type="general-purpose"), BASE + 1)
    write(event("PreToolUse", agent_id="a1", tool_name="Agent", tool_use_id="t2"), BASE + 2)
    write(event("SubagentStart", agent_id="a2", agent_type="Explore"), BASE + 3)
    # Its PostToolUse names the same call: only the PreToolUse counts as the launch.
    write(event("PostToolUse", agent_id="a1", tool_name="Agent", tool_use_id="t2"), BASE + 4)
    # A second PreToolUse row for t1, from another scope: the first row (lower seq) wins.
    write(event("PreToolUse", agent_id="a1", tool_name="Agent", tool_use_id="t1"), BASE + 4)
    # Never linked to a call.
    write(event("SubagentStop", agent_id="internal"), BASE + 5)
    write(event("PreToolUse", tool_name="Agent", tool_use_id="t9", session_id=OTHER), BASE + 6)
    assert {a.agent_id: a.tool_use_id for a in write.agents.values()} == {
        "a1": "t1",
        "a2": "t2",
        "internal": None,
    }

    assert repo.launch_scopes(SESSION, ["t1", "t2", "t9", "unknown"]) == {
        "t1": None,
        "t2": "a1",
    }
    assert repo.launch_scopes(SESSION, []) == {}
    detail = repo.get_session(SESSION)
    assert detail is not None
    assert detail.parents == {"a1": None, "a2": "a1", "internal": None}
    page = repo.list_agents(SESSION, 50)
    assert page is not None
    assert page.parents == detail.parents
