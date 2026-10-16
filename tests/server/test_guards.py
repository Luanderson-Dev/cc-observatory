"""The app's browser defences (ADRs 0001 and 0027): the Host allowlist, no CORS and the
body cap.

A web page can make the user's browser send requests to localhost. A ``Host`` header
outside the allowlist (DNS rebinding) is refused before the request reaches a route:
with an empty 204 on the hook route, which never answers anything else (CLAUDE.md), and
with a 400 elsewhere. No response carries an ``Access-Control-Allow-*`` header, so a
cross-site page cannot read any of them or pass a CORS preflight.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server import app as server_app
from ccobs.server.config import Settings
from ccobs.server.guards import (
    ALLOWED_HOSTS,
    ClientDisconnectedError,
    declared_length,
    host_allowed,
    is_json_media_type,
    read_body,
)
from ccobs.server.ingest import HookQueue

# The app without its metrics listener (only tests/server/test_metrics.py opens sockets)
# and without its sweeper (tests/server/test_sweeps.py covers it).
create_app: Final = functools.partial(
    server_app.create_app, serve_metrics=False, sweeps=False, reconcile=False
)
KIB: Final = 1024
BODY_MAX: Final = 64 * KIB
GOOD_HOST: Final = "127.0.0.1:4300"
JSON: Final = {"content-type": "application/json"}


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(data_dir=tmp_path / "data", hook_body_max_bytes=BODY_MAX)


@pytest.fixture
def queue(settings: Settings) -> HookQueue:
    return HookQueue(settings.hook_queue_max_items, settings.hook_queue_max_bytes)


@pytest.fixture
def app(settings: Settings, queue: HookQueue) -> FastAPI:
    return create_app(settings, queue=queue)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=f"http://{GOOD_HOST}") as test_client:
        yield test_client


@dataclass
class Exchange:
    status: int
    headers: dict[bytes, bytes]
    body: bytes
    receive_calls: int


async def _exchange(
    app: FastAPI,
    *,
    method: str,
    path: str,
    headers: Sequence[tuple[bytes, bytes]],
    chunks: Sequence[bytes],
) -> Exchange:
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    receive_calls = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4300),
    }
    await app(scope, receive, send)
    start = sent[0]
    return Exchange(
        status=start["status"],
        headers=dict(start.get("headers", [])),
        body=b"".join(message.get("body", b"") for message in sent[1:]),
        receive_calls=receive_calls,
    )


def raw_request(
    app: FastAPI,
    *,
    method: str = "POST",
    path: str = HOOK_PATH,
    headers: Sequence[tuple[bytes, bytes]] = (),
    chunks: Sequence[bytes] = (b"{}",),
) -> Exchange:
    """Call the app as an ASGI server would, with full control over the headers."""
    return asyncio.run(_exchange(app, method=method, path=path, headers=headers, chunks=chunks))


def assert_neutral(response: httpx2.Response) -> None:
    assert response.status_code == 204
    assert response.content == b""
    assert "content-type" not in response.headers


def assert_no_cors(headers: httpx2.Headers) -> None:
    assert [name for name in headers if name.lower().startswith("access-control-")] == []


# host_allowed.


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "localhost:4300",
        "LOCALHOST",
        "LocalHost:4300",
        "127.0.0.1",
        "127.0.0.1:4300",
        "127.0.0.1:1",
        "[::1]",
        "[::1]:4300",
        "[::1]:65535",
    ],
)
def test_allowed_hosts(value: str) -> None:
    assert host_allowed(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "evil.example",
        "evil.example:4300",
        "localhost.evil.example",
        "localhost.evil.example:4300",
        "evil.localhost",
        "localhost.",
        "127.0.0.2",
        "0.0.0.0:4300",
        "::1",
        "[::1",
        "[::2]:4300",
        "[::1]x",
        "[::1]:",
        "localhost:",
        "localhost:http",
        "localhost:4300:4300",
        "localhost:123456",
        "localhost:0",
        "localhost:65536",
        "localhost:+80",
        "user@localhost",
        " localhost",
        "localhost ",
    ],
)
def test_disallowed_hosts(value: str) -> None:
    assert not host_allowed(value)


def test_the_allowlist_is_the_one_of_adr_0001() -> None:
    assert frozenset({"localhost", "127.0.0.1", "[::1]"}) == ALLOWED_HOSTS


# Host on the other routes: 400.


@pytest.mark.parametrize(
    "host", ["localhost:4300", "127.0.0.1:4300", "[::1]:4300", "LOCALHOST", "localhost"]
)
def test_healthz_accepts_the_allowed_hosts(client: TestClient, host: str) -> None:
    response = client.get("/healthz", headers={"host": host})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "testserver"])
@pytest.mark.parametrize("path", ["/healthz", "/docs", "/openapi.json", "/nowhere"])
def test_other_routes_refuse_a_bad_host_with_400(client: TestClient, host: str, path: str) -> None:
    response = client.get(path, headers={"host": host})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/plain")
    assert len(response.content) < 64
    assert_no_cors(response.headers)


def test_other_routes_refuse_a_missing_host_with_400(app: FastAPI) -> None:
    exchange = raw_request(app, method="GET", path="/healthz", chunks=(b"",))

    assert exchange.status == 400
    assert exchange.receive_calls == 0


def test_other_routes_refuse_two_host_headers(app: FastAPI) -> None:
    headers = [(b"host", b"localhost"), (b"host", b"evil.example")]
    exchange = raw_request(app, method="GET", path="/healthz", headers=headers, chunks=(b"",))

    assert exchange.status == 400


def test_a_bad_host_on_other_routes_is_not_a_hook_drop(
    client: TestClient, queue: HookQueue
) -> None:
    client.get("/healthz", headers={"host": "evil.example"})

    assert queue.drops["bad_host"] == 0


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_api_docs_are_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


# Host on the hook route: 204, counted, body unread.


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "evil.example:4300"])
def test_the_hook_route_drops_a_bad_host_with_204(
    client: TestClient, queue: HookQueue, host: str
) -> None:
    response = client.post(HOOK_PATH, content=b"{}", headers={**JSON, "host": host})

    assert_neutral(response)
    assert queue.depth == 0
    assert queue.drops["bad_host"] == 1


def test_the_hook_route_drops_a_missing_host_without_reading_the_body(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    exchange = raw_request(app, headers=[(b"content-type", b"application/json")])

    assert exchange.status == 204
    assert exchange.body == b""
    assert b"content-type" not in exchange.headers
    assert exchange.receive_calls == 0
    assert queue.depth == 0
    assert queue.drops["bad_host"] == 1


def test_a_bad_host_on_the_hook_route_is_not_read(
    client: TestClient, app: FastAPI, queue: HookQueue
) -> None:
    headers = [(b"host", b"evil.example"), (b"content-type", b"application/json")]
    exchange = raw_request(app, headers=headers, chunks=(b"{", b"}"))

    assert exchange.status == 204
    assert exchange.receive_calls == 0
    assert queue.drops["bad_host"] == 1


def _websocket(app: FastAPI, headers: Sequence[tuple[bytes, bytes]], path: str) -> list[Message]:
    incoming: list[Message] = [{"type": "websocket.connect"}]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4300),
        "subprotocols": [],
    }
    asyncio.run(app(scope, receive, send))
    return sent


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"evil.example"), (b"origin", b"https://evil.example")],
        [(b"host", b"localhost.evil.example")],
        [],
    ],
    ids=["evil", "suffix", "missing"],
)
@pytest.mark.parametrize("path", ["/healthz", HOOK_PATH, "/ws"])
def test_a_websocket_with_a_bad_host_is_closed_with_1008(
    app: FastAPI, queue: HookQueue, headers: list[tuple[bytes, bytes]], path: str
) -> None:
    sent = _websocket(app, headers, path)

    assert sent == [{"type": "websocket.close", "code": 1008}]
    # Not a hook: nothing to count.
    assert queue.drops["bad_host"] == 0


def test_a_websocket_with_a_good_host_reaches_the_app(app: FastAPI) -> None:
    sent = _websocket(app, [(b"host", GOOD_HOST.encode())], "/ws")

    # The app has no websocket route, so Starlette closes it, with its own code.
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0].get("code") != 1008


def test_lifespan_passes_through_the_guard(settings: Settings) -> None:
    # TestClient's context runs the lifespan through every middleware.
    with TestClient(create_app(settings), base_url=f"http://{GOOD_HOST}") as test_client:
        assert test_client.get("/healthz").status_code == 200

    assert settings.database_path.exists()


# CORS: none.


def test_no_cors_on_any_response(client: TestClient) -> None:
    origin = {"origin": "https://evil.example"}
    responses = [
        client.post(HOOK_PATH, content=b"{}", headers={**JSON, **origin}),
        client.post(HOOK_PATH, content=b"{}", headers={**JSON, **origin, "host": "evil.example"}),
        client.get(HOOK_PATH, headers=origin),
        client.get("/healthz", headers=origin),
        client.get("/healthz", headers={**origin, "host": "evil.example"}),
        client.get("/nowhere", headers=origin),
        client.options("/healthz", headers=origin),
    ]

    for response in responses:
        assert_no_cors(response.headers)


def test_a_preflight_to_the_hook_route_gets_204_without_cors(
    client: TestClient, queue: HookQueue
) -> None:
    response = client.options(
        HOOK_PATH,
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert_neutral(response)
    assert_no_cors(response.headers)
    assert queue.depth == 0


def test_a_preflight_elsewhere_gets_no_cors(client: TestClient) -> None:
    response = client.options(
        "/healthz",
        headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
    )

    assert response.status_code == 405
    assert_no_cors(response.headers)


# The body cap: read_body and declared_length.


def _receiver(chunks: Sequence[bytes]) -> tuple[Receive, list[int]]:
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    calls = [0]

    async def receive() -> Message:
        calls[0] += 1
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    return receive, calls


def test_read_body_joins_the_chunks() -> None:
    receive, calls = _receiver([b"ab", b"", b"cd"])

    assert asyncio.run(read_body(receive, limit=4)) == b"abcd"
    assert calls == [3]


def test_read_body_accepts_a_body_at_the_limit() -> None:
    receive, _ = _receiver([b"x" * 10])

    assert asyncio.run(read_body(receive, limit=10)) == b"x" * 10


def test_read_body_stops_reading_past_the_limit() -> None:
    receive, calls = _receiver([b"x" * 4] * 5)

    assert asyncio.run(read_body(receive, limit=10)) is None
    assert calls == [3]


def test_read_body_accepts_a_message_without_a_body() -> None:
    async def receive() -> Message:
        return {"type": "http.request"}

    assert asyncio.run(read_body(receive, limit=10)) == b""


def test_read_body_raises_on_a_disconnect() -> None:
    receive, _ = _receiver([b"ab", b"cd"])

    async def disconnecting() -> Message:
        message = await receive()
        return message if message["body"] == b"ab" else {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnectedError):
        asyncio.run(read_body(disconnecting, limit=10))


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], None),
        ([(b"host", b"localhost")], None),
        ([(b"content-length", b"0")], 0),
        ([(b"content-length", b"123")], 123),
        ([(b"content-length", b"12"), (b"content-length", b"12")], 12),
    ],
)
def test_declared_length(headers: list[tuple[bytes, bytes]], expected: int | None) -> None:
    assert declared_length(headers) == expected


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"abc")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"+1")],
        [(b"content-length", b"")],
        [(b"content-length", b"1 2")],
        [(b"content-length", b"12"), (b"content-length", b"13")],
    ],
)
def test_declared_length_refuses_malformed_values(headers: list[tuple[bytes, bytes]]) -> None:
    with pytest.raises(ValueError, match="Content-Length"):
        declared_length(headers)


# The media type.


@pytest.mark.parametrize(
    "value",
    [
        b"application/json",
        b"Application/JSON",
        b"application/json; charset=utf-8",
        b"application/json;charset=UTF-8",
        b" application/json ",
    ],
)
def test_json_media_types(value: bytes) -> None:
    assert is_json_media_type(value)


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"text/plain",
        b"text/plain; charset=utf-8",
        b"application/jsonx",
        b"application/json-seq",
        b"application/x-www-form-urlencoded",
        b"multipart/form-data; boundary=x",
        b"application/ld+json",
        b"application",
        b"\xff\xfe",
    ],
)
def test_other_media_types(value: bytes) -> None:
    assert not is_json_media_type(value)
