"""What a commit changed, as the writer tells the live updates hub (ADR 0032).

Standard library only: the writer imports it, and must not pull in the web framework.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

__all__ = ["ALERTS", "ALL", "AlertsChanged", "Change", "Everything", "HubEvent"]


class Everything(Enum):
    """A change to every session (retention deleted some), or too many to list."""

    ALL = "all"


class AlertsChanged(Enum):
    """An alert fired or cleared (ADR 0041): the alerts changed, not a session."""

    ALERTS = "alerts"


ALL: Final = Everything.ALL
ALERTS: Final = AlertsChanged.ALERTS

# The sessions a commit changed, or ALL.
type Change = frozenset[str] | Everything
# What the writer tells the hub after a commit: the sessions it changed, or that the
# alerts did.
type HubEvent = Change | AlertsChanged
