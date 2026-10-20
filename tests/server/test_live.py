"""Live updates: the change hub, the server-sent events stream and live.js (ADR 0032).

The writer thread tells the hub which sessions a commit changed; the hub marks each
subscriber dirty, and each subscriber's stream sends one coalesced ``changed`` event,
which live.js turns into a ``ccobs:changed`` event on ``document.body`` for htmx to
refetch. The stream never reads the database, holds bounded memory per client and is
capped at ``MAX_STREAMS`` open at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, Final
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from ccobs.domain.app_metrics import UI_STREAMS, UI_STREAMS_REFUSED
from ccobs.server import __main__ as server_main
from ccobs.server import live
from ccobs.server.app import create_app
from ccobs.server.changes import ALERTS, ALL
from ccobs.server.config import Settings
from ccobs.server.live import ChangeHub, StreamTiming, Subscription, encode_change
from ccobs.server.metrics import AppMetrics

STATIC: Final = Path(__file__).parents[2] / "src" / "ccobs" / "server" / "static"
LIVE_JS: Final = STATIC / "js" / "live.js"
HOST: Final = "test-host"
GOOD_HOST: Final = "127.0.0.1:4300"
SID: Final = "5e551011-aaaa-bbbb-cccc-000000000001"
OTHER: Final = "5e551011-aaaa-bbbb-cccc-000000000002"
FAST: Final = StreamTiming(retry_ms=5000, coalesce_s=0.05, heartbeat_s=30.0)


# The hub and its subscriptions, on an event loop of their own.


def run[T](body: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run the coroutine function ``body`` on a fresh event loop."""
    return asyncio.run(body())


def test_changes_coalesce_until_taken() -> None:
    hub = ChangeHub()

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        hub.publish(frozenset({SID}))
        hub.publish(frozenset({OTHER, SID}))

        assert await subscription.wait(1.0)
        assert subscription.take() == frozenset({SID, OTHER})
        # Taken: nothing is pending until the next change.
        assert not await subscription.wait(0.01)

    run(body)


def test_a_session_subscription_sees_only_its_session_or_everything() -> None:
    hub = ChangeHub()

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(SID)
        assert subscription is not None
        assert subscription.session_id == SID

        hub.publish(frozenset({OTHER}))
        assert not await subscription.wait(0.01)
        hub.publish(frozenset({OTHER, SID}))
        assert await subscription.wait(1.0)
        assert subscription.take() == frozenset({SID})
        hub.publish(ALL)
        hub.publish(frozenset({SID}))
        assert subscription.take() is ALL

    run(body)


def test_too_many_pending_ids_collapse_to_everything() -> None:
    hub = ChangeHub(max_ids=3)

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        hub.publish(frozenset({"a", "b", "c"}))
        assert subscription.take() == frozenset({"a", "b", "c"})
        hub.publish(frozenset({"a", "b"}))
        hub.publish(frozenset({"c", "d"}))
        assert subscription.take() is ALL
        hub.publish(ALL)
        hub.publish(frozenset({"e"}))
        assert subscription.take() is ALL

    run(body)


def test_the_hub_caps_open_streams_and_counts_refusals() -> None:
    hub = ChangeHub(max_streams=2)

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        first, second = hub.subscribe(None), hub.subscribe(SID)
        assert first is not None
        assert second is not None
        assert hub.open_streams == 2

        assert hub.subscribe(None) is None
        assert hub.refused_streams == 1

        first.close()
        first.close()  # twice is harmless
        assert hub.open_streams == 1
        assert hub.subscribe(None) is not None
        assert hub.refused_streams == 1

    run(body)


def test_a_hub_not_open_or_closed_refuses_without_counting() -> None:
    hub = ChangeHub()
    assert not hub.is_open
    assert hub.subscribe(None) is None

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        assert hub.is_open
        hub.close()
        assert not hub.is_open
        assert hub.subscribe(None) is None

    run(body)
    assert hub.refused_streams == 0


