"""Static checks of Grafana's settings and datasources (ADR 0003, ADR 0006, ADR 0017).

Unit tests: they parse the Grafana service in ``compose.yaml`` and the provisioning
files under ``stack/grafana/``; Docker is not needed. Grafana has no offline check of its
settings, so ``tests/integration/test_stack_grafana.py`` and, for the outbound settings,
``tests/integration/test_stack_no_outbound.py`` check the same rules against a running
Grafana.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import pytest
import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE: Final = ROOT / "compose.yaml"
COMPOSE_DEBUG: Final = ROOT / "compose.debug.yaml"
STACK: Final = ROOT / "stack"
PROVISIONING: Final = STACK / "grafana" / "provisioning"
DATASOURCES: Final = PROVISIONING / "datasources" / "datasources.yaml"
TEMPO_CONFIG: Final = STACK / "tempo" / "config.yaml"
LOKI_CONFIG: Final = STACK / "loki" / "config.yaml"

SERVICE: Final = "grafana"
# compose.yaml mounts stack/grafana here, read-only; provisioning is its subdirectory.
CONFIG_DIR: Final = "/etc/ccobs/grafana"
PROVISIONING_DIR: Final = f"{CONFIG_DIR}/provisioning"
# Directories Grafana reads at every start, logging an error for each one that is
# missing; those with nothing to provision hold an empty file. The dashboard provider
# is checked in tests/test_grafana_dashboard.py.
EMPTY_PROVISIONING: Final = ("alerting", "plugins")
# The bundled overview dashboard, which is also Grafana's home page.
HOME_DASHBOARD: Final = f"{CONFIG_DIR}/dashboards/claude-code-overview.json"
# The fixed datasource UIDs that dashboards and the app's links use, each with its
# plugin type and the compose service it queries over the backend network.
DATASOURCE_TYPES: Final = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
DATASOURCE_SERVICES: Final = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
# Keys that would hold credentials, at the datasource level and in jsonData.
CREDENTIAL_KEYS: Final = ("user", "password", "basicAuthUser", "secureJsonData")
CREDENTIAL_JSON: Final = re.compile(r"(?i)auth|header|tls|password|token|secret|oauth")
# A URL anywhere in the datasources file, and its host.
URL: Final = re.compile(r"[a-z][a-z0-9+.-]*://(?P<host>[^/:\s\"']+)", re.I)
# The highest version Grafana 13.2 offers for Prometheus ("> 2.50.x"), which covers the
# stack's Prometheus 3.13: Grafana only checks for features older than that.
PROMETHEUS_VERSION: Final = "2.50.1"
# Grafana expands ${NAME} and $NAME in provisioning files; $$ is a literal $.
LITERAL_DOLLAR: Final = "$$"


@dataclass(frozen=True)
class Setting:
    """One Grafana setting as compose.yaml sets it: an ini section and key, through the
    environment variable Grafana derives from them, with its value."""

    section: str
    key: str
    value: str

    @property
    def variable(self) -> str:
        """``GF_<SECTION>_<KEY>``, upper case, with dots and dashes as underscores."""
        name = f"GF_{self.section}_{self.key}"
        return re.sub(r"[.-]", "_", name).upper()

    @property
    def name(self) -> str:
        """``section.key``, as the comments in compose.yaml name it."""
        return f"{self.section}.{self.key}"


# Where Grafana reads its provisioning, and the file it shows as its home page: both
# from the read-only mount of stack/grafana.
PATHS: Final = (
    Setting("paths", "provisioning", PROVISIONING_DIR),
    Setting("dashboards", "default_home_dashboard_path", HOME_DASHBOARD),
)
# Access (ADR 0017): whoever opens Grafana is an Editor of its one organization, with no
# login form, no basic auth and no admin user.
ACCESS: Final = (
    Setting("auth.anonymous", "enabled", "true"),
    Setting("auth.anonymous", "org_role", "Editor"),
    Setting("auth", "disable_login_form", "true"),
    Setting("auth", "disable_signout_menu", "true"),
    Setting("auth.basic", "enabled", "false"),
    Setting("security", "disable_initial_admin_creation", "true"),
)
# Browser defences (ADR 0017): Grafana answers only to localhost, which a DNS-rebinding
# page cannot be; refuses requests (GET included) from any other origin, even without a
# cookie; closes the legacy datasource proxy that a simple cross-site GET could reach;
# and does not serve /metrics, the widest route outside the Host check.
BROWSER: Final = (
    Setting("server", "domain", "localhost"),
    Setting("server", "enforce_domain", "true"),
    Setting("server", "root_url", "http://localhost:${CCOBS_GRAFANA_PORT:-4330}/"),
    Setting("security", "csrf_always_check", "true"),
    Setting("security", "data_source_proxy_whitelist", "127.0.0.1:9"),
    Setting("metrics", "enabled", "false"),
)
# No outbound traffic (ADR 0006): each of these defaults of Grafana 13.2.4 calls out to
# grafana.com, stats.grafana.org, gravatar.com, raintank.io or grafana.net.
OUTBOUND: Final = (
    Setting("analytics", "reporting_enabled", "false"),
    Setting("analytics", "check_for_updates", "false"),
    Setting("analytics", "check_for_plugin_updates", "false"),
    Setting("analytics", "feedback_links_enabled", "false"),
    Setting("news", "news_feed_enabled", "false"),
    Setting("security", "disable_gravatar", "true"),
    Setting("snapshots", "external_enabled", "false"),
    Setting("plugins", "public_key_retrieval_disabled", "true"),
    Setting("plugins", "preinstall_disabled", "true"),
    Setting("plugins", "preinstall_auto_update", "false"),
    Setting("plugins", "plugin_admin_enabled", "false"),
    Setting("cloud_migration", "enabled", "false"),
    # A closed port on Grafana's own loopback, for whatever still asks grafana.com.
    Setting("grafana_com", "api_url", "http://127.0.0.1:9/api"),
)
# Features the stack does not use: public dashboards, and alerting, which is the app's
# job (ADR 0007).
FEATURES: Final = (
    Setting("public_dashboards", "enabled", "false"),
    Setting("unified_alerting", "enabled", "false"),
)
# The backend datasource plugins bundled with Grafana 13.2.4 that no provisioned
# datasource uses. Grafana runs each bundled backend plugin as its own process, outside
# GOMEMLIMIT, so they are turned off; only the Prometheus, Loki and Tempo plugins run
# (tests/integration/test_stack_grafana.py checks the processes).
UNUSED_PLUGINS: Final = (
    "elasticsearch",
    "grafana-postgresql-datasource",
    "grafana-pyroscope-datasource",
    "influxdb",
    "jaeger",
    "mssql",
    "mysql",
    "opentsdb",
    "stackdriver",
    "zipkin",
)
PLUGINS: Final = (Setting("plugins", "disable_plugins", ",".join(UNUSED_PLUGINS)),)
SETTINGS: Final = (*PATHS, *ACCESS, *BROWSER, *OUTBOUND, *FEATURES, *PLUGINS)
# Environment variables of the Grafana service that are not Grafana settings.
OTHER_VARIABLES: Final = frozenset({"GOMEMLIMIT"})


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _load(path: Path) -> dict[str, object]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


def _service(path: Path, name: str = SERVICE) -> dict[str, object]:
    services = _mapping(_load(path).get("services"), f"{path.name} services")
    return _mapping(services.get(name), f"{path.name} services.{name}")


def _environment() -> dict[str, str]:
    """The Grafana service's environment, every value as a string. A YAML boolean
    becomes ``True`` or ``False``, which the tests then reject: compose wants strings."""
    environment = _mapping(_service(COMPOSE).get("environment"), "grafana environment")
    return {key: str(value) for key, value in environment.items()}


def _service_block() -> str:
    """The Grafana service's lines in compose.yaml, from its key to the next service or
    top-level key, with the comment block right above it."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {SERVICE}:")
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    end = len(lines)
    for index in range(lines.index(f"  {SERVICE}:") + 1, len(lines)):
        line = lines[index]
        if re.match(r"^(  )?[a-z]", line):
            end = index
            break
    return "\n".join(lines[start:end])


