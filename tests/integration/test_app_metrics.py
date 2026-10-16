"""The app's metrics in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. Prometheus scrapes the app's
metrics listener on the backend network every 15 s (job ``ccobs-app``, ADRs 0028 and
0030), and every series carries the stack's host label (ADR 0022). After a replay of
every hook fixture, the session, block and agent gauges must agree with the API, both
on the listener (published by compose.debug.yaml) and in Prometheus.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

import pytest
from app_support import (
    Samples,
    gauges_in,
    get,
    get_json,
    items,
    live_gauges,
    mapping,
    poll,
    samples,
)

from ccobs.domain.app_metrics import (
    AGENTS_RUNNING,
    APP_METRICS,
    BLOCKS_OPEN,
    SCRAPE_JOB,
    SESSIONS,
)
from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL
from tools.stack.replay import load_scenario, prepare_hooks, scenario_names, send_hooks

pytestmark = pytest.mark.integration

# Two scrapes (every 15 s, stack/prometheus/prometheus.yaml) and a margin.
SCRAPE_DEADLINE: Final = 35.0
# Seconds for the writer to apply what the route queued.
STORED_DEADLINE: Final = 15.0
GAUGES: Final = (SESSIONS.name, BLOCKS_OPEN.name, AGENTS_RUNNING.name)
# Its Elicitation opens a block that no permission query concerns (ADR 0029).
ELICITATION_SCENARIO: Final = "interactive-elicitation-idle"


def _stored_names() -> set[str]:
    """Every series name the app exports: a histogram's _bucket, _sum and _count."""
    names: set[str] = set()
    for metric in APP_METRICS:
        if metric.type == "histogram":
            names |= {f"{metric.name}_{suffix}" for suffix in ("bucket", "sum", "count")}
        else:
            names.add(metric.name)
    return names


def _query(prometheus_url: str, promql: str) -> list[dict[str, object]]:
    document = mapping(get_json(prometheus_url, "/api/v1/query", {"query": promql}), "query answer")
    assert document.get("status") == "success", document
    data = mapping(document.get("data"), "data")
    return [mapping(item, "series") for item in items(data.get("result"), "result")]


def _prometheus_gauges(prometheus_url: str) -> Samples:
    """The gauges as Prometheus last scraped them, keyed like ``samples``."""
    found: Samples = {}
    selector = f'{{__name__=~"{"|".join(GAUGES)}", job="{SCRAPE_JOB}"}}'
    for series in _query(prometheus_url, selector):
        metric = {key: str(value) for key, value in mapping(series["metric"], "metric").items()}
        name = metric.pop("__name__")
        for target_label in ("job", "instance"):
            metric.pop(target_label, None)
        value = items(series["value"], "value")
        found[(name, frozenset(metric.items()))] = float(str(value[1]))
    return found


def _app_gauges(app_metrics_url: str) -> Samples:
    return samples(get(app_metrics_url, "/metrics").decode())


def _describe(found: Mapping[tuple[str, frozenset[tuple[str, str]]], float]) -> str:
    return json.dumps(
        sorted([name, sorted(labels), value] for (name, labels), value in found.items())
    )


@pytest.fixture(scope="module")
def replayed(app_url: str) -> list[str]:
    """Every hook fixture, sent to the app, plus one cut right after its elicitation
    request, so that a block stays open; the session IDs."""
    runs = [prepare_hooks(load_scenario(name)) for name in scenario_names(with_hooks=True)]
    for run in runs:
        send_hooks(app_url, run.lines)
    waiting = prepare_hooks(load_scenario(ELICITATION_SCENARIO))
    events = [line.payload.get("hook_event_name") for line in waiting.lines]
    send_hooks(app_url, waiting.lines[: events.index("Elicitation") + 1])
    return [*(run.session_id for run in runs), waiting.session_id]


def test_the_app_target_is_up(prometheus_url: str) -> None:
    def health() -> list[str]:
        data = mapping(get_json(prometheus_url, "/api/v1/targets"), "targets answer")
        targets = items(mapping(data.get("data"), "data").get("activeTargets"), "targets")
        return [
            str(mapping(target, "target").get("health"))
            for target in targets
            if mapping(mapping(target, "target").get("labels"), "labels").get("job") == SCRAPE_JOB
        ]

    found = poll(health, lambda states: states == ["up"], SCRAPE_DEADLINE)
    assert found == ["up"]


def test_every_app_series_reaches_prometheus_with_the_host_label(prometheus_url: str) -> None:
    expected = _stored_names()

    def names() -> set[str]:
        selector = f'{{__name__=~"ccobs_.+", job="{SCRAPE_JOB}", {HOST_LABEL}="{DEFAULT_HOST}"}}'
        return {
            str(mapping(series["metric"], "metric")["__name__"])
            for series in _query(prometheus_url, f"group by (__name__) ({selector})")
        }

    found = poll(names, lambda got: got >= expected, SCRAPE_DEADLINE)
    assert found == expected
    # No ccobs series without the host label, or with another one.
    others = _query(
        prometheus_url, f'count({{__name__=~"ccobs_.+", {HOST_LABEL}!="{DEFAULT_HOST}"}})'
    )
    assert others == []


def test_the_gauges_match_the_api_after_a_replay(
    app_url: str, app_metrics_url: str, prometheus_url: str, replayed: list[str]
) -> None:
    assert replayed

    def compare_app() -> tuple[Samples, Samples]:
        return live_gauges(app_url, DEFAULT_HOST), gauges_in(_app_gauges(app_metrics_url), GAUGES)

    expected, listener = poll(compare_app, lambda pair: pair[0] == pair[1], STORED_DEADLINE)
    assert listener == expected, f"listener {_describe(listener)} != API {_describe(expected)}"
    host = (HOST_LABEL, DEFAULT_HOST)
    assert expected[(SESSIONS.name, frozenset({host, ("state", "idle")}))]
    assert expected[(SESSIONS.name, frozenset({host, ("state", "waiting_user")}))]
    assert expected[(BLOCKS_OPEN.name, frozenset({host, ("kind", "elicitation")}))]
    stored = poll(
        lambda: gauges_in(_prometheus_gauges(prometheus_url), GAUGES),
        lambda found: found == expected,
        SCRAPE_DEADLINE,
    )
    assert stored == expected, f"Prometheus {_describe(stored)} != API {_describe(expected)}"