def test_close_wakes_every_subscriber_and_ends_their_streams() -> None:
    hub = ChangeHub()

    async def body() -> list[bytes]:
        hub.open(asyncio.get_running_loop())
        subscriptions = [hub.subscribe(None), hub.subscribe(SID)]
        chunks: list[bytes] = []

        async def consume(subscription: Subscription) -> None:
            async for chunk in live.stream_events(subscription, FAST):
                chunks.append(chunk)

        tasks = []
        for subscription in subscriptions:
            assert subscription is not None
            tasks.append(asyncio.create_task(consume(subscription)))
        await asyncio.sleep(0.05)
        hub.close()
        # Both generators return on their own, without a cancellation.
        async with asyncio.timeout(2.0):
            await asyncio.gather(*tasks)
        assert all(
            subscription is not None and subscription.closed for subscription in subscriptions
        )
        assert hub.open_streams == 0
        return chunks

    assert run(body) == [b"retry: 5000\n\n", b"retry: 5000\n\n"]


def test_a_close_while_changes_gather_ends_the_stream_without_sending_them() -> None:
    hub = ChangeHub()
    slow = StreamTiming(retry_ms=1000, coalesce_s=30.0, heartbeat_s=30.0)

    async def body() -> list[bytes]:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        chunks: list[bytes] = []

        async def consume() -> None:
            async for chunk in live.stream_events(subscription, slow):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        hub.publish(frozenset({SID}))
        await asyncio.sleep(0.02)
        hub.close()
        async with asyncio.timeout(2.0):
            await task
        return chunks

    assert run(body) == [b"retry: 1000\n\n"]


def test_a_stream_sends_one_event_per_burst_and_heartbeats_when_idle() -> None:
    hub = ChangeHub()
    timing = StreamTiming(retry_ms=5000, coalesce_s=0.1, heartbeat_s=0.05)

    async def body() -> list[bytes]:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        stream = live.stream_events(subscription, timing)
        chunks = [await anext(stream)]
        # Idle: a heartbeat comment.
        chunks.append(await anext(stream))
        # A burst inside the coalescing window: one event.
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        hub.publish(frozenset({SID}))
        await asyncio.sleep(0.02)
        hub.publish(frozenset({OTHER}))
        chunks.append(await waiting)
        await stream.aclose()
        assert subscription.closed
        return chunks

    assert run(body) == [
        b"retry: 5000\n\n",
        b": keep-alive\n\n",
        f'event: changed\ndata: ["{SID}", "{OTHER}"]\n\n'.encode()
        if SID < OTHER
        else f'event: changed\ndata: ["{OTHER}", "{SID}"]\n\n'.encode(),
    ]


def test_events_say_all_or_list_the_ids_on_one_line() -> None:
    assert encode_change(ALL) == b"event: changed\ndata: all\n\n"
    assert encode_change(frozenset({"b", "a"})) == b'event: changed\ndata: ["a", "b"]\n\n'
    # IDs may hold any character but control ones; JSON keeps them on one line, ASCII.
    odd = frozenset({"a b\u2028\u00e9"})
    encoded = encode_change(odd)
    assert encoded.count(b"\n") == 3
    assert json.loads(encoded.split(b"data: ", 1)[1]) == ["a b\u2028\u00e9"]


def test_notify_threadsafe_reaches_subscribers_from_another_thread() -> None:
    hub = ChangeHub()

    async def body() -> object:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        thread = threading.Thread(target=hub.notify_threadsafe, args=(frozenset({SID}),))
        thread.start()
        thread.join()
        assert await subscription.wait(2.0)
        return subscription.take()

    assert run(body) == frozenset({SID})


