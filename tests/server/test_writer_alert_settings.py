"""Settings changes on the writer thread (ADRs 0042 and 0045).

The settings screen hands the writer a save or a reset with a future. The writer checks
a save against the effective configuration (``apply_settings``), saves it in one
transaction or nothing, reloads the effective configuration, and settles the future with
the saved overrides and any errors. A request that gave up (its future cancelled) is
skipped; too many waiting changes, a stop or a failed write settle it with
``AlertSettingsUnavailableError``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from ccobs.domain.alerts import AlertConfig, WaitingUserRule, apply_saved_settings, setting_error
from ccobs.server.db.engine import create_engine_for
from ccobs.server.db.repository import AlertSettingValue, Repository
from ccobs.server.ingest import HookQueue, RawHook
from ccobs.server.migrations import upgrade_to_head
from ccobs.server.writer import (
    SETTINGS_PENDING_MAX,
    AlertSettingsResult,
    AlertSettingsUnavailableError,
    Writer,
)

HOST: Final = "test-host"
BASE: Final = 1_800_000_000_000
SECOND: Final = 1000
MINUTE: Final = 60 * SECOND
SESSION: Final = "00000000-0000-4000-8000-000000000000"
RULE: Final = "repeated_failures"
MIB: Final = 1024 * 1024
PRIVATE_PATH: Final = "C:\\Users\\someone-private\\projects\\secret"
ENV: Final = AlertConfig(waiting_user=WaitingUserRule(threshold_s=45), cooldown_s=120)


class SettingsRepository(Repository):
    """A repository whose settings writes fail on demand."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.fail_saves = False

    def save_alert_settings(
        self, host: str, values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> None:
        if self.fail_saves:
            raise OperationalError("INSERT INTO alert_settings", None, Exception(PRIVATE_PATH))
        super().save_alert_settings(host, values, updated_at_ms=updated_at_ms)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SettingsRepository]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    yield SettingsRepository(engine)
    engine.dispose()


@pytest.fixture
def writer(repo: SettingsRepository) -> Writer:
    queue = HookQueue(max_items=100, max_bytes=MIB)
    return Writer(
        queue,
        repo,
        HOST,
        stale_after_ms=3_600_000,
        batch_wait_s=0.0,
        alert_config=ENV,
        sleep=lambda seconds: None,
    )


def run_all(writer: Writer) -> None:
    while writer.run_once(0.0):
        pass


def outcome(future: Future[AlertSettingsResult]) -> AlertSettingsResult:
    assert future.done()
    return future.result(timeout=0)


def test_a_save_is_stored_and_reloads_the_configuration(
    writer: Writer, repo: SettingsRepository
) -> None:
    future = writer.submit_alert_settings_save(
        {"alert_waiting_threshold_s": 60, "alert_daily_cost_enabled": True}, updated_at_ms=BASE
    )
    assert not future.done()

    run_all(writer)

    saved = {"alert_waiting_threshold_s": 60, "alert_daily_cost_enabled": True}
    assert outcome(future) == AlertSettingsResult(saved=saved)
    assert repo.alert_settings(HOST) == saved
    # In force at once, even before the first tick: the next tick uses it.
    config = writer.alert_config()
    assert config.waiting_user == WaitingUserRule(threshold_s=60)
    assert config.daily_cost.enabled
    assert config.cooldown_s == ENV.cooldown_s


def test_a_partial_save_keeps_the_other_saved_settings(
    writer: Writer, repo: SettingsRepository
) -> None:
    repo.save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=BASE)

    future = writer.submit_alert_settings_save({"alert_failures_count": 5}, updated_at_ms=BASE)
    run_all(writer)

    assert outcome(future).saved == {"alert_cooldown_s": 30, "alert_failures_count": 5}
    assert writer.alert_config().cooldown_s == 30
    assert writer.alert_config().repeated_failures.count == 5


def test_an_invalid_save_stores_nothing_and_names_the_fields(
    writer: Writer, repo: SettingsRepository
) -> None:
    repo.save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=BASE)

    future = writer.submit_alert_settings_save(
        {"alert_waiting_threshold_s": 4, "alert_failures_count": 5, "alert_cooldown_s": 99_999},
        updated_at_ms=BASE,
    )
    run_all(writer)

    assert outcome(future) == AlertSettingsResult(
        saved={"alert_cooldown_s": 30},
        errors=(setting_error("alert_waiting_threshold_s"), setting_error("alert_cooldown_s")),
    )
    assert repo.alert_settings(HOST) == {"alert_cooldown_s": 30}
    assert writer.alert_config() == ENV


