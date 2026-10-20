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
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast

import pytest
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError

from ccobs.domain.alerts import (
    DAY_SUBJECT,
    AlertCleared,
    AlertConfig,
    AlertFired,
    AlertKey,
    AlertRule,
    AlertState,
    KeyState,
    evaluate_repeated_failures,
    evaluate_waiting_user,
    record_failure_event,
)
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
from ccobs.server.config import Settings
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.models import (
    AgentModel,
    AlertModel,
    AlertSettingModel,
    BlockModel,
    HookEventModel,
    SessionModel,
    SessionStateModel,
)
from ccobs.server.db.repository import (
    ALERT_PAGE_MAX,
    ALERT_SETTING_TYPES,
    DELIVERY_BATCH_MAX,
    DETAIL_CLOSED_LIMIT,
    AlertRecord,
    AlertSettingValue,
    AlertView,
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
    assert repo.delete_older_than(BASE + 1) == frozenset()
    assert repo.delete_older_than(BASE + 3) == {SESSION, ended}

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


def test_find_session_returns_the_session_alone(repo: Repository) -> None:
    Writer(repo)(event("UserPromptSubmit"), BASE)

    found = repo.find_session(SESSION)
    detail = repo.get_session(SESSION)

    assert detail is not None
    assert found == detail.session
    assert repo.find_session(OTHER) is None


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


# Alerts (ADR 0042).


def fired(
    subject: str = SESSION,
    rule: AlertRule = "waiting_user",
    at: int = BASE,
    value: float = 31.5,
    threshold: float = 30.0,
) -> AlertFired:
    return AlertFired(rule=rule, subject=subject, fired_at_ms=at, value=value, threshold=threshold)


def daily(at: int = BASE, value: float = 51.25) -> AlertFired:
    return fired(DAY_SUBJECT, "daily_cost", at, value, 50.0)


def with_sessions(repo: Repository, *session_ids: str) -> Writer:
    write = Writer(repo)
    for session_id in session_ids or (SESSION, OTHER):
        write(event("UserPromptSubmit", session_id=session_id), BASE)
    return write


def reader_of(repo: Repository) -> Repository:
    """A repository on the readers' engine of the same file (ADR 0033)."""
    database = repo.engine.url.database
    assert database is not None
    return Repository(create_engine_for(Path(database), role="reader"))


def test_an_inserted_alert_reads_back_exactly(repo: Repository) -> None:
    with_sessions(repo)
    first = repo.insert_alert(HOST, fired(value=0.1, threshold=30), discord_state="none")
    second = repo.insert_alert(HOST, daily(BASE + 5), discord_state="pending", discord_next_at_ms=7)
    assert 0 < first < second
    newest, oldest = repo.list_alerts(HOST, 10)
    assert oldest == AlertRecord(
        id=first,
        rule="waiting_user",
        subject=SESSION,
        session_id=SESSION,
        fired_at_ms=BASE,
        cleared_at_ms=None,
        value=0.1,
        threshold=30.0,
        discord_state="none",
        discord_attempts=0,
        discord_next_at_ms=None,
    )
    # daily_cost has no session: its subject is the day.
    assert newest == AlertRecord(
        id=second,
        rule="daily_cost",
        subject=DAY_SUBJECT,
        session_id=None,
        fired_at_ms=BASE + 5,
        cleared_at_ms=None,
        value=51.25,
        threshold=50.0,
        discord_state="pending",
        discord_attempts=0,
        discord_next_at_ms=7,
    )


def test_an_alert_of_an_unknown_session_is_refused(repo: Repository) -> None:
    with pytest.raises(IntegrityError):
        repo.insert_alert(HOST, fired(), discord_state="none")
    assert repo.list_alerts(HOST, 10) == []


def test_clearing_closes_the_open_alert_of_its_key_only(repo: Repository) -> None:
    with_sessions(repo)
    mine = repo.insert_alert(HOST, fired(), discord_state="none")
    other_session = repo.insert_alert(HOST, fired(OTHER), discord_state="none")
    other_rule = repo.insert_alert(HOST, fired(rule="repeated_failures"), discord_state="none")
    day = repo.insert_alert(HOST, daily(), discord_state="none")
    cleared = AlertCleared(rule="waiting_user", subject=SESSION, cleared_at_ms=BASE + 9)

    assert repo.clear_alert("other-host", cleared) == 0
    assert repo.clear_alert(HOST, cleared) == 1
    # Already cleared: nothing open left for the key, and the first clear time stays.
    assert repo.clear_alert(HOST, dataclasses.replace(cleared, cleared_at_ms=BASE + 20)) == 0
    day_cleared = AlertCleared(rule="daily_cost", subject=DAY_SUBJECT, cleared_at_ms=BASE + 3)
    assert repo.clear_alert(HOST, day_cleared) == 1

    by_id = {a.id: a.cleared_at_ms for a in repo.list_alerts(HOST, 10)}
    assert by_id == {mine: BASE + 9, other_session: None, other_rule: None, day: BASE + 3}


def test_a_clear_is_never_earlier_than_its_fire(repo: Repository) -> None:
    # The domain clamps it already (ADR 0041); the store clamps too, so a clock that
    # went backwards can never break the CHECK and lose the write.
    with_sessions(repo)
    repo.insert_alert(HOST, fired(at=BASE + 100), discord_state="none")
    early = AlertCleared(rule="waiting_user", subject=SESSION, cleared_at_ms=BASE)
    assert repo.clear_alert(HOST, early) == 1
    (alert,) = repo.list_alerts(HOST, 10)
    assert alert.cleared_at_ms == BASE + 100


def test_a_ticks_alert_effects_are_written_in_order_in_one_transaction(
    repo: Repository,
) -> None:
    with_sessions(repo)
    repo.insert_alert(HOST, fired(OTHER), discord_state="none")
    cleared = AlertCleared(rule="waiting_user", subject=OTHER, cleared_at_ms=BASE + 5)

    repo.apply_alert_effects(
        HOST, [fired(at=BASE + 5), cleared, daily(BASE + 5)], discord_configured=False
    )

    newest, middle, oldest = repo.list_alerts(HOST, 10)
    assert (oldest.subject, oldest.cleared_at_ms) == (OTHER, BASE + 5)
    assert (middle.subject, middle.rule, middle.fired_at_ms) == (SESSION, "waiting_user", BASE + 5)
    assert (newest.subject, newest.rule) == (DAY_SUBJECT, "daily_cost")
    # Discord was not configured: never sent.
    assert {(a.discord_state, a.discord_next_at_ms) for a in (middle, newest)} == {("none", None)}


def test_with_discord_configured_a_fired_alert_is_due_at_once(repo: Repository) -> None:
    with_sessions(repo)

    repo.apply_alert_effects(HOST, [fired(at=BASE + 7)], discord_configured=True)

    (alert,) = repo.list_alerts(HOST, 10)
    assert (alert.discord_state, alert.discord_next_at_ms, alert.discord_attempts) == (
        "pending",
        BASE + 7,
        0,
    )
    assert repo.due_deliveries(HOST, BASE + 7, 10) == [alert]


def test_a_failed_effect_rolls_back_the_whole_tick(repo: Repository) -> None:
    with_sessions(repo, SESSION)
    repo.insert_alert(HOST, fired(), discord_state="none")
    cleared = AlertCleared(rule="waiting_user", subject=SESSION, cleared_at_ms=BASE + 5)

    # The second write refers to a session that is not stored.
    with pytest.raises(IntegrityError):
        repo.apply_alert_effects(
            HOST, [cleared, fired(OTHER, at=BASE + 5), daily()], discord_configured=False
        )

    (alert,) = repo.list_alerts(HOST, 10)
    assert alert.subject == SESSION
    assert alert.cleared_at_ms is None


def test_no_alert_effects_open_no_transaction(repo: Repository) -> None:
    begun: list[object] = []
    record = begun.append
    sa_event.listen(repo.engine, "begin", record)
    try:
        repo.apply_alert_effects(HOST, [], discord_configured=True)
    finally:
        sa_event.remove(repo.engine, "begin", record)
    assert begun == []


def test_alert_pages_by_id_cover_every_alert_once(repo: Repository) -> None:
    with_sessions(repo)
    ids = [repo.insert_alert(HOST, fired(at=BASE + i), discord_state="none") for i in range(8)]
    repo.insert_alert("other-host", fired(OTHER), discord_state="none")
    newest_first = ids[::-1]

    seen: list[int] = []
    page = repo.list_alerts(HOST, 3)
    while page:
        seen += [a.id for a in page]
        page = repo.list_alerts(HOST, 3, before_id=page[-1].id)
    assert seen == newest_first
    # The cursor is exclusive and a position: an ID deleted since still pages.
    assert [a.id for a in repo.list_alerts(HOST, 2, before_id=ids[5])] == [ids[4], ids[3]]
    assert repo.list_alerts(HOST, 2, before_id=ids[0]) == []
    assert repo.list_alerts(HOST, 0) == []


def test_alert_views_page_both_ways_with_the_sessions_cwd(repo: Repository) -> None:
    write = Writer(repo)
    write(event("UserPromptSubmit", session_id=SESSION, cwd="/home/someone/proj"), BASE)
    write(event("UserPromptSubmit", session_id=OTHER), BASE)
    session_alert = repo.insert_alert(HOST, fired(), discord_state="none")
    day = repo.insert_alert(HOST, daily(), discord_state="pending", discord_next_at_ms=BASE)
    no_cwd = repo.insert_alert(HOST, fired(OTHER), discord_state="none")
    ids = [repo.insert_alert(HOST, fired(at=BASE + i), discord_state="none") for i in range(5)]
    repo.insert_alert("other-host", fired(OTHER), discord_state="none")
    every = [session_alert, day, no_cwd, *ids]

    views = repo.alert_views(HOST, 10)
    assert [v.alert.id for v in views] == every[::-1]
    by_id = {v.alert.id: v for v in views}
    assert by_id[session_alert] == AlertView(
        alert=repo.list_alerts(HOST, 1, before_id=day)[0], cwd="/home/someone/proj"
    )
    assert by_id[day].cwd is None
    assert by_id[day].alert.session_id is None
    assert by_id[no_cwd].cwd is None

    # Older pages, newest first, below an exclusive cursor.
    seen: list[int] = []
    page = repo.alert_views(HOST, 3)
    while page:
        seen += [v.alert.id for v in page]
        page = repo.alert_views(HOST, 3, before_id=page[-1].alert.id)
    assert seen == every[::-1]
    # Newer ones, oldest first, above an exclusive cursor: a client catches up in order.
    seen = []
    page = repo.alert_views(HOST, 3, after_id=0)
    while page:
        seen += [v.alert.id for v in page]
        page = repo.alert_views(HOST, 3, after_id=page[-1].alert.id)
    assert seen == every
    assert [v.alert.id for v in repo.alert_views(HOST, 2, after_id=no_cwd)] == ids[:2]
    assert repo.alert_views(HOST, 2, after_id=ids[-1]) == []
    assert repo.alert_views(HOST, 0) == []
    with pytest.raises(ValueError, match="not both"):
        repo.alert_views(HOST, 2, before_id=ids[-1], after_id=0)


def test_alert_views_are_capped_both_ways(repo: Repository) -> None:
    with_sessions(repo)
    row = {
        "host": HOST,
        "rule": "waiting_user",
        "session_id": SESSION,
        "subject": SESSION,
        "fired_at_ms": BASE,
        "value": 1.0,
        "threshold": 1.0,
        "discord_state": "none",
    }
    with repo.engine.begin() as connection:
        connection.execute(insert(AlertModel), [row] * (ALERT_PAGE_MAX + 1))
    assert len(repo.alert_views(HOST, 10_000)) == ALERT_PAGE_MAX
    oldest = repo.alert_views(HOST, 10_000, after_id=0)
    assert len(oldest) == ALERT_PAGE_MAX
    assert oldest[0].alert.id < oldest[-1].alert.id


def test_an_alert_page_is_capped(repo: Repository) -> None:
    with_sessions(repo)
    row = {
        "host": HOST,
        "rule": "waiting_user",
        "session_id": SESSION,
        "subject": SESSION,
        "fired_at_ms": BASE,
        "value": 1.0,
        "threshold": 1.0,
        "discord_state": "none",
    }
    with repo.engine.begin() as connection:
        connection.execute(insert(AlertModel), [row] * (ALERT_PAGE_MAX + 1))
    assert len(repo.list_alerts(HOST, 10_000)) == ALERT_PAGE_MAX == 500
    assert len(repo.list_alerts(HOST, ALERT_PAGE_MAX + 1)) == ALERT_PAGE_MAX


def test_due_deliveries_are_the_pending_ones_due_in_order_and_capped(repo: Repository) -> None:
    with_sessions(repo)

    def pending(next_at: int, host: str = HOST) -> int:
        return repo.insert_alert(host, fired(), discord_state="pending", discord_next_at_ms=next_at)

    late = pending(BASE + 50)
    soon = pending(BASE + 10)
    tied = pending(BASE + 10)
    not_yet = pending(BASE + 101)
    pending(BASE, host="other-host")
    repo.insert_alert(HOST, fired(), discord_state="sent")
    repo.insert_alert(HOST, fired(), discord_state="none")

    due = repo.due_deliveries(HOST, BASE + 100, 10)
    assert [a.id for a in due] == [soon, tied, late]
    assert [a.id for a in repo.due_deliveries(HOST, BASE + 101, 10)] == [soon, tied, late, not_yet]
    assert [a.id for a in repo.due_deliveries(HOST, BASE + 100, 2)] == [soon, tied]
    assert repo.due_deliveries(HOST, BASE + 9, 10) == []
    assert DELIVERY_BATCH_MAX == 100


def test_due_deliveries_are_capped(repo: Repository) -> None:
    with_sessions(repo)
    row = {
        "host": HOST,
        "rule": "waiting_user",
        "session_id": SESSION,
        "subject": SESSION,
        "fired_at_ms": BASE,
        "value": 1.0,
        "threshold": 1.0,
        "discord_state": "pending",
        "discord_next_at_ms": BASE,
    }
    with repo.engine.begin() as connection:
        connection.execute(insert(AlertModel), [row] * (DELIVERY_BATCH_MAX + 1))
    assert len(repo.due_deliveries(HOST, BASE, 10_000)) == DELIVERY_BATCH_MAX


def test_set_delivery_moves_an_alert_through_its_states(repo: Repository) -> None:
    with_sessions(repo)
    alert_id = repo.insert_alert(HOST, fired(), discord_state="pending", discord_next_at_ms=BASE)

    assert repo.set_delivery(HOST, alert_id, state="pending", attempts=1, next_at_ms=BASE + 30)
    assert repo.due_deliveries(HOST, BASE + 29, 10) == []
    (retry,) = repo.due_deliveries(HOST, BASE + 30, 10)
    assert (retry.discord_attempts, retry.discord_next_at_ms) == (1, BASE + 30)

    assert repo.set_delivery(HOST, alert_id, state="sent", attempts=2)
    (sent,) = repo.list_alerts(HOST, 10)
    assert (sent.discord_state, sent.discord_attempts, sent.discord_next_at_ms) == ("sent", 2, None)
    assert repo.due_deliveries(HOST, BASE + 10**6, 10) == []

    # Another host's alert, or an unknown one, is left alone.
    assert not repo.set_delivery("other-host", alert_id, state="failed", attempts=3)
    assert not repo.set_delivery(HOST, alert_id + 1, state="failed", attempts=3)
    (still,) = repo.list_alerts(HOST, 10)
    assert still.discord_state == "sent"
    # A pending delivery needs a time.
    with pytest.raises(IntegrityError):
        repo.set_delivery(HOST, alert_id, state="pending", attempts=3)


def test_alert_keys_rebuild_firing_keys_and_running_cooldowns(repo: Repository) -> None:
    with_sessions(repo)
    since = BASE + 1_000
    # Firing, whatever its age.
    repo.insert_alert(HOST, fired(at=BASE), discord_state="none")
    # Cleared, fired within the cooldown: kept for its cooldown, not firing.
    repo.insert_alert(HOST, fired(OTHER, at=since), discord_state="none")
    repo.clear_alert(HOST, AlertCleared(rule="waiting_user", subject=OTHER, cleared_at_ms=since))
    # Cleared, fired before the cooldown window: forgotten.
    repo.insert_alert(HOST, fired(rule="repeated_failures", at=since - 1), discord_state="none")
    repo.clear_alert(
        HOST, AlertCleared(rule="repeated_failures", subject=SESSION, cleared_at_ms=since)
    )
    # Several fires of one key: the latest counts, and an open one makes it firing.
    repo.insert_alert(HOST, daily(BASE), discord_state="none")
    repo.clear_alert(
        HOST, AlertCleared(rule="daily_cost", subject=DAY_SUBJECT, cleared_at_ms=BASE + 1)
    )
    repo.insert_alert(HOST, daily(since + 5), discord_state="none")
    # Another host's alerts never count.
    repo.insert_alert("other-host", fired(rule="session_cost"), discord_state="none")

    assert repo.alert_keys(HOST, cooldown_since_ms=since) == {
        AlertKey(rule="waiting_user", subject=SESSION): KeyState(
            last_fired_at_ms=BASE, firing=True
        ),
        AlertKey(rule="waiting_user", subject=OTHER): KeyState(
            last_fired_at_ms=since, firing=False
        ),
        AlertKey(rule="daily_cost", subject=DAY_SUBJECT): KeyState(
            last_fired_at_ms=since + 5, firing=True
        ),
    }
    assert repo.alert_keys("nobody", cooldown_since_ms=since) == {}


def test_rebuilt_keys_keep_the_cooldown_across_a_restart(repo: Repository) -> None:
    # The evaluator fires, the app restarts, and the rebuilt state does not fire again
    # within the cooldown.
    write = with_sessions(repo, SESSION)
    write(event("PermissionRequest", tool_name="Bash"), BASE + 1)
    config = AlertConfig()
    state, effects = evaluate_waiting_user(
        AlertState(), write.states.values(), config=config, now_ms=BASE + 40_000
    )
    (fire,) = effects
    assert isinstance(fire, AlertFired)
    repo.insert_alert(HOST, fire, discord_state="none")

    now = BASE + 50_000
    keys = repo.alert_keys(HOST, cooldown_since_ms=now - config.cooldown_s * 1000)
    assert keys == state.keys
    rebuilt = AlertState(keys=keys)
    assert evaluate_waiting_user(rebuilt, write.states.values(), config=config, now_ms=now) == (
        rebuilt,
        [],
    )


def _failures(write: Writer, at: int, session_id: str = SESSION, **fields: Any) -> None:
    write(event("StopFailure", session_id=session_id, error="rate_limit", **fields), at)


def test_failure_windows_rebuild_the_streaks_after_the_last_successful_turn(
    repo: Repository,
) -> None:
    write = with_sessions(repo, SESSION, OTHER, "ended", "reset")
    _failures(write, BASE + 1)
    write(event("Stop"), BASE + 2)  # a successful main turn: drops what came before
    _failures(write, BASE + 3)
    _failures(write, BASE + 4, agent_id="a1")  # a subagent's failure counts
    write(event("Stop", agent_id="a1"), BASE + 5)  # a subagent's Stop resets nothing
    write(event("SubagentStop", agent_id="a1"), BASE + 6)
    _failures(write, BASE + 7)
    # Out of order: received later with an earlier time, still counts by its time.
    _failures(write, BASE + 2_500, session_id=OTHER)
    _failures(write, BASE + 2_000, session_id=OTHER)
    # Ended: none of its failures can alert.
    _failures(write, BASE + 8, session_id="ended")
    write(event("SessionEnd", session_id="ended", reason="other"), BASE + 9)
    # Reopened after a SessionEnd: only the failures after it count.
    _failures(write, BASE + 10, session_id="reset")
    write(event("SessionEnd", session_id="reset", reason="clear"), BASE + 11)
    write(event("SessionStart", session_id="reset", source="clear"), BASE + 12)
    _failures(write, BASE + 13, session_id="reset")
    assert write.states["ended"].session.status == "ended"
    assert write.states["reset"].session.status != "ended"

    assert repo.failure_windows(HOST, since_ms=BASE, count=5) == {
        SESSION: (BASE + 3, BASE + 4, BASE + 7),
        OTHER: (BASE + 2_000, BASE + 2_500),
        "reset": (BASE + 13,),
    }
    # The newest count times of each session, and only those at or after since_ms.
    assert repo.failure_windows(HOST, since_ms=BASE + 4, count=2) == {
        SESSION: (BASE + 4, BASE + 7),
        OTHER: (BASE + 2_000, BASE + 2_500),
        "reset": (BASE + 13,),
    }
    assert repo.failure_windows(HOST, since_ms=BASE + 2_001, count=3) == {OTHER: (BASE + 2_500,)}
    assert repo.failure_windows("other-host", since_ms=BASE, count=5) == {}


def test_failure_windows_equal_the_domain_window_after_a_replay(repo: Repository) -> None:
    # The same events through record_failure_event and through the store: after a
    # restart, the rebuilt windows are exactly what the evaluator held (ADR 0041).
    config = AlertConfig()
    write = with_sessions(repo, SESSION, OTHER)
    state = AlertState()
    steps: list[tuple[str, str, dict[str, Any]]] = [
        ("StopFailure", SESSION, {"error": "rate_limit"}),
        ("StopFailure", OTHER, {"error": "server_error"}),
        ("StopFailure", SESSION, {"error": "rate_limit", "agent_id": "a1"}),
        ("Stop", SESSION, {"agent_id": "a1"}),
        ("StopFailure", SESSION, {"error": "unknown"}),
        ("StopFailure", SESSION, {"error": "unknown"}),
        ("StopFailure", SESSION, {"error": "unknown"}),
        ("Stop", OTHER, {}),
        ("StopFailure", OTHER, {"error": "server_error"}),
    ]
    for index, (name, session_id, fields) in enumerate(steps):
        at = BASE + 60_000 * index
        hook_event = event(name, session_id=session_id, **fields)
        write(hook_event, at)
        state = record_failure_event(state, hook_event, at, config=config)
    now = BASE + 60_000 * len(steps)
    evaluated, _ = evaluate_repeated_failures(state, config=config, now_ms=now)

    window_ms = config.repeated_failures.window_s * 1000
    rebuilt = repo.failure_windows(
        HOST, since_ms=now - window_ms, count=config.repeated_failures.count
    )
    assert rebuilt == evaluated.failures
    assert rebuilt == {
        SESSION: (BASE + 240_000, BASE + 300_000, BASE + 360_000),
        OTHER: (now - 60_000,),
    }


def test_alert_reads_run_on_the_readers_engine(repo: Repository) -> None:
    write = with_sessions(repo)
    _failures(write, BASE + 1)
    repo.insert_alert(HOST, fired(), discord_state="pending", discord_next_at_ms=BASE)
    repo.save_alert_setting(HOST, "alert_cooldown_s", 60, updated_at_ms=BASE)
    reader = reader_of(repo)
    try:
        assert reader.list_alerts(HOST, 10) == repo.list_alerts(HOST, 10) != []
        assert reader.alert_views(HOST, 10) == repo.alert_views(HOST, 10) != []
        assert reader.due_deliveries(HOST, BASE, 10) == repo.due_deliveries(HOST, BASE, 10)
        assert reader.alert_keys(HOST, cooldown_since_ms=BASE) == repo.alert_keys(
            HOST, cooldown_since_ms=BASE
        )
        assert reader.failure_windows(HOST, since_ms=BASE, count=3) == {SESSION: (BASE + 1,)}
        assert reader.alert_settings(HOST) == {"alert_cooldown_s": 60}
    finally:
        reader.engine.dispose()


# Retention of alerts.


def test_retention_deletes_old_cleared_daily_alerts_and_cascades_session_alerts(
    repo: Repository,
) -> None:
    write = with_sessions(repo, SESSION, OTHER)
    write(event("SessionEnd", reason="other"), BASE + 1)
    session_alert = repo.insert_alert(HOST, fired(at=BASE + 1), discord_state="none")
    open_session_alert = repo.insert_alert(HOST, fired(OTHER, at=BASE), discord_state="none")
    old_day = repo.insert_alert(HOST, daily(BASE), discord_state="sent")
    repo.clear_alert(
        HOST, AlertCleared(rule="daily_cost", subject=DAY_SUBJECT, cleared_at_ms=BASE + 1)
    )
    at_cutoff = repo.insert_alert(HOST, daily(BASE + 10), discord_state="sent")
    repo.clear_alert(
        HOST, AlertCleared(rule="daily_cost", subject=DAY_SUBJECT, cleared_at_ms=BASE + 11)
    )
    # Still firing: kept whatever its age, so a restart still knows the key fires.
    still_firing = repo.insert_alert("other-host", daily(BASE), discord_state="sent")

    assert repo.delete_older_than(BASE + 10) == {SESSION}  # ended; OTHER is open

    left = {a.id for a in repo.list_alerts(HOST, 10)}
    assert left == {open_session_alert, at_cutoff}
    assert session_alert not in left
    assert old_day not in left
    assert [a.id for a in repo.list_alerts("other-host", 10)] == [still_firing]


# Alert settings (ADR 0042).

SETTINGS: Final[dict[str, AlertSettingValue]] = {
    "alert_waiting_enabled": False,
    "alert_waiting_threshold_s": 45,
    "alert_failures_enabled": True,
    "alert_failures_count": 7,
    "alert_failures_window_s": 3_600,
    "alert_session_cost_enabled": True,
    "alert_session_cost_usd": 0.1,
    "alert_daily_cost_enabled": False,
    "alert_daily_cost_usd": 12,
    "alert_cooldown_s": 0,
}


def test_the_setting_allowlist_is_the_alert_settings_of_the_environment() -> None:
    from_settings = {
        name: field.annotation
        for name, field in Settings.model_fields.items()
        if name.startswith("alert_")
    }
    assert from_settings == ALERT_SETTING_TYPES
    assert SETTINGS.keys() == ALERT_SETTING_TYPES.keys()


def test_saved_settings_round_trip_exactly(repo: Repository) -> None:
    for name, value in SETTINGS.items():
        repo.save_alert_setting(HOST, name, value, updated_at_ms=BASE)
    loaded = repo.alert_settings(HOST)
    assert loaded == SETTINGS
    # Exactly: a bool stays a bool, an int an int and a float a float.
    assert {k: type(v) for k, v in loaded.items()} == {k: type(v) for k, v in SETTINGS.items()}


@pytest.mark.parametrize(
    "value", [0.1, 1e-2, 12.345678901234567, 99_999.99, 100_000.0, 5e-324, 10, 10.0]
)
def test_dollar_amounts_round_trip_bit_for_bit(repo: Repository, value: float) -> None:
    repo.save_alert_setting(HOST, "alert_daily_cost_usd", value, updated_at_ms=BASE)
    loaded = repo.alert_settings(HOST)["alert_daily_cost_usd"]
    assert type(loaded) is type(value)
    assert loaded == value


def test_saving_a_setting_again_replaces_it(repo: Repository) -> None:
    repo.save_alert_setting(HOST, "alert_cooldown_s", 60, updated_at_ms=BASE)
    repo.save_alert_setting(HOST, "alert_cooldown_s", 120, updated_at_ms=BASE + 1)
    assert repo.alert_settings(HOST) == {"alert_cooldown_s": 120}
    with repo.engine.connect() as connection:
        rows = connection.execute(select(AlertSettingModel.__table__)).all()
    assert [tuple(row) for row in rows] == [(HOST, "alert_cooldown_s", "120", BASE + 1)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alert_cooldown_s", True),  # a bool is never a number (ADR 0041)
        ("alert_cooldown_s", 60.0),  # seconds are whole
        ("alert_cooldown_s", "60"),
        ("alert_cooldown_s", None),
        ("alert_waiting_enabled", 1),
        ("alert_waiting_enabled", "true"),
        ("alert_session_cost_usd", False),
        ("alert_session_cost_usd", math.nan),
        ("alert_session_cost_usd", math.inf),
        ("alert_session_cost_usd", "10"),
        ("alert_session_cost_usd", [10]),
    ],
)
def test_a_setting_of_the_wrong_type_is_refused_without_its_value(
    repo: Repository, field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=f"^{field} must be ") as error:
        repo.save_alert_setting(HOST, field, cast("AlertSettingValue", value), updated_at_ms=BASE)
    assert repr(value) not in str(error.value)
    assert repo.alert_settings(HOST) == {}


