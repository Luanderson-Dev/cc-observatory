"""Tests for ``ccobs status`` (``ccobs.cli.status``, ADR 0039): a short, fast summary.

Every I/O dependency is injected through ``status.Environment``: the HTTP client, the
TCP connect, the environment and the platform. No test reaches the network or runs a
command. The autouse fixture points ``CLAUDE_CONFIG_DIR``, ``HOME`` and ``USERPROFILE``
at temporary directories, so no test touches the real ``~/.claude``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import pytest

import ccobs
from ccobs.cli import build_parser, doctor, main, status
from ccobs.cli.status import Environment, run_status
from ccobs.domain.doctor import HttpAnswer
from ccobs.domain.hook_config import hook_settings
from ccobs.domain.telemetry_settings import telemetry_env

ENDPOINT: Final = "http://127.0.0.1:4318"
APP: Final = "http://127.0.0.1:4300"
GRAFANA: Final = "http://127.0.0.1:4330"
DIAGNOSTICS: Final = f"{APP}/api/v1/diagnostics"
# A value planted in the user's files and environment, which no output may ever echo.
PLANTED: Final = "planted-value-0001"
VERSION: Final = ccobs.__version__


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Never let a test reach the real ~/.claude, nor use the default environment."""
    isolation = tmp_path_factory.mktemp("isolation")
    home = isolation / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))

    def no_default() -> Environment:
        raise AssertionError("tests inject the environment")

    monkeypatch.setattr(status, "default_environment", no_default)
    return home


_GOOD_FAILURES: Final[dict[str, object]] = {
    "available": True,
    "window_s": 3600,
    "executions": 12,
    "non_blocking_error": 0,
    "cancelled": 0,
}


def _diagnostics(**change: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": VERSION,
        "host": "local",
        "started_at_ms": 1,
        "sessions": {
            "by_state": {"working": 2, "waiting_user": 1, "idle": 0, "ended": 3, "stale": 0},
            "open_blocks": 1,
        },
        "hooks": {"received": {"Stop": 2}, "dropped": {"invalid": 1, "full": 0}},
        "hook_failures": dict(_GOOD_FAILURES),
    }
    document.update(change)
    return document


def _body(document: object) -> HttpAnswer:
    return HttpAnswer(200, json.dumps(document).encode())


@dataclass
class Fakes:
    """The injected dependencies, with the whole stack up by default."""

    platform: str = "linux"
    answers: dict[tuple[str, str], HttpAnswer] = field(
        default_factory=lambda: {
            ("GET", f"{APP}/healthz"): HttpAnswer(200),
            ("GET", DIAGNOSTICS): _body(_diagnostics()),
            ("GET", f"{GRAFANA}/api/health"): HttpAnswer(200),
        }
    )
    open_ports: set[int] = field(default_factory=lambda: {4318})
    environ: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str, bytes | None, float]] = field(default_factory=list)
    connects: list[tuple[str, int, float]] = field(default_factory=list)
    threads: set[int] = field(default_factory=set)

    def environment(self) -> Environment:
        def http(
            method: str, port: int, path: str, body: bytes | None, timeout: float
        ) -> HttpAnswer:
            url = f"http://127.0.0.1:{port}{path}"
            self.threads.add(threading.get_ident())
            self.requests.append((method, url, body, timeout))
            return self.answers.get((method, url), HttpAnswer(None))

        def connect(host: str, port: int, timeout: float) -> bool:
            self.threads.add(threading.get_ident())
            self.connects.append((host, port, timeout))
            return port in self.open_ports

        return Environment(http=http, connect=connect, environ=self.environ, platform=self.platform)

    def app_down(self) -> None:
        self.answers = {
            key: value for key, value in self.answers.items() if not key[1].startswith(APP)
        }


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str

    def json(self) -> dict[str, object]:
        document: dict[str, object] = json.loads(self.out)
        return document

    def line(self, prefix: str) -> str:
        found = [line for line in self.out.splitlines() if line.startswith(prefix)]
        assert len(found) == 1, self.out
        return found[0]


