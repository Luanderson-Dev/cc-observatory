"""The reconciler (``ccobs.server.reconcile``): Esc denials closed from Loki (ADR 0029).

A permission dialog denied with Esc fires no closing hook (ADR 0024); telemetry records
a ``tool_decision`` reject. The unit tests drive ``Reconciler.poll_once`` and ``run`` on
the test thread with a fake writer, a fake clock, a fake wait and an
``httpx.MockTransport`` that stands in for Loki. The end-to-end tests post the
``interactive-permission-compact`` hook fixture up to its Esc-denied PermissionRequest
to the real app, persist it with a writer the test drives (``run_once``), and answer the
query with the fixture's own ``tool_decision`` reject, in the shape that
``tests/integration/test_stack_logs.py`` reads from Loki.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from ccobs.domain.app_metrics import LOKI_QUERIES, LokiQueryResult
from ccobs.domain.denials import OpenBlock
from ccobs.domain.hook_config import HOOK_PATH
from ccobs.domain.telemetry import is_identity_key
from ccobs.server.app import create_app
from ccobs.server.config import Settings
from ccobs.server.db.repository import Repository
from ccobs.server.ingest import HookQueue
from ccobs.server.metrics import AppMetrics
from ccobs.server.reconcile import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    GRACE_S,
    HORIZON_S,
    HTTP_TIMEOUT_S,
    MAX_ANSWER_BYTES,
    POLL_INTERVAL_S,
    QUERY_LIMIT,
    QUERY_RANGE_PATH,
    WINDOW_MARGIN_S,
    Reconciler,
)
from ccobs.server.sweeps import Sweeper
from ccobs.server.writer import BlockClosedCallback, Writer

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
HOOKS: Final = FIXTURES / "hooks" / "interactive-permission-compact.jsonl"
OTLP_LOGS: Final = FIXTURES / "otlp" / "interactive-permission-compact" / "logs.jsonl"
MANIFEST: Final = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
# Up to the PermissionRequest that was denied with Esc (its 13th line); the first
# permission was approved and closed by its PostToolUse.
ESC_PREFIX: Final = 13
SESSION: Final = next(
    scenario["session_id"]
    for scenario in MANIFEST["scenarios"]
    if scenario["name"] == "interactive-permission-compact"
)
DENIED_CALL: Final = "toolu_01EXAMPLE000000000000023"
APPROVED_CALL: Final = "toolu_01EXAMPLE000000000000022"
# The reject's time in the fixture (2026-10-13T23:20:41.510Z).
DENIED_AT_MS: Final = 1_791_933_641_510
DENIED_REQUEST_OFFSET_MS: Final = 36_026
# The replay puts the Esc-denied PermissionRequest 5 s before the reject.
BASE: Final = DENIED_AT_MS - 5_000 - DENIED_REQUEST_OFFSET_MS
OPENED_MS: Final = BASE + DENIED_REQUEST_OFFSET_MS
NOW_MS: Final = DENIED_AT_MS + 3_000
HOST: Final = "test-host"
LOKI_URL: Final = "http://loki.test:3100"
HOUR_MS: Final = 3_600_000
NS_PER_MS: Final = 1_000_000
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}
API: Final = "/api/v1/sessions"
CANARY: Final = "CANARY-7f3a"
PATIENCE_S: Final = 10.0
EXPECTED_QUERY: Final = (
    '{service_name="claude-code", host="test-host"} | event_name="tool_decision"'
    ' | decision="reject"'
)

type Payload = dict[str, Any]
type Handler = Callable[[httpx.Request], httpx.Response]


# Loki's answer, from the OTLP fixture.


def _string(value: Payload) -> str:
    (inner,) = value.values()
    return str(inner)


def fixture_rejects() -> list[list[Any]]:
    """The fixture's ``tool_decision`` rejects as Loki entries (``categorize-labels``)."""
    entries = []
    for line in OTLP_LOGS.read_text(encoding="utf-8").splitlines():
        for resource_logs in json.loads(line)["resourceLogs"]:
            resource = resource_logs["resource"]["attributes"]
            for scope_logs in resource_logs["scopeLogs"]:
                for record in scope_logs["logRecords"]:
                    metadata = {
                        item["key"].replace(".", "_"): _string(item["value"])
                        for item in [*resource, *record["attributes"]]
                        if item["key"] != "service.name" and not is_identity_key(item["key"])
                    }
                    if metadata["event_name"] == "tool_decision" and (
                        metadata["decision"] == "reject"
                    ):
                        entries.append(
                            [
                                record["timeUnixNano"],
                                record["body"]["stringValue"],
                                {"structuredMetadata": metadata},
                            ]
                        )
    return entries


