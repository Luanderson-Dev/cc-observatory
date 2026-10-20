"""The app's browser defences (ADRs 0001, 0027, 0034 and 0045).

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
- ``SecurityHeaders`` adds a strict Content Security Policy, ``nosniff``, no referrer,
  same-origin resource and opener policies and ``Cache-Control: no-store`` to every
  response (``no-store`` only outside ``/static/``, whose files hold no personal data
  and get a cache policy of their own; a header that a route set is kept as is).
- ``FetchMetadataGuard`` answers a short 403 to any browser request from another site,
  same-site included (other ``localhost`` ports, such as Grafana's), unless it is a
  top-level ``GET`` navigation, and closes such websockets. Clients that send no
  ``Sec-Fetch-Site`` (curl, Prometheus, tests) pass.
- ``UnsafeMethodGuard`` checks every ``POST`` to the state-changing routes it is given
  (the alert settings routes, ADR 0045) with ``post_refusal``: same-origin Fetch
  Metadata when sent, an ``Origin`` equal to ``<scheme>://<Host>``, ``HX-Request:
  true`` and a urlencoded form; a short 403 (or 415 for the media type) otherwise.

The hook route (``HOOK_PATHS``) is outside all of them: its answer stays the neutral
empty 204 with no headers, whatever the request says (CLAUDE.md, ADR 0011).

Pure ASGI: Starlette's ``BaseHTTPMiddleware`` adds a task and a stream per request,
which the hook route's latency budget (< 50 ms, CLAUDE.md) does not need.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ccobs.domain.hook_config import HOOK_PATH
from ccobs.server.ingest import HookQueue

__all__ = [
    "ALLOWED_HOSTS",
    "CONTENT_SECURITY_POLICY",
    "HOOK_PATHS",
    "SECURITY_HEADERS",
    "STATIC_PREFIX",
    "ClientDisconnectedError",
    "FetchMetadataGuard",
    "HostGuard",
    "SecurityHeaders",
    "UnsafeMethodGuard",
    "declared_length",
    "fetch_allowed",
    "header_values",
    "host_allowed",
    "is_json_media_type",
    "post_refusal",
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

# Only our own files: no inline script or style (htmx's indicator style is turned off
# with htmx.config.includeIndicatorStyles), no plugin, no <base>, no form submission and
# no framing. ``data:`` images allow small inline icons in CSS.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    # A page on another origin cannot load a response as an image, script or style,
    # not even with a no-cors request.
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
)
_NO_STORE: Final = (b"cache-control", b"no-store")
# Static files (mounted here from stage 7) hold no personal data, so they get no
# ``no-store`` and can carry a cache policy of their own. Every other response may hold
# ``cwd`` or IDs, so the browser never stores it.
STATIC_PREFIX: Final = "/static/"
_FORBIDDEN_BODY: Final = b"forbidden\n"
_UNSUPPORTED_BODY: Final = b"unsupported media type\n"
_FORBIDDEN: Final = 403
_UNSUPPORTED: Final = 415
_POST: Final = "POST"
_FORM: Final = "application/x-www-form-urlencoded"
_SAME_ORIGIN: Final = b"same-origin"
# Sec-Fetch-Site values of requests started by our own pages or by the user.
_OWN_SITES: Final = frozenset({b"same-origin", b"none"})
# Another site, or another port or scheme of ours: ``localhost:4330`` (Grafana) is
# same-site to ``localhost:4300``, since a site ignores the port.
_FOREIGN_SITES: Final = frozenset({b"same-site", b"cross-site"})


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
    return _is_media_type(value, _JSON)


def _is_media_type(value: bytes, expected: str) -> bool:
    media_type = value.split(b";", 1)[0].strip()
    return media_type.lower() == expected.encode()


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


def fetch_allowed(headers: Iterable[tuple[bytes, bytes]], method: str | None) -> bool:
    """Whether Fetch Metadata lets a request through.

    No ``Sec-Fetch-Site``: not a browser that sends Fetch Metadata (curl, Prometheus),
    allowed. ``same-origin`` (our own pages) or ``none`` (a typed URL, a bookmark):
    allowed. ``same-site`` or ``cross-site``: only a top-level navigation by ``GET``
    (``Sec-Fetch-Mode: navigate`` and, when sent, ``Sec-Fetch-Dest: document``), so a
    link from Grafana still opens a page, while images, scripts, fetches, event streams,
    frames and form submissions from another page do not. A repeated or unknown value is
    refused.
    """
    headers = list(headers)
    sites = header_values(headers, b"sec-fetch-site")
    if not sites:
        return True
    if len(sites) > 1:
        return False
    site = sites[0].lower()
    if site in _OWN_SITES:
        return True
    if site not in _FOREIGN_SITES:
        return False
    return (
        method == "GET"
        and header_values(headers, b"sec-fetch-mode") == [b"navigate"]
        and header_values(headers, b"sec-fetch-dest") in ([], [b"document"])
    )


async def _send_text(send: Send, status: int, body: bytes, *, secure: bool = False) -> None:
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
    ]
    if secure:
        headers.extend((*SECURITY_HEADERS, _NO_STORE))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_bad_host(send: Send) -> None:
    # The Host guard is outermost, so its 400 carries the security headers itself.
    await _send_text(send, 400, _BAD_HOST_BODY, secure=True)


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


class SecurityHeaders:
    """Adds ``SECURITY_HEADERS``, and ``Cache-Control: no-store`` outside
    ``STATIC_PREFIX``, to every HTTP response but the hook route's.

    A header that the route already set (in any case) is kept and never duplicated. The
    lifespan and websockets pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in HOOK_PATHS:
            await self.app(scope, receive, send)
            return
        added = SECURITY_HEADERS
        if not scope["path"].startswith(STATIC_PREFIX):
            added = (*SECURITY_HEADERS, _NO_STORE)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(header for header in added if header[0] not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class FetchMetadataGuard:
    """Refuses browser requests from another site that are not top-level ``GET``
    navigations (``fetch_allowed``): HTTP with a short 403, websockets with code 1008
    before the handshake. The hook route and the lifespan pass through, and the body of
    a refused request is never read.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] not in ("http", "websocket")
            or scope["path"] in HOOK_PATHS
            or fetch_allowed(scope["headers"], scope.get("method"))
        ):
            await self.app(scope, receive, send)
            return
        # Neither the headers nor the path are logged: both come from the request.
        if scope["type"] == "websocket":
            _log.debug("websocket refused: Fetch Metadata")
            await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
        else:
            _log.debug("request refused: Fetch Metadata")
            await _send_text(send, 403, _FORBIDDEN_BODY)


def post_refusal(headers: Iterable[tuple[bytes, bytes]], scheme: str) -> int | None:
    """Why a ``POST`` to a state-changing route is refused (ADR 0045), or None when it
    passes. Each check takes exactly one value of its header, in this order:

    1. ``Sec-Fetch-Site``, when sent, is ``same-origin`` (any case): never another
       site, another port of ours or a typed URL;
    2. ``Origin`` is ``<scheme>://<Host>`` (any case), with ``Host`` already on the
       allowlist (``HostGuard``): never ``null``, missing, another port or scheme;
    3. ``HX-Request`` is ``true``: a custom header that a page of another origin can
       only send after a CORS preflight, which fails here (no CORS headers at all);
    4. ``Content-Type`` is ``application/x-www-form-urlencoded``, parameters allowed:
       what htmx's ``hx-post`` sends.

    The first three failing answer 403; the last, 415.
    """
    headers = list(headers)
    sites = header_values(headers, b"sec-fetch-site")
    if sites and [site.lower() for site in sites] != [_SAME_ORIGIN]:
        return _FORBIDDEN
    hosts = header_values(headers, b"host")
    origins = header_values(headers, b"origin")
    if len(hosts) != 1 or len(origins) != 1:
        return _FORBIDDEN
    expected = f"{scheme}://{hosts[0].decode('latin-1')}".lower()
    if origins[0].decode("latin-1").lower() != expected:
        return _FORBIDDEN
    if header_values(headers, b"hx-request") != [b"true"]:
        return _FORBIDDEN
    content_types = header_values(headers, b"content-type")
    if len(content_types) != 1 or not _is_media_type(content_types[0], _FORM):
        return _UNSUPPORTED
    return None


class UnsafeMethodGuard:
    """Checks every ``POST`` to ``paths``, the state-changing routes, with
    ``post_refusal`` (ADR 0045), and answers a refusal with a short 403 or 415 whose
    body never quotes the request, without reading the body. Every other request, the
    lifespan and websockets pass through: other methods on those paths, and any method
    elsewhere, get the routes' own answers (405 where only ``GET`` is served).
    """

    def __init__(self, app: ASGIApp, *, paths: frozenset[str]) -> None:
        self.app = app
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != _POST
            or scope["path"] not in self.paths
            or (status := post_refusal(scope["headers"], scope["scheme"])) is None
        ):
            await self.app(scope, receive, send)
            return
        # Neither the headers nor the path are logged: both come from the request.
        _log.debug("state-changing request refused (%d)", status)
        body = _FORBIDDEN_BODY if status == _FORBIDDEN else _UNSUPPORTED_BODY
        await _send_text(send, status, body)
