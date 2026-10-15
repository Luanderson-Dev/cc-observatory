"""The session state machine: a pure reducer over hook events (ADR 0024).

``apply`` folds one hook event (``ccobs.domain.hooks``) into the state of its session and
returns the new state with the effects the storage layer persists. The state is
immutable: a session, its agents keyed by ``agent_id``, its open blocks, and the
bookkeeping that links blocks and agents to tool calls. Closed blocks leave the state and
travel only as ``BlockClosed`` effects.

Hook payloads carry no timestamp (ADR 0011), so time comes in as the server's receive
time (``received_at_ms``) and order as the persisted sequence number (``seq``). There is
no wall clock here: ``mark_stale`` (the sweep) and ``close_block_denied`` (telemetry) get
their time as arguments. A time that goes backwards never yields a negative duration:
every close and notification is clamped to the block's opening.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from ccobs.domain.hooks import (
    Elicitation,
    ElicitationResult,
    HookEvent,
    Notification,
    PermissionDenied,
    PermissionRequest,
    PostCompact,
    PostToolBatch,
    PostToolUse,
    PostToolUseFailure,
    PreCompact,
    PreToolUse,
    SessionEnd,
    SessionStart,
    Stop,
    StopFailure,
    SubagentStart,
    SubagentStop,
    UserPromptSubmit,
)

__all__ = [
    "Agent",
    "AgentChanged",
    "AgentStatus",
    "AgentStopReason",
    "Block",
    "BlockClosed",
    "BlockKind",
    "BlockNotified",
    "BlockOpened",
    "CloseSource",
    "Effect",
    "PendingTool",
    "Session",
    "SessionChanged",
    "SessionState",
    "SessionStatus",
    "apply",
    "close_block_denied",
    "mark_stale",
]

type SessionStatus = Literal["working", "waiting_user", "idle", "ended", "stale"]
type AgentStatus = Literal["running", "stopped"]
type AgentStopReason = Literal["stopped", "session_ended", "stale"]
type BlockKind = Literal["permission", "elicitation"]
type CloseSource = Literal["hook", "telemetry", "sweep"]

# How many completed tool_use_ids a session remembers, so that a PreToolUse delivered
# after its PostToolUse (async delivery, ADR 0011) creates no pending call.
_COMPLETED_KEPT: Final = 64
# How many stopped agent IDs a session remembers, so that a late event of a stopped agent
# does not bring it back as running. Stopped agents leave the state, as closed blocks do.
_STOPPED_KEPT: Final = 64

# The tool that starts subagents, and its tool_response.status for a background one.
_AGENT_TOOL: Final = "Agent"
_ASYNC_LAUNCHED: Final = "async_launched"

# Notification types that mark a dialog: a block of this kind (P1: idle_prompt is not).
_NOTIFIED_KINDS: Final[dict[str, BlockKind]] = {
    "permission_prompt": "permission",
    "elicitation_dialog": "elicitation",
}
_IDLE_PROMPT: Final = "idle_prompt"
_MANUAL_TRIGGER: Final = "manual"
# The documented ElicitationResult actions. Any other value closes as elicitation_other,
# so close reasons stay a bounded set.
_ELICITATION_ACTIONS: Final = frozenset({"accept", "decline", "cancel"})


# State.


@dataclass(frozen=True, kw_only=True)
class Session:
    """One Claude Code session. Times are receive times in milliseconds since the epoch.

    ``turn_open`` is the main thread's turn, from ``UserPromptSubmit`` to ``Stop`` or
    ``StopFailure``; ``compacting`` runs from ``PreCompact`` to ``PostCompact``.
    """

    session_id: str
    status: SessionStatus
    started_at_ms: int
    last_event_at_ms: int
    last_seq: int
    turn_open: bool = False
    compacting: bool = False
    source: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    effort_level: str | None = None
    last_prompt_id: str | None = None
    last_error_code: str | None = None
    idle_notified_at_ms: int | None = None
    ended_at_ms: int | None = None
    end_reason: str | None = None
    # Personal data (ADR 0006): out of repr, like HookEvent.cwd.
    cwd: str | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class Agent:
    """A subagent. ``internal`` agents (compaction, titles, suggestions) fire
    ``SubagentStop`` without ``SubagentStart`` and never make the session work.
    ``tool_use_id`` is the Agent tool call that launched it, when it could be linked.
    ``last_event_at_ms`` is the latest receive time of an event in its scope.
    ``stop_reason`` is ``stopped`` (its SubagentStop), ``session_ended`` or ``stale``."""

    agent_id: str
    status: AgentStatus
    last_event_at_ms: int
    agent_type: str | None = None
    internal: bool = False
    background: bool = False
    tool_use_id: str | None = None
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    stop_reason: AgentStopReason | None = None


@dataclass(frozen=True, kw_only=True)
class Block:
    """A period in which the session waits on a person. ``block_id`` is the ``seq`` of the
    event that opened it; ``scope`` is the ``agent_id`` it belongs to, ``None`` for the
    main thread."""

    block_id: int
    kind: BlockKind
    scope: str | None
    opened_at_ms: int
    tool_name: str | None = None
    tool_use_id: str | None = None
    mcp_server_name: str | None = None
    elicitation_id: str | None = None
    notified_at_ms: int | None = None
    closed_at_ms: int | None = None
    close_reason: str | None = None
    close_source: CloseSource | None = None


@dataclass(frozen=True, kw_only=True)
class PendingTool:
    """A ``PreToolUse`` with no outcome yet, for linking ``PermissionRequest`` (which has
    no ``tool_use_id``), elicitations and subagents to their tool call."""

    tool_use_id: str
    tool_name: str | None
    scope: str | None
    mcp_server_name: str | None


@dataclass(frozen=True, kw_only=True)
class SessionState:
    session: Session
    # Running agents only: a stopped agent leaves through its AgentChanged effect.
    agents: tuple[Agent, ...] = ()
    stopped_agent_ids: tuple[str, ...] = ()
    open_blocks: tuple[Block, ...] = ()
    pending_tools: tuple[PendingTool, ...] = ()
    completed_tool_use_ids: tuple[str, ...] = ()
    # Agent calls reported as async_launched that no SubagentStart has claimed yet.
    async_launches: tuple[str, ...] = ()

    def agent(self, agent_id: str) -> Agent | None:
        return next((agent for agent in self.agents if agent.agent_id == agent_id), None)


# Effects: each carries the full new record, so storage can upsert it.


@dataclass(frozen=True, kw_only=True)
class SessionChanged:
    """Always the last effect of ``apply``; ``previous_status`` is ``None`` on creation."""

    session: Session
    previous_status: SessionStatus | None


@dataclass(frozen=True, kw_only=True)
class AgentChanged:
    agent: Agent


@dataclass(frozen=True, kw_only=True)
class BlockOpened:
    block: Block


@dataclass(frozen=True, kw_only=True)
class BlockNotified:
    block: Block


@dataclass(frozen=True, kw_only=True)
class BlockClosed:
    block: Block


type Effect = SessionChanged | AgentChanged | BlockOpened | BlockNotified | BlockClosed


# The working copy that one call mutates and then freezes.


class _Work:
    def __init__(self, state: SessionState, at: int, seq: int) -> None:
        self.session = state.session
        self.agents = {agent.agent_id: agent for agent in state.agents}
        self.stopped = list(state.stopped_agent_ids)
        self.blocks = list(state.open_blocks)
        self.pending = list(state.pending_tools)
        self.completed = list(state.completed_tool_use_ids)
        self.launches = list(state.async_launches)
        self.at = at
        self.seq = seq
        self.effects: list[Effect] = []

    def freeze(self) -> SessionState:
        return SessionState(
            session=self.session,
            agents=tuple(self.agents.values()),
            stopped_agent_ids=tuple(self.stopped),
            open_blocks=tuple(self.blocks),
            pending_tools=tuple(self.pending),
            completed_tool_use_ids=tuple(self.completed),
            async_launches=tuple(self.launches),
        )

    def put_agent(self, agent: Agent) -> None:
        """Record an agent change; a stopped agent leaves the state, keeping its ID."""
        self.effects.append(AgentChanged(agent=agent))
        if agent.status == "running":
            self.agents[agent.agent_id] = agent
            return
        self.agents.pop(agent.agent_id, None)
        self.stopped = [*self.stopped, agent.agent_id][-_STOPPED_KEPT:]

    def stop_agent(self, agent: Agent, at: int, reason: AgentStopReason) -> None:
        # Its Agent call is no longer claimable by another SubagentStart; the call's own
        # PostToolUse still completes it.
        self.pending = [p for p in self.pending if p.tool_use_id != agent.tool_use_id]
        self.drop_pending(agent.agent_id)
        self.put_agent(
            replace(
                agent,
                status="stopped",
                stopped_at_ms=at,
                stop_reason=reason,
                last_event_at_ms=max(agent.last_event_at_ms, at),
            )
        )

    # Blocks.

    def open_block(self, block: Block) -> None:
        self.blocks.append(block)
        self.effects.append(BlockOpened(block=block))

    def close(
        self, match: Callable[[Block], bool], reason: str, source: CloseSource = "hook"
    ) -> None:
        kept: list[Block] = []
        for block in self.blocks:
            if not match(block):
                kept.append(block)
                continue
            closed = replace(
                block,
                closed_at_ms=max(self.at, block.opened_at_ms),
                close_reason=reason,
                close_source=source,
            )
            self.effects.append(BlockClosed(block=closed))
        self.blocks = kept

    def supersede(self, scope: str | None, keep_tool_use_id: str | None = None) -> None:
        """Progress in ``scope`` means nobody is waiting there any more."""
        self.close(
            lambda b: (
                b.scope == scope and (keep_tool_use_id is None or b.tool_use_id != keep_tool_use_id)
            ),
            "superseded",
        )

    def notify(self, block: Block) -> None:
        notified = replace(block, notified_at_ms=max(self.at, block.opened_at_ms))
        self.blocks = [notified if b.block_id == block.block_id else b for b in self.blocks]
        self.effects.append(BlockNotified(block=notified))

    # Tool calls.

    def latest_unblocked(self, match: Callable[[PendingTool], bool]) -> PendingTool | None:
        """The latest pending call that ``match`` accepts and no open block links to."""
        blocked = {block.tool_use_id for block in self.blocks}
        candidates = [p for p in self.pending if match(p) and p.tool_use_id not in blocked]
        return candidates[-1] if candidates else None

    def complete(self, tool_use_id: str | None) -> None:
        if tool_use_id is None:
            return
        self.pending = [p for p in self.pending if p.tool_use_id != tool_use_id]
        if tool_use_id not in self.completed:
            self.completed = [*self.completed, tool_use_id][-_COMPLETED_KEPT:]

    def drop_pending(self, scope: str | None) -> None:
        self.pending = [p for p in self.pending if p.scope != scope]


def _same_call(
    block: Block, scope: str | None, tool_name: str | None, tool_use_id: str | None
) -> bool:
    """Whether a tool outcome belongs to ``block``: by ``tool_use_id`` when both have one,
    else by scope and tool (a block with no tool takes any tool of its scope)."""
    if block.tool_use_id is not None and tool_use_id is not None:
        return block.tool_use_id == tool_use_id
    return block.scope == scope and block.tool_name in (None, tool_name)


def _derive(work: _Work) -> SessionStatus:
    if work.blocks:
        return "waiting_user"
    agent_running = any(a.status == "running" and not a.internal for a in work.agents.values())
    if work.session.turn_open or work.session.compacting or agent_running:
        return "working"
    return "idle"


# Handlers, one per event that changes more than the session metadata.


def _turn_end(work: _Work, scope: str | None) -> None:
    """A main-thread turn ended. It also proves that a compaction is over, in case its
    PostCompact was lost or the compaction failed."""
    work.supersede(scope)
    work.drop_pending(scope)
    if scope is None:
        work.session = replace(work.session, turn_open=False, compacting=False)


def _pre_tool_use(work: _Work, event: PreToolUse, scope: str | None) -> None:
    tool_use_id = event.tool_use_id
    if tool_use_id is not None and tool_use_id in work.completed:
        return  # its outcome already arrived (async delivery)
    work.supersede(scope, keep_tool_use_id=tool_use_id)
    if tool_use_id is not None and all(p.tool_use_id != tool_use_id for p in work.pending):
        work.pending.append(
            PendingTool(
                tool_use_id=tool_use_id,
                tool_name=event.tool_name,
                scope=scope,
                mcp_server_name=event.mcp_server_name,
            )
        )


def _permission_request(work: _Work, event: PermissionRequest, scope: str | None) -> None:
    link = work.latest_unblocked(lambda p: p.scope == scope and p.tool_name == event.tool_name)
    work.open_block(
        Block(
            block_id=work.seq,
            kind="permission",
            scope=scope,
            opened_at_ms=work.at,
            tool_name=event.tool_name,
            tool_use_id=None if link is None else link.tool_use_id,
            mcp_server_name=event.mcp_server_name
            or (None if link is None else link.mcp_server_name),
        )
    )


def _tool_outcome(
    work: _Work,
    event: PostToolUse | PostToolUseFailure | PermissionDenied,
    scope: str | None,
    reason: str,
) -> None:
    work.close(lambda b: _same_call(b, scope, event.tool_name, event.tool_use_id), reason)
    work.complete(event.tool_use_id)


def _post_tool_use(work: _Work, event: PostToolUse, scope: str | None) -> None:
    _tool_outcome(work, event, scope, "tool_completed")
    tool_use_id = event.tool_use_id
    if (
        event.tool_name != _AGENT_TOOL
        or event.tool_response_status != _ASYNC_LAUNCHED
        or tool_use_id is None
    ):
        return
    linked = next((a for a in work.agents.values() if a.tool_use_id == tool_use_id), None)
    if linked is None:
        work.launches.append(tool_use_id)
    else:
        work.put_agent(replace(linked, background=True))


def _notification(work: _Work, event: Notification, scope: str | None) -> None:
    if event.notification_type == _IDLE_PROMPT:
        work.session = replace(work.session, idle_notified_at_ms=work.at)
        return
    kind = _NOTIFIED_KINDS.get(event.notification_type or "")
    if kind is None:
        return
    same_kind = [b for b in work.blocks if b.kind == kind]
    if not same_kind:
        _open_fallback(work, kind, scope)
        return
    candidates = [b for b in same_kind if b.notified_at_ms is None]
    if candidates:
        work.notify(min(candidates, key=lambda b: (b.scope != scope, b.block_id)))


def _open_fallback(work: _Work, kind: BlockKind, scope: str | None) -> None:
    """A dialog notification with no block of its kind: the request event was missed."""
    if kind == "permission":
        link = work.latest_unblocked(lambda p: p.scope == scope)
    else:
        link = work.latest_unblocked(lambda p: p.scope == scope and p.mcp_server_name is not None)
    work.open_block(
        Block(
            block_id=work.seq,
            kind=kind,
            scope=scope,
            opened_at_ms=work.at,
            notified_at_ms=work.at,
            tool_name=None if link is None else link.tool_name,
            tool_use_id=None if link is None else link.tool_use_id,
            mcp_server_name=None if link is None else link.mcp_server_name,
        )
    )


def _elicitation(work: _Work, event: Elicitation, scope: str | None) -> None:
    server = event.mcp_server_name
    link = work.latest_unblocked(
        lambda p: p.scope == scope and p.mcp_server_name is not None and p.mcp_server_name == server
    )
    work.open_block(
        Block(
            block_id=work.seq,
            kind="elicitation",
            scope=scope,
            opened_at_ms=work.at,
            tool_name=None if link is None else link.tool_name,
            tool_use_id=None if link is None else link.tool_use_id,
            mcp_server_name=server,
            elicitation_id=event.elicitation_id,
        )
    )


def _elicitation_result(work: _Work, event: ElicitationResult, scope: str | None) -> None:
    def matches(block: Block) -> bool:
        if block.kind != "elicitation":
            return False
        if block.elicitation_id is not None and event.elicitation_id is not None:
            return block.elicitation_id == event.elicitation_id
        return event.mcp_server_name in (None, block.mcp_server_name)

    candidates = [b for b in work.blocks if matches(b)]
    if not candidates:
        return
    target = min(candidates, key=lambda b: (b.scope != scope, b.block_id)).block_id
    action = event.action if event.action in _ELICITATION_ACTIONS else "other"
    work.close(lambda b: b.block_id == target, f"elicitation_{action}")


def _link_agent(work: _Work, agent: Agent) -> Agent:
    """Link a starting subagent to its Agent call: a pending async launch first (a
    background agent), else the latest pending Agent call no agent has claimed."""
    if agent.tool_use_id is not None:
        return agent
    if work.launches:
        return replace(agent, tool_use_id=work.launches.pop(0), background=True)
    claimed = {a.tool_use_id for a in work.agents.values()}
    calls = [p for p in work.pending if p.tool_name == _AGENT_TOOL and p.tool_use_id not in claimed]
    return replace(agent, tool_use_id=calls[-1].tool_use_id) if calls else agent


def _subagent_start(work: _Work, event: SubagentStart) -> None:
    # Events of a stopped agent never get here (apply), so a late start cannot revive it.
    agent_id = event.agent_id
    if agent_id is None:
        return
    agent_type = event.agent_type or None
    known = work.agents.get(agent_id)
    if known is None:
        agent = Agent(
            agent_id=agent_id,
            status="running",
            last_event_at_ms=work.at,
            agent_type=agent_type,
            started_at_ms=work.at,
        )
    else:  # created by an earlier event of its scope
        agent = replace(
            known,
            agent_type=agent_type or known.agent_type,
            last_event_at_ms=max(known.last_event_at_ms, work.at),
        )
    work.put_agent(_link_agent(work, agent))


def _subagent_stop(work: _Work, event: SubagentStop) -> None:
    agent_id = event.agent_id
    if agent_id is None:
        return
    known = work.agents.get(agent_id)
    if known is not None:
        work.stop_agent(known, work.at, "stopped")
    else:
        work.put_agent(
            Agent(
                agent_id=agent_id,
                status="stopped",
                last_event_at_ms=work.at,
                agent_type=event.agent_type or None,
                internal=True,
                stopped_at_ms=work.at,
                stop_reason="stopped",
            )
        )
    work.close(lambda b: b.scope == agent_id, "agent_stopped")
    work.drop_pending(agent_id)


def _session_end(work: _Work, event: SessionEnd) -> None:
    work.close(lambda b: True, "session_ended")
    work.pending = []
    work.launches = []
    for agent in list(work.agents.values()):
        work.stop_agent(agent, work.at, "session_ended")
    work.session = replace(
        work.session,
        turn_open=False,
        compacting=False,
        ended_at_ms=work.at,
        end_reason=event.reason,
    )


def _touch_agent(work: _Work, event: HookEvent) -> None:
    """Every event in an agent's scope updates its ``last_event_at_ms``. Agent events can
    arrive before the agent's SubagentStart, so the first one creates it. Events of a
    stopped agent never get here (apply), so they do not bring it back."""
    agent_id = event.agent_id
    if agent_id is None or isinstance(event, SubagentStart | SubagentStop):
        return
    known = work.agents.get(agent_id)
    if known is None:
        agent = Agent(
            agent_id=agent_id,
            status="running",
            last_event_at_ms=work.at,
            agent_type=event.agent_type or None,
            started_at_ms=work.at,
        )
    else:
        agent = replace(known, last_event_at_ms=max(known.last_event_at_ms, work.at))
    work.put_agent(agent)


def _handle(work: _Work, event: HookEvent) -> None:
    scope = event.agent_id
    session = work.session
    match event:
        case UserPromptSubmit():
            work.supersede(scope)
            if scope is None:
                # A new turn also proves that a compaction is over.
                work.session = replace(
                    session, turn_open=True, compacting=False, idle_notified_at_ms=None
                )
        case Stop():
            _turn_end(work, scope)
        case StopFailure():
            work.session = replace(session, last_error_code=event.error)
            _turn_end(work, scope)
        case PreCompact():
            if event.trigger == _MANUAL_TRIGGER and scope is None:
                # /compact is typed at the prompt, so the main thread runs no turn: a
                # turn still open was left by an Esc, which fires no Stop.
                _turn_end(work, scope)
            else:
                work.supersede(scope)
            work.session = replace(work.session, compacting=True)
        case PostCompact():
            work.session = replace(session, compacting=False)
        case PostToolBatch():
            work.supersede(scope)
            work.drop_pending(scope)
        case PreToolUse():
            _pre_tool_use(work, event, scope)
        case PermissionRequest():
            _permission_request(work, event, scope)
        case PostToolUse():
            _post_tool_use(work, event, scope)
        case PostToolUseFailure():
            _tool_outcome(work, event, scope, "tool_failed")
        case PermissionDenied():
            _tool_outcome(work, event, scope, "denied")
        case Notification():
            _notification(work, event, scope)
        case Elicitation():
            _elicitation(work, event, scope)
        case ElicitationResult():
            _elicitation_result(work, event, scope)
        case SubagentStart():
            _subagent_start(work, event)
        case SubagentStop():
            _subagent_stop(work, event)
        case SessionStart():
            work.session = replace(
                session,
                source=session.source or event.source,
                model=event.model or session.model,
            )
        case SessionEnd():
            _session_end(work, event)
        case _:
            pass  # unknown or unhandled: the metadata update in apply is all


# Public functions.


def apply(
    state: SessionState | None, event: HookEvent, received_at_ms: int, seq: int
) -> tuple[SessionState, list[Effect]]:
    """Fold one hook event into its session's state (ADR 0024).

    ``state`` is ``None`` for a session not seen yet: any event creates it.
    ``received_at_ms`` is when the app received the event and ``seq`` its persisted
    order; an event whose ``seq`` is not above the session's ``last_seq`` was already
    applied and changes nothing. The effects end with one ``SessionChanged``.
    """
    if state is None:
        previous: SessionStatus | None = None
        state = SessionState(
            session=Session(
                session_id=event.session_id,
                status="idle",
                started_at_ms=received_at_ms,
                last_event_at_ms=received_at_ms,
                last_seq=seq,
            )
        )
    elif event.session_id != state.session.session_id:
        # A programming error: the caller picks the state by the event's session_id.
        raise ValueError("the event belongs to another session")
    elif seq <= state.session.last_seq:
        return state, []
    else:
        previous = state.session.status

    work = _Work(state, received_at_ms, seq)
    session = work.session
    work.session = replace(
        session,
        started_at_ms=min(session.started_at_ms, received_at_ms),
        last_event_at_ms=max(session.last_event_at_ms, received_at_ms),
        last_seq=seq,
        last_prompt_id=event.prompt_id or session.last_prompt_id,
        permission_mode=event.permission_mode or session.permission_mode,
        effort_level=event.effort_level or session.effort_level,
        cwd=event.cwd or session.cwd,
    )
    if event.agent_id is None or event.agent_id not in work.stopped:
        _touch_agent(work, event)
        _handle(work, event)
    # else: an event of a stopped agent's scope only updates the session's metadata, so
    # that, for example, its PermissionRequest cannot open a block nothing would close.

    status: SessionStatus
    if isinstance(event, SessionEnd):
        status = "ended"
    elif isinstance(event, SessionStart) and previous in ("ended", "stale"):
        status = work.session.status  # a late SessionStart never moves the state back
    else:
        status = _derive(work)
    if status != "ended":
        work.session = replace(work.session, ended_at_ms=None, end_reason=None)
    work.session = replace(work.session, status=status)
    work.effects.append(SessionChanged(session=work.session, previous_status=previous))
    return work.freeze(), work.effects


def mark_stale(
    state: SessionState, now_ms: int, stale_after_ms: int
) -> tuple[SessionState, list[Effect]]:
    """The sweep: give up on what has been silent for ``stale_after_ms``.

    A silent session becomes ``stale``: its open blocks close with reason ``stale`` and
    source ``sweep`` at ``now_ms`` (clamped to each opening), and its running agents
    stop with ``stop_reason`` ``stale``. In a session that is not silent, a running
    agent silent that long (its SubagentStop was lost) stops the same way, and the blocks
    of its scope close. A stale agent's ``stopped_at_ms`` is its ``last_event_at_ms``.
    Ended and already stale sessions are left alone.
    """
    session = state.session
    if session.status in ("ended", "stale"):
        return state, []
    session_silent = now_ms - session.last_event_at_ms >= stale_after_ms
    silent = [
        agent
        for agent in state.agents
        if session_silent or now_ms - agent.last_event_at_ms >= stale_after_ms
    ]
    if not session_silent and not silent:
        return state, []
    work = _Work(state, now_ms, session.last_seq)
    scopes = {agent.agent_id for agent in silent}
    work.close(lambda b: session_silent or b.scope in scopes, "stale", "sweep")
    for agent in silent:
        work.stop_agent(agent, agent.last_event_at_ms, "stale")
    status: SessionStatus = "stale" if session_silent else _derive(work)
    if status != session.status:
        work.session = replace(session, status=status)
        work.effects.append(SessionChanged(session=work.session, previous_status=session.status))
    return work.freeze(), work.effects


def close_block_denied(
    state: SessionState, *, tool_use_id: str, at_ms: int
) -> tuple[SessionState, list[Effect]]:
    """Close the open blocks of ``tool_use_id`` as ``denied`` with source ``telemetry``,
    for a denial that only telemetry reports (a dialog dismissed with Esc, ADR 0012).
    ``at_ms`` is clamped to each block's opening. Without a match nothing changes."""
    if all(block.tool_use_id != tool_use_id for block in state.open_blocks):
        return state, []
    work = _Work(state, at_ms, state.session.last_seq)
    work.close(lambda b: b.tool_use_id == tool_use_id, "denied", "telemetry")
    # Blocks are open only in a derived state: SessionEnd and mark_stale close them all.
    status = _derive(work)
    previous = state.session.status
    if status != previous:
        work.session = replace(work.session, status=status)
        work.effects.append(SessionChanged(session=work.session, previous_status=previous))
    return work.freeze(), work.effects
