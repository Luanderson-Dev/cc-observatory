"""The alert rules on the writer thread (ADRs 0041 and 0042).

The writer feeds every hook event it commits to ``record_failure_event`` and, on each
``AlertTick``, runs the rules in a fixed order (``waiting_user``, ``repeated_failures``,
``session_cost``, ``daily_cost``) over the sessions it holds and the last cost sample,
then writes their effects in one transaction. Its state is rebuilt from the database at
the first tick, so a restart never fires an episode twice. ``run_once`` drives it with
a fake clock: the tick's ``now_ms``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from ccobs.domain.alerts import (
    ALERT_RULES,
    DAY_SUBJECT,
    AlertConfig,
    AlertEffect,
    AlertKey,
    CostSample,
    DailyCostRule,
    KeyState,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
)
from ccobs.server.changes import ALERTS, HubEvent
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import AlertRecord, AlertSettingValue, Repository
from ccobs.server.ingest import HookQueue, RawHook
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.writer import Writer

HOOK_FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "hooks"
HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
SECOND: Final = 1000
MINUTE: Final = 60 * SECOND
STALE_AFTER_MS: Final = 12 * 3_600_000
MIB: Final = 1024 * 1024
SESSION: Final = "00000000-0000-4000-8000-000000000000"
OTHER: Final = "00000000-0000-4000-8000-000000000001"
PRIVATE_PATH: Final = "C:\\Users\\someone-private\\projects\\secret"
# The real headless-tools-subagent session up to its first PermissionRequest (Bash): one
# open permission block from BASE + 6153, the session waiting_user.
TOOLS: Final = HOOK_FIXTURES / "headless-tools-subagent.jsonl"
PERMISSION_TOOL_USE_ID: Final = "toolu_01EXAMPLE000000000000004"
WAITING_SINCE: Final = BASE + 6153
COSTS: Final = AlertConfig(
    session_cost=SessionCostRule(enabled=True, limit_usd=10.0),
    daily_cost=DailyCostRule(enabled=True, limit_usd=50.0),
)

type Line = tuple[int, dict[str, Any]]


def fixture_lines(path: Path) -> list[Line]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        lines.append((document["offset_ms"], document["payload"]))
    return lines


PERMISSION_PREFIX: Final = fixture_lines(TOOLS)[:12]


def payload(name: str, session_id: str = SESSION, **fields: Any) -> dict[str, Any]:
    return {"session_id": session_id, "hook_event_name": name, **fields}


class AlertRepository(Repository):
    """A repository that records the alert writes and fails them on demand."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.alert_writes: list[list[AlertEffect]] = []
        self.fail_alert_writes = 0
        self.fail_alert_keys = False
        self.fail_alert_settings = False

    def apply_alert_effects(
        self, host: str, effects: Sequence[AlertEffect], *, discord_configured: bool
    ) -> None:
        self.alert_writes.append(list(effects))
        if self.fail_alert_writes:
            self.fail_alert_writes -= 1
            raise OperationalError("INSERT INTO alerts", None, Exception(PRIVATE_PATH))
        super().apply_alert_effects(host, effects, discord_configured=discord_configured)

    def alert_keys(self, host: str, *, cooldown_since_ms: int) -> dict[AlertKey, KeyState]:
        if self.fail_alert_keys:
            raise OperationalError("SELECT rule FROM alerts", None, Exception(PRIVATE_PATH))
        return super().alert_keys(host, cooldown_since_ms=cooldown_since_ms)

    def alert_settings(self, host: str) -> dict[str, AlertSettingValue]:
        if self.fail_alert_settings:
            raise OperationalError(
                "SELECT value FROM alert_settings", None, Exception(PRIVATE_PATH)
            )
        return super().alert_settings(host)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[AlertRepository]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    yield AlertRepository(engine)
    engine.dispose()


@pytest.fixture
def queue() -> HookQueue:
    return HookQueue(max_items=10_000, max_bytes=64 * MIB)


class Hub:
    """An ``on_committed`` that records what each commit tells the hub."""

    def __init__(self) -> None:
        self.events: list[HubEvent] = []

    def __call__(self, change: HubEvent) -> None:
        self.events.append(change)

    @property
    def alerts(self) -> int:
        return self.events.count(ALERTS)


