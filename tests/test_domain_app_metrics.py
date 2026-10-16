"""Unit tests of ``ccobs.domain.app_metrics``, the names of the app's own metrics (ADR 0028).

The constants are what Prometheus stores for the app's ``/metrics`` (the app's series
are scraped, so their names are not translated), and what the dashboards and
``ccobs doctor`` import. They must keep a fixed cardinality: a ``host`` label on every
series, fixed label values, and never a ``session_id``.
"""

from __future__ import annotations

import re
from typing import Final, get_args

import pytest

from ccobs.domain import app_metrics
from ccobs.domain.app_metrics import (
    APP_METRICS,
    BLOCK_DURATION,
    BLOCK_DURATION_BUCKETS,
    BLOCK_KINDS,
    BUILD_INFO,
    EVENT_LABELS,
    HOOK_EVENTS_DROPPED,
    HOOK_EVENTS_RECEIVED,
    METRICS_PATH,
    OTHER_EVENT,
    RECEIVED_EVENTS,
    SCRAPE_JOB,
    SESSION_STATES,
    SESSIONS,
    AppMetric,
    event_label,
)
from ccobs.domain.hook_config import ASYNC_COMMAND_EVENTS, REGISTERED_HTTP_EVENTS
from ccobs.domain.sessions import BlockKind, SessionStatus
from ccobs.domain.telemetry import HOST_LABEL

# Prometheus's metric and label name syntax.
_NAME: Final = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL: Final = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def test_the_proposed_series_are_all_there() -> None:
    assert {metric.name: (metric.type, metric.labels) for metric in APP_METRICS} == {
        "ccobs_sessions": ("gauge", ("host", "state")),
        "ccobs_sessions_ended_total": ("counter", ("host",)),
        "ccobs_blocks_open": ("gauge", ("host", "kind")),
        "ccobs_agents_running": ("gauge", ("host",)),
        "ccobs_block_duration_seconds": ("histogram", ("host", "kind")),
        "ccobs_hook_events_processed_total": ("counter", ("host",)),
        "ccobs_hook_events_received_total": ("counter", ("host", "event")),
        "ccobs_hook_events_dropped_total": ("counter", ("host", "reason")),
        "ccobs_hook_queue_depth": ("gauge", ("host",)),
        "ccobs_hook_queue_bytes": ("gauge", ("host",)),
        "ccobs_build_info": ("gauge", ("host", "version")),
        "ccobs_database_wal": ("gauge", ("host",)),
    }


@pytest.mark.parametrize("metric", APP_METRICS, ids=lambda metric: metric.name)
def test_each_metric_follows_the_naming_rules(metric: AppMetric) -> None:
    assert _NAME.fullmatch(metric.name)
    assert metric.name.startswith("ccobs_")
    assert all(_LABEL.fullmatch(label) for label in metric.labels)
    # The host label first, on every series (ADR 0022); never a session ID (ADR 0028).
    assert metric.labels[0] == HOST_LABEL
    assert "session_id" not in metric.labels
    assert len(set(metric.labels)) == len(metric.labels)
    assert metric.help
    assert metric.help.endswith(".")
    # Prometheus's conventions: counters end in _total, the histogram in its unit.
    assert metric.name.endswith("_total") == (metric.type == "counter")
    if metric.type == "histogram":
        assert metric.name.endswith("_seconds")


def test_names_are_unique_and_every_constant_is_listed() -> None:
    assert len({metric.name for metric in APP_METRICS}) == len(APP_METRICS)
    constants = {value for value in vars(app_metrics).values() if isinstance(value, AppMetric)}
    assert constants == set(APP_METRICS)


def test_the_states_are_every_status_but_ended() -> None:
    statuses = get_args(SessionStatus.__value__)
    assert tuple(status for status in statuses if status != "ended") == SESSION_STATES
    assert SESSIONS.labels == (HOST_LABEL, "state")


def test_the_kinds_are_the_block_kinds() -> None:
    assert get_args(BlockKind.__value__) == BLOCK_KINDS
    assert BLOCK_DURATION.labels == (HOST_LABEL, "kind")


def test_the_event_label_is_bounded_to_the_registered_events() -> None:
    assert (*ASYNC_COMMAND_EVENTS, *REGISTERED_HTTP_EVENTS) == RECEIVED_EVENTS
    assert (*RECEIVED_EVENTS, OTHER_EVENT) == EVENT_LABELS
    assert OTHER_EVENT not in RECEIVED_EVENTS
    assert HOOK_EVENTS_RECEIVED.labels == (HOST_LABEL, "event")
    assert HOOK_EVENTS_DROPPED.labels == (HOST_LABEL, "reason")


@pytest.mark.parametrize("name", [*ASYNC_COMMAND_EVENTS, *REGISTERED_HTTP_EVENTS])
def test_a_registered_event_is_its_own_label(name: str) -> None:
    assert event_label(name) == name


@pytest.mark.parametrize(
    "name",
    # Parsed by the app but not registered by ccobs setup; unknown; a case variant;
    # empty; a value built to blow up the label's cardinality.
    ["InstructionsLoaded", "PostToolBatch", "MadeUpEvent", "stop", "", "x" * 10_000],
)
def test_any_other_event_is_other(name: str) -> None:
    assert event_label(name) == OTHER_EVENT


def test_the_buckets_suit_human_waits() -> None:
    assert list(BLOCK_DURATION_BUCKETS) == sorted(set(BLOCK_DURATION_BUCKETS))
    assert BLOCK_DURATION_BUCKETS[0] == 1
    # Up to an hour; longer waits land in +Inf, which prometheus_client adds.
    assert BLOCK_DURATION_BUCKETS[-1] == 3600
    assert all(bound > 0 for bound in BLOCK_DURATION_BUCKETS)


def test_build_info_and_the_listener_constants() -> None:
    assert BUILD_INFO.labels == (HOST_LABEL, "version")
    assert METRICS_PATH == "/metrics"
    assert SCRAPE_JOB == "ccobs-app"
