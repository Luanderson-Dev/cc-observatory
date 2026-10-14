"""Static checks of the Prometheus config and its command line (ADR 0013, ADR 0016,
ADR 0018, ADR 0022).

Unit tests: they parse ``stack/prometheus/prometheus.yaml`` and the Prometheus service in
``compose.yaml``; Docker is not needed. ``tests/integration/test_stack_metrics.py`` runs
``promtool check config`` on the file inside the running Prometheus container (the CI
``stack`` job will run it too, from a later stage of milestone 1) and checks the stored
data against that Prometheus.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG: Final = ROOT / "stack" / "prometheus" / "prometheus.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
COLLECTOR_CONFIG: Final = ROOT / "stack" / "otel-collector" / "config.yaml"
ENV_EXAMPLE: Final = ROOT / ".env.example"

CONFIG_DIR: Final = "/etc/ccobs/prometheus"
DATA_DIR: Final = "/prometheus"
RETENTION_VARIABLE: Final = "CCOBS_PROMETHEUS_RETENTION"
RETENTION_DEFAULT: Final = "90d"
# Serves /api/v1/otlp/v1/metrics, which the collector pushes to (ADR 0013).
OTLP_RECEIVER: Final = "--web.enable-otlp-receiver"
# The zero sample at each stream's start, and the anchored range modifier (ADR 0013).
FEATURES: Final = frozenset({"created-timestamp-zero-ingestion", "promql-extended-range-selectors"})
# Flags that would open unauthenticated write or admin endpoints on Prometheus.
FORBIDDEN_FLAGS: Final = (
    "--web.enable-admin-api",
    "--web.enable-lifecycle",
    "--web.enable-remote-write-receiver",
)
# Required: without it, concurrent sessions lose whole requests (ADR 0013).
OUT_OF_ORDER_WINDOW: Final = "30m"
DEFAULT_TRANSLATION: Final = "UnderscoreEscapingWithSuffixes"
# The only resource attribute promoted to a label: the stack's host label (ADR 0022).
PROMOTED: Final = ["host"]
# Sections that would send data out of Prometheus or read it from elsewhere.
OUTBOUND_SECTIONS: Final = ("remote_write", "remote_read", "alerting")
# Keys a scrape job may use: static targets over plain HTTP, no service discovery.
SCRAPE_KEYS: Final = frozenset(
    {"job_name", "static_configs", "scrape_interval", "scrape_timeout", "metrics_path"}
)
# Prometheus scrapes itself and the collector's own telemetry (otelcol_*).
SELF_TARGET: Final = "localhost:9090"
COLLECTOR_SERVICE: Final = "otel-collector"
DURATION: Final = re.compile(r"(?P<amount>\d+)(?P<unit>h|d|w|y)")
_HOURS: Final = {"h": 1, "d": 24, "w": 24 * 7, "y": 24 * 365}


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _strings(value: object, where: str) -> list[str]:
    items = _list(value, where)
    assert all(isinstance(item, str) for item in items), f"{where} must hold strings"
    return [str(item) for item in items]


def _at(node: object, *path: str) -> object:
    """The value at ``path`` in nested mappings."""
    for depth, key in enumerate(path):
        node = _mapping(node, ".".join(path[:depth]) or "document").get(key)
    return node


def _config() -> dict[str, object]:
    return _mapping(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), CONFIG.name)


def _compose() -> dict[str, object]:
    return _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), COMPOSE.name)


def _service(name: str) -> dict[str, object]:
    services = _mapping(_compose().get("services"), "services")
    return _mapping(services.get(name), f"services.{name}")


def _command() -> list[str]:
    return _strings(_service("prometheus").get("command"), "prometheus command")


def _flag_values(flag: str) -> list[str]:
    """The values of every ``--flag=value`` in the command."""
    return [item.partition("=")[2] for item in _command() if item.partition("=")[0] == flag]


def _flag_names() -> set[str]:
    return {item.partition("=")[0] for item in _command()}


def _scrape_configs() -> list[dict[str, object]]:
    configs = _list(_config().get("scrape_configs"), "scrape_configs")
    return [_mapping(job, "scrape_configs entry") for job in configs]


def _targets(job: dict[str, object]) -> list[str]:
    targets: list[str] = []
    for group in _list(job.get("static_configs"), f"{job.get('job_name')} static_configs"):
        targets += _strings(_mapping(group, "static_configs entry").get("targets"), "targets")
    return targets


def _hours(duration: str) -> int:
    match = DURATION.fullmatch(duration)
    assert match, f"unexpected duration {duration!r}"
    return int(match["amount"]) * _HOURS[match["unit"]]


# Command line.


def test_every_flag_uses_the_equals_form() -> None:
    # One list item per flag, so these tests can read each value.
    for item in _command():
        assert re.fullmatch(r"--[a-z][a-z0-9.-]*(=\S+)?", item), f"unexpected item {item!r}"


def test_compose_runs_this_config_from_a_read_only_directory() -> None:
    assert _flag_values("--config.file") == [f"{CONFIG_DIR}/{CONFIG.name}"]
    volumes = _strings(_service("prometheus").get("volumes"), "prometheus volumes")
    assert f"./stack/prometheus:{CONFIG_DIR}:ro" in volumes
    assert CONFIG.is_file()


def test_data_lives_in_the_named_volume() -> None:
    assert _flag_values("--storage.tsdb.path") == [DATA_DIR]
    volumes = _strings(_service("prometheus").get("volumes"), "prometheus volumes")
    assert f"prometheus-data:{DATA_DIR}" in volumes


def test_retention_comes_from_the_environment_with_a_90_day_default() -> None:
    expected = f"${{{RETENTION_VARIABLE}:-{RETENTION_DEFAULT}}}"
    assert _flag_values("--storage.tsdb.retention.time") == [expected]
    assert _hours(RETENTION_DEFAULT) == 90 * 24
    assigned = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert assigned.get(RETENTION_VARIABLE) == RETENTION_DEFAULT


def test_the_otlp_receiver_is_on() -> None:
    assert OTLP_RECEIVER in _command()


def test_exactly_the_two_feature_flags_are_on() -> None:
    features = [
        feature for value in _flag_values("--enable-feature") for feature in value.split(",")
    ]
    assert sorted(features) == sorted(FEATURES)


def test_no_flag_opens_admin_lifecycle_or_remote_write_endpoints() -> None:
    assert not _flag_names() & set(FORBIDDEN_FLAGS)


def test_prometheus_listens_on_its_default_port_and_paths() -> None:
    # The collector's exporter, the healthcheck and the debug override expect :9090 and
    # the default paths (/api/v1/otlp/v1/metrics, /-/ready); an external URL with a path
    # would move them too.
    moved = {"--web.listen-address", "--web.route-prefix", "--web.external-url"}
    assert not _flag_names() & moved


# Storage.


def test_out_of_order_window_is_30_minutes() -> None:
    tsdb = _mapping(_at(_config(), "storage", "tsdb"), "storage.tsdb")
    assert tsdb.get("out_of_order_time_window") == OUT_OF_ORDER_WINDOW


def test_retention_is_set_only_on_the_command_line() -> None:
    # One place for the setting: the flag, which reads CCOBS_PROMETHEUS_RETENTION.
    tsdb = _mapping(_at(_config(), "storage", "tsdb"), "storage.tsdb")
    assert "retention" not in tsdb


# OTLP translation.


def test_otlp_translation_keeps_its_defaults_and_promotes_only_host() -> None:
    # claude_code.cost.usage -> claude_code_cost_usage_USD_total, session.id -> session_id,
    # service.name -> job; host, which the collector sets (ADR 0022), becomes a label on
    # every series; the other resource attributes stay on target_info.
    otlp = _mapping(_config().get("otlp"), "otlp")
    assert otlp.get("promote_resource_attributes") == PROMOTED
    assert "promote_all_resource_attributes" not in otlp
    assert "ignore_resource_attributes" not in otlp
    allowed = {"translation_strategy", "promote_resource_attributes"}
    assert set(otlp) <= allowed, f"unexpected otlp keys {sorted(set(otlp) - allowed)}"
    assert otlp.get("translation_strategy", DEFAULT_TRANSLATION) == DEFAULT_TRANSLATION
    # Not an external label, which Prometheus adds only to data that leaves it.
    assert "external_labels" not in _mapping(_config().get("global"), "global")


# Nothing leaves Prometheus (ADR 0006, ADR 0018).


def test_no_remote_storage_and_no_alerting() -> None:
    config = _config()
    for section in OUTBOUND_SECTIONS:
        assert section not in config, f"{section} would send data out of Prometheus"


def test_scrape_jobs_use_static_internal_targets_over_http() -> None:
    services = _mapping(_compose().get("services"), "services")
    backend = {
        name
        for name, service in services.items()
        if "backend" in _strings(_mapping(service, name).get("networks") or [], f"{name} networks")
    }
    for job in _scrape_configs():
        name = job.get("job_name")
        assert set(job) <= SCRAPE_KEYS, f"{name}: unexpected keys {sorted(set(job) - SCRAPE_KEYS)}"
        for target in _targets(job):
            parts = urlsplit(f"//{target}")
            assert parts.port, f"{name}: target {target!r} needs a port"
            if target != SELF_TARGET:
                assert parts.hostname in backend, f"{name}: {target!r} is not a backend service"


def test_scrape_job_names_are_unique() -> None:
    names = [job.get("job_name") for job in _scrape_configs()]
    assert len(names) == len(set(names)), names


def test_prometheus_scrapes_itself_and_the_collector_telemetry() -> None:
    targets = {target for job in _scrape_configs() for target in _targets(job)}
    collector = yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    readers = _list(_at(collector, "service", "telemetry", "metrics", "readers"), "readers")
    ports = {_at(reader, "pull", "exporter", "prometheus", "port") for reader in readers}
    assert len(ports) == 1, f"expected one Prometheus reader, got ports {ports}"
    assert targets == {SELF_TARGET, f"{COLLECTOR_SERVICE}:{ports.pop()}"}
