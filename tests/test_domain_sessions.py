"""Unit tests of ``ccobs.domain.sessions``, the session, agent and block reducer (ADR 0024).

The first part is a transition table: one test per rule of ADR 0024, on synthetic
events parsed with ``parse_hook``. The second replays every hook fixture (ADR 0012)
through ``parse_hook`` and ``apply``, with the receive time taken from the fixture's
``offset_ms`` and the order from the line index, and checks the sessions, agents and
blocks that real Claude Code sessions produce.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Final

import pytest

from ccobs.domain.hooks import _PARSERS, UnknownHookEvent, parse_hook
from ccobs.domain.sessions import (
    HANDLED_EVENTS,
    AgentChanged,
    BlockClosed,
    SessionChanged,
    apply,
    close_block_denied,
    mark_stale,
)
from tools.stack.session_replay import BASE, HOOK_FIXTURES, SESSION, Run, event, replay, waited


def bash_permission(run: Run, at: int, tool_use_id: str = "t1", **scope: Any) -> None:
    run("PreToolUse", at, tool_name="Bash", tool_use_id=tool_use_id, **scope)
    run("PermissionRequest", at + 10, tool_name="Bash", **scope)


# Rule 1: session states.


def test_the_first_event_creates_an_idle_session() -> None:
    run = Run()
    state = run("InstructionsLoaded", 100)
    assert state.session.session_id == SESSION
    assert state.session.status == "idle"
    assert state.session.started_at_ms == 100
    assert state.session.last_event_at_ms == 100
    assert state.session.last_seq == 1
    assert state.agents == ()
    assert state.open_blocks == ()


def test_a_prompt_opens_a_turn_and_stop_closes_it() -> None:
    run = Run()
    assert run("UserPromptSubmit", 100, prompt="hi").session.status == "working"
    assert run.now.session.turn_open
    state = run("Stop", 200)
    assert state.session.status == "idle"
    assert not state.session.turn_open


def test_an_open_block_makes_the_session_wait_on_the_user() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    bash_permission(run, 200)
    assert run.now.session.status == "waiting_user"
    assert run.now.session.turn_open


def test_a_running_subagent_keeps_the_session_working_after_stop() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("SubagentStart", 200, agent_id="a1", agent_type="Explore")
    assert run("Stop", 300).session.status == "working"
    assert run("SubagentStop", 400, agent_id="a1", agent_type="Explore").session.status == "idle"


def test_session_end_ends_the_session_and_closes_everything() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("SubagentStart", 150, agent_id="a1", agent_type="Explore")
    bash_permission(run, 200)
    state = run("SessionEnd", 900, reason="prompt_input_exit")
    assert state.session.status == "ended"
    assert state.session.end_reason == "prompt_input_exit"
    assert state.session.ended_at_ms == 900
    assert not state.session.turn_open
    assert state.open_blocks == ()
    assert state.pending_tools == ()
    agent = run.agent("a1")
    assert (agent.status, agent.stopped_at_ms, agent.stop_reason) == (
        "stopped",
        900,
        "session_ended",
    )
    assert state.agents == ()
    (block,) = run.closed()
    assert (block.close_reason, block.close_source, block.closed_at_ms) == (
        "session_ended",
        "hook",
        900,
    )


def test_an_event_after_the_end_reopens_the_session() -> None:
    run = Run()
    run("SessionEnd", 100, reason="other")
    state = run("UserPromptSubmit", 200)
    assert state.session.status == "working"
    assert state.session.end_reason is None
    assert state.session.ended_at_ms is None


# Rule 2: lazy creation and a late SessionStart.


def test_a_late_session_start_fills_source_and_model_without_moving_the_state() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    state = run("SessionStart", 150, source="startup", model="claude-haiku-5-5")
    assert state.session.status == "working"
    assert state.session.source == "startup"
    assert state.session.model == "claude-haiku-5-5"
    # A later SessionStart (after /compact) keeps the first source and the latest model.
    state = run("SessionStart", 160, source="compact", model="claude-opus-5-5")
    assert state.session.source == "startup"
    assert state.session.model == "claude-opus-5-5"


def test_a_session_start_never_reopens_an_ended_session() -> None:
    run = Run()
    run("SessionEnd", 100, reason="other")
    state = run("SessionStart", 110, source="startup")
    assert state.session.status == "ended"
    assert state.session.source == "startup"


def test_started_at_is_the_earliest_and_last_event_at_the_latest_receive_time() -> None:
    run = Run()
    run("InstructionsLoaded", 500)
    state = run("SessionStart", 400, source="startup")
    assert state.session.started_at_ms == 400
    assert state.session.last_event_at_ms == 500


# Rule 3: turns.


def test_stop_failure_ends_the_turn_and_is_not_a_block() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    state = run("StopFailure", 200, error="model_not_found")
    assert state.session.status == "idle"
    assert state.session.last_error_code == "model_not_found"
    assert run.opened() == []


def test_stop_failure_supersedes_open_blocks() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    bash_permission(run, 200)
    run("StopFailure", 300, error="rate_limit")
    assert [b.close_reason for b in run.closed()] == ["superseded"]


def test_prompt_id_is_kept_from_any_event_but_is_not_the_turn_key() -> None:
    run = Run()
    run("UserPromptSubmit", 100, prompt_id="p1")
    state = run("PreCompact", 200, prompt_id="p2", trigger="auto")
    assert state.session.last_prompt_id == "p2"
    assert state.session.turn_open
    assert run("Stop", 300).session.last_prompt_id == "p2"


def test_auto_compaction_during_a_turn_restores_the_turn() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    state = run("PreCompact", 200, trigger="auto")
    assert state.session.compacting
    assert state.session.status == "working"
    state = run("PostCompact", 300, trigger="auto")
    assert not state.session.compacting
    assert state.session.turn_open
    assert state.session.status == "working"


def test_manual_compaction_ends_a_turn_left_open() -> None:
    # /compact is typed at the prompt, so the main thread runs no turn; a turn still open
    # there was left by a dialog denied with Esc, which fires no Stop (ADR 0012).
    run = Run()
    run("UserPromptSubmit", 100)
    run("PreToolUse", 150, tool_name="Bash", tool_use_id="t1")
    run("PreToolUse", 160, tool_name="Glob", tool_use_id="g1", agent_id="a1")
    state = run("PreCompact", 200, trigger="manual")
    assert state.session.status == "working"
    assert state.session.compacting
    assert not state.session.turn_open
    # The same turn end as Stop: the main thread's pending calls are dropped.
    assert [p.tool_use_id for p in state.pending_tools] == ["g1"]
    assert run("PostCompact", 300, trigger="manual").session.status == "working"
    assert run("SubagentStop", 310, agent_id="a1").session.status == "idle"


def test_a_manual_compaction_inside_a_subagent_keeps_the_main_turn() -> None:
    # Not in the fixtures: only a main-thread /compact ends the main turn.
    run = Run()
    run("UserPromptSubmit", 100)
    run("PreToolUse", 150, tool_name="Bash", tool_use_id="t1")
    state = run("PreCompact", 200, trigger="manual", agent_id="a1")
    assert state.session.turn_open
    assert [p.tool_use_id for p in state.pending_tools] == ["t1"]


@pytest.mark.parametrize("closer", ["Stop", "StopFailure"])
def test_a_turn_ends_a_compaction_whose_post_compact_was_lost(closer: str) -> None:
    run = Run()
    run("PreCompact", 100, trigger="manual")
    state = run("UserPromptSubmit", 200)
    assert (state.session.compacting, state.session.status) == (False, "working")
    run("PreCompact", 300, trigger="auto")
    state = run(closer, 400)
    assert (state.session.compacting, state.session.status) == (False, "idle")


def test_compaction_outside_a_turn_returns_to_idle() -> None:
    run = Run()
    run("InstructionsLoaded", 100)
    assert run("PreCompact", 200).session.status == "working"
    assert run("PostCompact", 300).session.status == "idle"


def test_a_new_prompt_clears_the_idle_notification() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("Stop", 200)
    assert run("Notification", 300, notification_type="idle_prompt").session.idle_notified_at_ms
    assert run("UserPromptSubmit", 400).session.idle_notified_at_ms is None


# Rule 4: scope.


def test_events_of_a_subagent_only_touch_its_scope() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    bash_permission(run, 200)
    run("SubagentStart", 300, agent_id="a1", agent_type="Explore")
    run("PostToolBatch", 400, agent_id="a1", agent_type="Explore")
    run("PreToolUse", 410, tool_name="Glob", tool_use_id="g1", agent_id="a1")
    assert len(run.now.open_blocks) == 1
    run("PostToolBatch", 500)
    assert not run.now.open_blocks


def test_a_prompt_or_stop_inside_a_subagent_leaves_the_main_turn_alone() -> None:
    # Not in the fixtures: only main-thread events open or close the turn.
    run = Run()
    run("UserPromptSubmit", 100, agent_id="a1")
    assert not run.now.session.turn_open
    run("UserPromptSubmit", 110)
    run("Stop", 120, agent_id="a1")
    assert run.now.session.turn_open


def test_agents_are_keyed_by_id_not_by_type() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("SubagentStart", 110, agent_id="a2", agent_type="Explore")
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    assert run.agent("a1").status == "stopped"
    assert run.agent("a2").status == "running"
    assert run.now.session.status == "working"


# Rule 5: agents.


def test_subagent_start_and_stop() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    agent = run.agent("a1")
    assert (agent.agent_type, agent.status, agent.internal, agent.started_at_ms) == (
        "Explore",
        "running",
        False,
        100,
    )
    assert isinstance(run.last[0], AgentChanged)
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    agent = run.agent("a1")
    assert (agent.status, agent.stopped_at_ms, agent.stop_reason) == ("stopped", 200, "stopped")
    assert agent.last_event_at_ms == 200
    assert run.now.agents == ()
    assert run.now.stopped_agent_ids == ("a1",)


def test_events_in_an_agent_scope_update_its_last_event() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("PreToolUse", 300, tool_name="Glob", tool_use_id="g1", agent_id="a1")
    assert run.agent("a1").last_event_at_ms == 300
    run("PostToolUse", 200, tool_name="Glob", tool_use_id="g1", agent_id="a1")
    assert run.agent("a1").last_event_at_ms == 300
    run("SubagentStart", 400, agent_id="a1", agent_type="Explore")
    assert run.agent("a1").last_event_at_ms == 400
    run("PostToolBatch", 500)
    assert run.agent("a1").last_event_at_ms == 400


def test_events_of_a_stopped_agent_only_update_the_session_metadata() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    state = run("PermissionRequest", 300, tool_name="Bash", agent_id="a1", permission_mode="plan")
    assert run.last == [SessionChanged(session=state.session, previous_status="idle")]
    assert (state.session.last_event_at_ms, state.session.last_seq) == (300, 3)
    assert state.session.permission_mode == "plan"
    run("UserPromptSubmit", 400)
    state = run("Stop", 500)
    assert state.session.status == "idle"
    assert state.open_blocks == ()
    assert run.opened() == []


def test_a_stopped_agent_is_not_brought_back() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    count = len(run.agents())
    for name in ("PreToolUse", "SubagentStop", "SubagentStart"):
        state = run(name, 300, tool_name="Glob", tool_use_id="g1", agent_id="a1")
        assert [e for e in run.last if isinstance(e, AgentChanged)] == []
        assert state.agents == ()
        assert state.session.status == "idle"
    assert len(run.agents()) == count
    assert run.agent("a1").stop_reason == "stopped"


def test_stopped_agents_leave_the_state_and_their_ids_are_bounded() -> None:
    run = Run()
    for turn in range(2000):
        at = turn * 10
        run("UserPromptSubmit", at)
        run("SubagentStart", at + 1, agent_id=f"a{turn}", agent_type="Explore")
        run("SubagentStop", at + 2, agent_id=f"a{turn}", agent_type="Explore")
        run("Stop", at + 3)
        run("SubagentStop", at + 4, agent_id=f"i{turn}", agent_type="")
    assert len(run.now.agents) < 65
    assert run.now.agents == ()
    assert len(run.now.stopped_agent_ids) == 64
    assert run.now.stopped_agent_ids[-1] == "i1999"
    # The internal-agent rule still holds after the bound: an unknown stop is internal.
    run("SubagentStop", 30_000, agent_id="fresh", agent_type="")
    (changed,) = [e for e in run.last if isinstance(e, AgentChanged)]
    assert (changed.agent.internal, changed.agent.status) == (True, "stopped")


def test_a_stopped_agent_releases_its_agent_call() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Agent", tool_use_id="ag1")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    assert run.now.pending_tools == ()
    run("SubagentStart", 210, agent_id="a2", agent_type="Explore")
    assert run.agent("a2").tool_use_id is None


def test_a_stop_without_a_start_is_an_internal_agent() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("Stop", 200)
    state = run("SubagentStop", 300, agent_id="i1", agent_type="")
    agent = run.agent("i1")
    assert agent.internal
    assert agent.agent_type is None
    assert agent.started_at_ms is None
    assert agent.status == "stopped"
    assert state.session.status == "idle"


def test_a_late_start_of_an_internal_agent_does_not_revive_it() -> None:
    run = Run()
    run("SubagentStop", 100, agent_id="a1", agent_type="Explore")
    state = run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    # Ignored: storage keeps the stopped record (still marked internal).
    assert run.last == [SessionChanged(session=state.session, previous_status="idle")]
    agent = run.agent("a1")
    assert (agent.status, agent.internal, agent.started_at_ms) == ("stopped", True, None)
    assert state.session.status == "idle"


def test_events_of_an_unseen_agent_create_it_running() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Glob", tool_use_id="g1", agent_id="a1", agent_type="Explore")
    agent = run.agent("a1")
    assert (agent.status, agent.internal, agent.agent_type) == ("running", False, "Explore")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    assert run.agent("a1").started_at_ms == 100
    # A stop now marks a seen agent stopped, not internal.
    run("SubagentStop", 200, agent_id="a1", agent_type="Explore")
    assert not run.agent("a1").internal


def test_subagent_events_without_an_agent_id_create_no_agent() -> None:
    run = Run()
    run("SubagentStart", 100, agent_type="Explore")
    run("SubagentStop", 200, agent_type="Explore")
    assert run.now.agents == ()


def test_a_foreground_subagent_links_to_its_agent_tool_call() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Agent", tool_use_id="ag1")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    agent = run.agent("a1")
    assert (agent.tool_use_id, agent.background) == ("ag1", False)
    run("SubagentStart", 120, agent_id="a2", agent_type="Explore")
    assert run.agent("a2").tool_use_id is None


def test_an_async_launch_before_the_start_marks_a_background_agent() -> None:
    run = Run()
    run(
        "PostToolUse",
        100,
        tool_name="Agent",
        tool_use_id="ag1",
        tool_response={"status": "async_launched"},
    )
    assert run.now.async_launches == ("ag1",)
    run("PreToolUse", 101, tool_name="Agent", tool_use_id="ag1")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    agent = run.agent("a1")
    assert (agent.tool_use_id, agent.background) == ("ag1", True)
    assert not run.now.async_launches
    assert run.now.pending_tools == ()


def test_an_async_launch_after_the_start_marks_the_linked_agent() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Agent", tool_use_id="ag1")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    run(
        "PostToolUse",
        120,
        tool_name="Agent",
        tool_use_id="ag1",
        tool_response={"status": "async_launched"},
    )
    assert run.agent("a1").background
    assert run.now.async_launches == ()


def test_a_completed_agent_call_is_not_a_background_launch() -> None:
    run = Run()
    run(
        "PostToolUse",
        100,
        tool_name="Agent",
        tool_use_id="ag1",
        tool_response={"status": "completed"},
    )
    run(
        "PostToolUse",
        110,
        tool_name="Read",
        tool_use_id="r1",
        tool_response={"status": "async_launched"},
    )
    assert run.now.async_launches == ()


def test_a_known_agent_started_twice_keeps_its_link() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Agent", tool_use_id="ag1")
    run("SubagentStart", 110, agent_id="a1", agent_type="Explore")
    run("PreToolUse", 120, tool_name="Agent", tool_use_id="ag2")
    run("SubagentStart", 130, agent_id="a1", agent_type="Explore")
    assert run.agent("a1").tool_use_id == "ag1"


# Rule 6: blocks.


def test_a_permission_request_borrows_the_pending_tool_use_id() -> None:
    run = Run()
    bash_permission(run, 100, mcp_server={"name": "srv"})
    (block,) = run.now.open_blocks
    assert (block.kind, block.scope, block.tool_name, block.tool_use_id) == (
        "permission",
        None,
        "Bash",
        "t1",
    )
    assert block.opened_at_ms == 110
    assert block.mcp_server_name == "srv"
    assert block.block_id == 2
    assert run.opened() == [block]


def test_a_permission_request_with_no_pending_tool_has_no_tool_use_id() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Read", tool_use_id="r1")
    run("PermissionRequest", 110, tool_name="Bash")
    (block,) = run.now.open_blocks
    assert block.tool_use_id is None
    # Without a tool_use_id, a completion of the same tool in the same scope closes it.
    run("PostToolUse", 120, tool_name="Read", tool_use_id="r1")
    assert len(run.now.open_blocks) == 1
    run("PostToolUse", 130, tool_name="Bash", tool_use_id="b9")
    assert [b.close_reason for b in run.closed()] == ["tool_completed"]


def test_tool_completion_closes_the_linked_block() -> None:
    run = Run()
    bash_permission(run, 100)
    state = run("PostToolUse", 500, tool_name="Bash", tool_use_id="t1")
    (block,) = run.closed()
    assert (block.close_reason, block.close_source, block.closed_at_ms) == (
        "tool_completed",
        "hook",
        500,
    )
    assert state.open_blocks == ()
    assert state.pending_tools == ()


def test_tool_failure_closes_the_linked_block() -> None:
    run = Run()
    bash_permission(run, 100)
    run("PostToolUseFailure", 500, tool_name="Bash", tool_use_id="t1", is_interrupt=True)
    assert [b.close_reason for b in run.closed()] == ["tool_failed"]


def test_permission_denied_closes_by_tool_use_id() -> None:
    run = Run()
    bash_permission(run, 100)
    run("PermissionDenied", 200, tool_name="Bash", tool_use_id="t1", reason="rule")
    assert [b.close_reason for b in run.closed()] == ["denied"]
    assert run.now.pending_tools == ()


def test_permission_denied_closes_by_tool_and_scope() -> None:
    run = Run()
    run("PermissionRequest", 100, tool_name="Bash", agent_id="a1")
    run("PermissionDenied", 200, tool_name="Bash", agent_id="a1")
    assert [b.close_reason for b in run.closed()] == ["denied"]


def test_permission_denied_of_another_tool_closes_nothing() -> None:
    run = Run()
    bash_permission(run, 100)
    run("PermissionDenied", 200, tool_name="Write", tool_use_id="w1")
    run("PermissionDenied", 210, tool_name="Bash", tool_use_id="b2", agent_id="a1")
    assert run.closed() == []


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("PreToolUse", {"tool_name": "Read", "tool_use_id": "r1"}),
        ("PreToolUse", {"tool_name": "Read"}),
        ("Stop", {}),
        ("UserPromptSubmit", {}),
        ("PreCompact", {"trigger": "auto"}),
        ("PostToolBatch", {}),
    ],
)
def test_progress_in_the_same_scope_supersedes_a_block(name: str, fields: dict[str, Any]) -> None:
    run = Run()
    run("UserPromptSubmit", 50)
    bash_permission(run, 100)
    run(name, 300, **fields)
    (block,) = run.closed()
    assert (block.close_reason, block.closed_at_ms) == ("superseded", 300)


def test_a_late_pre_tool_use_of_the_linked_call_does_not_supersede() -> None:
    run = Run()
    bash_permission(run, 100)
    run("PreToolUse", 200, tool_name="Bash", tool_use_id="t1")
    assert run.closed() == []


def test_subagent_stop_closes_the_blocks_of_its_scope() -> None:
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    bash_permission(run, 200, agent_id="a1", agent_type="Explore")
    bash_permission(run, 300, tool_use_id="t2")
    run("SubagentStop", 400, agent_id="a1", agent_type="Explore")
    (block,) = run.closed()
    assert (block.scope, block.close_reason) == ("a1", "agent_stopped")
    assert [b.tool_use_id for b in run.now.pending_tools] == ["t2"]


def test_an_elicitation_opens_a_block_linked_to_its_mcp_tool_call() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="mcp__srv__pick", tool_use_id="m1")
    run("Elicitation", 110, mcp_server_name="srv", mode="form", elicitation_id="e1")
    (block,) = run.now.open_blocks
    assert (block.kind, block.mcp_server_name, block.elicitation_id) == ("elicitation", "srv", "e1")
    assert (block.tool_name, block.tool_use_id) == ("mcp__srv__pick", "m1")
    assert run.now.session.status == "waiting_user"


def test_an_elicitation_without_a_pending_call_is_unlinked() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Read", tool_use_id="r1")
    run("Elicitation", 110, mcp_server_name="srv")
    (block,) = run.now.open_blocks
    assert (block.tool_name, block.tool_use_id) == (None, None)


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("accept", "elicitation_accept"),
        ("decline", "elicitation_decline"),
        ("cancel", "elicitation_cancel"),
        ("something", "elicitation_other"),
        (None, "elicitation_other"),
    ],
)
def test_an_elicitation_result_closes_its_block(action: str | None, reason: str) -> None:
    run = Run()
    run("Elicitation", 100, mcp_server_name="srv")
    run("ElicitationResult", 200, mcp_server_name="srv", action=action)
    (block,) = run.closed()
    assert (block.close_reason, block.closed_at_ms) == (reason, 200)


def test_an_elicitation_result_matches_by_id_then_by_server() -> None:
    run = Run()
    run("Elicitation", 100, mcp_server_name="srv", elicitation_id="e1")
    run("Elicitation", 110, mcp_server_name="srv", elicitation_id="e2")
    run("Elicitation", 120, mcp_server_name="other")
    run("ElicitationResult", 200, mcp_server_name="srv", action="accept", elicitation_id="e2")
    assert [b.elicitation_id for b in run.closed()] == ["e2"]
    run("ElicitationResult", 210, mcp_server_name="other", action="accept")
    assert [b.mcp_server_name for b in run.closed()] == ["srv", "other"]
    run("ElicitationResult", 220, action="decline")
    assert [b.elicitation_id for b in run.closed()] == ["e2", None, "e1"]


def test_an_unmatched_elicitation_result_closes_nothing() -> None:
    run = Run()
    run("PermissionRequest", 90, tool_name="Bash", agent_id="a1")
    run("Elicitation", 100, mcp_server_name="srv", elicitation_id="e1")
    run("ElicitationResult", 200, mcp_server_name="srv", elicitation_id="e9", action="accept")
    run("ElicitationResult", 210, mcp_server_name="nope", action="accept")
    assert run.closed() == []


def test_a_notification_marks_the_matching_block_notified() -> None:
    run = Run()
    bash_permission(run, 100)
    run("Elicitation", 120, mcp_server_name="srv")
    run("Notification", 6100, notification_type="permission_prompt")
    (block,) = run.notified()
    assert (block.kind, block.notified_at_ms) == ("permission", 6100)
    # A repeated notification changes nothing.
    run("Notification", 6200, notification_type="permission_prompt")
    assert len(run.notified()) == 1
    run("Notification", 6300, notification_type="elicitation_dialog")
    assert [b.kind for b in run.notified()] == ["permission", "elicitation"]
    assert len(run.opened()) == 2


def test_a_notification_prefers_the_oldest_block_of_its_scope() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Bash", tool_use_id="t1", agent_id="a1")
    run("PreToolUse", 110, tool_name="Bash", tool_use_id="t3", agent_id="a1")
    run("PermissionRequest", 120, tool_name="Bash", agent_id="a1")
    run("PermissionRequest", 130, tool_name="Bash", agent_id="a1")
    bash_permission(run, 200, tool_use_id="t2")
    run("Notification", 6100, notification_type="permission_prompt")
    run("Notification", 6200, notification_type="permission_prompt", agent_id="a1")
    # With every block of its scope notified, a notification falls back to another scope.
    run("Notification", 6300, notification_type="permission_prompt")
    assert [(b.scope, b.tool_use_id) for b in run.notified()] == [
        (None, "t2"),
        ("a1", "t3"),
        ("a1", "t1"),
    ]


def test_a_notification_with_no_open_block_opens_one() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("PreToolUse", 200, tool_name="Bash", tool_use_id="t1")
    run("Notification", 6200, notification_type="permission_prompt")
    (block,) = run.now.open_blocks
    assert (block.kind, block.tool_name, block.tool_use_id) == ("permission", "Bash", "t1")
    assert block.opened_at_ms == block.notified_at_ms == 6200
    assert run.now.session.status == "waiting_user"
    run("PostToolUse", 9000, tool_name="Bash", tool_use_id="t1")
    assert [b.close_reason for b in run.closed()] == ["tool_completed"]


def test_an_elicitation_notification_with_no_open_block_opens_one() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Read", tool_use_id="r1")
    run("PreToolUse", 110, tool_name="mcp__srv__pick", tool_use_id="m1")
    run("PreToolUse", 120, tool_name="Read", tool_use_id="r2")
    run("Notification", 6200, notification_type="elicitation_dialog")
    (block,) = run.now.open_blocks
    assert (block.kind, block.tool_use_id, block.mcp_server_name) == ("elicitation", "m1", "srv")


def test_a_fallback_block_with_no_pending_tool_is_unlinked() -> None:
    run = Run()
    run("Notification", 100, notification_type="permission_prompt")
    (block,) = run.now.open_blocks
    assert (block.tool_name, block.tool_use_id) == (None, None)
    # An unlinked block with no tool closes on any completion in its scope.
    run("PostToolUse", 200, tool_name="Read", tool_use_id="r1")
    assert run.now.open_blocks == ()


def test_an_idle_prompt_is_not_a_block() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    run("Stop", 200)
    state = run("Notification", 60_200, notification_type="idle_prompt")
    assert state.session.idle_notified_at_ms == 60_200
    assert state.session.status == "idle"
    assert state.open_blocks == ()


def test_other_notifications_change_nothing() -> None:
    run = Run()
    run("Notification", 100, notification_type="elicitation_response")
    run("Notification", 110)
    assert run.now.open_blocks == ()
    assert run.now.session.idle_notified_at_ms is None


# Rule 7: pending tool uses.


def test_a_post_tool_use_before_its_pre_tool_use_leaves_nothing_pending() -> None:
    run = Run()
    run("PostToolUse", 100, tool_name="Read", tool_use_id="r1")
    run("PreToolUse", 101, tool_name="Read", tool_use_id="r1")
    assert run.now.pending_tools == ()
    run("PermissionRequest", 110, tool_name="Read")
    assert run.now.open_blocks[0].tool_use_id is None


def test_parallel_calls_of_the_same_tool_link_to_their_own_blocks() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Bash", tool_use_id="b1")
    run("PreToolUse", 110, tool_name="Bash", tool_use_id="b2")
    run("PreToolUse", 110, tool_name="Bash", tool_use_id="b2")  # a duplicate delivery
    assert [p.tool_use_id for p in run.now.pending_tools] == ["b1", "b2"]
    run("PermissionRequest", 120, tool_name="Bash")
    run("PermissionRequest", 130, tool_name="Bash")
    assert [b.tool_use_id for b in run.now.open_blocks] == ["b2", "b1"]
    run("PostToolUse", 500, tool_name="Bash", tool_use_id="b1")
    assert [b.tool_use_id for b in run.closed()] == ["b1"]
    assert [b.tool_use_id for b in run.now.open_blocks] == ["b2"]


def test_pending_tools_end_with_their_batch_and_turn() -> None:
    run = Run()
    run("PreToolUse", 100, tool_name="Bash", tool_use_id="b1")
    run("PreToolUse", 110, tool_name="Glob", tool_use_id="g1", agent_id="a1")
    run("PostToolBatch", 120)
    assert [p.tool_use_id for p in run.now.pending_tools] == ["g1"]
    run("PreToolUse", 130, tool_name="Bash", tool_use_id="b2")
    run("Stop", 140)
    assert [p.tool_use_id for p in run.now.pending_tools] == ["g1"]


def test_completed_tool_use_ids_are_bounded() -> None:
    run = Run()
    for number in range(200):
        run("PostToolUse", 100 + number, tool_name="Read", tool_use_id=f"r{number}")
    assert len(run.now.completed_tool_use_ids) == 64
    assert run.now.completed_tool_use_ids[-1] == "r199"
    # A duplicate outcome is remembered once.
    run("PostToolUseFailure", 400, tool_name="Read", tool_use_id="r150")
    assert run.now.completed_tool_use_ids.count("r150") == 1


def test_a_tool_event_without_an_id_completes_nothing() -> None:
    run = Run()
    run("PostToolUse", 100, tool_name="Read")
    run("PreToolUse", 110, tool_name="Read")
    assert run.now.completed_tool_use_ids == ()
    assert run.now.pending_tools == ()


# Rule 8: unknown and unhandled events.


def test_unknown_events_only_update_the_session_metadata() -> None:
    run = Run()
    run("UserPromptSubmit", 100, permission_mode="default")
    state = run(
        "CwdChanged", 200, permission_mode="acceptEdits", cwd="/w", effort={"level": "high"}
    )
    assert state.session.status == "working"
    assert state.session.last_event_at_ms == 200
    assert state.session.permission_mode == "acceptEdits"
    assert state.session.cwd == "/w"
    assert state.session.effort_level == "high"
    assert "/w" not in repr(state.session)
    state = run("TaskCreated", 300)
    assert state.session.permission_mode == "acceptEdits"
    assert state.session.cwd == "/w"


# For each handled event: extra events after the common prior (a turn with a pending Bash
# call and its permission block), and the fields of the event under test. Each case is
# chosen so that the event has an effect of its own.
HANDLED_CASES: Final[dict[str, tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]]] = {
    "UserPromptSubmit": ([], {"prompt": "next"}),
    "Stop": ([], {}),
    "StopFailure": ([], {"error": "rate_limit"}),
    "PreCompact": ([], {"trigger": "auto"}),
    "PostCompact": ([("PreCompact", {"trigger": "auto"})], {"trigger": "auto"}),
    "PostToolBatch": ([], {}),
    "PreToolUse": ([], {"tool_name": "Read", "tool_use_id": "t2"}),
    "PermissionRequest": ([], {"tool_name": "Read"}),
    "PostToolUse": ([], {"tool_name": "Bash", "tool_use_id": "t1"}),
    "PostToolUseFailure": ([], {"tool_name": "Bash", "tool_use_id": "t1"}),
    "PermissionDenied": ([], {"tool_name": "Bash", "tool_use_id": "t1"}),
    "Notification": ([], {"notification_type": "permission_prompt"}),
    "Elicitation": ([], {"mcp_server_name": "srv", "elicitation_id": "e1"}),
    "ElicitationResult": (
        [("Elicitation", {"mcp_server_name": "srv", "elicitation_id": "e1"})],
        {"mcp_server_name": "srv", "elicitation_id": "e1", "action": "accept"},
    ),
    # An unknown event of the scope also creates the agent; only SubagentStart links it to
    # its Agent tool call.
    "SubagentStart": (
        [("PreToolUse", {"tool_name": "Agent", "tool_use_id": "t3"})],
        {"agent_id": "a1", "agent_type": "Explore"},
    ),
    "SubagentStop": ([], {"agent_id": "a1", "agent_type": "Explore"}),
    "SessionStart": ([], {"source": "startup", "model": "claude-opus-5"}),
    "SessionEnd": ([], {"reason": "other"}),
}


def test_every_handled_event_has_a_case() -> None:
    assert set(HANDLED_CASES) == HANDLED_EVENTS
    assert set(_PARSERS) >= HANDLED_EVENTS


@pytest.mark.parametrize("name", sorted(HANDLED_CASES))
def test_every_handled_event_changes_more_than_an_unknown_event(name: str) -> None:
    extra, fields = HANDLED_CASES[name]
    run = Run()
    run("UserPromptSubmit", 100, prompt="hi")
    bash_permission(run, 110)
    for at, (prior, prior_fields) in enumerate(extra, start=200):
        run(prior, at, **prior_fields)
    seq = run.seq + 1

    handled, _ = apply(run.now, event(name, **fields), 500, seq)
    # The same common fields (agent_id included) under a name the reducer does not know.
    unknown_event = event("NotAHookEvent", **fields)
    assert isinstance(unknown_event, UnknownHookEvent)
    unknown, _ = apply(run.now, unknown_event, 500, seq)

    assert handled != unknown


# Order and time.


def test_an_event_of_another_session_is_a_programming_error() -> None:
    state = Run()("UserPromptSubmit", 100)
    other = parse_hook({"session_id": "another", "hook_event_name": "Stop"})
    with pytest.raises(ValueError, match="another session"):
        apply(state, other, 200, state.session.last_seq + 1)


def test_an_event_with_an_old_seq_is_ignored() -> None:
    run = Run()
    state = run("UserPromptSubmit", 100)
    again, effects = apply(state, event("Stop"), 200, state.session.last_seq)
    assert (again, effects) == (state, [])


def test_durations_are_clamped_when_the_receive_time_goes_backwards() -> None:
    run = Run()
    bash_permission(run, 1000)
    run("Notification", 500, notification_type="permission_prompt")
    run("PostToolUse", 400, tool_name="Bash", tool_use_id="t1")
    (block,) = run.closed()
    assert block.opened_at_ms == 1010
    assert block.notified_at_ms == 1010
    assert block.closed_at_ms == 1010
    assert run.now.session.last_event_at_ms == 1010
    assert run.now.session.started_at_ms == 400


def test_session_changed_is_the_last_effect_with_the_previous_status() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    assert run.last == [SessionChanged(session=run.now.session, previous_status=None)]
    bash_permission(run, 200)
    last = run.last[-1]
    assert isinstance(last, SessionChanged)
    assert (last.previous_status, last.session.status) == ("working", "waiting_user")


def test_the_state_is_immutable() -> None:
    state = Run()("UserPromptSubmit", 100)
    attribute = "status"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(state.session, attribute, "idle")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(state, attribute, None)
    assert isinstance(state.agents, tuple)
    assert isinstance(state.open_blocks, tuple)


# The sweep: mark_stale.


def test_mark_stale_stops_a_silent_agent_of_a_live_session() -> None:
    # A lost SubagentStop would keep the session working forever.
    run = Run()
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("PreToolUse", 110, tool_name="Bash", tool_use_id="b1", agent_id="a1")
    run("PermissionRequest", 120, tool_name="Bash", agent_id="a1")
    run("UserPromptSubmit", 200)
    run("Stop", 5000)
    assert run.now.session.status == "waiting_user"
    state, effects = mark_stale(run.now, now_ms=5100, stale_after_ms=1000)
    closed, changed_agent, changed_session = effects
    assert isinstance(closed, BlockClosed)
    assert (closed.block.scope, closed.block.close_reason, closed.block.close_source) == (
        "a1",
        "stale",
        "sweep",
    )
    assert closed.block.closed_at_ms == 5100
    assert isinstance(changed_agent, AgentChanged)
    agent = changed_agent.agent
    assert (agent.status, agent.stop_reason, agent.stopped_at_ms) == ("stopped", "stale", 120)
    assert changed_session == SessionChanged(session=state.session, previous_status="waiting_user")
    assert state.session.status == "idle"
    assert state.agents == ()
    assert state.stopped_agent_ids == ("a1",)
    assert state.pending_tools == ()


def test_mark_stale_stops_a_silent_agent_without_changing_a_working_session() -> None:
    run = Run()
    run("UserPromptSubmit", 0)
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("SubagentStart", 4900, agent_id="a2", agent_type="Explore")
    run("PreToolUse", 5000, tool_name="Read", tool_use_id="r1")
    state, effects = mark_stale(run.now, now_ms=5100, stale_after_ms=1000)
    (changed,) = effects
    assert isinstance(changed, AgentChanged)
    assert (changed.agent.agent_id, changed.agent.stop_reason) == ("a1", "stale")
    assert state.session.status == "working"
    assert [a.agent_id for a in state.agents] == ["a2"]


def test_mark_stale_stops_every_running_agent_of_a_stale_session() -> None:
    run = Run()
    run("UserPromptSubmit", 0)
    run("SubagentStart", 100, agent_id="a1", agent_type="Explore")
    run("SubagentStart", 900, agent_id="a2", agent_type="Explore")
    state, effects = mark_stale(run.now, now_ms=1500, stale_after_ms=600)
    stopped = [e.agent for e in effects if isinstance(e, AgentChanged)]
    assert [(a.agent_id, a.stop_reason, a.stopped_at_ms) for a in stopped] == [
        ("a1", "stale", 100),
        ("a2", "stale", 900),
    ]
    assert state.session.status == "stale"
    assert state.agents == ()


def test_mark_stale_leaves_a_recent_session_alone() -> None:
    run = Run()
    state = run("UserPromptSubmit", 100)
    assert mark_stale(state, now_ms=1099, stale_after_ms=1000) == (state, [])


def test_mark_stale_marks_a_silent_session_and_closes_its_blocks() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    bash_permission(run, 200)
    stale, effects = mark_stale(run.now, now_ms=5000, stale_after_ms=1000)
    assert stale.session.status == "stale"
    assert stale.open_blocks == ()
    closed, changed = effects
    assert isinstance(closed, BlockClosed)
    assert (closed.block.close_reason, closed.block.close_source) == ("stale", "sweep")
    assert closed.block.closed_at_ms == 5000
    assert changed == SessionChanged(session=stale.session, previous_status="waiting_user")
    # A later event shows the session is alive: the state comes from the turn again.
    state, _ = apply(stale, event("PostToolUse", tool_name="Bash", tool_use_id="t1"), 6000, 99)
    assert state.session.status == "working"


@pytest.mark.parametrize("final", ["SessionEnd", "stale"])
def test_mark_stale_ignores_ended_and_stale_sessions(final: str) -> None:
    run = Run()
    state = run("SessionEnd", 100, reason="other")
    if final == "stale":
        state = run("UserPromptSubmit", 110)
        state, _ = mark_stale(state, now_ms=10_000, stale_after_ms=1000)
    assert mark_stale(state, now_ms=100_000, stale_after_ms=1000) == (state, [])


def test_mark_stale_clamps_the_close_to_the_opening() -> None:
    run = Run()
    bash_permission(run, 1000)
    stale, effects = mark_stale(run.now, now_ms=500, stale_after_ms=-1000)
    assert isinstance(effects[0], BlockClosed)
    assert effects[0].block.closed_at_ms == 1010
    assert stale.session.status == "stale"


# Telemetry: close_block_denied.


def test_close_block_denied_closes_from_telemetry() -> None:
    run = Run()
    run("UserPromptSubmit", 100)
    bash_permission(run, 200)
    state, effects = close_block_denied(run.now, tool_use_id="t1", at_ms=250)
    closed, changed = effects
    assert isinstance(closed, BlockClosed)
    assert (closed.block.close_reason, closed.block.close_source, closed.block.closed_at_ms) == (
        "denied",
        "telemetry",
        250,
    )
    assert state.open_blocks == ()
    assert changed == SessionChanged(session=state.session, previous_status="waiting_user")
    assert state.session.status == "working"
    assert state.session.last_event_at_ms == run.now.session.last_event_at_ms


def test_close_block_denied_clamps_to_the_opening() -> None:
    run = Run()
    run("PreToolUse", 1000, tool_name="Bash", tool_use_id="t1")
    run("PreToolUse", 1001, tool_name="Bash", tool_use_id="t2")
    run("PermissionRequest", 1010, tool_name="Bash")
    run("PermissionRequest", 1020, tool_name="Bash")
    state, effects = close_block_denied(run.now, tool_use_id="t1", at_ms=10)
    (closed,) = effects  # the status stays waiting_user, so the session did not change
    assert isinstance(closed, BlockClosed)
    assert (closed.block.tool_use_id, closed.block.closed_at_ms) == ("t1", 1020)
    assert state.session.status == "waiting_user"
    assert [b.tool_use_id for b in state.open_blocks] == ["t2"]


def test_close_block_denied_without_a_match_changes_nothing() -> None:
    run = Run()
    bash_permission(run, 1000)
    assert close_block_denied(run.now, tool_use_id="nope", at_ms=2000) == (run.now, [])


# Fixture replays.


def statuses(run: Run) -> list[str]:
    return [e.session.status for e in run.effects if isinstance(e, SessionChanged)]


def fixture_value(name: str, event_name: str, key: str) -> str:
    """``key`` of the last ``event_name`` payload of a fixture, read at test time so that no
    captured ID is copied into the tests (tests/test_fixtures.py checks it)."""
    lines = (HOOK_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    payloads = [json.loads(line)["payload"] for line in lines]
    value = [p[key] for p in payloads if p["hook_event_name"] == event_name][-1]
    assert isinstance(value, str)
    return value


@pytest.mark.parametrize("path", sorted(HOOK_FIXTURES.glob("*.jsonl")), ids=lambda p: p.stem)
def test_every_fixture_replays_into_one_session(path: Path) -> None:
    run = replay(path.stem)
    session_ids = {e.session.session_id for e in run.effects if isinstance(e, SessionChanged)}
    assert len(session_ids) == 1
    assert run.now.session.started_at_ms == BASE
    assert {b.block_id for b in run.opened()} == {b.block_id for b in run.closed()}


def test_replay_interactive_permission_compact() -> None:
    run = replay("interactive-permission-compact")
    first, second = run.closed()
    # The approved dialog: closed by its PostToolUse after about 11.4 s, notified at 6 s.
    assert (first.tool_name, first.tool_use_id) == ("Bash", "toolu_01EXAMPLE000000000000022")
    assert (first.close_reason, first.close_source) == ("tool_completed", "hook")
    assert waited(first) == 11_391
    assert first.notified_at_ms == BASE + 17_445
    # The dialog denied with Esc fires no hook: PreCompact supersedes it 102.8 s later.
    assert second.tool_use_id == "toolu_01EXAMPLE000000000000023"
    assert (second.close_reason, second.closed_at_ms) == ("superseded", BASE + 138_866)
    assert second.notified_at_ms is None
    # The compaction's and the post-Stop SubagentStop are internal agents.
    assert [(a.internal, a.status) for a in run.agents()] == [(True, "stopped")] * 2
    # The /compact ends the turn the Esc left open: idle after PostCompact, and the
    # Esc-denied call is no longer pending.
    assert statuses(run)[13:16] == ["working", "working", "idle"]
    assert run.now.pending_tools == ()
    assert run.now.agents == ()
    session = run.now.session
    assert (session.status, session.end_reason) == ("ended", "prompt_input_exit")
    assert (session.source, session.model) == ("startup", "claude-haiku-5-5")
    # The prompt_id of /exit, which no UserPromptSubmit carried.
    assert session.last_prompt_id == fixture_value(
        "interactive-permission-compact", "SessionEnd", "prompt_id"
    )


def test_replay_interactive_elicitation_idle() -> None:
    run = replay("interactive-elicitation-idle")
    (block,) = run.closed()
    assert (block.kind, block.mcp_server_name, block.close_reason) == (
        "elicitation",
        "spike-elicit",
        "elicitation_cancel",
    )
    assert block.tool_use_id == "toolu_01EXAMPLE000000000000025"
    assert block.notified_at_ms == BASE + 20_232
    assert block.closed_at_ms == BASE + 23_748
    session = run.now.session
    assert session.idle_notified_at_ms == BASE + 85_094
    assert (session.status, session.end_reason) == ("ended", "prompt_input_exit")
    (agent,) = run.agents()
    assert agent.internal


def test_replay_headless_tools_subagent() -> None:
    run = replay("headless-tools-subagent")
    subagent = fixture_value("headless-tools-subagent", "SubagentStart", "agent_id")
    closed = run.closed()
    assert [(b.tool_name, b.scope, b.close_reason, b.closed_at_ms) for b in closed] == [
        ("Bash", None, "superseded", BASE + 6166),  # PostToolBatch
        ("Write", None, "superseded", BASE + 9015),  # the Agent call's PreToolUse
        ("PowerShell", subagent, "superseded", BASE + 10_787),  # the agent's batch
    ]
    assert [b.tool_use_id for b in closed] == [
        "toolu_01EXAMPLE000000000000004",
        "toolu_01EXAMPLE000000000000005",
        "toolu_01EXAMPLE000000000000007",
    ]
    (agent,) = run.agents()
    assert (agent.agent_type, agent.status, agent.internal, agent.background) == (
        "Explore",
        "stopped",
        False,
        False,
    )
    assert agent.tool_use_id == "toolu_01EXAMPLE000000000000006"
    assert (run.now.session.status, run.now.session.end_reason) == ("ended", "other")


def test_replay_headless_async_command_transport() -> None:
    run = replay("headless-async-command-transport")
    assert run.closed() == []
    (agent,) = run.agents()
    subagent = fixture_value("headless-async-command-transport", "SubagentStart", "agent_id")
    assert (agent.agent_id, agent.background, agent.internal) == (subagent, True, False)
    assert agent.tool_use_id == "toolu_01EXAMPLE000000000000020"
    assert agent.status == "stopped"
    # The main Stop arrives while the background agent runs: still working until it stops.
    assert statuses(run)[18:] == ["working", "idle", "working", "idle"]
    assert run.now.session.status == "idle"
    assert run.now.pending_tools == ()


def test_replay_headless_api_error() -> None:
    run = replay("headless-api-error")
    session = run.now.session
    assert session.last_error_code == "model_not_found"
    assert statuses(run) == ["idle", "idle", "working", "idle", "ended"]
    assert run.opened() == []


def test_replay_without_session_start_creates_the_session_lazily() -> None:
    run = replay("headless-sessionstart-http-ignored")
    assert run.now.session.source is None
    assert statuses(run) == ["idle", "working", "idle", "ended"]


@pytest.mark.parametrize("name", ["headless-edit-commit-tasks", "hooks-endpoint-timeout"])
def test_replay_plain_headless_runs(name: str) -> None:
    run = replay(name)
    assert run.opened() == []
    assert run.now.pending_tools == ()
    assert set(statuses(run)[2:-2]) == {"working"}
    assert statuses(run)[-2:] == ["idle", "ended"]
