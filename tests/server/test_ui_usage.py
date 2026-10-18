"""The session page's "Tokens and cost" section (``/ui/sessions/{id}/usage``, ADR 0035).

The page loads the section with htmx on ``load`` and every 60 s, never on live updates.
The fragment reads the session once through the read gate, then asks Prometheus (an
``httpx.MockTransport`` here) for its totals with no read slot held, and always answers
200 for the states it swaps in: values, empty, Prometheus unavailable, values it can't
read, or a database it could not read. Model names from Prometheus are escaped.
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from sqlalchemy import insert
from sqlalchemy.exc import OperationalError

from ccobs.domain.app_metrics import PROMETHEUS_QUERIES
from ccobs.domain.grafana_links import WINDOW_AFTER_MS
from ccobs.domain.usage import WINDOW_BEFORE_MS
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.models import SessionModel
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue
from ccobs.server.reads import ReadGate
from ccobs.server.writer import Writer, WriterObservers

HOST: Final = "test-host"
NOW: Final = 1_800_000_000_000
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS
GOOD_HOST: Final = "127.0.0.1:4300"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
XSS: Final = "<script>\"'&"
ESCAPED_XSS: Final = "&lt;script&gt;&#34;&#39;&amp;"
UNAVAILABLE: Final = "Prometheus is unavailable, so tokens and cost can't be shown right now."
UNREADABLE: Final = (
    "Prometheus answered with values the app can't read, so tokens and cost can't be shown"
    " right now."
)
EMPTY: Final = "No usage recorded for this session yet."


def vector(label: str, items: dict[str, str]) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {label: name}, "value": [1_800_000_000.0, value]}
                for name, value in items.items()
            ],
        },
    }


class Prometheus:
    """A fake Prometheus behind an ``httpx.MockTransport``; ``respond`` builds each
    answer, by the query's kind."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_by_type = {"input": "1200", "output": "345", "cacheRead": "100000"}
        self.tokens_by_model = {"claude-haiku-5-5": "101545"}
        self.cost_by_model = {"claude-haiku-5-5": "0.0123"}
        self.status = 200
        self.on_request: Callable[[], None] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request()
        if self.status != 200:
            return httpx.Response(self.status)
        query = request.url.params["query"]
        if query.startswith("sum by (type)"):
            body = vector("type", self.tokens_by_type)
        elif "cost" in query:
            body = vector("model", self.cost_by_model)
        else:
            body = vector("model", self.tokens_by_model)
        return httpx.Response(200, json=body)


class ManualWriter(Writer):
    def start(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def prometheus() -> Prometheus:
    return Prometheus()


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prometheus: Prometheus
) -> Iterator[TestClient]:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    settings = Settings(data_dir=tmp_path / "data", host_label=HOST)

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
        prometheus_transport=httpx.MockTransport(prometheus.handle),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def seed(client: TestClient, status: str = "working", **values: object) -> None:
    row = {
        "session_id": SID,
        "host": HOST,
        "status": status,
        "started_at_ms": NOW - HOUR_MS,
        "last_event_at_ms": NOW - MINUTE_MS,
        "last_seq": 1,
        "turn_open": status in ("working", "waiting_user"),
        "compacting": False,
        "cwd": "/work/api-server",
        "last_prompt_id": None,
        "last_error_code": None,
        "idle_notified_at_ms": None,
        "ended_at_ms": None,
        "end_reason": None,
        **values,
    }
    repository = app_of(client).state.repository
    assert isinstance(repository, Repository)
    with repository.engine.begin() as connection:
        connection.execute(insert(SessionModel), [row])


def usage_url(session_id: str = SID) -> str:
    return f"/ui/sessions/{session_id}/usage"


def fragment(client: TestClient, session_id: str = SID, status: int = 200) -> str:
    response = client.get(usage_url(session_id))
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "<html" not in response.text
    return response.text