def loki_answer(entries: Sequence[Any]) -> Payload:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": {"service_name": "claude-code", "host": HOST}, "values": entries}
            ],
            "encodingFlags": ["categorize-labels"],
        },
    }


class Loki:
    """A MockTransport that records requests and answers from a script: each item is a
    response, or an exception to raise; the last item repeats."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script = list(script) or [httpx.Response(200, json=loki_answer(fixture_rejects()))]
        self.requests: list[httpx.Request] = []
        self.closed = False
        self.seen = threading.Event()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.seen.set()
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        try:
            content = item.content
        except httpx.ResponseNotRead:
            return item  # a streamed body, used once
        # A fresh response each time: the client closes the one it reads.
        return httpx.Response(item.status_code, headers=item.headers, content=content)

    def transport(self) -> httpx.MockTransport:
        loki = self

        class Transport(httpx.MockTransport):
            def close(self) -> None:
                loki.closed = True

        return Transport(self.handle)


# Unit tests: a fake writer, clock and wait.


class Blocks:
    """The writer as the reconciler sees it: open blocks, and the closes submitted."""

    def __init__(self, *blocks: OpenBlock) -> None:
        self.blocks = list(blocks)
        self.closes: list[tuple[str, str, int]] = []
        self.fail: Exception | None = None

    def open_permission_blocks(self) -> list[OpenBlock]:
        if self.fail is not None:
            raise self.fail
        return list(self.blocks)

    def submit_telemetry_close(self, session_id: str, tool_use_id: str, at_ms: int) -> None:
        self.closes.append((session_id, tool_use_id, at_ms))


class FakeTime:
    """A wall clock only the fake wait moves; the wait stops after ``rounds``."""

    def __init__(self, rounds: int, now: int = NOW_MS) -> None:
        self.now = now
        self.rounds = rounds
        self.waits: list[float] = []

    def clock(self) -> int:
        return self.now

    def wait(self, timeout_s: float) -> bool:
        if len(self.waits) == self.rounds:
            return True
        self.waits.append(timeout_s)
        self.now += int(timeout_s * 1000)
        return False


class Results:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, result: LokiQueryResult) -> None:
        self.counts[result] += 1


def denied_block() -> OpenBlock:
    return OpenBlock(session_id=SESSION, tool_use_id=DENIED_CALL, opened_at_ms=OPENED_MS)


def make(
    writer: Blocks,
    loki: Loki,
    *,
    now: int = NOW_MS,
    fake: FakeTime | None = None,
    results: Results | None = None,
    **kwargs: Any,
) -> Reconciler:
    return Reconciler(
        writer,
        loki_url=LOKI_URL,
        host=HOST,
        clock=fake.clock if fake else (lambda: now),
        wait=fake.wait if fake else None,
        transport=loki.transport(),
        on_query=results,
        **kwargs,
    )


def test_the_defaults() -> None:
    assert POLL_INTERVAL_S == 2.0
    assert GRACE_S == 2.0
    assert HORIZON_S == 900.0
    assert MAX_ANSWER_BYTES == 8 * 1024 * 1024
    assert WINDOW_MARGIN_S == 600.0
    assert QUERY_LIMIT == 1000
    assert HTTP_TIMEOUT_S == 5.0
    assert (BACKOFF_INITIAL_S, BACKOFF_MAX_S) == (2.0, 60.0)
    assert QUERY_RANGE_PATH == "/loki/api/v1/query_range"
    reconciler = Reconciler(Blocks(), loki_url=LOKI_URL, host=HOST)
    try:
        assert reconciler.poll_interval_s == POLL_INTERVAL_S
        assert reconciler.query == EXPECTED_QUERY
    finally:
        reconciler.stop(1.0)


@pytest.mark.parametrize(
    "name",
    [
        "poll_interval_s",
        "grace_s",
        "horizon_s",
        "margin_s",
        "timeout_s",
        "backoff_initial_s",
        "backoff_max_s",
    ],
)
def test_intervals_must_be_positive(name: str) -> None:
    settings: dict[str, Any] = {name: 0.0}
    with pytest.raises(ValueError, match=name):
        Reconciler(Blocks(), loki_url=LOKI_URL, host=HOST, **settings)


@pytest.mark.parametrize("horizon_s", [1.0, 2.0])
def test_the_horizon_must_exceed_the_grace_period(horizon_s: float) -> None:
    with pytest.raises(ValueError, match="horizon_s"):
        Reconciler(Blocks(), loki_url=LOKI_URL, host=HOST, grace_s=2.0, horizon_s=horizon_s)


def test_the_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limit"):
        Reconciler(Blocks(), loki_url=LOKI_URL, host=HOST, limit=0)


def test_no_query_without_an_open_permission_block() -> None:
    loki, writer = Loki(), Blocks()

    assert make(writer, loki).poll_once() == "idle"

    assert loki.requests == []
    assert writer.closes == []


def test_no_query_while_the_open_blocks_are_within_the_grace_period() -> None:
    loki = Loki()
    young = OpenBlock(SESSION, DENIED_CALL, opened_at_ms=NOW_MS - 1_999)

    assert make(Blocks(young), loki).poll_once() == "idle"
    assert loki.requests == []
    # Once past it, the query goes out.
    assert make(Blocks(young), loki, now=NOW_MS + 1).poll_once() == "ok"
    assert len(loki.requests) == 1


def test_no_query_when_the_open_blocks_are_past_the_horizon() -> None:
    loki = Loki()
    old = OpenBlock(SESSION, DENIED_CALL, opened_at_ms=NOW_MS - 900_001)

    assert make(Blocks(old), loki).poll_once() == "idle"
    assert loki.requests == []
    # At the horizon itself, it is still due.
    assert make(Blocks(old), loki, now=NOW_MS - 1).poll_once() == "ok"
    assert len(loki.requests) == 1


def test_only_the_due_blocks_are_queried_and_bound_the_window() -> None:
    loki = Loki()
    young = OpenBlock("00000000-0000-4000-8000-0000000000b2", "toolu_young", NOW_MS - 1_000)
    old = OpenBlock("00000000-0000-4000-8000-0000000000b3", "toolu_old", NOW_MS - 3_600_000)
    due = OpenBlock("00000000-0000-4000-8000-0000000000b4", "toolu_due", NOW_MS - 120_000)
    writer = Blocks(young, old, due, denied_block())

    assert make(writer, loki).poll_once() == "ok"

    (request,) = loki.requests
    # From the oldest due block, not the one past the horizon.
    assert request.url.params["start"] == str((NOW_MS - 120_000 - 600_000) * NS_PER_MS)
    assert request.url.params["end"] == str((NOW_MS + 600_000) * NS_PER_MS)
    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)]


def test_a_rejection_for_a_block_past_the_horizon_is_not_submitted() -> None:
    late = OpenBlock(SESSION, DENIED_CALL, opened_at_ms=NOW_MS - 1_000_000)
    due = OpenBlock("00000000-0000-4000-8000-0000000000b4", "toolu_due", NOW_MS - 120_000)
    writer = Blocks(late, due)

    assert make(writer, Loki()).poll_once() == "ok"

    assert writer.closes == []


def test_the_request_is_bounded_and_asks_for_categorized_labels() -> None:
    loki = Loki()
    older = OpenBlock("00000000-0000-4000-8000-0000000000b1", "toolu_x", OPENED_MS - 60_000)

    assert make(Blocks(denied_block(), older), loki).poll_once() == "ok"

    (request,) = loki.requests
    assert request.method == "GET"
    assert f"{request.url.scheme}://{request.url.netloc.decode()}" == LOKI_URL
    assert request.url.path == QUERY_RANGE_PATH
    assert dict(request.url.params) == {
        "query": EXPECTED_QUERY,
        "start": str((OPENED_MS - 60_000 - 600_000) * NS_PER_MS),
        "end": str((NOW_MS + 600_000) * NS_PER_MS),
        "limit": "1000",
        # Newest first: a hit limit cuts the oldest rejects.
        "direction": "backward",
    }
    assert request.headers["X-Loki-Response-Encoding-Flags"] == "categorize-labels"
    # No session ID anywhere in the request: Loki logs every query at info (ADR 0015).
    for session in (SESSION, older.session_id):
        assert session not in str(request.url)
        assert session not in str(dict(request.headers))
    assert request.extensions["timeout"] == {
        "connect": HTTP_TIMEOUT_S,
        "read": HTTP_TIMEOUT_S,
        "write": HTTP_TIMEOUT_S,
        "pool": HTTP_TIMEOUT_S,
    }


def test_a_match_is_submitted_to_the_writer_at_the_event_time() -> None:
    writer, results = Blocks(denied_block()), Results()

    assert make(writer, Loki(), results=results).poll_once() == "ok"

    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)]
    assert results.counts == Counter({"ok": 1})


def test_a_rejection_without_an_open_block_is_not_submitted() -> None:
    other = OpenBlock(SESSION, APPROVED_CALL, OPENED_MS)
    writer = Blocks(other)

    assert make(writer, Loki()).poll_once() == "ok"

    assert writer.closes == []


def test_the_event_time_is_clamped_to_the_opening() -> None:
    late_open = OpenBlock(SESSION, DENIED_CALL, opened_at_ms=DENIED_AT_MS + 1_000)
    writer = Blocks(late_open)

    make(writer, Loki()).poll_once()

    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS + 1_000)]


ERRORS: Final = (
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.Response(503, text=f"Loki is not ready {CANARY}"),
    httpx.Response(400, json={"status": "error", "error": f"bad query {SESSION}"}),
    httpx.Response(200, text=f"not json {CANARY}"),
    httpx.Response(200, json={"status": "error", "error": CANARY}),
    httpx.Response(200, json=[SESSION, CANARY]),
)


@pytest.mark.parametrize("error", ERRORS, ids=lambda error: repr(error)[:40])
def test_a_failed_query_is_an_error_and_submits_nothing(
    error: httpx.Response | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, results = Blocks(denied_block()), Results()

    assert make(writer, Loki(error), results=results).poll_once() == "error"

    assert writer.closes == []
    assert results.counts == Counter({"error": 1})
    assert_quiet(caplog)
    assert "loki query failed" in caplog.text


def assert_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """No URL, query, session ID, call ID, host or body in what the reconciler logs."""
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        SESSION,
        DENIED_CALL,
        CANARY,
        "loki.test",
        "http",
        "query_range",
        "service_name",
        "tool_decision",
        "refused",
        "slow",
    ):
        assert secret not in text, secret
    for record in caplog.records:
        assert record.exc_info is None
        assert record.name.startswith("ccobs")


def test_malformed_entries_are_skipped_and_counted_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    good = fixture_rejects()
    bad = [["nope", CANARY], [good[0][0], CANARY, {"structuredMetadata": {"x": SESSION}}]]
    writer = Blocks(denied_block())
    loki = Loki(httpx.Response(200, json=loki_answer([*bad, *good])))

    assert make(writer, loki).poll_once() == "ok"

    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)]
    assert "2 malformed entries skipped" in caplog.text
    assert_quiet(caplog)


def test_a_full_answer_is_logged_once_per_transition(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = Blocks(denied_block())
    full = httpx.Response(200, json=loki_answer(fixture_rejects() * 3))
    short = httpx.Response(200, json=loki_answer(fixture_rejects()))
    failed = httpx.ConnectError("down")
    loki = Loki(full, full, failed, full, short, full)
    reconciler = make(writer, loki, limit=3)

    results = [reconciler.poll_once() for _ in range(6)]

    assert results == ["ok", "ok", "error", "ok", "ok", "ok"]
    limited = [r for r in caplog.records if "hit its limit" in r.getMessage()]
    # Into the limited state at the first poll, out at the short answer, in again at the
    # last; an error in between changes nothing.
    assert len(limited) == 2
    assert "loki answer hit its limit of 3 entries" in caplog.text
    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)] * 5
    assert_quiet(caplog)


def _oversized_chunks() -> Iterator[bytes]:
    chunk = b" " * (1024 * 1024)
    for _ in range(8):
        yield chunk
    yield b" " * 2 + json.dumps(loki_answer(fixture_rejects())).encode()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b" " * MAX_ANSWER_BYTES + b"{}"),
        httpx.Response(200, content=_oversized_chunks()),
    ],
    ids=["with a length", "streamed"],
)
def test_an_answer_over_the_cap_is_an_error(
    response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer, results = Blocks(denied_block()), Results()

    assert make(writer, Loki(response), results=results).poll_once() == "error"

    assert writer.closes == []
    assert results.counts == Counter({"error": 1})
    assert "loki query failed (answer too large)" in caplog.text
    assert_quiet(caplog)


def test_an_answer_at_the_cap_is_read() -> None:
    body = json.dumps(loki_answer(fixture_rejects())).encode()
    padded = body + b" " * (MAX_ANSWER_BYTES - len(body))
    writer = Blocks(denied_block())

    assert make(writer, Loki(httpx.Response(200, content=padded))).poll_once() == "ok"

    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)]


# The loop: back-off, recovery, errors outside Loki.


def test_errors_back_off_doubling_to_a_cap_then_recover() -> None:
    errors: list[httpx.Response | Exception] = [httpx.ConnectError("down")] * 7
    loki = Loki(*errors, httpx.ReadTimeout("slow"), httpx.Response(200, json=loki_answer([])))
    fake, results = FakeTime(rounds=12), Results()
    writer = Blocks(denied_block())

    make(writer, loki, fake=fake, results=results).run()

    # The first wait is the poll interval; each error doubles the next one from 2 s up
    # to 60 s; the first success brings back the poll interval.
    assert fake.waits == [2.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 2.0, 2.0, 2.0]
    # One round after each wait: 8 errors, then 4 answers.
    assert results.counts == Counter({"error": 8, "ok": 4})
    assert len(loki.requests) == 12


def test_idle_rounds_reset_the_back_off() -> None:
    loki = Loki(httpx.ConnectError("down"))
    fake = FakeTime(rounds=4)
    writer = Blocks(denied_block())

    def wait(timeout_s: float) -> bool:
        # After two failures, the block closes by a hook: nothing left to query.
        if len(fake.waits) == 2:
            writer.blocks.clear()
        return fake.wait(timeout_s)

    Reconciler(
        writer,
        loki_url=LOKI_URL,
        host=HOST,
        clock=fake.clock,
        wait=wait,
        transport=loki.transport(),
    ).run()

    assert fake.waits == [2.0, 2.0, 4.0, 2.0]
    assert len(loki.requests) == 2


def test_a_failing_writer_or_clock_is_logged_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = Blocks(denied_block())
    writer.fail = RuntimeError(f"broken {SESSION}")
    fake, results = FakeTime(rounds=3), Results()
    loki = Loki()

    make(writer, loki, fake=fake, results=results).run()

    assert fake.waits == [2.0, 2.0, 2.0]
    assert loki.requests == []
    assert results.counts == Counter()
    assert "reconcile round failed (RuntimeError)" in caplog.text
    assert_quiet(caplog)


def test_a_failing_counter_does_not_stop_a_round() -> None:
    def broken(result: LokiQueryResult) -> None:
        raise RuntimeError(result)

    writer = Blocks(denied_block())
    reconciler = Reconciler(
        writer,
        loki_url=LOKI_URL,
        host=HOST,
        clock=lambda: NOW_MS,
        transport=Loki().transport(),
        on_query=broken,
    )

    assert reconciler.poll_once() == "ok"
    assert writer.closes == [(SESSION, DENIED_CALL, DENIED_AT_MS)]
    reconciler.stop(1.0)


def test_stop_is_prompt_and_closes_the_client() -> None:
    loki = Loki()
    reconciler = make(Blocks(), loki, poll_interval_s=60.0)
    reconciler.start()
    assert reconciler.is_alive()
    with pytest.raises(RuntimeError, match="already started"):
        reconciler.start()

    started = time.monotonic()
    reconciler.stop(5.0)

    assert time.monotonic() - started < 1.0
    assert not reconciler.is_alive()
    assert loki.closed


def test_stop_without_start_closes_the_client() -> None:
    loki = Loki()
    reconciler = make(Blocks(), loki)

    reconciler.stop(1.0)

    assert not reconciler.is_alive()
    assert loki.closed


def test_a_stop_that_times_out_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    release = threading.Event()

    def stuck(request: httpx.Request) -> httpx.Response:
        release.wait(PATIENCE_S)
        return httpx.Response(200, json=loki_answer([]))

    reconciler = Reconciler(
        Blocks(denied_block()),
        loki_url=LOKI_URL,
        host=HOST,
        clock=lambda: NOW_MS,
        poll_interval_s=0.01,
        transport=httpx.MockTransport(stuck),
    )
    reconciler.start()
    time.sleep(0.1)
    reconciler.stop(0.2)
    release.set()

    assert "reconciler did not stop within 0.2 s" in caplog.text


# End to end: the real app and writer.


class ManualWriter(Writer):
    """The app's writer without its thread: the tests persist with ``run_once``."""

    def start(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = BASE

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST, loki_url=LOKI_URL)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def manual(
    queue: HookQueue, repository: Repository, on_block_closed: BlockClosedCallback
) -> ManualWriter:
    return ManualWriter(
        queue,
        repository,
        HOST,
        stale_after_ms=12 * HOUR_MS,
        batch_wait_s=0,
        on_block_closed=on_block_closed,
    )


