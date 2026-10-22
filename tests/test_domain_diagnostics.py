"""Unit tests of ``ccobs.domain.diagnostics``: the LogQL of the hook failure counts and the
parser of Loki's answer (ADR 0038).

The counts are those ``ccobs doctor`` reads (ADR 0011): ``num_non_blocking_error`` and
``num_cancelled`` of ``claude_code.hook_execution_complete``. The records come from the
``hooks-endpoint-down`` and ``hooks-endpoint-timeout`` OTLP fixtures, where every hook
failed (down) or was cancelled (timeout); their attributes reach Loki as structured
metadata under the same names (ADRs 0014 and 0015).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from ccobs.domain.denials import EVENT_NAME_KEY, SERVICE_NAME_LABEL
from ccobs.domain.diagnostics import (
    ALERTS_NOT_UNDERSTOOD,
    ALERTS_PREDATE,
    ALERTS_UNREAD,
    CANCELLED,
    CANCELLED_KEY,
    EXECUTIONS,
    FIELD_LABEL,
    HOOK_COMPLETE_EVENT,
    HOOK_FAILURES_WINDOW_S,
    NON_BLOCKING_ERROR,
    NON_BLOCKING_ERROR_KEY,
    UNAVAILABLE_REASONS,
    CountedHookFailures,
    Diagnostics,
    HookFailures,
    ReportedAlerts,
    ReportedDiscord,
    ReportedHooks,
    ReportedSessions,
    UnavailableReason,
    UncountedHookFailures,
    hook_failures_query,
    parse_diagnostics,
    parse_hook_failures,
)
from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL, SERVICE_NAME

FIXTURES: Final = Path(__file__).parent / "fixtures"
MANIFEST: Final = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
SESSION_IDS: Final = tuple(scenario["session_id"] for scenario in MANIFEST["scenarios"])


def complete_records(scenario: str) -> list[dict[str, str]]:
    """The string attributes of every ``hook_execution_complete`` record of a fixture."""
    found = []
    for line in (
        (FIXTURES / "otlp" / scenario / "logs.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        for resource in json.loads(line)["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    attributes = {
                        item["key"]: item["value"].get("stringValue")
                        for item in record["attributes"]
                    }
                    if attributes.get("event.name") == HOOK_COMPLETE_EVENT:
                        found.append(attributes)
    return found


def answer(values: dict[str, str]) -> dict[str, Any]:
    """Loki's answer to the query: one series per field, as an instant vector."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {FIELD_LABEL: name}, "value": [1_800_000_000.123, value]}
                for name, value in values.items()
            ],
            "stats": {"summary": {"execTime": 0.01}},
        },
    }


# The fixtures hold what the query reads.


@pytest.mark.parametrize(
    ("scenario", "key"),
    [("hooks-endpoint-down", NON_BLOCKING_ERROR_KEY), ("hooks-endpoint-timeout", CANCELLED_KEY)],
)
def test_the_fixtures_report_failures_under_the_unwrapped_keys(scenario: str, key: str) -> None:
    records = complete_records(scenario)

    assert records
    # Integer strings: what unwrap reads as a sample value.
    for record in records:
        assert record[NON_BLOCKING_ERROR_KEY].isdigit()
        assert record[CANCELLED_KEY].isdigit()
    assert sum(int(record[key]) for record in records) > 0


# The query.


def test_the_query_sums_the_three_counts_over_the_hosts_stream() -> None:
    selector = f'{{{SERVICE_NAME_LABEL}="{SERVICE_NAME}", {HOST_LABEL}="{DEFAULT_HOST}"}}'
    pipeline = f'{selector} | {EVENT_NAME_KEY}="{HOOK_COMPLETE_EVENT}"'

    assert hook_failures_query(DEFAULT_HOST, 3600) == (
        f'label_replace(sum(count_over_time({pipeline} [3600s])), "{FIELD_LABEL}",'
        f' "{EXECUTIONS}", "", "")'
        f" or label_replace(sum(sum_over_time({pipeline} | unwrap {NON_BLOCKING_ERROR_KEY}"
        f' | __error__="" [3600s])), "{FIELD_LABEL}", "{NON_BLOCKING_ERROR}", "", "")'
        f" or label_replace(sum(sum_over_time({pipeline} | unwrap {CANCELLED_KEY}"
        f' | __error__="" [3600s])), "{FIELD_LABEL}", "{CANCELLED}", "", "")'
    )


