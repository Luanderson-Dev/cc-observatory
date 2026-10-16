"""No outbound traffic (ADR 0006) in the isolated ``ccobs-it`` compose project: nothing in
the stack reports usage or checks for updates.

Needs Docker: ``uv run pytest -m integration --no-cov``. This module is the one place
that states the rule for every service, each read from the running service:

- Loki and Tempo: their usage reports, to stats.grafana.org, are off in the
  configuration they run with (``/config``, ``/status/config``).
- Grafana: it applied every setting compose.yaml gives it, so each outbound one is in
  effect; the settings its pages read are off, and it knows of no newer version (no
  update check ran); the background jobs that call grafana.com never ran; avatars are
  not fetched from Gravatar; and its grafana.com proxy reaches a closed port on its own
  loopback. Grafana answers only to ``localhost`` (ADR 0017), so the requests go to
  127.0.0.1 with ``Host: localhost:<port>``, as in test_stack_grafana.py.
- Prometheus: no remote write or read, no Alertmanager and no service discovery; it
  scrapes only itself and services of the stack.
- The collector: its only exporters are the stack's three backends.
- The app: it calls only Loki (ADR 0029). A snapshot of its sockets, read from
  ``/proc/net`` inside its container (after the app tests when the whole suite runs,
  since test_app_*.py sort first), must listen only on its two ports, and every other
  socket must be an answer on those ports, a loopback one (the healthcheck, Docker's
  DNS) or a connection to Loki's port on one of Loki's addresses. A snapshot only sees
  the sockets open at that moment, and may hold no connection to Loki at all, so it
  does not require one.
- Networks: the backend network is internal, so Docker drops its traffic to any other
  network. That Prometheus, Loki and Tempo join only backend is checked on compose.yaml
  by tests/test_compose.py; this project also runs compose.debug.yaml, which attaches
  them to edge to publish their ports.

Prometheus and the OpenTelemetry Collector have no usage reporting and no update check
at all, so they have no such setting to read. That no connection leaves the stack,
whatever the settings say, is the manual check described in
tools/stack/compose.egress-sink.yaml.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest
import yaml

from tools.capture.jsontypes import JSON, JSONObject
from tools.stack.isolated import PROJECT

pytestmark = pytest.mark.integration

GRAFANA: Final = "grafana"
LOOPBACK: Final = "127.0.0.1"
COMPOSE_FILE: Final = Path(__file__).resolve().parents[2] / "compose.yaml"
HTTP_TIMEOUT: Final = 20.0
POLL_INTERVAL: Final = 0.5
# Seconds to wait for a line in Grafana's logs, and for Prometheus to scrape the
# collector (every 15 s) after a start.
LOG_DEADLINE: Final = 15.0
SCRAPE_DEADLINE: Final = 60.0
# Grafana logs each environment variable it applies, at every start (setting.go in
# Grafana 13.2.4): msg="Config overridden from Environment variable" var="GF_...=value".
APPLIED_OVERRIDE: Final = re.compile(
    r'msg="Config overridden from Environment variable" '
    r'var="(?P<name>GF_[A-Z0-9_]+)=(?P<value>[^"]*)"'
)
# Those lines mask the value of a variable whose name matches one of these patterns
# (RedactedValue in Grafana 13.2.4's setting.go), and the password of a URL; compose.yaml
# gives Grafana neither (tests/test_grafana_config.py).
MASKED_NAMES: Final = re.compile(
    "|".join(
        (
            "PASSWORD",
            "SECRET",
            "PROVIDER_CONFIG",
            "PRIVATE_KEY",
            "SECRET_KEY",
            "CERTIFICATE",
            "ACCOUNT_KEY",
            "ENCRYPTION_KEY",
            "VAULT_TOKEN",
            "CLIENT_SECRET",
            "ENTERPRISE_LICENSE",
            "API_DB_PASS",
            "^TOKEN$",
            "ID_FORWARDING_TOKEN$",
            "AUTHENTICATION_TOKEN$",
            "AUTH_TOKEN$",
            "RENDERER_TOKEN$",
            "API_TOKEN$",
            "WEBHOOK_TOKEN$",
            "INSTALL_TOKEN$",
            "PROXY_TOKEN$",
        )
    )
)
MASKED_VALUE: Final = "*********"
# Grafana's console log: logger=... t=... level=... msg=...
ERROR_LINE: Final = re.compile(r"\blevel=error\b")
# What Grafana's pages read that would send the browser or Grafana to grafana.com: the
# feedback links, the news feed, the plugin catalog and its updates, Cloud migration.
OUTBOUND_FRONTEND_SETTINGS: Final = (
    "feedbackLinksEnabled",
    "newsFeedEnabled",
    "pluginAdminEnabled",
    "pluginCatalogPreinstalledAutoUpdate",
    "cloudMigrationEnabled",
)
# Browser-side trackers, off by default: Google Analytics, Rudderstack, PostHog and
# Application Insights (Faro, Grafana's JavaScript agent, has a setting of its own).
BROWSER_TRACKERS: Final = (
    "googleAnalyticsId",
    "googleAnalytics4Id",
    "rudderstackWriteKey",
    "postHogToken",
    "applicationInsightsConnectionString",
)
# Background jobs that call grafana.com log these lines when they run, even when the call
# succeeds (Grafana 13.2.4).
OUTBOUND_JOB_LINES: Final = (
    "Installing plugin",
    "Patterns update finished",
    "grafana.update.checker",
    "plugins.update.checker",
)
# What Grafana logs when asked for an avatar while Gravatar is off.
NO_GRAVATAR: Final = "called despite gravatars being disabled"
# Seconds within which the grafana.com proxy fails: its API URL is a closed port.
GRAFANA_COM_DEADLINE: Final = 5.0
# Sections that would send data out of Prometheus (its own traces, over OTLP, included)
# or read it from elsewhere.
PROMETHEUS_OUTBOUND_SECTIONS: Final = ("remote_write", "remote_read", "alerting", "tracing")
# Prometheus scrapes its own address and services of the stack, never another host.
PROMETHEUS_SELF: Final = "localhost"
# The collector's own metrics, as Prometheus scrapes them, and its exporters: one per
# backend, each pinned to its service on the backend network by
# tests/test_collector_config.py.
COLLECTOR_JOB: Final = "otel-collector"
BACKEND_EXPORTERS: Final = frozenset({"otlp_http/loki", "otlp_http/prometheus", "otlp_http/tempo"})
# Services only on the backend network in compose.yaml (ADR 0018).
STORAGE_SERVICES: Final = frozenset({"prometheus", "loki", "tempo"})
# A container Compose names <project>-<service>-<number>.
CONTAINER_NAME: Final = re.compile(rf"^{re.escape(PROJECT)}-(?P<service>.+)-\d+$")
# The app: its own ports inside the container (ADRs 0027 and 0028), Loki's, and the
# script that prints its sockets as JSON lists of [table, local, remote, state], the
# fields of /proc/net/{tcp,tcp6,udp,udp6}.
APP: Final = "app"
APP_PORTS: Final = frozenset({4300, 9464})
LOKI_SERVICE: Final = "loki"
LOKI_PORT: Final = 3100
LISTEN: Final = "0A"
SOCKETS_SCRIPT: Final = (
    "import json\n"
    "rows = []\n"
    "for table in ('tcp', 'tcp6', 'udp', 'udp6'):\n"
    "    with open('/proc/net/' + table) as file:\n"
    "        for line in file.readlines()[1:]:\n"
    "            fields = line.split()\n"
    "            rows.append([table, fields[1], fields[2], fields[3]])\n"
    "print(json.dumps(rows))\n"
)


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

    def request(self, method: str, path: str) -> Response:
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=HTTP_TIMEOUT)
        try:
            connection.request(method, path, headers={"Host": self.host})
            response = connection.getresponse()
            found = {key.lower(): value for key, value in response.getheaders()}
            return Response(response.status, found, response.read())
        finally:
            connection.close()

    def get_json(self, path: str) -> JSON:
        response = self.request("GET", path)
        assert response.status == 200, f"GET {path}: {response.status} {response.body[:300]!r}"
        return response.json()


# Helpers.


def _object(value: JSON) -> JSONObject:
    assert isinstance(value, dict), f"expected a JSON object, got {value!r}"
    return value


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _at(node: object, *path: str) -> object:
    for key in path:
        node = _mapping(node, ".".join(path)).get(key)
    return node


def _poll[T](read: Callable[[], T], done: Callable[[T], bool], seconds: float) -> T:
    """Call ``read`` until ``done`` accepts its result or ``seconds`` have passed; return
    the last result, for the caller to assert on."""
    deadline = time.monotonic() + seconds
    while True:
        result = read()
        if done(result) or time.monotonic() > deadline:
            return result
        time.sleep(POLL_INTERVAL)


def _get(base: str, path: str, params: Mapping[str, str] | None = None) -> bytes:
    """The body of a 200 answer to ``GET {base}{path}?{params}``."""
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=HTTP_TIMEOUT)
    try:
        target = f"{path}?{urlencode(params)}" if params else path
        connection.request("GET", target)
        response = connection.getresponse()
        status, body = response.status, response.read()
    finally:
        connection.close()
    assert status == 200, f"GET {path} answered {status}: {body[:300]!r}"
    return body


def _yaml_config(base: str, path: str) -> dict[str, object]:
    """A store's running configuration: one YAML mapping (Tempo writes a title line
    before it)."""
    body = _get(base, path)
    documents = [item for item in yaml.safe_load_all(body) if isinstance(item, dict)]
    assert len(documents) == 1, f"{path}: {body[:300]!r}"
    return _mapping(documents[0], path)


def _prometheus_data(base: str, path: str, params: Mapping[str, str] | None = None) -> object:
    """The ``data`` field of a successful Prometheus API answer."""
    document = _mapping(json.loads(_get(base, path, params)), path)
    assert document.get("status") == "success", document
    return document.get("data")


def _stack_services() -> set[str]:
    document = _mapping(yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8")), "compose")
    return set(_mapping(document.get("services"), "services"))


def _exporters(prometheus_url: str) -> set[str]:
    """The exporters that the collector's own metrics name, as Prometheus last scraped
    them."""
    query = f'group by (exporter) ({{job="{COLLECTOR_JOB}", exporter!=""}})'
    data = _mapping(_prometheus_data(prometheus_url, "/api/v1/query", {"query": query}), "data")
    series = [_mapping(item, "series") for item in _list(data.get("result"), "result")]
    return {str(_mapping(item.get("metric"), "metric").get("exporter")) for item in series}


# Fixtures.


@pytest.fixture(scope="module")
def grafana(grafana_url: str) -> Grafana:
    parts = urlsplit(grafana_url)
    assert parts.hostname == "localhost", grafana_url
    assert parts.port, grafana_url
    return Grafana(port=parts.port, host=parts.netloc)


# Loki and Tempo.


def test_loki_sends_no_usage_reports(loki_url: str) -> None:
    config = _yaml_config(loki_url, "/config")
    assert _at(config, "analytics", "reporting_enabled") is False


def test_tempo_sends_no_usage_reports(tempo_url: str) -> None:
    config = _yaml_config(tempo_url, "/status/config")
    assert _at(config, "usage_report", "reporting_enabled") is False


# Grafana.


def test_grafana_applied_every_setting_compose_gives_it(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    service_logs: Callable[[str], str],
    service_started: Callable[[str], bool],
) -> None:
    # Grafana logs each environment variable it applies at start. The outbound settings
    # are among them: compose.yaml explains each one, and tests/test_grafana_config.py
    # pins their values. A variable that never reached Grafana, or whose section Grafana
    # no longer has, is missing. Grafana 13.2 also applies (and logs) a key it does not
    # know in a section it has, so the tests below check the effects that can be seen.
    if not service_started(GRAFANA):
        pytest.skip("Grafana was reused from an earlier session (CCOBS_IT_KEEP=1)")
    config = _mapping(json.loads(compose("config", "--format", "json").stdout), "config")
    service = _mapping(_at(config, "services", GRAFANA), GRAFANA)
    environment = _mapping(service.get("environment"), "environment")
    expected = {
        name: MASKED_VALUE if MASKED_NAMES.search(name) else str(value)
        for name, value in environment.items()
        if name.startswith("GF_")
    }
    assert expected
    logs = service_logs(GRAFANA)
    applied = {match["name"]: match["value"] for match in APPLIED_OVERRIDE.finditer(logs)}
    wrong = {name: applied.get(name) for name in expected if applied.get(name) != expected[name]}
    assert not wrong, f"not applied as compose.yaml sets them: {wrong}"


def test_settings_that_reach_the_internet_are_off(grafana: Grafana) -> None:
    settings = _object(grafana.get_json("/api/frontend/settings"))
    for key in OUTBOUND_FRONTEND_SETTINGS:
        assert settings.get(key) is False, key
    # No update check ran, so Grafana knows of no newer version.
    build = _object(settings.get("buildInfo"))
    assert build.get("latestVersion") == ""
    assert build.get("hasUpdate") is False
    for key in BROWSER_TRACKERS:
        # Present, so that a renamed key fails here instead of passing as empty.
        assert key in settings, key
        assert not settings[key], key
    assert _object(settings.get("grafanaJavascriptAgent")).get("enabled") is False


def test_background_jobs_that_call_grafana_com_never_ran(
    service_logs: Callable[[str], str], service_started: Callable[[str], bool]
) -> None:
    if not service_started(GRAFANA):
        pytest.skip("Grafana was reused from an earlier session (CCOBS_IT_KEEP=1)")
    logs = service_logs(GRAFANA)
    assert "HTTP Server Listen" in logs, "Grafana's start-up lines are missing"
    found = [line for line in OUTBOUND_JOB_LINES if line in logs]
    assert not found, found


def test_avatars_are_not_fetched_from_gravatar(
    grafana: Grafana, service_logs: Callable[[str], str]
) -> None:
    # The visitor's avatar is Grafana's own image, and an avatar request is answered
    # with it instead of a fetch from gravatar.com.
    user = _object(_object(grafana.get_json("/bootdata")).get("user"))
    assert str(user.get("gravatarUrl")).startswith("/public/")
    response = grafana.request("GET", f"/avatar/{uuid.uuid4().hex}")
    assert response.status == 200
    assert response.headers.get("content-type") == "image/jpeg"
    logs = _poll(lambda: service_logs(GRAFANA), lambda text: NO_GRAVATAR in text, LOG_DEADLINE)
    assert NO_GRAVATAR in logs
    # The warning above is all; test_stack_grafana.py's check of Grafana's error lines
    # runs before this module.
    errors = [
        line
        for line in logs.splitlines()
        if ERROR_LINE.search(line) and ("/avatar/" in line or "logger=avatar " in line)
    ]
    assert not errors, "\n".join(errors[:5])


def test_grafana_com_requests_stay_on_the_machine(grafana: Grafana) -> None:
    # The plugin catalog, dashboard templates and community dashboards reach grafana.com
    # through /api/gnet; its API URL is a closed port on Grafana's own loopback. The
    # error line this leaves is one that test_stack_grafana.py expects.
    started = time.monotonic()
    response = grafana.request("GET", "/api/gnet/plugins")
    assert response.status == 502
    assert time.monotonic() - started < GRAFANA_COM_DEADLINE


# Prometheus and the collector.


def test_prometheus_has_no_remote_storage_alerting_or_service_discovery(
    prometheus_url: str,
) -> None:
    # What could leave Prometheus: remote write and read, alerts to an Alertmanager, its
    # own traces, and scrapes of what service discovery finds. The configuration it runs
    # with has none.
    data = _mapping(_prometheus_data(prometheus_url, "/api/v1/status/config"), "data")
    config = _mapping(yaml.safe_load(str(data.get("yaml"))), "config")
    for key in PROMETHEUS_OUTBOUND_SECTIONS:
        assert not config.get(key), key
    # Even if Prometheus wrote an empty tracing block, it would name no endpoint.
    assert not _mapping(config.get("tracing") or {}, "tracing").get("endpoint")
    jobs = [_mapping(job, "job") for job in _list(config.get("scrape_configs"), "scrape_configs")]
    assert jobs
    for job in jobs:
        discovery = [key for key in job if key.endswith("_sd_configs")]
        assert not discovery, f"{job.get('job_name')}: {discovery}"
    managers = _mapping(_prometheus_data(prometheus_url, "/api/v1/alertmanagers"), "managers")
    assert managers == {"activeAlertmanagers": [], "droppedAlertmanagers": []}


def test_prometheus_scrapes_only_itself_and_the_stack(prometheus_url: str) -> None:
    data = _mapping(_prometheus_data(prometheus_url, "/api/v1/targets", {"state": "any"}), "data")
    assert _list(data.get("droppedTargets"), "droppedTargets") == []
    targets = [_mapping(item, "target") for item in _list(data.get("activeTargets"), "targets")]
    hosts = {urlsplit(str(target.get("scrapeUrl"))).hostname for target in targets}
    assert hosts
    assert hosts <= {PROMETHEUS_SELF, *_stack_services()}, hosts


def test_collector_exports_only_to_the_stacks_backends(prometheus_url: str) -> None:
    # The collector sends data only through its exporters. Its own metrics name each one
    # it runs: every exporter has a sending queue, whose capacity it reports from the
    # start, before any data goes through.
    # Polled until all are there, so a scrape taken mid-start is not read as the whole.
    exporters = _poll(
        lambda: _exporters(prometheus_url),
        lambda found: found >= BACKEND_EXPORTERS,
        SCRAPE_DEADLINE,
    )
    assert exporters == BACKEND_EXPORTERS


# The app.

type Address = tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]


def _address(text: str) -> Address:
    """A /proc/net address, ``<hex IP>:<hex port>``: an IPv4 address is one 32-bit word
    and an IPv6 address four, each in the kernel's byte order (little-endian on the
    amd64 and arm64 that Docker runs). An IPv4-mapped IPv6 address becomes IPv4."""
    host, port = text.split(":")
    raw = bytes.fromhex(host)
    swapped = b"".join(raw[index : index + 4][::-1] for index in range(0, len(raw), 4))
    address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(swapped)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address, int(port, 16)


def test_proc_net_addresses_are_read_in_kernel_byte_order() -> None:
    # From the app's own /proc/net/tcp and tcp6 (172.20.0.5:57574 to 172.20.0.6:3100).
    assert _address("050014AC:E0E6") == (ipaddress.ip_address("172.20.0.5"), 0xE0E6)
    assert _address("060014AC:0C1C") == (ipaddress.ip_address("172.20.0.6"), LOKI_PORT)
    assert _address("0000000000000000FFFF0000060014AC:0C1C") == (
        ipaddress.ip_address("172.20.0.6"),
        LOKI_PORT,
    )
    assert _address("00000000000000000000000001000000:24F8") == (
        ipaddress.ip_address("::1"),
        9464,
    )


def _service_addresses(
    network_inspect: Callable[[str], dict[str, object]], service: str
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The addresses of ``service``'s container on the project's networks."""
    found: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for network in ("backend", "edge"):
        containers = _mapping(network_inspect(network).get("Containers"), "Containers")
        for item in containers.values():
            entry = _mapping(item, "container")
            match = CONTAINER_NAME.fullmatch(str(entry.get("Name")))
            if match is None or match["service"] != service:
                continue
            for key in ("IPv4Address", "IPv6Address"):
                value = str(entry.get(key) or "")
                if value:
                    found.add(ipaddress.ip_interface(value).ip)
    return found


