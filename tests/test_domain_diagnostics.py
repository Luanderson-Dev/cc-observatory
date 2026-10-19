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
from pathlib import Path
from typing import Any, Final

import pytest

from ccobs.domain.denials import EVENT_NAME_KEY, SERVICE_NAME_LABEL
from ccobs.domain.diagnostics import (
    CANCELLED,
    CANCELLED_KEY,
    EXECUTIONS,
    FIELD_LABEL,
    HOOK_COMPLETE_EVENT,
    HOOK_FAILURES_WINDOW_S,
    NON_BLOCKING_ERROR,
    NON_BLOCKING_ERROR_KEY,
    HookFailures,
    hook_failures_query,
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