def test_a_reset_of_one_setting_falls_back_to_the_environment(
    writer: Writer, repo: SettingsRepository
) -> None:
    repo.save_alert_settings(
        HOST, {"alert_waiting_threshold_s": 60, "alert_cooldown_s": 30}, updated_at_ms=BASE
    )
    run_all(writer)

    future = writer.submit_alert_settings_reset("alert_waiting_threshold_s")
    run_all(writer)

    assert outcome(future) == AlertSettingsResult(saved={"alert_cooldown_s": 30})
    assert writer.alert_config().waiting_user.threshold_s == 45
    assert writer.alert_config().cooldown_s == 30


def test_a_reset_of_every_setting_brings_back_the_environment(
    writer: Writer, repo: SettingsRepository
) -> None:
    save = writer.submit_alert_settings_save(
        {"alert_waiting_threshold_s": 60, "alert_cooldown_s": 30}, updated_at_ms=BASE
    )
    run_all(writer)
    assert outcome(save).saved

    future = writer.submit_alert_settings_reset(None)
    run_all(writer)

    assert outcome(future) == AlertSettingsResult(saved={})
    assert repo.alert_settings(HOST) == {}
    assert writer.alert_config() == ENV


def test_a_change_the_request_gave_up_on_is_skipped(
    writer: Writer, repo: SettingsRepository
) -> None:
    future = writer.submit_alert_settings_save({"alert_cooldown_s": 30}, updated_at_ms=BASE)
    assert future.cancel()

    run_all(writer)

    assert repo.alert_settings(HOST) == {}
    assert writer.alert_config() == ENV


def test_too_many_waiting_changes_are_refused(writer: Writer) -> None:
    futures = [writer.submit_alert_settings_reset(None) for _ in range(SETTINGS_PENDING_MAX)]

    with pytest.raises(AlertSettingsUnavailableError):
        writer.submit_alert_settings_save({"alert_cooldown_s": 30}, updated_at_ms=BASE)
    with pytest.raises(AlertSettingsUnavailableError):
        writer.submit_alert_settings_reset(None)

    run_all(writer)
    assert all(future.done() for future in futures)
    # Taken by the writer, they make room again.
    writer.submit_alert_settings_reset(None)


