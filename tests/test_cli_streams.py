"""Tests for ``ccobs.cli.streams``: output never crashes on an unencodable character."""

from __future__ import annotations

import io

from ccobs.cli.streams import tolerate_unencodable


def test_a_text_wrapper_writes_backslash_escapes() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    tolerate_unencodable(stream)
    stream.write("caf\u00e9 \u8a2d")
    stream.flush()

    assert raw.getvalue() == b"caf\xe9 \\u8a2d"


def test_other_streams_are_left_alone() -> None:
    stream = io.StringIO()

    tolerate_unencodable(stream)
    stream.write("\u8a2d")

    assert stream.getvalue() == "\u8a2d"
