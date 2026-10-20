"""The CLI and the domain stay light: no server-only dependency at import time (ADR 0008).

Server dependencies live in the ``server`` extra, which the app image installs. ``uvx
cc-observatory`` installs the base distribution only, so importing the CLI (and the
domain code it will share with the app) must not load any of them.
"""

import functools
import importlib.metadata
import json
import pkgutil
import re
import subprocess
import sys

import pytest

import ccobs.cli
import ccobs.domain

SERVER_PACKAGES = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "anyio",
    "pydantic-settings",
    "prometheus-client",
    "httpx",
    "jinja2",
)

SERVER_MODULES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "anyio",
    "pydantic",
    "pydantic_settings",
    "prometheus_client",
    "httpx",
    "jinja2",
    "markupsafe",
    "ccobs.server",
)


def _domain_modules() -> list[str]:
    return [
        info.name for info in pkgutil.walk_packages(ccobs.domain.__path__, prefix="ccobs.domain.")
    ]


def _cli_modules() -> list[str]:
    """Return the CLI package and every submodule, imported or not by ``ccobs.cli``."""
    return [
        "ccobs.cli",
        *(info.name for info in pkgutil.walk_packages(ccobs.cli.__path__, prefix="ccobs.cli.")),
    ]


# The CLI on its own, and the CLI plus every domain module (the M4 CLI imports the domain).
IMPORT_SETS = (
    (*_cli_modules(),),
    (*_cli_modules(), "ccobs.domain", *_domain_modules()),
)

# Top-level names outside sys.stdlib_module_names that the child may hold. Each one was
# observed in the child, locally or on a CI runner; `_distutils_hack` never showed up
# (setuptools is not installed), so it is not allowed.
ALLOWED_TOP_LEVEL = frozenset(
    {
        # The package under test.
        "ccobs",
        # The interpreter's own entry for the `-c` script; not listed as stdlib.
        "__main__",
        # Alias of `__main__` that multiprocessing registers when it is imported
        # (coverage imports it in the child).
        "__mp_main__",
        # uv's virtualenv hook, imported at startup by `_virtualenv.pth`.
        "_virtualenv",
        # pytest-cov measures subprocesses (`patch = ["subprocess"]`), so coverage starts
        # in the child through its `.pth` file; `$coverage.*` are the sys.modules keys it
        # uses for process-wide singletons.
        "coverage",
        "$coverage",
        # The interpreter installation's site hook, imported by `site` at startup when the
        # Python build ships one (the CI runners' Linux builds do); not a dependency.
        "sitecustomize",
    }
)

# Standard library modules generated per platform at build time (sysconfig's build data,
# such as `_sysconfigdata__linux_x86_64-linux-gnu` or `_sysconfigdata__darwin_darwin`),
# which `sys.stdlib_module_names` does not list.
STDLIB_GENERATED_PREFIX = "_sysconfigdata_"


@functools.cache
def _loaded_modules(imports: tuple[str, ...]) -> frozenset[str]:
    """Import ``imports`` in a fresh interpreter and return every name in its ``sys.modules``."""
    code = "\n".join(
        [
            "import importlib, json, sys",
            *(f"importlib.import_module({name!r})" for name in imports),
            "print(json.dumps(sorted(sys.modules)))",
        ]
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and generated code
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded: list[str] = json.loads(result.stdout)
    return frozenset(loaded)


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9._-]+", requirement)
    assert match is not None, requirement
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _requirements() -> list[str]:
    return importlib.metadata.requires("cc-observatory") or []


def _extra(requirement: str) -> str | None:
    """Return the extra named in the requirement's marker, or ``None`` for a base one."""
    match = re.search(r"""extra\s*==\s*["']?([A-Za-z0-9._-]*)""", requirement)
    return match.group(1) if match else None


def test_domain_has_modules_to_check() -> None:
    assert "ccobs.domain.telemetry" in _domain_modules()


@pytest.mark.parametrize("imports", IMPORT_SETS, ids=["cli", "cli-and-domain"])
def test_import_loads_only_the_stdlib_and_ccobs(imports: tuple[str, ...]) -> None:
    third_party = sorted(
        name
        for name in _loaded_modules(imports)
        if name.partition(".")[0] not in sys.stdlib_module_names
        and name.partition(".")[0] not in ALLOWED_TOP_LEVEL
        and not name.startswith(STDLIB_GENERATED_PREFIX)
    )

    assert third_party == []


@pytest.mark.parametrize("imports", IMPORT_SETS, ids=["cli", "cli-and-domain"])
def test_import_loads_no_server_module(imports: tuple[str, ...]) -> None:
    server = sorted(
        name
        for name in _loaded_modules(imports)
        if any(name == s or name.startswith(s + ".") for s in SERVER_MODULES)
    )

    assert server == []


def test_server_extra_is_declared() -> None:
    metadata = importlib.metadata.metadata("cc-observatory")

    assert "server" in (metadata.get_all("Provides-Extra") or [])


def test_server_extra_carries_the_server_packages() -> None:
    server = {_requirement_name(req) for req in _requirements() if _extra(req) == "server"}

    assert set(SERVER_PACKAGES) <= server


def test_base_requirements_have_no_server_package() -> None:
    base = {_requirement_name(req) for req in _requirements() if _extra(req) is None}

    assert base.isdisjoint(SERVER_PACKAGES)
