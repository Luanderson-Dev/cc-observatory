"""The web UI's static files and templates (ADRs 0004 and 0031).

Third-party files (htmx, the fonts) are vendored unmodified and listed in
``static/VENDOR.json`` with a checksum and a licence. Nothing the browser loads may come
from another origin, and our own JavaScript and templates never use JavaScript dialogs.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

ROOT: Final = Path(__file__).parents[2]
SERVER: Final = Path(str(resources.files("ccobs.server")))
STATIC: Final = SERVER / "static"
TEMPLATES: Final = SERVER / "templates"
VENDOR_JSON: Final = STATIC / "VENDOR.json"
APP_CSS: Final = STATIC / "css" / "app.css"
# Directories that hold third-party files, each of which needs a VENDOR.json entry.
VENDORED_DIRS: Final = ("vendor", "fonts")
LICENSES: Final = STATIC / "licenses"
SPDX: Final = {"0BSD", "OFL-1.1"}
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
URL: Final = re.compile(r"https?://[^\s\"'<>)]*", re.IGNORECASE)
# A protocol-relative URL (//cdn.example/x.js) loads from another origin too.
PROTOCOL_RELATIVE: Final = re.compile(r"""(?:src|href|action)\s*=\s*["']?//""", re.IGNORECASE)
# Text tokens and the backgrounds they sit on (WCAG 2.2 AA: 4.5:1 for body text).
TEXT_TOKENS: Final = ("--ink", "--ink-muted", "--waiting-text", "--working", "--error")
BACKGROUNDS: Final = ("--board", "--strip")
AA: Final = 4.5
INK_ON_STRIP: Final = 12.0
# XML namespace names are identifiers, never fetched (an inline SVG needs its xmlns).
NAMESPACES: Final = {"http://www.w3.org/2000/svg"}
DIALOG: Final = re.compile(r"\b(?:alert|confirm|prompt)\s*\(")
# htmx attributes the UI never uses: hx-confirm calls window.confirm, and hx-boost and
# hx-push-url turn pages into client-side navigation (ADR 0031).
FORBIDDEN_ATTRIBUTES: Final = ("hx-confirm", "hx-boost", "hx-push-url")
COLOUR_TOKENS: Final = (
    "--board",
    "--strip",
    "--ink",
    "--ink-muted",
    "--waiting",
    "--waiting-text",
    "--on-waiting",
    "--working",
    "--idle",
    "--ended",
    "--error",
)


def _files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


def _vendor() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(VENDOR_JSON.read_text(encoding="utf-8"))["files"]
    return entries