def _comments(block: str) -> str:
    return "\n".join(line.split("#", 1)[1] for line in block.splitlines() if "#" in line)


def _datasources() -> dict[str, dict[str, object]]:
    """The provisioned datasources by UID."""
    entries = _list(_load(DATASOURCES).get("datasources"), "datasources")
    found: dict[str, dict[str, object]] = {}
    for entry in entries:
        datasource = _mapping(entry, "datasource")
        uid = datasource.get("uid")
        assert isinstance(uid, str), f"a datasource has no uid: {datasource}"
        assert uid not in found, f"two datasources share the uid {uid}"
        found[uid] = datasource
    return found


def _json_data(uid: str) -> dict[str, object]:
    return _mapping(_datasources()[uid].get("jsonData"), f"{uid} jsonData")


def _container_port(service: str) -> int:
    """The container port compose.debug.yaml publishes for a store's HTTP API."""
    ports = _list(_service(COMPOSE_DEBUG, service).get("ports"), f"{service} ports")
    assert len(ports) == 1, f"{service}: expected one published port, got {ports}"
    return int(str(ports[0]).rsplit(":", 1)[1])


def _networks(service: Mapping[str, object]) -> set[str]:
    return {str(name) for name in _list(service.get("networks"), "networks")}


# Settings.


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda setting: setting.name)
def test_grafana_setting_has_its_value(setting: Setting) -> None:
    assert _environment().get(setting.variable) == setting.value, setting.variable


