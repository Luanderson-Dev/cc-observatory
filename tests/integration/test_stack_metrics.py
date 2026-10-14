"""Claude Code's metrics in Prometheus, in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. The metric requests of every
fixture scenario are replayed through the collector with fresh session IDs and read back
from Prometheus on its debug port (ADR 0013). Totals come from
``increase(x[1h] anchored)`` and must equal the replayer's expected totals exactly: with
delta input (Claude Code's default) sent as concurrent sessions, with cumulative input,
for sessions that export once, and across a collector restart in the middle of a
session where no continuing series is contiguous. A strict xfail test records the
contiguous case, a documented limit of ADR 0013. Every query filters on the run's
session IDs, since the other modules of the test session share this Prometheus.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest

from tools.capture.jsontypes import JSONObject
from tools.stack.replay import (
    CANARY_PREFIX,
    Number,
    Prepared,
    SeriesKey,
    contiguous_series,
    expected_totals,
    load_scenario,
    post_otlp,
    prepare,
    scenario_names,
    send,
)

pytestmark = pytest.mark.integration

COLLECTOR: Final = "otel-collector"
PROMETHEUS: Final = "prometheus"
# The job Prometheus derives from service.name, and its two scrape jobs.
JOB: Final = "claude-code"
COLLECTOR_JOB: Final = "otel-collector"
PROMETHEUS_JOB: Final = "prometheus"
PROMETHEUS_EXPORTER: Final = "otlp_http/prometheus"
OTLP_HANDLER: Final = "/api/v1/otlp/v1/metrics"
# Claude Code's metrics as Prometheus names them with the default OTLP translation:
# dots become underscores, the unit and _total are appended (ADR 0013).
PROMETHEUS_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "claude_code.session.count": "claude_code_session_count_total",
        "claude_code.active_time.total": "claude_code_active_time_seconds_total",
        "claude_code.token.usage": "claude_code_token_usage_tokens_total",
        "claude_code.cost.usage": "claude_code_cost_usage_USD_total",
        "claude_code.lines_of_code.count": "claude_code_lines_of_code_count_total",
        "claude_code.code_edit_tool.decision": "claude_code_code_edit_tool_decision_total",
        "claude_code.commit.count": "claude_code_commit_count_total",
    }
)
EXPECTED_NAMES: Final = frozenset(PROMETHEUS_NAMES.values())
# Totals are increase(... anchored), never plain increase(), rate() or sum_over_time()
# (ADR 0013). The range covers every replayed sample: a session spans at most 150 s.
INCREASE: Final = "increase({selector}[1h] anchored)"
LAST: Final = "last_over_time({selector}[1h])"
RESETS: Final = "resets({selector}[1h])"
RANGE_SECONDS: Final = 3600
# Replayed samples carry this machine's timestamps, and every query passes an explicit
# time from the same clock, so ranges line up whatever the Docker VM's clock says;
# queries are evaluated this many seconds ahead only as a margin past the latest sample.
EVALUATION_MARGIN: Final = 60.0
# The risk is the VM's clock (Prometheus's own) being far from this machine's: Prometheus
# then refuses the replayed samples as too old for its 30m out-of-order window, or they
# land far from its own samples. WSL2 clocks drift after the machine sleeps. Beyond this
# many seconds the metrics tests stop at once, saying how to fix it.
MAX_CLOCK_SKEW: Final = 120.0
# Seconds to wait until the replayed totals can be queried, and between two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
# Seconds for the collector to be healthy again after a restart, and for Prometheus to
# scrape every target once more (it scrapes every 15 s).
HEALTHY_DEADLINE: Final = 120.0
SCRAPE_DEADLINE: Final = 60.0
HTTP_TIMEOUT: Final = 10.0
REL_TOL: Final = 1e-9
# Sessions whose metrics arrive in one export: one sample per series.
SINGLE_EXPORT: Final = ("headless-api-error", "hooks-endpoint-down")
# Collector restarts: the scenario, and how many of its metric requests go before the
# restart. After 5 requests of the session with the most exports, twelve series go on
# and none is contiguous (its next point starts where its last one ended), so totals
# stay exact. After 1 request of the second, all seven continuing series are contiguous:
# the new streams' zero samples duplicate the last stored samples, and Prometheus drops
# them (the limit under Temporality in ADR 0013).
RESTART_SCENARIO: Final = "interactive-permission-compact"
RESTART_SPLIT: Final = 5
CONTIGUOUS_RESTART_SCENARIO: Final = "headless-edit-commit-tasks"
CONTIGUOUS_RESTART_SPLIT: Final = 1
PROMETHEUS_CONFIG: Final = "/etc/ccobs/prometheus/prometheus.yaml"
# Identity and content as Prometheus would name them; the collector deletes them
# (ADR 0021).
IDENTITY_PREFIXES: Final = ("user_", "enduser_")
FORBIDDEN_LABELS: Final = frozenset(
    {"organization_id", "identity_source", "host_name", "workspace_host_paths"}
)

type Labels = frozenset[tuple[str, str]]
# A stored series: its Prometheus name and labels, without job.
type Key = tuple[str, Labels]
type Totals = dict[Key, float]


def _metric_scenarios() -> list[str]:
    """The scenarios that carry metrics, read at collection time."""
    return [name for name in scenario_names(with_otlp=True) if load_scenario(name).metrics]


METRIC_SCENARIOS: Final = _metric_scenarios()


@dataclass(frozen=True)
class Replay:
    """The scenarios sent, by name, and each one's totals as Prometheus stored them, once
    they all matched or the deadline passed (the tests then report the difference)."""

    runs: Mapping[str, Prepared]
    stored: Mapping[str, Totals]

    def session_ids(self) -> list[str]:
        return [run.session_id for run in self.runs.values()]


# Expected totals.


def _label(key: str) -> str:
    """The Prometheus label of an OTel attribute: every character that is not a letter,
    a digit or an underscore becomes ``_`` (``session.id`` -> ``session_id``)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def _key(series: SeriesKey) -> Key:
    return PROMETHEUS_NAMES[series.metric], frozenset((_label(k), v) for k, v in series.labels)


