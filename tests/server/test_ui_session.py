"""The web UI's session page and its fragments (``ccobs.server.ui``, ADR 0031).

Sessions, agents, blocks and hook events are written straight to the database for exact
control, and real hook fixtures go through ``/hooks`` for the shapes Claude Code sends.
The page shows the path with the home directory as ``~`` (never the full ``cwd``, product
decision P2), only the hook metadata ADR 0023 stores, escapes every stored value, reads
through the read gate once per request and pages every list by keyset without
duplicates.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import OperationalError

from ccobs.domain.grafana_links import dashboard_url, session_links, session_range
from ccobs.domain.hook_config import HOOK_PATH
from ccobs.domain.presentation import format_duration
from ccobs.server import ui
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.models import (
    AgentModel,
    BlockModel,
    HookEventModel,
    SessionModel,
)
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue
from ccobs.server.reads import ReadGate
from ccobs.server.writer import Writer, WriterObservers

HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
APP_JS: Final = Path(__file__).parents[2] / "src" / "ccobs" / "server" / "static" / "js" / "app.js"
HOST: Final = "test-host"
NOW: Final = 1_800_000_000_000
SECOND_MS: Final = 1_000
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
USERNAME: Final = "canaryuser"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
CWD: Final = f"/home/{USERNAME}/work/api-server"
XSS: Final = "<script>\"'&"
RAW_XSS: Final = ("<script>", "\"'&")
ESCAPED_XSS: Final = "&lt;script&gt;&#34;&#39;&amp;"
CONTENT_CANARY: Final = "content-canary-7f3a"
GRAFANA: Final = "http://localhost:4330"
MOVED_GRAFANA: Final = "http://localhost:24330"
type RowValues = dict[str, object]
SESSION_OPTIONAL: Final = (
    "last_prompt_id",
    "last_error_code",
    "idle_notified_at_ms",
    "ended_at_ms",
    "end_reason",
)
EVENT_OPTIONAL: Final = (
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


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _client(tmp_path, monkeypatch)


@pytest.fixture
def moved_grafana_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with Grafana on another port, as compose gives it from CCOBS_GRAFANA_PORT."""
    yield from _client(tmp_path, monkeypatch, grafana_url=MOVED_GRAFANA + "/")


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **values: Any) -> Iterator[TestClient]:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    settings = Settings(data_dir=tmp_path / "data", host_label=HOST, **values)

    def manual(
        queue: HookQueue, repository: Repository, observers: WriterObservers
    ) -> ManualWriter:
        return ManualWriter(queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0)

    app = create_app(
        settings,
        clock=Clock(),
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def engine_of(client: TestClient) -> Engine:
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    return repository.engine


def session_row(
    session_id: str = SID, status: str = "working", last_event_at_ms: int = NOW, **values: object
) -> RowValues:
    return {
        "session_id": session_id,
        "host": HOST,
        "status": status,
        "started_at_ms": last_event_at_ms - HOUR_MS,
        "last_event_at_ms": last_event_at_ms,
        "last_seq": 1,
        "turn_open": status in ("working", "waiting_user"),
        "compacting": False,
        "cwd": CWD,
        **dict.fromkeys(SESSION_OPTIONAL),
        **values,
    }


def block_row(block_id: int, opened_at_ms: int, **values: object) -> RowValues:
    return {
        "session_id": SID,
        "host": HOST,
        "block_id": block_id,
        "kind": "permission",
        "scope_agent_id": None,
        "tool_name": "Bash",
        "mcp_server_name": None,
        "opened_at_ms": opened_at_ms,
        "closed_at_ms": None,
        "close_reason": None,
        "close_source": None,
        **values,
    }


def closed_block(block_id: int, opened_at_ms: int, waited_ms: int, reason: str) -> RowValues:
    return block_row(
        block_id,
        opened_at_ms,
        closed_at_ms=opened_at_ms + waited_ms,
        close_reason=reason,
        close_source="hook",
    )


def agent_row(agent_id: str, started_at_ms: int | None, **values: object) -> RowValues:
    return {
        "session_id": SID,
        "host": HOST,
        "agent_id": agent_id,
        "status": "running",
        "agent_type": "general-purpose",
        "internal": False,
        "background": False,
        "tool_use_id": None,
        "started_at_ms": started_at_ms,
        "last_event_at_ms": started_at_ms if started_at_ms is not None else NOW,
        "stopped_at_ms": None,
        "stop_reason": None,
        **values,
    }


def event_row(seq: int, name: str = "PreToolUse", **values: object) -> RowValues:
    return {
        "seq": seq,
        "host": HOST,
        "session_id": SID,
        "received_at_ms": NOW - HOUR_MS + seq * SECOND_MS,
        "hook_event_name": name,
        **dict.fromkeys(EVENT_OPTIONAL),
        **values,
    }


def seed(
    client: TestClient,
    sessions: Sequence[RowValues] = (),
    *,
    blocks: Sequence[RowValues] = (),
    agents: Sequence[RowValues] = (),
    events: Sequence[RowValues] = (),
) -> None:
    with engine_of(client).begin() as connection:
        if sessions:
            connection.execute(insert(SessionModel), list(sessions))
        for model, rows in (
            (BlockModel, blocks),
            (AgentModel, agents),
            (HookEventModel, events),
        ):
            if rows:
                connection.execute(insert(model), list(rows))


def page(
    client: TestClient, url: str, *, status: int = 200, headers: dict[str, str] | None = None
) -> str:
    response = client.get(url, headers=headers)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    return response.text


def section(body: str, element_id: str) -> str:
    """The HTML of the element with ``element_id``, up to its closing tag (no nesting of
    the same tag inside, which holds for the lists and controls checked here)."""
    found = re.search(rf'<(\w+)\b[^>]*\bid="{element_id}"[^>]*>(.*?)</\1>', body, re.DOTALL)
    assert found is not None, element_id
    return found.group(2)


def block_ids(body: str) -> list[int]:
    return [int(found) for found in re.findall(r'data-block-id="(\d+)"', body)]


def agent_ids(body: str) -> list[str]:
    return [html.unescape(found) for found in re.findall(r'data-agent-id="([^"]+)"', body)]


def seqs(body: str) -> list[int]:
    return [int(found) for found in re.findall(r'data-seq="(\d+)"', body)]


def more_href(body: str, element_id: str) -> str | None:
    """The ``hx-get`` of the "Show older" control in ``element_id``, if it has one."""
    found = re.search(rf'<div\b[^>]*\bid="{element_id}"[^>]*>(.*?)</div>', body, re.DOTALL)
    if found is None:
        return None
    href = re.search(r'hx-get="([^"]+)"', found.group(1))
    return html.unescape(href.group(1)) if href else None


def tail(body: str) -> re.Match[str]:
    found = re.search(r'<div\b[^>]*\bid="timeline-tail"[^>]*>', body)
    assert found is not None
    return found


def tail_href(body: str) -> str:
    href = re.search(r'hx-get="([^"]+)"', tail(body).group(0))
    assert href is not None
    return html.unescape(href.group(1))


def url(suffix: str = "", session_id: str = SID) -> str:
    return f"/ui/sessions/{session_id}/{suffix}"


# Not found.


@pytest.mark.parametrize(
    "session_id",
    ["unknown-session", "%01bad", "x" * 300],
    ids=["unknown", "control-character", "too-long"],
)
def test_an_unknown_or_invalid_session_is_a_styled_404_without_echo(
    client: TestClient, session_id: str
) -> None:
    seed(client, [session_row()])

    body = page(client, f"/sessions/{session_id}", status=404)

    assert "Session not found" in body
    assert '<header class="masthead">' in body
    assert 'href="/sessions"' in body
    assert session_id not in body
    assert html.unescape(session_id) not in body


@pytest.mark.parametrize(
    "suffix",
    [
        "blocks?before=5",
        "agents?before=a1",
        "events?before=5",
        "events?after=0",
        "live?blocks_since=1&agents_since=1",
    ],
)
def test_the_fragments_of_an_unknown_session_are_404(client: TestClient, suffix: str) -> None:
    for session_id in ("unknown-session", "%01bad"):
        body = page(client, url(suffix, session_id), status=404)
        assert "Session not found" in body
        assert "<html" not in body
        assert "unknown-session" not in body


# The header.


def test_the_header_shows_the_tilde_path_and_never_the_cwd(client: TestClient) -> None:
    seed(client, [session_row()])

    body = page(client, f"/sessions/{SID}")

    assert "~/work/api-server" in body
    assert USERNAME not in body
    assert CWD not in body
    assert "<title>api-server - cc-observatory</title>" in body
    assert '<h1 class="session-head__project">api-server</h1>' in body


def test_the_header_shows_the_ids_state_and_times(client: TestClient) -> None:
    seed(client, [session_row(last_event_at_ms=NOW - 2 * MINUTE_MS, last_error_code="rate_limit")])

    body = page(client, f"/sessions/{SID}")
    head = section(body, "session-head")

    assert "session-head strip--working" in body
    assert '<span class="session-head__state">Working</span>' in head
    assert "In a turn" in head
    assert f'<code class="session-head__id">{SID}</code>' in head
    assert f'<code class="session-head__short">{SID[:8]}</code>' in head
    started = NOW - 2 * MINUTE_MS - HOUR_MS
    assert f'data-since-ms="{started}">{format_duration(HOUR_MS + 2 * MINUTE_MS)}</time>' in head
    assert f'data-since-ms="{NOW - 2 * MINUTE_MS}">2m 00s</time>' in head
    assert "<code>rate_limit</code>" in head
    assert "Ended" not in head


def test_a_waiting_session_shows_its_wait_since_its_oldest_open_block(
    client: TestClient,
) -> None:
    seed(
        client,
        [session_row(status="waiting_user")],
        blocks=[block_row(7, NOW - 4 * MINUTE_MS), block_row(9, NOW - MINUTE_MS)],
    )

    head = section(page(client, f"/sessions/{SID}"), "session-head")

    assert "Waiting on you" in head
    assert (
        f'<time class="session-head__wait" datetime="2027-01-15T07:56:00Z" '
        f'data-since-ms="{NOW - 4 * MINUTE_MS}">4m 00s</time>'
    ) in head
    assert "Permission: Bash" in head


def test_a_waiting_session_without_an_open_block_waits_since_its_last_event(
    client: TestClient,
) -> None:
    seed(client, [session_row(status="waiting_user", last_event_at_ms=NOW - MINUTE_MS)])

    head = section(page(client, f"/sessions/{SID}"), "session-head")

    assert (
        'class="session-head__wait" datetime="2027-01-15T07:59:00Z" '
        f'data-since-ms="{NOW - MINUTE_MS}"'
    ) in head


def test_an_ended_session_says_when_and_why(client: TestClient) -> None:
    seed(
        client,
        [
            session_row(
                status="ended",
                last_event_at_ms=NOW - HOUR_MS,
                ended_at_ms=NOW - HOUR_MS,
                end_reason="prompt_input_exit",
            )
        ],
    )

    head = section(page(client, f"/sessions/{SID}"), "session-head")

    assert '<span class="session-head__state">Ended</span>' in head
    assert "Exited" in head
    assert f'data-since-ms="{NOW - HOUR_MS}">1h 00m</time> ago' in head


def test_an_ended_session_without_a_known_reason_does_not_repeat_ended(
    client: TestClient,
) -> None:
    seed(client, [session_row(status="ended", ended_at_ms=NOW, end_reason="other")])

    head = section(page(client, f"/sessions/{SID}"), "session-head")

    assert head.count('<span class="session-head__state">Ended</span>') == 1
    assert "session-head__doing" not in head


def test_a_session_without_cwd_has_placeholders(client: TestClient) -> None:
    seed(client, [session_row(cwd=None)])

    body = page(client, f"/sessions/{SID}")

    assert "Unknown project" in body
    assert "<title>Session - cc-observatory</title>" in body
    assert "session-head__path" not in body


def test_the_overview_links_to_the_session_page(client: TestClient) -> None:
    seed(client, [session_row()])

    assert f'href="/sessions/{SID}"' in page(client, "/")
    page(client, f"/sessions/{SID}")


# Links to Grafana.


def anchors(body: str, element_id: str) -> dict[str, str]:
    """The links in ``element_id``, by their text, with their href unescaped."""
    links = {}
    for attributes, text in re.findall(r"<a\b([^>]*)>([^<]*)</a>", section(body, element_id)):
        href = re.search(r'\bhref="([^"]*)"', attributes)
        assert href is not None, attributes
        assert 'target="_blank"' in attributes, attributes
        assert 'rel="noopener noreferrer"' in attributes, attributes
        assert "hx-" not in attributes, attributes
        links[text.strip()] = html.unescape(href.group(1))
    return links


def header_grafana(body: str) -> str:
    found = re.search(r'<a\b[^>]*\bclass="masthead__grafana"[^>]*>', body)
    assert found is not None
    assert 'target="_blank"' in found.group(0)
    assert 'rel="noopener noreferrer"' in found.group(0)
    href = re.search(r'\bhref="([^"]*)"', found.group(0))
    assert href is not None
    return html.unescape(href.group(1))


def test_an_ended_session_links_grafana_over_its_lifetime(client: TestClient) -> None:
    ended = NOW - HOUR_MS
    seed(client, [session_row(status="ended", last_event_at_ms=ended, ended_at_ms=ended)])

    body = page(client, f"/sessions/{SID}")

    expected = session_links(
        GRAFANA, host=HOST, session_id=SID, time_range=session_range(ended - HOUR_MS, ended)
    )
    links = anchors(body, "grafana-links")
    assert links == {
        "Logs": expected.logs,
        "Tokens and cost": expected.usage,
        "Dashboard": expected.dashboard,
    }
    assert "Open in Grafana" in section(body, "grafana-links")


def test_a_live_session_links_grafana_up_to_now(client: TestClient) -> None:
    seed(client, [session_row(status="waiting_user")])

    links = anchors(page(client, f"/sessions/{SID}"), "grafana-links")

    expected = session_links(
        GRAFANA, host=HOST, session_id=SID, time_range=session_range(NOW - HOUR_MS, None)
    )
    assert links["Logs"] == expected.logs
    assert "to=now" in links["Dashboard"]


def test_a_stale_session_links_grafana_up_to_its_last_event(client: TestClient) -> None:
    last = NOW - 13 * HOUR_MS
    seed(client, [session_row(status="stale", last_event_at_ms=last)])

    links = anchors(page(client, f"/sessions/{SID}"), "grafana-links")

    window = session_range(last - HOUR_MS, last)
    assert links["Dashboard"] == dashboard_url(GRAFANA, host=HOST, time_range=window)


def test_the_grafana_links_escape_a_hostile_session_id(client: TestClient) -> None:
    hostile = f"{XSS}-{SID}"
    seed(client, [session_row(session_id=hostile)])

    body = page(client, "/sessions/" + quote(hostile, safe=""))

    links = anchors(body, "grafana-links")
    expected = session_links(
        GRAFANA, host=HOST, session_id=hostile, time_range=session_range(NOW - HOUR_MS, None)
    )
    assert links["Logs"] == expected.logs
    assert links["Tokens and cost"] == expected.usage
    # Percent-encoded in the URL, and escaped again in the attribute.
    for raw in RAW_XSS:
        assert raw not in section(body, "grafana-links")


def test_the_grafana_links_stay_out_of_the_live_refresh(client: TestClient) -> None:
    seed(client, [session_row()])

    live = page(client, url("live?blocks_since=1&agents_since=1"))

    assert "grafana-links" not in live
    assert "localhost:4330" not in live


def test_every_page_header_links_the_grafana_dashboard(client: TestClient) -> None:
    seed(client, [session_row()])
    expected = dashboard_url(GRAFANA, host=HOST)

    for path, status in (
        ("/", 200),
        ("/sessions", 200),
        (f"/sessions/{SID}", 200),
        ("/sessions/unknown-session", 404),
    ):
        assert header_grafana(page(client, path, status=status)) == expected, path


def test_the_links_follow_the_grafana_url_setting(moved_grafana_client: TestClient) -> None:
    seed(moved_grafana_client, [session_row()])

    body = page(moved_grafana_client, f"/sessions/{SID}")

    assert header_grafana(body) == dashboard_url(MOVED_GRAFANA, host=HOST)
    for href in anchors(body, "grafana-links").values():
        assert href.startswith(MOVED_GRAFANA + "/")
        assert not href.startswith(MOVED_GRAFANA + "//")


# Blocks.


def test_open_blocks_come_first_then_closed_ones_newest_first(client: TestClient) -> None:
    seed(
        client,
        [session_row(status="waiting_user")],
        blocks=[
            closed_block(1, NOW - HOUR_MS, 4 * MINUTE_MS + 5 * SECOND_MS, "denied"),
            closed_block(2, NOW - 50 * MINUTE_MS, 30 * SECOND_MS, "tool_completed"),
            {
                **closed_block(0, NOW - 2 * HOUR_MS, MINUTE_MS, "tool_failed"),
                "kind": "elicitation",
                "mcp_server_name": "tracker",
            },
            block_row(3, NOW - 2 * MINUTE_MS, kind="elicitation", mcp_server_name="tracker"),
            block_row(4, NOW - MINUTE_MS, scope_agent_id="agent-0000aaaa1111"),
        ],
    )

    body = page(client, f"/sessions/{SID}")

    assert block_ids(section(body, "open-blocks")) == [3, 4]
    assert block_ids(section(body, "closed-blocks")) == [2, 1, 0]
    open_blocks = section(body, "open-blocks")
    assert "Question from tracker" in open_blocks
    assert f'data-since-ms="{NOW - 2 * MINUTE_MS}">2m 00s</time>' in open_blocks
    assert "<code>agent-00</code>" in open_blocks
    closed = section(body, "closed-blocks")
    assert "Waited 4m 05s" in closed
    assert "Waited 30s" in closed
    assert "Denied" in closed
    assert "Allowed, the tool ran" in closed
    assert "Answered, the tool failed" in closed
    assert more_href(body, "blocks-more") is None


def test_a_session_without_blocks_says_so(client: TestClient) -> None:
    seed(client, [session_row()])

    body = page(client, f"/sessions/{SID}")

    assert "No blocks: this session never waited on you." in body
    assert 'id="closed-blocks"' not in body


def test_the_blocks_pager_pages_to_the_end_without_duplicates(client: TestClient) -> None:
    closed = [
        closed_block(block_id, NOW - HOUR_MS, MINUTE_MS, "denied") for block_id in range(10, 130)
    ]
    # An open block below every closed one: on the first page only.
    seed(
        client,
        [session_row(status="waiting_user")],
        blocks=[block_row(5, NOW - 2 * HOUR_MS), *closed, block_row(200, NOW)],
    )

    body = page(client, f"/sessions/{SID}")
    assert block_ids(section(body, "open-blocks")) == [5, 200]
    seen = block_ids(section(body, "closed-blocks"))
    assert seen == list(range(129, 79, -1))
    href = more_href(body, "blocks-more")
    assert href == url("blocks?before=80")
    control = section(body, "blocks-more")
    # Older pages go to a list of their own, which the live refresh leaves alone.
    assert 'hx-target="#closed-blocks-older"' in control
    assert '<ol id="closed-blocks-older" class="blocks blocks--older"></ol>' in body
    assert 'hx-swap="beforeend"' in control

    fragments = 0
    while href is not None:
        fragment = page(client, href)
        fragments += 1
        assert "<html" not in fragment
        assert '<div id="blocks-more" class="more" hx-swap-oob="true">' in fragment
        seen += block_ids(fragment)
        href = more_href(fragment, "blocks-more")

    assert fragments == 2
    assert seen == list(range(129, 9, -1))
    assert 5 not in seen


def test_a_blocks_page_past_the_end_is_empty(client: TestClient) -> None:
    seed(client, [session_row()], blocks=[closed_block(3, NOW, MINUTE_MS, "denied")])

    fragment = page(client, url("blocks?before=3"))

    assert block_ids(fragment) == []
    assert more_href(fragment, "blocks-more") is None


# Agents.


def test_the_agents_tree_hangs_each_subagent_under_its_launcher(client: TestClient) -> None:
    seed(
        client,
        [session_row()],
        agents=[
            agent_row(
                "parent-agent-1",
                NOW - 10 * MINUTE_MS,
                tool_use_id="toolu_parent",
                status="stopped",
                stopped_at_ms=NOW - 5 * MINUTE_MS,
                stop_reason="stopped",
            ),
            agent_row(
                "child-agent-1",
                NOW - 8 * MINUTE_MS,
                tool_use_id="toolu_child",
                agent_type="Explore",
            ),
            agent_row("other-agent-1", NOW - 2 * MINUTE_MS, background=True),
            agent_row(
                "internal-agent",
                None,
                internal=True,
                status="stopped",
                stopped_at_ms=NOW - MINUTE_MS,
                agent_type=None,
                stop_reason="stopped",
            ),
        ],
        events=[
            event_row(1, tool_name="Agent", tool_use_id="toolu_parent"),
            event_row(2, tool_name="Agent", tool_use_id="toolu_child", agent_id="parent-agent-1"),
        ],
    )

    body = page(client, f"/sessions/{SID}")
    agents = section(body, "agents")

    # Newest first among siblings, each child right under its parent.
    assert agent_ids(agents) == [
        "internal-agent",
        "other-agent-1",
        "parent-agent-1",
        "child-agent-1",
    ]
    assert re.search(r'class="agent agent--depth-2[^"]*" data-agent-id="child-agent-1"', agents)
    assert re.search(r'class="agent agent--depth-1[^"]*" data-agent-id="parent-agent-1"', agents)
    assert "Main thread" in body
    assert "Explore" in agents
    assert "<code>child-ag</code>" in agents
    assert "Launched by <code>parent-a</code>" in agents
    # A stopped agent shows how long it ran; a running one ticks from its start.
    assert "Ran 5m 00s" in agents
    assert f'data-since-ms="{NOW - 8 * MINUTE_MS}">8m 00s</time>' in agents
    assert "Running" in agents
    assert "Stopped" in agents
    assert "Background" in agents
    assert "Internal" in agents


def test_launched_by_names_the_stored_parent_even_off_the_page(client: TestClient) -> None:
    seed(
        client,
        [session_row()],
        agents=[
            agent_row("orphan-agent-1", NOW - MINUTE_MS, tool_use_id="toolu_orphan"),
            agent_row("self-agent-01", NOW - 2 * MINUTE_MS, tool_use_id="toolu_self"),
        ],
        events=[
            # The launcher is not an agent of this page (nor stored at all).
            event_row(1, tool_name="Agent", tool_use_id="toolu_orphan", agent_id="ghost-agent-1"),
            event_row(2, tool_name="Agent", tool_use_id="toolu_self", agent_id="self-agent-01"),
        ],
    )

    agents = section(page(client, f"/sessions/{SID}"), "agents")

    assert re.search(r'class="agent agent--depth-1[^"]*" data-agent-id="orphan-agent-1"', agents)
    assert "Launched by <code>ghost-ag</code>" in agents
    # An agent that names itself as its parent has no launcher to show.
    assert "Launched by <code>self-age</code>" not in agents


def test_a_session_without_agents_says_so(client: TestClient) -> None:
    seed(client, [session_row()])

    body = page(client, f"/sessions/{SID}")

    assert "No subagents ran in this session." in body
    assert more_href(body, "agents-more") is None


def test_the_agents_pager_pages_to_the_end_without_duplicates(client: TestClient) -> None:
    stopped = [
        agent_row(
            f"stopped-{index:03d}",
            NOW - HOUR_MS + index * SECOND_MS,
            status="stopped",
            stopped_at_ms=NOW,
            stop_reason="stopped",
        )
        for index in range(120)
    ]
    # Running agents older than every stopped one: on the first page only.
    running = [agent_row(f"running-{index}", NOW - 2 * HOUR_MS + index) for index in range(2)]
    seed(client, [session_row()], agents=[*stopped, *running])

    body = page(client, f"/sessions/{SID}")
    seen = agent_ids(section(body, "agents"))
    assert len(seen) == 52
    href = more_href(body, "agents-more")
    assert href == url("agents?before=stopped-070")
    assert 'hx-target="#agents-older"' in section(body, "agents-more")
    assert '<ol id="agents-older" class="agents agents--older"></ol>' in body

    fragments = 0
    while href is not None:
        fragment = page(client, href)
        fragments += 1
        assert "<html" not in fragment
        assert '<div id="agents-more" class="more" hx-swap-oob="true">' in fragment
        seen += agent_ids(fragment)
        href = more_href(fragment, "agents-more")

    assert fragments == 2
    assert len(seen) == len(set(seen)) == 122
    expected_stopped = [f"stopped-{index:03d}" for index in range(119, -1, -1)]
    assert [agent for agent in seen if agent.startswith("stopped")] == expected_stopped


@pytest.mark.parametrize("before", ["no-such-agent", "%01", "x" * 300])
def test_an_unknown_or_invalid_agent_cursor_is_a_400_without_echo(
    client: TestClient, before: str
) -> None:
    seed(client, [session_row()])

    body = page(client, url(f"agents?before={before}"), status=400)

    assert "This page link is not valid" in body
    assert "no-such-agent" not in body


# The hook timeline.


def test_the_timeline_shows_the_newest_events_first_with_their_metadata(
    client: TestClient,
) -> None:
    seed(
        client,
        [session_row()],
        events=[
            event_row(1, "SessionStart", start_source="startup"),
            event_row(2, "PreToolUse", tool_name="mcp__tracker__pick", mcp_server_name="tracker"),
            event_row(
                3,
                "PostToolUse",
                tool_name="Bash",
                duration_ms=1_500,
                tool_response_status="ok",
                agent_id="agent-1234abcd5678",
                agent_type="Explore",
            ),
            event_row(4, "Notification", notification_type="permission_prompt"),
            event_row(5, "StopFailure", error_code="rate_limit"),
            event_row(
                6, "PostToolUseFailure", tool_name="Edit", duration_ms=250, is_interrupt=True
            ),
            event_row(7, "UserPromptSubmit", prompt_length=42),
        ],
    )

    body = page(client, f"/sessions/{SID}")
    timeline = section(body, "timeline")

    assert seqs(timeline) == [7, 6, 5, 4, 3, 2, 1]
    for text in (
        "SessionStart",
        "startup",
        "<code>mcp__tracker__pick</code>",
        "<code>tracker</code>",
        "<code>agent-12</code>",
        "Explore",
        "1s",
        "250 ms",
        "Interrupted",
        "permission_prompt",
        "rate_limit",
        "42 characters",
    ):
        assert text in timeline, text
    received = NOW - HOUR_MS + 7 * SECOND_MS
    assert f'data-since-ms="{received}">' in timeline
    assert more_href(body, "events-more") is None


def test_the_timeline_pager_pages_to_the_end_without_duplicates(client: TestClient) -> None:
    seed(client, [session_row()], events=[event_row(seq) for seq in range(1, 131)])

    body = page(client, f"/sessions/{SID}")
    seen = seqs(section(body, "timeline"))
    assert seen == list(range(130, 80, -1))
    href = more_href(body, "events-more")
    assert href == url("events?before=81")
    control = section(body, "events-more")
    assert 'hx-target="#timeline"' in control
    assert 'hx-swap="beforeend"' in control

    fragments = 0
    while href is not None:
        fragment = page(client, href)
        fragments += 1
        assert '<div id="events-more" class="more" hx-swap-oob="true">' in fragment
        seen += seqs(fragment)
        href = more_href(fragment, "events-more")

    assert fragments == 2
    assert seen == list(range(130, 0, -1))


def test_a_session_without_events_says_so_and_tails_from_zero(client: TestClient) -> None:
    seed(client, [session_row()])

    body = page(client, f"/sessions/{SID}")

    assert '<p id="timeline-empty" class="session-section__empty">' in body
    assert "No hook events stored for this session yet." in body
    assert tail_href(body) == url("events?after=0")


def test_the_first_rows_of_an_empty_timeline_delete_its_empty_message(
    client: TestClient,
) -> None:
    seed(client, [session_row()])
    delete = '<p id="timeline-empty" hx-swap-oob="delete"></p>'

    # Nothing new: the message stays.
    assert delete not in page(client, url("events?after=0"))

    seed(client, events=[event_row(1), event_row(2)])
    first = page(client, url("events?after=0"))
    assert delete in first
    assert first.index(delete) < first.index('<ol hx-swap-oob="afterbegin:#timeline">')
    # A later tail belongs to a page whose message is gone, or that never had one.
    seed(client, events=[event_row(3)])
    assert delete not in page(client, url("events?after=2"))
    assert 'id="timeline-empty"' not in page(client, f"/sessions/{SID}")


def test_the_tail_waits_for_a_change_and_swaps_itself(client: TestClient) -> None:
    seed(client, [session_row()], events=[event_row(seq) for seq in range(1, 4)])

    body = page(client, f"/sessions/{SID}")
    element = tail(body).group(0)

    assert tail_href(body) == url("events?after=3")
    assert 'hx-trigger="ccobs:changed from:body"' in element
    assert 'hx-swap="outerHTML"' in element
    # A change during a request replaces it: the last change of a burst is fetched.
    assert 'hx-sync="this:replace"' in element


def test_the_tail_returns_only_newer_events_and_a_new_cursor(client: TestClient) -> None:
    seed(client, [session_row()], events=[event_row(seq) for seq in range(1, 4)])

    quiet = page(client, url("events?after=3"))
    assert seqs(quiet) == []
    assert "hx-swap-oob" not in quiet
    assert tail_href(quiet) == url("events?after=3")

    seed(client, events=[event_row(seq) for seq in range(4, 7)])
    first = page(client, url("events?after=3"))
    again = page(client, url("events?after=3"))

    assert first == again
    assert '<ol hx-swap-oob="afterbegin:#timeline">' in first
    assert seqs(first) == [6, 5, 4]
    assert tail_href(first) == url("events?after=6")
    assert 'hx-trigger="ccobs:changed from:body"' in tail(first).group(0)
    assert seqs(page(client, url("events?after=6"))) == []


def test_a_tail_behind_by_more_than_a_page_catches_up_at_once(client: TestClient) -> None:
    total = ui.EVENTS_PAGE_SIZE + 5
    seed(client, [session_row()], events=[event_row(seq) for seq in range(1, total + 1)])

    first = page(client, url("events?after=0"))

    assert seqs(first) == list(range(ui.EVENTS_PAGE_SIZE, 0, -1))
    assert tail_href(first) == url(f"events?after={ui.EVENTS_PAGE_SIZE}")
    # The next batch is fetched as soon as the new tail is in the page.
    assert 'hx-trigger="load, ccobs:changed from:body"' in tail(first).group(0)
    rest = page(client, url(f"events?after={ui.EVENTS_PAGE_SIZE}"))
    assert seqs(rest) == list(range(total, ui.EVENTS_PAGE_SIZE, -1))
    assert 'hx-trigger="ccobs:changed from:body"' in tail(rest).group(0)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "before=1&after=1",
        "before=x",
        "after=-1",
        "before=" + str(2**63),
        "after=99999999999999999999",
        "before=",
    ],
)
def test_an_invalid_events_cursor_is_a_400_without_echo(client: TestClient, query: str) -> None:
    seed(client, [session_row()])

    body = page(client, url(f"events?{query}"), status=400)

    assert "This page link is not valid" in body
    assert "99999999999999999999" not in body


