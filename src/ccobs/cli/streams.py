"""The CLI's output streams: never crash on a character the console cannot encode.

A Windows console or a redirected stream can use a legacy code page (cp1252, cp437),
and a path under the user's profile can hold characters it lacks; ``print`` would then
raise ``UnicodeEncodeError`` halfway through a report. Each command makes its streams
write such characters as backslash escapes instead.

Standard library only: the CLI stays light (tests/test_cli_light.py).
"""

from __future__ import annotations

import io
from typing import TextIO

__all__ = ["tolerate_unencodable"]


def tolerate_unencodable(*streams: TextIO) -> None:
    """Make every ``io.TextIOWrapper`` in ``streams`` write the characters its encoding
    lacks as backslash escapes; other streams (``io.StringIO``) are left alone."""
    for stream in streams:
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
