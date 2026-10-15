"""Static checks of the Tempo config (ADR 0003, ADR 0006, ADR 0016, ADR 0018).

Unit tests: they parse ``stack/tempo/config.yaml`` and the Tempo service in
``compose.yaml``; Docker is not needed. The CI ``stack`` job runs ``tempo
-config.verify=true`` on the same file, and ``tests/integration/test_stack_traces.py``
checks the same rules against a running Tempo.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG: Final = ROOT / "stack" / "tempo" / "config.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
COMPOSE_DEBUG: Final = ROOT / "compose.debug.yaml"
COLLECTOR_CONFIG: Final = ROOT / "stack" / "otel-collector" / "config.yaml"
ENV_EXAMPLE: Final = ROOT / ".env.example"

# The named volume compose mounts for Tempo's data, and where: the image's own data
# directory (/var/tempo, owned by its user 10001), where every default path points.
DATA_DIR: Final = PurePosixPath("/var/tempo")
DATA_VOLUME: Final = "tempo-data"
CONFIG_DIR: Final = "/etc/ccobs/tempo"
HTTP_PORT: Final = 3200
# The OTLP/HTTP receiver, on every interface of the container: the collector runs in
# another one, over the internal backend network.
RECEIVER_ENDPOINT: Final = "0.0.0.0:4318"
TEMPO_EXPORTER: Final = "otlp_http/tempo"
RETENTION_VARIABLE: Final = "CCOBS_TEMPO_RETENTION"
RETENTION_DEFAULT: Final = "168h"
# In Tempo 3.x the backend worker runs the retention jobs, with its own compaction
# settings; the scheduler's compaction block_retention does not apply retention.
RETENTION_PATH: Final = ("backend_worker", "compaction", "block_retention")
# ${VAR} and ${VAR:-default}, which -config.expand-env fills from the environment.
VARIABLE: Final = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")
# A Go duration as Tempo parses it (time.Duration): no days or weeks.
GO_DURATION: Final = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|ms|s|m|h))+")
HOURS: Final = re.compile(r"(?P<hours>\d+)h")
# Environment variables that make Tempo trace itself and send its own spans out.
SELF_TRACING_PREFIXES: Final = ("OTEL_", "JAEGER_")
# After SIGTERM the backend worker keeps asking the stopped scheduler for jobs until its
# finish_on_shutdown_timeout runs out (upstream default 30s); Compose's grace period
# must outlast it with room to spare, or Tempo is killed (exit 137).
MAX_FINISH_SECONDS: Final = 10
GRACE_MARGIN_SECONDS: Final = 5
COMPOSE_DURATION: Final = re.compile(r"(?P<amount>\d+)(?P<unit>ms|s|m)")
_SECONDS: Final = {"ms": 0.001, "s": 1, "m": 60}


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _config() -> dict[str, object]:
    return _mapping(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), CONFIG.name)


def _at(node: object, *path: str) -> object:
    """The value at ``path`` in nested mappings (``None`` where a key is missing)."""
    for depth, key in enumerate(path):
        if node is None:
            return None
        node = _mapping(node, ".".join(path[:depth]) or CONFIG.name).get(key)
    return node


def _service(path: Path, name: str = "tempo") -> dict[str, object]:
    compose = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
    services = _mapping(compose.get("services"), f"{path.name} services")
    return _mapping(services.get(name), f"{path.name} services.{name}")


def _keyed(node: object, wanted: str, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """The paths of every key named ``wanted`` under ``node``."""
    found: list[tuple[str, ...]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = (*prefix, str(key))
            if key == wanted:
                found.append(path)
            found.extend(_keyed(value, wanted, path))
    return found


def _paths(node: object) -> list[str]:
    """Every string value under a key that names a path or a directory."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and str(key).endswith(("path", "_dir", "directory")):
                found.append(value)
            else:
                found.extend(_paths(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_paths(value))
    return found


def _seconds(duration: object) -> float:
    """A simple duration such as ``5s`` or ``1m`` (Tempo's and Compose's) in seconds."""
    match = COMPOSE_DURATION.fullmatch(str(duration))
    assert match, f"unexpected duration {duration!r}"
    return int(match["amount"]) * _SECONDS[match["unit"]]


def _env_example() -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


# Single binary, single tenant.


def test_tempo_runs_single_tenant() -> None:
    # Tempo's default; without a tenant header every request goes to the one tenant.
    config = _config()
    assert config.get("multitenancy_enabled") in (None, False)
    assert config.get("auth_enabled") in (None, False)


def test_tempo_runs_the_single_binary() -> None:
    # The default target, all: distributor, live store, querier, query frontend and the
    # backend scheduler and worker in one process.
    assert _config().get("target") in (None, "all")


def test_tempo_listens_where_the_debug_override_and_the_healthcheck_expect() -> None:
    assert _at(_config(), "server", "http_listen_port") == HTTP_PORT
    ports = _service(COMPOSE_DEBUG).get("ports")
    assert isinstance(ports, list)
    assert [str(port).rsplit(":", 1)[-1] for port in ports] == [str(HTTP_PORT)]


# The receiver.


def test_the_only_receiver_is_otlp_over_http_on_the_backend_network() -> None:
    receivers = _mapping(_at(_config(), "distributor", "receivers"), "distributor.receivers")
    assert set(receivers) == {"otlp"}
    protocols = _mapping(_at(receivers, "otlp", "protocols"), "otlp.protocols")
    # The collector pushes over HTTP only; no gRPC receiver to keep open.
    assert set(protocols) == {"http"}
    assert _at(protocols, "http", "endpoint") == RECEIVER_ENDPOINT


def test_the_receiver_is_where_the_collector_pushes_traces() -> None:
    collector = _mapping(yaml.safe_load(COLLECTOR_CONFIG.read_text("utf-8")), "collector")
    exporter = _mapping(_at(collector, "exporters", TEMPO_EXPORTER), TEMPO_EXPORTER)
    endpoint = urlsplit(str(exporter.get("endpoint")))
    assert endpoint.hostname == "tempo"
    assert endpoint.port == int(RECEIVER_ENDPOINT.rsplit(":", 1)[-1])


# Storage.


def test_traces_are_stored_on_the_local_backend() -> None:
    assert _at(_config(), "storage", "trace", "backend") == "local"


def test_data_lives_under_the_mounted_volume() -> None:
    paths = _paths(_config())
    assert paths, "the config sets no data path"
    for path in paths:
        assert PurePosixPath(path).is_relative_to(DATA_DIR), f"{path} is outside {DATA_DIR}"
    volumes = _service(COMPOSE).get("volumes")
    assert isinstance(volumes, list)
    assert f"{DATA_VOLUME}:{DATA_DIR}" in volumes


def test_tempo_exits_within_the_compose_grace_period() -> None:
    # A short finish timeout lets Tempo exit by itself on SIGTERM; an explicit grace
    # period, rather than the Docker daemon's default-stop-timeout (10 s upstream, 3 s on
    # one Docker Desktop), gives it the time to.
    finish = _seconds(_at(_config(), "backend_worker", "finish_on_shutdown_timeout"))
    grace = _seconds(_service(COMPOSE).get("stop_grace_period"))
    assert 0 < finish <= MAX_FINISH_SECONDS
    assert grace >= finish + GRACE_MARGIN_SECONDS


def test_compose_runs_this_config_with_env_expansion() -> None:
    service = _service(COMPOSE)
    # Tempo's -config.* switches need an explicit =true.
    assert service.get("command") == [
        f"-config.file={CONFIG_DIR}/config.yaml",
        "-config.expand-env=true",
    ]
    volumes = service.get("volumes")
    assert isinstance(volumes, list)
    assert f"./stack/tempo:{CONFIG_DIR}:ro" in volumes


# Retention.


def test_retention_comes_from_the_environment_with_a_7_day_default() -> None:
    assert _at(_config(), *RETENTION_PATH) == f"${{{RETENTION_VARIABLE}:-{RETENTION_DEFAULT}}}"
    match = HOURS.fullmatch(RETENTION_DEFAULT)
    assert match
    assert int(match["hours"]) == 7 * 24


def test_retention_is_set_only_where_tempo_applies_it() -> None:
    # A block_retention elsewhere (the scheduler's compaction settings, or the per-tenant
    # overrides) would look like retention without being the one that applies.
    assert _keyed(_config(), "block_retention") == [RETENTION_PATH]


def test_retention_default_is_a_go_duration() -> None:
    # Tempo refuses 7d: Go durations stop at hours.
    assert GO_DURATION.fullmatch(RETENTION_DEFAULT)
    assert _env_example().get(RETENTION_VARIABLE) == RETENTION_DEFAULT


def test_compose_passes_the_retention_variable_to_tempo() -> None:
    environment = _mapping(_service(COMPOSE).get("environment"), "tempo environment")
    assert environment.get(RETENTION_VARIABLE) == f"${{{RETENTION_VARIABLE}:-{RETENTION_DEFAULT}}}"


def test_only_the_retention_variable_is_expanded() -> None:
    # -config.expand-env replaces every ${VAR} and $VAR in the file, comments included.
    text = CONFIG.read_text(encoding="utf-8")
    assert {match["name"] for match in VARIABLE.finditer(text)} == {RETENTION_VARIABLE}
    assert "$" not in VARIABLE.sub("", text)


# Nothing beyond storing and querying traces, and no outbound traffic (ADR 0006).


def test_usage_reporting_is_off() -> None:
    assert _at(_config(), "usage_report", "reporting_enabled") is False


def test_metrics_generator_is_off() -> None:
    # It would derive span metrics and push them to Prometheus's remote write receiver,
    # which the stack keeps off; with no storage path Tempo leaves it disabled.
    config = _config()
    assert "metrics_generator" not in config
    assert not _keyed(config, "metrics_generator")
    assert not _keyed(config, "remote_write")


def test_mcp_server_is_not_enabled() -> None:
    # The upstream example turns it on; it would let AI tools query the traces.
    assert _at(_config(), "query_frontend", "mcp_server", "enabled") in (None, False)


def test_tempo_does_not_trace_itself() -> None:
    # Tempo's own tracing is off unless OTEL_* or JAEGER_* variables are set.
    environment = _mapping(_service(COMPOSE).get("environment"), "tempo environment")
    assert not [name for name in environment if name.startswith(SELF_TRACING_PREFIXES)]