def test_grafana_sets_only_the_settings_listed_here() -> None:
    # A new setting needs a reason, here and in the comments of compose.yaml.
    variables = set(_environment()) - OTHER_VARIABLES
    assert variables == {setting.variable for setting in SETTINGS}


def test_every_outbound_setting_is_explained_in_compose() -> None:
    comments = _comments(_service_block())
    missing = [setting.name for setting in OUTBOUND if setting.name not in comments]
    assert not missing, f"the comments above Grafana's settings do not explain {missing}"


def test_only_unused_datasource_plugins_are_disabled() -> None:
    # Disabling the plugin of a provisioned datasource would break its panels and queries.
    disabled = _environment()["GF_PLUGINS_DISABLE_PLUGINS"].split(",")
    assert disabled == sorted(set(disabled)), "keep the list sorted, without repeats"
    assert all(re.fullmatch(r"[a-z0-9-]+", plugin) for plugin in disabled), disabled
    assert not set(disabled) & set(DATASOURCE_TYPES.values())


def test_disabled_plugins_are_explained_in_compose() -> None:
    comments = _comments(_service_block())
    for setting in PLUGINS:
        assert setting.name in comments, f"the comments above Grafana do not explain {setting.name}"


def test_root_url_names_localhost_and_the_published_port() -> None:
    # enforce_domain redirects every other host name to root_url, so its port must be
    # the one compose publishes, through the same variable and default.
    root_url = _environment()["GF_SERVER_ROOT_URL"]
    port = root_url.removeprefix("http://localhost:").removesuffix("/")
    published = _list(_service(COMPOSE).get("ports"), "grafana ports")
    assert published == [f"127.0.0.1:{port}:3000"]