def start(settings: Settings, clock: Clock, **kwargs: Any) -> TestClient:
    queue = HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    app = create_app(
        settings,
        queue=queue,
        clock=clock,
        writer_factory=manual,
        serve_metrics=False,
        sweeps=False,
        **kwargs,
    )
    return TestClient(app, base_url=f"http://{GOOD_HOST}")


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    with start(settings, clock, reconcile=False) as test_client:
        yield test_client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def writer_of(client: TestClient) -> ManualWriter:
    writer = app_of(client).state.writer
    assert isinstance(writer, ManualWriter)
    return writer


def esc_prefix() -> list[tuple[int, Payload]]:
    lines = []
    for line in HOOKS.read_text(encoding="utf-8").splitlines()[:ESC_PREFIX]:
        document = json.loads(line)
        lines.append((document["offset_ms"], document["payload"]))
    assert lines[-1][1]["hook_event_name"] == "PermissionRequest"
    assert lines[-1][0] == DENIED_REQUEST_OFFSET_MS
    return lines


def post(client: TestClient, clock: Clock, lines: Sequence[tuple[int, Payload]]) -> None:
    for offset, document in lines:
        clock.now = BASE + offset
        response = client.post(HOOK_PATH, content=json.dumps(document).encode(), headers=JSON)
        assert response.status_code == 204