def test_a_failed_write_settles_the_change_without_details(
    writer: Writer, repo: SettingsRepository, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ccobs")
    repo.fail_saves = True

    future = writer.submit_alert_settings_save({"alert_cooldown_s": 30}, updated_at_ms=BASE)
    run_all(writer)

    error = future.exception(timeout=0)
    assert isinstance(error, AlertSettingsUnavailableError)
    assert PRIVATE_PATH not in str(error)
    assert writer.alert_config() == ENV
    assert "alert settings not changed (OperationalError)" in caplog.text
    assert PRIVATE_PATH not in caplog.text


class ReadFailsAfterWrite(SettingsRepository):
    """A repository whose settings read fails once a settings write has succeeded."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.written = False

    def alert_settings(self, host: str) -> dict[str, AlertSettingValue]:
        if self.written:
            raise OperationalError("SELECT value FROM alert_settings", None, Exception())
        return super().alert_settings(host)

    def save_alert_settings(
        self, host: str, values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> None:
        super().save_alert_settings(host, values, updated_at_ms=updated_at_ms)
        self.written = True

    def reset_alert_setting(self, host: str, field: str) -> bool:
        deleted = super().reset_alert_setting(host, field)
        self.written = True
        return deleted

    def reset_alert_settings(self, host: str) -> int:
        deleted = super().reset_alert_settings(host)
        self.written = True
        return deleted


@pytest.fixture
def read_fails(tmp_path: Path) -> Iterator[ReadFailsAfterWrite]:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    yield ReadFailsAfterWrite(engine)
    engine.dispose()


def writer_on(repo: Repository, queue: HookQueue | None = None) -> Writer:
    return Writer(
        queue or HookQueue(max_items=100, max_bytes=MIB),
        repo,
        HOST,
        stale_after_ms=3_600_000,
        batch_wait_s=0.0,
        alert_config=ENV,
        sleep=lambda seconds: None,
    )


BEFORE: Final[dict[str, AlertSettingValue]] = {
    "alert_cooldown_s": 30,
    "alert_failures_count": 5,
}


def submit(writer: Writer, change: str) -> Future[AlertSettingsResult]:
    """``save``, ``reset-all``, or the name of the one setting to reset."""
    if change == "save":
        return writer.submit_alert_settings_save(
            {"alert_failures_count": 4, "alert_waiting_threshold_s": 60}, updated_at_ms=BASE
        )
    return writer.submit_alert_settings_reset(None if change == "reset-all" else change)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            "save",
            {"alert_cooldown_s": 30, "alert_failures_count": 4, "alert_waiting_threshold_s": 60},
        ),
        ("alert_failures_count", {"alert_cooldown_s": 30}),
        ("alert_failures_enabled", BEFORE),  # none saved: nothing changes
        ("reset-all", {}),
    ],
)
def test_a_committed_change_is_reported_without_reading_the_settings_again(
    read_fails: ReadFailsAfterWrite, change: str, expected: dict[str, AlertSettingValue]
) -> None:
    # The write is the only failure left: once it commits, the outcome is built in
    # memory, so a read failing after it cannot turn a saved change into a 503.
    read_fails.save_alert_settings(HOST, BEFORE, updated_at_ms=BASE)
    read_fails.written = False
    writer = writer_on(read_fails)

    future = submit(writer, change)
    run_all(writer)

    assert outcome(future) == AlertSettingsResult(saved=expected)
    assert writer.alert_config() == apply_saved_settings(ENV, expected)[0]
    read_fails.written = False
    assert read_fails.alert_settings(HOST) == expected


# Failure windows (ADR 0045): turning repeated_failures on, or widening it, from the
# screen reloads them from the database, as a restart does.


def fail_hook(queue: HookQueue, seq: int, at_ms: int) -> None:
    body = {"session_id": SESSION, "hook_event_name": "StopFailure", "error": "server_error"}
    assert queue.offer(RawHook(json.dumps(body).encode(), at_ms, seq))


def tick(writer: Writer, now_ms: int) -> None:
    writer.submit_alert_tick(now_ms)
    run_all(writer)


def failure_values(repo: Repository) -> list[float]:
    return [alert.value for alert in repo.list_alerts(HOST, 50) if alert.rule == RULE]


def test_enabling_repeated_failures_reloads_the_failures_recorded_while_off(
    repo: SettingsRepository,
) -> None:
    repo.save_alert_setting(HOST, "alert_failures_enabled", False, updated_at_ms=BASE)
    queue = HookQueue(max_items=100, max_bytes=MIB)
    writer = writer_on(repo, queue)
    tick(writer, BASE)  # the state is built, with the rule off
    for seq in (1, 2, 3):
        fail_hook(queue, seq, BASE + seq * SECOND)
    run_all(writer)
    tick(writer, BASE + 4 * SECOND)
    assert failure_values(repo) == []

    future = writer.submit_alert_settings_save(
        {"alert_failures_enabled": True}, updated_at_ms=BASE + 5 * SECOND
    )
    run_all(writer)
    assert outcome(future).errors == ()
    tick(writer, BASE + 6 * SECOND)

    assert failure_values(repo) == [3.0]


def test_resetting_repeated_failures_to_on_reloads_its_windows(
    repo: SettingsRepository,
) -> None:
    # The environment has it on; the saved override turned it off.
    repo.save_alert_setting(HOST, "alert_failures_enabled", False, updated_at_ms=BASE)
    queue = HookQueue(max_items=100, max_bytes=MIB)
    writer = writer_on(repo, queue)
    tick(writer, BASE)
    for seq in (1, 2, 3):
        fail_hook(queue, seq, BASE + seq * SECOND)
    run_all(writer)

    future = writer.submit_alert_settings_reset("alert_failures_enabled")
    run_all(writer)
    assert outcome(future) == AlertSettingsResult(saved={})
    tick(writer, BASE + 6 * SECOND)

    assert failure_values(repo) == [3.0]


def test_settings_reloaded_from_the_database_reload_the_windows_too(
    repo: SettingsRepository,
) -> None:
    # The same for a change saved elsewhere and announced with
    # submit_alert_settings_changed.
    repo.save_alert_setting(HOST, "alert_failures_enabled", False, updated_at_ms=BASE)
    queue = HookQueue(max_items=100, max_bytes=MIB)
    writer = writer_on(repo, queue)
    tick(writer, BASE)
    for seq in (1, 2, 3):
        fail_hook(queue, seq, BASE + seq * SECOND)
    run_all(writer)

    repo.reset_alert_settings(HOST)
    writer.submit_alert_settings_changed()
    run_all(writer)
    tick(writer, BASE + 6 * SECOND)

    assert failure_values(repo) == [3.0]


def test_raising_the_window_brings_back_an_older_failure(repo: SettingsRepository) -> None:
    repo.save_alert_setting(HOST, "alert_failures_window_s", 60, updated_at_ms=BASE)
    queue = HookQueue(max_items=100, max_bytes=MIB)
    writer = writer_on(repo, queue)
    tick(writer, BASE)
    # One failure, then two more 2 minutes later: a 60 s window keeps only the last two.
    fail_hook(queue, 1, BASE + SECOND)
    fail_hook(queue, 2, BASE + 2 * MINUTE)
    fail_hook(queue, 3, BASE + 2 * MINUTE + SECOND)
    run_all(writer)
    tick(writer, BASE + 2 * MINUTE + 2 * SECOND)
    assert failure_values(repo) == []

    future = writer.submit_alert_settings_save(
        {"alert_failures_window_s": 600}, updated_at_ms=BASE + 3 * MINUTE
    )
    run_all(writer)
    assert outcome(future).errors == ()
    tick(writer, BASE + 3 * MINUTE)

    assert failure_values(repo) == [3.0]


def test_raising_the_count_reloads_failures_the_window_had_dropped(
    repo: SettingsRepository,
) -> None:
    # A window keeps at most `count` failures: with 2, the third oldest is gone, so a
    # count raised to 3 needs the database to see three.
    repo.save_alert_setting(HOST, "alert_failures_count", 2, updated_at_ms=BASE)
    queue = HookQueue(max_items=100, max_bytes=MIB)
    writer = writer_on(repo, queue)
    tick(writer, BASE)
    for seq in (1, 2, 3):
        fail_hook(queue, seq, BASE + seq * SECOND)
    run_all(writer)
    tick(writer, BASE + 4 * SECOND)
    assert failure_values(repo) == [2.0]

    future = writer.submit_alert_settings_save(
        {"alert_failures_count": 3}, updated_at_ms=BASE + 5 * SECOND
    )
    run_all(writer)
    assert outcome(future).errors == ()
    tick(writer, BASE + 6 * SECOND)

    # Three failures still reach the new count: the episode goes on, as after a restart
    # (the two the window kept would have cleared it).
    (alert,) = repo.list_alerts(HOST, 50)
    assert alert.cleared_at_ms is None


def test_a_failed_reload_of_the_windows_rebuilds_them_at_the_next_tick(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    repo = WindowsFail(engine)
    try:
        repo.save_alert_setting(HOST, "alert_failures_enabled", False, updated_at_ms=BASE)
        queue = HookQueue(max_items=100, max_bytes=MIB)
        writer = writer_on(repo, queue)
        tick(writer, BASE)
        for seq in (1, 2, 3):
            fail_hook(queue, seq, BASE + seq * SECOND)
        run_all(writer)

        repo.fail_windows = True
        future = writer.submit_alert_settings_save(
            {"alert_failures_enabled": True}, updated_at_ms=BASE + 5 * SECOND
        )
        run_all(writer)
        # Saved all the same: the windows are memory, the next tick rebuilds them.
        assert outcome(future) == AlertSettingsResult(saved={"alert_failures_enabled": True})
        assert "failure windows not reloaded (OperationalError)" in caplog.text

        repo.fail_windows = False
        tick(writer, BASE + 6 * SECOND)
        assert failure_values(repo) == [3.0]
    finally:
        engine.dispose()


def test_a_change_that_keeps_or_narrows_the_rule_reads_no_windows(
    tmp_path: Path,
) -> None:
    engine = create_engine_for(tmp_path / "ccobs.db", role="writer")
    upgrade_to_head(engine)
    repo = WindowsFail(engine)
    try:
        writer = writer_on(repo)
        tick(writer, BASE)
        repo.fail_windows = True
        for values in (
            {"alert_cooldown_s": 30},
            {"alert_failures_count": 2, "alert_failures_window_s": 60},
            {"alert_failures_enabled": False},
        ):
            future = writer.submit_alert_settings_save(values, updated_at_ms=BASE)
            run_all(writer)
            assert outcome(future).errors == ()
        assert repo.window_reads == 0
        # Before the first tick there is nothing to reload: the tick builds it all.
        fresh = writer_on(repo)
        future = fresh.submit_alert_settings_save(
            {"alert_failures_enabled": True}, updated_at_ms=BASE
        )
        run_all(fresh)
        assert outcome(future).errors == ()
        assert repo.window_reads == 0
    finally:
        engine.dispose()


class WindowsFail(SettingsRepository):
    """A repository that counts the failure-window reads and fails them on demand."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.fail_windows = False
        self.window_reads = 0

    def failure_windows(
        self, host: str, *, since_ms: int, count: int
    ) -> dict[str, tuple[int, ...]]:
        if self.fail_windows:
            self.window_reads += 1
            raise OperationalError("SELECT seq FROM hook_events", None, Exception(PRIVATE_PATH))
        return super().failure_windows(host, since_ms=since_ms, count=count)


def test_changes_left_at_stop_are_settled_as_unavailable(writer: Writer) -> None:
    save = writer.submit_alert_settings_save({"alert_cooldown_s": 30}, updated_at_ms=BASE)
    reset = writer.submit_alert_settings_reset(None)
    gave_up = writer.submit_alert_settings_reset(None)
    assert gave_up.cancel()

    # A writer that never ran: stop drains on the calling thread with no time left.
    writer.stop(0.0)

    assert isinstance(save.exception(timeout=0), AlertSettingsUnavailableError)
    assert isinstance(reset.exception(timeout=0), AlertSettingsUnavailableError)
    assert gave_up.cancelled()
