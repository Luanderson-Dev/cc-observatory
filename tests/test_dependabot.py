"""Static checks of ``.github/dependabot.yml`` for the stack's images (ADRs 0020 and 0030).

Unit tests: they parse the file; nothing is fetched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
DEPENDABOT: Final = ROOT / ".github" / "dependabot.yml"
STACK: Final = ROOT / "stack"
COOLDOWN_DAYS: Final = 7
TARGET_BRANCH: Final = "develop"
# Images built locally and never pushed: no registry has them, so the docker-compose
# entry must not look them up.
LOCAL_IMAGES: Final = frozenset({"cc-observatory-otel-collector", "cc-observatory-app"})
MINOR_AND_MAJOR: Final = ["version-update:semver-minor", "version-update:semver-major"]


def _mapping(value: object, where: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{where} must be a mapping, got {value!r}"
    return {str(key): item for key, item in value.items()}


def _updates() -> list[dict[str, object]]:
    document = _mapping(yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8")), DEPENDABOT.name)
    updates = document.get("updates")
    assert isinstance(updates, list)
    return [_mapping(entry, "updates entry") for entry in updates]


def _entry(ecosystem: str, directory: str) -> dict[str, object]:
    found = [
        entry
        for entry in _updates()
        if entry.get("package-ecosystem") == ecosystem and entry.get("directory") == directory
    ]
    assert len(found) == 1, f"expected one {ecosystem} entry for {directory}, got {len(found)}"
    return found[0]


def _ignores(entry: dict[str, object]) -> dict[str, object]:
    ignore = entry.get("ignore") or []
    assert isinstance(ignore, list)
    return {
        str(_mapping(item, "ignore")["dependency-name"]): _mapping(item, "ignore").get(
            "update-types"
        )
        for item in ignore
    }


def _dockerfile_directories() -> list[str]:
    paths = STACK.rglob("Dockerfile")
    return sorted(f"/{path.parent.relative_to(ROOT).as_posix()}" for path in paths)


@pytest.mark.parametrize("directory", _dockerfile_directories())
def test_every_dockerfile_under_stack_has_a_docker_entry(directory: str) -> None:
    entry = _entry("docker", directory)
    assert entry.get("target-branch") == TARGET_BRANCH
    assert _mapping(entry.get("cooldown"), "cooldown") == {"default-days": COOLDOWN_DAYS}
    assert _mapping(entry.get("commit-message"), "commit-message").get("prefix") == "build"


def test_the_compose_entry_skips_the_local_images() -> None:
    ignores = _ignores(_entry("docker-compose", "/"))
    for name in LOCAL_IMAGES:
        assert name in ignores, f"docker-compose must ignore {name}"
        # No update types: every version is ignored.
        assert ignores[name] is None, name


def test_the_app_entry_keeps_python_and_uv_on_the_project_lines() -> None:
    # Python follows .python-version and requires-python, uv follows [tool.uv]
    # required-version and the uv_build bound: a new minor is moved by hand with them.
    ignores = _ignores(_entry("docker", "/stack/app"))
    assert ignores.get("python") == MINOR_AND_MAJOR
    assert ignores.get("ghcr.io/astral-sh/uv") == MINOR_AND_MAJOR