def _keyed(totals: Mapping[SeriesKey, Number]) -> Totals:
    """The replayer's totals keyed as Prometheus stores the series."""
    keyed = {_key(series): float(total) for series, total in totals.items()}
    assert len(keyed) == len(totals), "two series would share one Prometheus series"
    return keyed


def _added(*parts: Mapping[SeriesKey, Number]) -> Totals:
    added: Totals = {}
    for part in parts:
        for key, total in _keyed(part).items():
            added[key] = added.get(key, 0.0) + total
    return added


def _close(found: float, expected: float) -> bool:
    return math.isclose(found, expected, rel_tol=REL_TOL)


def _matches(found: Totals, expected: Totals) -> bool:
    return found.keys() == expected.keys() and all(
        _close(found[key], total) for key, total in expected.items()
    )


def _describe(key: Key) -> str:
    name, labels = key
    return name + "{" + ",".join(f"{k}={v!r}" for k, v in sorted(labels)) + "}"


def _assert_totals(found: Totals, expected: Totals, what: str) -> None:
    missing = sorted(_describe(key) for key in expected.keys() - found.keys())
    extra = sorted(_describe(key) for key in found.keys() - expected.keys())
    wrong = sorted(
        f"{_describe(key)}: stored {found[key]!r}, expected {total!r}"
        for key, total in expected.items()
        if key in found and not _close(found[key], total)
    )
    assert not missing, f"{what}: not stored {missing}"
    assert not extra, f"{what}: unexpected series {extra}"
    assert not wrong, f"{what}: wrong totals {wrong}"


