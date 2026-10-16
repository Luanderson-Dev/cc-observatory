"""The hook settings snippet in ``docs/app.md`` equals ``hook_settings`` (ADR 0025).

Until ``ccobs setup`` writes the hooks (milestone 4), users copy the snippet by hand, so
it must be exactly the block ``ccobs setup`` will write. The snippet is the first
``json`` code block after the ``hook-settings`` marker comment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

from ccobs.domain.hook_config import DEFAULT_HOOK_URL, hook_settings

APP_DOC: Final = Path(__file__).resolve().parent.parent / "docs" / "app.md"
MARKER: Final = "<!-- hook-settings:"
# A fenced json block (``~~~`` or backticks, indented or not) and its body.
_JSON_BLOCK: Final = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>~~~|```)json[ \t]*\n(?P<body>.*?)^(?P=indent)(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_POSIX_NULL: Final = '"/dev/null"'
_WINDOWS_NULL: Final = '"NUL"'


def _snippet() -> str:
    text = APP_DOC.read_text(encoding="utf-8")
    assert text.count(MARKER) == 1, "docs/app.md must hold exactly one hook-settings marker"
    match = _JSON_BLOCK.search(text, text.index(MARKER))
    assert match is not None, "no json code block after the hook-settings marker"
    return match["body"]


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