def persist(client: TestClient) -> None:
    writer = writer_of(client)
    while writer.run_once(0.0):
        pass


def detail(client: TestClient) -> Any:
    response = client.get(f"{API}/{SESSION}")
    assert response.status_code == 200, response.text
    return response.json()


def blocks_by_call(client: TestClient) -> dict[str, Any]:
    return {block["tool_use_id"]: block for block in detail(client)["blocks"]}


def test_the_writer_lists_open_permission_blocks_with_a_call_id(
    client: TestClient, clock: Clock
) -> None:
    post(client, clock, esc_prefix())
    # Another session whose PermissionRequest has no PreToolUse to borrow a call ID from.
    orphan = {**esc_prefix()[-1][1], "session_id": "00000000-0000-4000-8000-0000000000c1"}
    post(client, clock, [(DENIED_REQUEST_OFFSET_MS, orphan)])
    persist(client)

    assert writer_of(client).open_permission_blocks() == [
        OpenBlock(SESSION, DENIED_CALL, OPENED_MS)
    ]


def test_a_rejection_in_loki_closes_the_block_as_denied(client: TestClient, clock: Clock) -> None:
    post(client, clock, esc_prefix())
    persist(client)
    before = detail(client)
    assert before["status"] == "waiting_user"
    assert blocks_by_call(client)[DENIED_CALL]["closed_at_ms"] is None
    approved = blocks_by_call(client)[APPROVED_CALL]
    loki = Loki()
    clock.now = NOW_MS
    reconciler = Reconciler(
        writer_of(client), loki_url=LOKI_URL, host=HOST, clock=clock, transport=loki.transport()
    )

    try:
        assert reconciler.poll_once() == "ok"
    finally:
        reconciler.stop(1.0)
    persist(client)

    (request,) = loki.requests
    assert request.url.params["start"] == str((OPENED_MS - 600_000) * NS_PER_MS)
    assert request.url.params["end"] == str((NOW_MS + 600_000) * NS_PER_MS)
    after = detail(client)
    denied = blocks_by_call(client)[DENIED_CALL]
    assert denied["closed_at_ms"] == DENIED_AT_MS
    assert denied["close_reason"] == "denied"
    assert denied["close_source"] == "telemetry"
    assert after["status"] != "waiting_user"
    # The approved permission keeps what its hook recorded.
    assert blocks_by_call(client)[APPROVED_CALL] == approved
    assert writer_of(client).open_permission_blocks() == []


