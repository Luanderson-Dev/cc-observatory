"""Tests for the rules of ``ccobs doctor`` (``ccobs.domain.doctor``, ADR 0039).

Each rule gets parsed data and returns a ``Check``; one test (or case) per branch. The
last tests seed recognizable placeholder secrets into every source a rule reads and
check that no title, detail or fix ever quotes one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, cast

import pytest

from ccobs.domain import doctor
from ccobs.domain.doctor import (
    Check,
    DockerVersions,
    EnvSource,
    HttpAnswer,
    SettingsSource,
    StatEntry,
)
from ccobs.domain.hook_config import REGISTERED_HTTP_EVENTS, hook_settings
from ccobs.domain.telemetry_settings import telemetry_env

HOOK_URL: Final = "http://127.0.0.1:4300/hooks"
ENDPOINT: Final = "http://127.0.0.1:4318"
PLANTED: Final = "planted-value-0001"
ALL_EVENTS: Final = len(REGISTERED_HTTP_EVENTS) + 1  # plus SessionStart


def _hooks(url: str = HOOK_URL, *, windows: bool = False) -> dict[str, object]:
    return cast(dict[str, object], hook_settings(url, windows=windows)["hooks"])


def _complete(**extra: object) -> dict[str, object]:
    return {"hooks": _hooks(), "env": telemetry_env(ENDPOINT), **extra}


def _texts(check: Check) -> str:
    return "\n".join([check.title, check.detail, check.fix or ""])


# Summary and exit code.


def test_summary_counts_every_status() -> None:
    checks = [
        Check("a", "ok", "t", ""),
        Check("b", "warn", "t", ""),
        Check("c", "warn", "t", ""),
        Check("d", "info", "t", ""),
    ]

    assert doctor.summary(checks) == {"ok": 1, "warn": 2, "fail": 0, "info": 1}


@pytest.mark.parametrize(
    ("statuses", "code"),
    [(("ok", "warn", "info"), 0), (("ok", "fail"), 1), ((), 0)],
)
def test_exit_code_is_1_only_with_a_failure(statuses: tuple[doctor.Status, ...], code: int) -> None:
    assert doctor.exit_code([Check(str(i), s, "t", "") for i, s in enumerate(statuses)]) == code


def test_urls() -> None:
    assert doctor.hook_url(4400) == "http://127.0.0.1:4400/hooks"
    assert doctor.otlp_endpoint(4319) == "http://127.0.0.1:4319"


# Claude Code and the host.


def test_curl_found() -> None:
    check = doctor.check_curl("/usr/bin/curl")

    assert (check.id, check.status, check.detail) == ("curl", "ok", "/usr/bin/curl")


def test_curl_missing_warns() -> None:
    check = doctor.check_curl(None)

    assert check.status == "warn"
    assert "SessionStart" in check.detail


@pytest.mark.parametrize(
    ("output", "status", "title"),
    [
        (None, "warn", "Claude Code was not found"),
        ("garbage", "warn", "could not read Claude Code's version"),
        ("2.1 (Claude Code)", "warn", "could not read Claude Code's version"),
        ("2.1.295 (Claude Code)", "warn", "Claude Code 2.1.295 is older than 2.1.296"),
        ("2.1.296 (Claude Code)", "ok", "Claude Code 2.1.296"),
        ("v3.0.1", "ok", "Claude Code 3.0.1"),
    ],
)
def test_claude_version(output: str | None, status: str, title: str) -> None:
    check = doctor.check_claude_version(output)

    assert (check.id, check.status, check.title) == ("claude-version", status, title)


GOOD_DOCKER: Final = DockerVersions(
    found=True,
    engine="28.3.2",
    compose="2.39.1",
    buildx="github.com/docker/buildx v0.26.1 abc",
)


def test_docker_ok() -> None:
    check = doctor.check_docker(GOOD_DOCKER, linux=True)

    assert (check.status, check.fix) == ("ok", None)
    assert "Docker Engine 28.3.2" in check.detail
    assert "Buildx 0.26.1" in check.detail


def test_docker_not_found_warns() -> None:
    check = doctor.check_docker(
        DockerVersions(found=False, engine=None, compose=None, buildx=None), linux=False
    )

    assert (check.status, check.title) == ("warn", "Docker was not found")


@pytest.mark.parametrize(
    ("change", "linux", "status", "text"),
    [
        ({"engine": None}, False, "warn", "the Docker daemon did not answer"),
        ({"engine": "nonsense"}, False, "warn", "could not read the Docker Engine version"),
        ({"engine": "24.0.9"}, False, "fail", "Docker Engine 24.0.9 is older than 25.0.0"),
        ({"engine": "27.5.1"}, True, "warn", "on Linux"),
        ({"engine": "27.5"}, True, "warn", "on Linux"),
        ({"compose": None}, False, "fail", "Compose plugin"),
        ({"compose": "v2.20.1"}, False, "fail", "Docker Compose 2.20.1 is older than 2.20.2"),
        ({"buildx": None}, False, "warn", "classic builder"),
        ({"buildx": "github.com/docker/buildx v0.16.2 x"}, False, "fail", "Buildx 0.16.2"),
    ],
)
def test_docker_versions(
    change: dict[str, str | None], linux: bool, status: str, text: str
) -> None:
    versions = DockerVersions(**{**GOOD_DOCKER.__dict__, **change})

    check = doctor.check_docker(versions, linux=linux)

    assert check.status == status
    assert text in check.detail
    assert check.fix is not None


def test_docker_old_engine_on_other_os_is_fine() -> None:
    versions = DockerVersions(**{**GOOD_DOCKER.__dict__, "engine": "27.5.1"})

    assert doctor.check_docker(versions, linux=False).status == "ok"


def test_docker_too_old_engine_on_linux_fails_without_the_linux_warning() -> None:
    versions = DockerVersions(**{**GOOD_DOCKER.__dict__, "engine": "24.0.0"})

    check = doctor.check_docker(versions, linux=True)

    assert check.status == "fail"
    assert "on Linux" not in check.detail


# Settings.


def test_env_of() -> None:
    assert doctor.env_of(None) == {}
    assert doctor.env_of({"env": []}) == {}
    assert doctor.env_of({"env": {"A": "1"}}) == {"A": "1"}


@pytest.mark.parametrize(
    ("exists", "error", "status", "fix"),
    [
        (True, None, "ok", ""),
        (False, None, "fail", "run ccobs setup"),
        (True, "the settings file is not valid JSON (line 1, column 2)", "fail", "fix the file"),
    ],
)
def test_settings_file(exists: bool, error: str | None, status: str, fix: str) -> None:
    check = doctor.check_settings_file("/x/settings.json", exists=exists, error=error)

    assert check.status == status
    assert "/x/settings.json" in check.detail
    assert (check.fix or "").startswith(fix)
    if error is not None:
        assert error in check.detail


# Hooks.


@pytest.mark.parametrize("check_id", ["hooks", "telemetry-env"])
def test_unreadable_settings_skip_the_hook_and_env_checks(check_id: str) -> None:
    check = (
        doctor.check_hooks(None, app_port=4300, windows=False)
        if check_id == "hooks"
        else doctor.check_telemetry_env(None, otlp_port=4318)
    )

    assert (check.id, check.status) == (check_id, "info")
    assert "Skipped" in check.detail


@pytest.mark.parametrize("windows", [False, True])
def test_hooks_all_present(windows: bool) -> None:
    settings = {"hooks": _hooks(windows=windows)}

    check = doctor.check_hooks(settings, app_port=4300, windows=windows)

    assert check.status == "ok"
    assert f"all {ALL_EVENTS} events" in check.title
    assert check.detail == HOOK_URL


def test_hooks_present_alongside_user_groups() -> None:
    hooks = _hooks()
    user_group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    hooks["PreToolUse"] = [user_group, *cast(list[object], hooks["PreToolUse"])]
    hooks["Custom"] = "not a list"

    assert doctor.check_hooks({"hooks": hooks}, app_port=4300, windows=False).status == "ok"


@pytest.mark.parametrize("block", [None, [], {}])
def test_hooks_missing_everywhere_fails(block: object) -> None:
    settings = {} if block is None else {"hooks": block}

    check = doctor.check_hooks(settings, app_port=4300, windows=False)

    assert check.status == "fail"
    assert f"{ALL_EVENTS} of {ALL_EVENTS}" in check.title
    assert check.fix == "run ccobs setup"


def test_hooks_some_missing_fail_naming_the_events() -> None:
    hooks = _hooks()
    del hooks["Stop"]
    hooks["SessionEnd"] = [{"hooks": "not a list"}, "not a group"]

    check = doctor.check_hooks({"hooks": hooks}, app_port=4300, windows=False)

    assert check.status == "fail"
    assert "Missing: Stop, SessionEnd." in check.detail


def test_hooks_fix_names_a_non_default_port() -> None:
    check = doctor.check_hooks({}, app_port=4400, windows=False)

    assert check.fix == "run ccobs setup --app-port 4400"


def test_hooks_of_an_older_version_warn() -> None:
    hooks = _hooks()
    hooks["Stop"] = [{"hooks": [{"type": "http", "url": HOOK_URL, "timeout": 5}]}]

    check = doctor.check_hooks({"hooks": hooks}, app_port=4300, windows=False)

    assert check.status == "warn"
    assert "Stop" in check.detail
    assert check.fix == "re-run ccobs setup"


def test_hooks_for_another_port_warn_naming_the_port() -> None:
    settings = {"hooks": _hooks("http://127.0.0.1:4400/hooks")}

    check = doctor.check_hooks(settings, app_port=4300, windows=False)

    assert check.status == "warn"
    assert "port 4400, not --app-port 4300" in check.detail
    assert check.fix is not None
    assert "--app-port" in check.fix


def test_hooks_without_a_port_count_as_port_80() -> None:
    settings = {"hooks": _hooks("http://127.0.0.1/hooks")}

    check = doctor.check_hooks(settings, app_port=4300, windows=False)

    assert "port 80," in check.detail


def test_leftover_handlers_beside_the_current_group_warn() -> None:
    hooks = _hooks()
    old = {"hooks": [{"type": "http", "url": "http://127.0.0.1:4400/hooks", "timeout": 1}]}
    hooks["Notification"] = [*cast(list[object], hooks["Notification"]), old]
    hooks["FileChanged"] = [old]  # an event this version does not register

    check = doctor.check_hooks({"hooks": hooks}, app_port=4300, windows=False)

    assert check.status == "warn"
    assert "FileChanged, Notification" in check.detail


# Telemetry env.


def test_telemetry_env_ok() -> None:
    check = doctor.check_telemetry_env(_complete(), otlp_port=4318)

    assert (check.status, check.detail) == ("ok", ENDPOINT)


def test_telemetry_env_missing_fails() -> None:
    check = doctor.check_telemetry_env({}, otlp_port=4318)

    assert check.status == "fail"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY is missing" in check.detail
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT is missing (expected {ENDPOINT})" in check.detail
    assert check.fix == "run ccobs setup"


def test_telemetry_env_null_values_are_missing() -> None:
    env = dict.fromkeys(telemetry_env(ENDPOINT))

    check = doctor.check_telemetry_env({"env": env}, otlp_port=4318)

    assert check.status == "fail"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY is missing" in check.detail
    assert check.fix == "run ccobs setup"


def test_telemetry_env_other_values_fail_with_force() -> None:
    env = {**telemetry_env(ENDPOINT), "OTEL_LOGS_EXPORTER": PLANTED}

    check = doctor.check_telemetry_env({"env": env}, otlp_port=4318)

    assert check.status == "fail"
    assert "OTEL_LOGS_EXPORTER holds another value" in check.detail
    assert check.fix is not None
    assert "--force" in check.fix
    assert PLANTED not in _texts(check)


def test_telemetry_env_for_another_port() -> None:
    check = doctor.check_telemetry_env(_complete(), otlp_port=4319)

    assert check.status == "fail"
    assert check.fix is not None
    assert check.fix.startswith("run ccobs setup --otlp-port 4319")


@pytest.mark.parametrize("value", [None, "delta"])
def test_telemetry_env_temporality_only_warns(value: str | None) -> None:
    env = telemetry_env(ENDPOINT)
    key = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
    if value is None:
        del env[key]
    else:
        env[key] = value

    check = doctor.check_telemetry_env({"env": env}, otlp_port=4318)

    assert check.status == "warn"
    assert "cumulative" in check.detail


# Env rules over several sources.


def _sources(**env: object) -> list[EnvSource]:
    return [EnvSource("your shell environment", env)]


@pytest.mark.parametrize(
    ("name", "value", "warns"),
    [
        ("OTEL_LOG_USER_PROMPTS", "1", True),
        ("OTEL_LOG_TOOL_DETAILS", "TRUE", True),
        ("OTEL_LOG_RAW_API_BODIES", "file:/tmp/x", True),
        ("OTEL_LOG_USER_PROMPTS", True, True),
        ("OTEL_LOG_USER_PROMPTS", "0", False),
        ("OTEL_LOG_USER_PROMPTS", " False ", False),
        ("OTEL_LOG_USER_PROMPTS", "off", False),
        ("OTEL_LOG_USER_PROMPTS", "", False),
        ("BETA_TRACING_ENDPOINT", "0", True),
        ("BETA_TRACING_ENDPOINT", " ", False),
        # JSON null is "not set".
        ("OTEL_LOG_USER_PROMPTS", None, False),
        ("BETA_TRACING_ENDPOINT", None, False),
        ("UNRELATED", "1", False),
    ],
)
def test_content_variables(name: str, value: object, warns: bool) -> None:
    check = doctor.check_content_variables(_sources(**{name: value}))

    assert check.status == ("warn" if warns else "ok")
    if warns:
        assert f"{name} in your shell environment" in check.detail


def test_values_that_are_not_json_are_judged_as_text() -> None:
    # A macOS plist can hold dates; any non-empty value enables a content flag.
    sources = [EnvSource("plist", {"OTEL_LOG_USER_PROMPTS": datetime(2026, 1, 1, tzinfo=UTC)})]

    assert doctor.check_content_variables(sources).status == "warn"


@pytest.mark.parametrize(
    ("value", "warns"),
    [("false", True), ("FALSE", True), ("true", False), ("0", False), (None, False)],
)
def test_session_id(value: str | None, warns: bool) -> None:
    sources = [EnvSource("/x/settings.json", {"OTEL_METRICS_INCLUDE_SESSION_ID": value})]

    check = doctor.check_session_id(sources)

    assert check.status == ("warn" if warns else "ok")
    if warns:
        assert "/x/settings.json" in check.detail


@pytest.mark.parametrize(
    ("env", "warns"),
    [
        ({"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://elsewhere"}, True),
        ({"OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc"}, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}, False),
        ({"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": None}, False),
        ({}, False),
    ],
)
def test_per_signal_endpoints(env: dict[str, str], warns: bool) -> None:
    check = doctor.check_per_signal_endpoints(_sources(**env))

    assert check.status == ("warn" if warns else "ok")


def test_shell_overrides_are_info() -> None:
    environ = {"OTEL_METRICS_EXPORTER": PLANTED, "OTEL_TRACES_EXPORTER": "otlp", "PATH": "x"}

    check = doctor.check_shell_overrides(environ, otlp_port=4318)

    assert check.status == "info"
    assert "OTEL_METRICS_EXPORTER, OTEL_TRACES_EXPORTER" in check.detail
    assert "Desktop" in check.detail


def test_no_shell_overrides() -> None:
    assert doctor.check_shell_overrides({"PATH": "x"}, otlp_port=4318).status == "ok"


def test_long_lists_are_cut() -> None:
    sources = [
        EnvSource(f"file{i}", {"OTEL_METRICS_INCLUDE_SESSION_ID": "false"}) for i in range(12)
    ]

    check = doctor.check_session_id(sources)

    assert "and 2 more" in check.detail


# Policies.


def _managed(data: Mapping[str, object] | None, *, error: str | None = None) -> SettingsSource:
    return SettingsSource("managed", "/etc/claude-code/managed-settings.json", data, error)


def _hkcu(data: Mapping[str, object]) -> SettingsSource:
    return SettingsSource("hkcu", "HKCU\\SOFTWARE\\Policies\\ClaudeCode", data)


def _user(data: Mapping[str, object]) -> SettingsSource:
    return SettingsSource("user", "/home/u/.claude/settings.json", data)


def _project(data: Mapping[str, object]) -> SettingsSource:
    return SettingsSource("project", "/p/.claude/settings.json", data)


def _local(data: Mapping[str, object]) -> SettingsSource:
    return SettingsSource("local", "/p/.claude/settings.local.json", data)


USER_FILE: Final = "/home/u/.claude/settings.json"


def test_settings_sources_without_managed_documents() -> None:
    check = doctor.check_settings_sources([_user({})])

    assert check.status == "info"
    assert "No managed settings document" in check.detail
    assert "/status" in check.detail


def test_settings_sources_lists_read_and_unreadable_documents() -> None:
    sources = [
        _managed({}),
        _hkcu({}),
        SettingsSource("managed", "/etc/claude-code/managed-settings.d/x.json", None, "not JSON"),
        SettingsSource("project", "/p/.claude/settings.json", None, "not UTF-8"),
    ]

    check = doctor.check_settings_sources(sources)

    assert check.status == "info"
    assert "/etc/claude-code/managed-settings.json, HKCU" in check.detail
    assert "could not read /etc/claude-code/managed-settings.d/x.json: not JSON" in check.detail
    assert "could not read /p/.claude/settings.json: not UTF-8" in check.detail


@pytest.mark.parametrize(
    ("sources", "status", "where"),
    [
        ([_managed({"disableAllHooks": True})], "fail", "managed-settings.json"),
        ([_user({"disableAllHooks": True})], "fail", "/home/u/.claude/settings.json"),
        ([_project({"disableAllHooks": True})], "warn", "/p/.claude/settings.json"),
        ([_user({}), _local({"disableAllHooks": True})], "warn", "settings.local.json"),
        ([_project({"disableAllHooks": True}), _local({"disableAllHooks": False})], "ok", ""),
        ([_hkcu({"disableAllHooks": True})], "warn", "HKCU"),
        ([_managed({"disableAllHooks": "true"}), _user({})], "ok", ""),
        ([_managed(None, error="x"), _user({})], "ok", ""),
        # An admin source that defines the key decides, whatever the other files say.
        ([_managed({"disableAllHooks": False}), _user({"disableAllHooks": True})], "ok", ""),
        ([_managed({"disableAllHooks": False}), _hkcu({"disableAllHooks": True})], "ok", ""),
        (
            [_managed({"disableAllHooks": True}), _project({"disableAllHooks": False})],
            "fail",
            "managed-settings.json",
        ),
        # JSON null is "not set": the next level decides.
        ([_managed({"disableAllHooks": None}), _user({"disableAllHooks": True})], "fail", "/home"),
        ([_project({"disableAllHooks": None}), _user({"disableAllHooks": True})], "fail", "/home"),
        # HKCU counts only when no admin source defines the key, and below a user true.
        ([_hkcu({"disableAllHooks": True}), _user({"disableAllHooks": False})], "warn", "HKCU"),
        ([_hkcu({"disableAllHooks": True}), _user({"disableAllHooks": True})], "fail", "/home"),
        ([_hkcu({"disableAllHooks": True}), _project({"disableAllHooks": True})], "warn", "HKCU"),
        ([_hkcu({"disableAllHooks": False}), _user({})], "ok", ""),
    ],
)
def test_disable_all_hooks(sources: list[SettingsSource], status: str, where: str) -> None:
    check = doctor.check_disable_all_hooks(sources)

    assert check.status == status
    assert where in check.detail


@pytest.mark.parametrize(
    "sources",
    [
        [_user({"disableAllHooks": True}), _project({"disableAllHooks": False})],
        [_user({"disableAllHooks": True}), _local({"disableAllHooks": False})],
    ],
)
def test_a_project_that_turns_hooks_back_on_is_noted(sources: list[SettingsSource]) -> None:
    check = doctor.check_disable_all_hooks(sources)

    assert check.status == "info"
    assert "other projects" in check.title
    assert sources[0].label in check.detail
    assert sources[1].label in check.detail
    assert check.fix == f"remove disableAllHooks from {sources[0].label}"


@pytest.mark.parametrize(
    ("sources", "status"),
    [
        ([_managed({"allowManagedHooksOnly": True})], "fail"),
        ([_hkcu({"allowManagedHooksOnly": True})], "warn"),
        ([_user({"allowManagedHooksOnly": True})], "ok"),
        ([_managed({"allowManagedHooksOnly": False})], "ok"),
    ],
)
def test_managed_hooks_only(sources: list[SettingsSource], status: str) -> None:
    assert doctor.check_managed_hooks_only(sources).status == status


@pytest.mark.parametrize(
    ("sources", "status"),
    [
        ([], "ok"),
        ([_user({"allowedHttpHookUrls": ["http://127.0.0.1:4300/hooks"]})], "ok"),
        ([_user({"allowedHttpHookUrls": ["http://127.0.0.1:*"]})], "ok"),
        ([_user({"allowedHttpHookUrls": ["*"]})], "ok"),
        ([_user({"allowedHttpHookUrls": []})], "fail"),
        # Not a list (or JSON null): not defined.
        ([_user({"allowedHttpHookUrls": "not a list"})], "ok"),
        ([_user({"allowedHttpHookUrls": None})], "ok"),
        ([_user({"allowedHttpHookUrls": None}), _project({"allowedHttpHookUrls": []})], "fail"),
        # The scheme and host match case-insensitively, and a trailing dot on the host
        # is the same host; the path stays case-sensitive.
        ([_user({"allowedHttpHookUrls": ["HTTP://127.0.0.1:4300/hooks"]})], "ok"),
        ([_user({"allowedHttpHookUrls": ["http://127.0.0.1.:4300/hooks"]})], "ok"),
        ([_user({"allowedHttpHookUrls": ["Http://127.0.0.1.:*"]})], "ok"),
        ([_user({"allowedHttpHookUrls": ["http://127.0.0.1:4300/HOOKS"]})], "fail"),
        ([_user({"allowedHttpHookUrls": ["127.0.0.1:4300/hooks"]})], "fail"),
        ([_user({"allowedHttpHookUrls": [7, "https://hooks.example.com/*"]})], "fail"),
        # "." is literal: it must not match any character.
        ([_user({"allowedHttpHookUrls": ["http://127x0x0x1:4300/hooks"]})], "fail"),
        ([_user({"allowedHttpHookUrls": ["http://127.0.0.1:4300/hook"]})], "fail"),
        # Arrays merge across files: one file's pattern allows the URL.
        (
            [
                _managed({"allowedHttpHookUrls": []}),
                _project({"allowedHttpHookUrls": ["http://127.0.0.1:4300/*"]}),
            ],
            "ok",
        ),
        ([_hkcu({"allowedHttpHookUrls": []})], "warn"),
        ([_project({"allowedHttpHookUrls": []}), _user({})], "fail"),
        (
            [_hkcu({"allowedHttpHookUrls": []}), _user({"allowedHttpHookUrls": ["*"]})],
            "ok",
        ),
        ([SettingsSource("managed", "m", None, "unreadable")], "ok"),
    ],
)
def test_http_hook_allowlist(sources: list[SettingsSource], status: str) -> None:
    check = doctor.check_http_hook_allowlist(sources, app_port=4300, user_file=USER_FILE)

    assert check.status == status
    if status != "ok":
        assert check.fix is not None
        assert f'"http://127.0.0.1:4300/hooks" to allowedHttpHookUrls in {USER_FILE}' in check.fix
        assert "merge" in check.fix
        defining = [
            s for s in sources if isinstance((s.data or {}).get("allowedHttpHookUrls"), list)
        ]
        assert all(source.label in check.detail for source in defining)


def test_http_hook_allowlist_never_sends_the_user_to_a_managed_file() -> None:
    sources = [_managed({"allowedHttpHookUrls": []})]

    check = doctor.check_http_hook_allowlist(sources, app_port=4300, user_file=USER_FILE)

    assert check.status == "fail"
    assert check.fix is not None
    assert "managed-settings.json" not in check.fix
    assert USER_FILE in check.fix


def test_http_hook_allowlist_never_quotes_patterns() -> None:
    sources = [_user({"allowedHttpHookUrls": [f"https://{PLANTED}.example.com/*"]})]

    check = doctor.check_http_hook_allowlist(sources, app_port=4300, user_file=USER_FILE)

    assert check.status == "fail"
    assert PLANTED not in _texts(check)


@pytest.mark.parametrize(
    ("env", "text"),
    [
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": PLANTED}, "OTEL_EXPORTER_OTLP_ENDPOINT to another"),
        ({"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": PLANTED}, "sets OTEL_EXPORTER_OTLP_METRICS"),
        ({"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, "turns CLAUDE_CODE_ENABLE_TELEMETRY off"),
        ({"CLAUDE_CODE_ENABLE_TELEMETRY": ""}, "turns CLAUDE_CODE_ENABLE_TELEMETRY off"),
        ({"OTEL_LOGS_EXPORTER": "None"}, "turns OTEL_LOGS_EXPORTER off"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "sets OTEL_EXPORTER_OTLP_PROTOCOL to another"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": PLANTED}, "OTEL_EXPORTER_OTLP_PROTOCOL to another"),
        ({"OTEL_METRICS_EXPORTER": "prometheus"}, "sets OTEL_METRICS_EXPORTER to an exporter"),
        ({"OTEL_LOGS_EXPORTER": PLANTED}, "sets OTEL_LOGS_EXPORTER to an exporter"),
    ],
)
def test_managed_env_fails(env: dict[str, str], text: str) -> None:
    check = doctor.check_managed_env([_managed({"env": env})], otlp_port=4318)

    assert check.status == "fail"
    assert text in check.detail
    assert PLANTED not in _texts(check)


@pytest.mark.parametrize(
    "env",
    [
        {"OTEL_METRICS_EXPORTER": "none"},
        {"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"},
        {"OTEL_LOGS_EXPORTER": "console"},
    ],
)
def test_managed_env_in_hkcu_warns(env: dict[str, str]) -> None:
    check = doctor.check_managed_env([_hkcu({"env": env})], otlp_port=4318)

    assert check.status == "warn"


@pytest.mark.parametrize(
    "sources",
    [
        [_managed({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT}})],
        [_managed({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_LOGS_EXPORTER": "otlp"}})],
        [_managed({"env": {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ""}})],
        [_managed({"env": {"OTEL_EXPORTER_OTLP_PROTOCOL": " HTTP/protobuf "}})],
        # A list of exporters that includes otlp still sends to the stack.
        [
            _managed(
                {"env": {"OTEL_METRICS_EXPORTER": "otlp,console", "OTEL_LOGS_EXPORTER": "OTLP"}}
            )
        ],
        # JSON null is "not set".
        [
            _managed(
                {
                    "env": dict.fromkeys(
                        (
                            "OTEL_EXPORTER_OTLP_ENDPOINT",
                            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                            "CLAUDE_CODE_ENABLE_TELEMETRY",
                            "OTEL_EXPORTER_OTLP_PROTOCOL",
                            "OTEL_METRICS_EXPORTER",
                        )
                    )
                }
            )
        ],
        [_managed({"env": "not an object"})],
        [_user({"env": {"OTEL_LOGS_EXPORTER": "none"}})],
        # The stack's endpoint with a trailing slash still points at the stack.
        [_managed({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318/"}})],
    ],
)
def test_managed_env_ok(sources: list[SettingsSource]) -> None:
    assert doctor.check_managed_env(sources, otlp_port=4318).status == "ok"


@pytest.mark.parametrize(
    ("sources", "status"),
    [
        ([_project({"env": {"OTEL_METRICS_EXPORTER": "none"}})], "warn"),
        ([_local({"env": {"OTEL_LOGS_EXPORTER": "NONE"}})], "warn"),
        # Claude Code drops a project's CLAUDE_CODE_ENABLE_TELEMETRY (settings reference).
        ([_project({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}})], "ok"),
        ([_user({"env": {"OTEL_METRICS_EXPORTER": "none"}})], "ok"),
        ([_project({})], "ok"),
    ],
)
def test_project_telemetry_off(sources: list[SettingsSource], status: str) -> None:
    check = doctor.check_project_telemetry_off(sources)

    assert check.status == status
    if status == "warn":
        assert sources[0].label in check.detail


# Stack.


def test_http_answer_ok() -> None:
    assert HttpAnswer(204).ok
    assert not HttpAnswer(301).ok
    assert not HttpAnswer(199).ok
    assert not HttpAnswer(None).ok


@pytest.mark.parametrize(
    ("answer", "status"), [(HttpAnswer(200), "ok"), (HttpAnswer(None), "fail")]
)
def test_app(answer: HttpAnswer, status: str) -> None:
    check = doctor.check_app(answer, app_port=4300)

    assert check.status == status
    if status == "fail":
        assert "no answer" in check.detail


@pytest.mark.parametrize(
    ("answer", "status"),
    [(HttpAnswer(204), "ok"), (HttpAnswer(422), "fail"), (HttpAnswer(None), "fail")],
)
def test_hook_endpoint(answer: HttpAnswer, status: str) -> None:
    check = doctor.check_hook_endpoint(answer, app_port=4300)

    assert check.status == status
    assert "invalid" in check.detail
    assert HOOK_URL in check.detail


def _diagnostics(**change: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": "0.5.0",
        "host": "local",
        "started_at_ms": 1,
        "sessions": {"by_state": {"working": 1}, "open_blocks": 0},
        "hooks": {
            "received": {"Stop": 3, "other": 0},
            "dropped": {"invalid": 4, "full": 0, "too_large": 0, "internal": 0},
        },
        "hook_failures": {
            "available": True,
            "window_s": 3600,
            "executions": 10,
            "non_blocking_error": 0,
            "cancelled": 0,
        },
    }
    document.update(change)
    return document


def _answer(document: object, status: int = 200) -> HttpAnswer:
    return HttpAnswer(status, json.dumps(document).encode())


def test_diagnostics_skipped_when_the_app_is_down() -> None:
    check = doctor.check_diagnostics(None, cli_version="0.5.0")

    assert check.status == "info"


def test_diagnostics_ok() -> None:
    check = doctor.check_diagnostics(_answer(_diagnostics()), cli_version="0.5.0")

    assert check.status == "ok"
    assert "App 0.5.0; 3 hook events" in check.detail


@pytest.mark.parametrize(
    "answer",
    [
        HttpAnswer(404, b'{"detail": "Not Found"}'),
        HttpAnswer(200, b"not json"),
        HttpAnswer(200, b"[]"),
        _answer(_diagnostics(version=5)),
        _answer(_diagnostics(hooks=[])),
        _answer(_diagnostics(hooks={"received": {"Stop": "x"}, "dropped": {}})),
        _answer(_diagnostics(hooks={"received": {"Stop": True}, "dropped": {}})),
        _answer(_diagnostics(hooks={"received": {}, "dropped": []})),
        _answer(_diagnostics(hook_failures=[])),
        _answer(_diagnostics(hook_failures={"available": "yes"})),
    ],
)
def test_diagnostics_not_understood_warns(answer: HttpAnswer) -> None:
    check = doctor.check_diagnostics(answer, cli_version="0.5.0")

    assert check.status == "warn"
    assert "rebuild the app" in (check.fix or "")


def test_diagnostics_version_mismatch() -> None:
    check = doctor.check_diagnostics(_answer(_diagnostics()), cli_version="0.6.0")

    assert check.status == "warn"
    assert "CLI 0.6.0, app 0.5.0: rebuild the app or upgrade the CLI." in check.detail


def test_diagnostics_never_echoes_an_odd_version() -> None:
    answer = _answer(_diagnostics(version=f"<{PLANTED} >"))

    check = doctor.check_diagnostics(answer, cli_version="0.5.0")

    assert "app unknown" in check.detail
    assert PLANTED not in _texts(check)


@pytest.mark.parametrize(
    ("reason", "shown"), [("timeout", "timeout"), (PLANTED, "unknown"), (None, "unknown")]
)
def test_diagnostics_loki_unavailable(reason: str | None, shown: str) -> None:
    failures = {"available": False, "window_s": 3600, "reason": reason}

    check = doctor.check_diagnostics(
        _answer(_diagnostics(hook_failures=failures)), cli_version="0.5.0"
    )

    assert check.status == "warn"
    assert f"The app cannot read Loki: {shown}." in check.detail


def test_diagnostics_hook_failures() -> None:
    failures = {
        "available": True,
        "window_s": 3600,
        "executions": 3,
        "non_blocking_error": 2,
        "cancelled": 1,
    }

    check = doctor.check_diagnostics(
        _answer(_diagnostics(hook_failures=failures)), cli_version="0.5.0"
    )

    assert check.status == "warn"
    assert "2 non-blocking errors, 1 cancelled" in check.detail
    assert "your other hooks" in check.detail


def test_diagnostics_serious_drops() -> None:
    hooks = {"received": {"Stop": 1}, "dropped": {"invalid": 9, "full": 2, "internal": 1}}

    check = doctor.check_diagnostics(_answer(_diagnostics(hooks=hooks)), cli_version="0.5.0")

    assert check.status == "warn"
    assert "full 2, too_large 0, internal 1" in check.detail


def test_diagnostics_no_hook_event_yet_is_info() -> None:
    hooks = {"received": {"Stop": 0}, "dropped": {}}

    check = doctor.check_diagnostics(_answer(_diagnostics(hooks=hooks)), cli_version="0.5.0")

    assert check.status == "info"
    assert "open a Claude Code session" in check.detail


@pytest.mark.parametrize(("connected", "status"), [(True, "ok"), (False, "fail")])
def test_otlp(connected: bool, status: str) -> None:
    check = doctor.check_otlp(connected, otlp_port=4318)

    assert (check.status, check.detail) == (status, "127.0.0.1:4318")


@pytest.mark.parametrize(("answer", "status"), [(HttpAnswer(200), "ok"), (HttpAnswer(503), "warn")])
def test_grafana(answer: HttpAnswer, status: str) -> None:
    assert doctor.check_grafana(answer, grafana_port=4330).status == status


# Clone.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("services:\nname: cc-observatory\n", True),
        ("name: 'cc-observatory'  # the project\r\n", True),
        ('name: "cc-observatory"', True),
        ("  name: cc-observatory", False),
        ("name: cc-observatory-it", False),
        ("name: \"cc-observatory'", False),
        ("", False),
    ],
)
def test_is_ccobs_compose(text: str, expected: bool) -> None:
    assert doctor.is_ccobs_compose(text) is expected


def test_stack_readable_ok() -> None:
    entries = [StatEntry("stack", True, 0o755), StatEntry("stack/a.yaml", False, 0o644)]

    assert doctor.check_stack_readable(entries).status == "ok"


def test_stack_readable_fails_naming_paths() -> None:
    entries = [
        StatEntry("stack", True, 0o754),  # not searchable by others
        StatEntry("stack/a.yaml", False, 0o640),  # not readable by others
        StatEntry("stack/b", True, 0o711),  # not readable by others
        StatEntry("stack/c.yaml", False, 0o604),
    ]

    check = doctor.check_stack_readable(entries)

    assert check.status == "fail"
    assert "stack, stack/a.yaml, stack/b." in check.detail
    assert check.fix == "chmod -R o+rX stack/"


def test_stack_entries_that_could_not_be_checked_warn() -> None:
    entries = [
        StatEntry("stack", True, 0o755),
        StatEntry("stack/secret", True, 0, "cannot list it: Permission denied"),
        StatEntry("stack/a.yaml", False, 0, "Permission denied"),
    ]

    check = doctor.check_stack_readable(entries)

    assert check.status == "warn"
    assert "Could not check: stack/secret (cannot list it: Permission denied)" in check.detail
    assert "stack/a.yaml (Permission denied)" in check.detail
    assert check.fix is not None


def test_stack_entries_blocked_and_unchecked_fail() -> None:
    entries = [
        StatEntry("stack", True, 0o700),
        StatEntry("stack/a.yaml", False, 0, "Permission denied"),
    ]

    check = doctor.check_stack_readable(entries)

    assert check.status == "fail"
    assert "Not readable (or searchable) by others: stack." in check.detail
    assert "Could not check: stack/a.yaml" in check.detail
    assert check.fix == "chmod -R o+rX stack/"


def test_parse_env_file_reads_only_known_keys() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "CCOBS_APP_PORT=4400",
            'CCOBS_HOST_LABEL="my-box"',
            "CCOBS_LOKI_RETENTION='48h'",
            "CCOBS_GRAFANA_PORT=4331 # mine",
            f"CCOBS_DISCORD_WEBHOOK_URL={PLANTED}",
            "NOT A PAIR",
            " CCOBS_OTLP_HTTP_PORT = 4319 ",
        ]
    )

    assert doctor.parse_env_file(text) == {
        "CCOBS_APP_PORT": "4400",
        "CCOBS_HOST_LABEL": "my-box",
        "CCOBS_LOKI_RETENTION": "48h",
        "CCOBS_GRAFANA_PORT": "4331",
        "CCOBS_OTLP_HTTP_PORT": "4319",
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Empty is absent: compose's ${VAR:-default} uses the default.
        ("CCOBS_APP_PORT=", {}),
        ("CCOBS_HOST_LABEL=''", {}),
        ('CCOBS_HOST_LABEL=""  # empty', {}),
        ("CCOBS_APP_PORT= # nothing", {}),
        # An unquoted value's comment goes before the quotes are judged.
        ('CCOBS_HOST_LABEL="my-box" # mine', {"CCOBS_HOST_LABEL": "my-box"}),
        ("CCOBS_HOST_LABEL='my-box'\t# mine", {"CCOBS_HOST_LABEL": "my-box"}),
        ("CCOBS_LOKI_RETENTION=48h\t# two days", {"CCOBS_LOKI_RETENTION": "48h"}),
        # A quoted value may hold '#'; an unquoted one only after something other than
        # a blank.
        ('CCOBS_HOST_LABEL="a # b"', {"CCOBS_HOST_LABEL": "a # b"}),
        ("CCOBS_HOST_LABEL='a#b' # c", {"CCOBS_HOST_LABEL": "a#b"}),
        ("CCOBS_HOST_LABEL=a#b", {"CCOBS_HOST_LABEL": "a#b"}),
        # An unmatched quote is part of the value.
        ("CCOBS_HOST_LABEL='my-box", {"CCOBS_HOST_LABEL": "'my-box"}),
        ('CCOBS_HOST_LABEL="', {"CCOBS_HOST_LABEL": '"'}),
        # compose's dotenv accepts a shell-style export prefix.
        ("export CCOBS_APP_PORT=4400", {"CCOBS_APP_PORT": "4400"}),
    ],
)
def test_parse_env_file_values(line: str, expected: dict[str, str]) -> None:
    assert doctor.parse_env_file(line) == expected


def _env_check(values: Mapping[str, str] | None, **ports: int) -> Check:
    used = {"app_port": 4300, "otlp_port": 4318, "grafana_port": 4330, **ports}
    return doctor.check_env_file(values, **used)


def test_env_file_absent_with_default_ports() -> None:
    check = _env_check(None)

    assert check.status == "ok"
    assert "No .env" in check.detail


def test_env_file_absent_with_other_ports_warns() -> None:
    check = _env_check(None, app_port=4400)

    assert check.status == "warn"
    assert "the stack's default is 4300, but doctor used --app-port 4400" in check.detail
    assert check.fix is None


def test_env_file_valid() -> None:
    values = {
        "CCOBS_LOKI_RETENTION": "720h",
        "CCOBS_HOST_LABEL": "a.b_c-1",
        "CCOBS_APP_PORT": "4300",
    }

    check = _env_check(values)

    assert check.status == "ok"
    assert "valid" in check.detail


@pytest.mark.parametrize(
    ("values", "status", "text"),
    [
        ({"CCOBS_LOKI_RETENTION": "30h"}, "warn", "not a multiple of 24h"),
        ({"CCOBS_LOKI_RETENTION": PLANTED}, "warn", "CCOBS_LOKI_RETENTION is not a duration"),
        ({"CCOBS_HOST_LABEL": PLANTED + " x"}, "fail", "CCOBS_HOST_LABEL must be"),
        ({"CCOBS_HOST_LABEL": ""}, "fail", "CCOBS_HOST_LABEL must be"),
        ({"CCOBS_APP_PORT": PLANTED}, "warn", "CCOBS_APP_PORT is not a port"),
        ({"CCOBS_APP_PORT": "70000"}, "warn", "CCOBS_APP_PORT is not a port"),
        ({"CCOBS_APP_PORT": "\u0664\u0663"}, "warn", "CCOBS_APP_PORT is not a port"),
        (
            {"CCOBS_OTLP_HTTP_PORT": "4319"},
            "warn",
            "CCOBS_OTLP_HTTP_PORT is 4319 in .env, but doctor used --otlp-port 4318: pass "
            "--otlp-port 4319",
        ),
        ({"CCOBS_GRAFANA_PORT": "4331"}, "warn", "--grafana-port 4331"),
    ],
)
def test_env_file_problems(values: dict[str, str], status: str, text: str) -> None:
    check = _env_check(values)

    assert check.status == status
    assert text in check.detail
    assert PLANTED not in _texts(check)
    assert check.fix is not None


# Privacy across every rule.


def test_no_rule_quotes_a_secret_value() -> None:
    secret_env = dict.fromkeys(
        (
            *telemetry_env(ENDPOINT),
            "OTEL_LOG_USER_PROMPTS",
            "BETA_TRACING_ENDPOINT",
            "OTEL_METRICS_INCLUDE_SESSION_ID",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "ANTHROPIC_API_KEY",
        ),
        PLANTED,
    )
    settings = {
        "env": secret_env,
        "hooks": {"Stop": [{"hooks": [{"type": "http", "url": f"http://{PLANTED}/hooks"}]}]},
        "allowedHttpHookUrls": [PLANTED],
        "disableAllHooks": PLANTED,
    }
    sources = [
        SettingsSource(level, f"/{level}", settings)
        for level in ("managed", "hkcu", "user", "project", "local")
    ]
    sources.append(SettingsSource("managed", "/bad", None, "not valid JSON"))
    env_sources = [EnvSource("x", secret_env)]
    checks = [
        doctor.check_hooks(settings, app_port=4300, windows=False),
        doctor.check_telemetry_env(settings, otlp_port=4318),
        doctor.check_content_variables(env_sources),
        doctor.check_session_id(env_sources),
        doctor.check_per_signal_endpoints(env_sources),
        doctor.check_shell_overrides(secret_env, otlp_port=4318),
        doctor.check_settings_sources(sources),
        doctor.check_disable_all_hooks(sources),
        doctor.check_managed_hooks_only(sources),
        doctor.check_http_hook_allowlist(sources, app_port=4300, user_file="/user"),
        doctor.check_managed_env(sources, otlp_port=4318),
        doctor.check_project_telemetry_off(sources),
        doctor.check_env_file(
            dict.fromkeys(doctor.ENV_FILE_KEYS, PLANTED), app_port=1, otlp_port=2, grafana_port=3
        ),
        doctor.check_stack_readable([StatEntry("stack", True, 0, "Permission denied")]),
        doctor.check_diagnostics(_answer(_diagnostics(host=PLANTED)), cli_version="0.5.0"),
        doctor.check_alerts(
            _answer(_diagnostics(host=PLANTED, alerts=_alerts({"suspended_status": 404})))
        ),
    ]

    assert all(PLANTED not in _texts(check) for check in checks)
    assert {check.status for check in checks} >= {"warn", "fail"}


# The alerts check (ADR 0046): Discord delivery, from the diagnostics answer.


def _alerts(discord: dict[str, object] | None = None, **change: object) -> dict[str, object]:
    alerts: dict[str, object] = {
        "rules": ["waiting_user", "repeated_failures"],
        "open": 2,
        "fired_24h": 5,
        "discord": {
            "configured": True,
            "suspended_status": None,
            "pending": 0,
            "failed_24h": 0,
            "expired_24h": 0,
            **(discord or {}),
        },
    }
    alerts.update(change)
    return alerts


def _alerts_check(alerts: object) -> Check:
    return doctor.check_alerts(_answer(_diagnostics(alerts=alerts)))


def test_alerts_ok_when_discord_delivers() -> None:
    check = _alerts_check(_alerts())

    assert check.id == "alerts"
    assert check.status == "ok"
    assert check.fix is None
    assert "2 on, 5 fired in the last 24 h; rules on: waiting_user, repeated_failures." in (
        check.detail
    )
    assert "Discord is configured." in check.detail


def test_alerts_ok_names_deliveries_waiting() -> None:
    check = _alerts_check(_alerts({"pending": 3}))

    assert check.status == "ok"
    assert "Discord is configured; 3 alerts waiting to be sent." in check.detail


def test_alerts_with_no_rule_on_say_so() -> None:
    check = _alerts_check(_alerts(rules=[]))

    assert "rules on: none." in check.detail


def test_discord_not_configured_is_info() -> None:
    check = _alerts_check(_alerts({"configured": False}))

    # The browser alone is a valid setup, not a problem (ADR 0046).
    assert check.status == "info"
    assert "Discord is not configured; alerts show in the browser only." in check.detail
    assert check.fix is None


@pytest.mark.parametrize(
    ("failed", "expired", "line"),
    [
        (1, 0, "1 alert did not reach Discord in the last 24 h (1 failed, 0 expired)."),
        (0, 1, "1 alert did not reach Discord in the last 24 h (0 failed, 1 expired)."),
        (2, 3, "5 alerts did not reach Discord in the last 24 h (2 failed, 3 expired)."),
    ],
)
def test_undelivered_alerts_warn(failed: int, expired: int, line: str) -> None:
    check = _alerts_check(_alerts({"failed_24h": failed, "expired_24h": expired}))

    assert check.status == "warn"
    assert line in check.detail
    assert "docker compose logs app" in (check.fix or "")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_suspended_webhook_fails(status: int) -> None:
    check = _alerts_check(_alerts({"suspended_status": status, "failed_24h": 1}))

    assert check.status == "fail"
    assert (
        f"Discord refused the webhook (status {status}); sending is suspended until the app"
        " restarts." in check.detail
    )
    assert check.fix == (
        "check CCOBS_DISCORD_WEBHOOK_URL in the stack's .env, then `docker compose up -d`"
    )


def test_an_app_that_predates_alerts_is_info() -> None:
    document = _diagnostics()
    assert "alerts" not in document

    check = doctor.check_alerts(_answer(document))

    assert check.status == "info"
    assert check.title == "the app predates alerts"
    assert "rebuild the app" in (check.fix or "")


def test_alerts_the_app_could_not_read_warn() -> None:
    check = _alerts_check(None)

    assert check.status == "warn"
    assert "could not read its alerts" in check.detail
    assert "docker compose logs app" in (check.fix or "")


def test_alerts_skipped_when_the_app_is_down() -> None:
    check = doctor.check_alerts(None)

    assert check.status == "info"
    assert check.detail == "Skipped: the app is down."


@pytest.mark.parametrize(
    "answer",
    [
        HttpAnswer(404, b'{"detail": "Not Found"}'),
        HttpAnswer(503, b'{"detail": "busy"}'),
        HttpAnswer(None),
    ],
)
def test_alerts_skipped_without_diagnostics(answer: HttpAnswer) -> None:
    check = doctor.check_alerts(answer)

    # The diagnostics check already warns on the answer itself.
    assert check.status == "info"
    assert check.detail == "Skipped: no diagnostics from the app (see `diagnostics` above)."
    assert check.fix is None


@pytest.mark.parametrize("body", [b"not json", b"{}", json.dumps({"version": 1}).encode()])
def test_alerts_skipped_when_the_diagnostics_were_not_understood(body: bytes) -> None:
    check = doctor.check_alerts(HttpAnswer(200, body))

    assert check.status == "info"
    assert check.detail == "Skipped: the app's diagnostics were not understood."


@pytest.mark.parametrize(
    "alerts",
    [[], "on", _alerts({"suspended_status": "404"}), _alerts(open={"count": 2})],
)
def test_alerts_not_understood_warn(alerts: object) -> None:
    check = _alerts_check(alerts)

    assert check.status == "warn"
    assert check.title == "the app's alerts were not understood"
    assert check.detail == "The app's alerts were not understood (a newer or older app?)."
    fix = check.fix or ""
    assert "uvx cc-observatory@latest" in fix
    assert "docker compose build app" in fix
    assert "logs" not in fix


def test_alerts_not_understood_leave_the_diagnostics_check_alone() -> None:
    answer = _answer(_diagnostics(alerts=_alerts(open="2")))

    check = doctor.check_diagnostics(answer, cli_version="0.5.0")

    assert check.title != "the app's diagnostics were not understood"


def test_the_alerts_check_never_shows_a_webhook_url() -> None:
    url = f"https://discord.com/api/webhooks/1/{PLANTED}"
    document = _diagnostics(
        host=url,
        alerts=_alerts({"suspended_status": 404, "webhook": url}, rules=[url, "daily_cost"]),
    )

    check = doctor.check_alerts(_answer(document))

    assert check.status == "fail"
    assert "rules on: daily_cost." in check.detail
    assert PLANTED not in _texts(check)
    assert "/webhooks/" not in _texts(check)