def test_the_window_is_an_hour_by_default() -> None:
    assert HOOK_FAILURES_WINDOW_S == 3600
    assert "[3600s]" in hook_failures_query(DEFAULT_HOST)
    assert "[60s]" in hook_failures_query(DEFAULT_HOST, 60)


def test_the_keys_are_the_attribute_names() -> None:
    # Structured metadata keys are the OTel attribute names, dots to underscores (ADR
    # 0015); these have no dot.
    record = complete_records("hooks-endpoint-timeout")[0]

    assert NON_BLOCKING_ERROR_KEY in record
    assert CANCELLED_KEY in record
    assert record["event.name"] == HOOK_COMPLETE_EVENT


def test_the_host_is_quoted() -> None:
    query = hook_failures_query('a"b\\c')

    assert 'host="a\\"b\\\\c"' in query
    assert query.count('host="') == 3


def test_the_query_carries_no_session_id() -> None:
    query = hook_failures_query(DEFAULT_HOST)

    assert "session" not in query
    assert SESSION_IDS
    for session_id in SESSION_IDS:
        assert session_id not in query


@pytest.mark.parametrize("window_s", [0, -1])
def test_a_window_that_is_not_positive_is_refused(window_s: int) -> None:
    with pytest.raises(ValueError, match="window"):
        hook_failures_query(DEFAULT_HOST, window_s)


def test_a_host_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        hook_failures_query("\ud800")


# The answer.


def test_an_answer_gives_the_three_counts() -> None:
    document = answer({EXECUTIONS: "12", NON_BLOCKING_ERROR: "5", CANCELLED: "4"})

    assert parse_hook_failures(document) == HookFailures(
        executions=12, non_blocking_error=5, cancelled=4
    )


def test_a_missing_series_counts_zero() -> None:
    # A sum over no record has no series: Loki leaves it out of the vector.
    assert parse_hook_failures(answer({})) == HookFailures(0, 0, 0)
    assert parse_hook_failures(answer({EXECUTIONS: "3"})) == HookFailures(3, 0, 0)


def test_an_integral_float_is_a_count() -> None:
    assert parse_hook_failures(answer({EXECUTIONS: "3.0", CANCELLED: "1e1"})) == HookFailures(
        3, 0, 10
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "garbage",
        {"status": "error", "data": {"resultType": "vector", "result": []}},
        {"status": "success", "data": {"resultType": "streams", "result": []}},
        {"status": "success", "data": {"resultType": "vector", "result": {}}},
        {"status": "success"},
        answer({EXECUTIONS: "NaN"}),
        answer({EXECUTIONS: "+Inf"}),
        answer({EXECUTIONS: "-1"}),
        answer({EXECUTIONS: "1.5"}),
        answer({EXECUTIONS: "many"}),
        answer({"other": "1"}),
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {FIELD_LABEL: EXECUTIONS}, "value": [1.0, "1"]},
                    {"metric": {FIELD_LABEL: EXECUTIONS}, "value": [1.0, "2"]},
                ],
            },
        },
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "1"]}]},
        },
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {FIELD_LABEL: EXECUTIONS}, "value": [1.0, 1]}],
            },
        },
    ],
)
def test_a_malformed_answer_is_rejected(document: object) -> None:
    assert parse_hook_failures(document) is None


# The client side: the app's diagnostics document, as doctor and status read it.