def test_an_alerts_change_marks_every_stream_but_no_session() -> None:
    # The writer publishes ALERTS after a fire or a clear (ADR 0041): every stream,
    # filtered on a session or not, gets the alerts ping (ADR 0044), and no session is
    # marked as changed.
    hub = ChangeHub()

    async def body() -> list[tuple[bool, bool, object, bool]]:
        hub.open(asyncio.get_running_loop())
        subscriptions = [hub.subscribe(None), hub.subscribe(SID), hub.subscribe(OTHER)]
        hub.publish(ALERTS)
        hub.publish(ALERTS)
        seen: list[tuple[bool, bool, object, bool]] = []
        for subscription in subscriptions:
            assert subscription is not None
            woke = await subscription.wait(1.0)
            alerts = subscription.take_alerts()
            change = subscription.take()
            # Taken: nothing is pending until the next change.
            seen.append((woke, alerts, change, await subscription.wait(0.01)))
        return seen

    assert run(body) == [(True, True, frozenset(), False)] * 3


def test_taking_the_changes_keeps_the_alerts_pending_and_back() -> None:
    hub = ChangeHub()

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(SID)
        assert subscription is not None
        hub.publish(ALERTS)
        hub.publish(frozenset({SID}))
        assert subscription.take() == frozenset({SID})
        # The alerts are still pending, so the stream still wakes for them.
        assert await subscription.wait(0.5)
        assert subscription.take_alerts()
        assert not subscription.take_alerts()
        assert not await subscription.wait(0.01)

        hub.publish(frozenset({SID}))
        hub.publish(ALERTS)
        assert subscription.take_alerts()
        assert await subscription.wait(0.5)
        assert subscription.take() == frozenset({SID})
        assert not await subscription.wait(0.01)

    run(body)


def test_a_stream_pings_alerts_without_data_and_apart_from_the_changes() -> None:
    hub = ChangeHub()

    async def body() -> list[bytes]:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(SID)
        assert subscription is not None
        stream = live.stream_events(subscription, FAST)
        chunks = [await anext(stream)]
        # Alerts alone: the ping, no changed event.
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        hub.publish(ALERTS)
        chunks.append(await waiting)
        # A change and alerts in one burst: the changed event, then the ping.
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        hub.publish(ALERTS)
        hub.publish(frozenset({SID}))
        chunks.append(await waiting)
        chunks.append(await anext(stream))
        await stream.aclose()
        return chunks

    assert run(body) == [
        b"retry: 5000\n\n",
        live.ALERTS_EVENT,
        f'event: changed\ndata: ["{SID}"]\n\n'.encode(),
        live.ALERTS_EVENT,
    ]
    # A fixed marker: nothing about the alert, its session or its value.
    assert live.ALERTS_EVENT == b"event: alerts\ndata: alerts\n\n"


def test_notify_threadsafe_schedules_nothing_without_subscribers_or_a_loop() -> None:
    loop = mock.create_autospec(asyncio.AbstractEventLoop, instance=True)
    hub = ChangeHub()

    hub.notify_threadsafe(ALL)  # not open
    hub.open(loop)
    hub.notify_threadsafe(ALL)  # no subscriber
    assert loop.call_soon_threadsafe.call_count == 0

    subscription = hub.subscribe(None)
    assert subscription is not None
    hub.notify_threadsafe(ALL)
    loop.call_soon_threadsafe.assert_called_once_with(hub.publish, ALL)


def test_notify_threadsafe_after_the_loop_closed_does_not_raise() -> None:
    loop = asyncio.new_event_loop()
    hub = ChangeHub()
    hub.open(loop)
    assert hub.subscribe(None) is not None
    loop.close()

    hub.notify_threadsafe(frozenset({SID}))


def test_a_closed_subscription_ignores_changes() -> None:
    hub = ChangeHub()

    async def body() -> None:
        hub.open(asyncio.get_running_loop())
        subscription = hub.subscribe(None)
        assert subscription is not None
        subscription.close()
        subscription.offer(frozenset({SID}))
        subscription.offer_alerts()
        assert subscription.take() == frozenset()
        assert not subscription.take_alerts()

    run(body)


# The stream over HTTP, through the whole app.


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", host_label=HOST)


def start(settings: Settings, **options: Any) -> TestClient:
    app = create_app(
        settings,
        serve_metrics=False,
        sweeps=False,
        reconcile=False,
        stream_timing=options.pop("stream_timing", FAST),
        **options,
    )
    return TestClient(app, base_url=f"http://{GOOD_HOST}")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with start(settings) as test_client:
        yield test_client