def test_an_unknown_setting_is_refused_without_its_name(repo: Repository) -> None:
    for field in ("discord_webhook_url", "retention", "alert_waiting", "CANARY-9e1d"):
        with pytest.raises(ValueError, match=r"^not an alert setting$"):
            repo.save_alert_setting(HOST, field, 1, updated_at_ms=BASE)
    assert repo.alert_settings(HOST) == {}


@pytest.mark.parametrize(
    ("field", "stored"),
    [
        ("alert_cooldown_s", "true"),
        ("alert_cooldown_s", "60.5"),
        ("alert_cooldown_s", '"60"'),
        ("alert_cooldown_s", "not json"),
        ("alert_cooldown_s", "null"),
        ("alert_waiting_enabled", "1"),
        ("alert_session_cost_usd", "NaN"),
        ("alert_session_cost_usd", "Infinity"),
        ("alert_session_cost_usd", "1e400"),
        ("alert_session_cost_usd", "[1]"),
        ("discord_webhook_url", '"https://discord.com/api/webhooks/1/x"'),
    ],
)
def test_an_invalid_stored_setting_is_ignored_and_counted(
    repo: Repository, caplog: pytest.LogCaptureFixture, field: str, stored: str
) -> None:
    repo.save_alert_setting(HOST, "alert_failures_count", 4, updated_at_ms=BASE)
    with repo.engine.begin() as connection:
        connection.execute(
            insert(AlertSettingModel).values(
                host=HOST, field=field, value=stored, updated_at_ms=BASE
            )
        )
    with caplog.at_level(logging.WARNING, logger="ccobs.server.db.repository"):
        assert repo.alert_settings(HOST) == {"alert_failures_count": 4}
    assert [r.getMessage() for r in caplog.records] == ["ignored 1 invalid saved alert settings"]