def diagnostics_document(**change: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": "0.5.0",
        "host": "local",
        "started_at_ms": 1,
        "sessions": {
            "by_state": {"working": 2, "waiting_user": 1, "idle": 0, "ended": 3, "stale": 0},
            "open_blocks": 1,
        },
        "hooks": {"received": {"Stop": 2, "other": 0}, "dropped": {"invalid": 1, "full": 0}},
        "hook_failures": {
            "available": True,
            "window_s": 3600,
            "executions": 12,
            "non_blocking_error": 1,
            "cancelled": 2,
        },
    }
    document.update(change)
    return document


def encoded(document: object) -> bytes:
    return json.dumps(document).encode()


def test_a_document_parses_into_its_parts() -> None:
    parsed = parse_diagnostics(encoded(diagnostics_document()))

    assert parsed == Diagnostics(
        version="0.5.0",
        sessions=ReportedSessions(
            by_state={"working": 2, "waiting_user": 1, "idle": 0, "ended": 3, "stale": 0},
            open_blocks=1,
        ),
        hooks=ReportedHooks(received={"Stop": 2, "other": 0}, dropped={"invalid": 1, "full": 0}),
        hook_failures=CountedHookFailures(
            window_s=3600, counts=HookFailures(executions=12, non_blocking_error=1, cancelled=2)
        ),
    )


def test_a_missing_state_counts_0_and_an_unknown_one_is_left_out() -> None:
    sessions = {"by_state": {"working": 1, "planted": 5}, "open_blocks": 0}

    parsed = parse_diagnostics(encoded(diagnostics_document(sessions=sessions)))

    assert parsed is not None
    assert parsed.sessions.by_state == {
        "working": 1,
        "waiting_user": 0,
        "idle": 0,
        "ended": 0,
        "stale": 0,
    }


@pytest.mark.parametrize("version", ["<planted>", "", "x" * 65, "0.5.0 beta"])
def test_an_odd_version_becomes_unknown(version: str) -> None:
    parsed = parse_diagnostics(encoded(diagnostics_document(version=version)))

    assert parsed is not None
    assert parsed.version == "unknown"


@pytest.mark.parametrize("reason", ["unreachable", "timeout", "status", "shape"])
def test_loki_unavailable_keeps_a_reason_of_adr_0038(reason: UnavailableReason) -> None:
    failures = {"available": False, "reason": reason, "window_s": 600}

    parsed = parse_diagnostics(encoded(diagnostics_document(hook_failures=failures)))

    assert parsed is not None
    assert parsed.hook_failures == UncountedHookFailures(window_s=600, reason=reason)


def test_the_reasons_are_those_of_adr_0038() -> None:
    assert frozenset(UNAVAILABLE_REASONS) == {"unreachable", "timeout", "status", "shape"}


@pytest.mark.parametrize("reason", [None, 7, "<b>", "planted", "Timeout"])
def test_any_other_reason_is_unknown(reason: object) -> None:
    failures = {"available": False, "reason": reason, "window_s": 3600}

    parsed = parse_diagnostics(encoded(diagnostics_document(hook_failures=failures)))

    assert parsed is not None
    assert parsed.hook_failures == UncountedHookFailures(window_s=3600, reason="unknown")


def test_a_reason_may_be_missing() -> None:
    failures = {"available": False, "window_s": 3600}

    parsed = parse_diagnostics(encoded(diagnostics_document(hook_failures=failures)))

    assert parsed is not None
    assert parsed.hook_failures == UncountedHookFailures(window_s=3600, reason="unknown")