def hub_of(client: TestClient) -> ChangeHub:
    app = client.app
    assert isinstance(app, FastAPI)
    hub = app.state.live
    assert isinstance(hub, ChangeHub)
    return hub


def wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def stream(
    client: TestClient,
    url: str = live.STREAM_PATH,
    *,
    headers: dict[str, str] | None = None,
    during: Callable[[ChangeHub], None] = lambda hub: None,
    streams_before: int = 0,
) -> Any:
    """GET the stream while another thread waits for it to open, runs ``during`` and
    closes the hub on the app's event loop, which ends the stream (TestClient returns a
    response only once its body is complete)."""
    hub = hub_of(client)
    portal = client.portal
    assert portal is not None
    errors: list[BaseException] = []

    def drive() -> None:
        try:
            wait_until(lambda: hub.open_streams > streams_before)
            during(hub)
        except BaseException as error:  # reported by the test thread
            errors.append(error)
        finally:
            portal.call(hub.close)

    thread = threading.Thread(target=drive)
    thread.start()
    try:
        response = client.get(url, headers=headers)
    finally:
        thread.join(10)
    assert not errors, errors
    return response


# The client fixture's streams use FAST: changes coalesce for 0.05 s, so each sleep below
# (0.5 s) leaves a wide margin for a slow CI runner.
SETTLE_S: Final = 0.5


def test_the_stream_speaks_server_sent_events(client: TestClient) -> None:
    assert FAST.coalesce_s * 5 <= SETTLE_S

    def during(hub: ChangeHub) -> None:
        hub.notify_threadsafe(frozenset({SID}))
        time.sleep(SETTLE_S)
        hub.notify_threadsafe(ALL)
        time.sleep(SETTLE_S)

    response = stream(client, during=during)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "content-length" not in response.headers
    assert response.text == (
        f'retry: 5000\n\nevent: changed\ndata: ["{SID}"]\n\nevent: changed\ndata: all\n\n'
    )


def test_a_session_stream_skips_other_sessions(client: TestClient) -> None:
    def during(hub: ChangeHub) -> None:
        hub.notify_threadsafe(frozenset({OTHER}))
        time.sleep(SETTLE_S)
        hub.notify_threadsafe(frozenset({OTHER, SID}))
        time.sleep(SETTLE_S)

    response = stream(client, f"{live.STREAM_PATH}?session={SID}", during=during)

    assert response.text == f'retry: 5000\n\nevent: changed\ndata: ["{SID}"]\n\n'


@pytest.mark.parametrize("url", [live.STREAM_PATH, f"{live.STREAM_PATH}?session={SID}"])
def test_the_alerts_ping_reaches_every_stream(client: TestClient, url: str) -> None:
    def during(hub: ChangeHub) -> None:
        hub.notify_threadsafe(ALERTS)
        time.sleep(SETTLE_S)
        hub.notify_threadsafe(frozenset({SID}))
        time.sleep(SETTLE_S)

    response = stream(client, url, during=during)

    # The ping carries a fixed marker only; the changed event is as before.
    assert response.text == (
        f'retry: 5000\n\nevent: alerts\ndata: alerts\n\nevent: changed\ndata: ["{SID}"]\n\n'
    )


def test_heartbeats_keep_an_idle_stream_open(settings: Settings) -> None:
    timing = StreamTiming(retry_ms=1234, coalesce_s=0.05, heartbeat_s=0.05)
    with start(settings, stream_timing=timing) as client:
        response = stream(client, during=lambda hub: time.sleep(0.3))

    assert response.text.startswith("retry: 1234\n\n: keep-alive\n\n")
    assert set(response.text.split("\n\n")[1:-1]) == {": keep-alive"}


def test_an_invalid_session_filter_is_a_plain_400(client: TestClient) -> None:
    for value in ("%01bad", "x" * 300):
        response = client.get(f"{live.STREAM_PATH}?session={value}")
        assert response.status_code == 400
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.text == "invalid session\n"
    assert hub_of(client).open_streams == 0