@pytest.mark.parametrize("query", ["", "before=x", "before=-1", "before=" + str(2**63)])
def test_an_invalid_blocks_cursor_is_a_400(client: TestClient, query: str) -> None:
    seed(client, [session_row()])

    body = page(client, url(f"blocks?{query}"), status=400)

    assert "This page link is not valid" in body


def test_an_absent_agents_cursor_is_a_400(client: TestClient) -> None:
    seed(client, [session_row()])

    assert "This page link is not valid" in page(client, url("agents"), status=400)


# Privacy and escaping.


def test_replayed_fixtures_show_no_content_and_no_username(client: TestClient) -> None:
    """Content fields of real payloads, filled with a canary, never reach the page: only
    ADR 0023 metadata is stored, and the page shows only stored columns."""
    payloads: list[dict[str, Any]] = []
    for path in sorted(HOOK_FIXTURES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            document = json.loads(line)["payload"]
            for key in (
                "prompt",
                "tool_input",
                "tool_response",
                "message",
                "last_assistant_message",
            ):
                if key in document:
                    document[key] = (
                        {"canary": CONTENT_CANARY}
                        if isinstance(document[key], dict)
                        else CONTENT_CANARY
                    )
            payloads.append(document)
    for document in payloads:
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    while writer.run_once(0.0):
        pass
    with engine_of(client).connect() as connection:
        stored = connection.execute(select(SessionModel.session_id, SessionModel.cwd)).all()
    assert stored

    for session_id, cwd in stored:
        body = page(client, f"/sessions/{session_id}")
        assert CONTENT_CANARY not in body
        assert 'data-seq="' in body
        if cwd:
            assert cwd not in body
            assert html.escape(cwd) not in body
            assert re.split(r"[\\/]", cwd)[-1] in body
        for user_path in re.findall(r"(?:Users|home)[\\/]+([^\\/]+)", cwd or ""):
            assert f"Users\\{user_path}" not in body
            assert f"home/{user_path}" not in body


def assert_escaped(body: str) -> None:
    for raw in RAW_XSS:
        assert raw not in body


def test_stored_values_are_escaped_everywhere(client: TestClient) -> None:
    session_id = f"s{XSS}"
    quoted = "s%3Cscript%3E%22%27%26"
    agent = f"a{XSS}"
    with engine_of(client).begin() as connection:
        connection.execute(
            insert(SessionModel),
            [
                session_row(
                    session_id,
                    "ended",
                    cwd=f"/srv/{XSS}",
                    last_error_code=XSS,
                    end_reason=XSS,
                    ended_at_ms=NOW,
                )
            ],
        )
        connection.execute(
            insert(BlockModel),
            [
                {
                    **block_row(1, NOW, tool_name=XSS, scope_agent_id=agent),
                    "session_id": session_id,
                },
                {
                    **closed_block(2, NOW, MINUTE_MS, XSS),
                    "session_id": session_id,
                    "kind": "elicitation",
                    "mcp_server_name": XSS,
                },
            ],
        )
        connection.execute(
            insert(AgentModel),
            [{**agent_row(agent, NOW, agent_type=XSS, tool_use_id="t"), "session_id": session_id}],
        )
        connection.execute(
            insert(HookEventModel),
            [
                {
                    **event_row(
                        1,
                        XSS,
                        **{
                            name: XSS
                            for name in EVENT_OPTIONAL
                            if name
                            not in (
                                "duration_ms",
                                "is_interrupt",
                                "prompt_length",
                                "tool_input_bytes",
                                "tool_response_bytes",
                            )
                        },
                    ),
                    "session_id": session_id,
                    "tool_use_id": "t",
                }
            ],
        )

    body = page(client, f"/sessions/{quoted}")
    assert_escaped(body)
    assert ESCAPED_XSS in body
    assert f'<code class="session-head__id">s{ESCAPED_XSS}</code>' in body
    assert f"Permission: {ESCAPED_XSS}" in body
    assert f"Question from {ESCAPED_XSS}" in body
    assert f'data-agent-id="a{ESCAPED_XSS}"' in body
    # Cursors and tails quote the session ID as a path segment.
    assert f'hx-get="/ui/sessions/{quoted}/events?after=1"' in body
    for suffix in ("blocks?before=3", "agents?before=zzz", "events?before=5", "events?after=0"):
        response = client.get(f"/ui/sessions/{quoted}/{suffix}")
        assert_escaped(response.text)
    fragment = page(client, f"/ui/sessions/{quoted}/events?after=0")
    assert ESCAPED_XSS in fragment
    assert f'hx-get="/ui/sessions/{quoted}/events?after=1"' in fragment


def test_an_agent_cursor_is_quoted(client: TestClient) -> None:
    agents = [
        agent_row(f"a&b {index:03d}", NOW - HOUR_MS + index, status="stopped", stopped_at_ms=NOW)
        for index in range(60)
    ]
    seed(client, [session_row()], agents=agents)

    href = more_href(page(client, f"/sessions/{SID}"), "agents-more")

    assert href == url("agents?before=a%26b+010")
    assert len(agent_ids(page(client, href))) == 10


# Live refresh (ADR 0032): the header, the open blocks and running agents, and what
# closed or stopped since the page loaded; never the older pages the user loaded.


def live_element(body: str) -> str:
    found = re.search(r'<div\b[^>]*\bid="session-live"[^>]*>', body)
    assert found is not None
    return found.group(0)


def live_url(
    *,
    blocks_from: int | None = None,
    agents_from: str | None = None,
    blocks_since: int = NOW,
    agents_since: int = NOW,
    keep_blocks: Sequence[int] = (),
    keep_agents: Sequence[str] = (),
) -> str:
    query: list[tuple[str, object]] = []
    if blocks_from is not None:
        query.append(("blocks_from", blocks_from))
    query.append(("blocks_since", blocks_since))
    if agents_from is not None:
        query.append(("agents_from", agents_from))
    query.append(("agents_since", agents_since))
    query += [("keep_block", block_id) for block_id in keep_blocks]
    query += [("keep_agent", agent_id) for agent_id in keep_agents]
    return url("live?" + urlencode(query))


def clock_of(client: TestClient) -> Clock:
    clock = app_of(client).state.clock
    assert isinstance(clock, Clock)
    return clock


def live_href(body: str) -> str:
    href = re.search(r'hx-get="([^"]+)"', live_element(body))
    assert href is not None
    return html.unescape(href.group(1))


def change(client: TestClient, model: Any, where: dict[str, object], **values: object) -> None:
    table = model.__table__
    condition = [table.c[name] == value for name, value in where.items()]
    with engine_of(client).begin() as connection:
        connection.execute(
            update(table).where(*condition, table.c.session_id == SID).values(values)
        )


def stopped_agent(
    agent_id: str, started_at_ms: int, stopped_at_ms: int = NOW - MINUTE_MS
) -> RowValues:
    return agent_row(
        agent_id,
        started_at_ms,
        status="stopped",
        stopped_at_ms=stopped_at_ms,
        stop_reason="stopped",
    )


def test_the_page_listens_for_changes_to_its_session(client: TestClient) -> None:
    seed(
        client,
        [session_row()],
        blocks=[closed_block(3, NOW - HOUR_MS, MINUTE_MS, "denied")],
        agents=[stopped_agent("a1", NOW - HOUR_MS), agent_row("r1", NOW - MINUTE_MS)],
    )

    body = page(client, f"/sessions/{SID}")
    element = live_element(body)

    # The page's boundaries: its oldest closed block and stopped agent, and its time.
    assert live_href(body) == live_url(blocks_from=3, agents_from="a1")
    assert " hidden " in element
    assert 'hx-trigger="ccobs:changed from:body"' in element
    assert 'hx-swap="outerHTML"' in element
    assert 'hx-sync="this:replace"' in element
    stream = f"/ui/stream?{urlencode({'session': SID})}"
    status = f'<div id="live-status" class="live-status" role="status" data-stream="{stream}">'
    assert status + "</div>" in body


def test_a_page_without_closed_rows_has_no_boundaries(client: TestClient) -> None:
    seed(client, [session_row()])

    assert live_href(page(client, f"/sessions/{SID}")) == live_url()


def test_the_live_fragment_refreshes_the_header_blocks_and_agents(client: TestClient) -> None:
    seed(
        client,
        [session_row(status="waiting_user")],
        blocks=[block_row(4, NOW - MINUTE_MS)],
        agents=[agent_row("r1", NOW - MINUTE_MS)],
    )
    href = live_href(page(client, f"/sessions/{SID}"))
    # The session moves on: the block closes and another opens, the agent stops and
    # another starts.
    change(client, SessionModel, {}, status="working")
    change(client, BlockModel, {"block_id": 4}, closed_at_ms=NOW, close_reason="denied")
    seed(client, blocks=[block_row(7, NOW)], agents=[agent_row("r2", NOW)])
    change(client, AgentModel, {"agent_id": "r1"}, status="stopped", stopped_at_ms=NOW)

    fragment = page(client, href)

    assert "<html" not in fragment
    # The main swap: the element that asked, with the same boundaries.
    assert fragment.lstrip().startswith('<div id="session-live"')
    assert live_href(fragment) == href
    header = '<header id="session-head" class="session-head strip--working" hx-swap-oob="true">'
    assert header in fragment
    assert '<span class="session-head__state">Working</span>' in section(fragment, "session-head")
    assert '<div id="blocks-live" hx-swap-oob="true">' in fragment
    assert block_ids(section(fragment, "open-blocks")) == [7]
    assert block_ids(section(fragment, "closed-blocks")) == [4]
    assert 'Blocks <span class="bay__count">2</span>' in fragment
    assert '<div id="agents-live" hx-swap-oob="true">' in fragment
    assert agent_ids(section(fragment, "agents")) == ["r2", "r1"]
    assert 'Agents <span class="bay__count">2</span>' in fragment
    # The older lists and their controls are left as they are.
    for untouched in ("closed-blocks-older", "blocks-more", "agents-older", "agents-more"):
        assert untouched not in fragment


def test_the_live_blocks_keep_their_boundary_and_take_blocks_closed_since(
    client: TestClient,
) -> None:
    closed = [
        closed_block(block_id, NOW - HOUR_MS, MINUTE_MS, "denied") for block_id in range(10, 130)
    ]
    seed(
        client,
        [session_row(status="waiting_user")],
        blocks=[block_row(5, NOW - HOUR_MS), *closed],
    )
    body = page(client, f"/sessions/{SID}")
    href = live_href(body)
    # Block 5, open below the boundary, is named in the link.
    assert href == live_url(blocks_from=80, keep_blocks=[5])
    assert more_href(body, "blocks-more") == url("blocks?before=80")
    # Block 5 closes with a time before the page's (as a sweep or a late hook would
    # close it); two new blocks close.
    change(client, BlockModel, {"block_id": 5}, closed_at_ms=NOW - 1, close_reason="stale")
    seed(
        client,
        blocks=[closed_block(block_id, NOW, MINUTE_MS, "denied") for block_id in (300, 301)],
    )

    fragment = page(client, href)

    # Everything from the boundary up, plus block 5: the pager, still before=80, goes on
    # below the boundary with no gap and no repeat.
    assert block_ids(section(fragment, "closed-blocks")) == [301, 300, *range(129, 79, -1), 5]
    assert 'id="open-blocks"' not in fragment
    assert live_href(fragment) == href


def test_a_live_block_window_past_its_cap_starts_over_with_a_new_pager(
    client: TestClient,
) -> None:
    old = [closed_block(block_id, NOW - HOUR_MS, MINUTE_MS, "denied") for block_id in range(1, 61)]
    seed(client, [session_row()], blocks=old)
    href = live_href(page(client, f"/sessions/{SID}"))
    assert href == live_url(blocks_from=11)
    new = range(100, 100 + ui.LIVE_CLOSED_LIMIT)
    seed(client, blocks=[closed_block(block_id, NOW, MINUTE_MS, "denied") for block_id in new])

    fragment = page(client, href)

    # The latest page of closed blocks, the older list emptied and a pager from there.
    assert block_ids(section(fragment, "closed-blocks")) == list(range(299, 249, -1))
    emptied = '<ol id="closed-blocks-older" class="blocks blocks--older" hx-swap-oob="true"></ol>'
    assert emptied in fragment
    assert '<div id="blocks-more" class="more" hx-swap-oob="true">' in fragment
    assert more_href(fragment, "blocks-more") == url("blocks?before=250")
    assert 'hx-target="#closed-blocks-older"' in section(fragment, "blocks-more")
    assert live_href(fragment) == live_url(blocks_from=250)
    assert "agents-older" not in fragment
    assert block_ids(page(client, url("blocks?before=250")))[:2] == [249, 248]


def test_after_a_reset_the_next_refresh_holds_the_new_page_and_new_rows_only(
    client: TestClient,
) -> None:
    old = [closed_block(block_id, NOW - HOUR_MS, MINUTE_MS, "denied") for block_id in range(1, 61)]
    seed(client, [session_row()], blocks=old)
    href = live_href(page(client, f"/sessions/{SID}"))
    burst = range(100, 100 + ui.LIVE_CLOSED_LIMIT)
    seed(client, blocks=[closed_block(block_id, NOW, MINUTE_MS, "denied") for block_id in burst])
    # The reset happens later than the burst closed.
    clock_of(client).now = NOW + 2 * MINUTE_MS
    reset = page(client, href)
    assert "closed-blocks-older" in reset
    second_href = live_href(reset)
    assert second_href == live_url(blocks_from=250, blocks_since=NOW + 2 * MINUTE_MS)
    seed(
        client,
        blocks=[
            closed_block(block_id, NOW + 2 * MINUTE_MS, SECOND_MS, "denied")
            for block_id in (400, 401)
        ],
    )

    again = page(client, second_href)

    # No new reset: the 50 rows of the new page and the two new ones, older lists kept.
    region = block_ids(section(again, "closed-blocks"))
    assert region == [401, 400, *range(299, 249, -1)]
    assert "closed-blocks-older" not in again
    assert "blocks-more" not in again
    assert live_href(again) == second_href
    # The pager from the new boundary repeats nothing the region holds.
    older = block_ids(page(client, url("blocks?before=250")))
    assert older
    assert not set(older) & set(region)


def test_the_live_agents_keep_their_boundary_and_take_agents_stopped_since(
    client: TestClient,
) -> None:
    stopped = [
        stopped_agent(f"stopped-{index:03d}", NOW - HOUR_MS + index * SECOND_MS)
        for index in range(60)
    ]
    seed(client, [session_row()], agents=[*stopped, agent_row("running-0", NOW - 2 * HOUR_MS)])
    href = live_href(page(client, f"/sessions/{SID}"))
    # The running agent below the boundary is named in the link.
    assert href == live_url(agents_from="stopped-010", keep_agents=["running-0"])
    # It stops with a time before the page's (the stale sweep stops an agent at its last
    # event); a new agent starts.
    change(client, AgentModel, {"agent_id": "running-0"}, status="stopped", stopped_at_ms=NOW - 1)
    seed(client, agents=[agent_row("new-agent", NOW)])

    fragment = page(client, href)

    shown = agent_ids(section(fragment, "agents"))
    assert sorted(shown) == sorted(
        ["new-agent", "running-0", *[f"stopped-{index:03d}" for index in range(10, 60)]]
    )
    assert live_href(fragment) == href


def test_a_live_link_leaves_out_agent_ids_too_long_to_carry(client: TestClient) -> None:
    long_id = "r" * (ui.LIVE_KEEP_ID_MAX + 1)
    fits = "f" * ui.LIVE_KEEP_ID_MAX
    seed(
        client,
        [session_row()],
        agents=[
            agent_row(long_id, NOW - 3 * HOUR_MS),
            agent_row(fits, NOW - 3 * HOUR_MS + 1),
            stopped_agent("stopped-1", NOW - HOUR_MS),
        ],
    )

    href = live_href(page(client, f"/sessions/{SID}"))

    assert href == live_url(agents_from="stopped-1", keep_agents=[fits])
    # The refresh still works, and the running agent left out is shown while it runs.
    assert sorted(agent_ids(section(page(client, href), "agents"))) == sorted(
        [long_id, fits, "stopped-1"]
    )


def test_a_live_agent_window_past_its_cap_starts_over_with_a_new_pager(
    client: TestClient,
) -> None:
    old = [stopped_agent(f"old-{index:03d}", NOW - 2 * HOUR_MS + index) for index in range(60)]
    seed(client, [session_row()], agents=old)
    href = live_href(page(client, f"/sessions/{SID}"))
    assert href == live_url(agents_from="old-010")
    limit = ui.LIVE_CLOSED_LIMIT
    seed(
        client,
        agents=[stopped_agent(f"new-{index:03d}", NOW - HOUR_MS + index) for index in range(limit)],
    )

    fragment = page(client, href)

    newest = [f"new-{index:03d}" for index in range(limit - 50, limit)]
    assert sorted(agent_ids(section(fragment, "agents"))) == newest
    emptied = '<ol id="agents-older" class="agents agents--older" hx-swap-oob="true"></ol>'
    assert emptied in fragment
    assert '<div id="agents-more" class="more" hx-swap-oob="true">' in fragment
    boundary = newest[0]
    assert more_href(fragment, "agents-more") == url(f"agents?before={boundary}")
    assert 'hx-target="#agents-older"' in section(fragment, "agents-more")
    assert live_href(fragment) == live_url(agents_from=boundary)
    assert "closed-blocks-older" not in fragment


@pytest.mark.parametrize(
    "query",
    [
        "",
        "blocks_since=1",
        "agents_since=1",
        "blocks_since=x&agents_since=1",
        "blocks_since=1&agents_since=" + str(2**63),
        "blocks_since=1&agents_since=1&blocks_from=x",
        "blocks_since=1&agents_since=1&blocks_from=",
        "blocks_since=1&agents_since=1&agents_from=%01",
        "blocks_since=1&agents_since=1&agents_from=" + "x" * 300,
        "blocks_since=1&agents_since=1&keep_block=x",
        "blocks_since=1&agents_since=1&keep_agent=%01",
        "blocks_since=1&agents_since=1" + "&keep_block=1" * 9,
        "blocks_since=1&agents_since=1" + "&keep_agent=a" * 9,
    ],
)
def test_an_invalid_live_link_is_a_400_without_echo(client: TestClient, query: str) -> None:
    seed(client, [session_row()])

    body = page(client, url(f"live?{query}"), status=400)

    assert "This page link is not valid" in body
    assert "xxxx" not in body


def test_an_unknown_agent_boundary_still_refreshes(client: TestClient) -> None:
    seed(client, [session_row()], agents=[agent_row("r1", NOW)])

    fragment = page(client, url("live?blocks_since=1&agents_since=1&agents_from=gone-agent"))

    assert agent_ids(section(fragment, "agents")) == ["r1"]


# Reads and errors.


def test_each_request_takes_one_read_slot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(
        client,
        [session_row()],
        blocks=[closed_block(3, NOW, MINUTE_MS, "denied")],
        agents=[agent_row("a1", NOW)],
        events=[event_row(1)],
    )
    calls: list[str] = []
    original = ReadGate.run

    async def run(self: ReadGate, read: Any, *args: Any) -> Any:
        calls.append(getattr(read, "__name__", "read"))
        return await original(self, read, *args)

    monkeypatch.setattr(ReadGate, "run", run)

    for target in (
        f"/sessions/{SID}",
        "/sessions/unknown",
        url("blocks?before=9"),
        url("agents?before=a1"),
        url("events?before=9"),
        url("events?after=0"),
        url("events?after=0", "unknown"),
        url("live?blocks_from=3&blocks_since=1&agents_from=a1&agents_since=1"),
        url("live?blocks_since=1&agents_since=1", "unknown"),
    ):
        calls.clear()
        client.get(target)
        assert len(calls) == 1, target
    for invalid in (
        "/sessions/%01",
        url("events?before=x"),
        url("agents?before=%01"),
        url("live?blocks_since=x&agents_since=1"),
    ):
        calls.clear()
        client.get(invalid)
        assert calls == [], invalid


class FailingGate(ReadGate):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
        raise self.error


BUSY: Final = HTTPException(status_code=503, detail="database busy")
BROKEN: Final = OperationalError("SELECT secret", {}, Exception("disk I/O error"))


@pytest.mark.parametrize(
    ("error", "message"),
    [(BUSY, "The database is busy"), (BROKEN, "The app could not read its database")],
    ids=["busy", "broken"],
)
@pytest.mark.parametrize(
    "target",
    [
        f"/sessions/{SID}",
        url("blocks?before=9"),
        url("agents?before=a1"),
        url("events?before=9"),
        url("events?after=0"),
        url("live?blocks_since=1&agents_since=1"),
    ],
)
def test_a_failed_read_says_what_failed_with_503(
    client: TestClient, error: Exception, message: str, target: str
) -> None:
    app_of(client).state.reads = FailingGate(error)

    body = page(client, target, status=503)

    assert message in body
    assert "secret" not in body
    assert 'class="error-state"' in body
    if target.startswith("/ui/"):
        assert "<html" not in body
    else:
        assert '<header class="masthead">' in body
        # Nothing to refresh: the page does not open a live stream.
        assert "data-stream" not in body


def test_session_pages_are_guarded_and_never_stored(client: TestClient) -> None:
    seed(client, [session_row()])
    for target in (
        f"/sessions/{SID}",
        url("events?after=0"),
        url("live?blocks_since=1&agents_since=1"),
    ):
        response = client.get(target)
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert client.get(target, headers={"host": "evil.example"}).status_code == 400
        foreign = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}
        assert client.get(target, headers=foreign).status_code == 403


