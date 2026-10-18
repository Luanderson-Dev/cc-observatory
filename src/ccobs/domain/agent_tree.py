"""The agents tree of a session page: each subagent under the agent that launched it.

The input is one ``AgentRef`` per agent: its ``agent_id``, its ``parent_agent_id`` (the
scope of the event that started it, ``None`` for the main thread) and an ordering key,
such as its start time. The data is not trusted to be a tree, so:

- an agent whose parent is not in the input (on another page of the API, or never
  seen), or that names itself as its parent, hangs from the main thread;
- parents that form a cycle are cut at the cycle's first-seen agent (lowest ordering
  key, then input position), which then hangs from the main thread;
- a repeated ``agent_id`` keeps its first-seen entry.

Siblings come in first-seen order. The result is the root (the main thread, with
``agent_id`` ``None`` and depth 0) and the same nodes flattened in pre-order, so a
template iterates ``rows`` and indents by ``depth`` without recursion. Building is
iterative too: a deep chain cannot hit Python's recursion limit.

Standard library only, and no I/O: the CLI imports the domain (tests/test_cli_light.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["AgentNode", "AgentRef", "AgentTree", "build_agent_tree"]


@dataclass(frozen=True, kw_only=True)
class AgentRef:
    agent_id: str
    parent_agent_id: str | None
    order: int


@dataclass(frozen=True, kw_only=True)
class AgentNode:
    """An agent in the tree. ``parent_agent_id`` is the parent it hangs from, which is
    ``None`` (the main thread) for an orphan or for the agent that cut a cycle."""

    agent_id: str | None
    parent_agent_id: str | None
    depth: int
    children: tuple[AgentNode, ...]


@dataclass(frozen=True, kw_only=True)
class AgentTree:
    root: AgentNode
    rows: tuple[AgentNode, ...]


def build_agent_tree(refs: Iterable[AgentRef]) -> AgentTree:
    # First-seen order: the ordering key, then the input position (sorted is stable).
    ordered: dict[str, AgentRef] = {}
    for item in sorted(refs, key=lambda item: item.order):
        ordered.setdefault(item.agent_id, item)
    rank = {agent_id: position for position, agent_id in enumerate(ordered)}
    parent: dict[str, str | None] = {
        agent_id: (
            item.parent_agent_id
            if item.parent_agent_id in ordered and item.parent_agent_id != agent_id
            else None
        )
        for agent_id, item in ordered.items()
    }
    _cut_cycles(parent, rank)

    children: dict[str | None, list[str]] = {}
    for known in ordered:
        children.setdefault(parent[known], []).append(known)

    # Pre-order with an explicit stack of (agent_id, depth).
    preorder: list[tuple[str | None, int]] = []
    stack: list[tuple[str | None, int]] = [(None, 0)]
    while stack:
        visited, depth = stack.pop()
        preorder.append((visited, depth))
        stack.extend((child, depth + 1) for child in reversed(children.get(visited, [])))

    # In reverse pre-order every child is built before its parent.
    nodes: dict[str | None, AgentNode] = {}
    for node_id, depth in reversed(preorder):
        nodes[node_id] = AgentNode(
            agent_id=node_id,
            parent_agent_id=None if node_id is None else parent[node_id],
            depth=depth,
            children=tuple(nodes[child] for child in children.get(node_id, [])),
        )
    return AgentTree(root=nodes[None], rows=tuple(nodes[node_id] for node_id, _ in preorder[1:]))


def _cut_cycles(parent: dict[str, str | None], rank: dict[str, int]) -> None:
    """Point the first-seen agent of every cycle at the main thread, in place."""
    resolved: set[str] = set()
    for start in rank:
        path: list[str] = []
        on_path: set[str] = set()
        current = start
        while current not in resolved and current not in on_path:
            path.append(current)
            on_path.add(current)
            next_up = parent[current]
            if next_up is None:
                break
            current = next_up
        else:
            if current in on_path:
                cycle = path[path.index(current) :]
                parent[min(cycle, key=rank.__getitem__)] = None
        resolved.update(path)