# Talking to Prometheus.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _get(base: str, path: str, params: Sequence[tuple[str, str]]) -> object:
    """The ``data`` of a successful answer to ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        connection.request("GET", f"{path}?{urlencode(params)}")
        response = connection.getresponse()
        status, body = response.status, response.read()
    finally:
        connection.close()
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    document = _mapping(json.loads(body), path)
    assert document.get("status") == "success", document
    return document.get("data")


def _selector(name: str, labels: Mapping[str, str]) -> str:
    """A PromQL selector; JSON string escapes are valid PromQL string escapes."""
    matchers = ",".join(f"{key}={json.dumps(value)}" for key, value in sorted(labels.items()))
    return f"{name}{{{matchers}}}"


def _sessions_selector(session_ids: Iterable[str], name: str | None = None) -> str:
    """A selector of the metric ``name`` (by default, of every Claude Code metric) in
    these sessions. Session IDs are UUIDs: nothing to escape in the regular expression."""
    sessions = f'job="{JOB}",session_id=~"{"|".join(session_ids)}"'
    if name is None:
        return f'{{__name__=~"claude_code_.*",{sessions}}}'
    return f"{name}{{{sessions}}}"


def _query(base: str, promql: str, *, ahead: bool = True) -> list[tuple[dict[str, str], float]]:
    """Each series of the instant vector ``promql`` gives, with its value; evaluated
    ``EVALUATION_MARGIN`` seconds ahead, or at Prometheus's own time without ``ahead``."""
    params = [("query", promql)]
    if ahead:
        params.append(("time", f"{time.time() + EVALUATION_MARGIN:.3f}"))
    data = _mapping(_get(base, "/api/v1/query", params), "data")
    assert data.get("resultType") == "vector", f"{promql}: {data}"
    found: list[tuple[dict[str, str], float]] = []
    for item in _list(data.get("result"), "result"):
        entry = _mapping(item, "result entry")
        labels = {key: str(value) for key, value in _mapping(entry.get("metric"), "metric").items()}
        found.append((labels, float(str(_list(entry.get("value"), "value")[1]))))
    return found


def _scalar(base: str, promql: str, *, ahead: bool = True) -> float:
    """The value of ``promql``, which must give exactly one series."""
    found = _query(base, promql, ahead=ahead)
    assert len(found) == 1, f"{promql} gave {found}"
    return found[0][1]


def _series(base: str, *matchers: str) -> list[dict[str, str]]:
    """The label sets of every series that any of ``matchers`` selects in the last hour."""
    now = time.time()
    params = [("match[]", matcher) for matcher in matchers]
    params += [("start", f"{now - RANGE_SECONDS:.3f}"), ("end", f"{now + EVALUATION_MARGIN:.3f}")]
    found = _list(_get(base, "/api/v1/series", params), "series")
    return [{key: str(value) for key, value in _mapping(item, "series").items()} for item in found]


def _stored(base: str, run: Prepared, template: str) -> Totals:
    """Each series of ``run`` as ``template`` (such as ``INCREASE``) evaluates it."""
    found: Totals = {}
    for name in sorted({PROMETHEUS_NAMES[series.metric] for series in run.expected}):
        selector = _selector(name, {"job": JOB, "session_id": run.session_id})
        for labels, value in _query(base, template.format(selector=selector)):
            rest = {key: item for key, item in labels.items() if key not in ("__name__", "job")}
            found[(name, frozenset(rest.items()))] = value
    return found


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


def _wait_for_totals(base: str, runs: Mapping[str, Prepared]) -> dict[str, Totals]:
    expected = {name: _keyed(run.expected) for name, run in runs.items()}

    def read() -> dict[str, Totals]:
        return {name: _stored(base, run, INCREASE) for name, run in runs.items()}

    def done(found: dict[str, Totals]) -> bool:
        return all(_matches(found[name], expected[name]) for name in runs)

    return _poll(read, done, DEADLINE)


