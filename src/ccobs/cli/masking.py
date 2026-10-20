"""The masked ``--dry-run`` diff shared by ``ccobs setup`` and ``ccobs uninstall``
(ADR 0037), and the error text both print.

The user's settings can hold API keys and credentials, so the diff shows only the keys
the command touches, and of those only what cc-observatory itself writes:

- a hook group appears in full only when it is identical to one the command writes;
  a group made only of handlers ``is_ccobs_handler`` recognizes appears as a
  placeholder (an older one can carry extra arguments, such as an Authorization
  header, or a token in its URL), any other group as ``YOUR_GROUP``;
- an ``env`` value appears only when it is one the command lets through; any other
  appears as ``YOUR_VALUE``.

Standard library only: the CLI stays light (tests/test_cli_light.py).
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from typing import Final, cast

from ccobs.domain.settings_merge import is_ccobs_handler
from ccobs.domain.telemetry_settings import TELEMETRY_VARIABLES, TRACES_VARIABLES

__all__ = [
    "MANAGED_VARIABLES",
    "YOUR_GROUP",
    "YOUR_VALUE",
    "masked_view_diff",
    "reason",
]

MANAGED_VARIABLES: Final = (*TELEMETRY_VARIABLES, *TRACES_VARIABLES)
YOUR_GROUP: Final = "<your hook group>"
YOUR_VALUE: Final = "<your value>"


def reason(error: OSError) -> str:
    """The reason of ``error`` without its file names: ``str(error)`` and
    ``error.filename`` can name a temporary file."""
    return error.strerror or type(error).__name__


def _identical(first: object, second: object) -> bool:
    # Equal and in the same key order ({"a": 1} == {"a": True} in Python).
    return json.dumps(first) == json.dumps(second)


def _group_view(group: Mapping[str, object], written: Sequence[object], recognized: str) -> object:
    """``group`` itself only when it is one in ``written``, else a placeholder. The
    shape (a group is an object with a "hooks" list) was checked by the merge."""
    if any(_identical(group, ours) for ours in written):
        return group
    handlers = cast(list[object], group["hooks"])
    if handlers and all(is_ccobs_handler(handler) for handler in handlers):
        return recognized
    return YOUR_GROUP


def _view(
    settings: Mapping[str, object],
    events: Mapping[str, Sequence[object]],
    shown_values: Mapping[str, frozenset[str]],
    recognized: str,
) -> dict[str, object]:
    view: dict[str, object] = {}
    block = settings.get("hooks")
    if isinstance(block, dict):
        hooks = {
            event: [_group_view(group, written, recognized) for group in block[event]]
            for event, written in events.items()
            if event in block
        }
        if hooks:
            view["hooks"] = hooks
    env = settings.get("env")
    if isinstance(env, dict):
        variables = {
            key: value
            if isinstance(value, str) and value in shown_values.get(key, frozenset())
            else YOUR_VALUE
            for key, value in env.items()
            if key in MANAGED_VARIABLES
        }
        if variables:
            view["env"] = variables
    return view


def masked_view_diff(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    events: Mapping[str, Sequence[object]],
    shown_values: Mapping[str, frozenset[str]],
    recognized: str,
) -> list[str]:
    """A unified diff of the indent-2 JSON views of ``before`` and ``after``.

    A view holds the hook ``events`` (each mapped to the groups shown in full) that
    the settings have, and the telemetry variables of their ``env``; a value is shown
    only when it is in ``shown_values[key]``. A group made only of recognized handlers
    shows as ``recognized``; nothing else from the file ever appears."""

    def lines(settings: Mapping[str, object]) -> list[str]:
        view = _view(settings, events, shown_values, recognized)
        return json.dumps(view, indent=2, ensure_ascii=False).splitlines()

    return list(
        difflib.unified_diff(
            lines(before),
            lines(after),
            fromfile="settings.json (before)",
            tofile="settings.json (after)",
            lineterm="",
        )
    )
