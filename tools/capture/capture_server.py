"""Capture server for Claude Code hooks (fixture capture tool, standard library only).

Listens on 127.0.0.1, appends every request as one JSON line to the output file and
answers with an empty 204, the neutral hook response. The failure modes simulate a broken
app: ``slow`` waits ``--delay`` seconds before answering, ``error`` answers 500.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Literal, get_args, override

from tools.capture.jsontypes import JSON, JSONObject

Mode = Literal["ok", "slow", "error"]

MODES: Final = get_args(Mode)
DEFAULT_OUT: Final = Path(__file__).resolve().parent / "out" / "hooks.jsonl"
_KEPT_HEADERS: Final = frozenset(
    {"content-type", "content-length", "user-agent", "transfer-encoding"}
)


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """Request body, with or without chunked transfer encoding."""
    if handler.headers.get("Transfer-Encoding", "").lower() != "chunked":
        return handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    chunks: list[bytes] = []
    while True:
        size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
        if size == 0:
            handler.rfile.readline()
            return b"".join(chunks)
        chunks.append(handler.rfile.read(size))
        handler.rfile.readline()


def make_handler(out: Path, mode: Mode, delay: float) -> type[BaseHTTPRequestHandler]:
    """Request handler class that records to ``out`` and answers according to ``mode``."""
    lock = threading.Lock()

    class CaptureHandler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            received_at = datetime.now(UTC).isoformat()
            raw = _read_body(self)
            body: JSON
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"_raw": raw.decode("utf-8", "replace")}
            headers: JSONObject = {
                key.lower(): value
                for key, value in self.headers.items()
                if key.lower() in _KEPT_HEADERS
            }
            record: JSONObject = {
                "received_at": received_at,
                "path": self.path,
                "mode": mode,
                "headers": headers,
                "body": body,
            }
            with lock, out.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if mode == "slow":
                time.sleep(delay)
            self.send_response(500 if mode == "error" else 204)
            if mode == "error":
                self.send_header("Content-Length", "0")
            self.end_headers()

    return CaptureHandler


def make_server(
    port: int, out: Path, mode: Mode = "ok", delay: float = 10.0
) -> ThreadingHTTPServer:
    """Capture server bound to 127.0.0.1:``port`` (0 picks a free port), not started."""
    out.parent.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(out, mode, delay))


def main(argv: Sequence[str] | None = None) -> None:
    """Run the capture server until interrupted."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.capture.capture_server",
        description="Record Claude Code hook requests as JSON lines.",
    )
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSONL file to append to")
    parser.add_argument("--mode", choices=MODES, default="ok")
    parser.add_argument("--delay", type=float, default=10.0, help="seconds to wait in slow mode")
    args = parser.parse_args(argv)
    server = make_server(args.port, args.out, args.mode, args.delay)
    print(
        f"capturing hooks on http://127.0.0.1:{server.server_port} -> {args.out}"
        f" (mode={args.mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