def test_the_app_connects_to_nothing_but_loki(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    network_inspect: Callable[[str], dict[str, object]],
) -> None:
    loki = _service_addresses(network_inspect, LOKI_SERVICE)
    assert loki, "Loki has no address on the project's networks"
    rows = json.loads(compose("exec", "-T", APP, "python", "-c", SOCKETS_SCRIPT).stdout)
    listening: set[int] = set()
    unexpected: list[str] = []
    to_loki = 0
    for table, local_text, remote_text, state in rows:
        (local, local_port), (remote, remote_port) = _address(local_text), _address(remote_text)
        tcp = table.startswith("tcp")
        if tcp and state == LISTEN:
            if not local.is_loopback:
                listening.add(local_port)
            continue
        if remote.is_unspecified and remote_port == 0:
            continue  # an unconnected UDP socket
        if local_port in APP_PORTS or remote.is_loopback:
            continue  # an answer on the app's ports, or loopback (healthcheck, DNS)
        if tcp and remote in loki and remote_port == LOKI_PORT:
            to_loki += 1
            continue
        unexpected.append(f"{table} {local}:{local_port} -> {remote}:{remote_port} ({state})")
    assert listening == APP_PORTS
    assert not unexpected, unexpected
    print(f"app sockets: {len(rows)}, to Loki: {to_loki}")


# Networks.


def test_backend_network_is_internal_and_holds_the_stores(
    network_inspect: Callable[[str], dict[str, object]],
) -> None:
    backend = network_inspect("backend")
    assert backend.get("Internal") is True
    containers = _mapping(backend.get("Containers"), "Containers").values()
    names = {str(_mapping(item, "container").get("Name")) for item in containers}
    services = {match["service"] for name in names if (match := CONTAINER_NAME.fullmatch(name))}
    assert services >= STORAGE_SERVICES, sorted(names)
    # The control: edge, the normal bridge network, reads as not internal.
    assert network_inspect("edge").get("Internal") is False
