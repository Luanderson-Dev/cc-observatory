"""Static checks of the OpenTelemetry Collector config (ADR 0003, ADR 0006, ADR 0013,
ADR 0015, ADR 0020, ADR 0021, ADR 0022).

Unit tests: they parse ``stack/otel-collector/config.yaml`` and the OTTL statements of
its privacy processors, and compare the keys and events they delete with the canaries
that ``tools.stack.replay`` plants, so that every planted canary is covered by a filter.
They also check the processor that sets the stack's host label, where each signal goes,
and that each exporter's queue lets the collector stop within the grace period that
``compose.yaml`` gives it. The integration tests send the canaries through a running
collector.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import pytest
import yaml

from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL
from tools.stack.replay import (
    CONTENT_EVENT_NAMES,
    CONTENT_SPAN_EVENT,
    DATA_POINT_CANARY_KEYS,
    DEFAULT_FIXTURES,
    IDENTITY_CANARY_KEYS,
    LOG_CONTENT_CANARY_KEYS,
    LOG_RECORD_CANARY_KEYS,
    RESOURCE_CANARY_KEYS,
    SPAN_CANARY_KEYS,
    SPAN_CONTENT_CANARY_KEYS,
    SPAN_EVENT_CANARY_KEYS,
    is_identity_key,
)

ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG: Final = ROOT / "stack" / "otel-collector" / "config.yaml"
COMPOSE: Final = ROOT / "compose.yaml"
LOKI_CONFIG: Final = ROOT / "stack" / "loki" / "config.yaml"
TEMPO_CONFIG: Final = ROOT / "stack" / "tempo" / "config.yaml"

IDENTITY: Final = "transform/identity"
CONTENT_FILTER: Final = "filter/content"
CONTENT_TRANSFORM: Final = "transform/content"
# The processors that delete personal data, in pipeline order where present (ADR 0021).
PRIVACY_TRANSFORMS: Final = (IDENTITY, CONTENT_TRANSFORM)
# The stack's host label (ADR 0022): the resource attribute host, set from
# CCOBS_HOST_LABEL with the default local, right after the privacy processors.
HOST_PROCESSOR: Final = "transform/host"
HOST_VARIABLE: Final = "CCOBS_HOST_LABEL"
HOST_SET: Final = (
    f'set(resource.attributes["{HOST_LABEL}"], "${{env:{HOST_VARIABLE}:-{DEFAULT_HOST}}}")'
)
# Where Claude Code copies OTEL_RESOURCE_ATTRIBUTES, so a client's host could arrive, per
# signal: the host step deletes it there, and the resource's value is the only one.
HOST_DELETE_CONTEXTS: Final = {
    "metrics": ("datapoint",),
    "logs": ("log",),
    "traces": ("span", "spanevent"),
}
# The OTTL statement lists of the transform processor, per signal.
STATEMENTS: Final = {
    "metrics": "metric_statements",
    "logs": "log_statements",
    "traces": "trace_statements",
}
# Where the collector must delete identity attributes, per signal (ADR 0021).
IDENTITY_CONTEXTS: Final = {
    "metrics": ("resource", "datapoint"),
    "logs": ("resource", "log"),
    "traces": ("resource", "span", "spanevent"),
}
# The keys replay plants at each level, per signal: every one must be deleted.
PLANTED: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "metrics": {"resource": RESOURCE_CANARY_KEYS, "datapoint": DATA_POINT_CANARY_KEYS},
    "logs": {"resource": RESOURCE_CANARY_KEYS, "log": LOG_RECORD_CANARY_KEYS},
    "traces": {
        "resource": RESOURCE_CANARY_KEYS,
        "span": SPAN_CANARY_KEYS,
        "spanevent": SPAN_EVENT_CANARY_KEYS,
    },
}
HOST_PATHS: Final = "workspace.host_paths"
# Exporters that would print telemetry, content included, into the container logs.
FORBIDDEN_EXPORTERS: Final = frozenset({"debug", "file", "logging"})
# Events go to Loki's native OTLP endpoint; the exporter appends /v1/logs (ADR 0015).
LOKI_EXPORTER: Final = "otlp_http/loki"
LOKI_ENDPOINT: Final = "http://loki:3100/otlp"
# Metrics go to Prometheus's native OTLP receiver, made cumulative first (ADR 0013). The
# exporter appends /v1/metrics to a plain endpoint, so the full URL is metrics_endpoint.
PROMETHEUS_EXPORTER: Final = "otlp_http/prometheus"
PROMETHEUS_METRICS_ENDPOINT: Final = "http://prometheus:9090/api/v1/otlp/v1/metrics"
DELTA_TO_CUMULATIVE: Final = "delta_to_cumulative"
# Streams of a session that waits on a person for hours keep their running totals.
MAX_STALE: Final = "24h"
# Exporters that would take metrics to Prometheus another way (ADR 0013).
OTHER_PROMETHEUS_EXPORTERS: Final = frozenset(
    {"prometheus", "prometheusremotewrite", "prometheus_remote_write"}
)
# The pre-0.162.0 names, now deprecated aliases.
DEPRECATED_TYPES: Final = frozenset({"deltatocumulative", "otlphttp"})
# Traces go to Tempo's OTLP/HTTP receiver; the exporter appends /v1/traces. Tempo only
# runs in the traces profile (ADR 0003).
TEMPO_EXPORTER: Final = "otlp_http/tempo"
TEMPO_ENDPOINT: Final = "http://tempo:4318"
# While Tempo is off, the traces exporter drops what it gets within a minute and holds
# at most this many requests, so traces never pile up in the collector's memory.
MAX_TEMPO_QUEUE_REQUESTS: Final = 200
MAX_TEMPO_RETRY_SECONDS: Final = 60
# The exporters that push to the stack's backends over the internal network.
BACKEND_EXPORTERS: Final = (LOKI_EXPORTER, PROMETHEUS_EXPORTER, TEMPO_EXPORTER)
# Where the collector serves its own metrics (otelcol_*) for Prometheus to scrape: every
# interface of its container, since Prometheus runs in another one; never published.
TELEMETRY_ADDRESS: Final = "0.0.0.0:8888"
# Upper bound for an exporter's in-memory queue, in requests: enough to ride out a backend
# restart, small enough to stay well inside the collector's memory limit.
MAX_QUEUE_REQUESTS: Final = 5000
# Each backend exporter's queue, in requests (those in flight count), served by one
# consumer per request, and the timeout of one export attempt.
QUEUE_REQUESTS: Final = {LOKI_EXPORTER: 100, PROMETHEUS_EXPORTER: 100, TEMPO_EXPORTER: 20}
ATTEMPT_TIMEOUT: Final = "5s"
# The share of the collector's stop_grace_period (compose.yaml) that the exporters
# together may take to stop while their backends are down (ADR 0020). The collector
# shuts its components down one after another, exporters last, so their waits add up;
# the rest is for its other components and for Docker.
SHUTDOWN_SHARE: Final = 0.5
COLLECTOR_SERVICE: Final = "otel-collector"
# The keys the backend exporters may use, each seen to take effect. The collector 0.162.0
# accepts an unknown key at an exporter's top level or in retry_on_failure with no error
# from `validate` or at start-up (sending_queue is checked), so a misspelt timeout would
# go unnoticed.
EXPORTER_KEYS: Final = frozenset(
    {"endpoint", "metrics_endpoint", "timeout", "sending_queue", "retry_on_failure"}
)
RETRY_KEYS: Final = frozenset({"enabled", "initial_interval", "max_interval", "max_elapsed_time"})
# Keys near the identity rule that must survive it.
KEPT_KEYS: Final = (
    "user",
    "user_prompt",
    "user_prompt_length",
    "user_system_prompt",
    "username",
    "enduser",
    "enduser_id",
    "xuser.id",
    "superuser.id",
    "host",
    "host.arch",
    "host.id",
    "organization",
    "organization.name",
    "identity",
    "session.id",
    "prompt.id",
    "service.name",
)

# Metadata near the content names that must survive every privacy processor (ADR 0021).
METADATA_KEYS: Final = (
    "skill_name",
    "subagent_type",
    "workflow.name",
    "vcs.repository.name",
    "managed_settings.helper.path",
    "prompt_length",
    "tool_input_size_bytes",
    "error_type",
    "error_class",
    "status_code",
    "hook_name",
    "tool_name",
)

# OTTL paths carry their context prefix (``log.attributes``), as 0.162.0 expects.
DELETE_KEY: Final = re.compile(r'^delete_key\((?P<context>\w+)\.attributes, "(?P<key>[^"\\]+)"\)$')
DELETE_MATCHING: Final = re.compile(
    r'^delete_matching_keys\((?P<context>\w+)\.attributes, "(?P<pattern>(?:[^"\\]|\\.)+)"\)$'
)
EVENT_NAME_CONDITION: Final = re.compile(r'^log\.attributes\["event\.name"\] == "(?P<name>[^"]+)"$')
EVENT_FIELD_CONDITION: Final = re.compile(r'^log\.event_name == "(?P<name>[^"]+)"$')
BODY_CONDITION: Final = re.compile(r'^log\.body == "claude_code\.(?P<name>[^"]+)"$')
SPAN_EVENT_CONDITION: Final = re.compile(r'^spanevent\.name == "(?P<name>[^"]+)"$')


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _strings(value: object, where: str) -> list[str]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    assert all(isinstance(item, str) for item in value), f"{where} must hold strings"
    return [str(item) for item in value]


def _config() -> dict[str, object]:
    return _mapping(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), CONFIG.name)


def _section(name: str) -> dict[str, dict[str, object]]:
    return {
        key: _mapping(value or {}, f"{name}.{key}")
        for key, value in _mapping(_config().get(name), name).items()
    }


def _pipelines() -> dict[str, dict[str, list[str]]]:
    service = _mapping(_config().get("service"), "service")
    pipelines = _mapping(service.get("pipelines"), "service.pipelines")
    return {
        name: {
            part: _strings(value, f"{name}.{part}")
            for part, value in _mapping(pipeline, name).items()
        }
        for name, pipeline in pipelines.items()
    }


def _pipeline_names() -> list[str]:
    """Pipeline IDs for parametrize, read at collection time (none if the config is
    missing, so that ``test_every_signal_has_a_pipeline`` reports it)."""
    return sorted(_pipelines()) if CONFIG.is_file() else []


def _signal(pipeline: str) -> str:
    """The signal of a pipeline ID such as ``logs`` or ``logs/loki``."""
    return pipeline.partition("/")[0]


def _component_type(component: str) -> str:
    """The type of a component ID such as ``filter/content``."""
    return component.partition("/")[0]


def _unquote(ottl: str) -> str:
    """The value of an OTTL string literal's content (``\\\\`` is one backslash)."""
    return re.sub(r"\\(.)", r"\1", ottl)


