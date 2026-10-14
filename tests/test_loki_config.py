"""Static checks of the Loki config (ADR 0015, ADR 0016, ADR 0006).

Unit tests: they parse ``stack/loki/config.yaml`` and the Loki service in
``compose.yaml``; Docker is not needed. The integration tests in
``tests/integration/test_stack_logs.py`` check the same rules against a running Loki.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG: Final = ROOT / "stack" / "loki" / "config.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
ENV_EXAMPLE: Final = ROOT / ".env.example"

# The named volume compose mounts for Loki's data, and where.
DATA_DIR: Final = PurePosixPath("/loki")
CONFIG_DIR: Final = "/etc/ccobs/loki"
RETENTION_VARIABLE: Final = "CCOBS_LOKI_RETENTION"
RETENTION_DEFAULT: Final = "720h"
# The resource attributes that become index labels (service.name is stored as
# service_name); every other attribute becomes structured metadata (ADR 0015).
INDEX_LABEL_ATTRIBUTES: Final = ["service.name", "host"]
# Attributes that must never be index labels: one value per session, prompt, event or
# span would create a stream each (ADR 0016).
NEVER_LABELS: Final = ("session.id", "prompt.id", "event.name", "trace_id", "span_id")
# ${VAR} and ${VAR:-default}, which -config.expand-env fills from the environment.
VARIABLE: Final = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")
# A Prometheus-style duration as Loki parses it, such as 720h or 30d.
DURATION: Final = re.compile(r"(?P<amount>\d+)(?P<unit>h|d|w)")
_HOURS: Final = {"h": 1, "d": 24, "w": 24 * 7}


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _list(value: object, where: str) -> list[object]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _config() -> dict[str, object]:
    return _mapping(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), CONFIG.name)


def _section(*path: str) -> dict[str, object]:
    node: object = _config()
    for depth, key in enumerate(path):
        node = _mapping(node, ".".join(path[:depth]) or CONFIG.name).get(key)
    return _mapping(node, ".".join(path))


def _loki_service() -> dict[str, object]:
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), COMPOSE.name)
    return _mapping(_mapping(compose.get("services"), "services").get("loki"), "services.loki")


def _hours(duration: str) -> int:
    match = DURATION.fullmatch(duration)
    assert match, f"unexpected duration {duration!r}"
    return int(match["amount"]) * _HOURS[match["unit"]]


def _paths(node: object) -> list[str]:
    """Every string value under a key that names a directory or a path prefix."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and str(key).endswith(("directory", "_dir", "path_prefix")):
                found.append(value)
            else:
                found.extend(_paths(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_paths(value))
    return found


# Single binary, single tenant.


def test_loki_runs_single_tenant_with_an_in_memory_ring() -> None:
    config = _config()
    assert config.get("auth_enabled") is False
    common = _section("common")
    assert common.get("replication_factor") == 1
    assert _section("common", "ring", "kvstore").get("store") == "inmemory"


def test_loki_listens_where_the_collector_and_the_debug_override_expect() -> None:
    assert _section("server").get("http_listen_port") == 3100


def test_stream_sharding_is_off() -> None:
    # It splits streams above 1.5 MB/s, which one machine never reaches; while it is on,
    # the distributor logs an "empty ring" error at every start.
    assert _section("limits_config", "shard_streams").get("enabled") is False


# Storage.


def test_schema_is_v13_on_tsdb_with_a_daily_index() -> None:
    configs = _list(_section("schema_config").get("configs"), "schema_config.configs")
    assert configs, "schema_config needs at least one period"
    for entry in configs:
        period = _mapping(entry, "schema_config.configs entry")
        assert period.get("store") == "tsdb"
        assert period.get("schema") == "v13"
        assert period.get("object_store") == "filesystem"
        # Retention needs the 24h index period.
        assert _mapping(period.get("index"), "index").get("period") == "24h"


def test_data_lives_under_the_mounted_volume() -> None:
    assert _section("common").get("path_prefix") == str(DATA_DIR)
    paths = _paths(_config())
    assert paths, "the config sets no data directory"
    for path in paths:
        assert PurePosixPath(path).is_relative_to(DATA_DIR), f"{path} is outside {DATA_DIR}"
    volumes = _loki_service().get("volumes")
    assert isinstance(volumes, list)
    assert f"loki-data:{DATA_DIR}" in volumes


def test_compose_runs_this_config_with_env_expansion() -> None:
    service = _loki_service()
    command = service.get("command")
    assert command == [f"-config.file={CONFIG_DIR}/config.yaml", "-config.expand-env=true"]
    volumes = service.get("volumes")
    assert isinstance(volumes, list)
    assert f"./stack/loki:{CONFIG_DIR}:ro" in volumes


# Labels and structured metadata.


def test_structured_metadata_is_allowed() -> None:
    assert _section("limits_config").get("allow_structured_metadata") is True


def test_only_service_name_and_host_are_index_labels() -> None:
    resource = _section("limits_config", "otlp_config", "resource_attributes")
    # Without ignore_defaults, Loki also indexes its default list (k8s.*, cloud.*, ...).
    assert resource.get("ignore_defaults") is True
    rules = [
        _mapping(rule, "attributes_config entry")
        for rule in _list(resource.get("attributes_config"), "attributes_config")
    ]
    labels = [rule for rule in rules if rule.get("action") == "index_label"]
    assert len(labels) == 1, f"expected one index_label rule, got {labels}"
    assert labels[0].get("attributes") == INDEX_LABEL_ATTRIBUTES
    assert "regex" not in labels[0], "a regex could promote more attributes"


def test_log_and_scope_attributes_stay_structured_metadata() -> None:
    # Loki stores every log record and scope attribute as structured metadata unless a
    # rule says otherwise; a drop rule would lose session.id or event.name.
    otlp = _section("limits_config", "otlp_config")
    assert set(otlp) == {"resource_attributes"}, f"unexpected otlp_config keys {sorted(otlp)}"


def test_session_and_trace_ids_are_never_index_labels() -> None:
    otlp = yaml.safe_dump(_section("limits_config", "otlp_config"))
    for name in NEVER_LABELS:
        assert name not in otlp, f"{name} appears in otlp_config"


# Retention.


def test_retention_comes_from_the_environment_with_a_30_day_default() -> None:
    limits = _section("limits_config")
    assert limits.get("retention_period") == f"${{{RETENTION_VARIABLE}:-{RETENTION_DEFAULT}}}"
    assert _hours(RETENTION_DEFAULT) == 30 * 24


def test_retention_default_is_a_multiple_of_the_index_period() -> None:
    assert _hours(RETENTION_DEFAULT) % 24 == 0
    assigned = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert assigned.get(RETENTION_VARIABLE) == RETENTION_DEFAULT
    assert _hours(assigned[RETENTION_VARIABLE]) % 24 == 0


def test_compose_passes_the_retention_variable_to_loki() -> None:
    environment = _mapping(_loki_service().get("environment"), "loki environment")
    assert environment.get(RETENTION_VARIABLE) == f"${{{RETENTION_VARIABLE}:-{RETENTION_DEFAULT}}}"


def test_compactor_applies_retention() -> None:
    compactor = _section("compactor")
    assert compactor.get("retention_enabled") is True
    # Loki refuses retention without a store for delete requests.
    assert compactor.get("delete_request_store") == "filesystem"
    working = compactor.get("working_directory")
    assert isinstance(working, str)
    assert PurePosixPath(working).is_relative_to(DATA_DIR)


def test_only_the_retention_variable_is_expanded() -> None:
    # -config.expand-env replaces every ${VAR} in the file, comments included.
    found = {match["name"] for match in VARIABLE.finditer(CONFIG.read_text(encoding="utf-8"))}
    assert found == {RETENTION_VARIABLE}
    assert "$" not in VARIABLE.sub("", CONFIG.read_text(encoding="utf-8"))


# No outbound traffic (ADR 0006).


def test_usage_reporting_is_off() -> None:
    assert _section("analytics").get("reporting_enabled") is False