_GOOD_FAILURES: Final = {
    "available": True,
    "window_s": 3600,
    "executions": 1,
    "non_blocking_error": 0,
    "cancelled": 0,
}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[" * 100_000, id="too-deep"),
        pytest.param(b"\xff", id="not-utf-8"),
        encoded([]),
        encoded(diagnostics_document(version=4)),
        encoded({k: v for k, v in diagnostics_document().items() if k != "version"}),
        encoded(diagnostics_document(sessions=[])),
        encoded(diagnostics_document(sessions={"by_state": [], "open_blocks": 0})),
        encoded(diagnostics_document(sessions={"by_state": {}, "open_blocks": -1})),
        encoded(diagnostics_document(sessions={"by_state": {}, "open_blocks": True})),
        encoded(diagnostics_document(sessions={"by_state": {}})),
        encoded(diagnostics_document(sessions={"by_state": {"idle": "1"}, "open_blocks": 0})),
        encoded(diagnostics_document(sessions={"by_state": {"idle": -1}, "open_blocks": 0})),
        encoded(diagnostics_document(hooks=[])),
        encoded(diagnostics_document(hooks={"received": {"Stop": "x"}, "dropped": {}})),
        encoded(diagnostics_document(hooks={"received": {"Stop": True}, "dropped": {}})),
        encoded(diagnostics_document(hooks={"received": {"Stop": -1}, "dropped": {}})),
        encoded(diagnostics_document(hooks={"received": {}, "dropped": []})),
        encoded(diagnostics_document(hooks={"received": {}})),
        encoded(diagnostics_document(hook_failures=None)),
        encoded(diagnostics_document(hook_failures=[])),
        encoded(diagnostics_document(hook_failures={"available": "yes", "window_s": 3600})),
        encoded(diagnostics_document(hook_failures={"available": True, "window_s": "1"})),
        encoded(diagnostics_document(hook_failures={"available": False, "reason": "timeout"})),
        encoded(diagnostics_document(hook_failures={**_GOOD_FAILURES, "window_s": 1.5})),
        encoded(diagnostics_document(hook_failures={**_GOOD_FAILURES, "cancelled": None})),
        encoded(diagnostics_document(hook_failures={**_GOOD_FAILURES, "executions": -1})),
        encoded(
            diagnostics_document(
                hook_failures={k: v for k, v in _GOOD_FAILURES.items() if k != "executions"}
            )
        ),
    ],
)
def test_a_malformed_document_is_rejected(body: bytes) -> None:
    assert parse_diagnostics(body) is None


# The alerts part (ADR 0046): optional, since an app older than v0.6.0 has none.

_GOOD_DISCORD: Final[dict[str, object]] = {
    "configured": True,
    "suspended_status": None,
    "pending": 1,
    "failed_24h": 2,
    "expired_24h": 3,
}


def alerts_document(**change: object) -> dict[str, object]:
    alerts: dict[str, object] = {
        "rules": ["waiting_user", "repeated_failures"],
        "open": 4,
        "fired_24h": 5,
        "discord": dict(_GOOD_DISCORD),
    }
    alerts.update(change)
    return alerts


def test_the_alerts_part_parses() -> None:
    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts_document())))

    assert parsed is not None
    assert parsed.alerts == ReportedAlerts(
        rules=("waiting_user", "repeated_failures"),
        open=4,
        fired_24h=5,
        discord=ReportedDiscord(
            configured=True, suspended_status=None, pending=1, failed_24h=2, expired_24h=3
        ),
    )


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_suspension_keeps_its_status(status: int) -> None:
    discord = {**_GOOD_DISCORD, "suspended_status": status}

    parsed = parse_diagnostics(
        encoded(diagnostics_document(alerts=alerts_document(discord=discord)))
    )

    assert parsed is not None
    assert isinstance(parsed.alerts, ReportedAlerts)
    assert parsed.alerts.discord.suspended_status == status


def test_the_rules_keep_adr_0041s_order_and_leave_unknown_names_out() -> None:
    rules = ["daily_cost", "planted", "waiting_user", "daily_cost"]

    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts_document(rules=rules))))

    assert parsed is not None
    assert isinstance(parsed.alerts, ReportedAlerts)
    assert parsed.alerts.rules == ("waiting_user", "daily_cost")


def test_no_rule_on_is_an_empty_tuple() -> None:
    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts_document(rules=[]))))

    assert parsed is not None
    assert isinstance(parsed.alerts, ReportedAlerts)
    assert parsed.alerts.rules == ()


