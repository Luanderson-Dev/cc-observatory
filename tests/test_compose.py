"""Static checks of the compose stack (ADR 0001, ADR 0003, ADR 0020).

Unit tests: they parse ``compose.yaml``, ``compose.debug.yaml``, the Dockerfiles and the
configs under ``stack/`` and ``.env.example``; Docker is not needed. The rules are
parametrized over the services found, so they keep holding as later stages add
services; the explicit tests at the end pin what exists now.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest
import yaml

from ccobs.domain.telemetry import DEFAULT_HOST, HOST_VALUE_PATTERN
from ccobs.server.config import ENV_PREFIX, Settings

ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE: Final = ROOT / "compose.yaml"
COMPOSE_DEBUG: Final = ROOT / "compose.debug.yaml"
STACK: Final = ROOT / "stack"
ENV_EXAMPLE: Final = ROOT / ".env.example"

# name:tag@sha256:<index digest>, with an optional registry and path.
PINNED_IMAGE: Final = re.compile(
    r"^(?P<name>[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::\d+/[a-z0-9._/-]+)?)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
    r"@sha256:[0-9a-f]{64}$"
)
# The name of an image built locally from a Dockerfile in stack/: no registry, a tag.
LOCAL_IMAGE: Final = re.compile(r"^cc-observatory-[a-z0-9-]+:[A-Za-z0-9_][A-Za-z0-9_.-]*$")
FROM_LINE: Final = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)", re.I | re.M)
# ${VAR:-default}, the only interpolation form allowed in the compose files.
INTERPOLATION: Final = re.compile(r"(?<!\$)\$\{(?P<inner>[^}]*)\}")
WITH_DEFAULT: Final = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*):-(?P<default>[^}]+)$")
BARE_VARIABLE: Final = re.compile(r"(?<!\$)\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
# Variables in the configs under stack/: ${VAR}, ${VAR:-default} and the collector's
# ${env:VAR} and ${env:VAR:-default}.
CONFIG_VARIABLE: Final = re.compile(r"\$\{(?:env:)?(?P<name>[A-Z][A-Z0-9_]*)(?::-[^}]*)?\}")
CONFIG_DEFAULT: Final = re.compile(r"\$\{(?:env:)?(?P<name>[A-Z][A-Z0-9_]*):-(?P<default>[^}]*)\}")
ENV_ASSIGNMENT: Final = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

LOOPBACK: Final = "127.0.0.1"
# Services written in Go: they get GOMEMLIMIT so the runtime collects garbage before the
# container reaches mem_limit.
GO_SERVICES: Final = frozenset({"otel-collector", "prometheus", "loki", "tempo", "grafana"})
# Storage services: only on the internal network in compose.yaml, published only by the
# debug override.
BACKEND_ONLY: Final = frozenset({"prometheus", "loki", "tempo"})
LOG_OPTIONS: Final = {"max-size": "10m", "max-file": "3"}
# Above every daemon default seen (10 s upstream), so each service can shut down cleanly.
MIN_STOP_GRACE_SECONDS: Final = 10
# The pinned images are checked by repository and pin, not by version: Dependabot moves
# their tags and digests (ADR 0020), never their repositories.
# An exact version tag (x.y.z), with an optional v prefix and suffix such as -musl.
VERSION_TAG: Final = re.compile(r"^v?\d+\.\d+\.\d+(?:-[a-z0-9]+)*$")
LOKI_REPOSITORY: Final = "grafana/loki"
TEMPO_REPOSITORY: Final = "grafana/tempo"
GRAFANA_REPOSITORY: Final = "grafana/grafana"
# The services that publish ports in compose.yaml: OTLP for Claude Code, Grafana and the
# app for the browser, and the app's hook route for Claude Code (ADRs 0018 and 0030).
PUBLISHING_SERVICES: Final = frozenset({"otel-collector", "grafana", "app"})
# Optional services, by compose profile: Tempo, because tracing is a beta feature of
# Claude Code and off by default (ADR 0003).
OPTIONAL_SERVICES: Final = {"tempo": ["traces"]}
BUSYBOX_REPOSITORY: Final = "busybox"
CONTRIB_REPOSITORY: Final = "otel/opentelemetry-collector-contrib"
# Prometheus stays on the 3.13 LTS line (ADR 0013); dependabot.yml lets only its patch
# releases in.
PROMETHEUS_IMAGE: Final = re.compile(r"^prom/prometheus:v3\.13\.\d+@sha256:[0-9a-f]{64}$")
# The derived collector image is built locally and tagged with the contrib version of its
# Dockerfile; the collector config's header names the same version.
COLLECTOR_LOCAL_REPOSITORY: Final = "cc-observatory-otel-collector"
COLLECTOR_DOCKERFILE: Final = STACK / "otel-collector" / "Dockerfile"
COLLECTOR_CONFIG: Final = STACK / "otel-collector" / "config.yaml"
CONTRIB_MENTION: Final = re.compile(r"\bcontrib (?P<version>v?\d+(?:\.\d+)+)")
# The stack's host label (ADR 0022): the variable the collector reads, and the characters
# .env.example allows in its value.
HOST_VARIABLE: Final = "CCOBS_HOST_LABEL"
HOST_VALUE: Final = HOST_VALUE_PATTERN
# The services that read the host label: the collector (ADR 0022) and the app (ADR 0030).
HOST_LABEL_SERVICES: Final = frozenset({"otel-collector", "app"})
_UNITS: Final = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_GO_UNITS: Final = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


# Loading.


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _load(path: Path) -> dict[str, object]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


def _services(path: Path) -> dict[str, dict[str, object]]:
    services = _load(path).get("services") or {}
    return {
        name: _mapping(service or {}, f"{path.name} services.{name}")
        for name, service in _mapping(services, f"{path.name} services").items()
    }


def _list(value: object, where: str) -> list[object]:
    """A list (``None``: empty)."""
    if value is None:
        return []
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    return list(value)


def _strings(value: object, where: str) -> list[str]:
    assert isinstance(value, list), f"{where} must be a list, got {value!r}"
    assert all(isinstance(item, str) for item in value), f"{where} must hold strings"
    return [str(item) for item in value]


def _service_names(path: Path) -> list[str]:
    """Service names for parametrize, read at collection time (none if the file is
    missing, so that the explicit tests below report it)."""
    if not path.is_file():
        return []
    return sorted(_services(path))


def _networks(service: Mapping[str, object]) -> set[str]:
    networks = service.get("networks") or []
    if isinstance(networks, dict):
        return {str(name) for name in networks}
    return set(_strings(networks, "networks"))


def _dockerfiles() -> list[Path]:
    return sorted(STACK.rglob("Dockerfile")) if STACK.is_dir() else []


def _stack_configs() -> Iterator[Path]:
    for path in sorted(STACK.rglob("*")):
        if path.is_file() and path.name != "Dockerfile":
            yield path


def _env_example() -> dict[str, str]:
    """The variables .env.example assigns (comment lines ignored)."""
    assigned: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if match:
            assigned[match["name"]] = match["value"]
    return assigned


def _bytes(size: str) -> int:
    """A compose size (``256m``) in bytes."""
    match = re.fullmatch(r"(\d+)([bkmg])?", size.strip().lower())
    assert match, f"unexpected size {size!r}"
    return int(match[1]) * _UNITS[match[2] or "b"]


def _go_bytes(limit: str) -> int:
    """A GOMEMLIMIT value (``204MiB``) in bytes."""
    match = re.fullmatch(r"(\d+)(B|KiB|MiB|GiB)?", limit.strip())
    assert match, f"unexpected GOMEMLIMIT {limit!r}"
    return int(match[1]) * _GO_UNITS[match[2] or "B"]


def _environment(service: Mapping[str, object]) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        pairs = (str(item).partition("=") for item in environment)
        return {key: value for key, _, value in pairs}
    return {key: str(value) for key, value in _mapping(environment, "environment").items()}


def _pinned_tag(image: object, repository: str, where: str) -> str:
    """The tag of ``image``, which must be ``<repository>:<x.y.z>@sha256:<digest>``."""
    assert isinstance(image, str), f"{where} needs an image"
    match = PINNED_IMAGE.fullmatch(image)
    assert match, f"{where}: {image!r} is not name:tag@sha256:<digest>"
    assert match["name"] == repository, f"{where}: {image!r} is not from {repository}"
    assert VERSION_TAG.fullmatch(match["tag"]), f"{where}: {image!r} has no exact version tag"
    return match["tag"]


def _collector_base_images() -> list[str]:
    """The images of the collector Dockerfile's FROM lines, in order."""
    return FROM_LINE.findall(COLLECTOR_DOCKERFILE.read_text(encoding="utf-8"))