def test_the_app_runs_the_reconciler_and_counts_its_queries(
    settings: Settings, clock: Clock
) -> None:
    loki = Loki(
        httpx.ConnectError("down"), httpx.Response(200, json=loki_answer(fixture_rejects()))
    )
    with start(
        settings,
        clock,
        loki_transport=loki.transport(),
        reconcile_interval_s=0.02,
    ) as client:
        reconciler = app_of(client).state.reconciler
        assert isinstance(reconciler, Reconciler)
        assert reconciler.is_alive()
        assert reconciler.query == EXPECTED_QUERY
        post(client, clock, esc_prefix())
        persist(client)
        # Within the grace period: no query yet.
        time.sleep(0.1)
        assert loki.requests == []
        clock.now = NOW_MS
        assert loki.seen.wait(PATIENCE_S)
        deadline = time.monotonic() + PATIENCE_S
        while blocks_by_call(client)[DENIED_CALL]["closed_at_ms"] is None:
            assert time.monotonic() < deadline, "the reconciler did not close the block"
            persist(client)
            time.sleep(0.01)
        metrics = app_of(client).state.metrics
        assert isinstance(metrics, AppMetrics)
        text = generate_latest(metrics.registry).decode()
    assert app_of(client).state.reconciler is None
    assert not reconciler.is_alive()
    assert loki.closed
    # Both permissions closed: the approved one by its hook, the other from Loki.
    assert f'ccobs_block_duration_seconds_count{{host="{HOST}",kind="permission"}} 2.0' in text
    assert f'{LOKI_QUERIES.name}{{host="{HOST}",result="error"}} 1.0' in text
    assert f'{LOKI_QUERIES.name}{{host="{HOST}",result="ok"}}' in text


