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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server import api
from ccobs.server import app as server_app
from ccobs.server.config import Settings
from ccobs.server.guards import (
    ALLOWED_HOSTS,
    HOOK_PATHS,
    ClientDisconnectedError,
    FetchMetadataGuard,
    HostGuard,
    SecurityHeaders,
    declared_length,
    header_values,
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
    raw_headers: list[tuple[bytes, bytes]]


async def _exchange(
    app: ASGIApp,
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
        raw_headers=list(start.get("headers", [])),
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


# Response headers (ADR 0034): on every response but the hook route's.

CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
EXPECTED_HEADERS: Final = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}
ADDED_HEADERS: Final = (*EXPECTED_HEADERS, "cache-control")
SESSION_ID: Final = "0b6f2b1e-4c1a-4d55-9a57-3f0c3f0f8a10"


def assert_security_headers(headers: httpx2.Headers) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert headers.get_list(name) == [value], name
    assert headers.get_list("cache-control") == ["no-store"]


def assert_no_added_headers(headers: httpx2.Headers) -> None:
    assert [name for name in ADDED_HEADERS if name in headers] == []


def fetch_metadata(mode: str, dest: str, *, site: str = "cross-site") -> dict[str, str]:
    return {"sec-fetch-site": site, "sec-fetch-mode": mode, "sec-fetch-dest": dest}


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/api/v1/sessions",
        f"/api/v1/sessions/{SESSION_ID}",
        f"/api/v1/sessions/{SESSION_ID}/events",
        "/api/v1/sessions?limit=0",
        "/nowhere",
    ],
)
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code in (200, 404, 422)
    assert_security_headers(response.headers)


def test_a_405_carries_the_security_headers(client: TestClient) -> None:
    response = client.post("/healthz")

    assert response.status_code == 405
    assert_security_headers(response.headers)


def test_a_bad_host_400_carries_the_security_headers(client: TestClient) -> None:
    response = client.get("/api/v1/sessions", headers={"host": "evil.example"})

    assert response.status_code == 400
    assert_security_headers(response.headers)


def test_an_unhandled_500_carries_no_security_headers_and_a_generic_body(
    settings: Settings,
) -> None:
    # Starlette's ServerErrorMiddleware wraps every user middleware, so its 500 is sent
    # after SecurityHeaders has seen no response start: no headers, a generic body that
    # quotes neither the request nor the exception (ADR 0034).
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("detail that must not leak")

    with TestClient(
        app, base_url=f"http://{GOOD_HOST}", raise_server_exceptions=False
    ) as test_client:
        response = test_client.get("/boom")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert_no_added_headers(response.headers)


def test_a_fetch_metadata_403_carries_the_security_headers(client: TestClient) -> None:
    response = client.get("/healthz", headers=fetch_metadata("no-cors", "image"))

    assert response.status_code == 403
    assert_security_headers(response.headers)


@pytest.mark.parametrize("path", sorted(HOOK_PATHS))
@pytest.mark.parametrize("host", [GOOD_HOST, "evil.example"])
def test_the_hook_route_gets_no_added_header(client: TestClient, path: str, host: str) -> None:
    response = client.post(path, content=b"{}", headers={**JSON, "host": host})

    assert_neutral(response)
    assert_no_added_headers(response.headers)


async def _headers_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Answers with the headers listed in the request's ``x-set`` header."""
    extra: list[tuple[bytes, bytes]] = []
    for value in header_values(scope["headers"], b"x-set"):
        for item in value.split(b","):
            name, _, header = item.partition(b"=")
            extra.append((name, header))
    await send({"type": "http.response.start", "status": 200, "headers": extra})
    await send({"type": "http.response.body", "body": b"ok"})


def _wrapped(path: str, headers: Sequence[tuple[bytes, bytes]] = ()) -> Exchange:
    return asyncio.run(
        _exchange(
            SecurityHeaders(_headers_app), method="GET", path=path, headers=headers, chunks=(b"",)
        )
    )


def _values(exchange: Exchange, name: bytes) -> list[bytes]:
    return [value for key, value in exchange.raw_headers if key.lower() == name]


