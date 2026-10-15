"""The Claude Code overview dashboard in the isolated ``ccobs-it`` compose project (ADR
0003, ADR 0013, ADR 0014, ADR 0015, ADR 0017).

Needs Docker: ``uv run pytest -m integration --no-cov``. As in test_stack_grafana.py, the
tests connect to 127.0.0.1 and send ``Host: localhost:<port>`` (enforce_domain). They
check:

- provisioning: the dashboard is in the "Claude Code" folder, marked provisioned, served
  in every version of Grafana's dashboard API without a schema migration or a conversion
  error, and Grafana logs no warning or error about it;
- the home page: Grafana serves the same file as its home dashboard;
- the file is the source of truth: saving or deleting the dashboard is refused, while a
  copy under another uid saves (and is deleted again);
- the queries: after the fixtures are replayed through the collector, every panel's
  targets run through ``/api/ds/query`` over the dashboard's default range, as Grafana's
  frontend sends them (the variables set to "All", the interval it computes, and
  $__interval and $__range left to the backend, which fills them and aligns the range to
  the step), with no error, and every panel shows the replayed data, so the PromQL with
  ``anchored`` and the LogQL are valid and match the stored names and labels; the bars of
  every over-time panel add up to the matching total, at the default range and at a
  short one; the Loki panels answer over 90 days and a year; the variables list the
  replayed host and models; the stats read 0 over an idle range.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest

from ccobs.domain.telemetry import DEFAULT_HOST, EVENTS, TOKEN_USAGE
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import Prepared, load_scenario, prepare, scenario_names, send

pytestmark = pytest.mark.integration

GRAFANA: Final = "grafana"
LOOPBACK: Final = "127.0.0.1"
HTTP_TIMEOUT: Final = 20.0
DASHBOARD_FILE: Final = (
    Path(__file__).resolve().parents[2]
    / "stack"
    / "grafana"
    / "dashboards"
    / "claude-code-overview.json"
)
UID: Final = "claude-code-overview"
FOLDER_UID: Final = "claude-code"
FOLDER_TITLE: Final = "Claude Code"
# Grafana 13.2 serves the file of dashboards.default_home_dashboard_path through its
# dashboard API under this fixed name (DASHBOARD_NAME in
# pkg/registry/apis/dashboard/home/getter.go), and /api/dashboards/home redirects there.
HOME_NAME: Final = "default-home-dashboard"
DASHBOARD_API: Final = "/apis/dashboard.grafana.app/{version}/namespaces/default/dashboards/{name}"
# Every version of Grafana 13.2's dashboard API, each converted from the stored file; the
# classic ones keep its schemaVersion, the last one Grafana 13.2.4 migrates to.
API_VERSIONS: Final = ("v1beta1", "v1", "v2beta1", "v2")
CLASSIC_VERSIONS: Final = ("v1beta1", "v1")
SCHEMA_VERSION: Final = 42
# Test dashboards use this uid prefix, whose access-control lines test_stack_grafana.py
# expects in Grafana's logs (EXPECTED_ERRORS).
COPY_PREFIX: Final = "ccobs-it-"
# An hour 20 days back, before anything the stack stores, for the idle stats. It stays
# inside Loki's retention (30 days): Loki answers a range older than that with no data
# at all (max_query_lookback), so a stat there reads "No data", not 0.
IDLE_OFFSET_S: Final = 20 * 24 * 3600
# What Grafana's frontend sends with a panel's queries: the panel's maxDataPoints and
# the datasource's default line limit for logs.
MAX_DATA_POINTS: Final = 100
MAX_LINES: Final = 1000
# What Grafana puts for a multi-value variable set to "All": its allValue.
ALL: Final = ".*"
# Grafana's Loki and Prometheus frontends leave the interval to the backend: they put
# the variables back for it, so `${__interval_ms}ms` reaches it as `$__interval_msms`,
# which it fills as 86400000ms (applyTemplateVariables in Grafana 13.2.4's plugins).
FRONTEND_VARIABLES: Final = {"${__interval_ms}": "$__interval_ms", "${__interval}": "$__interval"}
# Each over-time window and the total over the same range (the dashboard's form).
OVER_TIME: Final = {
    "prometheus": ("[$__interval] anchored offset -$__interval", "[$__range] anchored"),
    "loki": ("[$__interval] offset -${__interval_ms}ms", "[$__range]"),
}
REL_TOL: Final = 1e-9
# Seconds to wait until every panel shows the replayed data, and between two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 1.0
# Grafana's console log: logger=... t=... level=... msg=...
WARNING_OR_ERROR: Final = re.compile(r"\blevel=(?:warn|error)\b")
# Words in a log line about the dashboard: its provisioner, the home dashboard loader,
# its uid and file, and the dashboard API's conversions.
DASHBOARD_LOG_MARKERS: Final = (
    "logger=provisioning.dashboard",
    "dashboards-apiserver-home",
    UID,
    HOME_NAME,
    "/etc/ccobs/grafana/dashboards",
    "conversion",
)
PROVISIONED_LINE: Final = "finished to provision dashboards"
# The fields the events log adds to each event's name (key=value).
EVENT_LINE_FIELDS: Final = frozenset(
    {"hook_event", "tool_name", "model", "status_code", "cost_usd"}
)
CANNOT_SAVE: Final = "Cannot save provisioned dashboard"
CANNOT_DELETE: Final = "provisioned dashboard cannot be deleted"
# A cost on the events log, printed with 4 decimals.
COST: Final = re.compile(r"\d+\.\d{4}")
NAMED_COLOURS: Final = frozenset({"red", "orange", "yellow", "green", "blue", "purple"})


@dataclass(frozen=True)
class Window:
    """A dashboard range that ends now, with the interval Grafana's frontend computes
    for it: the range over maxDataPoints, rounded by rangeutil's roundInterval and kept
    at least at the panel's minimum interval, 1m (calculateInterval, Grafana 13.2.4)."""

    name: str
    range_s: int
    interval_ms: int