def test_proxy_whitelist_matches_no_datasource() -> None:
    # Grafana's legacy datasource proxy forwards only to a host:port on this list, so one
    # that no datasource uses closes it for all of them (ADR 0017). An empty list would
    # turn the check off, and a datasource's host:port would reopen the proxy for it.
    entries = _environment()["GF_SECURITY_DATA_SOURCE_PROXY_WHITELIST"].split()
    assert entries, "an empty whitelist turns Grafana's check off"
    for entry in entries:
        assert re.fullmatch(r"[^\s:/]+:\d+", entry), f"{entry!r} is not host:port"
    used = {urlsplit(str(item.get("url"))).netloc for item in _datasources().values()}
    assert all(re.fullmatch(r"[^\s:/]+:\d+", netloc) for netloc in used), used
    assert not set(entries) & used, f"the whitelist reopens the proxy for {set(entries) & used}"


def test_grafana_has_no_admin_credentials_or_secrets() -> None:
    # No admin user is created, so there is no password to set; Grafana's own secrets
    # stay in its data volume.
    for variable in _environment():
        assert not variable.startswith(("GF_SECURITY_ADMIN_", "GF_SECURITY_SECRET_KEY"))
        assert not variable.endswith("__FILE"), variable


def test_grafana_reads_its_provisioning_from_the_read_only_mount() -> None:
    volumes = _list(_service(COMPOSE).get("volumes"), "grafana volumes")
    assert f"./stack/grafana:{CONFIG_DIR}:ro" in volumes
    assert (PROVISIONING / "datasources").is_dir()


@pytest.mark.parametrize("directory", EMPTY_PROVISIONING)
def test_unused_provisioning_directories_exist_and_provision_nothing(directory: str) -> None:
    files = sorted((PROVISIONING / directory).glob("*.yaml"))
    assert files, f"stack/grafana/provisioning/{directory} needs a YAML file"
    for path in files:
        assert _load(path) == {"apiVersion": 1}, path


# Datasources.


def test_datasources_have_fixed_uids_and_types() -> None:
    found = {uid: datasource.get("type") for uid, datasource in _datasources().items()}
    assert found == DATASOURCE_TYPES


def test_datasources_file_prunes_datasources_it_no_longer_lists() -> None:
    document = _load(DATASOURCES)
    assert document.get("apiVersion") == 1
    assert document.get("prune") is True


@pytest.mark.parametrize("uid", sorted(DATASOURCE_TYPES))
def test_datasource_is_read_only_and_queried_through_grafana(uid: str) -> None:
    datasource = _datasources()[uid]
    assert datasource.get("editable") is False
    # The browser never talks to the stores, which are only on the backend network.
    assert datasource.get("access") == "proxy"
    assert isinstance(datasource.get("name"), str)


@pytest.mark.parametrize("uid", sorted(DATASOURCE_TYPES))
def test_datasource_queries_its_service_on_the_backend_network(uid: str) -> None:
    service = DATASOURCE_SERVICES[uid]
    url = urlsplit(str(_datasources()[uid].get("url")))
    assert url.scheme == "http"
    assert url.hostname == service
    assert url.port == _container_port(service)
    assert url.path in ("", "/")
    assert "backend" in _networks(_service(COMPOSE, service))
    assert "backend" in _networks(_service(COMPOSE))


@pytest.mark.parametrize("uid", sorted(DATASOURCE_TYPES))
def test_datasource_has_no_credentials(uid: str) -> None:
    datasource = _datasources()[uid]
    for key in CREDENTIAL_KEYS:
        assert key not in datasource, f"{uid}: {key}"
    assert datasource.get("basicAuth") in (None, False)
    assert datasource.get("withCredentials") in (None, False)
    keys = [key for key in _json_data(uid) if CREDENTIAL_JSON.search(key)]
    assert not keys, f"{uid} jsonData: {keys}"


def test_datasources_file_points_only_at_the_stack() -> None:
    text = DATASOURCES.read_text(encoding="utf-8")
    hosts = {match["host"] for match in URL.finditer(text)}
    assert hosts == set(DATASOURCE_SERVICES.values())


