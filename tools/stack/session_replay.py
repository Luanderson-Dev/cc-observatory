"""Replay the hook fixtures in process through ``parse_hook`` and ``sessions.apply``.

The unit tests of the reducer (``tests/test_domain_sessions.py``) and of the alert rules
(``tests/test_domain_alerts.py``) share these helpers. They live in a package that the
repository root on ``sys.path`` makes importable (``pythonpath = ["."]``), so the import
works in every pytest import mode, unlike an import of one test module from another.

``timeline`` and ``replay`` take the receive time from each fixture line's ``offset_ms``
after ``BASE``, and the order from the line index.

The helpers fail with ``AssertionError``, as a test's ``assert`` would; they raise it
explicitly because ``assert`` is reserved to test modules (ruff S101).

Developer tool; not part of the published package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

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
)

__all__ = ["BASE", "HOOK_FIXTURES", "SESSION", "Run", "event", "replay", "timeline", "waited"]

HOOK_FIXTURES: Final = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hooks"
SESSION: Final = "00000000-0000-4000-8000-000000000000"
BASE: Final = 1_800_000_000_000


def event(name: str, **fields: Any) -> HookEvent:
    return parse_hook({"session_id": SESSION, "hook_event_name": name, **fields})


class Run:
    """Feeds events to ``apply`` one at a time, with increasing ``seq``, and keeps every
    effect."""

    def __init__(self) -> None:
        self.state: SessionState | None = None
        self.effects: list[Effect] = []
        self.last: list[Effect] = []
        self.seq = 0

    def __call__(self, name: str, at: int, **fields: Any) -> SessionState:
        self.seq += 1
        self.state, self.last = apply(self.state, event(name, **fields), at, self.seq)
        self.effects.extend(self.last)
        return self.state

    @property
    def now(self) -> SessionState:
        if self.state is None:
            raise AssertionError("no event was applied yet")
        return self.state

    def opened(self) -> list[Block]:
        return [e.block for e in self.effects if isinstance(e, BlockOpened)]

    def notified(self) -> list[Block]:
        return [e.block for e in self.effects if isinstance(e, BlockNotified)]

    def closed(self) -> list[Block]:
        return [e.block for e in self.effects if isinstance(e, BlockClosed)]

    def agents(self) -> list[Agent]:
        """The latest record of every agent, from the effects: stopped agents leave the
        state, as closed blocks do."""
        latest: dict[str, Agent] = {}
        for effect in self.effects:
            if isinstance(effect, AgentChanged):
                latest[effect.agent.agent_id] = effect.agent
        return list(latest.values())

    def agent(self, agent_id: str) -> Agent:
        (found,) = [agent for agent in self.agents() if agent.agent_id == agent_id]
        in_state = self.now.agent(agent_id)
        if in_state != (found if found.status == "running" else None):
            raise AssertionError(f"agent {agent_id} in the state: {in_state!r}, expected {found!r}")
        return found


def timeline(name: str) -> list[tuple[int, HookEvent]]:
    """The events of the hook fixture ``tests/fixtures/hooks/<name>.jsonl``, parsed, each
    with its receive time, in file order."""
    lines = (HOOK_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    documents = [json.loads(line) for line in lines]
    return [(BASE + d["offset_ms"], parse_hook(d["payload"])) for d in documents]


def replay(name: str) -> Run:
    """The hook fixture ``tests/fixtures/hooks/<name>.jsonl`` fed through ``apply``."""
    run = Run()
    for index, (at, hook) in enumerate(timeline(name)):
        run.seq = index + 1
        run.state, run.last = apply(run.state, hook, at, run.seq)
        run.effects.extend(run.last)
    return run


def waited(block: Block) -> int:
    """How long a closed block was open, in milliseconds."""
    if block.closed_at_ms is None:
        raise AssertionError(f"block {block.block_id} is still open")
    return block.closed_at_ms - block.opened_at_ms
