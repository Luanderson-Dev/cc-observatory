"""The settings snippets users copy by hand equal the blocks ``ccobs setup`` writes.

- The hook settings snippet in ``docs/app.md`` equals ``hook_settings`` (ADR 0025): the
  first ``json`` code block after the ``hook-settings`` marker comment.
- The telemetry ``env`` snippet in ``README.md`` and ``README.pt-BR.md`` equals
  ``{"env": telemetry_env()}``: the first ``json`` code block after the
  ``telemetry-env`` marker comment.

Until ``ccobs setup`` writes them (milestone 4), users copy the snippets by hand, so they
must be exactly the blocks ``ccobs setup`` will write, keys in the same order.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest

from ccobs.domain.hook_config import DEFAULT_HOOK_URL, hook_settings
from ccobs.domain.telemetry_settings import telemetry_env

ROOT: Final = Path(__file__).resolve().parent.parent
APP_DOC: Final = ROOT / "docs" / "app.md"
READMES: Final = (ROOT / "README.md", ROOT / "README.pt-BR.md")
MARKER: Final = "<!-- hook-settings:"
TELEMETRY_MARKER: Final = "<!-- telemetry-env:"
# A fenced json block (``~~~`` or backticks, indented or not) and its body.
_JSON_BLOCK: Final = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>~~~|```)json[ \t]*\n(?P<body>.*?)^(?P=indent)(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_POSIX_NULL: Final = '"/dev/null"'
_WINDOWS_NULL: Final = '"NUL"'


def _block_after(path: Path, marker: str) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.count(marker) == 1, f"{path.name} must hold exactly one {marker!r} marker"
    match = _JSON_BLOCK.search(text, text.index(marker))
    assert match is not None, f"no json code block after the {marker!r} marker in {path.name}"
    return match["body"]


def _snippet() -> str:
    return _block_after(APP_DOC, MARKER)


def _ordered_keys(value: object) -> object:
    """The value with every object turned into its list of (key, value) pairs, so that
    a comparison also checks the order in which the keys are written."""
    if isinstance(value, dict):
        return [(key, _ordered_keys(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [_ordered_keys(item) for item in value]
    return value


def test_the_snippet_equals_hook_settings() -> None:
    snippet = json.loads(_snippet())
    expected = hook_settings(DEFAULT_HOOK_URL, windows=False)

    assert snippet == expected
    assert _ordered_keys(snippet) == _ordered_keys(expected)


def test_the_windows_variant_differs_only_in_the_null_device() -> None:
    # docs/app.md tells Windows users to change "/dev/null" to "NUL" and nothing else.
    snippet = _snippet()
    assert snippet.count(_POSIX_NULL) == 1

    windows = json.loads(snippet.replace(_POSIX_NULL, _WINDOWS_NULL))

    assert windows == hook_settings(DEFAULT_HOOK_URL, windows=True)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_readme_env_snippet_equals_telemetry_env(readme: Path) -> None:
    snippet = json.loads(_block_after(readme, TELEMETRY_MARKER))
    expected = {"env": telemetry_env()}

    assert snippet == expected
    assert _ordered_keys(snippet) == _ordered_keys(expected)
