"""The queue between the hook route and the writer (ADR 0027).

The hook route stamps each raw body with its receive time and a ``seq`` and offers it to
a ``HookQueue``; one writer thread (stage 8 of milestone 2) takes the items, parses and
persists them. The queue is bounded by item count and by total bytes, so a burst of
hooks or of large bodies cannot grow the app's memory without limit, and ``offer`` never
blocks: a full queue drops the hook, and Claude Code still gets its empty 2xx.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, get_args

__all__ = [
    "DROP_REASONS",
    "Clock",
    "DropReason",
    "HookQueue",
    "RawHook",
    "SeqAllocator",
    "wall_clock_ms",
]

# Why a hook was dropped instead of stored:
# - full: the queue was at its item or byte bound;
# - too_large: the body passed CCOBS_HOOK_BODY_MAX_BYTES;
# - bad_content_type: not application/json (a cross-site form post, ADR 0001);
# - bad_host: a Host outside the allowlist, or none (DNS rebinding, ADR 0001);
# - bad_request: not a hook request (another method, a malformed or conflicting
#   Content-Length, a client gone mid-body);
# - invalid: counted by the writer, a body it cannot parse as a hook payload;
# - internal: an unexpected error in the app.
type DropReason = Literal[
    "full", "too_large", "bad_content_type", "bad_host", "bad_request", "invalid", "internal"
]
DROP_REASONS: Final[tuple[DropReason, ...]] = get_args(DropReason.__value__)

# Returns wall-clock UTC time in integer milliseconds; injectable for tests.
type Clock = Callable[[], int]


def wall_clock_ms() -> int:
    """Wall-clock UTC time in milliseconds since the Unix epoch."""
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class RawHook:
    """A hook request as received: its unparsed body, receive time and order."""

    body: bytes
    received_at_ms: int
    seq: int


class HookQueue:
    """A thread-safe FIFO of ``RawHook``, bounded by item count and by total body bytes.

    An item holds its share of both bounds from ``offer`` until the consumer calls
    ``task_done`` with it, so items a writer has taken but not yet stored still count:
    ``depth`` and ``bytes`` are what the app holds in memory, not only what waits.
    """

    def __init__(self, max_items: int, max_bytes: int) -> None:
        if max_items < 1:
            raise ValueError(f"max_items must be at least 1, got {max_items}")
        if max_bytes < 1:
            raise ValueError(f"max_bytes must be at least 1, got {max_bytes}")
        self.max_items: Final = max_items
        self.max_bytes: Final = max_bytes
        # Unbounded underneath: the bounds are checked under self._lock, by count and by
        # bytes together, which queue.Queue's maxsize cannot do.
        self._items: queue.SimpleQueue[RawHook] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._depth = 0
        self._bytes = 0
        self._drops: dict[DropReason, int] = dict.fromkeys(DROP_REASONS, 0)

    @property
    def depth(self) -> int:
        """Items offered and not yet marked done."""
        return self._depth

    @property
    def bytes(self) -> int:
        """Body bytes of the items offered and not yet marked done."""
        return self._bytes

    @property
    def drops(self) -> dict[DropReason, int]:
        """A snapshot of the drop counters, by reason."""
        with self._lock:
            return dict(self._drops)

    def count_drop(self, reason: DropReason) -> None:
        """Count one hook dropped for ``reason``."""
        with self._lock:
            self._drops[reason] += 1

    def offer(self, item: RawHook) -> bool:
        """Enqueue ``item`` without blocking; False (counted ``full``) when it does not fit."""
        size = len(item.body)
        with self._lock:
            if self._depth >= self.max_items or self._bytes + size > self.max_bytes:
                self._drops["full"] += 1
                return False
            self._depth += 1
            self._bytes += size
            self._items.put(item)
        return True

    def get(self, timeout: float | None) -> RawHook | None:
        """The oldest item, waiting up to ``timeout`` seconds (forever if None); None
        when the wait ends empty. Call ``task_done`` with it once it is handled."""
        try:
            return self._items.get(timeout=timeout)
        except queue.Empty:
            return None

    def task_done(self, item: RawHook) -> None:
        """Release the room ``item`` held in both bounds."""
        with self._lock:
            self._depth -= 1
            self._bytes -= len(item.body)


class SeqAllocator:
    """Hands out increasing ``seq`` values from ``first``, thread-safely.

    The app starts it at ``Repository.max_seq() + 1``, so numbering continues after a
    restart, and the reducer, which ignores an event whose ``seq`` is not above the last
    one it applied (ADR 0024), never applies an event twice.
    """

    def __init__(self, first: int) -> None:
        if first < 1:
            raise ValueError(f"seq starts at 1 or above, got {first}")
        self._next = first
        self._lock = threading.Lock()

    def next(self) -> int:
        """The next ``seq``."""
        with self._lock:
            value = self._next
            self._next += 1
            return value