@pytest.fixture
def hub() -> Hub:
    return Hub()


class Feeder:
    """Offers hooks with increasing seq values, as the hook route does; after a restart
    a new feeder goes on from the last seq."""

    def __init__(self, queue: HookQueue) -> None:
        self.queue = queue
        self.seq = 0

    def __call__(self, document: dict[str, Any], at: int) -> None:
        self.seq += 1
        assert self.queue.offer(RawHook(json.dumps(document).encode(), at, self.seq))

    def lines(self, lines: Sequence[Line]) -> None:
        for offset, document in lines:
            self(document, BASE + offset)


@pytest.fixture
def feed(queue: HookQueue) -> Feeder:
    return Feeder(queue)


@pytest.fixture
def make_writer(repo: AlertRepository, queue: HookQueue, hub: Hub) -> Callable[..., Writer]:
    def make(**kwargs: Any) -> Writer:
        options: dict[str, Any] = {
            "stale_after_ms": STALE_AFTER_MS,
            "batch_wait_s": 0.0,
            "sleep": lambda seconds: None,
            "on_committed": hub,
            **kwargs,
        }
        return Writer(queue, repo, HOST, **options)

    return make


def run_all(writer: Writer) -> None:
    while writer.run_once(0.0):
        pass


def tick(writer: Writer, now_ms: int) -> None:
    writer.submit_alert_tick(now_ms)
    run_all(writer)


def alerts(repo: Repository) -> list[AlertRecord]:
    """Every alert of the host, oldest first."""
    return list(reversed(repo.list_alerts(HOST, 500)))


def summary(repo: Repository) -> list[tuple[str, str, int, int | None]]:
    return [(a.rule, a.subject, a.fired_at_ms, a.cleared_at_ms) for a in alerts(repo)]


def waiting(make_writer: Callable[..., Writer], feed: Feeder, **kwargs: Any) -> Writer:
    """A writer holding SESSION, waiting on a permission prompt since WAITING_SINCE."""
    writer = make_writer(**kwargs)
    feed.lines(
        [(offset, {**document, "session_id": SESSION}) for offset, document in PERMISSION_PREFIX]
    )
    run_all(writer)
    state = writer.state(SESSION)
    assert state is not None
    assert state.session.status == "waiting_user"
    return writer


def fail(feed: Feeder, *times: int, session_id: str = SESSION) -> None:
    for at in times:
        feed(payload("StopFailure", session_id, error="server_error"), at)


# Replays.


def test_a_waiting_episode_fires_once_at_the_first_tick_past_30_s_and_clears(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository, hub: Hub
) -> None:
    # The real interactive session whose permission dialog, denied with Esc, waits
    # 102.8 s (tests/test_domain_alerts.py replays the same through the rules).
    writer = make_writer()
    lines = fixture_lines(HOOK_FIXTURES / "interactive-permission-compact.jsonl")
    (session_id,) = {document["session_id"] for _, document in lines}
    pending = list(lines)
    for now in range(BASE, BASE + lines[-1][0] + 10 * SECOND, 5 * SECOND):
        while pending and BASE + pending[0][0] <= now:
            offset, document = pending.pop(0)
            feed(document, BASE + offset)
        run_all(writer)
        tick(writer, now)

    ((rule, subject, fired_at, cleared_at),) = summary(repo)
    assert (rule, subject, fired_at) == ("waiting_user", session_id, BASE + 70_000)
    assert cleared_at is not None
    (alert,) = alerts(repo)
    assert (alert.value, alert.threshold) == (33.974, 30.0)
    assert writer.counts().alerts_fired == {"waiting_user": 1}
    # One hub event for the fire, one for the clear.
    assert hub.alerts == 2


