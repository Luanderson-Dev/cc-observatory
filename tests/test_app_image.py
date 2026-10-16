"""Static checks of the app's image: ``stack/app/Dockerfile`` and the root
``.dockerignore`` (ADRs 0020 and 0030).

Unit tests: they parse the files; Docker is not needed. The image itself is built and run
by the integration tests (``docker compose up --build``) and by the CI ``stack`` job.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

import pytest

from ccobs.server.config import Settings

ROOT: Final = Path(__file__).resolve().parents[1]
DOCKERFILE: Final = ROOT / "stack" / "app" / "Dockerfile"
DOCKERIGNORE: Final = ROOT / ".dockerignore"
PYPROJECT: Final = ROOT / "pyproject.toml"
PYTHON_VERSION_FILE: Final = ROOT / ".python-version"

# name:tag@sha256:<index digest>, with an optional registry and path.
PINNED_IMAGE: Final = re.compile(
    r"^(?P<name>[a-z0-9]+(?:[._/-][a-z0-9]+)*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
    r"@sha256:[0-9a-f]{64}$"
)
PYTHON_REPOSITORY: Final = "python"
# python:<x.y.z>-slim-<Debian codename>.
PYTHON_TAG: Final = re.compile(r"^(?P<version>\d+\.\d+)\.\d+-slim-[a-z]+$")
UV_REPOSITORY: Final = "ghcr.io/astral-sh/uv"
UV_TAG: Final = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
# The settings the image fixes (compose never passes them; tests/test_compose.py).
LISTEN_HOST: Final = "0.0.0.0"  # noqa: S104 - all interfaces inside the container
COMMAND: Final = ["python", "-m", "ccobs.server"]
# What the build context must never carry: secrets, history, raw captures, local state.
EXCLUDED: Final = (
    ".env",
    ".env.local",
    ".git/HEAD",
    ".venv/bin/python",
    "tests/test_compose.py",
    "tools/capture/out/session.json",
    "docs/adr/README.md",
    ".github/workflows/ci.yml",
    "src/ccobs/__pycache__/cli.cpython-312.pyc",
    ".mypy_cache/x",
    ".ruff_cache/x",
    ".pytest_cache/x",
    "dist/x.whl",
    "build/x",
    "ccobs.db",
)
# What the build needs: the lockfile, the project metadata with the files the build
# backend reads (readme, license-files) and the package.
KEPT: Final = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    ".python-version",
    "src/ccobs/__init__.py",
    "src/ccobs/server/__main__.py",
    "src/ccobs/server/migrations/env.py",
)


def _instructions() -> list[tuple[str, str]]:
    """The Dockerfile's instructions, ``(KEYWORD, arguments)``, with comment lines dropped
    and continuation lines joined."""
    joined: list[str] = []
    pending = ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if pending and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        joined.append(pending + stripped)
        pending = ""
    assert not pending, "the Dockerfile ends with a continuation"
    result = []
    for instruction in joined:
        keyword, _, arguments = instruction.partition(" ")
        result.append((keyword.upper(), arguments.strip()))
    return result


def _arguments(keyword: str) -> list[str]:
    return [arguments for found, arguments in _instructions() if found == keyword]


def _from_images() -> list[str]:
    images = []
    for arguments in _arguments("FROM"):
        words = [word for word in arguments.split() if not word.startswith("--")]
        images.append(words[0])
    return images


def _pinned(image: str) -> re.Match[str]:
    match = PINNED_IMAGE.fullmatch(image)
    assert match, f"{image!r} is not name:tag@sha256:<digest>"
    return match


def _env() -> dict[str, str]:
    """Every ``ENV key=value`` pair (the legacy ``ENV key value`` form is refused)."""
    found: dict[str, str] = {}
    for arguments in _arguments("ENV"):
        for pair in arguments.split():
            key, equals, value = pair.partition("=")
            assert equals, f"ENV {arguments!r}: use key=value"
            found[key] = value
    return found


def _ignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _ignored(path: str) -> bool:
    """Whether ``.dockerignore`` leaves ``path`` out of the context: the last matching
    pattern wins, ``!`` re-includes, and a pattern also matches everything below it
    (Docker's rules, with ``**`` for any number of directories)."""
    ignored = False
    for pattern in _ignore_patterns():
        negated = pattern.startswith("!")
        body = pattern.removeprefix("!").strip("/")
        regex = re.escape(body).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
        regex = regex.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        if re.fullmatch(f"{regex}(?:/.*)?", path):
            ignored = not negated
    return ignored


# Base images.


def test_every_from_is_pinned_by_digest() -> None:
    images = _from_images()
    assert images, "the Dockerfile has no FROM"
    for image in images:
        assert _pinned(image)["tag"] != "latest"


def test_the_python_base_matches_the_project_python() -> None:
    expected = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    pythons = [_pinned(image) for image in _from_images() if image.startswith("python:")]
    assert pythons, "no python base image"
    for match in pythons:
        assert match["name"] == PYTHON_REPOSITORY
        tag = PYTHON_TAG.fullmatch(match["tag"])
        assert tag, f"python:{match['tag']} is not <x.y.z>-slim-<codename>"
        assert tag["version"] == expected, f"python:{match['tag']} is not {expected}"
    # The builder and the runtime use the same image, so the venv's interpreter links
    # resolve in the runtime.
    assert len({match[0] for match in pythons}) == 1


def test_uv_comes_from_its_pinned_image_within_the_project_range() -> None:
    # The same minor as [tool.uv] required-version (>=0.12.23,<0.13), which uv enforces.
    required = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"][
        "required-version"
    ]
    low = re.search(r">=(\d+)\.(\d+)\.(\d+)", required)
    high = re.search(r"<(\d+)\.(\d+)", required)
    assert low, required
    assert high, required
    uvs = [_pinned(image) for image in _from_images() if image.startswith(f"{UV_REPOSITORY}:")]
    assert len(uvs) == 1, "exactly one uv stage"
    tag = UV_TAG.fullmatch(uvs[0]["tag"])
    assert tag, f"uv:{uvs[0]['tag']} is not an exact version"
    version = tuple(int(part) for part in tag.groups())
    assert tuple(int(part) for part in low.groups()) <= version
    assert version[:2] < tuple(int(part) for part in high.groups())
    assert any(arguments.startswith("--from=uv ") for arguments in _arguments("COPY")), (
        "uv must be copied from the uv stage"
    )