def test_the_seventeenth_stream_gets_a_plain_503_and_is_counted(client: TestClient) -> None:
    hub = hub_of(client)
    held = [hub.subscribe(None) for _ in range(live.MAX_STREAMS)]
    assert all(subscription is not None for subscription in held)

    response = client.get(live.STREAM_PATH)

    assert live.MAX_STREAMS == 16
    assert response.status_code == 503
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["retry-after"] == "5"
    assert response.text == "too many live streams\n"
    assert hub.refused_streams == 1
    exposition = generate_latest(metrics_of(client).registry).decode()
    assert f'{UI_STREAMS.name}{{host="{HOST}"}} 16.0' in exposition
    assert f'{UI_STREAMS_REFUSED.name}{{host="{HOST}"}} 1.0' in exposition
    for subscription in held:
        assert subscription is not None
        subscription.close()


def test_a_stopping_app_refuses_new_streams_with_503(client: TestClient) -> None:
    hub = hub_of(client)
    portal = client.portal
    assert portal is not None
    portal.call(hub.close)

    response = client.get(live.STREAM_PATH)

    assert response.status_code == 503
    assert response.text == "the app is stopping\n"
    assert hub.refused_streams == 0


def metrics_of(client: TestClient) -> AppMetrics:
    app = client.app
    assert isinstance(app, FastAPI)
    metrics = app.state.metrics
    assert isinstance(metrics, AppMetrics)
    return metrics


def test_a_finished_stream_frees_its_slot(client: TestClient) -> None:
    stream(client)

    assert hub_of(client).open_streams == 0


def test_the_stream_never_reads_the_database(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the stream touched {name}")

    app.state.reads = Exploding()
    app.state.read_repository = Exploding()
    app.state.repository = Exploding()

    def during(hub: ChangeHub) -> None:
        hub.notify_threadsafe(ALL)
        time.sleep(SETTLE_S)

    response = stream(client, during=during)

    assert response.status_code == 200
    assert "data: all" in response.text


def test_foreign_pages_cannot_open_the_stream(client: TestClient) -> None:
    assert client.get(live.STREAM_PATH, headers={"host": "evil.example"}).status_code == 400
    for site in ("cross-site", "same-site"):
        foreign = {"sec-fetch-site": site, "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}
        response = client.get(live.STREAM_PATH, headers=foreign)
        assert response.status_code == 403
    assert hub_of(client).open_streams == 0


def test_our_own_pages_can_open_the_stream(client: TestClient) -> None:
    own = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}

    response = stream(client, headers=own)

    assert response.status_code == 200
    assert response.text.startswith("retry: 5000\n\n")


def test_the_app_closes_the_hub_when_it_stops(settings: Settings) -> None:
    with start(settings) as client:
        hub = hub_of(client)
        assert hub.is_open
    assert not hub.is_open


# A real server: uvicorn waits for open connections before the lifespan's shutdown, so
# the server must end the streams itself, or every stop would wait for the graceful
# timeout and then cancel the stream ("Cancel 1 running task(s)").


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_real_server_with_an_open_stream_stops_at_once(settings: Settings) -> None:
    app = create_app(settings, serve_metrics=False, sweeps=False, reconcile=False)
    server = server_main.build_server(settings, app)
    records = Records()
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(records)
    listening = socket.socket()
    listening.bind(("127.0.0.1", 0))
    port = listening.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started, 20)
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as http,
            http.stream("GET", live.STREAM_PATH) as response,
        ):
            assert response.status_code == 200
            chunks = response.iter_raw()
            assert next(chunks) == b"retry: 5000\n\n"
            stopping = time.monotonic()
            server.should_exit = True
            # The stream ends cleanly: the server closes it, nothing is cut.
            rest = b"".join(chunks)
        thread.join(20)
        elapsed = time.monotonic() - stopping
    finally:
        server.should_exit = True
        thread.join(20)
        uvicorn_logger.removeHandler(records)
        listening.close()

    assert not thread.is_alive()
    assert rest == b""
    # Without the early close: the 5 s graceful timeout, then a cancellation.
    assert elapsed < 2, elapsed
    assert not [message for message in records.messages if "Cancel" in message]
    assert not hub_of_app(app).is_open