def test_three_stop_failures_fire_once_and_a_successful_stop_clears(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer()
    tick(writer, BASE)  # the state is built: from now on failures are recorded live
    feed(payload("UserPromptSubmit"), BASE + 1)
    fail(feed, BASE + MINUTE, BASE + 2 * MINUTE)
    run_all(writer)
    tick(writer, BASE + 2 * MINUTE)
    assert alerts(repo) == []

    fail(feed, BASE + 3 * MINUTE)
    run_all(writer)
    tick(writer, BASE + 3 * MINUTE)
    tick(writer, BASE + 3 * MINUTE + 5 * SECOND)  # the same episode: no second fire

    (alert,) = alerts(repo)
    assert (alert.rule, alert.subject, alert.value, alert.threshold) == (
        "repeated_failures",
        SESSION,
        3.0,
        3.0,
    )
    assert alert.cleared_at_ms is None

    feed(payload("Stop"), BASE + 4 * MINUTE)
    run_all(writer)
    tick(writer, BASE + 4 * MINUTE)
    assert summary(repo) == [("repeated_failures", SESSION, BASE + 3 * MINUTE, BASE + 4 * MINUTE)]


def test_the_headless_api_error_fixture_fires_nothing(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    # One StopFailure (model_not_found), then the session ends.
    writer = make_writer()
    tick(writer, BASE)
    lines = fixture_lines(HOOK_FIXTURES / "headless-api-error.jsonl")
    feed.lines(lines)
    run_all(writer)
    tick(writer, BASE + lines[-1][0] + 5 * SECOND)
    assert alerts(repo) == []
    assert repo.alert_writes == []


def test_failures_committed_before_the_first_tick_come_from_the_database(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer()
    feed(payload("UserPromptSubmit"), BASE)
    fail(feed, BASE + 1, BASE + 2, BASE + 3)
    run_all(writer)

    tick(writer, BASE + 4)

    assert summary(repo) == [("repeated_failures", SESSION, BASE + 4, None)]


def test_failures_across_the_first_tick_count_once_each(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    # Two from the database at the first tick, one recorded live after it: neither
    # lost nor counted twice.
    writer = make_writer()
    feed(payload("UserPromptSubmit"), BASE)
    fail(feed, BASE + 1, BASE + 2)
    run_all(writer)
    tick(writer, BASE + 3)
    assert alerts(repo) == []

    fail(feed, BASE + 4)
    run_all(writer)
    tick(writer, BASE + 5)
    tick(writer, BASE + 10)

    (alert,) = alerts(repo)
    assert (alert.rule, alert.subject, alert.fired_at_ms, alert.value) == (
        "repeated_failures",
        SESSION,
        BASE + 5,
        3.0,
    )


# Retention.


def test_retention_forgets_the_alert_state_of_the_sessions_it_deletes(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = make_writer()
    tick(writer, BASE)  # the state is built: failures are recorded live
    # OTHER fails three times, goes stale and retention deletes it before any tick
    # sees its window.
    feed(payload("UserPromptSubmit", OTHER), BASE)
    fail(feed, BASE + 1, BASE + 2, BASE + 3, session_id=OTHER)
    run_all(writer)
    writer.submit_tick(BASE + 3 + STALE_AFTER_MS + 1)
    run_all(writer)
    writer.submit_retention(BASE + 4)
    run_all(writer)
    assert writer.state(OTHER) is None
    # SESSION, still open, waits on a permission prompt.
    feed.lines(
        [(offset, {**document, "session_id": SESSION}) for offset, document in PERMISSION_PREFIX]
    )
    run_all(writer)

    # The clock stepped back after the retention: OTHER's failures would still count.
    tick(writer, WAITING_SINCE + 30 * SECOND)

    assert summary(repo) == [("waiting_user", SESSION, WAITING_SINCE + 30 * SECOND, None)]
    assert "alert evaluation failed" not in caplog.text
    assert writer.counts().alerts_fired == {"waiting_user": 1}


def test_retention_forgets_the_keys_of_the_sessions_it_deletes(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    # OTHER's alert fires, then OTHER goes stale and is deleted with its alert; its
    # key goes too, so a later tick has nothing of OTHER's to clear or cool down.
    writer = make_writer()
    tick(writer, BASE)
    feed(payload("UserPromptSubmit", OTHER), BASE)
    fail(feed, BASE + 1, BASE + 2, BASE + 3, session_id=OTHER)
    run_all(writer)
    tick(writer, BASE + 4)
    assert [a.subject for a in alerts(repo)] == [OTHER]
    writer.submit_tick(BASE + 3 + STALE_AFTER_MS + 1)
    run_all(writer)
    writer.submit_retention(BASE + 4)
    run_all(writer)
    assert alerts(repo) == []
    repo.alert_writes.clear()

    # Past the window: had the key stayed, the episode's end would write a clear.
    tick(writer, BASE + 3 + STALE_AFTER_MS + 2)

    assert repo.alert_writes == []


# Restarts.


def test_a_restart_mid_episode_never_fires_twice_and_clears_the_same_alert(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository, hub: Hub
) -> None:
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE + 30 * SECOND)
    assert summary(repo) == [("waiting_user", SESSION, WAITING_SINCE + 30 * SECOND, None)]
    writer.stop(1.0)

    restarted = make_writer()
    tick(restarted, WAITING_SINCE + 90 * SECOND)  # still waiting, past the cooldown too
    tick(restarted, WAITING_SINCE + 400 * SECOND)
    assert len(alerts(repo)) == 1
    assert hub.alerts == 1

    restarted.submit_telemetry_close(SESSION, PERMISSION_TOOL_USE_ID, WAITING_SINCE + 401 * SECOND)
    run_all(restarted)
    tick(restarted, WAITING_SINCE + 405 * SECOND)

    assert summary(repo) == [
        ("waiting_user", SESSION, WAITING_SINCE + 30 * SECOND, WAITING_SINCE + 405 * SECOND)
    ]
    assert restarted.counts().alerts_fired == {}  # this run fired nothing


def test_a_restart_keeps_the_cooldown(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE + 30 * SECOND)
    writer.submit_telemetry_close(SESSION, PERMISSION_TOOL_USE_ID, WAITING_SINCE + 31 * SECOND)
    run_all(writer)
    tick(writer, WAITING_SINCE + 35 * SECOND)
    writer.stop(1.0)

    # A new wait right after the restart, within the 300 s cooldown of the first fire.
    restarted = make_writer()
    feed(
        {**PERMISSION_PREFIX[-1][1], "session_id": SESSION, "tool_use_id": "toolu_again"},
        WAITING_SINCE + 40 * SECOND,
    )
    run_all(restarted)
    tick(restarted, WAITING_SINCE + 100 * SECOND)
    assert len(alerts(repo)) == 1

    tick(restarted, WAITING_SINCE + 330 * SECOND)  # the cooldown ran out
    assert [a.fired_at_ms for a in alerts(repo)] == [
        WAITING_SINCE + 30 * SECOND,
        WAITING_SINCE + 330 * SECOND,
    ]


def test_a_restart_rebuilds_the_failure_windows(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer()
    tick(writer, BASE)
    feed(payload("UserPromptSubmit"), BASE)
    fail(feed, BASE + MINUTE, BASE + 2 * MINUTE)
    run_all(writer)
    tick(writer, BASE + 2 * MINUTE)
    writer.stop(1.0)

    restarted = make_writer()
    tick(restarted, BASE + 3 * MINUTE)
    fail(feed, BASE + 4 * MINUTE)
    run_all(restarted)
    tick(restarted, BASE + 4 * MINUTE)

    assert summary(repo) == [("repeated_failures", SESSION, BASE + 4 * MINUTE, None)]


def test_a_restart_with_the_failures_rule_off_drops_the_windows_until_it_is_back_on(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer()
    feed(payload("UserPromptSubmit"), BASE)
    fail(feed, BASE + MINUTE, BASE + 2 * MINUTE)
    run_all(writer)
    writer.stop(1.0)

    off = AlertConfig(repeated_failures=RepeatedFailuresRule(enabled=False))
    restarted = make_writer(alert_config=off)
    tick(restarted, BASE + 3 * MINUTE)
    fail(feed, BASE + 3 * MINUTE + SECOND)
    run_all(restarted)
    tick(restarted, BASE + 3 * MINUTE + 2 * SECOND)
    assert alerts(repo) == []  # the windows were dropped with the rule off (ADR 0042)

    # The rule comes back on (a saved setting): its windows are reloaded from the
    # database, as a restart with the rule on would rebuild them (ADR 0045), so the
    # three failures count at the next tick.
    repo.save_alert_setting(HOST, "alert_failures_enabled", True, updated_at_ms=BASE)
    restarted.submit_alert_settings_changed()
    run_all(restarted)
    tick(restarted, BASE + 4 * MINUTE)

    assert restarted.alert_config().repeated_failures.enabled
    assert summary(repo) == [("repeated_failures", SESSION, BASE + 4 * MINUTE, None)]


# The effective configuration.


def test_saved_settings_override_the_environment(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    repo.save_alert_setting(HOST, "alert_waiting_threshold_s", 60, updated_at_ms=BASE)
    env = AlertConfig(waiting_user=WaitingUserRule(threshold_s=45))

    writer = waiting(make_writer, feed, alert_config=env)
    tick(writer, WAITING_SINCE + 50 * SECOND)
    assert alerts(repo) == []
    tick(writer, WAITING_SINCE + 60 * SECOND)

    (alert,) = alerts(repo)
    assert (alert.fired_at_ms, alert.threshold) == (WAITING_SINCE + 60 * SECOND, 60.0)
    assert writer.alert_config().waiting_user.threshold_s == 60


def test_an_invalid_saved_setting_is_ignored_and_logged_by_name_only(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    # The repository checks types, not bounds: 3 s is below the 5 s minimum.
    repo.save_alert_setting(HOST, "alert_waiting_threshold_s", 3, updated_at_ms=BASE)
    repo.save_alert_setting(HOST, "alert_cooldown_s", 77_777, updated_at_ms=BASE)
    env = AlertConfig(waiting_user=WaitingUserRule(threshold_s=45))

    writer = waiting(make_writer, feed, alert_config=env)
    tick(writer, WAITING_SINCE + 30 * SECOND)
    assert alerts(repo) == []
    tick(writer, WAITING_SINCE + 45 * SECOND)

    assert [a.fired_at_ms for a in alerts(repo)] == [WAITING_SINCE + 45 * SECOND]
    assert writer.alert_config().waiting_user.threshold_s == 45
    assert writer.alert_config().cooldown_s == 77_777
    (record,) = [r for r in caplog.records if "alert_waiting_threshold_s" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "3" not in record.getMessage().replace("alert_waiting_threshold_s", "")
    assert "77" not in caplog.text


def test_a_settings_change_applies_from_the_next_tick(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE + 10 * SECOND)
    repo.save_alert_setting(HOST, "alert_waiting_threshold_s", 15, updated_at_ms=BASE)
    tick(writer, WAITING_SINCE + 20 * SECOND)
    assert alerts(repo) == []  # not reloaded yet

    writer.submit_alert_settings_changed()
    tick(writer, WAITING_SINCE + 25 * SECOND)

    assert [a.threshold for a in alerts(repo)] == [15.0]

    # Reset: back to the environment's 30 s.
    repo.reset_alert_setting(HOST, "alert_waiting_threshold_s")
    writer.submit_alert_settings_changed()
    run_all(writer)
    assert writer.alert_config() == AlertConfig()


def test_a_failed_settings_reload_keeps_the_configuration(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    repo.save_alert_setting(HOST, "alert_waiting_threshold_s", 60, updated_at_ms=BASE)
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE)
    repo.reset_alert_setting(HOST, "alert_waiting_threshold_s")
    repo.fail_alert_settings = True

    writer.submit_alert_settings_changed()
    run_all(writer)

    assert writer.alert_config().waiting_user.threshold_s == 60
    assert "OperationalError" in caplog.text
    assert "someone-private" not in caplog.text


def test_settings_changed_before_the_first_tick_are_read_by_it(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed)
    repo.save_alert_setting(HOST, "alert_waiting_enabled", False, updated_at_ms=BASE)
    writer.submit_alert_settings_changed()
    tick(writer, WAITING_SINCE + 60 * SECOND)

    assert alerts(repo) == []
    assert not writer.alert_config().waiting_user.enabled


def test_disabled_rules_fire_nothing(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    off = AlertConfig(
        waiting_user=WaitingUserRule(enabled=False),
        repeated_failures=RepeatedFailuresRule(enabled=False),
    )
    writer = waiting(make_writer, feed, alert_config=off)
    tick(writer, WAITING_SINCE)
    feed(payload("UserPromptSubmit", OTHER), WAITING_SINCE)
    fail(feed, WAITING_SINCE + 1, WAITING_SINCE + 2, WAITING_SINCE + 3, session_id=OTHER)
    run_all(writer)
    writer.submit_cost_sample(
        CostSample(observed_at_ms=WAITING_SINCE, sessions={SESSION: 99.0}, daily_usd=999.0)
    )
    tick(writer, WAITING_SINCE + 10 * MINUTE)

    assert alerts(repo) == []
    assert repo.alert_writes == []


# The cost rules: the reader's samples (stage 7) are stored and evaluated.


def test_a_cost_sample_is_stored_and_evaluated_at_the_next_tick(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer(alert_config=COSTS)
    feed(payload("UserPromptSubmit"), BASE)
    feed(payload("UserPromptSubmit", OTHER), BASE)
    run_all(writer)
    tick(writer, BASE + 5 * SECOND)  # no sample yet: nothing

    sample = CostSample(
        observed_at_ms=BASE + 6 * SECOND,
        sessions={SESSION: 10.0, OTHER: 9.99, "00000000-0000-4000-8000-0000000000ff": 50.0},
        daily_usd=50.0,
    )
    writer.submit_cost_sample(sample)
    run_all(writer)
    assert alerts(repo) == []  # stored, not evaluated until the tick
    tick(writer, BASE + 10 * SECOND)

    assert [(a.rule, a.subject, a.value, a.threshold) for a in alerts(repo)] == [
        ("session_cost", SESSION, 10.0, 10.0),
        ("daily_cost", DAY_SUBJECT, 50.0, 50.0),
    ]
    (day,) = [a for a in alerts(repo) if a.rule == "daily_cost"]
    assert day.session_id is None

    # A newer sample below 90 % of the limits clears both.
    writer.submit_cost_sample(
        CostSample(observed_at_ms=BASE + 70 * SECOND, sessions={SESSION: 8.9}, daily_usd=44.0)
    )
    tick(writer, BASE + 75 * SECOND)
    assert all(a.cleared_at_ms == BASE + 75 * SECOND for a in alerts(repo))


def test_a_stale_cost_sample_neither_fires_nor_clears(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer(alert_config=COSTS)
    feed(payload("UserPromptSubmit"), BASE)
    run_all(writer)
    writer.submit_cost_sample(CostSample(observed_at_ms=BASE, sessions={SESSION: 11.0}))
    tick(writer, BASE + 181 * SECOND)
    assert alerts(repo) == []


# Order, transactions, the hub and the metric.


def test_the_rules_run_in_a_fixed_order_in_one_write_per_tick(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed, alert_config=COSTS)
    tick(writer, WAITING_SINCE)
    # The failures in another session: a StopFailure ends SESSION's wait.
    feed(payload("UserPromptSubmit", OTHER), WAITING_SINCE)
    fail(feed, WAITING_SINCE + 1, WAITING_SINCE + 2, WAITING_SINCE + 3, session_id=OTHER)
    run_all(writer)
    writer.submit_cost_sample(
        CostSample(observed_at_ms=WAITING_SINCE, sessions={SESSION: 12.0}, daily_usd=60.0)
    )
    repo.alert_writes.clear()

    tick(writer, WAITING_SINCE + 30 * SECOND)

    (write,) = repo.alert_writes
    assert [(effect.rule, effect.subject) for effect in write] == [
        ("waiting_user", SESSION),
        ("repeated_failures", OTHER),
        ("session_cost", SESSION),
        ("daily_cost", DAY_SUBJECT),
    ]
    assert [a.rule for a in alerts(repo)] == list(ALERT_RULES)
    assert writer.counts().alerts_fired == dict.fromkeys(ALERT_RULES, 1)


def test_a_tick_without_effects_writes_nothing_and_tells_the_hub_nothing(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository, hub: Hub
) -> None:
    writer = waiting(make_writer, feed)
    for seconds in (0, 5, 10, 29):
        tick(writer, WAITING_SINCE + seconds * SECOND)

    assert repo.alert_writes == []
    assert hub.alerts == 0
    assert writer.counts().alerts_fired == {}


def test_a_failed_alert_write_changes_nothing_and_the_next_tick_retries(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    hub: Hub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = waiting(make_writer, feed)
    repo.fail_alert_writes = 1

    tick(writer, WAITING_SINCE + 30 * SECOND)
    assert alerts(repo) == []
    assert hub.alerts == 0
    assert writer.counts().alerts_fired == {}
    assert "OperationalError" in caplog.text
    assert "someone-private" not in caplog.text

    tick(writer, WAITING_SINCE + 35 * SECOND)
    assert summary(repo) == [("waiting_user", SESSION, WAITING_SINCE + 35 * SECOND, None)]
    assert hub.alerts == 1
    assert writer.counts().alerts_fired == {"waiting_user": 1}


def test_a_failed_rebuild_is_retried_at_the_next_tick(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    writer = waiting(make_writer, feed)
    repo.fail_alert_keys = True
    tick(writer, WAITING_SINCE + 30 * SECOND)
    assert alerts(repo) == []
    assert "OperationalError" in caplog.text

    repo.fail_alert_keys = False
    tick(writer, WAITING_SINCE + 35 * SECOND)
    assert len(alerts(repo)) == 1


def test_with_discord_configured_a_fired_alert_waits_to_be_sent(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed, discord_configured=True)
    tick(writer, WAITING_SINCE + 30 * SECOND)

    (alert,) = alerts(repo)
    assert (alert.discord_state, alert.discord_next_at_ms) == (
        "pending",
        WAITING_SINCE + 30 * SECOND,
    )


def test_without_discord_a_fired_alert_is_never_sent(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE + 30 * SECOND)

    (alert,) = alerts(repo)
    assert (alert.discord_state, alert.discord_next_at_ms) == ("none", None)


def test_hooks_queued_before_a_tick_are_applied_before_it(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository
) -> None:
    writer = make_writer()
    tick(writer, BASE)
    feed(payload("UserPromptSubmit"), BASE)
    fail(feed, BASE + 1, BASE + 2, BASE + 3)
    writer.submit_alert_tick(BASE + 4)

    assert writer.run_once(0.0) == 5

    assert summary(repo) == [("repeated_failures", SESSION, BASE + 4, None)]


def test_an_ended_session_clears_its_alert(
    make_writer: Callable[..., Writer], feed: Feeder, repo: AlertRepository, hub: Hub
) -> None:
    writer = waiting(make_writer, feed)
    tick(writer, WAITING_SINCE + 30 * SECOND)
    feed(payload("SessionEnd", reason="other"), WAITING_SINCE + 31 * SECOND)
    run_all(writer)
    tick(writer, WAITING_SINCE + 35 * SECOND)

    assert summary(repo) == [
        ("waiting_user", SESSION, WAITING_SINCE + 30 * SECOND, WAITING_SINCE + 35 * SECOND)
    ]
    assert hub.alerts == 2


def test_a_failed_hub_callback_is_logged_and_ignored(
    make_writer: Callable[..., Writer],
    feed: Feeder,
    repo: AlertRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    calls: list[HubEvent] = []

    def broken(change: HubEvent) -> None:
        calls.append(change)
        if change == ALERTS:
            raise RuntimeError(PRIVATE_PATH)

    writer = waiting(make_writer, feed, on_committed=broken)
    tick(writer, WAITING_SINCE + 30 * SECOND)

    assert len(alerts(repo)) == 1
    assert writer.counts().alerts_fired == {"waiting_user": 1}
    assert ALERTS in calls
    assert "RuntimeError" in caplog.text
    assert "someone-private" not in caplog.text


# The start of the cost reader's session query (ADR 0043).


def test_session_cost_since_is_the_oldest_open_session_start(
    make_writer: Callable[..., Writer], feed: Feeder
) -> None:
    writer = make_writer()
    assert writer.session_cost_since() is None  # no session held

    third = "00000000-0000-4000-8000-000000000002"
    feed(payload("UserPromptSubmit", OTHER), BASE)
    feed(payload("UserPromptSubmit"), BASE + MINUTE)
    feed(payload("UserPromptSubmit", third), BASE + STALE_AFTER_MS)
    run_all(writer)
    assert writer.session_cost_since() == BASE

    # OTHER ends: the next oldest open session.
    feed(payload("SessionEnd", OTHER, reason="other"), BASE + 2 * MINUTE)
    run_all(writer)
    assert writer.session_cost_since() == BASE + MINUTE

    # SESSION turns stale; the third, started later, is still open.
    writer.submit_tick(BASE + MINUTE + STALE_AFTER_MS + SECOND)
    run_all(writer)
    stale = writer.state(SESSION)
    assert stale is not None
    assert stale.session.status == "stale"
    assert writer.session_cost_since() == BASE + STALE_AFTER_MS