# app.js after a "Show older" page.

# A list of rows as plain objects, enough of the DOM for dropRepeats: rows with
# attributes, a list that removes children and a root that finds rows by attribute.
DROP_REPEATS_SCRIPT: Final = """
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const sandbox = {window: {}, document: {addEventListener() {}, readyState: 'loading'}};
new Function('window', 'document', src)(sandbox.window, sandbox.document);
const drop = sandbox.window.ccobsDropRepeats;
const row = (key, value) => ({
  attrs: {[key]: value},
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
  hasAttribute(name) { return name in this.attrs; },
});
const ids = (rows) => rows.map((r) => Object.values(r.attrs)[0]);
function list(rows) {
  return {children: rows, removeChild(r) { this.children = this.children.filter((c) => c !== r); }};
}
function run(elsewhere, before, appended) {
  const target = list([...before, ...appended]);
  const root = {querySelectorAll(selector) {
    const key = selector.slice(1, -1);
    return [...elsewhere, ...target.children].filter((r) => r.hasAttribute(key));
  }};
  const fresh = drop(root, target, before.length);
  return {fresh: ids(fresh), list: ids(target.children)};
}
const b = (id) => row('data-block-id', id);
const a = (id) => row('data-agent-id', id);
console.log(JSON.stringify([
  // A block shown open (another list) comes again, closed, in an older page.
  run([b('5')], [b('90'), b('80')], [b('79'), b('5'), b('78')]),
  // An agent shown running comes again in an older page of the same list.
  run([], [a('run-1'), a('new-1')], [a('old-1'), a('run-1')]),
  // Nothing repeated: every row stays.
  run([], [b('3')], [b('2'), b('1')]),
]));
"""