def test_the_reconciler_is_off_when_telemetry_reconcile_is(
    settings: Settings, clock: Clock
) -> None:
    loki = Loki()
    off = settings.model_copy(update={"telemetry_reconcile": False})
    with start(off, clock, loki_transport=loki.transport(), reconcile_interval_s=0.01) as client:
        assert app_of(client).state.reconciler is None
        post(client, clock, esc_prefix())
        persist(client)
        clock.now = NOW_MS
        time.sleep(0.1)
    assert loki.requests == []


def test_create_app_can_leave_the_reconciler_off(settings: Settings, clock: Clock) -> None:
    with start(settings, clock, reconcile=False) as client:
        assert app_of(client).state.reconciler is None


def test_the_default_reconciler_follows_the_settings(settings: Settings, clock: Clock) -> None:
    with start(settings, clock) as client:
        reconciler = app_of(client).state.reconciler
        assert isinstance(reconciler, Reconciler)
        assert reconciler.poll_interval_s == POLL_INTERVAL_S
        assert reconciler.loki_url == LOKI_URL
        assert reconciler.query == EXPECTED_QUERY


def test_the_lifespan_starts_the_reconciler_last_and_stops_it_first(
    settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    for owner, name in ((Writer, "writer"), (Sweeper, "sweeper"), (Reconciler, "reconciler")):
        original_start, original_stop = owner.start, owner.stop

        def started(self: Any, original: Any = original_start, name: str = name) -> None:
            order.append(f"{name} start")
            original(self)

        def stopped(
            self: Any, timeout_s: float, original: Any = original_stop, name: str = name
        ) -> None:
            order.append(f"{name} stop")
            original(self, timeout_s)

        monkeypatch.setattr(owner, "start", started)
        monkeypatch.setattr(owner, "stop", stopped)
    queue = HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)
    app = create_app(settings, queue=queue, clock=clock, serve_metrics=False)

    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert order == ["writer start", "sweeper start", "reconciler start"]

    assert order[3:] == ["reconciler stop", "sweeper stop", "writer stop"]