def test_a_header_the_route_set_is_neither_duplicated_nor_overwritten() -> None:
    set_by_route = b"cache-control=max-age=60,content-security-policy=default-src 'self'"
    exchange = _wrapped("/page", [(b"x-set", set_by_route)])

    assert _values(exchange, b"cache-control") == [b"max-age=60"]
    assert _values(exchange, b"content-security-policy") == [b"default-src 'self'"]
    assert _values(exchange, b"x-content-type-options") == [b"nosniff"]


def test_header_names_set_by_the_route_match_in_any_case() -> None:
    exchange = _wrapped("/page", [(b"x-set", b"Cache-Control=max-age=60")])

    assert _values(exchange, b"cache-control") == [b"max-age=60"]


@pytest.mark.parametrize("path", ["/static/vendor/htmx-2.0.11.min.js", "/static/css/app.css"])
def test_static_files_get_the_security_headers_but_no_cache_policy(path: str) -> None:
    exchange = _wrapped(path)

    assert _values(exchange, b"content-security-policy") == [CSP.encode()]
    assert _values(exchange, b"x-content-type-options") == [b"nosniff"]
    assert _values(exchange, b"cache-control") == []


@pytest.mark.parametrize("path", ["/staticx", "/static", "/ui/static/x.css"])
def test_a_path_outside_static_gets_no_store(path: str) -> None:
    assert _values(_wrapped(path), b"cache-control") == [b"no-store"]


async def _nothing() -> Message:
    return {"type": "http.disconnect"}


async def _nothing_sent(message: Message) -> None:
    raise AssertionError("nothing is sent")


@pytest.mark.parametrize("middleware", [SecurityHeaders, FetchMetadataGuard])
@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
def test_the_lifespan_and_good_websockets_pass_through(
    middleware: type[SecurityHeaders | FetchMetadataGuard], kind: str
) -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    scope = {"type": kind, "path": "/ws", "headers": []}
    asyncio.run(middleware(inner)(scope, _nothing, _nothing_sent))

    assert seen == [kind]