# Build steps.


def test_no_mount_flag_since_the_classic_builder_rejects_it() -> None:
    # Compose falls back to the classic builder without Buildx (ADR 0020).
    for keyword, arguments in _instructions():
        assert "--mount" not in arguments, f"{keyword} {arguments}"
    assert not DOCKERFILE.read_text(encoding="utf-8").startswith("# syntax=")


def test_no_add_and_no_remote_sources() -> None:
    assert not _arguments("ADD"), "use COPY; ADD fetches URLs and unpacks archives"
    for arguments in _arguments("COPY"):
        assert "://" not in arguments, arguments


def test_the_project_is_installed_from_the_lockfile_without_dev_dependencies() -> None:
    syncs = [run for run in _arguments("RUN") if "uv sync" in run]
    assert syncs, "no uv sync"
    for run in syncs:
        for flag in ("--frozen", "--no-dev", "--extra server"):
            assert flag in run, f"{flag} missing in: {run}"
    # The dependencies first (cached across source changes), then the project itself,
    # installed as a package rather than linked to the source tree.
    assert "--no-install-project" in syncs[0]
    assert "--no-editable" in syncs[-1]


def test_no_secret_is_copied_or_set() -> None:
    for arguments in _arguments("COPY"):
        assert ".env" not in arguments, arguments
    for arguments in _arguments("ARG"):
        assert not re.search(r"(?i)secret|token|password|key", arguments), arguments
    for key in _env():
        assert not re.search(r"(?i)secret|token|password|webhook", key), key


# Runtime.


def test_runs_as_a_fixed_non_root_user() -> None:
    users = _arguments("USER")
    assert users, "no USER"
    user, _, group = users[-1].partition(":")
    assert user.isdigit(), f"USER {users[-1]}: use a numeric UID"
    assert int(user) >= 1000, f"USER {users[-1]} is a system or root UID"
    assert not group or (group.isdigit() and int(group) >= 1000), f"USER {users[-1]}"
    # The data directory exists in the image, owned by that user, so a new named volume
    # starts with the right owner (ADR 0020).
    data_dir = Settings.model_fields["data_dir"].default.as_posix()
    assert any(data_dir in run and user in run for run in _arguments("RUN")), (
        f"{data_dir} must be created for UID {user}"
    )


def test_the_app_listens_on_all_interfaces_in_the_container() -> None:
    # Loopback is the code's default; inside the container the app must listen on the
    # container's interface, or neither the published port nor Prometheus reaches it.
    env = _env()
    assert env.get("CCOBS_LISTEN_HOST") == LISTEN_HOST
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    # Nothing else of the app's settings: compose passes the rest.
    assert {key for key in env if key.startswith("CCOBS_")} == {"CCOBS_LISTEN_HOST"}


def test_expose_documents_the_app_and_metrics_ports() -> None:
    ports = {port for arguments in _arguments("EXPOSE") for port in arguments.split()}
    expected = {
        str(Settings.model_fields["port"].default),
        str(Settings.model_fields["metrics_port"].default),
    }
    assert ports == expected


def test_cmd_is_the_server_module_in_exec_form() -> None:
    # Exec form: python is PID 1 and receives Docker's SIGTERM, which uvicorn handles.
    commands = _arguments("CMD")
    assert len(commands) == 1
    assert commands[0].startswith("["), "CMD must be in exec form"
    assert re.findall(r'"([^"]*)"', commands[0]) == COMMAND
    assert not _arguments("ENTRYPOINT")


# The build context.


@pytest.mark.parametrize("path", EXCLUDED)
def test_dockerignore_leaves_out(path: str) -> None:
    assert _ignored(path), f".dockerignore must leave out {path}"


@pytest.mark.parametrize("path", KEPT)
def test_dockerignore_keeps(path: str) -> None:
    assert not _ignored(path), f".dockerignore must keep {path}"