def _contrib_tag() -> str:
    """The contrib version the derived collector image is built from (the last FROM)."""
    images = _collector_base_images()
    assert images, f"{COLLECTOR_DOCKERFILE} has no FROM"
    return _pinned_tag(images[-1], CONTRIB_REPOSITORY, "collector Dockerfile, last FROM")


def _header(path: Path) -> str:
    """The comment lines at the top of ``path``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(itertools.takewhile(lambda line: line.startswith("#"), lines))


SERVICES: Final = _service_names(COMPOSE)
DEBUG_SERVICES: Final = _service_names(COMPOSE_DEBUG)


# Rules for every service in compose.yaml.


@pytest.mark.parametrize("name", SERVICES)
def test_service_image_is_pinned_or_built_locally(name: str) -> None:
    service = _services(COMPOSE)[name]
    image = service.get("image")
    assert isinstance(image, str), f"{name} needs an image"
    if "build" in service:
        # Built from a pinned Dockerfile (checked below), never pulled.
        assert service.get("pull_policy") == "build", f"{name} must set pull_policy: build"
        assert LOCAL_IMAGE.fullmatch(image), f"{name}: local image name {image!r}"
        assert not image.endswith(":latest")
    else:
        match = PINNED_IMAGE.fullmatch(image)
        assert match, f"{name}: {image!r} is not name:tag@sha256:<digest>"
        assert match["tag"] != "latest"


@pytest.mark.parametrize("name", SERVICES)
def test_service_has_a_memory_limit(name: str) -> None:
    limit = _services(COMPOSE)[name].get("mem_limit")
    assert isinstance(limit, str), f"{name} needs mem_limit"
    assert _bytes(limit) > 0


@pytest.mark.parametrize("name", SERVICES)
def test_service_has_a_healthcheck(name: str) -> None:
    healthcheck = _mapping(_services(COMPOSE)[name].get("healthcheck"), f"{name} healthcheck")
    assert healthcheck.get("disable") is not True
    test = _strings(healthcheck.get("test"), f"{name} healthcheck.test")
    assert test[0] in ("CMD", "CMD-SHELL")
    assert len(test) > 1


@pytest.mark.parametrize("name", SERVICES)
def test_service_restarts_unless_stopped(name: str) -> None:
    assert _services(COMPOSE)[name].get("restart") == "unless-stopped"


@pytest.mark.parametrize("name", SERVICES)
def test_service_has_an_explicit_stop_grace_period(name: str) -> None:
    # Without it the Docker daemon's default-stop-timeout applies (10 s upstream, about
    # 3 s measured on Docker Desktop 29.8), and a backend killed before it finishes
    # replays its write-ahead log at the next start.
    grace = _services(COMPOSE)[name].get("stop_grace_period")
    assert isinstance(grace, str), f"{name} needs stop_grace_period"
    match = re.fullmatch(r"(\d+)(s|m)", grace)
    assert match, f"{name}: stop_grace_period {grace!r}"
    assert int(match[1]) * {"s": 1, "m": 60}[match[2]] >= MIN_STOP_GRACE_SECONDS


@pytest.mark.parametrize("name", SERVICES)
def test_service_rotates_its_logs(name: str) -> None:
    logging = _mapping(_services(COMPOSE)[name].get("logging"), f"{name} logging")
    assert logging.get("driver") == "json-file"
    options = _mapping(logging.get("options"), f"{name} logging.options")
    # Quoted in YAML: compose rejects a number for max-file.
    assert {key: options.get(key) for key in LOG_OPTIONS} == LOG_OPTIONS


@pytest.mark.parametrize("name", SERVICES)
def test_service_drops_privileges(name: str) -> None:
    service = _services(COMPOSE)[name]
    security = _strings(service.get("security_opt"), f"{name} security_opt")
    assert "no-new-privileges:true" in security
    assert _strings(service.get("cap_drop"), f"{name} cap_drop") == ["ALL"]
    assert "cap_add" not in service
    assert service.get("privileged") is not True


@pytest.mark.parametrize("name", SERVICES)
def test_service_has_no_container_name(name: str) -> None:
    # A fixed name would stop isolated projects (such as ccobs-it) running side by side.
    assert "container_name" not in _services(COMPOSE)[name]


@pytest.mark.parametrize("name", SERVICES)
def test_service_mounts_config_read_only_and_data_on_named_volumes(name: str) -> None:
    service = _services(COMPOSE)[name]
    declared = set(_mapping(_load(COMPOSE).get("volumes") or {}, "volumes"))
    for entry in _list(service.get("volumes"), f"{name} volumes"):
        if isinstance(entry, str):
            source, _, rest = entry.partition(":")
            target, _, mode = rest.partition(":")
            kind = "bind" if source.startswith((".", "/", "~")) else "volume"
            read_only = "ro" in mode.split(",")
        else:
            mount = _mapping(entry, f"{name} volumes entry")
            kind, source = str(mount.get("type")), str(mount.get("source"))
            target, read_only = str(mount.get("target")), mount.get("read_only") is True
        assert target.startswith("/"), f"{name}: mount target {target!r}"
        if kind == "bind":
            assert read_only, f"{name}: bind mount {source} must be read-only"
            path = (ROOT / source).resolve()
            assert path.is_dir(), f"{name}: bind mount {source} must be a directory"
            assert path.is_relative_to(STACK), f"{name}: bind mount {source} not under stack/"
        else:
            assert kind == "volume", f"{name}: unexpected mount type {kind}"
            assert source in declared, f"{name}: {source} must be a declared named volume"


@pytest.mark.parametrize("name", sorted(GO_SERVICES.intersection(SERVICES)))
def test_go_service_sets_gomemlimit_below_mem_limit(name: str) -> None:
    service = _services(COMPOSE)[name]
    limit = _go_bytes(_environment(service)["GOMEMLIMIT"])
    ratio = limit / _bytes(str(service["mem_limit"]))
    assert 0.7 <= ratio <= 0.9, f"{name}: GOMEMLIMIT is {ratio:.0%} of mem_limit"


@pytest.mark.parametrize("name", sorted(set(SERVICES) - GO_SERVICES))
def test_only_go_services_set_gomemlimit(name: str) -> None:
    # GOMEMLIMIT tunes Go's garbage collector; anywhere else it does nothing and misleads.
    assert "GOMEMLIMIT" not in _environment(_services(COMPOSE)[name])


@pytest.mark.parametrize("name", sorted(BACKEND_ONLY.intersection(SERVICES)))
def test_storage_service_is_only_on_backend_and_publishes_nothing(name: str) -> None:
    service = _services(COMPOSE)[name]
    assert _networks(service) == {"backend"}
    assert "ports" not in service


@pytest.mark.parametrize("name", sorted({"tempo"}.intersection(SERVICES)))
def test_tempo_runs_only_in_the_traces_profile(name: str) -> None:
    assert _strings(_services(COMPOSE)[name].get("profiles"), "profiles") == ["traces"]


@pytest.mark.parametrize("name", SERVICES)
def test_only_the_publishing_services_publish_ports(name: str) -> None:
    # By default only OTLP, Grafana and the app listen on the host (ADRs 0018 and 0030);
    # a service that publishes a port joins edge to do so.
    service = _services(COMPOSE)[name]
    if name in PUBLISHING_SERVICES:
        assert service.get("ports"), f"{name} must publish its port"
        assert "edge" in _networks(service), f"{name} publishes a port, so it joins edge"
    else:
        assert "ports" not in service, f"{name} must not publish ports in compose.yaml"


# Rules for both compose files.


@pytest.mark.parametrize(
    ("path", "name"),
    [(COMPOSE, name) for name in SERVICES] + [(COMPOSE_DEBUG, name) for name in DEBUG_SERVICES],
)
def test_published_ports_bind_to_loopback(path: Path, name: str) -> None:
    for entry in _list(_services(path)[name].get("ports"), f"{path.name} {name} ports"):
        if isinstance(entry, dict):
            assert entry.get("host_ip") == LOOPBACK, f"{path.name} {name}: {entry}"
        else:
            assert str(entry).startswith(f"{LOOPBACK}:"), f"{path.name} {name}: {entry}"


@pytest.mark.parametrize(
    ("path", "name"),
    [(COMPOSE, name) for name in SERVICES] + [(COMPOSE_DEBUG, name) for name in DEBUG_SERVICES],
)
def test_service_shares_no_host_namespace(path: Path, name: str) -> None:
    # The host's network stack would bypass the loopback-only ports (ADR 0001); its PID
    # and IPC namespaces would expose the host's processes and shared memory.
    service = _services(path)[name]
    for key in ("network_mode", "pid", "ipc"):
        assert service.get(key) != "host", f"{path.name} {name}: {key}: host"


@pytest.mark.parametrize("name", DEBUG_SERVICES)
def test_debug_override_publishes_only_services_on_edge(name: str) -> None:
    # A service only on the internal backend network cannot publish ports.
    override = _services(COMPOSE_DEBUG)[name]
    if override.get("ports"):
        base = _services(COMPOSE).get(name, {})
        assert "edge" in _networks(base) | _networks(override), name


@pytest.mark.parametrize("path", [COMPOSE, COMPOSE_DEBUG], ids=lambda path: path.name)
def test_every_variable_has_a_default(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for match in INTERPOLATION.finditer(text):
        assert WITH_DEFAULT.fullmatch(match["inner"]), f"{path.name}: ${{{match['inner']}}}"
    assert not BARE_VARIABLE.findall(INTERPOLATION.sub("", text)), f"{path.name}: bare $VAR"


def _variables_in_use() -> set[str]:
    names: set[str] = set()
    for path in (COMPOSE, COMPOSE_DEBUG):
        text = path.read_text(encoding="utf-8")
        names.update(
            match["name"]
            for inner in INTERPOLATION.finditer(text)
            if (match := WITH_DEFAULT.fullmatch(inner["inner"]))
        )
    for path in _stack_configs():
        names.update(match["name"] for match in CONFIG_VARIABLE.finditer(path.read_text("utf-8")))
    return names


def test_env_example_lists_every_variable_in_use() -> None:
    missing = _variables_in_use() - set(_env_example())
    assert not missing, f".env.example misses {sorted(missing)}"


def test_env_example_defaults_match_compose() -> None:
    assigned = _env_example()
    for path in (COMPOSE, COMPOSE_DEBUG):
        for inner in INTERPOLATION.finditer(path.read_text(encoding="utf-8")):
            match = WITH_DEFAULT.fullmatch(inner["inner"])
            if match:
                assert assigned.get(match["name"]) == match["default"], match["name"]


def test_every_stack_config_variable_has_the_default_of_env_example() -> None:
    # The configs keep their own default, for a run outside compose (such as the CI
    # checks of each config); it must be the value compose and .env.example give, so
    # each default is one value wherever it is written.
    assigned = _env_example()
    found = 0
    for path in _stack_configs():
        text = path.read_text(encoding="utf-8")
        variables = [match[0] for match in CONFIG_VARIABLE.finditer(text)]
        defaults = list(CONFIG_DEFAULT.finditer(text))
        assert [match[0] for match in defaults] == variables, f"{path}: a variable has no default"
        for match in defaults:
            assert assigned.get(match["name"]) == match["default"], f"{path}: {match[0]}"
        found += len(defaults)
    assert found


def test_env_example_works_as_an_env_file() -> None:
    # The integration tests pass it to compose with --env-file: only comments, blank
    # lines and NAME=value, with no quotes, spaces or inline comments in the value.
    for number, line in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = ENV_ASSIGNMENT.fullmatch(line)
        assert match, f".env.example:{number}: {line!r}"
        assert re.fullmatch(r"[^\s\"'#]+", match["value"]), f".env.example:{number}: {line!r}"


def test_env_example_says_dot_env_is_optional_and_never_committed() -> None:
    header = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert "optional" in header
    assert "never committed" in header


def test_gitignore_ignores_dot_env() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert "!.env.example" in lines


def test_backend_network_is_internal_and_edge_is_not() -> None:
    networks = _mapping(_load(COMPOSE).get("networks"), "networks")
    assert _mapping(networks.get("backend"), "networks.backend").get("internal") is True
    assert _mapping(networks.get("edge") or {}, "networks.edge").get("internal") is not True


@pytest.mark.parametrize("dockerfile", _dockerfiles(), ids=lambda path: path.parent.name)
def test_dockerfile_base_images_are_pinned(dockerfile: Path) -> None:
    images = FROM_LINE.findall(dockerfile.read_text(encoding="utf-8"))
    assert images, f"{dockerfile} has no FROM"
    for image in images:
        match = PINNED_IMAGE.fullmatch(image)
        assert match, f"{dockerfile}: {image!r} is not name:tag@sha256:<digest>"
        assert match["tag"] != "latest"


@pytest.mark.parametrize("name", [name for name in SERVICES if "build" in _services(COMPOSE)[name]])
def test_built_service_uses_a_dockerfile_under_stack(name: str) -> None:
    # Either the context is a directory under stack/ with its Dockerfile (the collector),
    # or the context is the repository root, which the app needs for its source, with a
    # Dockerfile under stack/ and the root .dockerignore keeping the rest out (ADR 0030).
    build = _services(COMPOSE)[name]["build"]
    settings = {"context": build} if isinstance(build, str) else _mapping(build, "build")
    assert set(settings) <= {"context", "dockerfile"}, f"{name}: build {settings}"
    context = (ROOT / str(settings["context"])).resolve()
    dockerfile = (context / str(settings.get("dockerfile", "Dockerfile"))).resolve()
    assert dockerfile.is_relative_to(STACK), f"{name}: {dockerfile} is not under stack/"
    assert dockerfile.name == "Dockerfile"
    assert dockerfile.is_file()
    if context == ROOT:
        assert (ROOT / ".dockerignore").is_file(), "a root build context needs .dockerignore"
    else:
        assert context.is_relative_to(STACK), f"{name}: build context {context}"


# What exists now (milestone 1, stage 10).


def test_compose_project_is_named_cc_observatory() -> None:
    assert _load(COMPOSE).get("name") == "cc-observatory"


def test_compose_defines_the_collector() -> None:
    assert "otel-collector" in SERVICES


def test_collector_is_built_from_the_pinned_upstream_image_plus_busybox() -> None:
    service = _services(COMPOSE)["otel-collector"]
    assert service.get("build") == "stack/otel-collector"
    images = _collector_base_images()
    # The busybox stage first, then the contrib image that the final image is built on.
    assert len(images) == 2, f"collector Dockerfile FROM lines: {images}"
    busybox = _pinned_tag(images[0], BUSYBOX_REPOSITORY, "collector Dockerfile, first FROM")
    # The musl build is one static binary, with no shared libraries to copy along.
    assert busybox.endswith("-musl"), f"busybox:{busybox} is not the musl build"
    _contrib_tag()
    dockerfile = COLLECTOR_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^COPY --from=busybox /bin/busybox /busybox$", dockerfile, re.M)


def test_collector_local_image_is_tagged_with_the_contrib_version() -> None:
    # Dependabot updates the Dockerfile but not this local name (dependabot.yml ignores
    # it), so its pull request fails here until the tag is changed by hand.
    contrib = _contrib_tag()
    expected = f"{COLLECTOR_LOCAL_REPOSITORY}:{contrib}"
    image = _services(COMPOSE)["otel-collector"].get("image")
    assert image == expected, (
        f"the collector Dockerfile builds on contrib {contrib}, but compose.yaml names the "
        f"image {image!r}: set `image: {expected}` in compose.yaml"
    )


def test_collector_config_header_names_the_contrib_version() -> None:
    contrib = _contrib_tag()
    header = _header(COLLECTOR_CONFIG)
    assert CONTRIB_MENTION.search(header), (
        "the header of stack/otel-collector/config.yaml must name the contrib version"
    )
    for match in CONTRIB_MENTION.finditer(header):
        assert match["version"] == contrib, (
            f"the collector Dockerfile builds on contrib {contrib}, but the header of "
            f"stack/otel-collector/config.yaml says {match[0]!r}: update it"
        )


def test_collector_reads_its_config_from_a_read_only_directory() -> None:
    service = _services(COMPOSE)["otel-collector"]
    assert service.get("volumes") == ["./stack/otel-collector:/etc/ccobs/otel-collector:ro"]
    assert service.get("command") == ["--config=/etc/ccobs/otel-collector/config.yaml"]
    assert (STACK / "otel-collector" / "config.yaml").is_file()


def test_collector_publishes_otlp_on_loopback_with_configurable_ports() -> None:
    assert _services(COMPOSE)["otel-collector"].get("ports") == [
        "127.0.0.1:${CCOBS_OTLP_GRPC_PORT:-4317}:4317",
        "127.0.0.1:${CCOBS_OTLP_HTTP_PORT:-4318}:4318",
    ]


def test_collector_joins_backend_and_edge() -> None:
    assert _networks(_services(COMPOSE)["otel-collector"]) == {"backend", "edge"}


def test_collector_healthcheck_uses_busybox_against_the_health_check_extension() -> None:
    healthcheck = _mapping(_services(COMPOSE)["otel-collector"]["healthcheck"], "healthcheck")
    assert healthcheck["test"] == [
        "CMD",
        "/busybox",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        "http://127.0.0.1:13133/",
    ]


def test_collector_memory_settings() -> None:
    service = _services(COMPOSE)["otel-collector"]
    assert service.get("mem_limit") == "256m"
    assert _environment(service).get("GOMEMLIMIT") == "204MiB"


@pytest.mark.parametrize("name", sorted(HOST_LABEL_SERVICES))
def test_the_host_label_reaches_the_collector_and_the_app_with_its_default(name: str) -> None:
    # The collector sets it on every signal (ADR 0022) and the app on its series and rows
    # (ADRs 0026, 0028 and 0030): local unless .env says otherwise, never the machine's
    # hostname, which is personal data (ADR 0021).
    environment = _environment(_services(COMPOSE)[name])
    assert environment.get(HOST_VARIABLE) == f"${{{HOST_VARIABLE}:-{DEFAULT_HOST}}}"
    assert _env_example().get(HOST_VARIABLE) == DEFAULT_HOST
    assert HOST_VALUE.fullmatch(DEFAULT_HOST)


def test_only_the_collector_and_the_app_read_the_host_label() -> None:
    readers = {
        name
        for name, service in _services(COMPOSE).items()
        if HOST_VARIABLE in _environment(service)
    }
    assert readers == HOST_LABEL_SERVICES


def test_env_example_documents_the_host_label() -> None:
    # What it is, that it is stored (so a hostname would be too) and what it may hold.
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    index = lines.index(f"{HOST_VARIABLE}={DEFAULT_HOST}")
    above = itertools.takewhile(lambda line: line.startswith("#"), reversed(lines[:index]))
    comment = " ".join(line.lstrip("# ") for line in reversed(list(above))).lower()
    for words in ("host label", "hostname", "letters", "digits"):
        assert words in comment, f"the comment above {HOST_VARIABLE} does not say {words!r}"


def test_compose_defines_loki_from_the_pinned_image() -> None:
    assert "loki" in SERVICES
    service = _services(COMPOSE)["loki"]
    _pinned_tag(service.get("image"), LOKI_REPOSITORY, "loki")
    assert "build" not in service


def test_loki_reads_its_config_read_only_and_keeps_data_in_a_named_volume() -> None:
    service = _services(COMPOSE)["loki"]
    assert service.get("volumes") == [
        "./stack/loki:/etc/ccobs/loki:ro",
        "loki-data:/loki",
    ]
    assert "loki-data" in _mapping(_load(COMPOSE).get("volumes"), "volumes")
    assert (STACK / "loki" / "config.yaml").is_file()


def test_loki_healthcheck_uses_its_own_health_flag() -> None:
    # The image has no shell or HTTP client; -health requests /ready on port 3100.
    healthcheck = _mapping(_services(COMPOSE)["loki"]["healthcheck"], "healthcheck")
    assert healthcheck["test"] == ["CMD", "/usr/bin/loki", "-health"]


def test_loki_memory_settings() -> None:
    service = _services(COMPOSE)["loki"]
    assert service.get("mem_limit") == "512m"
    assert _environment(service).get("GOMEMLIMIT") == "409MiB"


def test_debug_override_publishes_loki_on_loopback() -> None:
    override = _services(COMPOSE_DEBUG)["loki"]
    assert override.get("ports") == ["127.0.0.1:${CCOBS_LOKI_PORT:-4310}:3100"]
    assert _networks(override) == {"backend", "edge"}


def test_compose_defines_prometheus_from_the_pinned_image() -> None:
    assert "prometheus" in SERVICES
    service = _services(COMPOSE)["prometheus"]
    image = service.get("image")
    assert isinstance(image, str), "prometheus needs an image"
    assert PROMETHEUS_IMAGE.fullmatch(image), (
        f"prometheus: {image!r} is not prom/prometheus:v3.13.<patch>@sha256:<digest>; the "
        "stack stays on the 3.13 LTS line (ADR 0013)"
    )
    assert "build" not in service


def test_prometheus_reads_its_config_read_only_and_keeps_data_in_a_named_volume() -> None:
    service = _services(COMPOSE)["prometheus"]
    assert service.get("volumes") == [
        "./stack/prometheus:/etc/ccobs/prometheus:ro",
        "prometheus-data:/prometheus",
    ]
    assert "prometheus-data" in _mapping(_load(COMPOSE).get("volumes"), "volumes")
    assert (STACK / "prometheus" / "prometheus.yaml").is_file()


def test_prometheus_healthcheck_uses_busybox_wget_against_ready() -> None:
    # The image is busybox-based; /-/ready answers once Prometheus serves queries and
    # writes, after replaying its write-ahead log.
    healthcheck = _mapping(_services(COMPOSE)["prometheus"]["healthcheck"], "healthcheck")
    assert healthcheck["test"] == [
        "CMD",
        "/bin/wget",
        "-q",
        "-O",
        "/dev/null",
        "http://127.0.0.1:9090/-/ready",
    ]


def test_prometheus_memory_settings() -> None:
    service = _services(COMPOSE)["prometheus"]
    assert service.get("mem_limit") == "512m"
    assert _environment(service).get("GOMEMLIMIT") == "409MiB"


def test_debug_override_publishes_prometheus_on_loopback() -> None:
    override = _services(COMPOSE_DEBUG)["prometheus"]
    assert override.get("ports") == ["127.0.0.1:${CCOBS_PROMETHEUS_PORT:-4390}:9090"]
    assert _networks(override) == {"backend", "edge"}


def test_compose_defines_tempo_from_the_pinned_image() -> None:
    assert "tempo" in SERVICES
    service = _services(COMPOSE)["tempo"]
    _pinned_tag(service.get("image"), TEMPO_REPOSITORY, "tempo")
    assert "build" not in service


def test_tempo_reads_its_config_read_only_and_keeps_data_in_a_named_volume() -> None:
    # /var/tempo is the image's data directory, owned by its user (10001), so the named
    # volume starts with the right owner.
    service = _services(COMPOSE)["tempo"]
    assert service.get("volumes") == [
        "./stack/tempo:/etc/ccobs/tempo:ro",
        "tempo-data:/var/tempo",
    ]
    assert "tempo-data" in _mapping(_load(COMPOSE).get("volumes"), "volumes")
    assert (STACK / "tempo" / "config.yaml").is_file()


def test_tempo_healthcheck_uses_its_own_health_flag() -> None:
    # The image has no shell or HTTP client; -health requests /ready on port 3200.
    healthcheck = _mapping(_services(COMPOSE)["tempo"]["healthcheck"], "healthcheck")
    assert healthcheck["test"] == ["CMD", "/tempo", "-health"]


def test_tempo_memory_settings() -> None:
    service = _services(COMPOSE)["tempo"]
    assert service.get("mem_limit") == "512m"
    assert _environment(service).get("GOMEMLIMIT") == "409MiB"


def test_debug_override_publishes_tempo_on_loopback() -> None:
    # Only the HTTP API (queries, /status/config and /ready); the OTLP receiver stays
    # inside the backend network.
    override = _services(COMPOSE_DEBUG)["tempo"]
    assert override.get("ports") == ["127.0.0.1:${CCOBS_TEMPO_PORT:-4320}:3200"]
    assert _networks(override) == {"backend", "edge"}


def test_compose_defines_grafana_from_the_pinned_image() -> None:
    assert "grafana" in SERVICES
    service = _services(COMPOSE)["grafana"]
    _pinned_tag(service.get("image"), GRAFANA_REPOSITORY, "grafana")
    assert "build" not in service


def test_grafana_publishes_its_port_on_loopback_with_a_configurable_port() -> None:
    assert _services(COMPOSE)["grafana"].get("ports") == [
        "127.0.0.1:${CCOBS_GRAFANA_PORT:-4330}:3000"
    ]


def test_grafana_joins_backend_and_edge() -> None:
    # edge publishes its port; backend reaches Prometheus, Loki and Tempo (ADR 0018).
    assert _networks(_services(COMPOSE)["grafana"]) == {"backend", "edge"}


def test_grafana_reads_its_provisioning_read_only_and_keeps_data_in_a_named_volume() -> None:
    # /var/lib/grafana is the image's data directory, owned by its user (472), so the
    # named volume starts with the right owner.
    service = _services(COMPOSE)["grafana"]
    assert service.get("volumes") == [
        "./stack/grafana:/etc/ccobs/grafana:ro",
        "grafana-data:/var/lib/grafana",
    ]
    assert "grafana-data" in _mapping(_load(COMPOSE).get("volumes"), "volumes")
    assert (STACK / "grafana" / "provisioning" / "datasources" / "datasources.yaml").is_file()


def test_grafana_healthcheck_uses_curl_against_api_health() -> None:
    # The image has curl. /api/health answers 200 once the database is reachable, and,
    # unlike the other pages, it answers any Host header, so enforce_domain does not
    # turn it into a redirect (which curl -f would accept).
    healthcheck = _mapping(_services(COMPOSE)["grafana"]["healthcheck"], "healthcheck")
    assert healthcheck["test"] == [
        "CMD",
        "/usr/bin/curl",
        "-fsS",
        "-o",
        "/dev/null",
        "http://127.0.0.1:3000/api/health",
    ]


def test_grafana_memory_settings() -> None:
    # Measured in milestone 1: opening the UI takes the cgroup memory.peak to about
    # 600-630 MiB, the Prometheus, Loki and Tempo plugins (20-40 MiB for the three, outside
    # GOMEMLIMIT) included; at 384m, with all 13 bundled plugins, the limit was reached.
    service = _services(COMPOSE)["grafana"]
    assert service.get("mem_limit") == "768m"
    assert _environment(service).get("GOMEMLIMIT") == "614MiB"


def test_grafana_starts_without_waiting_for_the_backends() -> None:
    # Grafana queries its datasources only when a page asks, so it starts on its own and
    # reports a store that is down (Tempo without its profile) on the pages that use it.
    assert "depends_on" not in _services(COMPOSE)["grafana"]


def test_debug_override_leaves_grafana_alone() -> None:
    # Grafana is published by compose.yaml itself.
    assert "grafana" not in DEBUG_SERVICES


@pytest.mark.parametrize("name", SERVICES)
def test_only_the_optional_services_have_a_profile(name: str) -> None:
    # `docker compose up -d` starts every other service.
    profiles = _services(COMPOSE)[name].get("profiles")
    assert (_strings(profiles, "profiles") if profiles else None) == OPTIONAL_SERVICES.get(name)


@pytest.mark.parametrize("name", SERVICES)
def test_no_service_depends_on_an_optional_service(name: str) -> None:
    # Compose refuses to start a service whose dependency is in a profile that is off.
    depends_on = _services(COMPOSE)[name].get("depends_on") or []
    names = set(depends_on) if isinstance(depends_on, dict) else set(_strings(depends_on, name))
    assert not names & set(OPTIONAL_SERVICES), f"{name} depends on {names}"


def test_collector_starts_without_waiting_for_the_backends() -> None:
    # The collector is the OTLP entry point: it accepts telemetry at once and its
    # exporters queue and retry while Prometheus, Loki and Tempo start (ADR 0013,
    # ADR 0015), or drop traces while Tempo's profile is off.
    assert "depends_on" not in _services(COMPOSE)["otel-collector"]


def test_debug_override_is_a_valid_compose_mapping() -> None:
    document = _load(COMPOSE_DEBUG)
    keys = {key for key in document if not key.startswith("x-")}
    assert keys <= {"services", "networks", "volumes"}
    assert "name" not in document


# The app (milestone 2, stage 13; ADR 0030).

APP: Final = "app"
APP_DOCKERFILE: Final = "stack/app/Dockerfile"
APP_IMAGE: Final = "cc-observatory-app:local"
# The app's settings that the image fixes: compose never passes them, so the container's
# ports, address and data directory always match what the Dockerfile, the healthcheck,
# the port mappings, the scrape target and the volume expect.
APP_FIXED_VARIABLES: Final = frozenset(
    {"CCOBS_LISTEN_HOST", "CCOBS_PORT", "CCOBS_METRICS_PORT", "CCOBS_DATA_DIR"}
)
# The app's settings that compose derives from another variable instead of passing
# through: Grafana's URL is Grafana's root URL (ADR 0017), from CCOBS_GRAFANA_PORT, since
# Grafana redirects every other URL there, so .env does not offer it.
APP_DERIVED_VARIABLES: Final = {"CCOBS_GRAFANA_URL": "http://localhost:${CCOBS_GRAFANA_PORT:-4330}"}
APP_HEALTHCHECK: Final = [
    "CMD",
    "python",
    "-c",
    "import urllib.request as u; "
    "u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:4300/healthz', timeout=2)",
]


def _settings_variables() -> dict[str, str]:
    """Each Settings field's environment variable, by field name."""
    return {
        name: field.validation_alias
        if isinstance(field.validation_alias, str)
        else f"{ENV_PREFIX}{name.upper()}"
        for name, field in Settings.model_fields.items()
    }