def _interleaved(runs: Iterable[Prepared]) -> Iterator[JSONObject]:
    """The metric requests of ``runs`` as concurrent sessions send them: the n-th request
    of every session before the next one of any."""
    queues = [list(run.metrics) for run in runs]
    for index in range(max((len(queue) for queue in queues), default=0)):
        for queue in queues:
            if index < len(queue):
                yield queue[index]


# Fixtures.


@pytest.fixture(scope="module")
def clock_skew(prometheus_url: str) -> float:
    """Seconds by which Prometheus's clock (the Docker VM's) is ahead of this machine's.
    Beyond ``MAX_CLOCK_SKEW`` it fails at once, with how to fix it: every test that sends
    metrics depends on it."""
    before = time.time()
    server = _scalar(prometheus_url, "vector(time())", ahead=False)
    skew = server - (before + time.time()) / 2
    if abs(skew) > MAX_CLOCK_SKEW:
        pytest.fail(
            f"Prometheus's clock (the Docker VM's) is {skew:+.0f} s off this machine's, more "
            f"than {MAX_CLOCK_SKEW:.0f} s: it would refuse or misplace the replayed samples, "
            "which carry this machine's time. Restart Docker Desktop (on Windows, run "
            "`wsl --shutdown` first, then start Docker Desktop) and run the tests again.",
            pytrace=False,
        )
    return skew


@pytest.fixture(scope="module")
def delta(otlp_http_url: str, prometheus_url: str, clock_skew: float) -> Replay:
    """Every scenario with metrics, in delta temporality (Claude Code's default) and with
    the privacy canaries planted, sent as concurrent sessions."""
    now_ns = time.time_ns()
    runs = {
        name: prepare(load_scenario(name), now_ns=now_ns, canaries=True)
        for name in METRIC_SCENARIOS
    }
    for request in _interleaved(runs.values()):
        post_otlp(otlp_http_url, "metrics", request)
    return Replay(runs, _wait_for_totals(prometheus_url, runs))


@pytest.fixture(scope="module")
def cumulative(otlp_http_url: str, prometheus_url: str, clock_skew: float) -> Replay:
    """Every scenario with metrics, in the cumulative temporality that ``ccobs setup``
    asks Claude Code for (ADR 0013), one session after the other."""
    now_ns = time.time_ns()
    runs = {
        name: prepare(load_scenario(name), now_ns=now_ns, temporality="cumulative")
        for name in METRIC_SCENARIOS
    }
    for run in runs.values():
        send(otlp_http_url, run, signals=("metrics",))
    return Replay(runs, _wait_for_totals(prometheus_url, runs))


# Tests.


def test_prometheus_is_healthy(service_health: Callable[[str], str]) -> None:
    assert service_health(PROMETHEUS) == "healthy"


def test_prometheus_clock_is_close_to_this_machines(clock_skew: float) -> None:
    assert abs(clock_skew) <= MAX_CLOCK_SKEW


