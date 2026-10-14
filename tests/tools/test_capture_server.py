"""Tests for the capture tool's hook capture server."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.capture.capture_server import Mode, make_server


@contextmanager
def running(out: Path, mode: Mode = "ok", delay: float = 0.0) -> Iterator[int]:
    server = make_server(0, out, mode=mode, delay=delay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(
    port: int, body: bytes | Iterable[bytes], *, path: str = "/hooks/http", chunked: bool = False
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if chunked:
        headers["Transfer-Encoding"] = "chunked"
    try:
        conn.request("POST", path, body=body, headers=headers, encode_chunked=chunked)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def records(out: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


def test_server_binds_to_loopback(tmp_path: Path) -> None:
    server = make_server(0, tmp_path / "hooks.jsonl")
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_post_json_gets_an_empty_204_and_one_record(tmp_path: Path) -> None:
    out = tmp_path / "out" / "hooks.jsonl"
    payload = {"session_id": "s1", "hook_event_name": "Stop"}

    with running(out) as port:
        status, body = post(port, json.dumps(payload).encode())

    assert (status, body) == (204, b"")
    (record,) = records(out)
    assert record["path"] == "/hooks/http"
    assert record["mode"] == "ok"
    assert record["body"] == payload
    assert record["headers"]["content-type"] == "application/json"
    assert datetime.fromisoformat(record["received_at"]).tzinfo is not None


def test_error_mode_answers_500_and_still_records(tmp_path: Path) -> None:
    out = tmp_path / "hooks.jsonl"

    with running(out, mode="error") as port:
        status, body = post(port, b'{"hook_event_name": "Stop"}', path="/hooks/command")

    assert (status, body) == (500, b"")
    (record,) = records(out)
    assert (record["path"], record["mode"]) == ("/hooks/command", "error")


def test_slow_mode_waits_before_answering(tmp_path: Path) -> None:
    out = tmp_path / "hooks.jsonl"

    with running(out, mode="slow", delay=0.3) as port:
        started = time.monotonic()
        status, _ = post(port, b"{}")
        elapsed = time.monotonic() - started

    assert status == 204
    # Leave room for the ~16 ms clock resolution on Windows.
    assert elapsed >= 0.25


def test_chunked_body_is_parsed(tmp_path: Path) -> None:
    out = tmp_path / "hooks.jsonl"

    with running(out) as port:
        status, _ = post(port, iter([b'{"session_id": ', b'"s2", "n": 1}']), chunked=True)

    assert status == 204
    (record,) = records(out)
    assert record["body"] == {"session_id": "s2", "n": 1}
    assert record["headers"]["transfer-encoding"] == "chunked"


def test_invalid_json_is_kept_raw_and_still_gets_204(tmp_path: Path) -> None:
    out = tmp_path / "hooks.jsonl"

    with running(out) as port:
        status, body = post(port, b"not json")

    assert (status, body) == (204, b"")
    (record,) = records(out)
    assert record["body"] == {"_raw": "not json"}