def _app() -> dict[str, object]:
    return _services(COMPOSE)[APP]


def test_compose_defines_the_app_built_from_the_clone() -> None:
    # Built on the user's machine from the clone, under a local name that no registry
    # has; GHCR images come in milestone 7 (ADR 0008).
    service = _app()
    assert service.get("build") == {"context": ".", "dockerfile": APP_DOCKERFILE}
    assert service.get("image") == APP_IMAGE
    assert service.get("pull_policy") == "build"


def test_app_uses_the_shared_service_settings() -> None:
    # The x-service anchor: restart, stop_grace_period, privileges and log rotation.
    text = COMPOSE.read_text(encoding="utf-8")
    block = re.search(r"^  app:\n((?:    .*\n|[ \t]*\n)+)", text, re.M)
    assert block, "compose.yaml has no app service"
    assert re.search(r"^    <<: \*service$", block[1], re.M), "app must merge *service"
    assert _app().get("stop_grace_period") == "30s"


def test_app_publishes_its_port_on_loopback_with_a_configurable_port() -> None:
    port = Settings.model_fields["port"].default
    assert _app().get("ports") == [f"127.0.0.1:${{CCOBS_APP_PORT:-{port}}}:{port}"]


def test_app_joins_backend_and_edge() -> None:
    # backend reaches Loki, and Prometheus scrapes the app there; edge publishes its port
    # (ADR 0018).
    assert _networks(_app()) == {"backend", "edge"}


