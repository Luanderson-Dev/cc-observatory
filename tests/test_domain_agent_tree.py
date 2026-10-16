"""The agents tree of a session page: subagents under the agent that launched them.

``parent_agent_id`` comes from the API (stage 3): the scope of the event that started
the agent, ``None`` for the main thread. The data is not trusted to be a tree: a parent
can be missing (on another page, or forgotten), an agent can name itself, and parents can
form a cycle. Every such agent hangs from the main thread, and building never loops.
"""

from __future__ import annotations

import pytest

from ccobs.domain.agent_tree import AgentNode, AgentRef, build_agent_tree


def ref(agent_id: str, parent: str | None = None, order: int = 0) -> AgentRef:
    return AgentRef(agent_id=agent_id, parent_agent_id=parent, order=order)


def outline(refs: list[AgentRef]) -> list[tuple[str | None, int, str | None]]:
    return [(row.agent_id, row.depth, row.parent_agent_id) for row in build_agent_tree(refs).rows]


def test_an_empty_session_has_only_the_main_thread() -> None:
    tree = build_agent_tree([])
    assert tree.root == AgentNode(agent_id=None, parent_agent_id=None, depth=0, children=())
    assert tree.rows == ()


def test_flat_agents_hang_from_the_main_thread_in_order() -> None:
    assert outline([ref("b", order=2), ref("a", order=1), ref("c", order=3)]) == [
        ("a", 1, None),
        ("b", 1, None),
        ("c", 1, None),
    ]


def test_nested_agents_come_in_pre_order_with_their_depth() -> None:
    refs = [
        ref("a", order=1),
        ref("b", order=2),
        ref("a1", "a", order=3),
        ref("a1x", "a1", order=5),
        ref("a2", "a", order=4),
        ref("b1", "b", order=6),
    ]
    assert outline(refs) == [
        ("a", 1, None),
        ("a1", 2, "a"),
        ("a1x", 3, "a1"),
        ("a2", 2, "a"),
        ("b", 1, None),
        ("b1", 2, "b"),
    ]


def test_root_holds_the_same_nodes_as_the_rows() -> None:
    tree = build_agent_tree([ref("a", order=1), ref("a1", "a", order=2)])
    (a,) = tree.root.children
    (a1,) = a.children
    assert tree.rows == (a, a1)
    assert a.agent_id == "a"
    assert a1 == AgentNode(agent_id="a1", parent_agent_id="a", depth=2, children=())


def test_a_child_listed_before_its_parent_still_nests() -> None:
    assert outline([ref("child", "parent", order=1), ref("parent", order=2)]) == [
        ("parent", 1, None),
        ("child", 2, "parent"),
    ]


def test_an_orphan_hangs_from_the_main_thread() -> None:
    assert outline([ref("a", order=1), ref("lost", "gone", order=2)]) == [
        ("a", 1, None),
        ("lost", 1, None),
    ]


def test_a_self_parented_agent_hangs_from_the_main_thread() -> None:
    assert outline([ref("a", "a", order=1)]) == [("a", 1, None)]


def test_a_two_cycle_is_cut_at_its_first_seen_agent() -> None:
    assert outline([ref("b", "a", order=2), ref("a", "b", order=1)]) == [
        ("a", 1, None),
        ("b", 2, "a"),
    ]


def test_a_longer_cycle_with_a_tail_is_cut_once() -> None:
    # tail -> c -> a -> b -> c: the cycle is a, b, c and a is seen first.
    refs = [
        ref("tail", "c", order=0),
        ref("c", "b", order=3),
        ref("a", "c", order=1),
        ref("b", "a", order=2),
    ]
    assert outline(refs) == [
        ("a", 1, None),
        ("b", 2, "a"),
        ("c", 3, "b"),
        ("tail", 4, "c"),
    ]


def test_several_cycles_are_each_cut() -> None:
    refs = [
        ref("x", "y", order=1),
        ref("y", "x", order=2),
        ref("p", "q", order=4),
        ref("q", "p", order=3),
    ]
    assert outline(refs) == [
        ("x", 1, None),
        ("y", 2, "x"),
        ("q", 1, None),
        ("p", 2, "q"),
    ]


def test_duplicates_keep_the_first_seen_entry() -> None:
    assert outline([ref("a", order=1), ref("b", order=2), ref("b", "a", order=3)]) == [
        ("a", 1, None),
        ("b", 1, None),
    ]


def test_equal_order_keys_keep_the_input_order() -> None:
    assert outline([ref("z"), ref("y"), ref("x")]) == [
        ("z", 1, None),
        ("y", 1, None),
        ("x", 1, None),
    ]


@pytest.mark.parametrize("size", [1, 2, 5000])
def test_a_deep_chain_builds_without_recursion(size: int) -> None:
    refs = [ref(f"a{i}", f"a{i - 1}" if i else None, order=i) for i in range(size)]
    rows = build_agent_tree(refs).rows
    assert [row.depth for row in rows] == list(range(1, size + 1))
    assert rows[-1].agent_id == f"a{size - 1}"


def test_a_long_cycle_builds() -> None:
    size = 5000
    refs = [ref(f"a{i}", f"a{(i + 1) % size}", order=i) for i in range(size)]
    rows = build_agent_tree(refs).rows
    assert rows[0].agent_id == "a0"
    assert rows[0].depth == 1
    assert len(rows) == size
    assert rows[-1].agent_id == "a1"
    assert rows[-1].depth == size