# Fetch Metadata (ADR 0034).


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize(
    ("mode", "dest"),
    [
        ("no-cors", "image"),
        ("no-cors", "script"),
        ("no-cors", "style"),
        ("no-cors", "empty"),
        ("cors", "empty"),
        ("navigate", "iframe"),
        ("navigate", "frame"),
        ("navigate", "object"),
    ],
)
@pytest.mark.parametrize(
    "path", ["/healthz", "/api/v1/sessions", "/static/css/app.css", "/ui/stream", "/nowhere"]
)
def test_foreign_subresource_requests_are_refused_with_403(
    client: TestClient, site: str, mode: str, dest: str, path: str
) -> None:
    response = client.get(path, headers=fetch_metadata(mode, dest, site=site))

    assert response.status_code == 403
    assert response.content == b"forbidden\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert_no_cors(response.headers)


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "HEAD"])
def test_foreign_navigations_other_than_get_are_refused(
    client: TestClient, site: str, method: str
) -> None:
    response = client.request(
        method, "/api/v1/sessions", headers=fetch_metadata("navigate", "document", site=site)
    )

    assert response.status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
        {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-user": "?1"},
        # A browser that sends Sec-Fetch-Mode but not Sec-Fetch-Dest.
        {"sec-fetch-mode": "navigate"},
    ],
    ids=["link", "user-activated", "no-dest"],
)
def test_a_foreign_top_level_navigation_is_allowed(
    client: TestClient, site: str, headers: dict[str, str]
) -> None:
    response = client.get("/healthz", headers={"sec-fetch-site": site, **headers})

    assert response.status_code == 200
    assert_security_headers(response.headers)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        fetch_metadata("cors", "empty", site="same-origin"),
        fetch_metadata("no-cors", "script", site="same-origin"),
        fetch_metadata("navigate", "document", site="same-origin"),
        fetch_metadata("navigate", "document", site="none"),
        {"sec-fetch-mode": "cors"},
        {"sec-fetch-dest": "image"},
        {"sec-fetch-site": "Same-Origin"},
    ],
    ids=[
        "none-sent",
        "same-origin-fetch",
        "same-origin-script",
        "same-origin-navigation",
        "typed-url",
        "mode-only",
        "dest-only",
        "any-case",
    ],
)
def test_own_and_non_browser_requests_are_allowed(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.get("/healthz", headers=headers).status_code == 200


@pytest.mark.parametrize("value", ["", "cross-origin", "evil", "same-origin, cross-site"])
def test_an_unknown_sec_fetch_site_is_refused(client: TestClient, value: str) -> None:
    response = client.get("/healthz", headers={"sec-fetch-site": value, "sec-fetch-mode": "cors"})

    assert response.status_code == 403


def test_two_sec_fetch_site_headers_are_refused(app: FastAPI) -> None:
    headers = [
        (b"host", GOOD_HOST.encode()),
        (b"sec-fetch-site", b"same-origin"),
        (b"sec-fetch-site", b"same-origin"),
    ]
    exchange = raw_request(app, method="GET", path="/healthz", headers=headers, chunks=(b"",))

    assert exchange.status == 403
    assert exchange.receive_calls == 0


@pytest.mark.parametrize("path", sorted(HOOK_PATHS))
@pytest.mark.parametrize("mode", ["no-cors", "cors", "navigate"])
def test_the_hook_route_ignores_fetch_metadata(
    client: TestClient, queue: HookQueue, path: str, mode: str
) -> None:
    response = client.post(path, content=b"{}", headers={**JSON, **fetch_metadata(mode, "empty")})

    assert_neutral(response)
    assert_no_added_headers(response.headers)
    assert queue.depth == 1


@pytest.mark.parametrize("site", [b"cross-site", b"same-site"])
def test_a_foreign_websocket_is_closed_with_1008(app: FastAPI, site: bytes) -> None:
    headers = [
        (b"host", GOOD_HOST.encode()),
        (b"sec-fetch-site", site),
        (b"sec-fetch-mode", b"websocket"),
    ]

    assert _websocket(app, headers, "/ws") == [{"type": "websocket.close", "code": 1008}]


def test_a_same_origin_websocket_reaches_the_app(app: FastAPI) -> None:
    headers = [
        (b"host", GOOD_HOST.encode()),
        (b"sec-fetch-site", b"same-origin"),
        (b"sec-fetch-mode", b"websocket"),
    ]
    sent = _websocket(app, headers, "/ws")

    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0].get("code") != 1008


def test_the_middlewares_wrap_in_order(app: FastAPI) -> None:
    # Outermost first: the Host guard, then the headers (so that a 403 carries them),
    # then Fetch Metadata.
    classes: list[object] = [middleware.cls for middleware in app.user_middleware]

    assert classes == [
        HostGuard,
        SecurityHeaders,
        FetchMetadataGuard,
    ]


# Read-only (ADR 0034): every route but the hook route answers only GET.


# The routers that create_app includes: FastAPI lists an included router in app.routes
# as one entry without a path, so their routes are read from the routers themselves.
INCLUDED_ROUTERS: Final = (api.router,)


def _route_paths(app: FastAPI) -> list[str]:
    routes = [*app.routes, *(route for router in INCLUDED_ROUTERS for route in router.routes)]
    paths = []
    for route in routes:
        path: str | None = getattr(route, "path", None)
        if path is None or path in HOOK_PATHS:
            continue
        paths.append(path.replace("{session_id}", SESSION_ID))
    return sorted(paths)


def test_the_read_only_check_sees_every_route(app: FastAPI) -> None:
    paths = _route_paths(app)

    assert "/healthz" in paths
    assert f"/api/v1/sessions/{SESSION_ID}/events" in paths
    assert not any("{" in path for path in paths)
    # A router included without being added to INCLUDED_ROUTERS fails here.
    without_path = [route for route in app.routes if getattr(route, "path", None) is None]
    assert len(without_path) == len(INCLUDED_ROUTERS)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_route_but_the_hook_route_is_read_only(
    client: TestClient, app: FastAPI, method: str
) -> None:
    for path in _route_paths(app):
        response = client.request(method, path)

        assert response.status_code == 405, path
        assert_security_headers(response.headers)