def test_an_app_that_predates_alerts_still_parses() -> None:
    # A v0.5.0 app: no alerts field at all.
    parsed = parse_diagnostics(encoded(diagnostics_document()))

    assert parsed is not None
    assert parsed.alerts == ALERTS_PREDATE
    assert parsed.sessions.open_blocks == 1


def test_alerts_the_app_could_not_read_are_unread() -> None:
    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=None)))

    assert parsed is not None
    assert parsed.alerts == ALERTS_UNREAD


def test_the_three_markers_differ() -> None:
    assert len({ALERTS_PREDATE, ALERTS_UNREAD, ALERTS_NOT_UNDERSTOOD}) == 3


@pytest.mark.parametrize(
    "alerts",
    [
        pytest.param([], id="list"),
        pytest.param("on", id="text"),
        pytest.param(0, id="number"),
        pytest.param({}, id="empty"),
        alerts_document(rules=None),
        alerts_document(rules="waiting_user"),
        alerts_document(rules=[1]),
        alerts_document(rules=[None]),
        alerts_document(open=-1),
        alerts_document(open=True),
        alerts_document(open="1"),
        alerts_document(open=None),
        alerts_document(fired_24h=1.5),
        {k: v for k, v in alerts_document().items() if k != "fired_24h"},
        alerts_document(discord=None),
        alerts_document(discord=[]),
        alerts_document(discord={**_GOOD_DISCORD, "configured": "yes"}),
        alerts_document(discord={**_GOOD_DISCORD, "configured": 1}),
        alerts_document(discord={**_GOOD_DISCORD, "suspended_status": "404"}),
        alerts_document(discord={**_GOOD_DISCORD, "suspended_status": True}),
        alerts_document(discord={**_GOOD_DISCORD, "suspended_status": 99}),
        alerts_document(discord={**_GOOD_DISCORD, "suspended_status": 600}),
        alerts_document(discord={**_GOOD_DISCORD, "pending": -1}),
        alerts_document(discord={**_GOOD_DISCORD, "failed_24h": None}),
        alerts_document(discord={**_GOOD_DISCORD, "expired_24h": 2.0}),
        alerts_document(discord={k: v for k, v in _GOOD_DISCORD.items() if k != "pending"}),
        alerts_document(
            discord={k: v for k, v in _GOOD_DISCORD.items() if k != "suspended_status"}
        ),
    ],
)
def test_a_malformed_alerts_part_is_not_understood_and_the_rest_stands(alerts: object) -> None:
    # A bad part is not half shown, but it does not hide the parts that parse: an app
    # newer or older than the CLI may change it (ADR 0046).
    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts)))

    without_alerts = parse_diagnostics(encoded(diagnostics_document()))
    assert without_alerts is not None
    assert parsed == replace(without_alerts, alerts=ALERTS_NOT_UNDERSTOOD)


def test_extra_keys_in_the_alerts_part_are_accepted() -> None:
    # Forward compatible: a newer app may add fields to alerts and to discord.
    discord = {**_GOOD_DISCORD, "last_sent_at_ms": 1}
    alerts = {**alerts_document(discord=discord), "muted": 0}

    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts)))

    assert parsed is not None
    assert parsed.alerts == ReportedAlerts(
        rules=("waiting_user", "repeated_failures"),
        open=4,
        fired_24h=5,
        discord=ReportedDiscord(
            configured=True, suspended_status=None, pending=1, failed_24h=2, expired_24h=3
        ),
    )


def test_a_changed_alerts_field_type_keeps_the_sessions() -> None:
    # A newer app that turned open into an object: alerts not understood, sessions read.
    alerts = alerts_document(open={"count": 4})

    parsed = parse_diagnostics(encoded(diagnostics_document(alerts=alerts)))

    assert parsed is not None
    assert parsed.alerts == ALERTS_NOT_UNDERSTOOD
    assert parsed.sessions.open_blocks == 1
    assert parsed.sessions.by_state["working"] == 2
    assert isinstance(parsed.hook_failures, CountedHookFailures)