def test_app_keeps_its_database_in_a_named_volume() -> None:
    data_dir = Settings.model_fields["data_dir"].default
    assert _app().get("volumes") == [f"app-data:{data_dir.as_posix()}"]
    assert "app-data" in _mapping(_load(COMPOSE).get("volumes"), "volumes")


def test_app_memory_limit() -> None:
    assert _app().get("mem_limit") == "256m"


def test_app_healthcheck_requests_healthz_on_loopback_without_a_proxy() -> None:
    # /healthz is behind the Host guard, and urllib sends Host: 127.0.0.1:4300, which it
    # allows; no proxy from the environment, so a proxy Docker injects is never used.
    healthcheck = _mapping(_app()["healthcheck"], "healthcheck")
    assert healthcheck["test"] == APP_HEALTHCHECK
    collector = _mapping(_services(COMPOSE)["otel-collector"]["healthcheck"], "healthcheck")
    for key in ("interval", "timeout", "retries", "start_period", "start_interval"):
        assert healthcheck.get(key) == collector.get(key), key


def test_app_starts_without_waiting_for_loki() -> None:
    # Loki serves only the reconciler, which tolerates it being down (ADR 0029); hooks
    # must be accepted from the first second.
    assert "depends_on" not in _app()


def test_app_environment_passes_every_setting_but_the_fixed_ones() -> None:
    variables = set(_settings_variables().values())
    assert variables >= APP_FIXED_VARIABLES | set(APP_DERIVED_VARIABLES)
    passed = variables - APP_FIXED_VARIABLES - set(APP_DERIVED_VARIABLES)
    expected = {name: f"${{{name}:-" for name in passed}
    environment = _environment(_app())
    assert set(environment) == set(expected) | set(APP_DERIVED_VARIABLES)
    for name, prefix in expected.items():
        assert environment[name].startswith(prefix), f"{name}: {environment[name]}"
    for name, value in APP_DERIVED_VARIABLES.items():
        assert environment[name] == value, f"{name}: {environment[name]}"
    assigned = _env_example()
    for name in APP_FIXED_VARIABLES | set(APP_DERIVED_VARIABLES):
        assert name not in assigned, f".env.example must not offer {name}: compose ignores it"


