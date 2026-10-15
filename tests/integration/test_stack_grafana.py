"""Grafana in the isolated ``ccobs-it`` compose project (ADR 0003, ADR 0017).

Needs Docker: ``uv run pytest -m integration --no-cov``. Grafana answers only to the host
name of its root URL, ``localhost`` (enforce_domain), so the tests connect to 127.0.0.1
(ADR 0018) and send ``Host: localhost:<port>``, as a browser at http://localhost:<port>
does. They check:

- datasources: Prometheus, Loki and Tempo (the project runs the traces profile) are
  provisioned read-only and healthy, return a session replayed through the collector
  with fresh IDs, and link Loki events and Tempo traces both ways;
- unused features (public dashboards, Grafana Alerting) are off, the only plugin
  processes are those of the provisioned datasources, and Grafana logs no unexpected
  error;
- access: the anonymous visitor is an Editor who can read, query and save dashboards but
  not administer, and there is no way to log in and no user at all (read from Grafana's
  own database, since /metrics is off);
- browser defences: any other host name (DNS rebinding) gets a redirect to localhost, a
  request from another origin is refused even as a GET, the legacy datasource proxy that
  a simple cross-site GET could reach is closed, and /metrics is not served.

That Grafana, like every other service, reports no usage and checks for no updates (ADR
0006) is checked in test_stack_no_outbound.py.

Every request the tests make to the legacy datasource proxy is a harmless read path, so
the proxy's refusal is proven by its status, never by reaching a store.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import pytest

from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL, METRICS, SERVICE_NAME
from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.replay import Prepared, load_scenario, prepare, scenario_names, send

pytestmark = pytest.mark.integration

GRAFANA: Final = "grafana"
LOOPBACK: Final = "127.0.0.1"
# The provisioned datasources, by UID, with their plugin types (stack/grafana).
DATASOURCE_TYPES: Final = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
# Seconds to wait until the replayed session can be queried, and between two polls.
DEADLINE: Final = 90.0
POLL_INTERVAL: Final = 0.5
HTTP_TIMEOUT: Final = 20.0
# Queries cover the last hour; the replayed session ends now.
WINDOW_MS: Final = 3_600_000
MARGIN_MS: Final = 60_000
QUERY_LIMIT: Final = 5000
# Host names other than localhost: a page using DNS rebinding sends its own; 127.0.0.1
# and [::1] are redirected too. {port} becomes Grafana's port.
OTHER_HOSTS: Final = ("evil.example", "evil.example:{port}", "127.0.0.1:{port}", "[::1]:{port}")
REDIRECTED_PATHS: Final = ("/", "/api/datasources", "/api/frontend/settings")
# Actions the anonymous Editor has, and actions only an administrator has (Grafana's
# role-based access control).
EDITOR_ACTIONS: Final = (
    "datasources:read",
    "datasources:query",
    "datasources:explore",
    "dashboards:create",
    "folders:create",
    "annotations:create",
)
ADMIN_ACTIONS: Final = (
    "datasources:create",
    "datasources:write",
    "datasources:delete",
    "users:read",
    "users:create",
    "org.users:read",
    "org.users:add",
    "serviceaccounts:create",
    "plugins:install",
    "settings:read",
    "server.stats:read",
    "orgs:create",
)
# Administration requests the anonymous Editor must not be able to make, with the
# answers that refuse them: 401 or 403, or 404 for a route that does not exist while its
# feature is off (plugin installs, with plugins.plugin_admin_enabled false).
DENIED: Final = (401, 403)
ADMIN_REQUESTS: Final[tuple[tuple[str, str, JSONObject | None, tuple[int, ...]], ...]] = (
    ("GET", "/api/admin/settings", None, DENIED),
    ("GET", "/api/admin/stats", None, DENIED),
    ("GET", "/api/users", None, DENIED),
    ("GET", "/api/org/users", None, DENIED),
    ("GET", "/api/serviceaccounts/search", None, DENIED),
    ("POST", "/api/datasources", {"name": "ccobs-it", "type": "loki"}, DENIED),
    ("PUT", "/api/datasources/uid/prometheus", {"name": "Prometheus"}, DENIED),
    ("DELETE", "/api/datasources/uid/loki", None, DENIED),
    ("POST", "/api/admin/users", {"name": "ccobs-it", "login": "ccobs-it"}, DENIED),
    ("POST", "/api/serviceaccounts", {"name": "ccobs-it", "role": "Admin"}, DENIED),
    ("POST", "/api/orgs", {"name": "ccobs-it"}, DENIED),
    ("POST", "/api/plugins/grafana-clock-panel/install", {}, (*DENIED, 404)),
)
# Cross-site writes: (Content-Type, Origin). text/plain and form posts need no CORS
# preflight, so a page can send them; "null" is the origin of sandboxed frames.
CROSS_SITE: Final = (
    ("text/plain", "http://evil.example"),
    ("application/x-www-form-urlencoded", "http://evil.example:{port}"),
    ("application/json", "http://evil.example"),
    ("text/plain", "null"),
)
# A harmless read path on each store, used to prove the legacy datasource proxy refuses
# the request (the whitelist answers 403 before forwarding, so no store is reached).
PROXY_READ_PATHS: Final = {
    "prometheus": "api/v1/status/buildinfo",
    "loki": "loki/api/v1/status/buildinfo",
    "tempo": "api/status/buildinfo",
}
# A read-only path that the store itself serves but that the resource allowlist of its
# datasource plugin leaves out (Loki's /config, Tempo's /status/config). The plugins of
# Grafana 13.2.4 answer 404 to it; if an update drops the allowlist, the request reaches
# only that read and the test fails, before a simple cross-site GET to the route could
# reach Loki's GET /flush or GET /ingester/shutdown (ADR 0017).
RESOURCE_PATHS_OUTSIDE_ALLOWLIST: Final = {"loki": "config", "tempo": "status/config"}
# Grafana's own process in its container; every other process is a backend plugin.
SERVER_COMMAND: Final = "grafana server"
# A bundled backend plugin as `docker compose top` shows it; the directory is the
# plugin's ID in Grafana 13.2.4.
BUNDLED_PLUGIN: Final = re.compile(
    r"/usr/share/grafana/data/plugins-bundled/(?P<id>[a-z0-9-]+)/gpx_[\w.-]+"
)
# Grafana's copy of its SQLite database: without WAL it keeps a rollback journal, which
# `docker compose cp` does not bring along, so a copy taken mid-commit can be torn.
DATABASE: Final = "/var/lib/grafana/grafana.db"
COPY_ATTEMPTS: Final = 3
# Error-level log lines that the integration tests cause on purpose, each tied to a
# request they make: the access-control denials of the org operations in ADMIN_REQUESTS
# (anonymous.invalid-org), the resolution of the test dashboards' UIDs (here and in
# test_stack_dashboard.py), and the request to the grafana.com proxy in
# test_stack_no_outbound.py, which dials a closed port (it may run first). Every other
# error line is a real fault.
DASHBOARD_UID_PREFIX: Final = "ccobs-it-"
GRAFANA_COM_CLOSED_PORT: Final = "127.0.0.1:9"
EXPECTED_ERRORS: Final = (
    f"could not resolve dashboards:uid:{DASHBOARD_UID_PREFIX}",
    "[anonymous.invalid-org]",
    f'msg="Proxy request failed" err="dial tcp {GRAFANA_COM_CLOSED_PORT}',
    "path=/api/gnet/",
)
# Grafana's console log: logger=... t=... level=... msg=...
ERROR_LINE: Final = re.compile(r"\blevel=error\b")
# The Loki query Grafana 13.2.4's trace view runs for "logs for this span" with
# filterByTraceID (createSpanLink), with ${__tags} and ${__span.traceId} filled in.
TRACE_TO_LOGS: Final = (
    "{{{tags}}} | label_format log_line_contains_trace_id="
    '`{{{{ contains "{trace_id}" __line__  }}}}` | log_line_contains_trace_id="true" '
    'or trace_id="{trace_id}"'
)
PROMETHEUS_NAMES: Final = {metric.otel_name: metric.name for metric in METRICS}


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> JSON:
        document: JSON = json.loads(self.body)
        return document


@dataclass(frozen=True)
class Grafana:
    """Grafana's published port and the Host header of its root URL."""

    port: int
    host: str

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        host: str | None = None,
    ) -> Response:
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=HTTP_TIMEOUT)
        try:
            sent = {"Host": host or self.host, **(headers or {})}
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            found = {key.lower(): value for key, value in response.getheaders()}
            return Response(response.status, found, response.read())
        finally:
            connection.close()

    def send_json(self, method: str, path: str, document: JSON) -> Response:
        """A same-origin JSON request, as Grafana's own pages send it."""
        headers = {"Content-Type": "application/json", "Origin": f"http://{self.host}"}
        return self.request(method, path, json.dumps(document).encode(), headers)

    def get_json(self, path: str) -> JSON:
        response = self.request("GET", path)
        assert response.status == 200, f"GET {path}: {response.status} {response.body[:300]!r}"
        return response.json()

    def query(self, uid: str, **query: JSON) -> list[JSONObject]:
        """The frames of one query through a datasource (``/api/ds/query``)."""
        now = int(time.time() * 1000)
        document: JSONObject = {
            "from": str(now - WINDOW_MS),
            "to": str(now + MARGIN_MS),
            "queries": [{"refId": "A", "datasource": {"uid": uid}, **query}],
        }
        response = self.send_json("POST", "/api/ds/query", document)
        assert response.status == 200, f"{uid} query: {response.status} {response.body[:500]!r}"
        result = _object(_object(_object(response.json()).get("results")).get("A"))
        assert "error" not in result, f"{uid} query: {result.get('error')}"
        return _objects(result.get("frames"))