# The dashboard's default range: 864 s rounds to 15m.
DEFAULT_WINDOW: Final = Window("24h", 24 * 3600, 15 * 60_000)
# A short range: 36 s rounds to 30 s, raised to the panel's 1m minimum.
SHORT_WINDOW: Final = Window("1h", 3600, 60_000)
# Longer than Loki's retention and its default max_query_length (721h): 77 760 s rounds
# to 12h, and 315 360 s to 1d, which $__interval prints in days.
LONG_WINDOWS: Final = (
    Window("90d", 90 * 24 * 3600, 12 * 3_600_000),
    Window("365d", 365 * 24 * 3600, 24 * 3_600_000),
)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> JSON:
        document: JSON = json.loads(self.body)
        return document


@dataclass(frozen=True)
class Grafana:
    """Grafana's published port and the Host header of its root URL."""

    port: int
    host: str

    def request(self, method: str, path: str, document: JSON | None = None) -> Response:
        """A request as Grafana's own pages send it: same origin, JSON body."""
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=HTTP_TIMEOUT)
        headers = {"Host": self.host, "Origin": f"http://{self.host}"}
        body = None
        if document is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(document).encode()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return Response(response.status, response.read())
        finally:
            connection.close()

    def get_json(self, path: str) -> JSONObject:
        response = self.request("GET", path)
        assert response.status == 200, f"GET {path}: {response.status} {response.body[:300]!r}"
        return _object(response.json())


@dataclass(frozen=True)
class Target:
    """One query of one panel of the dashboard file."""

    panel: JSONObject
    query: JSONObject

    @property
    def key(self) -> str:
        return (
            f"{self.panel.get('title')} (panel {self.panel.get('id')}, {self.query.get('refId')})"
        )


@dataclass(frozen=True)
class Result:
    """What ``/api/ds/query`` returned for one target: its frames, or its error."""

    target: Target
    frames: tuple[JSONObject, ...]
    error: str | None


# JSON helpers.


def _object(value: JSON) -> JSONObject:
    assert isinstance(value, dict), f"expected a JSON object, got {value!r}"
    return value


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _fields(frame: JSONObject) -> list[tuple[JSONObject, list[JSON]]]:
    """Each field of a data frame with its values."""
    fields = _objects(_object(frame.get("schema", {})).get("fields"))
    values = _object(frame.get("data", {})).get("values", [])
    assert isinstance(values, list), frame.get("schema")
    return [
        (field, list(column))
        for field, column in zip(fields, values, strict=True)
        if isinstance(column, list)
    ]


def _numbers(frame: JSONObject) -> list[float]:
    return [
        float(value)
        for field, column in _fields(frame)
        if field.get("type") == "number"
        for value in column
        if isinstance(value, int | float)
    ]