def _status(fakes: Fakes, config_dir: Path | None, *argv: str) -> Run:
    target = [] if config_dir is None else ["--config-dir", str(config_dir)]
    args = build_parser().parse_args(["status", *target, *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_status(args, env=fakes.environment(), out=out, err=err)
    return Run(code, out.getvalue(), err.getvalue())


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _complete(
    windows: bool = False, app: str = APP, env: Mapping[str, str | None] | None = None
) -> dict[str, object]:
    hooks = hook_settings(f"{app}/hooks", windows=windows)["hooks"]
    values: dict[str, str | None] = {**telemetry_env(ENDPOINT), **(env or {})}
    return {"hooks": hooks, "env": {k: v for k, v in values.items() if v is not None}}


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def config(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    _write_json(directory / "settings.json", _complete())
    return directory


def _settings_with(config: Path, settings: object) -> Path:
    _write_json(config / "settings.json", settings)
    return config


# The whole run.


def test_everything_up(fakes: Fakes, config: Path) -> None:
    run = _status(fakes, config)

    path = config / "settings.json"
    assert run.code == 0
    assert run.err == ""
    assert run.out == (
        f"cc-observatory {VERSION}\n"
        f"App       up    {APP}\n"
        "Collector up    127.0.0.1:4318 (OTLP/HTTP)\n"
        f"Grafana   up    {GRAFANA}\n"
        f"Claude Code settings: hooks installed, telemetry on ({path})\n"
        "Sessions: 2 working, 1 waiting on you, 0 idle, 3 ended, 0 stale; 1 open block\n"
        "Hook failures (last hour): 0 non-blocking errors, 0 cancelled (12 runs)\n"
        "Run `ccobs doctor` for details.\n"
    )


def test_the_probes_are_concurrent_on_loopback_with_short_timeouts(
    fakes: Fakes, config: Path
) -> None:
    _status(fakes, config)

    assert sorted(fakes.requests) == [
        ("GET", f"{APP}/api/v1/diagnostics", None, status.DIAGNOSTICS_TIMEOUT_S),
        ("GET", f"{APP}/healthz", None, doctor.HTTP_TIMEOUT_S),
        ("GET", f"{GRAFANA}/api/health", None, doctor.HTTP_TIMEOUT_S),
    ]
    assert fakes.connects == [("127.0.0.1", 4318, doctor.CONNECT_TIMEOUT_S)]
    assert threading.get_ident() not in fakes.threads
    # The diagnostics route waits up to 2 s on Loki itself (ADR 0038).
    assert 2.0 < status.DIAGNOSTICS_TIMEOUT_S <= 3.0


def test_the_port_flags_reach_every_probe(fakes: Fakes, config: Path) -> None:
    _status(fakes, config, "--app-port", "4400", "--otlp-port", "4319", "--grafana-port", "4331")

    assert {url for _, url, _, _ in fakes.requests} == {
        "http://127.0.0.1:4400/healthz",
        "http://127.0.0.1:4400/api/v1/diagnostics",
        "http://127.0.0.1:4331/api/health",
    }
    assert fakes.connects == [("127.0.0.1", 4319, doctor.CONNECT_TIMEOUT_S)]


def test_the_app_down_exits_1_with_unknown_lines(fakes: Fakes, config: Path) -> None:
    fakes.app_down()

    run = _status(fakes, config)

    assert run.code == 1
    assert run.line("App ") == f"App       down  {APP}"
    assert run.line("Sessions: ") == "Sessions: unknown (app down)"
    assert run.line("Hook failures") == "Hook failures (last hour): unknown (app down)"
    assert run.line("cc-observatory ") == f"cc-observatory {VERSION}"


def test_the_app_down_ignores_a_stray_diagnostics_answer(fakes: Fakes, config: Path) -> None:
    fakes.answers[("GET", f"{APP}/healthz")] = HttpAnswer(503)

    run = _status(fakes, config, "--json")

    document = run.json()
    assert run.code == 1
    assert document["app"] == {"up": False, "url": APP, "version": None}
    assert document["diagnostics"] == {"understood": False, "reason": "app_down"}
    assert document["sessions"] is None
    assert document["hook_failures"] is None


def test_the_collector_and_grafana_down_exit_0(fakes: Fakes, config: Path) -> None:
    fakes.open_ports.clear()
    del fakes.answers[("GET", f"{GRAFANA}/api/health")]

    run = _status(fakes, config)

    assert run.code == 0
    assert run.line("Collector ") == "Collector down  127.0.0.1:4318 (OTLP/HTTP)"
    assert run.line("Grafana ") == f"Grafana   down  {GRAFANA}"


# Diagnostics.


@pytest.mark.parametrize(
    ("reason", "shown"),
    [("timeout", "timeout"), ("unreachable", "unreachable"), ("<b>", "unknown"), (7, "unknown")],
)
def test_loki_unavailable(fakes: Fakes, config: Path, reason: object, shown: str) -> None:
    failures = {"available": False, "reason": reason, "window_s": 3600}
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(hook_failures=failures))

    run = _status(fakes, config, "--json")
    text = _status(fakes, config)

    assert run.code == text.code == 0
    assert run.json()["hook_failures"] == {"available": False, "reason": shown, "window_s": 3600}
    assert text.line("Hook failures") == (
        f"Hook failures (last hour): unknown (Loki unavailable: {shown})"
    )


def test_hook_failures_over_another_window(fakes: Fakes, config: Path) -> None:
    failures = {
        "available": True,
        "window_s": 600,
        "executions": 1,
        "non_blocking_error": 2,
        "cancelled": 3,
    }
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(hook_failures=failures))

    run = _status(fakes, config)

    assert run.line("Hook failures") == (
        "Hook failures (last 600 s): 2 non-blocking errors, 3 cancelled (1 run)"
    )


def test_open_blocks_plural(fakes: Fakes, config: Path) -> None:
    sessions = {"by_state": {"working": 1}, "open_blocks": 2}
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(sessions=sessions))

    run = _status(fakes, config)

    # A state the app did not list counts as 0.
    assert run.line("Sessions: ") == (
        "Sessions: 1 working, 0 waiting on you, 0 idle, 0 ended, 0 stale; 2 open blocks"
    )