def test_app_js_drops_rows_an_older_page_repeats() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [node, "-e", DROP_REPEATS_SCRIPT, str(APP_JS)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        {"fresh": ["79", "78"], "list": ["90", "80", "79", "78"]},
        {"fresh": ["old-1"], "list": ["run-1", "new-1", "old-1"]},
        {"fresh": ["2", "1"], "list": ["3", "2", "1"]},
    ]


def test_app_js_follows_up_pager_swaps() -> None:
    script = APP_JS.read_text(encoding="utf-8")

    for event in ("htmx:beforeRequest", "htmx:beforeSwap", "htmx:afterSettle"):
        assert f'addEventListener("{event}"' in script, event


# app.js after a live refresh (ADR 0032): an older list never repeats a row the refreshed
# region holds, and the board keeps its open bays and focus across its swap.

LIVE_SWAPS_SCRIPT: Final = """
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const sandbox = {window: {}, document: {addEventListener() {}, readyState: 'loading'}};
new Function('window', 'document', src)(sandbox.window, sandbox.document);
const w = sandbox.window;
const row = (key, value) => ({
  attrs: {[key]: value},
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
  hasAttribute(name) { return name in this.attrs; },
});
const ids = (rows) => rows.map((r) => Object.values(r.attrs)[0]);
function list(rows) {
  return {
    children: rows,
    removeChild(r) { this.children = this.children.filter((c) => c !== r); },
    querySelectorAll(selector) {
      const key = selector.slice(1, -1);
      return this.children.filter((r) => r.hasAttribute(key));
    },
  };
}
const b = (id) => row('data-block-id', id);
const a = (id) => row('data-agent-id', id);
const lists = {
  'blocks-live': list([b('9'), b('5')]),
  'closed-blocks-older': list([b('4'), b('5'), b('3')]),
  'agents-live': list([a('new'), a('ran')]),
  'agents-older': list([a('ran'), a('old')]),
};
w.ccobsDropOlderRepeats({getElementById: (id) => lists[id] || null});
const noRegion = {'closed-blocks-older': list([b('1')])};
w.ccobsDropOlderRepeats({getElementById: (id) => noRegion[id] || null});

// The board: two collapsible bays, a strip link and a summary that can take the focus.
let focused = null;
function focusable(name, attrs) {
  return Object.assign({
    name, attrs, tagName: name === 'summary' ? 'SUMMARY' : 'A',
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    hasAttribute(k) { return k in this.attrs; },
    focus() { focused = this.name + ':' + (this.attrs.href || this.bay); },
  });
}
function board(openKeys) {
  const bays = ['ended', 'stale'].map((key) => {
    const summary = focusable('summary', {});
    summary.bay = key;
    const details = {
      open: openKeys.includes(key), summary,
      getAttribute(k) { return k === 'data-bay' ? key : null; },
      querySelector(selector) { return selector === 'summary' ? summary : null; },
    };
    summary.parentNode = details;
    return details;
  });
  const links = [focusable('a', {href: '/sessions/s1'}), focusable('a', {href: '/sessions/s2'})];
  const nodes = [...bays.map((d) => d.summary), ...links];
  return {
    bays, links,
    contains(node) { return nodes.includes(node); },
    querySelectorAll(selector) {
      if (selector === 'details[data-bay]') return bays;
      if (selector === 'a[href]') return links;
      return [];
    },
  };
}
const results = [];
const old1 = board(['stale']);
const state1 = w.ccobsBoardState(old1, old1.links[1]);
const new1 = board([]);
w.ccobsRestoreBoard(new1, state1);
results.push({open: new1.bays.map((d) => d.open), focused});
focused = null;
const old2 = board(['ended']);
const new2 = board([]);
w.ccobsRestoreBoard(new2, w.ccobsBoardState(old2, old2.bays[0].summary));
results.push({open: new2.bays.map((d) => d.open), focused});
focused = null;
const old3 = board([]);
const new3 = board([]);
w.ccobsRestoreBoard(new3, w.ccobsBoardState(old3, {tagName: 'BUTTON'}));
results.push({open: new3.bays.map((d) => d.open), focused});
console.log(JSON.stringify({
  blocks: ids(lists['closed-blocks-older'].children),
  agents: ids(lists['agents-older'].children),
  untouched: ids(noRegion['closed-blocks-older'].children),
  boards: results,
}));
"""