@dataclass
class Deletions:
    """What the statements of one OTTL context delete: exact keys and key patterns."""

    keys: set[str] = field(default_factory=set)
    patterns: list[re.Pattern[str]] = field(default_factory=list)

    def deletes(self, key: str) -> bool:
        """Whether ``key`` is deleted (Go's ``MatchString`` finds a match anywhere)."""
        return key in self.keys or any(pattern.search(key) for pattern in self.patterns)


def _deletions(processor: str, signal: str) -> dict[str, Deletions]:
    """The deletions of a transform processor for ``signal``, per OTTL context. Any
    statement that is not a plain ``delete_key`` or ``delete_matching_keys`` on the
    attributes of its group's context fails the test, so the parser stays in step with
    the config."""
    groups = _section("processors")[processor].get(STATEMENTS[signal]) or []
    assert isinstance(groups, list), f"{processor} {STATEMENTS[signal]}"
    found: dict[str, Deletions] = {}
    for group in groups:
        mapping = _mapping(group, f"{processor} {STATEMENTS[signal]} entry")
        context = str(mapping.get("context"))
        deletions = found.setdefault(context, Deletions())
        for statement in _strings(mapping.get("statements"), f"{processor} {context}"):
            if (match := DELETE_KEY.fullmatch(statement)) and match["context"] == context:
                deletions.keys.add(match["key"])
            elif (match := DELETE_MATCHING.fullmatch(statement)) and match["context"] == context:
                deletions.patterns.append(re.compile(_unquote(match["pattern"])))
            else:
                pytest.fail(f"{processor} {context}: unexpected statement {statement!r}")
    return found