def test_datasources_file_has_no_variables_for_grafana_to_expand() -> None:
    # Grafana fills ${NAME} and $NAME from its environment into provisioning files (Go's
    # os.ExpandEnv, which leaves a $ that ends the value, as in a regex, alone). Grafana's
    # own link variables, such as ${__value.raw}, are written $${...}.
    lines = DATASOURCES.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        unescaped = line.replace(LITERAL_DOLLAR, "").removesuffix("$")
        assert "$" not in unescaped, f"datasources.yaml:{number}: {line.strip()}"


def test_prometheus_is_the_default_and_queried_with_post() -> None:
    datasource = _datasources()["prometheus"]
    assert datasource.get("isDefault") is True
    others = [uid for uid, item in _datasources().items() if uid != "prometheus"]
    assert all(_datasources()[uid].get("isDefault") in (None, False) for uid in others)
    json_data = _json_data("prometheus")
    # POST keeps long PromQL queries out of the URL.
    assert json_data.get("httpMethod") == "POST"
    assert json_data.get("prometheusType") == "Prometheus"
    assert json_data.get("prometheusVersion") == PROMETHEUS_VERSION


def test_loki_links_events_to_their_trace_in_tempo() -> None:
    # Events with a trace_id carry it as structured metadata (ADR 0015), which Grafana
    # returns among the entry's labels.
    fields = _list(_json_data("loki").get("derivedFields"), "derivedFields")
    assert len(fields) == 1
    field = _mapping(fields[0], "derived field")
    assert field.get("matcherType") == "label"
    pattern = re.compile(str(field.get("matcherRegex")))
    assert pattern.search("trace_id")
    assert not any(pattern.search(key) for key in ("span_id", "parent_trace_id", "trace_ids"))
    assert field.get("datasourceUid") == "tempo"
    # The link's query is the trace ID itself; $$ keeps ${__value.raw} for Grafana.
    assert field.get("url") == "$${__value.raw}"


def test_tempo_streams_search_results_as_tempo_allows() -> None:
    tempo = _load(TEMPO_CONFIG)
    assert tempo.get("stream_over_http_enabled") is True
    streaming = _mapping(_json_data("tempo").get("streamingEnabled"), "streamingEnabled")
    assert streaming.get("search") is True
    # TraceQL metrics need Tempo's metrics generator, which the stack does not run.
    assert streaming.get("metrics") in (None, False)


def test_tempo_links_spans_to_their_events_in_loki() -> None:
    links = _mapping(_json_data("tempo").get("tracesToLogsV2"), "tracesToLogsV2")
    assert links.get("datasourceUid") == "loki"
    # Grafana 13.2 filters on the trace_id structured metadata field (or the line).
    assert links.get("filterByTraceID") is True
    assert links.get("customQuery") in (None, False)
    # The span's resource attributes that are Loki's index labels select the stream,
    # under the names Loki stores them with (dots become underscores).
    tags: dict[str, str] = {}
    for entry in _list(links.get("tags"), "tags"):
        tag = _mapping(entry, "tag")
        tags[str(tag.get("key"))] = str(tag.get("value") or tag.get("key"))
    assert set(tags) == set(_index_label_attributes())
    for key, label in tags.items():
        assert label == key.replace(".", "_"), key


def _index_label_attributes() -> list[str]:
    """The resource attributes Loki turns into index labels (stack/loki/config.yaml)."""
    limits = _mapping(_load(LOKI_CONFIG).get("limits_config"), "limits_config")
    otlp = _mapping(limits.get("otlp_config"), "otlp_config")
    resource = _mapping(otlp.get("resource_attributes"), "resource_attributes")
    attributes: list[str] = []
    for rule in _list(resource.get("attributes_config"), "attributes_config"):
        item = _mapping(rule, "attributes_config rule")
        if item.get("action") == "index_label":
            attributes += [str(name) for name in _list(item.get("attributes"), "attributes")]
    return attributes