def test_promtool_accepts_the_mounted_config(
    compose: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    # The same check as promtool in the image, on the file Prometheus actually reads.
    result = compose("exec", "-T", PROMETHEUS, "promtool", "check", "config", PROMETHEUS_CONFIG)
    assert "SUCCESS" in result.stdout + result.stderr, result.stdout + result.stderr


def test_prometheus_runs_with_the_receiver_its_features_and_retention(prometheus_url: str) -> None:
    flags = _mapping(_get(prometheus_url, "/api/v1/status/flags", []), "flags")
    assert flags.get("web.enable-otlp-receiver") == "true"
    features = str(flags.get("enable-feature")).split(",")
    assert sorted(features) == [
        "created-timestamp-zero-ingestion",
        "promql-extended-range-selectors",
    ]
    # 90d from .env.example.
    assert flags.get("storage.tsdb.retention.time") == "90d"
    for flag in (
        "web.enable-admin-api",
        "web.enable-lifecycle",
        "web.enable-remote-write-receiver",
    ):
        assert flags.get(flag) == "false", flag
    config = _mapping(_get(prometheus_url, "/api/v1/status/config", []), "config")
    assert "out_of_order_time_window: 30m" in str(config.get("yaml"))


def test_the_fixtures_carry_metrics() -> None:
    assert len(METRIC_SCENARIOS) >= len(SINGLE_EXPORT) + 2, METRIC_SCENARIOS
    assert RESTART_SCENARIO in METRIC_SCENARIOS
    assert set(SINGLE_EXPORT) <= set(METRIC_SCENARIOS)


@pytest.mark.parametrize("scenario", METRIC_SCENARIOS)
def test_delta_totals_are_exact_for_every_series(
    prometheus_url: str, delta: Replay, scenario: str
) -> None:
    # Claude Code's consecutive deltas are often not contiguous (ADR 0013), and the
    # sessions are interleaved; neither may change a total.
    run = delta.runs[scenario]
    assert run.temporality == "delta"
    _assert_totals(delta.stored[scenario], _keyed(run.expected), scenario)
    for series, total in run.expected.items():
        name, labels = _key(series)
        # label="" matches series without the label: one series, not those with more.
        others = {key for other in run.expected_series(series.metric) for key, _ in _key(other)[1]}
        absent = dict.fromkeys(others - dict(labels).keys(), "")
        selector = _selector(name, {"job": JOB, **dict(labels), **absent})
        # With no restart there is no reset, so the last value is the total too.
        for template in (INCREASE, LAST):
            promql = template.format(selector=selector)
            found = _query(prometheus_url, promql)
            assert len(found) == 1, f"{promql} gave {found}"
            stored, value = found[0]
            rest = {key: item for key, item in stored.items() if key not in ("__name__", "job")}
            assert rest == dict(labels), promql
            assert _close(value, float(total)), f"{promql} = {value!r}, expected {total!r}"


@pytest.mark.parametrize("scenario", SINGLE_EXPORT)
def test_sessions_that_export_once_count_their_first_value(
    prometheus_url: str, delta: Replay, scenario: str
) -> None:
    # One sample per series: only the zero sample at each stream's start lets increase()
    # count it (created-timestamp-zero-ingestion, ADR 0013).
    run = delta.runs[scenario]
    assert len(run.metrics) == 1, f"{scenario} must export once"
    session = {"job": JOB, "session_id": run.session_id}
    count = _selector("claude_code_session_count_total", session)
    assert _scalar(prometheus_url, f"sum(increase({count}[1h] anchored))") == 1
    for metric in sorted({series.metric for series in run.expected}):
        selector = _selector(PROMETHEUS_NAMES[metric], session)
        found = _scalar(prometheus_url, f"sum({INCREASE.format(selector=selector)})")
        expected = float(run.expected_total(metric))
        assert _close(found, expected), f"{scenario} {metric}: {found!r}, expected {expected!r}"


@pytest.mark.parametrize("metric", ["claude_code.token.usage", "claude_code.cost.usage"])
def test_concurrent_sessions_add_up_by_model(
    prometheus_url: str, delta: Replay, metric: str
) -> None:
    expected: dict[str, float] = defaultdict(float)
    for run in delta.runs.values():
        for series, total in run.expected_series(metric).items():
            expected[series.label("model") or ""] += total
    assert len(expected) >= 1
    selector = _sessions_selector(delta.session_ids(), PROMETHEUS_NAMES[metric])
    promql = f"sum by (model) ({INCREASE.format(selector=selector)})"
    found = {labels.get("model", ""): value for labels, value in _query(prometheus_url, promql)}
    assert found.keys() == expected.keys()
    for model, total in expected.items():
        assert _close(found[model], total), (
            f"{metric} {model}: {found[model]!r}, expected {total!r}"
        )


@pytest.mark.parametrize("metric", sorted(PROMETHEUS_NAMES))
def test_concurrent_sessions_keep_their_own_totals(
    prometheus_url: str, delta: Replay, metric: str
) -> None:
    expected = {
        run.session_id: float(sum(run.expected_series(metric).values()))
        for run in delta.runs.values()
        if run.expected_series(metric)
    }
    selector = _sessions_selector(delta.session_ids(), PROMETHEUS_NAMES[metric])
    promql = f"sum by (session_id) ({INCREASE.format(selector=selector)})"
    found = {
        labels.get("session_id", ""): value for labels, value in _query(prometheus_url, promql)
    }
    assert found.keys() == expected.keys(), metric
    for session_id, total in expected.items():
        assert _close(found[session_id], total), f"{metric} {session_id}: {found[session_id]!r}"


@pytest.mark.parametrize("scenario", METRIC_SCENARIOS)
def test_cumulative_totals_are_exact_for_every_series(
    prometheus_url: str, cumulative: Replay, scenario: str
) -> None:
    run = cumulative.runs[scenario]
    assert run.temporality == "cumulative"
    _assert_totals(cumulative.stored[scenario], _keyed(run.expected), scenario)
    last = _stored(prometheus_url, run, LAST)
    _assert_totals(last, _keyed(run.expected), f"{scenario}, last values")


def test_no_identity_or_content_label_reaches_prometheus(
    prometheus_url: str, delta: Replay, cumulative: Replay
) -> None:
    # The delta replay planted identity canaries (user.*, enduser.*, organization.id,
    # identity.source, host.name) and workspace.host_paths on every resource and data
    # point; the fixtures already carry user.id, user.email and the account IDs.
    session_ids = delta.session_ids() + cumulative.session_ids()
    found = _series(prometheus_url, _sessions_selector(session_ids), f'target_info{{job="{JOB}"}}')
    assert found
    labels = {label for series in found for label in series}
    assert not [label for label in labels if label.startswith(IDENTITY_PREFIXES)], labels
    assert not labels & FORBIDDEN_LABELS, sorted(labels & FORBIDDEN_LABELS)
    values = [value for series in found for value in series.values()]
    assert not [value for value in values if CANARY_PREFIX in value]


def test_metric_names_and_labels_are_the_translated_attributes(
    prometheus_url: str, delta: Replay
) -> None:
    from_fixtures = {_key(series)[0] for run in delta.runs.values() for series in run.expected}
    assert from_fixtures == EXPECTED_NAMES
    # Any metric of the run's sessions, whatever its name.
    matcher = f'{{job="{JOB}",session_id=~"{"|".join(delta.session_ids())}"}}'
    end = time.time() + EVALUATION_MARGIN
    params = [("match[]", matcher), ("start", f"{end - RANGE_SECONDS:.3f}"), ("end", f"{end:.3f}")]
    names = _list(_get(prometheus_url, "/api/v1/label/__name__/values", params), "names")
    assert set(map(str, names)) == EXPECTED_NAMES
    # Every label is a data point attribute with dots turned into underscores, plus job:
    # no instance, no otel_scope_* and no resource attribute.
    attributes = {
        _label(key)
        for run in delta.runs.values()
        for series in run.expected
        for key, _ in series.labels
    }
    for series in _series(prometheus_url, matcher):
        assert series.get("job") == JOB
        assert set(series) - {"__name__", "job"} <= attributes, series


def test_resource_attributes_stay_on_target_info(prometheus_url: str, delta: Replay) -> None:
    assert delta.runs
    found = _series(prometheus_url, f'target_info{{job="{JOB}"}}')
    assert found, "no target_info series for Claude Code"
    for series in found:
        # service.version and os.type from the fixtures' resource.
        assert {"service_version", "os_type"} <= set(series), series


def test_prometheus_logs_no_errors(
    service_logs: Callable[[str], str], delta: Replay, cumulative: Replay
) -> None:
    assert delta.runs
    assert cumulative.runs
    logs = service_logs(PROMETHEUS)
    assert logs.strip(), "Prometheus logged nothing at all"
    errors = [line for line in logs.splitlines() if re.search(r"level=error", line, re.I)]
    assert not errors, "\n".join(errors[:5])


# Last in this module: they restart the collector, and then check that nothing failed
# since the module started, the restart included.


class UndercountError(AssertionError):
    """Totals lower than expected after a restart where continuing series are contiguous:
    the documented limit of ADR 0013, and the only failure its xfail test accepts."""


@dataclass(frozen=True)
class Restart:
    """A delta run whose metric requests go in two parts, around a collector restart."""

    run: Prepared
    first: tuple[JSONObject, ...]
    rest: tuple[JSONObject, ...]

    @classmethod
    def of(cls, scenario: str, split: int) -> Restart:
        run = prepare(load_scenario(scenario), now_ns=time.time_ns())
        return cls(run, run.metrics[:split], run.metrics[split:])

    def before(self) -> Totals:
        return _keyed(expected_totals(self.first))

    def total(self) -> Totals:
        return _added(expected_totals(self.first), expected_totals(self.rest))

    def continuing(self) -> set[SeriesKey]:
        return set(expected_totals(self.first)) & set(expected_totals(self.rest))

    def contiguous(self) -> set[SeriesKey]:
        return set(contiguous_series(self.first, self.rest))

    def last_values(self) -> Totals:
        """``last_over_time`` once both parts are stored: the restart starts every series
        of ``rest`` again from zero, so its last value is ``rest``'s own total, whether
        or not its zero sample was kept; the other series keep their total."""
        return {**self.before(), **_keyed(expected_totals(self.rest))}


def _send_around_a_restart(
    restart: Restart,
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_health: Callable[[str], str],
    otlp_http_url: str,
    prometheus_url: str,
) -> None:
    """Send ``restart.first``, restart the collector once it is stored, send
    ``restart.rest`` and return once Prometheus holds it."""
    run = restart.run
    _assert_totals(restart.total(), _keyed(run.expected), "the two parts")
    assert restart.continuing(), "no series goes on after the restart"
    for request in restart.first:
        post_otlp(otlp_http_url, "metrics", request)
    # Stored before the restart, so that no request is in the exporter's queue then.
    stored = _poll(
        lambda: _stored(prometheus_url, run, INCREASE),
        lambda found: _matches(found, restart.before()),
        DEADLINE,
    )
    _assert_totals(stored, restart.before(), "before the restart")
    compose("restart", COLLECTOR)
    health = _poll(
        lambda: service_health(COLLECTOR), lambda state: state == "healthy", HEALTHY_DEADLINE
    )
    assert health == "healthy"
    for request in restart.rest:
        post_otlp(otlp_http_url, "metrics", request)
    last = _poll(
        lambda: _stored(prometheus_url, run, LAST),
        lambda found: _matches(found, restart.last_values()),
        DEADLINE,
    )
    _assert_totals(last, restart.last_values(), "last values after the restart")


def test_totals_stay_exact_across_a_collector_restart(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_health: Callable[[str], str],
    otlp_http_url: str,
    prometheus_url: str,
    clock_skew: float,
) -> None:
    # The restart drops the running totals of delta_to_cumulative: each continuing series
    # starts again from zero, a counter reset that increase(... anchored) counts when the
    # new stream starts after the series' last stored sample.
    restart = Restart.of(RESTART_SCENARIO, RESTART_SPLIT)
    contiguous = sorted(restart.contiguous())
    assert not contiguous, (
        f"{RESTART_SCENARIO} split after {RESTART_SPLIT} requests has contiguous continuing "
        f"series {contiguous}: their totals can undercount after the restart (ADR 0013, "
        "Temporality); pick a split where no continuing series is contiguous"
    )
    _send_around_a_restart(restart, compose, service_health, otlp_http_url, prometheus_url)
    stored = _stored(prometheus_url, restart.run, INCREASE)
    _assert_totals(stored, restart.total(), "after the restart")
    # The restart did reset the continuing series.
    resets = _stored(prometheus_url, restart.run, RESETS)
    assert any(value > 0 for value in resets.values()), resets


@pytest.mark.xfail(
    raises=UndercountError,
    strict=True,
    reason="ADR 0013: a contiguous delta after a collector restart loses the old total",
)
def test_totals_after_a_restart_with_contiguous_series(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_health: Callable[[str], str],
    otlp_http_url: str,
    prometheus_url: str,
    clock_skew: float,
) -> None:
    # Records the documented limit. Only UndercountError is an expected failure: anything
    # else (a split that is no longer contiguous, a series counted too high, a stack
    # error) fails for real, and exact totals make the test pass, which strict turns into
    # a failure: then the limit is gone, and ADR 0013 needs updating.
    restart = Restart.of(CONTIGUOUS_RESTART_SCENARIO, CONTIGUOUS_RESTART_SPLIT)
    if restart.contiguous() != restart.continuing():
        pytest.fail(
            f"{CONTIGUOUS_RESTART_SCENARIO} split after {CONTIGUOUS_RESTART_SPLIT} requests "
            "must have only contiguous continuing series (ADR 0013, Temporality); got "
            f"{len(restart.contiguous())} of {len(restart.continuing())}"
        )
    _send_around_a_restart(restart, compose, service_health, otlp_http_url, prometheus_url)
    stored = _stored(prometheus_url, restart.run, INCREASE)
    total = restart.total()
    if stored.keys() != total.keys():
        pytest.fail(f"stored series differ: {sorted(map(_describe, stored.keys() ^ total.keys()))}")
    wrong = {key for key, value in total.items() if not _close(stored[key], value)}
    high = sorted(_describe(key) for key in wrong if stored[key] > total[key])
    if high:
        pytest.fail(f"counted too high, which is not the documented limit: {high}")
    contiguous = {_key(series) for series in restart.contiguous()}
    if not wrong <= contiguous:
        pytest.fail(f"undercounted series that are not contiguous: {sorted(map(_describe, wrong))}")
    if wrong:
        lost = sorted(f"{_describe(key)}: {stored[key]!r} of {total[key]!r}" for key in wrong)
        raise UndercountError(f"{len(wrong)} contiguous series lost their earlier total: {lost}")


def test_prometheus_scrapes_the_collector_and_no_export_failed(prometheus_url: str) -> None:
    # Prometheus's own clock, since sample timestamps come from it.
    started = _scalar(prometheus_url, "vector(time())", ahead=False)
    jobs = (COLLECTOR_JOB, PROMETHEUS_JOB)

    def scraped() -> dict[str, list[float]]:
        return {
            job: [value for _, value in _query(prometheus_url, f'timestamp(up{{job="{job}"}})')]
            for job in jobs
        }

    times = _poll(
        scraped,
        lambda found: all(values and min(values) > started for values in found.values()),
        SCRAPE_DEADLINE,
    )
    assert all(values and min(values) > started for values in times.values()), times
    for job in jobs:
        assert _scalar(prometheus_url, f'up{{job="{job}"}}') == 1, job
    exporter = f'exporter="{PROMETHEUS_EXPORTER}"'
    sent = _query(
        prometheus_url, f"max_over_time(otelcol_exporter_sent_metric_points{{{exporter}}}[1h])"
    )
    assert sent, "the exporter's counters were not scraped"
    assert all(value > 0 for _, value in sent), sent
    # The collector creates the failure counter at its first failure: no series is fine.
    failed = _query(
        prometheus_url,
        f"max_over_time(otelcol_exporter_send_failed_metric_points{{{exporter}}}[1h])",
    )
    assert all(value == 0 for _, value in failed), failed
    # Prometheus is never restarted here, so its counters hold every OTLP request it has
    # answered, one series per status code; a 400 means a refused request (ADR 0013).
    answers = _query(prometheus_url, f'prometheus_http_requests_total{{handler="{OTLP_HANDLER}"}}')
    codes = {labels.get("code", "") for labels, _ in answers}
    assert codes, "Prometheus counted no OTLP request"
    assert all(code.startswith("2") for code in codes), codes