def test_reset_deletes_one_setting_or_all_of_a_host(repo: Repository) -> None:
    for name, value in SETTINGS.items():
        repo.save_alert_setting(HOST, name, value, updated_at_ms=BASE)
    repo.save_alert_setting("other-host", "alert_cooldown_s", 30, updated_at_ms=BASE)

    assert repo.reset_alert_setting(HOST, "alert_cooldown_s")
    assert not repo.reset_alert_setting(HOST, "alert_cooldown_s")
    assert not repo.reset_alert_setting(HOST, "not-a-setting")
    expected = {k: v for k, v in SETTINGS.items() if k != "alert_cooldown_s"}
    assert repo.alert_settings(HOST) == expected

    assert repo.reset_alert_settings(HOST) == len(expected)
    assert repo.alert_settings(HOST) == {}
    assert repo.reset_alert_settings(HOST) == 0
    # Another host's settings are its own.
    assert repo.alert_settings("other-host") == {"alert_cooldown_s": 30}


def test_several_settings_are_saved_in_one_call(repo: Repository) -> None:
    repo.save_alert_setting(HOST, "alert_cooldown_s", 60, updated_at_ms=BASE)

    repo.save_alert_settings(
        HOST, {"alert_cooldown_s": 120, "alert_waiting_enabled": False}, updated_at_ms=BASE + 1
    )

    assert repo.alert_settings(HOST) == {"alert_cooldown_s": 120, "alert_waiting_enabled": False}
    with repo.engine.connect() as connection:
        rows = connection.execute(select(AlertSettingModel.__table__)).all()
    assert {row.updated_at_ms for row in rows} == {BASE + 1}


def test_saving_no_settings_writes_nothing(repo: Repository) -> None:
    repo.save_alert_settings(HOST, {}, updated_at_ms=BASE)

    assert repo.alert_settings(HOST) == {}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("alert_cooldown_s", True, "^alert_cooldown_s must be a whole number$"),
        ("CANARY-9e1d", 1, "^not an alert setting$"),
    ],
)
def test_one_bad_setting_saves_none_of_them(
    repo: Repository, field: str, value: object, message: str
) -> None:
    values = {"alert_failures_count": 4, field: cast("AlertSettingValue", value)}

    with pytest.raises(ValueError, match=message):
        repo.save_alert_settings(HOST, values, updated_at_ms=BASE)

    assert repo.alert_settings(HOST) == {}