def _column(frame: JSONObject, name: str) -> list[JSON]:
    return next((column for field, column in _fields(frame) if field.get("name") == name), [])


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


# The dashboard file and its queries.


def _dashboard_file() -> JSONObject:
    return _object(json.loads(DASHBOARD_FILE.read_text(encoding="utf-8")))


def _panels(dashboard: JSONObject) -> list[JSONObject]:
    return [panel for panel in _objects(dashboard.get("panels")) if panel.get("type") != "row"]


def _targets() -> list[Target]:
    return [
        Target(panel, query)
        for panel in _panels(_dashboard_file())
        for query in _objects(panel.get("targets"))
    ]


def _frontend_expr(expr: str) -> str:
    """``expr`` as Grafana's frontend sends it with the variables set to "All": $host
    and $model are filled, $__interval and $__range are left to the backend."""
    filled = expr.replace("$host", ALL).replace("$model", ALL)
    for variable, sent in FRONTEND_VARIABLES.items():
        filled = filled.replace(variable, sent)
    assert "$host" not in filled, filled
    assert "$model" not in filled, filled
    return filled


def _uid(target: Target) -> str:
    return str(_object(target.panel.get("datasource")).get("uid"))


def _run(grafana: Grafana, target: Target, window: Window, end_s: float | None = None) -> Result:
    """``target`` through ``/api/ds/query`` over ``window``, ending now or at ``end_s``."""
    end_ms = int((time.time() if end_s is None else end_s) * 1000)
    start_ms = end_ms - window.range_s * 1000
    query: JSONObject = {
        **target.query,
        "expr": _frontend_expr(str(target.query.get("expr"))),
        "intervalMs": window.interval_ms,
        "maxDataPoints": MAX_DATA_POINTS,
    }
    if target.panel.get("type") == "logs":
        query["maxLines"] = MAX_LINES
    document: JSONObject = {"from": str(start_ms), "to": str(end_ms), "queries": [query]}
    response = grafana.request("POST", "/api/ds/query", document)
    if response.status != 200:
        return Result(target, (), f"HTTP {response.status}: {response.body[:500]!r}")
    result = _object(_object(_object(response.json()).get("results")).get(str(query["refId"])))
    frames = tuple(_objects(result.get("frames")))
    notices = [
        notice
        for frame in frames
        for notice in _objects(
            _object(_object(frame.get("schema", {})).get("meta", {})).get("notices")
        )
        if notice.get("severity") == "error"
    ]
    error = result.get("error")
    if not error and notices:
        error = json.dumps(notices)
    return Result(target, frames, str(error) if error else None)


def _shows_data(results: list[Result]) -> bool:
    """Whether a panel's results hold something to draw: a log line, or a number that is
    not zero (a stat's `or vector(0)` is always there)."""
    for result in results:
        for frame in result.frames:
            if result.target.panel.get("type") == "logs" and _column(frame, "Line"):
                return True
            if any(_numbers(frame)):
                return True
    return False


def _total_target(target: Target) -> Target:
    """The instant query of the total that an over-time ``target`` splits into bars:
    the same expression over the dashboard's range."""
    uid = _uid(target)
    over_time, total = OVER_TIME[uid]
    expr = str(target.query.get("expr"))
    assert over_time in expr, f"{target.key}: {expr}"
    query: JSONObject = {**target.query, "expr": expr.replace(over_time, total)}
    if uid == "prometheus":
        query.update({"instant": True, "range": False})
    else:
        query["queryType"] = "instant"
    return Target(target.panel, query)


def _over_time_targets() -> list[Target]:
    return [target for target in _targets() if target.panel.get("type") == "timeseries"]


def _sum(result: Result) -> float:
    return math.fsum(value for frame in result.frames for value in _numbers(frame))


def _bars_and_totals(grafana: Grafana, window: Window) -> list[tuple[Target, float, float]]:
    """Each over-time target with the sum of its bars and its total over ``window``."""
    found = []
    for target in _over_time_targets():
        end_s = time.time()
        bars, total = (
            _run(grafana, target, window, end_s),
            _run(grafana, _total_target(target), window, end_s),
        )
        assert bars.error is None, f"{target.key}: {bars.error}"
        assert total.error is None, f"{target.key} total: {total.error}"
        found.append((target, _sum(bars), _sum(total)))
    return found