def hub_of_app(app: FastAPI) -> ChangeHub:
    hub = app.state.live
    assert isinstance(hub, ChangeHub)
    return hub


# live.js: no eval, no innerHTML, no dialogs (test_static.py scans every script), and
# its reconnection, checked under node with fake browser objects.


LIVE_HARNESS: Final = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const log = [];
const listeners = {};
const timers = [];
const status = {
  textContent: '',
  getAttribute(name) { return name === 'data-stream' ? '/ui/stream?session=s' : null; },
};
const body = {
  dispatchEvent(event) { log.push('changed:' + event.type + (event.bubbles ? ':bubbles' : '')); },
};
const document = {
  readyState: 'complete', body,
  getElementById(id) { return id === 'live-status' ? status : null; },
  addEventListener() {},
};
const sources = [];
class FakeSource {
  constructor(url) {
    this.url = url; this.handlers = {}; this.closed = false;
    sources.push(this); log.push('open:' + url);
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  close() { this.closed = true; log.push('close'); }
  fire(name) { this.handlers[name]({ type: name }); }
}
class FakeEvent {
  constructor(type, init) { this.type = type; this.bubbles = Boolean(init && init.bubbles); }
}
const window = {
  EventSource: FakeSource, Event: FakeEvent,
  setTimeout(fn, ms) { timers.push([fn, ms]); log.push('wait:' + ms); return timers.length; },
  clearTimeout() {},
  addEventListener(name, handler) { listeners[name] = handler; },
};
global.Event = FakeEvent;
new Function('window', 'document', src)(window, document);
const first = sources[0];
first.fire('open');
log.push('status:' + status.textContent);
first.fire('changed');
first.fire('alerts');
first.fire('error');
log.push('status:' + status.textContent);
for (let n = 0; n < 6; n += 1) {
  const [fn] = timers.shift();
  fn();
  sources[sources.length - 1].fire('error');
}
const [fn] = timers.shift();
fn();
sources[sources.length - 1].fire('open');
log.push('status:' + status.textContent);
sources[sources.length - 1].fire('error');
listeners.pagehide();
log.push('closed:' + sources.every((s) => s.closed));
listeners.pageshow({ persisted: false });
listeners.pageshow({ persisted: true });
log.push('sources:' + sources.length);
console.log(JSON.stringify(log));
"""


def test_live_js_reconnects_with_backoff_and_refetches_on_open() -> None:
    node = shutil.which("node")
    if node is None:
        # CI runners have node: there a missing node is a failure, not a skip.
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [node, "-e", LIVE_HARNESS, str(LIVE_JS)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    log = json.loads(completed.stdout)
    paused = "status:Live updates paused. Reconnecting\u2026"
    url = "open:/ui/stream?session=s"
    alerts = "changed:ccobs:alerts:bubbles"

    assert log == [
        url,
        # Every open, the first one included, makes the page refetch, the alerts too.
        "changed:ccobs:changed",
        alerts,
        "status:",
        "changed:ccobs:changed",
        # An alerts ping is its own event, which bubbles to document (ADR 0044).
        alerts,
        # An error closes the source (no browser retry storm on a 503) and waits.
        "close",
        "wait:5000",
        paused,
        url,
        "close",
        "wait:10000",
        url,
        "close",
        "wait:20000",
        url,
        "close",
        "wait:40000",
        url,
        "close",
        "wait:60000",
        url,
        "close",
        "wait:60000",
        url,
        "close",
        "wait:60000",
        url,
        "changed:ccobs:changed",
        alerts,
        "status:",
        # After an open the backoff starts over.
        "close",
        "wait:5000",
        "closed:true",
        url,
        "sources:9",
    ]
