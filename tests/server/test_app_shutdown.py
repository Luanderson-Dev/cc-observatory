"""The lifespan's shutdown budget (ADRs 0030 and 0046).

The background threads (the reconciler, the Discord sender, the cost reader, the alert
ticker and the sweeper) are signalled together, then joined against one deadline, so
two of them stuck at once (a Discord POST and a cost query) cost one timeout, not two;
the writer stops last, with a budget of its own, and drains the queue.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ccobs.server import app as server_app
from ccobs.server.config import Settings
from ccobs.server.costs import CostReader
from ccobs.server.discord import DiscordSender
from ccobs.server.reconcile import Reconciler
from ccobs.server.sweeps import AlertTicker, Sweeper
from ccobs.server.writer import Writer

GOOD_HOST: Final = "127.0.0.1:4300"
WEBHOOK: Final = "https://discord.com/api/webhooks/123456789012345678/CANARY-not-a-real-token"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Thread:
    """A background thread as ``stop_together`` sees it; a stuck one uses up every
    timeout it is given."""

    def __init__(self, name: str, events: list[str], time: FakeTime, *, stuck: bool) -> None:
        self.name = name
        self.events = events
        self.time = time
        self.stuck = stuck
        self.timeouts: list[float] = []

    def request_stop(self) -> None:
        self.events.append(f"signal {self.name}")

    def stop(self, timeout_s: float) -> None:
        self.events.append(f"join {self.name}")
        self.timeouts.append(timeout_s)
        if self.stuck:
            self.time.now += timeout_s


def test_every_thread_is_signalled_before_any_is_joined() -> None:
    events: list[str] = []
    time = FakeTime()
    threads = [Thread(name, events, time, stuck=False) for name in "abc"]

    server_app.stop_together([(thread, 5.0) for thread in threads], monotonic=time)

    assert events == ["signal a", "signal b", "signal c", "join a", "join b", "join c"]
    assert [thread.timeouts for thread in threads] == [[5.0], [5.0], [5.0]]


def test_stuck_threads_share_one_deadline() -> None:
    events: list[str] = []
    time = FakeTime()
    first = Thread("discord", events, time, stuck=True)
    second = Thread("costs", events, time, stuck=True)
    quick = Thread("sweeper", events, time, stuck=False)

    server_app.stop_together([(first, 5.0), (second, 5.0), (quick, 2.0)], monotonic=time)

    # The first stuck thread used the whole deadline; the others are not waited for.
    assert (first.timeouts, second.timeouts, quick.timeouts) == ([5.0], [0.0], [0.0])
    assert time.now == 1005.0


def test_a_shorter_timeout_still_bounds_its_own_join() -> None:
    events: list[str] = []
    time = FakeTime()
    short = Thread("ticker", events, time, stuck=False)
    long = Thread("reconciler", events, time, stuck=False)

    server_app.stop_together([(short, 1.0), (long, 5.0)], monotonic=time)

    assert (short.timeouts, long.timeouts) == ([1.0], [5.0])


def test_nothing_to_stop_waits_for_nothing() -> None:
    server_app.stop_together([], monotonic=FakeTime())


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    return Settings(
        data_dir=tmp_path / "data",
        host_label="test-host",
        discord_webhook_url=SecretStr(WEBHOOK),
    )


def test_the_lifespan_signals_every_background_thread_then_joins_them_then_the_writer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def record[T](
        owner: type[T],
        name: str,
        signal: Callable[[T], None],
        stop: Callable[[T, float], None],
    ) -> None:
        def signalled(self: T) -> None:
            events.append(f"signal {name}")
            signal(self)

        def stopped(self: T, timeout_s: float) -> None:
            events.append(f"join {name}")
            stop(self, timeout_s)

        monkeypatch.setattr(owner, "request_stop", signalled)
        monkeypatch.setattr(owner, "stop", stopped)

    record(Reconciler, "reconciler", Reconciler.request_stop, Reconciler.stop)
    record(DiscordSender, "discord", DiscordSender.request_stop, DiscordSender.stop)
    record(CostReader, "costs", CostReader.request_stop, CostReader.stop)
    record(AlertTicker, "ticker", AlertTicker.request_stop, AlertTicker.stop)
    record(Sweeper, "sweeper", Sweeper.request_stop, Sweeper.stop)
    writer_stop = Writer.stop

    def writer_stopped(self: Writer, timeout_s: float) -> None:
        events.append(f"writer {timeout_s}")
        writer_stop(self, timeout_s)

    monkeypatch.setattr(Writer, "stop", writer_stopped)
    quiet = httpx.MockTransport(lambda request: httpx.Response(503))
    app = server_app.create_app(
        settings,
        serve_metrics=False,
        loki_transport=quiet,
        cost_transport=quiet,
        discord_transport=quiet,
    )

    with TestClient(app, base_url=f"http://{GOOD_HOST}"):
        assert events == []

    names = ["reconciler", "discord", "costs", "ticker", "sweeper"]
    assert events == [
        *(f"signal {name}" for name in names),
        *(f"join {name}" for name in names),
        f"writer {server_app.WRITER_STOP_TIMEOUT_S}",
    ]
    for state in ("reconciler", "discord", "cost_reader", "alert_ticker", "sweeper"):
        assert getattr(app.state, state) is None