def _add_up(found: list[tuple[Target, float, float]]) -> bool:
    return all(total > 0 and math.isclose(bars, total, rel_tol=REL_TOL) for _, bars, total in found)


def _by_panel(results: list[Result]) -> dict[str, list[Result]]:
    panels: dict[str, list[Result]] = {}
    for result in results:
        panels.setdefault(str(result.target.panel.get("id")), []).append(result)
    return panels


# Fixtures.


@pytest.fixture(scope="module")
def grafana(grafana_url: str) -> Grafana:
    parts = urlsplit(grafana_url)
    assert parts.hostname == "localhost", grafana_url
    assert parts.port, grafana_url
    return Grafana(port=parts.port, host=parts.netloc)


@pytest.fixture(scope="module")
def replayed(otlp_http_url: str) -> tuple[Prepared, ...]:
    """Every scenario's metrics and events, sent to the collector with fresh IDs."""
    now_ns = time.time_ns()
    runs = tuple(
        prepare(load_scenario(name), now_ns=now_ns) for name in scenario_names(with_otlp=True)
    )
    for run in runs:
        send(otlp_http_url, run, signals=("metrics", "logs"))
    return runs


@pytest.fixture(scope="module")
def results(grafana: Grafana, replayed: tuple[Prepared, ...]) -> list[Result]:
    """Every target's result, once every panel shows data or the deadline has passed
    (the tests then report what is missing)."""
    records = sum(
        len(_objects(scope.get("logRecords")))
        for run in replayed
        for request in run.logs
        for resource in _objects(request.get("resourceLogs"))
        for scope in _objects(resource.get("scopeLogs"))
    )
    assert records
    targets = _targets()

    def read() -> list[Result]:
        end_s = time.time()
        return [_run(grafana, target, DEFAULT_WINDOW, end_s) for target in targets]

    def done(found: list[Result]) -> bool:
        # Every panel has data, and the events log has at least the replayed events.
        lines = [
            line
            for result in found
            if result.target.panel.get("type") == "logs"
            for frame in result.frames
            for line in _column(frame, "Line")
        ]
        complete = len(lines) >= min(records, MAX_LINES)
        return complete and all(_shows_data(panel) for panel in _by_panel(found).values())

    return _poll(read, done, DEADLINE)


# Provisioning.


def test_dashboard_is_provisioned_in_its_folder(grafana: Grafana) -> None:
    document = grafana.get_json(f"/api/dashboards/uid/{UID}")
    meta, dashboard = _object(document.get("meta")), _object(document.get("dashboard"))
    assert meta.get("provisioned") is True
    assert meta.get("folderUid") == FOLDER_UID
    assert meta.get("folderTitle") == FOLDER_TITLE
    expected = _dashboard_file()
    assert dashboard.get("title") == expected.get("title")
    assert [p.get("title") for p in _panels(dashboard)] == [
        p.get("title") for p in _panels(expected)
    ]


@pytest.mark.parametrize("version", API_VERSIONS)
@pytest.mark.parametrize("name", [UID, HOME_NAME])
def test_dashboard_is_served_without_migration_or_conversion_error(
    grafana: Grafana, name: str, version: str
) -> None:
    # Grafana stores the file as it is (v0alpha1) and converts it to the version that a
    # client asks for, recording how the conversion went in the status.
    document = grafana.get_json(DASHBOARD_API.format(version=version, name=name))
    conversion = _object(_object(document.get("status")).get("conversion"))
    assert conversion.get("failed") is False, conversion
    assert not conversion.get("error"), conversion
    if version in CLASSIC_VERSIONS:
        spec = _object(document.get("spec"))
        assert spec.get("schemaVersion") == SCHEMA_VERSION
        assert spec.get("title") == _dashboard_file().get("title")


@pytest.mark.parametrize("name", [UID, HOME_NAME])
def test_dashboard_has_one_built_in_annotation_with_a_named_colour(
    grafana: Grafana, name: str
) -> None:
    # Grafana adds its own, with an rgba() colour, only to a dashboard without one.
    document = grafana.get_json(DASHBOARD_API.format(version="v1", name=name))
    annotations = _objects(_object(_object(document.get("spec")).get("annotations")).get("list"))
    built_in = [annotation for annotation in annotations if annotation.get("builtIn") == 1]
    assert len(built_in) == 1, annotations
    assert built_in[0].get("iconColor") in NAMED_COLOURS, built_in