def test_unknown_states_are_not_shown(fakes: Fakes, config: Path) -> None:
    sessions = {"by_state": {"working": 1, PLANTED: 5}, "open_blocks": 0}
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(sessions=sessions))

    run = _status(fakes, config, "--json")

    assert run.json()["sessions"] == {
        "by_state": {"working": 1, "waiting_user": 0, "idle": 0, "ended": 0, "stale": 0},
        "open_blocks": 0,
    }
    assert PLANTED not in run.out


NOT_UNDERSTOOD: Final = "unknown (the app's diagnostics were not understood)"


@pytest.mark.parametrize(
    ("answer", "shown", "reason"),
    [
        # Nothing answered: refused, reset, or no answer within the timeout.
        (HttpAnswer(None), "unknown (no answer from the diagnostics route)", "no_answer"),
        # The read gate is saturated (ADR 0033).
        (HttpAnswer(503, b'{"detail": "busy"}'), "unknown (app busy, try again)", "busy"),
        # An app older than this CLI has no such route.
        (
            HttpAnswer(404, b'{"detail": "Not Found"}'),
            "unknown (the app predates /api/v1/diagnostics; rebuild it)",
            "no_route",
        ),
        (HttpAnswer(500), NOT_UNDERSTOOD, "not_understood"),
        (HttpAnswer(302), NOT_UNDERSTOOD, "not_understood"),
        (HttpAnswer(200, b"not json"), NOT_UNDERSTOOD, "not_understood"),
        (_body(_diagnostics(version=4)), NOT_UNDERSTOOD, "not_understood"),
    ],
)
def test_diagnostics_unavailable_says_why(
    fakes: Fakes, config: Path, answer: HttpAnswer, shown: str, reason: str
) -> None:
    fakes.answers[("GET", DIAGNOSTICS)] = answer

    run = _status(fakes, config)
    as_json = _status(fakes, config, "--json")

    assert run.code == as_json.code == 0
    assert run.line("Sessions: ") == f"Sessions: {shown}"
    assert run.line("Hook failures") == f"Hook failures (last hour): {shown}"
    assert run.line("cc-observatory ") == f"cc-observatory {VERSION}"
    document = as_json.json()
    assert document["diagnostics"] == {"understood": False, "reason": reason}
    assert document["sessions"] is None
    assert document["hook_failures"] is None
    assert document["app"] == {"up": True, "url": APP, "version": None}