def _relative_to_root(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _relative(path: Path) -> str:
    return path.relative_to(STATIC).as_posix()


def _own_text_files() -> list[Path]:
    """Our own text files under static/ and templates/: not vendored, not licence texts."""
    vendored = [STATIC / name for name in VENDORED_DIRS]
    return [
        path
        for path in _files(STATIC) + _files(TEMPLATES)
        if path != VENDOR_JSON
        and not path.is_relative_to(LICENSES)
        and not any(path.is_relative_to(directory) for directory in vendored)
    ]


# VENDOR.json.


def test_every_vendored_file_is_listed() -> None:
    listed = {entry["path"] for entry in _vendor()}
    on_disk = {_relative(path) for name in VENDORED_DIRS for path in _files(STATIC / name)}
    assert on_disk
    assert listed == on_disk


@pytest.mark.parametrize("entry", _vendor(), ids=lambda entry: entry["path"])
def test_a_vendored_file_matches_its_entry(entry: dict[str, Any]) -> None:
    path = STATIC / entry["path"]
    assert SHA256.match(entry["sha256"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    assert entry["name"]
    assert entry["version"]
    assert entry["source"].startswith("https://")
    assert entry["license"] in SPDX
    license_file = STATIC / entry["license_file"]
    assert license_file.is_relative_to(LICENSES)
    assert license_file.is_file()


def test_every_license_file_is_used() -> None:
    used = {entry["license_file"] for entry in _vendor()}
    assert {_relative(path) for path in _files(LICENSES)} == used


def test_the_htmx_file_name_carries_its_version() -> None:
    (htmx,) = (entry for entry in _vendor() if entry["name"] == "htmx")
    assert htmx["path"] == f"vendor/htmx-{htmx['version']}.min.js"
    assert htmx["version"].startswith("2.")


def test_the_licence_texts_match_their_spdx_ids() -> None:
    for entry in _vendor():
        text = (STATIC / entry["license_file"]).read_text(encoding="utf-8")
        marker = {"0BSD": "Zero-Clause BSD", "OFL-1.1": "SIL OPEN FONT LICENSE Version 1.1"}
        assert marker[entry["license"]] in text, entry["path"]


def test_the_fonts_declare_no_reserved_font_name() -> None:
    """OFL 1.1 lists Reserved Font Names after the copyright line; none means a renamed
    file or a future subset stays legal. Recorded in ADR 0031."""
    for path in LICENSES.glob("*OFL*.txt"):
        header = path.read_text(encoding="utf-8").split("-----", 1)[0]
        assert "Reserved Font Name" not in header, path.name


# Nothing from another origin.


def test_no_static_file_or_template_points_to_another_origin() -> None:
    files = [path for path in _files(STATIC) + _files(TEMPLATES) if path.suffix != ".woff2"]
    checked = 0
    for path in files:
        if path == VENDOR_JSON or path.is_relative_to(LICENSES):
            continue
        text = path.read_text(encoding="utf-8")
        assert set(URL.findall(text)) <= NAMESPACES, path.name
        assert not PROTOCOL_RELATIVE.search(text), path.name
        checked += 1
    assert checked >= 2


def test_vendor_json_has_urls_only_in_its_source_fields() -> None:
    for entry in _vendor():
        for key, value in entry.items():
            if key != "source":
                assert not URL.search(str(value)), (entry["path"], key)


def test_the_stylesheet_loads_only_local_files() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert "@import" not in css
    references = re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css)
    assert references
    for reference in references:
        assert not re.match(r"^[a-z][a-z0-9+.-]*:|^//", reference, re.IGNORECASE), reference
        assert (APP_CSS.parent / reference).resolve().is_file(), reference


# No JavaScript dialogs, no htmx client-side navigation.


def test_own_scripts_and_templates_open_no_javascript_dialogs() -> None:
    for path in _own_text_files():
        if path.suffix in {".js", ".html", ".jinja", ".j2"}:
            assert not DIALOG.search(path.read_text(encoding="utf-8")), path.name


def test_own_files_use_no_forbidden_htmx_attributes() -> None:
    for path in _own_text_files():
        text = path.read_text(encoding="utf-8")
        for attribute in FORBIDDEN_ATTRIBUTES:
            assert attribute not in text, (path.name, attribute)


# The strict CSP (ADR 0034): no inline script or style, no event handler attributes, no
# eval in our own scripts.
INLINE_SCRIPT: Final = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE)
STYLE_ELEMENT: Final = re.compile(r"<style\b", re.IGNORECASE)
STYLE_ATTRIBUTE: Final = re.compile(r"\sstyle\s*=", re.IGNORECASE)
HANDLER_ATTRIBUTE: Final = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
HTMX_HANDLER: Final = re.compile(r"\shx-on", re.IGNORECASE)
EVAL: Final = re.compile(r"\beval\s*\(|\bnew\s+Function\b|\.innerHTML\b|document\.write")
TEMPLATE_SUFFIXES: Final = {".html", ".jinja", ".j2"}


def _templates() -> list[Path]:
    return [path for path in _files(TEMPLATES) if path.suffix in TEMPLATE_SUFFIXES]


def test_there_are_templates() -> None:
    names = {path.name for path in _templates()}
    assert {"base.html", "overview.html", "_board.html", "_strip.html"} <= names


@pytest.mark.parametrize(
    "scan", [INLINE_SCRIPT, STYLE_ELEMENT, STYLE_ATTRIBUTE, HANDLER_ATTRIBUTE, HTMX_HANDLER]
)
def test_templates_hold_nothing_the_csp_refuses(scan: re.Pattern[str]) -> None:
    for path in _templates():
        assert not scan.search(path.read_text(encoding="utf-8")), (path.name, scan.pattern)


def test_own_scripts_never_evaluate_strings() -> None:
    scripts = [path for path in _own_text_files() if path.suffix == ".js"]
    assert {path.name for path in scripts} >= {"app.js", "live.js", "notify.js", "theme.js"}
    for path in scripts:
        assert not EVAL.search(path.read_text(encoding="utf-8")), path.name


def test_the_csp_scans_catch_what_they_must() -> None:
    assert INLINE_SCRIPT.search("<script>alert(1)</script>")
    assert INLINE_SCRIPT.search('<script type="module">')
    assert not INLINE_SCRIPT.search('<script src="/static/js/app.js" defer>')
    assert STYLE_ELEMENT.search("<STYLE>")
    assert STYLE_ATTRIBUTE.search('<p style="color: red">')
    assert not STYLE_ATTRIBUTE.search('<link rel="stylesheet">')
    assert HANDLER_ATTRIBUTE.search('<img src="x" onerror="go()">')
    assert not HANDLER_ATTRIBUTE.search('<p class="online">')
    assert HTMX_HANDLER.search('<a hx-on:click="x">')
    assert EVAL.search("eval (code)")
    assert EVAL.search("el.innerHTML = x")
    assert not EVAL.search("medieval(x)")


def test_the_scans_catch_what_they_must() -> None:
    assert DIALOG.search("if (confirm ('x')) {}")
    assert DIALOG.search("window.alert(1)")
    assert not DIALOG.search("htmx:confirm")
    assert URL.findall("src='https://cdn.example/x.js'") == ["https://cdn.example/x.js"]
    assert set(URL.findall('<svg xmlns="http://www.w3.org/2000/svg">')) <= NAMESPACES
    assert PROTOCOL_RELATIVE.search('<script src="//cdn.example/x.js">')
    assert PROTOCOL_RELATIVE.search("<a href=//cdn.example/>")
    assert PROTOCOL_RELATIVE.search("<form action = '//evil.example/'>")
    assert not PROTOCOL_RELATIVE.search('<script src="/static/vendor/x.js">')


# The stylesheet's tokens.


def _blocks(css: str, selector: str) -> list[str]:
    """The rules whose whole selector, on a line of its own, is ``selector``: ``body``
    finds ``body { ... }`` and not ``.strip__body { ... }``."""
    return re.findall(r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)


def _declared(block: str) -> set[str]:
    return set(_values(block))


def _values(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block))


