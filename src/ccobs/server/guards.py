"""The app's browser defences (ADRs 0001 and 0027).

Any web page open in the user's browser can send requests to localhost, so:

- ``HostGuard`` refuses every request whose ``Host`` header is not ``localhost``,
  ``127.0.0.1`` or ``[::1]`` (with any port or none) before it reaches a route, which
  defeats DNS rebinding. On the hook route the answer is the neutral empty 204, since
  that route never answers anything else (CLAUDE.md, ADR 0011), and the body is never
  read; elsewhere it is a short 400.
- ``is_json_media_type`` backs the hook route's ``Content-Type`` rule: a cross-site page
  can only post ``application/json`` after a CORS preflight, and the app sends no CORS
  headers at all, so the preflight fails.
- ``declared_length`` and ``read_body`` cap the hook body without reading past the cap.

Pure ASGI: Starlette's ``BaseHTTPMiddleware`` adds a task and a stream per request,
which the hook route's latency budget (< 50 ms, CLAUDE.md) does not need.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server.ingest import HookQueue

__all__ = [
    "ALLOWED_HOSTS",
    "HOOK_PATHS",
    "ClientDisconnectedError",
    "HostGuard",
    "declared_length",
    "header_values",
    "host_allowed",
    "is_json_media_type",
    "read_body",
    "send_empty",
]

_log = logging.getLogger(__name__)

ALLOWED_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "[::1]"})
# A Host header: a bracketed IPv6 literal or a name without colons, then an optional
# port of 1 to 5 digits. Anything else (userinfo, spaces, an empty port) is refused.
_HOST: Final = re.compile(r"(?P<name>\[[^\]]*\]|[^:\[\]]+)(?::(?P<port>[0-9]{1,5}))?")
_MAX_PORT: Final = 65535
_JSON: Final = "application/json"
_BAD_HOST_BODY: Final = b"invalid Host header\n"
# The websocket close code for a policy violation (RFC 6455, section 7.4.1).
_POLICY_VIOLATION: Final = 1008
# The hook route, with and without a trailing slash: both get the hook route's neutral
# answer, since a hook URL typed with a slash must not get a redirect or a 404.
HOOK_PATHS: Final = frozenset({HOOK_PATH, HOOK_PATH + "/"})


class ClientDisconnectedError(Exception):
    """The client went away before it sent the whole body."""


def header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    """Every value of the header ``name`` (lowercase, as ASGI gives them)."""
    return [value for key, value in headers if key == name]


def host_allowed(value: str) -> bool:
    """Whether a ``Host`` header names the local machine: ``localhost`` (any case),
    ``127.0.0.1`` or ``[::1]``, with a port from 1 to 65535 or none."""
    match = _HOST.fullmatch(value)
    if match is None:
        return False
    port = match["port"]
    if port is not None and not 1 <= int(port) <= _MAX_PORT:
        return False
    return match["name"].lower() in ALLOWED_HOSTS


def is_json_media_type(value: bytes) -> bool:
    """Whether a ``Content-Type`` value is ``application/json``, with or without
    parameters such as ``charset``, in any case."""
    media_type = value.split(b";", 1)[0].strip()
    return media_type.lower() == _JSON.encode()


def declared_length(headers: Iterable[tuple[bytes, bytes]]) -> int | None:
    """The request's ``Content-Length``, None when absent.

    Raises ``ValueError`` when it is not a decimal integer or repeated with different
    values (RFC 9112, section 6.3).
    """
    values = set(header_values(headers, b"content-length"))
    if not values:
        return None
    if len(values) > 1:
        raise ValueError("conflicting Content-Length headers")
    (value,) = values
    if not value.isdigit():
        raise ValueError("malformed Content-Length header")
    return int(value)


async def read_body(receive: Receive, *, limit: int) -> bytes | None:
    """The request body, read chunk by chunk; None as soon as it passes ``limit`` bytes,
    without reading the rest. Raises ``ClientDisconnectedError`` if the client goes away."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientDisconnectedError
        chunk: bytes = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def send_empty(send: Send, status: int = 204) -> None:
    """Answer with ``status`` and no body and no headers: the neutral hook response."""
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _send_bad_host(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_BAD_HOST_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _BAD_HOST_BODY})


class HostGuard:
    """Refuses HTTP requests and websockets whose ``Host`` is missing, repeated or not
    local.

    The lifespan passes through. A refused request never reaches the app and its body is
    never read; a refused websocket is closed with code 1008 (policy violation) before
    the handshake completes.
    """

    def __init__(self, app: ASGIApp, *, queue: HookQueue) -> None:
        self.app = app
        self.queue = queue

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hosts = header_values(scope["headers"], b"host")
        if len(hosts) == 1 and host_allowed(hosts[0].decode("latin-1")):
            await self.app(scope, receive, send)
            return
        # Neither the Host value nor the path is logged: both come from the request.
        if scope["type"] == "websocket":
            _log.debug("websocket refused: Host not allowed")
            await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
        elif scope["path"] in HOOK_PATHS:
            self.queue.count_drop("bad_host")
            _log.debug("hook dropped: Host not allowed")
            await send_empty(send)
        else:
            _log.debug("request refused: Host not allowed")
            await _send_bad_host(send)