def test_home_page_is_the_overview(grafana: Grafana) -> None:
    slug = _object(grafana.get_json(f"/api/dashboards/uid/{UID}").get("meta")).get("slug")
    assert grafana.get_json("/api/dashboards/home") == {"redirectUri": f"/d/{HOME_NAME}/{slug}"}
    home = grafana.get_json(DASHBOARD_API.format(version="v1", name=HOME_NAME))
    spec = _object(home.get("spec"))
    assert spec.get("uid") == UID
    assert [p.get("title") for p in _panels(spec)] == [
        p.get("title") for p in _panels(_dashboard_file())
    ]


def test_provisioned_dashboard_cannot_be_saved_or_deleted(grafana: Grafana) -> None:
    # The file is the source of truth (allowUiUpdates: false, disableDeletion: true).
    before = grafana.get_json(f"/api/dashboards/uid/{UID}")
    dashboard = {**_object(before.get("dashboard")), "title": "Changed through the API"}
    document: JSONObject = {"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True}
    saved = grafana.request("POST", "/api/dashboards/db", document)
    assert saved.status == 400, saved.body[:300]
    assert _object(saved.json()).get("message") == CANNOT_SAVE
    deleted = grafana.request("DELETE", f"/api/dashboards/uid/{UID}")
    assert deleted.status == 400, deleted.body[:300]
    assert _object(deleted.json()).get("message") == CANNOT_DELETE
    after = grafana.get_json(f"/api/dashboards/uid/{UID}")
    assert after == before


def test_a_copy_under_another_uid_saves(grafana: Grafana) -> None:
    # "Save as" in the UI: the anonymous Editor keeps a copy of their own (ADR 0017).
    uid = f"{COPY_PREFIX}{uuid.uuid4().hex[:12]}"
    copy = {**_dashboard_file(), "uid": uid, "title": f"Claude Code overview ({uid})"}
    created = grafana.request("POST", "/api/dashboards/db", {"dashboard": copy, "overwrite": False})
    try:
        assert created.status == 200, created.body[:300]
        meta = _object(grafana.get_json(f"/api/dashboards/uid/{uid}").get("meta"))
        assert meta.get("provisioned") is False
        assert meta.get("canSave") is True
    finally:
        grafana.request("DELETE", f"/api/dashboards/uid/{uid}")


def test_grafana_logs_nothing_wrong_about_the_dashboard(
    grafana: Grafana,
    results: list[Result],
    service_logs: Callable[[str], str],
    service_started: Callable[[str], bool],
) -> None:
    # After the provisioner, the home dashboard and every query have run.
    assert results
    grafana.get_json("/api/dashboards/home")
    logs = service_logs(GRAFANA)
    if service_started(GRAFANA):
        assert PROVISIONED_LINE in logs, "Grafana's provisioning lines are missing"
    found = [
        line
        for line in logs.splitlines()
        if WARNING_OR_ERROR.search(line) and any(marker in line for marker in DASHBOARD_LOG_MARKERS)
    ]
    assert not found, "\n".join(found[:5])


# Queries.


def test_every_target_runs_without_error(results: list[Result]) -> None:
    assert len(results) == len(_targets())
    errors = [f"{result.target.key}: {result.error}" for result in results if result.error]
    assert not errors, "\n".join(errors)


def test_every_panel_shows_the_replayed_data(results: list[Result]) -> None:
    # Cost, tokens, sessions, active time, lines changed, edit decisions, commits (the
    # headless-edit-commit-tasks scenario commits), API errors (headless-api-error) and
    # the events log all have data; pull requests read 0, since no fixture has one.
    empty = [
        str(panel[0].target.panel.get("title"))
        for panel in _by_panel(results).values()
        if not _shows_data(panel)
    ]
    assert not empty, empty


def test_grouped_queries_return_their_labels(results: list[Result]) -> None:
    # `sum by (...)` keeps the labels it groups by, so they are the stored names.
    grouped = re.compile(r"sum by \((?P<by>[^)]*)\)")
    for result in results:
        match = grouped.match(str(result.target.query.get("expr")))
        if match is None:
            continue
        labels = set(match["by"].split(", "))
        series = [
            set(_object(field.get("labels", {})))
            for frame in result.frames
            for field, _ in _fields(frame)
            if field.get("type") == "number"
        ]
        assert series, result.target.key
        assert all(labels <= found for found in series), f"{result.target.key}: {series}"


def test_events_log_shows_event_names_and_metadata(results: list[Result]) -> None:
    (logs,) = [result for result in results if result.target.panel.get("type") == "logs"]
    lines = [str(line) for frame in logs.frames for line in _column(frame, "Line")]
    assert lines
    shown: set[str] = set()
    for line in lines:
        name, *pairs = line.split(" ")
        assert name in EVENTS, line
        fields = dict(pair.split("=", 1) for pair in pairs)
        assert set(fields) <= EVENT_LINE_FIELDS, line
        if "cost_usd" in fields:
            assert COST.fullmatch(fields["cost_usd"]), line
        shown |= set(fields)
    # The fixtures have a hook's event, a tool, a model, a status code and a cost.
    assert shown == EVENT_LINE_FIELDS


@pytest.mark.parametrize("window", [DEFAULT_WINDOW, SHORT_WINDOW], ids=lambda w: w.name)
def test_bars_add_up_to_the_totals(grafana: Grafana, results: list[Result], window: Window) -> None:
    # Grafana floors both ends of a range query to the step, so each bar must cover the
    # bucket that starts at its point (offset -interval), or the newest bucket, the one
    # with the replayed session, would be missing. anchored makes the buckets exact, so
    # the bars of each over-time panel add up to the matching total.
    assert results
    found = _poll(lambda: _bars_and_totals(grafana, window), _add_up, DEADLINE)
    wrong = [
        f"{target.key}: bars {bars} != total {total}"
        for target, bars, total in found
        if not (total > 0 and math.isclose(bars, total, rel_tol=REL_TOL))
    ]
    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("window", LONG_WINDOWS, ids=lambda w: w.name)
def test_loki_panels_answer_over_long_ranges(
    grafana: Grafana, results: list[Result], window: Window
) -> None:
    # Loki starts such a query at its retention (max_query_lookback) instead of refusing
    # it for max_query_length, and the bars' offset is in milliseconds, which Loki
    # parses where `offset -1d` is refused.
    assert results
    for target in _targets():
        if _uid(target) == "loki":
            result = _run(grafana, target, window)
            assert result.error is None, f"{target.key}: {result.error}"
            assert result.frames, target.key


def test_variables_list_the_replayed_host_and_models(
    grafana: Grafana, replayed: tuple[Prepared, ...], results: list[Result]
) -> None:
    # Grafana's Prometheus datasource runs label_values(<selector>,<label>) as a label
    # values request with the selector as match[]. The results fixture has waited until
    # Prometheus stores the replayed metrics.
    assert results
    expected_models = {
        key.label_dict()["model"]
        for run in replayed
        for key in run.expected
        if key.metric == TOKEN_USAGE.otel_name
    }
    assert expected_models
    variables = {
        str(item.get("name")): _object(item.get("query"))
        for item in _objects(_object(_dashboard_file().get("templating")).get("list"))
    }
    now = time.time()
    found: dict[str, set[str]] = {}
    for name, query in variables.items():
        match = re.fullmatch(
            r"label_values\((?P<match>.+),(?P<label>\w+)\)", str(query.get("query"))
        )
        assert match, query
        params = {
            "match[]": match["match"].replace("$host", ALL),
            "start": f"{now - DEFAULT_WINDOW.range_s:.0f}",
            "end": f"{now:.0f}",
        }
        path = f"/api/datasources/uid/prometheus/resources/api/v1/label/{match['label']}/values"
        document = grafana.get_json(f"{path}?{urlencode(params)}")
        values = document.get("data")
        assert isinstance(values, list), document
        found[name] = {str(value) for value in values}
    assert DEFAULT_HOST in found["host"]
    assert expected_models <= found["model"]


def test_stats_read_zero_over_an_idle_range(grafana: Grafana) -> None:
    # `or vector(0)` gives a range with no data a 0 in the panel's unit, in Prometheus and
    # in Loki, instead of "No data".
    end_s = time.time() - IDLE_OFFSET_S
    for target in _targets():
        if target.panel.get("type") != "stat":
            continue
        result = _run(grafana, target, SHORT_WINDOW, end_s)
        assert result.error is None, f"{target.key}: {result.error}"
        values = [value for frame in result.frames for value in _numbers(frame)]
        assert values == [0.0], f"{target.key}: {values}"