@pytest.mark.parametrize(
    "answer",
    [
        HttpAnswer(200, b"[" * 100_000),
        _body([]),
        _body(_diagnostics(sessions=[])),
        _body(_diagnostics(sessions={"by_state": [], "open_blocks": 0})),
        _body(_diagnostics(sessions={"by_state": {}, "open_blocks": -1})),
        _body(_diagnostics(sessions={"by_state": {}, "open_blocks": True})),
        _body(_diagnostics(sessions={"by_state": {"idle": "1"}, "open_blocks": 0})),
        _body(_diagnostics(hooks=None)),
        _body(_diagnostics(hook_failures=None)),
        _body(_diagnostics(hook_failures={"available": True, "window_s": "1"})),
        _body(_diagnostics(hook_failures={**_GOOD_FAILURES, "cancelled": None})),
        _body(_diagnostics(hook_failures={"available": "no", "window_s": 3600})),
    ],
)
def test_diagnostics_not_understood(fakes: Fakes, config: Path, answer: HttpAnswer) -> None:
    fakes.answers[("GET", DIAGNOSTICS)] = answer

    run = _status(fakes, config)
    as_json = _status(fakes, config, "--json")

    assert run.line("Sessions: ") == f"Sessions: {NOT_UNDERSTOOD}"
    assert run.line("Hook failures") == f"Hook failures (last hour): {NOT_UNDERSTOOD}"
    assert as_json.json()["diagnostics"] == {"understood": False, "reason": "not_understood"}


def test_a_version_mismatch_shows_the_app_version(fakes: Fakes, config: Path) -> None:
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(version="0.0.1"))

    run = _status(fakes, config)
    as_json = _status(fakes, config, "--json")

    assert run.line("cc-observatory ") == f"cc-observatory {VERSION} (app 0.0.1)"
    assert as_json.json()["app"] == {"up": True, "url": APP, "version": "0.0.1"}


def test_an_odd_app_version_is_not_echoed(fakes: Fakes, config: Path) -> None:
    fakes.answers[("GET", DIAGNOSTICS)] = _body(_diagnostics(version=f"<{PLANTED}>"))

    run = _status(fakes, config)
    as_json = _status(fakes, config, "--json")

    assert run.line("cc-observatory ") == f"cc-observatory {VERSION} (app unknown)"
    assert as_json.json()["app"] == {"up": True, "url": APP, "version": "unknown"}
    assert PLANTED not in run.out + as_json.out


# Settings.


def _settings_line(run: Run) -> str:
    return run.line("Claude Code settings: ")


def _settings(run: Run) -> object:
    return run.json()["settings"]


def test_hooks_installed_on_windows_are_the_curl_form(fakes: Fakes, config: Path) -> None:
    fakes.platform = "win32"
    _settings_with(config, _complete(windows=True))

    run = _status(fakes, config, "--json")

    assert _settings(run) == {
        "path": str(config / "settings.json"),
        "file": "ok",
        "hooks": "installed",
        "telemetry": "on",
    }


def test_hooks_missing(fakes: Fakes, config: Path) -> None:
    settings = _complete()
    del settings["hooks"]
    _settings_with(config, settings)

    run = _status(fakes, config)

    assert run.code == 0
    assert _settings_line(run).startswith("Claude Code settings: hooks missing, telemetry on (")


def test_hooks_for_another_port_are_outdated(fakes: Fakes, config: Path) -> None:
    _settings_with(config, _complete(app="http://127.0.0.1:4301"))

    run = _status(fakes, config)

    assert _settings_line(run).startswith("Claude Code settings: hooks outdated, telemetry on (")


