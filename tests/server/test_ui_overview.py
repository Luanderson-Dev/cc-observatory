"""The web UI's overview, its board fragment and the page of all sessions
(``ccobs.server.ui``, ADR 0031).

Sessions are written straight to the database for exact control over their state, and
real hook fixtures go through ``/hooks`` for the shapes Claude Code sends. The pages read
through the read gate, show the project name only (never the full ``cwd``, product
decision P2), escape every stored value, and work under the strict CSP of ADR 0034.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import OperationalError

from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.presentation import RECENT_END_WINDOW_MS, format_duration
from ccobs.server import ui
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.models import AgentModel, BlockModel, SessionModel
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue
from ccobs.server.reads import ReadGate
from ccobs.server.writer import Writer, WriterObservers

HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
STATIC: Final = Path(__file__).parents[2] / "src" / "ccobs" / "server" / "static"
HOST: Final = "test-host"
NOW: Final = 1_800_000_000_000
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
USERNAME: Final = "canaryuser"
HTMX_CONFIG: Final = (
    '<meta name="htmx-config" content=\'{"includeIndicatorStyles":false,"allowEval":false,'
    '"allowScriptTags":false,"selfRequestsOnly":true,"historyCacheSize":0}\'>'
)
# What an XSS payload looks like, raw: none of it may reach the HTML unescaped.
XSS: Final = "<script>\"'&"
RAW_XSS: Final = ("<script>", "\"'&")
# A row to insert: column name to value.
type RowValues = dict[str, object]
SESSION_OPTIONAL: Final = (
    "last_prompt_id",
    "last_error_code",
    "idle_notified_at_ms",
    "ended_at_ms",
    "end_reason",
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
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@contextmanager
def started(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    def manual(
        queue: HookQueue, repository: Repository, observers: WriterObservers
    ) -> ManualWriter:
        return ManualWriter(queue, repository, HOST, stale_after_ms=12 * HOUR_MS, batch_wait_s=0)

    app = create_app(
        settings,
        clock=clock,
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    with started(settings, clock) as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def engine_of(client: TestClient) -> Engine:
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    return repository.engine


def session_row(session_id: str, status: str, last_event_at_ms: int, **values: object) -> RowValues:
    return {
        "session_id": session_id,
        "host": HOST,
        "status": status,
        "started_at_ms": last_event_at_ms - HOUR_MS,
        "last_event_at_ms": last_event_at_ms,
        "last_seq": 1,
        "turn_open": status in ("working", "waiting_user"),
        "compacting": False,
        "cwd": f"/home/{USERNAME}/work/{session_id}-project",
        # Every row has every column: an executemany needs the same keys in each.
        **dict.fromkeys(SESSION_OPTIONAL),
        **values,
    }


def block_row(session_id: str, block_id: int, opened_at_ms: int, **values: object) -> RowValues:
    return {
        "session_id": session_id,
        "host": HOST,
        "block_id": block_id,
        "kind": "permission",
        "scope_agent_id": None,
        "tool_name": "Bash",
        "mcp_server_name": None,
        "opened_at_ms": opened_at_ms,
        **values,
    }


def agent_row(session_id: str, agent_id: str, **values: object) -> RowValues:
    return {
        "session_id": session_id,
        "host": HOST,
        "agent_id": agent_id,
        "status": "running",
        "agent_type": "general-purpose",
        "internal": False,
        "background": False,
        "started_at_ms": NOW - MINUTE_MS,
        "last_event_at_ms": NOW - MINUTE_MS,
        "stopped_at_ms": None,
        **values,
    }


def seed(
    client: TestClient,
    sessions: Sequence[RowValues],
    blocks: Sequence[RowValues] = (),
    agents: Sequence[RowValues] = (),
) -> None:
    with engine_of(client).begin() as connection:
        connection.execute(insert(SessionModel), list(sessions))
        if blocks:
            connection.execute(insert(BlockModel), list(blocks))
        if agents:
            connection.execute(insert(AgentModel), list(agents))


def page(
    client: TestClient, url: str, *, status: int = 200, headers: dict[str, str] | None = None
) -> str:
    response = client.get(url, headers=headers)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    return response.text


def strip_ids(body: str) -> list[str]:
    """The session of each strip, in page order, from its link."""
    return [html.unescape(found) for found in re.findall(r'href="/sessions/([^"?]+)"', body)]


def board_section(body: str, heading: str) -> str:
    """The HTML of one bay, from its heading to the next bay or the end of the board."""
    start = body.index(f">{heading}")
    following = [body.find("<h2", start), body.find("</section>", start)]
    end = min(position for position in following if position != -1)
    return body[start:end]


# The board.


def board_sessions() -> tuple[list[RowValues], list[RowValues], list[RowValues]]:
    sessions = [
        session_row("wait-short", "waiting_user", NOW - MINUTE_MS),
        session_row("wait-long", "waiting_user", NOW - 2 * MINUTE_MS),
        session_row("working-1", "working", NOW - 3 * MINUTE_MS),
        session_row("idle-1", "idle", NOW - 4 * MINUTE_MS, last_prompt_id="p1"),
        session_row(
            "ended-1",
            "ended",
            NOW - HOUR_MS,
            ended_at_ms=NOW - HOUR_MS,
            end_reason="prompt_input_exit",
        ),
        session_row("stale-1", "stale", NOW - 13 * HOUR_MS),
    ]
    blocks = [
        # The short wait's session was active more recently, but its block is newer.
        block_row("wait-short", 10, NOW - MINUTE_MS),
        block_row("wait-long", 11, NOW - 10 * MINUTE_MS, tool_name="Edit"),
        # A second, newer open block: the wait counts from the oldest one.
        block_row("wait-long", 12, NOW - 5 * MINUTE_MS),
    ]
    agents = [
        agent_row("working-1", "a1"),
        agent_row("working-1", "a2"),
        # Internal and stopped agents do not count.
        agent_row("working-1", "a3", internal=True),
        agent_row("working-1", "a4", status="stopped", stopped_at_ms=NOW),
    ]
    return sessions, blocks, agents


def test_the_board_stacks_the_bays_in_order_with_the_longest_wait_first(
    client: TestClient,
) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    headings = [
        "Waiting on you",
        "Working",
        "Idle",
        "Ended in the last 24 hours",
        "Stale",
    ]
    positions = [body.index(f">{heading}") for heading in headings]
    assert positions == sorted(positions)
    assert strip_ids(body) == [
        "wait-long",
        "wait-short",
        "working-1",
        "idle-1",
        "ended-1",
        "stale-1",
    ]


def test_ended_and_stale_bays_are_collapsed(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    details = re.findall(r"<details\b[^>]*>(.*?)</details>", body, re.DOTALL)
    assert len(details) == 2
    assert "Ended in the last 24 hours" in details[0]
    assert "Stale" in details[1]
    assert not re.search(r"<details\b[^>]*\bopen\b", body)
    for live in ("Waiting on you", "Working", "Idle"):
        assert all(live not in found for found in details)


def test_each_bay_heading_shows_its_count(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    assert re.search(r">Waiting on you\s*<span class=\"bay__count\">2</span>", body)
    assert re.search(r">Working\s*<span class=\"bay__count\">1</span>", body)


def test_a_strip_says_what_the_session_waits_on_or_does(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    # The oldest open block of the long wait.
    assert "Permission: Edit" in board_section(body, "Waiting on you")
    assert "2 subagents running" in board_section(body, "Working")
    assert "Turn finished" in board_section(body, "Idle")
    assert "Exited" in board_section(body, "Ended in the last 24 hours")
    assert "No recent events" in board_section(body, "Stale")
    # The state in words on every strip, not only by colour.
    assert body.count('class="strip__state"') == len(strip_ids(body))


def test_a_waiting_strip_shows_the_wait_since_its_oldest_open_block(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    since = NOW - 10 * MINUTE_MS
    iso = datetime.fromtimestamp(since / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (
        f'<time class="strip__wait" datetime="{iso}" data-since-ms="{since}">'
        f"{format_duration(10 * MINUTE_MS)}</time>"
    ) in body


def test_other_strips_show_their_last_activity(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    working = board_section(body, "Working")
    since = NOW - 3 * MINUTE_MS
    assert f'data-since-ms="{since}">{format_duration(3 * MINUTE_MS)}</time>' in working
    ended = board_section(body, "Ended in the last 24 hours")
    assert f'data-since-ms="{NOW - HOUR_MS}">{format_duration(HOUR_MS)}</time>' in ended


def test_the_title_counts_the_sessions_waiting_on_you(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    assert "<title>(2) cc-observatory</title>" in body
    assert 'data-waiting="2"' in body


def test_without_waiting_sessions_the_title_has_no_count(client: TestClient) -> None:
    seed(client, [session_row("working-1", "working", NOW)])

    body = page(client, "/")

    assert "<title>cc-observatory</title>" in body
    assert "Nothing is waiting on you." in body


def test_the_error_code_and_subagents_of_a_waiting_session_show(client: TestClient) -> None:
    seed(
        client,
        [session_row("w", "waiting_user", NOW, last_error_code="rate_limit")],
        [block_row("w", 1, NOW - MINUTE_MS, kind="elicitation", mcp_server_name="tracker")],
        [agent_row("w", "a1")],
    )

    body = page(client, "/")

    assert "Question from tracker" in body
    assert "1 subagent running" in body
    assert "rate_limit" in body


def strip_of(body: str, session_id: str) -> str:
    start = body.index(f'href="/sessions/{session_id}"')
    return body[start : body.index("</li>", start)]


@pytest.mark.parametrize("end_reason", [None, "other", "something_new"])
def test_an_ended_strip_does_not_repeat_its_state(
    client: TestClient, end_reason: str | None
) -> None:
    seed(client, [session_row("e", "ended", NOW, ended_at_ms=NOW, end_reason=end_reason)])

    strip = strip_of(page(client, "/"), "e")

    assert '<span class="strip__state">Ended</span>' in strip
    assert "strip__doing" not in strip


def test_other_strips_keep_their_activity_next_to_the_state(client: TestClient) -> None:
    seed(
        client,
        [
            session_row("e", "ended", NOW, ended_at_ms=NOW, end_reason="clear"),
            session_row("s", "stale", NOW - 13 * HOUR_MS),
            session_row("w", "waiting_user", NOW),
        ],
        [block_row("w", 1, NOW)],
    )

    body = page(client, "/")

    assert '<span class="strip__doing">Cleared</span>' in strip_of(body, "e")
    assert '<span class="strip__doing">No recent events</span>' in strip_of(body, "s")
    assert '<span class="strip__doing">Permission: Bash</span>' in strip_of(body, "w")


def test_a_waiting_session_without_an_open_block_waits_since_its_last_event(
    client: TestClient,
) -> None:
    seed(client, [session_row("w", "waiting_user", NOW - 2 * MINUTE_MS)])

    body = page(client, "/")

    assert f'data-since-ms="{NOW - 2 * MINUTE_MS}">2m 00s</time>' in body


def test_ended_sessions_older_than_a_day_leave_the_board(client: TestClient) -> None:
    old_end = NOW - RECENT_END_WINDOW_MS - 1
    seed(
        client,
        [
            session_row("recent", "ended", NOW, ended_at_ms=NOW - RECENT_END_WINDOW_MS),
            session_row("old", "ended", old_end, ended_at_ms=old_end),
        ],
    )

    assert strip_ids(page(client, "/")) == ["recent"]
    assert set(strip_ids(page(client, "/sessions"))) == {"recent", "old"}


def test_with_only_old_sessions_the_board_points_to_all_sessions(client: TestClient) -> None:
    old_end = NOW - 2 * RECENT_END_WINDOW_MS
    seed(client, [session_row("old", "ended", old_end, ended_at_ms=old_end)])

    body = page(client, "/")

    assert strip_ids(body) == []
    assert "No sessions yet" not in body
    assert 'href="/sessions"' in body


def test_the_closed_bays_are_capped_with_a_link_to_all_sessions(client: TestClient) -> None:
    extra = 3
    stale = [
        session_row(f"stale-{index:03d}", "stale", NOW - 13 * HOUR_MS - index)
        for index in range(ui.CLOSED_BAY_LIMIT + extra)
    ]
    ended = [
        session_row(f"ended-{index:03d}", "ended", NOW - index, ended_at_ms=NOW - index)
        for index in range(ui.CLOSED_BAY_LIMIT + 1)
    ]
    seed(client, stale + ended)

    body = page(client, "/")

    stale_bay = board_section(body, "Stale")
    assert len(strip_ids(stale_bay)) == ui.CLOSED_BAY_LIMIT
    assert f'<span class="bay__count">{ui.CLOSED_BAY_LIMIT + extra}</span>' in stale_bay
    assert f"{extra} more in all sessions" in stale_bay
    # The latest activity first, so the oldest are the ones left out.
    assert strip_ids(stale_bay)[0] == "stale-000"
    ended_bay = board_section(body, "Ended in the last 24 hours")
    assert len(strip_ids(ended_bay)) == ui.CLOSED_BAY_LIMIT
    assert "1 more in all sessions" in ended_bay


# The board fragment.


def test_the_board_fragment_is_the_board_alone(client: TestClient) -> None:
    seed(client, *board_sessions())

    fragment = page(client, "/ui/board")
    full = page(client, "/")

    assert fragment.lstrip().startswith('<section id="board"')
    assert "<html" not in fragment
    assert "<head" not in fragment
    assert strip_ids(fragment) == strip_ids(full)
    assert 'data-waiting="2"' in fragment


# Live updates (ADR 0032): the board refetches itself when live.js says sessions changed.

BOARD_TRIGGER: Final = (
    'hx-get="/ui/board" hx-trigger="ccobs:changed from:body" hx-swap="outerHTML" '
    'hx-sync="this:replace"'
)


def board_tag(body: str) -> str:
    found = re.search(r'<section id="board"[^>]*>', body)
    assert found is not None
    return found.group(0)


def test_the_board_refetches_itself_when_sessions_change(client: TestClient) -> None:
    seed(client, *board_sessions())

    # The fragment carries the trigger too, so the board keeps listening after a swap.
    for url in ("/", "/ui/board"):
        assert BOARD_TRIGGER in board_tag(page(client, url)), url


def test_an_empty_or_failed_board_keeps_listening(client: TestClient) -> None:
    assert BOARD_TRIGGER in board_tag(page(client, "/"))
    app_of(client).state.reads = FailingGate(BROKEN)
    for url in ("/", "/ui/board"):
        assert BOARD_TRIGGER in board_tag(page(client, url, status=503)), url


def test_the_overview_opens_the_live_stream(client: TestClient) -> None:
    body = page(client, "/")

    expected = '<div id="live-status" class="live-status" role="status" data-stream="/ui/stream">'
    assert expected + "</div>" in body


def test_all_sessions_opens_no_live_stream(client: TestClient) -> None:
    body = page(client, "/sessions")

    assert '<div id="live-status" class="live-status" role="status"></div>' in body
    assert "data-stream" not in body


def test_collapsed_bays_name_themselves_for_app_js(client: TestClient) -> None:
    seed(client, *board_sessions())

    body = page(client, "/")

    assert re.search(r'<details class="bay bay--ended" data-bay="ended">', body)
    assert re.search(r'<details class="bay bay--stale" data-bay="stale">', body)


def test_each_request_takes_one_read_slot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(client, *board_sessions())
    calls: list[str] = []
    original = ReadGate.run

    async def run(self: ReadGate, read: Any, *args: Any) -> Any:
        calls.append(getattr(read, "__name__", "read"))
        return await original(self, read, *args)

    monkeypatch.setattr(ReadGate, "run", run)

    for url in ("/", "/ui/board", "/sessions", "/sessions?before=1-x"):
        calls.clear()
        client.get(url)
        assert len(calls) == 1, url


# Privacy: the project name, never the full cwd (P2).


@pytest.mark.parametrize(
    ("cwd", "project"),
    [
        (f"C:\\Users\\{USERNAME}\\work\\my-app", "my-app"),
        (f"/home/{USERNAME}/src/api-server", "api-server"),
        (f"/Users/{USERNAME}/docs-site/", "docs-site"),
    ],
)
def test_strips_show_the_project_name_and_never_the_cwd(
    client: TestClient, cwd: str, project: str
) -> None:
    old_end = NOW - 2 * RECENT_END_WINDOW_MS
    seed(
        client,
        [
            session_row("live", "working", NOW, cwd=cwd),
            session_row("old", "ended", old_end, ended_at_ms=old_end, cwd=cwd),
        ],
    )

    for url in ("/", "/ui/board", "/sessions"):
        body = page(client, url)
        assert f'<a class="strip__project" href="/sessions/live">{project}</a>' in body
        assert USERNAME not in body
        assert html.escape(cwd) not in body
        assert cwd not in body


def test_a_session_without_cwd_has_a_placeholder(client: TestClient) -> None:
    seed(client, [session_row("live", "working", NOW, cwd=None)])

    body = page(client, "/")

    assert "Unknown project" in body


def test_the_replayed_fixtures_show_their_project_names_only(
    client: TestClient, clock: Clock
) -> None:
    payloads: list[dict[str, Any]] = []
    for path in sorted(HOOK_FIXTURES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            payloads.append(json.loads(line)["payload"])
    for document in payloads:
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    while writer.run_once(0.0):
        pass

    body = page(client, "/sessions")

    cwds = {document["cwd"] for document in payloads if document.get("cwd")}
    assert cwds
    for cwd in cwds:
        assert cwd not in body
        assert html.escape(cwd) not in body
        assert re.split(r"[\\/]", cwd)[-1] in body
    with engine_of(client).connect() as connection:
        stored = connection.execute(select(SessionModel.session_id)).scalars().all()
    assert sorted(strip_ids(body)) == sorted(stored)


# Escaping.


def assert_escaped(body: str) -> None:
    for raw in RAW_XSS:
        assert raw not in body


def test_stored_values_are_escaped_everywhere(client: TestClient) -> None:
    session_id = f"s{XSS}"
    seed(
        client,
        [
            session_row(
                session_id,
                "waiting_user",
                NOW,
                cwd=f"/srv/{XSS}",
                last_error_code=XSS,
                end_reason=XSS,
            ),
            session_row(
                "ended", "ended", NOW, ended_at_ms=NOW, end_reason=XSS, last_error_code=XSS
            ),
            session_row("elicit", "waiting_user", NOW, cwd=f"/srv/{XSS}"),
        ],
        [
            block_row(session_id, 1, NOW - MINUTE_MS, tool_name=XSS),
            block_row("elicit", 2, NOW, kind="elicitation", tool_name=None, mcp_server_name=XSS),
        ],
        [agent_row(session_id, f"a{XSS}", agent_type=XSS)],
    )

    for url in ("/", "/ui/board", "/sessions"):
        body = page(client, url)
        assert_escaped(body)
        assert "&lt;script&gt;&#34;&#39;&amp;" in body
        # The link quotes the ID as a path segment.
        assert 'href="/sessions/s%3Cscript%3E%22%27%26"' in body
        assert "Permission: &lt;script&gt;" in body
        assert "Question from &lt;script&gt;" in body
    assert "<title>(2) cc-observatory</title>" in page(client, "/")


# The empty state (P8).


# What the strict CSP (ADR 0034) refuses in markup; test_static.py scans the templates,
# these scan the rendered empty state.
CSP_REFUSED: Final = (
    re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE),
    re.compile(r"<style\b", re.IGNORECASE),
    re.compile(r"\sstyle\s*=", re.IGNORECASE),
    re.compile(r"\son[a-z]+\s*=", re.IGNORECASE),
    re.compile(r"\shx-on", re.IGNORECASE),
)


def empty_state(body: str) -> str:
    found = re.search(r'<div class="empty-state">.*?</details>\s*</div>', body, re.DOTALL)
    assert found is not None
    return found.group(0)


def manual_path(body: str) -> str:
    found = re.search(r"<details\b[^>]*>(.*?)</details>", empty_state(body), re.DOTALL)
    assert found is not None
    return found.group(1)


def setup_command(body: str) -> str:
    """The command as the Copy button copies it (the code element's textContent)."""
    found = re.search(r'<code id="setup-command">(.*?)</code>', empty_state(body), re.DOTALL)
    assert found is not None
    return html.unescape(re.sub(r"<[^>]+>", "", found.group(1)))


def test_each_word_of_the_setup_command_stays_whole_when_it_wraps(client: TestClient) -> None:
    # On a phone the command wraps; a word such as --app-port must not split at its
    # hyphens, so each word is a nowrap span and the spaces between them are the breaks.
    body = page(client, "/", headers={"host": "localhost:4555"})

    found = re.search(r'<code id="setup-command">(.*?)</code>', empty_state(body), re.DOTALL)
    assert found is not None
    assert found.group(1) == " ".join(
        f'<span class="command-word">{word}</span>'
        for word in ("uvx", "cc-observatory", "setup", "--app-port", "4555")
    )


def hook_json(body: str) -> Any:
    """The hook settings, which only the collapsed manual path shows."""
    found = re.search(
        r'<pre class="hook-settings"><code id="hook-settings">(.*?)</code></pre>',
        manual_path(body),
        re.DOTALL,
    )
    assert found is not None
    return json.loads(html.unescape(found.group(1)))


@pytest.mark.parametrize("url", ["/", "/ui/board", "/sessions"])
def test_an_empty_page_leads_with_ccobs_setup(client: TestClient, url: str) -> None:
    body = page(client, url)
    state = empty_state(body)

    assert "<h2>No sessions yet</h2>" in state
    assert setup_command(body) == "uvx cc-observatory setup"
    assert "ccobs doctor" in state
    # The command comes first; the manual path is collapsed after it.
    assert state.index('id="setup-command"') < state.index("<details")
    assert "<details open" not in state
    assert re.search(r"<summary[^>]*>Connect one session without changing your settings<", state)


@pytest.mark.parametrize(
    ("host", "command"),
    [
        ("localhost:4300", "uvx cc-observatory setup"),
        # No port in the Host header means http's 80, as the manual hook JSON says.
        ("localhost", "uvx cc-observatory setup --app-port 80"),
        ("localhost:4555", "uvx cc-observatory setup --app-port 4555"),
        ("127.0.0.1:5300", "uvx cc-observatory setup --app-port 5300"),
    ],
)
def test_the_setup_command_names_the_request_port_unless_it_is_the_default(
    client: TestClient, host: str, command: str
) -> None:
    for url in ("/", "/sessions"):
        assert setup_command(page(client, url, headers={"host": host})) == command


def test_the_manual_path_explains_claude_settings(client: TestClient) -> None:
    manual = manual_path(page(client, "/"))

    assert "claude --settings" in manual
    assert "docs/app.md" in manual


def test_both_copy_buttons_target_an_element_of_the_empty_state(client: TestClient) -> None:
    state = empty_state(page(client, "/ui/board"))

    targets = re.findall(
        r'<button type="button" class="copy-button" data-copy-target="([^"]+)"', state
    )
    assert targets == ["setup-command", "hook-settings"]
    for target in targets:
        assert state.count(f'id="{target}"') == 1


@pytest.mark.parametrize("scan", CSP_REFUSED, ids=lambda scan: scan.pattern)
def test_the_empty_state_holds_nothing_the_csp_refuses(
    client: TestClient, scan: re.Pattern[str]
) -> None:
    for url in ("/ui/board", "/sessions"):
        assert not scan.search(empty_state(page(client, url)))


def test_the_csp_scans_of_the_empty_state_catch_what_they_must() -> None:
    caught = [
        "<script>x</script>",
        "<style>",
        '<p style="x">',
        '<p onclick="x">',
        '<a hx-on:click="x">',
    ]
    for scan, markup in zip(CSP_REFUSED, caught, strict=True):
        assert scan.search(markup)
    assert not any(scan.search('<script src="/static/js/app.js" defer>') for scan in CSP_REFUSED)


def test_the_empty_board_shows_the_hook_settings_for_the_request_port(
    client: TestClient,
) -> None:
    body = page(client, "/")

    assert hook_json(body) == hook_settings(f"http://127.0.0.1:4300{HOOK_PATH}", windows=False)
    assert hook_json(page(client, "/", headers={"host": "localhost:4555"})) == hook_settings(
        f"http://127.0.0.1:4555{HOOK_PATH}", windows=False
    )
    assert hook_json(page(client, "/", headers={"host": "localhost"})) == hook_settings(
        f"http://127.0.0.1{HOOK_PATH}", windows=False
    )


def test_the_empty_board_uses_the_windows_null_device_for_a_windows_browser(
    client: TestClient,
) -> None:
    agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    body = page(client, "/ui/board", headers={"user-agent": agent})

    assert hook_json(body) == hook_settings(f"http://127.0.0.1:4300{HOOK_PATH}", windows=True)


# Errors.


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
@pytest.mark.parametrize("url", ["/", "/ui/board", "/sessions"])
def test_a_failed_read_says_what_failed_with_503(
    client: TestClient, error: Exception, message: str, url: str
) -> None:
    app_of(client).state.reads = FailingGate(error)

    body = page(client, url, status=503)

    assert message in body
    assert "secret" not in body
    assert "Traceback" not in body
    assert 'class="error-state"' in body
    if url == "/ui/board":
        assert "<html" not in body
    else:
        assert '<header class="masthead">' in body


def test_another_http_error_from_the_gate_is_not_swallowed(client: TestClient) -> None:
    app_of(client).state.reads = FailingGate(HTTPException(status_code=418))

    assert client.get("/").status_code == 418


# All sessions.


def test_all_sessions_pages_newest_activity_first(client: TestClient) -> None:
    total = 2 * ui.SESSIONS_PAGE_SIZE + 7
    # Pairs share a last event time, so the session ID breaks ties.
    sessions = [
        session_row(f"s{index:04d}", "ended", NOW - index // 2, ended_at_ms=NOW - index // 2)
        for index in range(total)
    ]
    seed(client, sessions)
    # Keyset order: the latest activity first, then the session ID, descending too.
    newest_first = sorted(
        sessions, key=lambda row: (row["last_event_at_ms"], row["session_id"]), reverse=True
    )
    expected = [row["session_id"] for row in newest_first]

    seen: list[str] = []
    url: str | None = "/sessions"
    pages = 0
    while url is not None:
        body = page(client, url)
        ids = strip_ids(body)
        assert len(ids) <= ui.SESSIONS_PAGE_SIZE
        seen += ids
        pages += 1
        found = re.search(r'<a class="pager__next" href="([^"]+)"', body)
        url = html.unescape(found.group(1)) if found else None

    assert pages == 3
    assert seen == expected
    assert len(set(seen)) == total


def test_all_sessions_with_none_stored_points_to_the_setup(client: TestClient) -> None:
    body = page(client, "/sessions")

    assert "No sessions yet" in body
    assert "pager__next" not in body


@pytest.mark.parametrize(
    "before",
    [
        "x",
        "1",
        "-x",
        "12a-x",
        # 2**63, one past SQLite's largest integer, and a 20-digit number.
        f"{2**63}-x",
        "99999999999999999999-x",
        "1-\x01",
        "1-" + "x" * 300,
    ],
)
def test_an_invalid_cursor_is_a_400_without_echo(client: TestClient, before: str) -> None:
    response = client.get("/sessions", params={"before": before})

    assert response.status_code == 400
    assert "This page link is not valid" in response.text
    if len(before) > 2:
        assert before not in response.text


def test_a_cursor_of_a_deleted_session_still_pages(client: TestClient) -> None:
    seed(client, [session_row("b", "ended", NOW - 5, ended_at_ms=NOW - 5)])

    body = page(client, f"/sessions?before={NOW}-zzz")

    assert strip_ids(body) == ["b"]
    assert 'class="pager__first" href="/sessions"' in body
    assert strip_ids(page(client, f"/sessions?before={2**63 - 1}-x")) == ["b"]
    # Past the oldest session: an empty page, not the setup.
    older = page(client, f"/sessions?before={NOW - 5}-b")
    assert "No older sessions." in older
    assert "No sessions yet" not in older


# The page shell.


def test_the_page_configures_htmx_for_the_csp(client: TestClient) -> None:
    body = page(client, "/")

    assert HTMX_CONFIG in body
    assert '<html lang="en">' in body


def test_the_scripts_load_in_order_without_inline_code(client: TestClient) -> None:
    body = page(client, "/")
    head = body.split("</head>", 1)[0]

    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", body, re.DOTALL)
    assert [content for _, content in scripts] == ["", "", "", ""]
    sources = [re.search(r'src="([^"]+)"', attributes) for attributes, _ in scripts]
    assert [found.group(1) if found else None for found in sources] == [
        "/static/js/theme.js",
        "/static/vendor/htmx-2.0.11.min.js",
        "/static/js/app.js",
        "/static/js/live.js",
    ]
    # theme.js runs before the first paint; the others after parsing.
    assert "defer" not in scripts[0][0]
    assert all("defer" in attributes for attributes, _ in scripts[1:])
    assert '<script src="/static/js/theme.js"></script>' in head
    assert head.index('href="/static/css/app.css"') < head.index("theme.js")


def test_the_theme_toggle_offers_three_choices(client: TestClient) -> None:
    body = page(client, "/")

    toggle = re.search(r'<fieldset class="theme-toggle"[^>]*>(.*?)</fieldset>', body, re.DOTALL)
    assert toggle is not None
    values = re.findall(r'<input type="radio" name="theme" value="(\w+)"', toggle.group(1))
    assert values == ["system", "light", "dark"]
    assert "<legend" in toggle.group(1)


def test_pages_are_never_stored_by_the_browser(client: TestClient) -> None:
    for url in ("/", "/ui/board", "/sessions"):
        response = client.get(url)
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]


def test_the_ui_routes_are_behind_the_host_guard_and_fetch_metadata(client: TestClient) -> None:
    for url in ("/", "/ui/board", "/sessions", "/static/css/app.css"):
        assert client.get(url, headers={"host": "evil.example"}).status_code == 400
        foreign = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}
        assert client.get(url, headers=foreign).status_code == 403
    navigation = {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate"}
    assert client.get("/", headers=navigation).status_code == 200


# Static files.


def test_static_files_revalidate_with_their_etag(client: TestClient) -> None:
    for url in ("/static/css/app.css", "/static/js/app.js", "/static/js/theme.js"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        etag = response.headers["etag"]

        again = client.get(url, headers={"if-none-match": etag})
        assert again.status_code == 304
        assert again.headers["cache-control"] == "no-cache"


def test_versioned_vendor_files_are_cached_for_good(client: TestClient) -> None:
    response = client.get("/static/vendor/htmx-2.0.11.min.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"


@pytest.mark.parametrize(
    ("url", "content_type"),
    [
        ("/static/js/app.js", "text/javascript; charset=utf-8"),
        ("/static/js/theme.js", "text/javascript; charset=utf-8"),
        ("/static/css/app.css", "text/css; charset=utf-8"),
        ("/static/fonts/atkinson-hyperlegible-next-wght.woff2", "font/woff2"),
        ("/static/img/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_files_have_their_content_types(
    client: TestClient, url: str, content_type: str
) -> None:
    # nosniff makes the browser trust these, whatever the OS's own MIME table says.
    assert client.get(url).headers["content-type"] == content_type


def test_static_serves_no_directory_listing(client: TestClient) -> None:
    for url in ("/static/", "/static/css", "/static/../app.py", "/static/missing.css"):
        assert client.get(url).status_code == 404


# The browser's duration format matches the server's.

DURATIONS_MS: Final = (
    -5_000,
    0,
    999,
    1_000,
    42_000,
    59_999,
    60_000,
    245_000,
    3_599_999,
    3_600_000,
    3_780_000,
    86_399_000,
    86_400_000,
    187_200_000,
    90 * 86_400_000,
)


def test_app_js_formats_durations_like_the_server() -> None:
    node = shutil.which("node")
    if node is None:
        # CI runners have node: there a missing node is a failure, not a skip.
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    script = (
        "const src = require('fs').readFileSync(process.argv[1], 'utf8');"
        "const sandbox = {window: {}, document: {addEventListener() {}, readyState: 'loading'}};"
        "new Function('window', 'document', src)(sandbox.window, sandbox.document);"
        "const f = sandbox.window.ccobsFormatDuration;"
        "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(f)));"
    )
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [node, "-e", script, str(STATIC / "js" / "app.js"), json.dumps(DURATIONS_MS)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [format_duration(ms) for ms in DURATIONS_MS]
