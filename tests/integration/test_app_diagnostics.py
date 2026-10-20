"""The diagnostics route against the real stack, in the isolated ``ccobs-it`` compose
project (ADR 0038).

Needs Docker: ``uv run pytest -m integration --no-cov``. The log records of the
``hooks-endpoint-down`` (every hook failed: ``num_non_blocking_error``) and
``hooks-endpoint-timeout`` (every hook cut short: ``num_cancelled``) fixtures are
replayed through the collector with fresh session IDs, stamped now; the app's route must
then report them from Loki, which checks what ADR 0038 left to verify: that the app's
exact LogQL (``label_replace`` of three sums, ``unwrap`` on structured metadata) is
accepted and counts the records and both attributes. The counts are compared as the
change from a reading taken before the replay, since other tests write to the same
stream. The query names no session, so Loki's query log never records one
(test_app_privacy.py checks that log).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Final

import pytest
from app_support import get_json, mapping, poll, request

from ccobs.domain.app_metrics import EVENT_LABELS
from ccobs.domain.diagnostics import HOOK_COMPLETE_EVENT, HOOK_FAILURES_WINDOW_S
from ccobs.domain.telemetry import DEFAULT_HOST
from ccobs.server.ingest import DROP_REASONS
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import Prepared, load_scenario, prepare, send

pytestmark = pytest.mark.integration

URL: Final = "/api/v1/diagnostics"
SCENARIOS: Final = ("hooks-endpoint-down", "hooks-endpoint-timeout")
STATUSES: Final = ("working", "waiting_user", "idle", "ended", "stale")
# Seconds for the replayed records to be counted: the collector's batching, Loki's
# ingestion and the query, with a margin.
DEADLINE: Final = 60.0
COUNTS: Final = ("executions", "non_blocking_error", "cancelled")


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _records(run: Prepared) -> Iterator[dict[str, str]]:
    """The string attributes of each log record of a prepared run."""
    for exported in run.logs:
        for resource in _objects(exported.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                for record in _objects(scope.get("logRecords")):
                    attributes: dict[str, str] = {}
                    for attribute in _objects(record.get("attributes")):
                        value = attribute.get("value")
                        text = value.get("stringValue") if isinstance(value, dict) else None
                        if isinstance(attribute.get("key"), str) and isinstance(text, str):
                            attributes[str(attribute["key"])] = text
                    yield attributes


def expected_counts(runs: list[Prepared]) -> dict[str, int]:
    """What the replay adds to each count, from the records themselves."""
    complete = [
        record
        for run in runs
        for record in _records(run)
        if record.get("event.name") == HOOK_COMPLETE_EVENT
    ]
    return {
        "executions": len(complete),
        "non_blocking_error": sum(int(r["num_non_blocking_error"]) for r in complete),
        "cancelled": sum(int(r["num_cancelled"]) for r in complete),
    }


def diagnostics(app_url: str) -> dict[str, object]:
    return mapping(get_json(app_url, URL), "diagnostics")


def failures(app_url: str) -> dict[str, object]:
    return mapping(diagnostics(app_url)["hook_failures"], "hook_failures")


def test_the_route_answers_with_every_field(app_url: str) -> None:
    document = diagnostics(app_url)

    assert document["host"] == DEFAULT_HOST
    assert isinstance(document["started_at_ms"], int)
    sessions = mapping(document["sessions"], "sessions")
    assert sorted(mapping(sessions["by_state"], "by_state")) == sorted(STATUSES)
    hooks = mapping(document["hooks"], "hooks")
    assert sorted(mapping(hooks["received"], "received")) == sorted(EVENT_LABELS)
    assert sorted(mapping(hooks["dropped"], "dropped")) == sorted(DROP_REASONS)
    assert failures(app_url)["window_s"] == HOOK_FAILURES_WINDOW_S


def settled_failures(app_url: str) -> dict[str, object]:
    """The counts once two readings 2 s apart agree, so that records another test sent
    just before (still in the collector's batch) are counted before the replay."""
    previous = failures(app_url)
    for _ in range(10):
        time.sleep(2)
        current = failures(app_url)
        if all(current.get(name) == previous.get(name) for name in COUNTS):
            return current
        previous = current
    return previous


def test_the_hook_failure_counts_come_from_loki(app_url: str, otlp_http_url: str) -> None:
    before = settled_failures(app_url)
    assert before["available"] is True, before
    runs = [prepare(load_scenario(name), now_ns=time.time_ns()) for name in SCENARIOS]
    added = expected_counts(runs)
    # The fixtures hold failures of both kinds.
    assert all(added[name] > 0 for name in COUNTS), added
    for run in runs:
        assert send(otlp_http_url, run, signals=("logs",)) == len(run.logs)
    target = {name: int(str(before[name])) + added[name] for name in COUNTS}

    def reached(found: dict[str, object]) -> bool:
        return found.get("available") is True and all(
            int(str(found[name])) >= target[name] for name in COUNTS
        )

    after = poll(lambda: failures(app_url), reached, DEADLINE)

    assert after["available"] is True, after
    # At least the replayed amounts, not equal: a late batch from another test may still
    # land in the window. added > 0 above still proves the unwrap sums are read. With
    # CCOBS_IT_KEEP=1 and records near the 1 h edge, old records can leave the window
    # meanwhile; rerun on a fresh project if that ever bites.
    assert all(int(str(after[name])) >= target[name] for name in COUNTS), (after, target)


def test_a_foreign_host_and_a_cross_site_fetch_are_refused(app_url: str) -> None:
    foreign = request(app_url, "GET", URL, headers={"Host": "evil.example"})
    cross_site = request(
        app_url,
        "GET",
        URL,
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
    )
    posted = request(app_url, "POST", URL, body=b"{}")

    assert foreign.status == 400
    assert cross_site.status == 403
    assert posted.status == 405