def text_of(body: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def counted(client: TestClient) -> dict[str, float]:
    exposition = generate_latest(app_of(client).state.metrics.registry).decode()
    found = re.findall(
        rf'^{PROMETHEUS_QUERIES.name}{{host="{HOST}",result="(\w+)"}} (\S+)$', exposition, re.M
    )
    return {result: float(value) for result, value in found}


# The section on the session page.


def usage_element(body: str) -> str:
    found = re.search(r'<div\b[^>]*\bid="usage"[^>]*>', body)
    assert found is not None
    return found.group(0)


def test_the_session_page_loads_the_section_on_load_and_every_minute(
    client: TestClient, prometheus: Prometheus
) -> None:
    seed(client)

    body = client.get(f"/sessions/{SID}").text

    element = usage_element(body)
    assert f'hx-get="{usage_url()}"' in element
    assert 'hx-trigger="load, every 60s"' in element
    # Not driven by live updates.
    assert "ccobs:changed" not in element
    assert "Tokens and cost" in text_of(body)
    assert "Loading tokens and cost" in text_of(body)
    assert "can lag up to a minute" in text_of(body)
    # The page itself asks Prometheus nothing: htmx does, once loaded.
    assert prometheus.requests == []


def test_the_section_stays_out_of_the_live_refresh(client: TestClient) -> None:
    seed(client)

    live = client.get(f"/ui/sessions/{SID}/live?blocks_since=1&agents_since=1").text

    assert 'id="usage"' not in live


def test_the_grafana_link_to_tokens_and_cost_stays(client: TestClient) -> None:
    seed(client)

    body = client.get(f"/sessions/{SID}").text

    nav = re.search(r'<nav id="grafana-links".*?</nav>', body, re.DOTALL)
    assert nav is not None
    assert ">Tokens and cost</a>" in nav.group(0)


# The fragment's states.


def test_the_fragment_shows_the_totals_by_type_and_model(client: TestClient) -> None:
    seed(client)

    body = fragment(client)

    text = text_of(body)
    assert "Total cost $0.0123" in text
    assert "Tokens 101,545" in text
    assert re.search(r"Input 1,200 Output 345 Cache read 100,000", text)
    assert "claude-haiku-5-5 101,545 $0.0123" in text
    assert "<code>claude-haiku-5-5</code>" in body


def test_the_fragment_escapes_model_and_type_names(
    client: TestClient, prometheus: Prometheus
) -> None:
    seed(client)
    prometheus.tokens_by_type = {XSS: "1"}
    prometheus.tokens_by_model = {XSS: "1"}
    prometheus.cost_by_model = {XSS: "0.5"}

    body = fragment(client)

    assert "<script>" not in body
    assert body.count(ESCAPED_XSS) == 2


def test_a_session_with_no_series_says_so(client: TestClient, prometheus: Prometheus) -> None:
    seed(client)
    prometheus.tokens_by_type = {}
    prometheus.tokens_by_model = {}
    prometheus.cost_by_model = {}

    assert EMPTY in text_of(fragment(client))


@pytest.mark.parametrize("status", [500, 503, 400])
def test_an_unavailable_prometheus_is_a_message_with_200(
    client: TestClient, prometheus: Prometheus, status: int
) -> None:
    # htmx 2 swaps only 2xx answers by default: a 200 replaces the loading text.
    seed(client)
    prometheus.status = status

    body = fragment(client)

    assert UNAVAILABLE in text_of(body)
    assert 'class="usage__error"' in body
    assert counted(client)["error"] == 3


def test_unreadable_values_are_a_message_with_200(
    client: TestClient, prometheus: Prometheus
) -> None:
    seed(client)
    prometheus.cost_by_model = {"m": "NaN"}

    body = fragment(client)

    assert UNREADABLE in text_of(body)
    assert counted(client)["invalid"] == 1


def test_a_timeout_is_unavailable(client: TestClient, prometheus: Prometheus) -> None:
    seed(client)

    def slow() -> None:
        raise httpx.ReadTimeout("slow")

    prometheus.on_request = slow

    assert UNAVAILABLE in text_of(fragment(client))
    assert counted(client)["timeout"] == 3


def test_results_are_counted_and_cached(client: TestClient, prometheus: Prometheus) -> None:
    seed(client)

    fragment(client)
    fragment(client)

    assert len(prometheus.requests) == 3
    assert counted(client) == {"ok": 3, "error": 0, "timeout": 0, "invalid": 0}


@pytest.mark.parametrize("session_id", ["unknown-session", "%01bad", "x" * 300])
def test_an_unknown_session_is_a_404_without_a_query(
    client: TestClient, prometheus: Prometheus, session_id: str
) -> None:
    seed(client)

    body = fragment(client, session_id, status=404)

    assert "Session not found" in body
    assert prometheus.requests == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (HTTPException(status_code=503, detail="database busy"), "The database is busy"),
        (
            OperationalError("SELECT secret", {}, Exception("disk I/O error")),
            "The app could not read its database",
        ),
    ],
    ids=["busy", "broken"],
)
def test_a_failed_read_is_a_message_with_200_and_no_query(
    client: TestClient, prometheus: Prometheus, error: Exception, message: str
) -> None:
    class FailingGate(ReadGate):
        async def run[T, *Ts](self, read: Callable[[*Ts], T], *args: *Ts) -> T:
            raise error

    seed(client)
    app_of(client).state.reads = FailingGate()

    body = fragment(client)

    assert message in text_of(body)
    assert "secret" not in body
    assert prometheus.requests == []