def _fixture_keys() -> set[str]:
    """Every attribute key in the OTLP fixtures."""

    def walk(node: object) -> Iterator[str]:
        if isinstance(node, dict):
            if isinstance(node.get("key"), str) and "value" in node:
                yield str(node["key"])
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    keys: set[str] = set()
    for path in sorted((DEFAULT_FIXTURES / "otlp").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                keys.update(walk(json.loads(line)))
    return keys


def _conditions(signal: str) -> list[str]:
    """The ``log_conditions`` or ``trace_conditions`` of ``filter/content``: the filter
    processor's current layout (0.146.0 and later), with context-prefixed paths."""
    key = f"{signal}_conditions"
    return _strings(_section("processors")[CONTENT_FILTER].get(key), f"{CONTENT_FILTER} {key}")


PIPELINES: Final = _pipeline_names()


# Pipelines.


def test_every_signal_has_a_pipeline() -> None:
    assert {_signal(name) for name in _pipelines()} == {"metrics", "logs", "traces"}


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_memory_limiter_is_first_in_every_pipeline(pipeline: str) -> None:
    assert _pipelines()[pipeline]["processors"][0] == "memory_limiter"


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_identity_is_deleted_in_every_pipeline(pipeline: str) -> None:
    assert IDENTITY in _pipelines()[pipeline]["processors"]


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_content_processors_are_in_the_pipelines_that_carry_content(pipeline: str) -> None:
    processors = _pipelines()[pipeline]["processors"]
    assert CONTENT_TRANSFORM in processors
    if _signal(pipeline) in ("logs", "traces"):
        assert CONTENT_FILTER in processors
    else:
        assert CONTENT_FILTER not in processors


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_privacy_processors_then_the_host_label_run_before_any_other_processor(
    pipeline: str,
) -> None:
    # memory_limiter, transform/identity, then the content processors where present
    # (filter first, so the transform skips dropped records), then the host label, so
    # that it is set on what the privacy processors leave, then anything else.
    processors = _pipelines()[pipeline]["processors"]
    content = [name for name in (CONTENT_FILTER, CONTENT_TRANSFORM) if name in processors]
    expected = ["memory_limiter", IDENTITY, *content, HOST_PROCESSOR]
    assert processors[: len(expected)] == expected
    assert processors.count(HOST_PROCESSOR) == 1


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_pipelines_only_use_defined_components(pipeline: str) -> None:
    parts = _pipelines()[pipeline]
    for part in ("receivers", "processors", "exporters"):
        assert set(parts[part]) <= set(_section(part)), f"{pipeline}.{part}"


def test_memory_limiter_is_percentage_based() -> None:
    limiter = _section("processors")["memory_limiter"]
    assert isinstance(limiter.get("limit_percentage"), int)
    assert isinstance(limiter.get("spike_limit_percentage"), int)
    assert "limit_mib" not in limiter
    assert "spike_limit_mib" not in limiter
    assert limiter.get("check_interval")


def test_no_exporter_prints_telemetry() -> None:
    types = {_component_type(name) for name in _section("exporters")}
    assert not types & FORBIDDEN_EXPORTERS


@pytest.mark.parametrize("processor", [IDENTITY, CONTENT_FILTER, CONTENT_TRANSFORM])
def test_privacy_processors_fail_closed(processor: str) -> None:
    # On a statement error the batch is refused, never passed on unfiltered.
    assert _section("processors")[processor].get("error_mode") == "propagate"


# Exporters.


def _loki_exporter() -> dict[str, object]:
    exporters = _section("exporters")
    assert LOKI_EXPORTER in exporters, f"no {LOKI_EXPORTER} exporter"
    return exporters[LOKI_EXPORTER]


def test_logs_go_to_loki_through_its_otlp_endpoint() -> None:
    logs = [name for name in PIPELINES if _signal(name) == "logs"]
    assert logs == ["logs"]
    assert _pipelines()["logs"]["exporters"] == [LOKI_EXPORTER]
    exporter = _loki_exporter()
    assert exporter.get("endpoint") == LOKI_ENDPOINT
    # The default logs path (<endpoint>/v1/logs) is Loki's /otlp/v1/logs.
    assert "logs_endpoint" not in exporter


def test_loki_endpoint_matches_the_compose_service_and_its_port() -> None:
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    assert "loki" in _mapping(compose["services"], "services")
    loki = _mapping(yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8")), "loki config")
    port = _mapping(loki.get("server"), "server").get("http_listen_port")
    assert f"http://loki:{port}/otlp" == LOKI_ENDPOINT


def test_loki_exporter_is_only_in_logs_pipelines() -> None:
    # Loki's OTLP endpoint only accepts logs.
    for name, pipeline in _pipelines().items():
        if _signal(name) != "logs":
            assert LOKI_EXPORTER not in pipeline["exporters"], name


def _prometheus_exporter() -> dict[str, object]:
    exporters = _section("exporters")
    assert PROMETHEUS_EXPORTER in exporters, f"no {PROMETHEUS_EXPORTER} exporter"
    return exporters[PROMETHEUS_EXPORTER]


def test_metrics_go_to_prometheus_through_its_otlp_receiver() -> None:
    metrics = [name for name in PIPELINES if _signal(name) == "metrics"]
    assert metrics == ["metrics"]
    assert _pipelines()["metrics"]["exporters"] == [PROMETHEUS_EXPORTER]
    exporter = _prometheus_exporter()
    assert exporter.get("metrics_endpoint") == PROMETHEUS_METRICS_ENDPOINT
    # With a plain endpoint the exporter would append /v1/metrics to it.
    assert "endpoint" not in exporter


def test_no_other_exporter_takes_metrics_to_prometheus() -> None:
    types = {_component_type(name) for name in _section("exporters")}
    assert not types & OTHER_PROMETHEUS_EXPORTERS


def test_prometheus_endpoint_matches_the_compose_service_and_its_port() -> None:
    endpoint = urlsplit(PROMETHEUS_METRICS_ENDPOINT)
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    services = _mapping(compose["services"], "services")
    assert endpoint.hostname in services
    service = _mapping(services[endpoint.hostname], str(endpoint.hostname))
    command = _strings(service.get("command"), "prometheus command")
    # Prometheus keeps its default address (:9090), and serves the OTLP receiver at
    # /api/v1/otlp/v1/metrics only with this flag.
    assert endpoint.port == 9090
    assert not [item for item in command if item.startswith("--web.listen-address")]
    assert "--web.enable-otlp-receiver" in command
    assert endpoint.path == "/api/v1/otlp/v1/metrics"


def test_prometheus_exporter_is_only_in_metrics_pipelines() -> None:
    # Prometheus's OTLP receiver only accepts metrics.
    for name, pipeline in _pipelines().items():
        if _signal(name) != "metrics":
            assert PROMETHEUS_EXPORTER not in pipeline["exporters"], name


def test_metrics_are_made_cumulative_after_the_privacy_processors() -> None:
    # Claude Code sends delta sums by default; Prometheus stores cumulative series. The
    # host label is set before, so each stream is identified with it.
    assert _pipelines()["metrics"]["processors"] == [
        "memory_limiter",
        IDENTITY,
        CONTENT_TRANSFORM,
        HOST_PROCESSOR,
        DELTA_TO_CUMULATIVE,
    ]


def test_delta_to_cumulative_keeps_streams_for_a_day() -> None:
    # max_streams keeps its default (no practical limit): at most 18 series per session.
    assert _section("processors")[DELTA_TO_CUMULATIVE] == {"max_stale": MAX_STALE}


def test_delta_to_cumulative_is_only_in_metrics_pipelines() -> None:
    for name, pipeline in _pipelines().items():
        if _signal(name) != "metrics":
            assert DELTA_TO_CUMULATIVE not in pipeline["processors"], name


def _tempo_exporter() -> dict[str, object]:
    exporters = _section("exporters")
    assert TEMPO_EXPORTER in exporters, f"no {TEMPO_EXPORTER} exporter"
    return exporters[TEMPO_EXPORTER]


def _seconds(duration: object) -> float:
    """A collector duration such as ``500ms``, ``30s`` or ``1m`` in seconds."""
    assert isinstance(duration, str), f"expected a duration, got {duration!r}"
    match = re.fullmatch(r"(\d+)(ms|s|m|h)", duration)
    assert match, f"unexpected duration {duration!r}"
    return int(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]


def test_traces_go_to_tempo_through_its_otlp_receiver() -> None:
    traces = [name for name in PIPELINES if _signal(name) == "traces"]
    assert traces == ["traces"]
    assert _pipelines()["traces"]["exporters"] == [TEMPO_EXPORTER]
    exporter = _tempo_exporter()
    assert exporter.get("endpoint") == TEMPO_ENDPOINT
    # The default traces path (<endpoint>/v1/traces) is Tempo's receiver path.
    assert "traces_endpoint" not in exporter


def test_tempo_endpoint_matches_the_compose_service_and_its_receiver() -> None:
    endpoint = urlsplit(TEMPO_ENDPOINT)
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    assert endpoint.hostname in _mapping(compose["services"], "services")
    tempo = _mapping(yaml.safe_load(TEMPO_CONFIG.read_text(encoding="utf-8")), "tempo config")
    distributor = _mapping(tempo.get("distributor"), "distributor")
    receivers = _mapping(distributor.get("receivers"), "distributor.receivers")
    protocols = _mapping(_mapping(receivers.get("otlp"), "otlp").get("protocols"), "protocols")
    http = _mapping(protocols.get("http"), "otlp.protocols.http")
    assert str(http.get("endpoint")).rsplit(":", 1)[-1] == str(endpoint.port)


def test_tempo_exporter_is_only_in_traces_pipelines() -> None:
    # Tempo only stores traces.
    for name, pipeline in _pipelines().items():
        if _signal(name) != "traces":
            assert TEMPO_EXPORTER not in pipeline["exporters"], name


def test_tempo_exporter_drops_traces_quickly_while_tempo_is_off() -> None:
    # Tempo is optional (the traces profile). When it is not running the name tempo does
    # not resolve, and traces must leave the collector soon instead of filling its memory.
    exporter = _tempo_exporter()
    queue = _mapping(exporter.get("sending_queue"), "sending_queue")
    size = queue.get("queue_size")
    assert isinstance(size, int)
    assert 0 < size <= MAX_TEMPO_QUEUE_REQUESTS
    retry = _mapping(exporter.get("retry_on_failure"), "retry_on_failure")
    max_elapsed = _seconds(retry.get("max_elapsed_time"))
    assert 0 < max_elapsed <= MAX_TEMPO_RETRY_SECONDS
    assert _seconds(retry.get("max_interval")) <= max_elapsed
    # A Tempo that accepts the connection and then hangs holds a consumer at most this
    # long per attempt.
    assert 0 < _seconds(exporter.get("timeout")) <= max_elapsed


def test_every_exporter_is_used_by_a_pipeline() -> None:
    # No leftover placeholder (the nop exporter held traces until Tempo arrived).
    used = {name for pipeline in _pipelines().values() for name in pipeline["exporters"]}
    assert set(_section("exporters")) == used
    assert "nop" not in {_component_type(name) for name in _section("exporters")}


def test_no_component_uses_a_deprecated_name() -> None:
    for section in ("receivers", "processors", "exporters", "extensions"):
        types = {_component_type(name) for name in _section(section)}
        assert not types & DEPRECATED_TYPES, f"{section}: {sorted(types & DEPRECATED_TYPES)}"


@pytest.mark.parametrize("name", BACKEND_EXPORTERS)
def test_backend_exporters_need_no_credentials_inside_the_backend_network(name: str) -> None:
    # Loki, Prometheus and Tempo run without authentication on the internal backend
    # network: plain HTTP, no headers, no authenticator and no TLS settings to keep in
    # step.
    exporter = _section("exporters").get(name)
    assert exporter is not None, f"no {name} exporter"
    assert not set(exporter) & {"headers", "auth", "tls"}


@pytest.mark.parametrize("name", BACKEND_EXPORTERS)
def test_backend_exporter_queues_and_retries_are_bounded(name: str) -> None:
    exporter = _section("exporters").get(name)
    assert exporter is not None, f"no {name} exporter"
    queue = _mapping(exporter.get("sending_queue"), "sending_queue")
    assert queue.get("enabled") is True
    size = queue.get("queue_size")
    assert isinstance(size, int)
    assert 0 < size <= MAX_QUEUE_REQUESTS
    # Requests, so the bound does not depend on how many records each one carries.
    assert queue.get("sizer", "requests") == "requests"
    # A full queue refuses new data instead of blocking the OTLP receiver, which would
    # make Claude Code's exports time out.
    assert queue.get("block_on_overflow", False) is False
    retry = _mapping(exporter.get("retry_on_failure"), "retry_on_failure")
    assert retry.get("enabled") is True
    # 0 would retry forever.
    max_elapsed = retry.get("max_elapsed_time")
    assert isinstance(max_elapsed, str)
    assert re.fullmatch(r"[1-9]\d*(s|m)", max_elapsed), max_elapsed


@pytest.mark.parametrize("name", BACKEND_EXPORTERS)
def test_backend_exporters_only_use_keys_that_take_effect(name: str) -> None:
    exporter = _section("exporters").get(name)
    assert exporter is not None, f"no {name} exporter"
    extra = sorted(set(exporter) - EXPORTER_KEYS)
    assert not extra, (
        f"{name}: {extra} not in EXPORTER_KEYS; the collector 0.162.0 ignores unknown keys "
        "here, so add one only after seeing it take effect"
    )
    retry = _mapping(exporter.get("retry_on_failure"), "retry_on_failure")
    extra = sorted(set(retry) - RETRY_KEYS)
    assert not extra, (
        f"{name}: retry_on_failure {extra} not in RETRY_KEYS; the collector 0.162.0 "
        "ignores unknown keys here, so add one only after seeing it take effect"
    )


@pytest.mark.parametrize("name", BACKEND_EXPORTERS)
def test_backend_exporters_have_one_consumer_per_queued_request(name: str) -> None:
    # The requests in flight count toward queue_size, so with one consumer per request
    # every request already has a consumer. At SIGTERM each exporter stops retrying: the
    # attempts in progress end (timeout at most) and the requests waiting for their next
    # retry are dropped at once. With fewer consumers, each request no consumer had
    # picked would get one attempt in turn, however many a down backend left queued.
    exporter = _section("exporters").get(name)
    assert exporter is not None, f"no {name} exporter"
    queue = _mapping(exporter.get("sending_queue"), "sending_queue")
    assert queue.get("queue_size") == QUEUE_REQUESTS[name]
    assert queue.get("num_consumers") == queue.get("queue_size")
    # Explicit: the stop's bound, and the longest a backend that accepts the connection
    # and then hangs holds a consumer.
    assert exporter.get("timeout") == ATTEMPT_TIMEOUT


def _grace_seconds(service: str) -> float:
    """The ``stop_grace_period`` of a compose service (shared settings merged in)."""
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    found = _mapping(_mapping(compose["services"], "services")[service], service)
    return _seconds(found.get("stop_grace_period"))


def _worst_stop_seconds(name: str) -> float:
    """How long the exporter ``name`` can take to stop while its backend is down:
    ceil(queue_size / num_consumers) rounds of attempts that fail (a name that does not
    resolve took about 4 s to fail on Docker Desktop; a backend that accepts the
    connection and then hangs takes the whole timeout), each cut at its timeout."""
    exporter = _section("exporters").get(name)
    assert exporter is not None, f"no {name} exporter"
    queue = _mapping(exporter.get("sending_queue"), "sending_queue")
    size, consumers = queue.get("queue_size"), queue.get("num_consumers")
    assert isinstance(size, int)
    assert isinstance(consumers, int)
    assert consumers > 0
    return math.ceil(size / consumers) * _seconds(exporter.get("timeout"))


def test_the_exporters_stop_well_within_the_collectors_grace_period() -> None:
    # ADR 0020. The collector stops its exporters one after another, so with every
    # backend down their waits add up; past the grace period Docker kills the collector
    # (exit 137). Today: 5 s each, 15 s for the three.
    worst = {name: _worst_stop_seconds(name) for name in BACKEND_EXPORTERS}
    assert sum(worst.values()) <= SHUTDOWN_SHARE * _grace_seconds(COLLECTOR_SERVICE), worst


# Receivers and health.


def test_receivers_bind_the_otlp_container_ports() -> None:
    protocols = _mapping(_section("receivers")["otlp"].get("protocols"), "otlp.protocols")
    grpc = _mapping(protocols.get("grpc"), "grpc")
    http = _mapping(protocols.get("http"), "http")
    assert grpc.get("endpoint") == "0.0.0.0:4317"
    assert http.get("endpoint") == "0.0.0.0:4318"


def test_internal_telemetry_is_served_for_prometheus_to_scrape() -> None:
    # One pull reader in Prometheus format, and no push (periodic) reader: nothing else
    # receives the collector's own metrics.
    service = _mapping(_config().get("service"), "service")
    telemetry = _mapping(service.get("telemetry"), "service.telemetry")
    metrics = _mapping(telemetry.get("metrics"), "service.telemetry.metrics")
    readers = metrics.get("readers")
    assert isinstance(readers, list), "service.telemetry.metrics.readers must be a list"
    assert len(readers) == 1, readers
    reader = _mapping(readers[0], "reader")
    assert set(reader) == {"pull"}
    exporter = _mapping(_mapping(reader["pull"], "pull").get("exporter"), "pull.exporter")
    assert set(exporter) == {"prometheus"}
    prometheus = _mapping(exporter["prometheus"], "pull.exporter.prometheus")
    assert f"{prometheus.get('host')}:{prometheus.get('port')}" == TELEMETRY_ADDRESS


def test_health_check_extension_answers_the_compose_healthcheck() -> None:
    extensions = _section("extensions")
    endpoint = str(extensions["health_check"].get("endpoint"))
    service = _mapping(_config().get("service"), "service")
    assert "health_check" in _strings(service.get("extensions"), "service.extensions")
    compose = _mapping(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")), "compose")
    collector = _mapping(_mapping(compose["services"], "services")["otel-collector"], "collector")
    test = _strings(_mapping(collector["healthcheck"], "healthcheck")["test"], "test")
    assert test[-1] == f"http://{endpoint}/"


# Nothing calls out (ADR 0006): the collector opens connections only to its exporters'
# backends.


def test_collector_exports_no_telemetry_of_its_own() -> None:
    # Only its metrics, which Prometheus pulls (above); its own traces or logs would be
    # pushed over OTLP to an endpoint of their own.
    service = _mapping(_config().get("service"), "service")
    assert set(_mapping(service.get("telemetry"), "service.telemetry")) == {"metrics"}


def test_the_only_receiver_is_otlp() -> None:
    # A pull receiver (prometheus, httpcheck, ...) would make the collector connect to
    # its targets.
    used = {name for pipeline in _pipelines().values() for name in pipeline["receivers"]}
    assert used == {"otlp"}


def test_the_only_extension_is_health_check() -> None:
    # Extensions run whatever the pipelines do; opamp, for one, connects to a management
    # server.
    service = _mapping(_config().get("service"), "service")
    assert _strings(service.get("extensions"), "service.extensions") == ["health_check"]


# Identity (always deleted).


@pytest.mark.parametrize(
    ("signal", "context"),
    [(signal, context) for signal, contexts in IDENTITY_CONTEXTS.items() for context in contexts],
)
def test_identity_canaries_are_deleted_at_every_level(signal: str, context: str) -> None:
    deletions = _deletions(IDENTITY, signal).get(context, Deletions())
    kept = [key for key in IDENTITY_CANARY_KEYS if not deletions.deletes(key)]
    assert not kept, f"{signal} {context} keeps {kept}"


@pytest.mark.parametrize(
    ("signal", "context"),
    [(signal, context) for signal, contexts in IDENTITY_CONTEXTS.items() for context in contexts],
)
def test_identity_rule_deletes_exactly_the_identity_keys(signal: str, context: str) -> None:
    deletions = _deletions(IDENTITY, signal)[context]
    corpus = _fixture_keys() | set(IDENTITY_CANARY_KEYS) | set(KEPT_KEYS)
    wrong = sorted(key for key in corpus if deletions.deletes(key) != is_identity_key(key))
    assert not wrong, f"{signal} {context}: the identity rule disagrees on {wrong}"


@pytest.mark.parametrize("signal", sorted(STATEMENTS))
def test_identity_rule_has_no_other_contexts(signal: str) -> None:
    assert set(_deletions(IDENTITY, signal)) == set(IDENTITY_CONTEXTS[signal])


# Content (deleted while content capture is off).


def test_content_attributes_are_deleted_exactly() -> None:
    expected = {
        "metrics": {"resource": {HOST_PATHS}, "datapoint": {HOST_PATHS}},
        "logs": {"resource": {HOST_PATHS}, "log": set(LOG_CONTENT_CANARY_KEYS)},
        "traces": {
            "resource": {HOST_PATHS},
            "span": {*SPAN_CONTENT_CANARY_KEYS, HOST_PATHS},
            "spanevent": {HOST_PATHS},
        },
    }
    for signal, contexts in expected.items():
        found = _deletions(CONTENT_TRANSFORM, signal)
        assert {context: deletions.keys for context, deletions in found.items()} == contexts
        assert not any(deletions.patterns for deletions in found.values()), signal


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_every_planted_canary_is_deleted_by_its_pipeline(pipeline: str) -> None:
    signal = _signal(pipeline)
    transforms = [
        name for name in _pipelines()[pipeline]["processors"] if name in PRIVACY_TRANSFORMS
    ]
    assert transforms == list(PRIVACY_TRANSFORMS), pipeline
    for context, keys in PLANTED[signal].items():
        levels = [_deletions(name, signal).get(context, Deletions()) for name in transforms]
        kept = [key for key in keys if not any(level.deletes(key) for level in levels)]
        assert not kept, f"{pipeline} {context} keeps {kept}"


def test_content_events_are_dropped_by_event_name_and_by_body() -> None:
    conditions = _conditions("log")
    for name in CONTENT_EVENT_NAMES:
        assert f'log.attributes["event.name"] == "{name}"' in conditions
        assert f'log.event_name == "{name}"' in conditions
        assert f'log.body == "claude_code.{name}"' in conditions


def test_content_filter_drops_nothing_else() -> None:
    for condition in _conditions("log"):
        match = (
            EVENT_NAME_CONDITION.fullmatch(condition)
            or EVENT_FIELD_CONDITION.fullmatch(condition)
            or BODY_CONDITION.fullmatch(condition)
        )
        assert match, f"unexpected log condition {condition!r}"
        assert match["name"] in CONTENT_EVENT_NAMES
    # Only the current layout: no deprecated logs.log_record or traces.spanevent, and
    # nothing that drops metrics, whole spans or other records.
    content = _section("processors")[CONTENT_FILTER]
    assert set(content) == {"error_mode", "log_conditions", "trace_conditions"}


def test_content_span_events_are_dropped() -> None:
    conditions = _conditions("trace")
    assert conditions == [f'spanevent.name == "{CONTENT_SPAN_EVENT}"']
    assert SPAN_EVENT_CONDITION.fullmatch(conditions[0])


@pytest.mark.parametrize("processor", [IDENTITY, CONTENT_TRANSFORM])
def test_metadata_attributes_survive(processor: str) -> None:
    for signal in STATEMENTS:
        for context, deletions in _deletions(processor, signal).items():
            deleted = [key for key in METADATA_KEYS if deletions.deletes(key)]
            assert not deleted, f"{processor} {signal} {context} deletes {deleted}"


# The host label (ADR 0022).


def _host_statements(signal: str) -> dict[str, list[str]]:
    """The statements of the host processor for ``signal``, per OTTL context."""
    processor = _section("processors").get(HOST_PROCESSOR)
    assert processor is not None, f"no {HOST_PROCESSOR} processor"
    groups = processor.get(STATEMENTS[signal])
    assert isinstance(groups, list), f"{HOST_PROCESSOR} {STATEMENTS[signal]}"
    found: dict[str, list[str]] = {}
    for group in groups:
        mapping = _mapping(group, f"{HOST_PROCESSOR} {STATEMENTS[signal]} entry")
        context = str(mapping.get("context"))
        assert context not in found, f"{HOST_PROCESSOR} {signal}: two {context} groups"
        found[context] = _strings(mapping.get("statements"), f"{HOST_PROCESSOR} {context}")
    return found


def test_host_processor_lets_data_through_on_a_statement_error() -> None:
    # Not a privacy filter: an error must not cost the batch, which goes on (with the
    # error logged) as the privacy processors left it.
    processor = _section("processors").get(HOST_PROCESSOR)
    assert processor is not None, f"no {HOST_PROCESSOR} processor"
    assert processor.get("error_mode") == "ignore"
    assert set(processor) == {"error_mode", *STATEMENTS.values()}


@pytest.mark.parametrize("signal", sorted(STATEMENTS))
def test_host_label_is_set_on_the_resource_and_deleted_everywhere_else(signal: str) -> None:
    # The resource's host comes from CCOBS_HOST_LABEL (local by default) and replaces any
    # value a client sent; a host on data points, log records, spans or span events,
    # where Claude Code copies OTEL_RESOURCE_ATTRIBUTES, is deleted, so the resource's
    # value is the only one. Nothing else: the host step never sets host.name, which
    # transform/identity deletes (ADR 0021).
    expected = {"resource": [HOST_SET]} | {
        context: [f'delete_key({context}.attributes, "{HOST_LABEL}")']
        for context in HOST_DELETE_CONTEXTS[signal]
    }
    assert _host_statements(signal) == expected