# JSON helpers.


def _object(value: JSON) -> JSONObject:
    assert isinstance(value, dict), f"expected a JSON object, got {value!r}"
    return value


def _objects(value: JSON) -> list[JSONObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _columns(frame: JSONObject) -> dict[str, list[JSON]]:
    """A data frame's columns by field name."""
    fields = _objects(_object(frame.get("schema")).get("fields"))
    values = _object(frame.get("data")).get("values")
    assert isinstance(values, list), frame.get("schema")
    assert len(values) == len(fields), frame.get("schema")
    columns: dict[str, list[JSON]] = {}
    for field, column in zip(fields, values, strict=True):
        assert isinstance(column, list), field
        columns[str(field.get("name"))] = column
    return columns


def _strings(value: JSON) -> dict[str, str]:
    return {key: str(item) for key, item in _object(value).items()}


def _commands(top: str) -> list[str]:
    """The command of each process in ``docker compose top`` output: the text under the
    CMD heading of its table (Compose prints one table, or one per container)."""
    commands: list[str] = []
    column: int | None = None
    for line in top.splitlines():
        if re.search(r"\bPID\b.*\bCMD$", line.rstrip()):
            column = line.rstrip().rindex("CMD")
        elif column is not None and line[column:].strip():
            commands.append(line[column:].strip())
    return commands


def _plugin_processes(commands: list[str]) -> list[str]:
    """The plugin ID of each process that is not Grafana's server, sorted; a process that
    is not a bundled plugin keeps its whole command, so a failure shows it."""
    found = []
    for command in commands:
        if command.startswith(SERVER_COMMAND):
            continue
        match = BUNDLED_PLUGIN.fullmatch(command)
        found.append(match["id"] if match else command)
    return sorted(found)


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


# The replayed session.


def _records(run: Prepared) -> Iterator[JSONObject]:
    for request in run.logs:
        for resource in _objects(request.get("resourceLogs")):
            for scope in _objects(resource.get("scopeLogs")):
                yield from _objects(scope.get("logRecords"))


def _spans(run: Prepared) -> Iterator[JSONObject]:
    for request in run.traces:
        for resource in _objects(request.get("resourceSpans")):
            for scope in _objects(resource.get("scopeSpans")):
                yield from _objects(scope.get("spans"))


def _metric_names(run: Prepared) -> set[str]:
    """The Prometheus names of the metrics ``run`` sent (ADR 0014)."""
    names: set[str] = set()
    for request in run.metrics:
        for resource in _objects(request.get("resourceMetrics")):
            for scope in _objects(resource.get("scopeMetrics")):
                names.update(
                    PROMETHEUS_NAMES[str(m.get("name"))] for m in _objects(scope.get("metrics"))
                )
    return names


def _trace_ids(run: Prepared) -> set[str]:
    """The trace IDs of the records ``run`` sent that belong to a trace."""
    return {str(record["traceId"]) for record in _records(run) if record.get("traceId")}


def _scenario() -> str:
    """The smallest scenario with metrics, traces and events that belong to a trace."""
    candidates = []
    for name in scenario_names(with_otlp=True):
        scenario = load_scenario(name)
        linked = any(
            record.get("traceId")
            for request in scenario.logs
            for resource in _objects(request.get("resourceLogs"))
            for scope in _objects(resource.get("scopeLogs"))
            for record in _objects(scope.get("logRecords"))
        )
        if scenario.metrics and scenario.traces and linked:
            size = len(scenario.metrics) + len(scenario.logs) + len(scenario.traces)
            candidates.append((size, name))
    assert candidates, "no fixture scenario has metrics, traces and linked events"
    return min(candidates)[1]


def _loki_entries(grafana: Grafana, logql: str) -> list[dict[str, str]]:
    """The labels of every entry ``logql`` selects, through Grafana's Loki datasource;
    structured metadata comes back among them."""
    frames = grafana.query("loki", expr=logql, queryType="range", maxLines=QUERY_LIMIT)
    entries = [_strings(labels) for frame in frames for labels in _columns(frame)["labels"]]
    assert len(entries) < QUERY_LIMIT, f"{logql} hit the query limit"
    return entries


def _session_query(session_id: str) -> str:
    return f'{{service_name="{SERVICE_NAME}"}} | session_id="{session_id}"'


def _trace(grafana: Grafana, trace_id: str) -> dict[str, list[JSON]]:
    """The spans of one trace, through Grafana's Tempo datasource."""
    frames = grafana.query("tempo", queryType="traceId", query=trace_id)
    traces = [_columns(frame) for frame in frames if frame.get("schema")]
    assert len(traces) == 1, f"trace {trace_id}: {len(traces)} frames"
    return traces[0]


def _json_data(grafana: Grafana, uid: str) -> JSONObject:
    return _object(_object(grafana.get_json(f"/api/datasources/uid/{uid}")).get("jsonData"))


def _frontend_settings(grafana: Grafana) -> JSONObject:
    return _object(grafana.get_json("/api/frontend/settings"))


def _status(base: str, path: str) -> int:
    """The status of ``GET {base}{path}`` on a store's debug port."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


# Fixtures.


@pytest.fixture(scope="module")
def grafana(grafana_url: str) -> Grafana:
    parts = urlsplit(grafana_url)
    assert parts.hostname == "localhost", grafana_url
    assert parts.port, grafana_url
    return Grafana(port=parts.port, host=parts.netloc)


@pytest.fixture(scope="module")
def run(otlp_http_url: str) -> Prepared:
    """One scenario, every signal, sent to the collector with fresh IDs."""
    prepared = prepare(load_scenario(_scenario()), now_ns=time.time_ns())
    send(otlp_http_url, prepared)
    return prepared


@pytest.fixture(scope="module")
def loki_entries(grafana: Grafana, run: Prepared) -> list[dict[str, str]]:
    """The replayed session's events through Grafana, once all are there or the
    deadline has passed (the tests then report the difference)."""
    expected = sum(1 for _ in _records(run))
    return _poll(
        lambda: _loki_entries(grafana, _session_query(run.session_id)),
        lambda entries: len(entries) == expected,
        DEADLINE,
    )


# Health.


def test_grafana_is_healthy(service_health: Callable[[str], str]) -> None:
    assert service_health(GRAFANA) == "healthy"


def test_api_health_reports_the_database_ok(grafana: Grafana) -> None:
    assert _object(grafana.get_json("/api/health")).get("database") == "ok"


def test_healthcheck_reaches_api_health_without_a_redirect(
    compose: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    # The healthcheck uses curl -f, which accepts a redirect; /api/health must answer
    # 200 to 127.0.0.1 itself, outside enforce_domain.
    result = compose(
        "exec",
        "-T",
        GRAFANA,
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "http://127.0.0.1:3000/api/health",
    )
    assert result.stdout.strip() == "200"


# Datasources.


def test_datasources_are_provisioned_read_only(grafana: Grafana) -> None:
    datasources = [_object(item) for item in _objects(grafana.get_json("/api/datasources"))]
    assert {str(item.get("uid")): item.get("type") for item in datasources} == DATASOURCE_TYPES
    for item in datasources:
        assert item.get("readOnly") is True, item.get("uid")
        assert item.get("access") == "proxy", item.get("uid")
        assert item.get("basicAuth") is False, item.get("uid")


@pytest.mark.parametrize("uid", sorted(DATASOURCE_TYPES))
def test_datasource_is_healthy(grafana: Grafana, uid: str) -> None:
    health = _object(grafana.get_json(f"/api/datasources/uid/{uid}/health"))
    assert health.get("status") == "OK", health


def test_prometheus_returns_the_replayed_session(grafana: Grafana, run: Prepared) -> None:
    expected = _metric_names(run)

    def read() -> list[dict[str, str]]:
        frames = grafana.query(
            "prometheus", expr=f'{{session_id="{run.session_id}"}}', instant=True, range=False
        )
        fields = [_objects(_object(frame.get("schema")).get("fields")) for frame in frames]
        return [
            _strings(field["labels"]) for group in fields for field in group if "labels" in field
        ]

    series = _poll(read, lambda found: {s["__name__"] for s in found} == expected, DEADLINE)
    assert {labels["__name__"] for labels in series} == expected
    for labels in series:
        assert labels.get("session_id") == run.session_id
        assert labels.get(HOST_LABEL) == DEFAULT_HOST


def test_loki_returns_the_replayed_session(
    run: Prepared, loki_entries: list[dict[str, str]]
) -> None:
    assert len(loki_entries) == sum(1 for _ in _records(run))
    assert {entry.get("session_id") for entry in loki_entries} == {run.session_id}
    stored = {entry["trace_id"] for entry in loki_entries if "trace_id" in entry}
    assert stored == _trace_ids(run)


def test_loki_events_link_to_their_trace_in_tempo(
    grafana: Grafana, run: Prepared, loki_entries: list[dict[str, str]]
) -> None:
    # The derived field, as Grafana's Loki datasource applies it to each entry: the
    # first label that matches its regex holds the trace ID, and the link queries that
    # ID in the field's datasource.
    fields = _objects(_json_data(grafana, "loki").get("derivedFields"))
    assert len(fields) == 1
    field = fields[0]
    assert field.get("datasourceUid") == "tempo"
    # Grafana has unescaped the $$ of the provisioned file into a single $.
    assert field.get("matcherRegex") == "^trace_id$"
    assert field.get("url") == "${__value.raw}"
    pattern = re.compile(str(field.get("matcherRegex")))
    linked = set()
    for entry in loki_entries:
        key = next((label for label in entry if pattern.search(label)), None)
        if key is not None:
            linked.add(entry[key])
    assert linked == _trace_ids(run)
    sent: dict[str, set[str]] = {}
    for span in _spans(run):
        sent.setdefault(str(span.get("traceId")), set()).add(str(span.get("spanId")))
    for trace_id in sorted(linked):
        trace = _trace(grafana, trace_id)
        assert set(map(str, trace["traceID"])) == {trace_id}
        assert set(map(str, trace["spanID"])) == sent[trace_id]


def test_tempo_spans_link_to_their_events_in_loki(grafana: Grafana, run: Prepared) -> None:
    # Grafana's trace view builds the stream selector from the span's resource
    # attributes named in tracesToLogsV2.tags and filters on the trace ID.
    links = _object(_json_data(grafana, "tempo").get("tracesToLogsV2"))
    assert links.get("datasourceUid") == "loki"
    assert links.get("filterByTraceID") is True
    tags = [_strings(tag) for tag in _objects(links.get("tags"))]
    assert tags
    for trace_id in sorted(_trace_ids(run)):
        trace = _trace(grafana, trace_id)
        # Every span of a Claude Code trace has the same resource; serviceTags holds it.
        resource = {
            str(tag.get("key")): str(tag.get("value")) for tag in _objects(trace["serviceTags"][0])
        }
        selector = ", ".join(
            f'{tag.get("value") or tag["key"]}="{resource[tag["key"]]}"' for tag in tags
        )
        logql = TRACE_TO_LOGS.format(tags=selector, trace_id=trace_id)
        entries = _loki_entries(grafana, logql)
        expected = sum(1 for record in _records(run) if record.get("traceId") == trace_id)
        assert len(entries) == expected, logql
        assert {entry.get("session_id") for entry in entries} == {run.session_id}


# Unused features and logs.


def test_unused_features_are_off(grafana: Grafana) -> None:
    # Public dashboards would share a dashboard with anyone who has its link; alerts are
    # the app's job (ADR 0007).
    settings = _frontend_settings(grafana)
    assert settings.get("publicDashboardsEnabled") is False
    assert settings.get("unifiedAlertingEnabled") is False


def test_grafana_logs_no_errors(
    service_logs: Callable[[str], str], loki_entries: list[dict[str, str]]
) -> None:
    # Independent of test order: Grafana logs at error level every access-control denial
    # and every grafana.com proxy failure, so the integration tests that make such
    # requests on purpose leave expected error lines, each tied to one of their paths or
    # targets (EXPECTED_ERRORS). Every other error line is a real fault.
    assert loki_entries
    logs = service_logs(GRAFANA)
    assert logs.strip(), "Grafana logged nothing at all"
    errors = [
        line
        for line in logs.splitlines()
        if ERROR_LINE.search(line) and not any(expected in line for expected in EXPECTED_ERRORS)
    ]
    assert not errors, "\n".join(errors[:5])


# Access (ADR 0017).


def test_anonymous_visitor_is_an_editor_of_the_one_organization(grafana: Grafana) -> None:
    user = _object(_object(grafana.get_json("/bootdata")).get("user"))
    assert user.get("isSignedIn") is False
    assert user.get("orgRole") == "Editor"
    assert user.get("isGrafanaAdmin") is False
    assert user.get("orgCount") == 1


def test_anonymous_visitor_can_read_and_query_but_not_administer(grafana: Grafana) -> None:
    actions = _object(grafana.get_json("/api/access-control/user/actions"))
    missing = [action for action in EDITOR_ACTIONS if actions.get(action) is not True]
    assert not missing, missing
    granted = [action for action in ADMIN_ACTIONS if actions.get(action)]
    assert not granted, granted


def test_there_is_no_way_to_log_in(grafana: Grafana) -> None:
    assert _frontend_settings(grafana).get("disableLoginForm") is True
    # Neither the login form's endpoint nor basic auth accepts a password, not even
    # the one of Grafana's default admin.
    default_admin: JSONObject = {"user": "admin", "password": "admin"}
    login = grafana.send_json("POST", "/login", default_admin)
    assert 400 <= login.status < 500, login.body[:300]
    assert "grafana_session" not in login.headers.get("set-cookie", "")
    token = base64.b64encode(b"admin:admin").decode()
    basic = grafana.request("GET", "/api/user", headers={"Authorization": f"Basic {token}"})
    assert basic.status == 401


def _database(
    compose: Callable[..., subprocess.CompletedProcess[str]], directory: Path
) -> sqlite3.Connection:
    """A consistent copy of Grafana's SQLite database. Grafana runs it without WAL (wal =
    false in its defaults), with a rollback journal that the copy leaves behind, so a copy
    taken mid-commit can be torn: try again when the integrity check fails."""
    problem = ""
    for attempt in range(1, COPY_ATTEMPTS + 1):
        copy = directory / f"grafana-{attempt}.db"
        compose("cp", f"{GRAFANA}:{DATABASE}", str(copy))
        connection = sqlite3.connect(copy)
        try:
            problem = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        except sqlite3.DatabaseError as error:
            problem = str(error)
        if problem == "ok":
            return connection
        connection.close()
    pytest.fail(f"no consistent copy of {DATABASE} in {COPY_ATTEMPTS} attempts: {problem}")


def test_grafana_has_no_users_and_no_admin(
    compose: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    # Read Grafana's own database: no admin user is created and none can be, so there is
    # no account to take over.
    connection = _database(compose, tmp_path)
    try:
        users = connection.execute("SELECT COUNT(*) FROM user WHERE is_service_account = 0")
        admins = connection.execute("SELECT COUNT(*) FROM user WHERE is_admin = 1")
        orgs = connection.execute("SELECT COUNT(*) FROM org")
        assert users.fetchone()[0] == 0
        assert admins.fetchone()[0] == 0
        assert orgs.fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(("method", "path", "body", "refusals"), ADMIN_REQUESTS)
def test_administration_is_refused(
    grafana: Grafana,
    method: str,
    path: str,
    body: JSONObject | None,
    refusals: tuple[int, ...],
) -> None:
    if body is None:
        origin = {"Origin": f"http://{grafana.host}"}
        response = grafana.request(method, path, headers=origin)
    else:
        response = grafana.send_json(method, path, body)
    assert response.status in refusals, f"{method} {path}: {response.status}"
    datasources = _objects(grafana.get_json("/api/datasources"))
    assert {str(item.get("uid")) for item in datasources} == set(DATASOURCE_TYPES)


# Browser defences (ADR 0017).


@pytest.mark.parametrize("path", REDIRECTED_PATHS)
@pytest.mark.parametrize("host", OTHER_HOSTS)
def test_other_host_names_are_redirected_to_localhost(
    grafana: Grafana, host: str, path: str
) -> None:
    # A page using DNS rebinding sends its own host name: it reads only a redirect.
    response = grafana.request("GET", path, host=host.format(port=grafana.port))
    assert response.status == 301
    assert response.headers.get("location") == f"http://{grafana.host}{path}"
    assert b"prometheus" not in response.body.lower()


def test_bootdata_for_another_host_name_holds_no_data(grafana: Grafana) -> None:
    # The page shell's data request answers another host name with only where to go.
    response = grafana.request("GET", "/bootdata", host="evil.example")
    assert response.status == 204
    assert response.headers.get("redirect-domain") == "localhost"
    assert response.body == b""


def test_cross_site_get_is_refused(grafana: Grafana) -> None:
    # csrf_always_check covers GET too (only HEAD, OPTIONS and TRACE are exempt), so a
    # read whose Origin names another host is refused. A simple GET (an <img>) sends no
    # Origin and passes; the proxy whitelist and /metrics being off close what that
    # reaches.
    response = grafana.request("GET", "/api/datasources", headers={"Origin": "http://evil.example"})
    assert response.status == 403, response.body[:200]


@pytest.mark.parametrize("uid", sorted(PROXY_READ_PATHS))
def test_legacy_datasource_proxy_is_closed(
    grafana: Grafana, uid: str, service_health: Callable[[str], str]
) -> None:
    # A simple cross-site GET to this proxy (no Origin, so csrf lets it through) would
    # otherwise reach the store; Loki serves GET /flush and GET /ingester/shutdown. The
    # whitelist answers 403 before forwarding, so the harmless read path below never
    # reaches the store, and Loki stays healthy.
    response = grafana.request("GET", f"/api/datasources/proxy/uid/{uid}/{PROXY_READ_PATHS[uid]}")
    assert response.status == 403, f"{uid}: {response.status} {response.body[:200]!r}"
    assert "whitelist" in response.body.decode(errors="replace")
    assert service_health("loki") == "healthy"


@pytest.mark.parametrize("uid", sorted(RESOURCE_PATHS_OUTSIDE_ALLOWLIST))
def test_resource_route_forwards_only_its_plugins_allowlist(
    grafana: Grafana, uid: str, loki_url: str, tempo_url: str
) -> None:
    # /api/datasources/uid/<uid>/resources/* answers a simple cross-site GET and the
    # proxy whitelist does not cover it; what keeps it from Loki's GET /flush and GET
    # /ingester/shutdown is the resource allowlist of the Loki and Tempo plugins, which no
    # longer live in grafana/grafana. The path below is a read the store serves itself,
    # so a 404 from Grafana means the plugin did not forward it.
    path = RESOURCE_PATHS_OUTSIDE_ALLOWLIST[uid]
    store = {"loki": loki_url, "tempo": tempo_url}[uid]
    assert _status(store, f"/{path}") == 200, f"{uid} itself no longer serves /{path}"
    response = grafana.request("GET", f"/api/datasources/uid/{uid}/resources/{path}")
    assert response.status == 404, f"{uid}: {response.status} {response.body[:200]!r}"


def test_only_the_provisioned_datasources_run_a_plugin_process(
    compose: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    # Grafana 13.2 runs each bundled backend datasource plugin as a process of its own,
    # outside GOMEMLIMIT; compose.yaml disables those that no provisioned datasource uses
    # (plugins.disable_plugins). Every process but the server must be the plugin of a
    # provisioned datasource, each once, so a Grafana update that bundles a new backend
    # plugin fails this test until compose.yaml disables it too. Grafana may still be
    # starting its plugins when it first reports healthy, hence the polling.
    expected = sorted(set(DATASOURCE_TYPES.values()))
    top = _poll(
        lambda: compose("top", GRAFANA).stdout,
        lambda output: _plugin_processes(_commands(output)) == expected,
        DEADLINE,
    )
    # The raw `docker compose top` output in each message, in case parsing it failed.
    commands = _commands(top)
    assert sum(command.startswith(SERVER_COMMAND) for command in commands) == 1, top
    assert _plugin_processes(commands) == expected, top


def test_no_app_plugin_is_installed(grafana: Grafana) -> None:
    # /api/plugins/<id>/resources/* and /api/plugin-proxy/<id>/* answer a simple GET and
    # the proxy whitelist does not cover them; with no app plugin installed they reach
    # nothing. The control: the same listing does list the core datasource plugins that
    # run inside Grafana's server (Graphite, CloudWatch, ...), which plugins.disable_plugins
    # leaves alone; Grafana 13.2 leaves the bundled ones (Prometheus, Loki, Tempo and the
    # disabled ones) out of it. And the frontend settings, which hold every enabled app
    # plugin, hold none.
    assert grafana.get_json("/api/plugins?type=app") == []
    assert _objects(grafana.get_json("/api/plugins?type=datasource&enabled=1"))
    assert _object(grafana.get_json("/api/frontend/settings")).get("apps") == {}


def test_metrics_endpoint_is_not_served(grafana: Grafana) -> None:
    # /metrics is Grafana's own metrics, the widest route outside the Host check; with it
    # off, the path answers like any unknown one (the app shell, 404), not the metrics.
    response = grafana.request("GET", "/metrics")
    assert response.status == 404, response.body[:200]
    assert b"grafana_stat_" not in response.body
    assert b"go_gc_" not in response.body


@pytest.mark.parametrize(("content_type", "origin"), CROSS_SITE)
def test_cross_site_writes_are_refused(grafana: Grafana, content_type: str, origin: str) -> None:
    uid = f"ccobs-it-{uuid.uuid4().hex[:12]}"
    document = {"dashboard": {"uid": uid, "title": uid}, "overwrite": False}
    headers = {"Content-Type": content_type, "Origin": origin.format(port=grafana.port)}
    body = json.dumps(document).encode()
    response = grafana.request("POST", "/api/dashboards/db", body, headers)
    assert response.status == 403, response.body[:300]
    assert grafana.request("GET", f"/api/dashboards/uid/{uid}").status != 200


def test_same_origin_writes_are_accepted(grafana: Grafana) -> None:
    # The anonymous Editor saves a dashboard from Grafana's own page, so the refusals
    # above come from the origin, not from the role.
    uid = f"ccobs-it-{uuid.uuid4().hex[:12]}"
    document: JSONObject = {"dashboard": {"uid": uid, "title": uid}, "overwrite": False}
    created = grafana.send_json("POST", "/api/dashboards/db", document)
    try:
        assert created.status == 200, created.body[:300]
        meta = _object(_object(grafana.get_json(f"/api/dashboards/uid/{uid}")).get("meta"))
        assert meta.get("canSave") is True
    finally:
        origin = {"Origin": f"http://{grafana.host}"}
        grafana.request("DELETE", f"/api/dashboards/uid/{uid}", headers=origin)