@pytest.mark.parametrize(
    ("env", "state"),
    [
        ({"CLAUDE_CODE_ENABLE_TELEMETRY": None}, "off"),
        ({"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, "off"),
        ({"CLAUDE_CODE_ENABLE_TELEMETRY": "0", "OTEL_EXPORTER_OTLP_ENDPOINT": PLANTED}, "off"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": PLANTED}, "other endpoint"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": None}, "incomplete"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": None}, "incomplete"),
        ({"OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta"}, "on"),
    ],
)
def test_telemetry_states(
    fakes: Fakes, config: Path, env: dict[str, str | None], state: str
) -> None:
    _settings_with(config, _complete(env=env))

    run = _status(fakes, config)
    as_json = _status(fakes, config, "--json")

    assert _settings_line(run).startswith(
        f"Claude Code settings: hooks installed, telemetry {state} ("
    )
    assert _settings(as_json) == {
        "path": str(config / "settings.json"),
        "file": "ok",
        "hooks": "installed",
        "telemetry": state.replace(" ", "_"),
    }
    assert PLANTED not in run.out + as_json.out


def test_telemetry_on_another_otlp_port(fakes: Fakes, config: Path) -> None:
    run = _status(fakes, config, "--otlp-port", "4319")

    assert "telemetry other endpoint" in _settings_line(run)


def test_no_settings_file(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / "empty"

    run = _status(fakes, directory)
    as_json = _status(fakes, directory, "--json")

    path = directory / "settings.json"
    assert run.code == 0
    assert _settings_line(run) == (
        f"Claude Code settings: no settings file, hooks missing, telemetry off ({path})"
    )
    assert _settings(as_json) == {
        "path": str(path),
        "file": "missing",
        "hooks": "missing",
        "telemetry": "off",
    }


def test_unreadable_settings_never_show_content(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "settings.json").write_text('{"env": {"K": "' + PLANTED + '"}', encoding="utf-8")

    run = _status(fakes, directory)
    as_json = _status(fakes, directory, "--json")

    path = directory / "settings.json"
    assert run.code == 0
    assert _settings_line(run) == f"Claude Code settings: unreadable ({path})"
    assert _settings(as_json) == {
        "path": str(path),
        "file": "unreadable",
        "hooks": None,
        "telemetry": None,
    }
    assert PLANTED not in run.out + run.err + as_json.out + as_json.err


def test_the_config_dir_comes_from_the_environment(fakes: Fakes, config: Path) -> None:
    fakes.environ = {"CLAUDE_CONFIG_DIR": str(config)}

    run = _status(fakes, None, "--json")

    assert _settings(run) == {
        "path": str(config / "settings.json"),
        "file": "ok",
        "hooks": "installed",
        "telemetry": "on",
    }


# JSON.


def test_json_shape(fakes: Fakes, config: Path) -> None:
    run = _status(fakes, config, "--json")

    document = run.json()
    assert run.code == 0
    assert list(document) == [
        "version",
        "app",
        "collector",
        "grafana",
        "settings",
        "diagnostics",
        "sessions",
        "hook_failures",
    ]
    assert document["version"] == VERSION
    assert document["app"] == {"up": True, "url": APP, "version": VERSION}
    assert document["collector"] == {"up": True, "address": "127.0.0.1:4318"}
    assert document["grafana"] == {"up": True, "url": GRAFANA}
    assert document["diagnostics"] == {"understood": True, "reason": None}
    assert document["sessions"] == {
        "by_state": {"working": 2, "waiting_user": 1, "idle": 0, "ended": 3, "stale": 0},
        "open_blocks": 1,
    }
    assert document["hook_failures"] == {
        "available": True,
        "window_s": 3600,
        "executions": 12,
        "non_blocking_error": 0,
        "cancelled": 0,
    }


def test_json_with_the_app_down(fakes: Fakes, config: Path) -> None:
    fakes.app_down()
    fakes.open_ports.clear()

    run = _status(fakes, config, "--json")

    document = run.json()
    assert run.code == 1
    assert document["app"] == {"up": False, "url": APP, "version": None}
    assert document["collector"] == {"up": False, "address": "127.0.0.1:4318"}
    assert document["diagnostics"] == {"understood": False, "reason": "app_down"}
    assert document["sessions"] is None
    assert document["hook_failures"] is None


# Usage errors.


@pytest.mark.parametrize("flag", ["--app-port", "--otlp-port", "--grafana-port"])
def test_a_bad_port_is_a_usage_error(flag: str) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["status", flag, "70000"])

    assert raised.value.code == 2


def test_an_empty_config_dir_is_a_usage_error(fakes: Fakes) -> None:
    args = build_parser().parse_args(["status", "--config-dir", " "])
    out, err = io.StringIO(), io.StringIO()

    code = run_status(args, env=fakes.environment(), out=out, err=err)

    assert code == 2
    assert out.getvalue() == ""
    assert err.getvalue() == "ccobs status: --config-dir must not be empty\n"
    assert fakes.requests == []


def test_a_relative_config_dir_variable_is_a_usage_error(fakes: Fakes) -> None:
    fakes.environ = {"CLAUDE_CONFIG_DIR": PLANTED}

    run = _status(fakes, None)

    assert run.code == 2
    assert run.out == ""
    assert run.err.startswith("ccobs status: CLAUDE_CONFIG_DIR must be an absolute path")
    assert PLANTED not in run.err


# Privacy.


def test_no_output_quotes_a_planted_value(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / "config"
    planted_env = dict.fromkeys(
        (*telemetry_env(ENDPOINT), "ANTHROPIC_API_KEY", "OTEL_LOG_USER_PROMPTS"), PLANTED
    )
    _write_json(
        directory / "settings.json",
        {
            "env": planted_env,
            "apiKeyHelper": PLANTED,
            "hooks": {"Stop": [{"hooks": [{"type": "http", "url": f"http://{PLANTED}/hooks"}]}]},
        },
    )
    fakes.environ = dict(planted_env)

    text = _status(fakes, directory)
    as_json = _status(fakes, directory, "--json")

    assert text.code == as_json.code == 0
    assert PLANTED not in text.out + text.err
    assert PLANTED not in as_json.out + as_json.err
    assert "telemetry off" in text.out


# Encoding.

CJK: Final = "\u8a2d\u5b9a"  # not in cp1252


def _cp1252() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252"), raw


def test_text_output_survives_a_console_that_cannot_encode_the_path(
    fakes: Fakes, tmp_path: Path
) -> None:
    directory = tmp_path / f"config-{CJK}"
    _write_json(directory / "settings.json", _complete())
    args = build_parser().parse_args(["status", "--config-dir", str(directory)])
    out, raw = _cp1252()
    err, _ = _cp1252()

    code = run_status(args, env=fakes.environment(), out=out, err=err)

    out.flush()
    text = raw.getvalue().decode("cp1252")
    assert code == 0
    assert "config-\\u8a2d\\u5b9a" in text
    assert text.rstrip().endswith("Run `ccobs doctor` for details.")


def test_json_output_is_ascii(fakes: Fakes, tmp_path: Path) -> None:
    directory = tmp_path / f"config-{CJK}"
    _write_json(directory / "settings.json", _complete())
    args = build_parser().parse_args(["status", "--config-dir", str(directory), "--json"])
    out, raw = _cp1252()

    code = run_status(args, env=fakes.environment(), out=out, err=io.StringIO())

    out.flush()
    assert code == 0
    assert raw.getvalue().isascii()
    document = json.loads(raw.getvalue())
    assert document["settings"]["path"] == str(directory / "settings.json")


def test_usage_errors_survive_a_console_that_cannot_encode(fakes: Fakes) -> None:
    fakes.environ = {"CLAUDE_CONFIG_DIR": f"relative-{CJK}"}
    args = build_parser().parse_args(["status"])
    err, raw = _cp1252()

    code = run_status(args, env=fakes.environment(), out=io.StringIO(), err=err)

    err.flush()
    assert code == 2
    assert raw.getvalue().decode("cp1252").startswith("ccobs status: ")


# The default environment and the wiring.


def test_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the autouse fixture's guard

    env = status.default_environment()

    assert env.http is doctor.http_request
    assert env.connect is doctor.tcp_connect
    assert env.environ is os.environ
    assert env.platform == sys.platform


def test_main_runs_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fakes: Fakes,
    config: Path,
) -> None:
    monkeypatch.setattr(status, "default_environment", fakes.environment)

    code = main(["status", "--config-dir", str(config), "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["app"]["up"] is True


def test_help_lists_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "status" in capsys.readouterr().out


def test_status_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["status", "--help"])

    out = capsys.readouterr().out
    for option in ("--config-dir", "--app-port", "--otlp-port", "--grafana-port", "--json"):
        assert option in out
    assert "--stack-dir" not in out


def test_fakes_are_replaceable(fakes: Fakes) -> None:
    env = replace(fakes.environment(), platform="darwin")

    assert env.platform == "darwin"
