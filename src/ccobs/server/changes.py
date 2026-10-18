"""What a commit changed, as the writer tells the live updates hub (ADR 0032).

Standard library only: the writer imports it, and must not pull in the web framework.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

__all__ = ["ALL", "Change", "Everything"]


class Everything(Enum):
    """A change to every session (retention deleted some), or too many to list."""

    ALL = "all"


ALL: Final = Everything.ALL

# The sessions a commit changed, or ALL.
type Change = frozenset[str] | Everything