# The window and the read gate.


def times(prometheus: Prometheus) -> set[str]:
    return {request.url.params["time"] for request in prometheus.requests}


def ranges(prometheus: Prometheus) -> set[str]:
    return {
        found.group(1)
        for request in prometheus.requests
        if (found := re.search(r"\[(\d+s)\] anchored", request.url.params["query"]))
    }


def seconds(ms: int) -> str:
    return f"{ms // 1000}.{ms % 1000:03d}"


def test_a_live_session_is_read_up_to_now(client: TestClient, prometheus: Prometheus) -> None:
    seed(client)

    fragment(client)

    assert times(prometheus) == {seconds(NOW)}
    assert ranges(prometheus) == {f"{(HOUR_MS + WINDOW_BEFORE_MS) // 1000}s"}
    for request in prometheus.requests:
        assert f'host="{HOST}",session_id="{SID}"' in request.url.params["query"]


def test_an_ended_session_is_read_up_to_two_minutes_after_its_end(
    client: TestClient, prometheus: Prometheus
) -> None:
    ended = NOW - 30 * MINUTE_MS
    seed(client, "ended", ended_at_ms=ended, end_reason="prompt_input_exit")

    fragment(client)

    assert times(prometheus) == {seconds(ended + WINDOW_AFTER_MS)}


def test_a_session_that_just_ended_is_read_up_to_now(
    client: TestClient, prometheus: Prometheus
) -> None:
    seed(client, "ended", ended_at_ms=NOW - MINUTE_MS)

    fragment(client)

    assert times(prometheus) == {seconds(NOW)}


def test_a_stale_session_is_read_up_to_two_minutes_after_its_last_event(
    client: TestClient, prometheus: Prometheus
) -> None:
    last = NOW - 13 * HOUR_MS
    seed(client, "stale", started_at_ms=last - HOUR_MS, last_event_at_ms=last)

    fragment(client)

    assert times(prometheus) == {seconds(last + WINDOW_AFTER_MS)}


def test_one_read_and_no_slot_held_while_prometheus_answers(
    client: TestClient, prometheus: Prometheus, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(client)
    reads: list[str] = []
    in_read = {"now": False}
    original = ReadGate.run

    async def run(self: ReadGate, read: Any, *args: Any) -> Any:
        reads.append(getattr(read, "__name__", "read"))
        in_read["now"] = True
        try:
            return await original(self, read, *args)
        finally:
            in_read["now"] = False

    monkeypatch.setattr(ReadGate, "run", run)
    held: list[bool] = []
    prometheus.on_request = lambda: held.append(in_read["now"])

    fragment(client)

    assert len(reads) == 1
    assert held == [False, False, False]


def test_the_fragment_is_guarded_and_never_stored(client: TestClient) -> None:
    seed(client)

    response = client.get(usage_url())

    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert client.get(usage_url(), headers={"host": "evil.example"}).status_code == 400
    foreign = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}
    assert client.get(usage_url(), headers=foreign).status_code == 403


def test_the_reader_is_closed_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prometheus: Prometheus
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    app = create_app(
        Settings(data_dir=tmp_path / "data", host_label=HOST),
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        prometheus_transport=httpx.MockTransport(prometheus.handle),
    )
    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        reader = app.state.usage
        assert reader.closed is False

    assert reader.closed is True


def test_missing_values_and_labels_read_plainly(client: TestClient, prometheus: Prometheus) -> None:
    seed(client)
    prometheus.tokens_by_type = {"": "5", "thinking": "7"}
    prometheus.tokens_by_model = {"tokens-only": "12"}
    prometheus.cost_by_model = {"": "0.25"}

    body = fragment(client)

    text = text_of(body)
    assert "Unknown type 5" in text
    assert "thinking 7" in text
    assert "Unknown model - $0.2500" in text
    assert "tokens-only 12 -" in text
    assert "<code>Unknown model</code>" not in body
    assert "<code>tokens-only</code>" in body