def _themes() -> dict[str, dict[str, str]]:
    """The light tokens, and the dark ones laid over them."""
    css = APP_CSS.read_text(encoding="utf-8")
    (light,) = _blocks(css, ":root")
    (dark,) = _blocks(css, ':root[data-theme="dark"]')
    return {"light": _values(light), "dark": _values(light) | _values(dark)}


def _luminance(colour: str) -> float:
    assert re.fullmatch(r"#[0-9a-f]{6}", colour), colour
    channels = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    red, green, blue = (
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_the_session_header_takes_its_band_and_tint_from_the_strip_classes() -> None:
    """``.session-head`` comes after ``.strip--<status>`` with the same specificity: a
    colour or background in it would hide the state's band (a waiting header in grey)."""
    css = APP_CSS.read_text(encoding="utf-8")
    # Every rule for the bare selector, the phone layout's included.
    heads = _blocks(css, ".session-head")
    properties = {name for head in heads for name in re.findall(r"(?m)^\s*([a-z-]+)\s*:", head)}
    assert len(heads) == 2
    assert {"border-left-width", "border-left-style"} <= properties
    for forbidden in ("border", "border-left", "border-left-color", "border-color"):
        assert forbidden not in properties, forbidden
    assert not any(name.startswith("background") for name in properties)
    # The base surface has no specificity, so strip--waiting_user's tint wins.
    (surface,) = _blocks(css, ":where(.session-head)")
    assert "background" in surface
    for status in ("waiting_user", "working", "idle", "ended", "stale"):
        assert any("border-left-color" in band for band in _blocks(css, f".strip--{status}"))
    assert css.index(".strip--waiting_user {") < css.index(".session-head {")


def test_the_alert_settings_numbers_are_in_the_ui_face_with_tabular_figures() -> None:
    # Mono is only for identifiers: a setting's number and its range are not.
    css = APP_CSS.read_text(encoding="utf-8")
    (field,) = _blocks(css, ".alert-settings__input")
    assert "font-family: var(--font-sans);" in field
    assert "font-variant-numeric: tabular-nums;" in field
    (error,) = _blocks(css, ".alert-settings__error")
    assert "font-family: var(--font-sans);" in error


def test_the_stylesheet_has_light_and_dark_colour_tokens() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    (light,) = _blocks(css, ":root")
    (media_dark,) = _blocks(css, ':root:not([data-theme="light"])')
    (forced_dark,) = _blocks(css, ':root[data-theme="dark"]')
    media = css.index("@media (prefers-color-scheme: dark)")
    assert media < css.index(':root:not([data-theme="light"])')
    # The manual choice comes last, so it wins over the system preference.
    assert css.index(':root:not([data-theme="light"])') < css.index(':root[data-theme="dark"]')
    for token in COLOUR_TOKENS:
        assert token in _declared(light), token
        assert token in _declared(media_dark), token
        assert token in _declared(forced_dark), token
    assert _values(media_dark) == _values(forced_dark)
    assert "color-scheme: light" in light
    assert "color-scheme: dark" in media_dark


def test_blocks_match_whole_selectors() -> None:
    css = "body { a: b; }\n.strip__body { c: d; }\n@media x {\n  body { e: f; }\n}\n"
    assert _blocks(css, "body") == [" a: b; ", " e: f; "]


def test_strips_are_flat_rows_with_a_band() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    # The base rule first; the media queries only change its grid.
    strip = _blocks(css, ".strip")[0]
    assert "border-left: var(--band-width) solid" in strip
    assert "border-radius: var(--radius-strip)" in strip
    # No shadows: only the focus ring stands out (the visual direction).
    assert "box-shadow" not in css
    for state in ("waiting_user", "working", "idle", "ended", "stale"):
        assert _blocks(css, f".strip--{state}"), state


def test_the_contrast_formula() -> None:
    assert _contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert _contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_reach_wcag_aa(theme: str) -> None:
    tokens = _themes()[theme]
    for text in TEXT_TOKENS:
        for background in BACKGROUNDS:
            ratio = _contrast(tokens[text], tokens[background])
            assert ratio >= AA, (theme, text, background, round(ratio, 2))
    assert _contrast(tokens["--on-waiting"], tokens["--waiting"]) >= AA
    assert _contrast(tokens["--ink"], tokens["--strip"]) >= INK_ON_STRIP


def test_the_stylesheet_declares_the_fonts_and_the_base() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    assert len(faces) == len([entry for entry in _vendor() if entry["path"].startswith("fonts/")])
    for face in faces:
        assert "font-display: swap" in face
        assert "local(" not in face, "only the bundled files, never a system font"
    assert "prefers-reduced-motion: reduce" in css
    assert ":focus-visible" in css
    (body,) = _blocks(css, "body")
    assert "background" in body
    assert "color:" in body


# Line endings and pre-commit leave vendored bytes alone.


def test_git_keeps_vendored_bytes_as_they_are() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for pattern in (
        "src/ccobs/server/static/vendor/** -text",
        "src/ccobs/server/static/licenses/** -text",
        "*.woff2 binary",
    ):
        assert pattern in attributes


def test_editors_leave_vendored_files_alone() -> None:
    editorconfig = (ROOT / ".editorconfig").read_text(encoding="utf-8")
    section = editorconfig.split("[src/ccobs/server/static/{vendor,licenses}/**]", 1)[1]
    section = section.split("\n[", 1)[0]
    for setting in ("end_of_line", "insert_final_newline", "trim_trailing_whitespace"):
        assert f"{setting} = unset" in section, setting


def test_the_distribution_declares_every_licence() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    licences = set(project["license"].split(" AND "))
    assert licences == {"MIT"} | {entry["license"] for entry in _vendor()}
    assert "src/ccobs/server/static/licenses/*" in project["license-files"]


def test_pre_commit_leaves_vendored_files_alone() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    sample = "src/ccobs/server/static/vendor/htmx-2.0.11.min.js"
    licence = "src/ccobs/server/static/licenses/atkinson-hyperlegible-next-OFL.txt"
    for hook_id in ("trailing-whitespace", "end-of-file-fixer", "mixed-line-ending"):
        exclude = re.compile(hooks[hook_id]["exclude"])
        assert exclude.search(sample), hook_id
        assert exclude.search(licence), hook_id
        assert not exclude.search("src/ccobs/server/static/css/app.css"), hook_id
    assert re.compile(hooks["mixed-line-ending"]["exclude"]).search("tools/x.cmd")


# Packaging.


def test_the_wheel_ships_the_static_files_and_templates(tmp_path: Path) -> None:
    """Every file under static/ (and templates/, once it exists) is in the wheel: uv_build
    packages every file of the module, Python or not."""
    uv = shutil.which("uv")
    assert uv is not None, "uv builds the wheel (pyproject.toml's build backend is uv_build)"
    subprocess.run(  # noqa: S603 - uv from PATH, fixed arguments
        [uv, "build", str(ROOT), "--wheel", "--offline", "--quiet", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        (metadata,) = (name for name in names if name.endswith(".dist-info/METADATA"))
        headers = archive.read(metadata).decode("utf-8")
    assert "License-Expression: MIT AND OFL-1.1 AND 0BSD" in headers
    for path in _files(LICENSES):
        assert f"License-File: {_relative_to_root(path)}" in headers
        assert any(name.endswith("/licenses/" + _relative_to_root(path)) for name in names)
    on_disk = {
        "ccobs/server/" + path.relative_to(SERVER).as_posix()
        for path in _files(STATIC) + _files(TEMPLATES)
    }
    assert "ccobs/server/static/VENDOR.json" in on_disk
    assert on_disk <= names