def test_app_js_keeps_older_lists_and_the_board_steady_across_live_swaps() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [node, "-e", LIVE_SWAPS_SCRIPT, str(APP_JS)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        # A row the refresh now holds leaves the older list; the rest stay in order.
        "blocks": ["4", "3"],
        "agents": ["old"],
        "untouched": ["1"],
        "boards": [
            # The open bay stays open; the focused strip link keeps the focus.
            {"open": [False, True], "focused": "a:/sessions/s2"},
            {"open": [True, False], "focused": "summary:ended"},
            # Focus outside the board's links and summaries is left alone.
            {"open": [False, False], "focused": None},
        ],
    }


def test_app_js_moves_the_focus_to_the_heading_at_the_end_of_a_list() -> None:
    script = APP_JS.read_text(encoding="utf-8")

    # After the last page with nothing new, the focus goes to the list's heading, and a
    # response that is not swapped forgets the pressed button.
    assert 'closest("section")' in script
    assert 'querySelector("h2")' in script
    assert "pressed = null;" in script


STALE_PAGER_SCRIPT: Final = """
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const triggered = [];
const sandbox = {
  window: {htmx: {trigger(elt, name) { triggered.push(elt.name + ':' + name); }}},
  document: {addEventListener() {}, readyState: 'loading'},
};
new Function('window', 'document', src)(sandbox.window, sandbox.document);
const w = sandbox.window;
function button(name) {
  return {
    name, attrs: {},
    classList: {contains: (cls) => cls === 'more__button'},
    parentNode: {id: 'blocks-more'},
    setAttribute(k, v) { this.attrs[k] = v; },
    hasAttribute(k) { return k in this.attrs; },
    removeAttribute(k) { delete this.attrs[k]; },
  };
}
// In flight when a live reset replaces its control: aborted, once.
const stale = button('stale');
w.ccobsPagerRequest({detail: {elt: stale}});
w.ccobsAbortStalePager({target: stale});
w.ccobsAbortStalePager({target: stale});
// Its own response came first: the swap that replaces it aborts nothing.
const done = button('done');
w.ccobsPagerRequest({detail: {elt: done}});
const response = {requestConfig: {elt: done}, target: {children: []}, shouldSwap: true};
w.ccobsBeforePager({detail: response});
w.ccobsAbortStalePager({target: done});
// Never pressed, or not a pager at all: nothing.
w.ccobsAbortStalePager({target: button('idle')});
w.ccobsAbortStalePager({target: {name: 'row', classList: {contains: () => false}}});
console.log(JSON.stringify(triggered));
"""


def test_app_js_aborts_a_pager_request_whose_control_a_live_reset_replaces() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [node, "-e", STALE_PAGER_SCRIPT, str(APP_JS)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == ["stale:htmx:abort"]


def test_app_js_wires_the_stale_pager_abort_to_htmx_cleanup() -> None:
    script = APP_JS.read_text(encoding="utf-8")
    htmx = (APP_JS.parents[1] / "vendor" / "htmx-2.0.11.min.js").read_text(encoding="utf-8")

    assert 'addEventListener("htmx:beforeCleanupElement", abortStalePager)' in script
    assert 'trigger(element, "htmx:abort")' in script
    # The htmx events this relies on: cleanup before removal, and abort of the XHR.
    assert '"htmx:beforeCleanupElement"' in htmx
    assert 'addEventListener("htmx:abort"' in htmx