def test_app_links_to_grafanas_root_url() -> None:
    # The UI's Grafana links start at Grafana's root URL, the one URL Grafana does not
    # redirect (ADR 0017), through the same port variable and default.
    root_url = _environment(_services(COMPOSE)["grafana"])["GF_SERVER_ROOT_URL"]
    assert _environment(_app())["CCOBS_GRAFANA_URL"] == root_url.removesuffix("/")


def test_app_compose_defaults_are_the_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The value compose passes when .env is silent gives the same Settings as no
    # variable at all, so the defaults of config.py, compose.yaml and .env.example agree.
    for name in [key for key in os.environ if key.upper().startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name)
    defaults = Settings().model_dump()
    environment = _environment(_app())
    for name, value in environment.items():
        assert INTERPOLATION.search(value), f"{name}: {value}"

        def default(interpolation: re.Match[str], name: str = name) -> str:
            match = WITH_DEFAULT.fullmatch(interpolation["inner"])
            assert match, f"{name}: {interpolation[0]}"
            return match["default"]

        monkeypatch.setenv(name, INTERPOLATION.sub(default, value))
    assert Settings().model_dump() == defaults


def test_debug_override_publishes_the_app_metrics_on_loopback() -> None:
    port = Settings.model_fields["metrics_port"].default
    override = _services(COMPOSE_DEBUG)[APP]
    assert override.get("ports") == [f"127.0.0.1:${{CCOBS_APP_METRICS_PORT:-4301}}:{port}"]
    assert set(override) == {"ports"}
