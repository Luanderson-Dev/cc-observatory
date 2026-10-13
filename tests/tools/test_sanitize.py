"""Tests for the capture sanitizer, on synthetic data only (a fake person called jdoe)."""

from __future__ import annotations

import copy
import dataclasses
import getpass
import json
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.capture import sanitize as sanitize_module
from tools.capture.sanitize import (
    IDENTITY_PLACEHOLDERS,
    ContentError,
    DestinationError,
    LiteralConflictError,
    LocalContext,
    Names,
    Redactor,
    StableNames,
    StructureError,
    collect_identity_values,
    collect_names,
    encode_project_dir,
    filter_otlp,
    find_content,
    find_personal_data,
    git_identity,
    guard_literals,
    hook_records,
    redact_identity_attributes,
    replace_request_ids,
    reserved_conflict,
    sanitize,
    scrub_safety_net,
    short_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

HOME = "C:\\Users\\jdoe"
USER = "jdoe"
HOST = "JDOE-LAPTOP"
EMAIL = "jdoe@corp.invalid"
WORKSPACE = "C:\\Users\\jdoe\\AppData\\Local\\Temp\\scratch\\spike"
ACCOUNT_UUID = "6f1c2a9e-0d4b-4e1a-9a3c-2b7d8e9f0a1b"
ORG_ID = "9b2e4c6d-8f0a-4b1c-9d2e-3f4a5b6c7d8e"
ACCOUNT_ID = "user_01JDOEACCOUNT0000000000"
USER_ID = "ab" * 32
GROUPS = "eng-platform"
IDP = "okta"
REQ_A = "req_011CAbCdEfGhIjKlMnOpQrSt"
REQ_B = "req_011CZyXwVuTsRqPoNmLkJiHg"
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
THIRD_SESSION = "33333333-3333-4333-8333-333333333333"

CTX = LocalContext(home=HOME, user=USER, host=HOST, workspace_root=WORKSPACE)

FAKE_ACCOUNT_UUID = "00000000-0000-4000-8000-000000000001"
FAKE_ORG_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def no_machine_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real git identity and 8.3 short names out of the CLI tests."""
    monkeypatch.setattr(sanitize_module, "git_identity", list)
    monkeypatch.setattr(sanitize_module, "short_path", lambda path: None)


def kv(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def identity_attributes() -> list[dict[str, Any]]:
    return [
        kv("user.email", EMAIL),
        kv("user.account_uuid", ACCOUNT_UUID),
        kv("organization.id", ORG_ID),
        kv("user.account_id", ACCOUNT_ID),
        kv("user.id", USER_ID),
        kv("user.groups", GROUPS),
        kv("identity.source", IDP),
        kv("host.name", HOST),
    ]


def string_attributes(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    return {item["key"]: item["value"].get("stringValue") for item in attributes}


def log_record(session: str, *attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        "timeUnixNano": "1760318405000000000",
        "body": {"stringValue": "claude_code.api_request"},
        "attributes": [kv("session.id", session), *attributes],
    }


def logs_doc(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeLogs": [
                    {
                        "scope": {"name": "com.anthropic.claude_code.events"},
                        "logRecords": [*records],
                    }
                ],
            }
        ]
    }


def records_of(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for resource in doc["resourceLogs"]:
        for scope in resource["scopeLogs"]:
            yield from scope["logRecords"]


def point(session: str, value: float) -> dict[str, Any]:
    return {
        "attributes": [kv("session.id", session), kv("type", "input")],
        "asDouble": value,
        "timeUnixNano": "1760318405000000000",
    }


def metric(name: str, *points: dict[str, Any], kind: str = "sum") -> dict[str, Any]:
    data: dict[str, Any] = {"dataPoints": [*points], "aggregationTemporality": 1}
    return {"name": name, "unit": "tokens", kind: data}


def metrics_doc(*metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeMetrics": [
                    {"scope": {"name": "com.anthropic.claude_code"}, "metrics": [*metrics]}
                ],
            }
        ]
    }


def span(session: str, name: str, *attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": name,
        "kind": 1,
        "attributes": [kv("session.id", session), *attributes],
        "events": [{"name": "exception", "attributes": [kv("exception.type", "Error")]}],
    }


def traces_doc(*spans: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code"}, "spans": [*spans]}],
            }
        ]
    }


def redactor(identity: dict[str, str] | None = None, ctx: LocalContext = CTX) -> Redactor:
    return Redactor(ctx, Names(identity=identity or {}))


# Local context: home, workspace root, username and hostname.


def test_encode_project_dir_turns_every_non_alphanumeric_into_a_dash() -> None:
    assert encode_project_dir("C:\\Users\\jdoe\\my repo.v2") == "C--Users-jdoe-my-repo-v2"
    assert encode_project_dir("/home/jdoe/work") == "-home-jdoe-work"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\Users\\jdoe\\.claude\\CLAUDE.md", "C:\\Users\\user\\.claude\\CLAUDE.md"),
        ("C:/Users/jdoe/.claude/CLAUDE.md", "C:/Users/user/.claude/CLAUDE.md"),
        ("C:\\\\Users\\\\jdoe\\\\notes.txt", "C:\\\\Users\\\\user\\\\notes.txt"),
        ("/c/Users/jdoe/notes.txt", "/c/Users/user/notes.txt"),
        ("C--Users-jdoe-repo", "C--Users-user-repo"),
        ("c:\\users\\JDOE\\notes.txt", "C:\\Users\\user\\notes.txt"),
        ("C:\\Users\\jdoe", "C:\\Users\\user"),
    ],
)
def test_windows_home_is_replaced_in_every_variant(raw: str, expected: str) -> None:
    assert redactor().text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (WORKSPACE + "\\ws2", "C:\\Users\\user\\projects\\capture\\ws2"),
        (
            "C:/Users/jdoe/AppData/Local/Temp/scratch/spike/ws2/a.py",
            "C:/Users/user/projects/capture/ws2/a.py",
        ),
        (
            "C--Users-jdoe-AppData-Local-Temp-scratch-spike-ws2",
            "C--Users-user-projects-capture-ws2",
        ),
        (
            "C:\\Users\\jdoe\\.claude\\projects\\"
            "C--Users-jdoe-AppData-Local-Temp-scratch-spike-ws2\\abc.jsonl",
            "C:\\Users\\user\\.claude\\projects\\C--Users-user-projects-capture-ws2\\abc.jsonl",
        ),
        (
            "C:\\Users\\jdoe\\AppData\\Local\\Temp\\claude\\"
            "C--Users-jdoe-AppData-Local-Temp-scratch-spike-ws3\\abc\\scratchpad",
            "C:\\Users\\user\\AppData\\Local\\Temp\\claude\\"
            "C--Users-user-projects-capture-ws3\\abc\\scratchpad",
        ),
    ],
)
def test_workspace_root_maps_under_the_fake_home(raw: str, expected: str) -> None:
    assert redactor().text(raw) == expected


def test_workspace_root_given_with_forward_slashes_still_matches_backslash_paths() -> None:
    ctx = LocalContext(
        home=HOME, user=USER, host=HOST, workspace_root=WORKSPACE.replace("\\", "/") + "/"
    )

    assert redactor(ctx=ctx).text(WORKSPACE + "\\ws") == "C:\\Users\\user\\projects\\capture\\ws"


@pytest.mark.parametrize(
    ("home", "raw", "expected"),
    [
        ("/home/jdoe", "/home/jdoe/repo/a.py", "/home/user/repo/a.py"),
        ("/home/jdoe", "-home-jdoe-repo", "-home-user-repo"),
        ("/Users/jdoe", "/Users/jdoe/repo/a.py", "/home/user/repo/a.py"),
        ("/Users/jdoe", "-Users-jdoe-repo", "-home-user-repo"),
    ],
)
def test_posix_home_maps_to_the_fake_posix_home(home: str, raw: str, expected: str) -> None:
    ctx = LocalContext(home=home, user=USER, host=HOST)

    assert redactor(ctx=ctx).text(raw) == expected


def test_posix_workspace_root_maps_under_the_fake_posix_home() -> None:
    ctx = LocalContext(
        home="/home/jdoe", user=USER, host=HOST, workspace_root="/work/scratch/spike"
    )
    sanitizer = redactor(ctx=ctx)

    assert sanitizer.text("/work/scratch/spike/ws") == "/home/user/projects/capture/ws"
    assert sanitizer.text("-work-scratch-spike-ws") == "-home-user-projects-capture-ws"


def test_home_aliases_are_replaced_in_every_variant() -> None:
    ctx = LocalContext(home=HOME, user=USER, host=HOST, home_aliases=("C:\\Users\\JDOE~1",))
    sanitizer = redactor(ctx=ctx)

    assert sanitizer.text("C:\\Users\\JDOE~1\\AppData\\Local\\Temp\\x") == (
        "C:\\Users\\user\\AppData\\Local\\Temp\\x"
    )
    assert sanitizer.text("C--Users-JDOE-1-AppData-Local-Temp-x") == (
        "C--Users-user-AppData-Local-Temp-x"
    )


def test_workspace_root_under_the_home_also_matches_through_a_home_alias() -> None:
    ctx = LocalContext(
        home="C:\\Users\\jdoe.person",
        user="jdoe.person",
        host=HOST,
        workspace_root="C:\\Users\\jdoe.person\\AppData\\Local\\Temp\\spike",
        home_aliases=("C:\\Users\\JDOE~1.PER",),
    )

    assert redactor(ctx=ctx).text("C:\\Users\\JDOE~1.PER\\AppData\\Local\\Temp\\spike\\ws") == (
        "C:\\Users\\user\\projects\\capture\\ws"
    )


def test_short_path_is_none_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert short_path("C:\\Users\\jdoe") is None


@pytest.mark.parametrize(
    ("api_result", "expected"),
    [
        ("C:\\Users\\JDOE~1.PER", "C:\\Users\\JDOE~1.PER"),
        ("C:\\Users\\jdoe.person", None),
        ("c:\\users\\JDOE.PERSON", None),
        ("", None),
    ],
)
def test_short_path_on_windows_returns_only_a_different_short_name(
    monkeypatch: pytest.MonkeyPatch, api_result: str, expected: str | None
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sanitize_module, "_short_path_name", lambda path: api_result)

    assert short_path("C:\\Users\\jdoe.person") == expected


def test_username_and_hostname_are_replaced_longest_first() -> None:
    text = "jdoe ran it on JDOE-LAPTOP (jdoe-laptop.corp)"

    assert redactor().text(text) == "user ran it on example-host (example-host)"


def test_username_inside_longer_words_replaces_the_whole_token() -> None:
    text = "by Jdoerson-Dev at github.com/Jdoerson-Dev/repo, then jdoe2 and jdoe."

    assert redactor().text(text) == "by user at github.com/user/repo, then user and user."


def test_hostname_inside_longer_words_replaces_the_whole_token() -> None:
    assert redactor().text("built on JDOE-LAPTOP2.corp.invalid") == "built on example-host"


def test_short_usernames_only_match_whole_words() -> None:
    ctx = LocalContext(home="C:\\Users\\al", user="al", host=HOST)

    assert redactor(ctx=ctx).text("al also met al-b") == "user also met user-b"
    assert find_personal_data("also", {"username": "al"}) == []
    assert [f.kind for f in find_personal_data("al", {"username": "al"})] == ["username"]


def test_forbidden_texts_are_replaced_with_their_surrounding_token() -> None:
    ctx = LocalContext(
        home=HOME,
        user=USER,
        host=HOST,
        forbid=("Jdoe Q. Person", "acme-secret", "jdoe.work@corp.invalid"),
    )
    text = "Author: Jdoe Q. Person <jdoe.work@corp.invalid> on the acme-secret-project"

    assert redactor(ctx=ctx).text(text) == "Author: redacted <user@example.com> on the redacted"
    assert {
        "forbidden text 1": "Jdoe Q. Person",
        "forbidden text 2": "acme-secret",
        "forbidden text 3": "jdoe.work@corp.invalid",
    }.items() <= guard_literals(ctx, Names()).items()


def test_git_identity_reads_user_name_and_email(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    values = {"user.name": "Jdoe Person\n", "user.email": "jdoe@corp.invalid\n"}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=values[args[-1]], stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_identity() == ["Jdoe Person", "jdoe@corp.invalid"]
    assert [args for args, _ in calls] == [
        ["/usr/bin/git", "config", "--get", "user.name"],
        ["/usr/bin/git", "config", "--get", "user.email"],
    ]
    assert all(isinstance(kwargs.get("timeout"), int | float) for _, kwargs in calls)


def test_git_identity_skips_unset_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-1] == "user.name":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="jdoe@corp.invalid\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_identity() == ["jdoe@corp.invalid"]


def test_git_identity_is_empty_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert git_identity() == []


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["git"], 5), FileNotFoundError("git")])
def test_git_identity_is_empty_when_git_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_identity() == []


# Identity attributes.


def test_identity_placeholders_are_fixed_fakes() -> None:
    assert IDENTITY_PLACEHOLDERS == {
        "user.email": "user@example.com",
        "user.account_uuid": FAKE_ACCOUNT_UUID,
        "organization.id": FAKE_ORG_ID,
        "user.account_id": "user_01EXAMPLE",
        "user.id": "0" * 64,
        "user.groups": "example-group",
        "identity.source": "example-idp",
        "host.name": "example-host",
    }


def test_identity_attributes_are_replaced_by_key_wherever_they_appear() -> None:
    doc = traces_doc(span(SESSION, "claude_code.llm_request", *identity_attributes()))
    doc["resourceSpans"][0]["resource"]["attributes"].extend(identity_attributes())
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"][0]["attributes"].extend(
        identity_attributes()
    )
    original = copy.deepcopy(doc)

    out: Any = redact_identity_attributes(doc)

    resource = out["resourceSpans"][0]
    out_span = resource["scopeSpans"][0]["spans"][0]
    for attributes in (
        resource["resource"]["attributes"],
        out_span["attributes"],
        out_span["events"][0]["attributes"],
    ):
        values = string_attributes(attributes)
        for key, placeholder in IDENTITY_PLACEHOLDERS.items():
            assert values[key] == placeholder
    assert string_attributes(out_span["attributes"])["session.id"] == SESSION
    assert doc == original


def test_identity_array_values_keep_their_shape() -> None:
    groups = {
        "key": "user.groups",
        "value": {"arrayValue": {"values": [{"stringValue": "eng"}, {"stringValue": "ops"}]}},
    }
    doc = logs_doc(log_record(SESSION, groups))

    out: Any = redact_identity_attributes(doc)

    (record,) = records_of(out)
    assert record["attributes"][1] == {
        "key": "user.groups",
        "value": {
            "arrayValue": {
                "values": [{"stringValue": "example-group"}, {"stringValue": "example-group"}]
            }
        },
    }


def test_collect_identity_values_maps_real_values_to_placeholders() -> None:
    docs = [logs_doc(log_record(SESSION, *identity_attributes()))]

    assert collect_identity_values(docs) == {
        EMAIL: "user@example.com",
        ACCOUNT_UUID: FAKE_ACCOUNT_UUID,
        ORG_ID: FAKE_ORG_ID,
        ACCOUNT_ID: "user_01EXAMPLE",
        USER_ID: "0" * 64,
        GROUPS: "example-group",
        IDP: "example-idp",
        HOST: "example-host",
    }


def test_identity_values_are_also_replaced_in_any_other_string() -> None:
    identity = collect_identity_values([logs_doc(log_record(SESSION, *identity_attributes()))])
    payload: dict[str, Any] = {
        "compact_summary": f"User email is {EMAIL}. Account {ACCOUNT_UUID} in org {ORG_ID}.",
        "notes": [f"{ACCOUNT_ID} / {USER_ID} via {IDP}"],
    }

    assert redactor(identity).value(payload) == {
        "compact_summary": (
            f"User email is user@example.com. Account {FAKE_ACCOUNT_UUID} in org {FAKE_ORG_ID}."
        ),
        "notes": [f"user_01EXAMPLE / {'0' * 64} via example-idp"],
    }


def test_the_domain_of_a_collected_user_email_is_replaced_everywhere() -> None:
    names = collect_names([logs_doc(log_record(SESSION, *identity_attributes()))], [])

    assert Redactor(CTX, names).text("see https://wiki.corp.invalid/page and bob@corp.invalid") == (
        "see https://example.com/page and bob@example.com"
    )
    assert guard_literals(CTX, names)["email domain 1"] == "corp.invalid"


# Request IDs, plugins, marketplaces, skills and MCP servers.


def test_request_ids_map_to_stable_fakes() -> None:
    names = StableNames("req_EXAMPLE{:04d}")

    text = replace_request_ids(f"{REQ_A} then {REQ_B} then {REQ_A}", names)

    assert text == "req_EXAMPLE0001 then req_EXAMPLE0002 then req_EXAMPLE0001"
    assert replace_request_ids(f"again {REQ_B}", names) == "again req_EXAMPLE0002"
    assert replace_request_ids("already req_EXAMPLE0001", names) == "already req_EXAMPLE0001"


def test_request_ids_after_an_underscore_are_replaced() -> None:
    names = StableNames("req_EXAMPLE{:04d}")

    assert replace_request_ids(f"x_{REQ_A}", names) == "x_req_EXAMPLE0001"


def test_request_ids_stay_consistent_across_hooks_and_otlp() -> None:
    sanitizer = redactor()

    hook = sanitizer.value({"error": f"API error (request {REQ_B})"})
    doc: Any = sanitizer.otlp(
        logs_doc(
            log_record(SESSION, kv("request_id", REQ_A)),
            log_record(SESSION, kv("request_id", REQ_B)),
        )
    )

    assert hook == {"error": "API error (request req_EXAMPLE0001)"}
    assert [string_attributes(r["attributes"])["request_id"] for r in records_of(doc)] == [
        "req_EXAMPLE0002",
        "req_EXAMPLE0001",
    ]


def test_plugin_and_marketplace_names_get_stable_placeholders() -> None:
    doc = logs_doc(
        log_record(SESSION, kv("plugin.name", "acme-tools"), kv("marketplace.name", "acme-market")),
        log_record(
            SESSION, kv("plugin.name", "other-plugin"), kv("marketplace.name", "acme-market")
        ),
        log_record(SESSION, kv("plugin.name", "acme-tools"), kv("marketplace.name", "builtin")),
    )

    out: Any = Redactor(CTX, collect_names([doc], [])).otlp(doc)

    names = [string_attributes(r["attributes"]) for r in records_of(out)]
    assert [(n["plugin.name"], n["marketplace.name"]) for n in names] == [
        ("plugin-1", "marketplace-1"),
        ("plugin-2", "marketplace-1"),
        ("plugin-1", "marketplace-2"),
    ]


def test_plugin_marketplace_skill_and_mcp_names_are_replaced_everywhere() -> None:
    doc = logs_doc(
        log_record(
            SESSION, kv("plugin.name", "acme-private"), kv("marketplace.name", "acme-corp-market")
        ),
        log_record(SESSION, kv("skill.name", "acme-private:deploy")),
        log_record(SESSION, kv("skill.name", "simplify")),
        log_record(SESSION, kv("skill.name", "my-secret-skill")),
        log_record(SESSION, kv("mcp_server_name", "corp-db")),
        log_record(SESSION, kv("mcp_server_name", "spike-elicit")),
    )
    hooks: list[Any] = [
        {"tool_name": "mcp__plugin_acme-private_srv__run"},
        {"tool_name": "mcp__spike-elicit__pick_color"},
        {"file": "C:\\Users\\user\\.claude\\plugins\\cache\\other-market\\other-plugin\\1.0.0"},
    ]

    names = collect_names([doc], hooks)

    assert names == Names(
        plugins=("acme-private", "other-plugin"),
        marketplaces=("acme-corp-market", "other-market"),
        skills=("acme-private:deploy", "my-secret-skill"),
        mcp_servers=("corp-db", "plugin_acme-private_srv"),
    )
    sanitizer = Redactor(CTX, names)
    assert sanitizer.text("acme-private:deploy") == "skill-1"
    assert sanitizer.text("my-secret-skill") == "skill-2"
    assert sanitizer.text("simplify") == "simplify"
    assert sanitizer.text("mcp__corp-db__query") == "mcp__mcp-server-1__query"
    assert sanitizer.text("mcp__plugin_acme-private_srv__run") == "mcp__mcp-server-2__run"
    assert sanitizer.text("mcp__spike-elicit__pick_color") == "mcp__spike-elicit__pick_color"
    assert sanitizer.text(".claude/plugins/cache/acme-corp-market/acme-private/1.0.0") == (
        ".claude/plugins/cache/marketplace-1/plugin-1/1.0.0"
    )
    assert {
        "plugin name 1": "acme-private",
        "marketplace name 1": "acme-corp-market",
        "skill name 1": "acme-private:deploy",
        "MCP server name 1": "corp-db",
    }.items() <= guard_literals(CTX, names).items()


def test_plain_word_names_are_only_replaced_where_they_are_used_as_names() -> None:
    names = Names(
        plugins=("superpowers",),
        marketplaces=("acme-market",),
        skills=("commit",),
        mcp_servers=("git", "time", "memory"),
    )
    sanitizer = Redactor(CTX, names)

    text = "git commit -m 'time to save memory'"
    assert sanitizer.text(text) == text
    assert sanitizer.text("mcp__git__status") == "mcp__mcp-server-1__status"
    assert sanitizer.text(".claude/plugins/cache/acme-market/superpowers/4.0.3/skills/commit") == (
        ".claude/plugins/cache/marketplace-1/plugin-1/4.0.3/skills/skill-1"
    )
    payload: dict[str, Any] = {
        "mcp_server_name": "time",
        "tool_input": {"skill": "commit", "command": "git"},
    }
    assert sanitizer.value(payload) == {
        "mcp_server_name": "mcp-server-2",
        "tool_input": {"skill": "skill-1", "command": "git"},
    }
    doc: Any = sanitizer.otlp(
        logs_doc(log_record(SESSION, kv("mcp_server_name", "memory"), kv("skill.name", "commit")))
    )
    (record,) = records_of(doc)
    attributes = string_attributes(record["attributes"])
    assert (attributes["mcp_server_name"], attributes["skill.name"]) == ("mcp-server-3", "skill-1")


def test_guard_looks_for_plain_word_names_only_where_they_are_used_as_names() -> None:
    literals = guard_literals(CTX, Names(skills=("commit",), mcp_servers=("git",)))

    assert find_personal_data({"command": "git commit", "timeUnixNano": "1"}, literals) == []
    findings = find_personal_data({"tool_name": "mcp__git__status", "skill": "commit"}, literals)
    assert [f.kind for f in findings] == ["MCP server name 1", "skill name 1"]
    doc = logs_doc(log_record(SESSION, kv("mcp_server_name", "git")))
    assert [f.kind for f in find_personal_data(doc, literals)] == ["MCP server name 1"]


def test_names_and_token_matching_never_rewrite_keys() -> None:
    names = Names(mcp_servers=("time", "memory"))
    payload: dict[str, Any] = {
        "timeUnixNano": "1",
        "startTimeUnixNano": "2",
        "timeout": 3,
        "memory": 4,
        "jdoe2": 5,
    }

    assert Redactor(CTX, names).value(payload) == payload


def test_key_collisions_raise_without_showing_the_keys() -> None:
    payload: dict[str, Any] = {"files": {"C:\\Users\\jdoe\\a.txt": 1, "C:\\Users\\user\\a.txt": 2}}

    with pytest.raises(StructureError, match=r"\$\.files") as excinfo:
        redactor().value(payload)

    assert "jdoe" not in str(excinfo.value)


def test_id_shaped_tokens_are_left_alone() -> None:
    ctx = LocalContext(home="C:\\Users\\cafe", user="cafe", host=HOST)
    text = (
        "trace 0af7651916cdcafe8448eb211c80319c span b7ad6b71cafe3331"
        " session 6f1c2a9e-cafe-4e1a-9a3c-2b7d8e9f0a1b tool toolu_01CafeXyzAbcDefGhiJklMno"
    )

    assert redactor(ctx=ctx).text(text) == text
    assert redactor(ctx=ctx).text("cafe-bar and cafe") == "user and user"
    assert find_personal_data(text, guard_literals(ctx, Names())) == []
    uuid = "6f1c2a9e-dead-4e1a-9a3c-2b7d8e9f0a1b"
    assert Redactor(CTX, Names(identity={"dead": "example-group"})).text(uuid) == uuid


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"user": "host"}, "username"),
        ({"user": "Temp"}, "username"),
        ({"host": "example"}, "hostname"),
        ({"forbid": ("laude",)}, "forbidden text 1"),
    ],
)
def test_literals_inside_placeholders_or_allowed_path_words_are_refused(
    changes: dict[str, Any], label: str
) -> None:
    with pytest.raises(LiteralConflictError) as excinfo:
        Redactor(dataclasses.replace(CTX, **changes), Names())

    message = str(excinfo.value)
    assert label in message
    assert "--user ''" in message


def test_collected_values_inside_placeholders_are_dropped() -> None:
    names = Names(identity={"users": "example-group"}, mcp_servers=("skill-1",))

    literals = guard_literals(CTX, names)

    assert not any(label.startswith(("identity value", "MCP server name")) for label in literals)
    home = "C:\\Users\\user\\AppData\\Local\\Temp"
    assert Redactor(CTX, names).text(home) == home


def test_reserved_conflict_spots_texts_inside_placeholders() -> None:
    assert reserved_conflict("Claude")
    assert reserved_conflict("host")
    assert not reserved_conflict("Jdoe Person")


def test_forbidden_texts_match_with_their_original_case_rules() -> None:
    ctx = dataclasses.replace(CTX, forbid=("Weiß",))

    assert redactor(ctx=ctx).text("Herr Weiß kommt") == "Herr redacted kommt"
    findings = find_personal_data("Herr Weiß", guard_literals(ctx, Names()))
    assert [f.kind for f in findings] == ["forbidden text 1"]


# Safety net and what is kept.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mail alice@corp.invalid now", "mail user@example.com now"),
        ("keep bob@example.com", "keep bob@example.com"),
        ("npm react@18.3.1", "npm react@18.3.1"),
        ("D:\\Users\\alice\\repo", "C:\\Users\\user\\repo"),
        ("e:/Users/alice/repo", "C:/Users/user/repo"),
        ("C:\\\\Users\\\\alice\\\\repo", "C:\\\\Users\\\\user\\\\repo"),
        ("/Users/alice/repo", "/home/user/repo"),
        ("/home/carol/repo", "/home/user/repo"),
        ("C:\\Users\\user\\repo and /home/user", "C:\\Users\\user\\repo and /home/user"),
        ("/c/users/alice/repo", "/c/Users/user/repo"),
        ("/HOME/carol/repo", "/home/user/repo"),
        ("C:\\Users\\John Smith\\repo", "C:\\Users\\user\\repo"),
        ("C:\\Users\\John Smith", "C:\\Users\\user"),
        ("C:\\Users\\John Smith, then", "C:\\Users\\user, then"),
        ("cd C:\\Users\\alice && ls", "cd C:\\Users\\user && ls"),
        ("C:\\Users\\user and more", "C:\\Users\\user and more"),
        ("see C:\\Users\\alice and C:\\Users\\bob", "see C:\\Users\\user and C:\\Users\\user"),
        ("C--Users-alice-repo", "C--Users-user-repo"),
        ("D--Users-alice", "C--Users-user"),
        ("projects/-home-carol-work", "projects/-home-user-work"),
        ("-Users-bob-repo", "-home-user-repo"),
        ("nav-home-button", "nav-home-button"),
        ("C:\\Users/alice/x", "C:\\Users/user/x"),
        ("C:/Users\\alice\\x", "C:/Users\\user\\x"),
        ("C:\\Users\\O'Brien\\x", "C:\\Users\\user\\x"),
        ("'C:\\Users\\alice'", "'C:\\Users\\user'"),
        ("/mnt/c/Users/alice/x", "/mnt/c/Users/user/x"),
        ("file:///C%3A%5CUsers%5Calice%5Cx", "file:///C%3A%5CUsers%5Cuser%5Cx"),
        ("c%3a%2fusers%2fbob%2fx", "c%3a%2fusers%2fuser%2fx"),
        (
            "https://api.github.com/users/octocat/repos",
            "https://api.github.com/users/octocat/repos",
        ),
        ("https://example.org/home/index.html", "https://example.org/home/index.html"),
        ("file:///home/carol/x", "file:///home/user/x"),
    ],
)
def test_safety_net(raw: str, expected: str) -> None:
    assert scrub_safety_net(raw) == expected


def test_safety_net_is_linear_on_long_tokens() -> None:
    # A quadratic email pattern took minutes on a long run of letters (a base64 blob).
    text = "a" * 200_000

    started = time.perf_counter()
    assert scrub_safety_net(text) == text
    assert redactor().text(text) == text

    assert time.perf_counter() - started < 2


def test_redactor_applies_the_safety_net() -> None:
    assert redactor().text("ping alice@corp.invalid in /home/carol") == (
        "ping user@example.com in /home/user"
    )


def test_redactor_keeps_ids_models_numbers_and_content() -> None:
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "model": "claude-haiku-5-5",
        "duration_ms": 12,
        "cost": 0.25,
        "ok": True,
        "missing": None,
        "tool_input": {"command": "echo hello > created.txt"},
    }

    assert redactor().value(payload) == payload


def test_redactor_redacts_keys_too() -> None:
    assert redactor().value({"C:\\Users\\jdoe\\a.txt": 1}) == {"C:\\Users\\user\\a.txt": 1}


# OTLP filtering by session.


@pytest.mark.parametrize("kind", ["sum", "gauge", "histogram"])
def test_filter_otlp_keeps_the_sessions_datapoints_and_drops_empty_metrics(kind: str) -> None:
    doc = metrics_doc(
        metric("claude_code.token.usage", point(SESSION, 1), point(OTHER_SESSION, 2), kind=kind),
        metric("claude_code.cost.usage", point(OTHER_SESSION, 3), kind=kind),
    )
    original = copy.deepcopy(doc)

    out: Any = filter_otlp(doc, SESSION)

    assert out is not None
    (resource,) = out["resourceMetrics"]
    (scope,) = resource["scopeMetrics"]
    (kept,) = scope["metrics"]
    assert kept["name"] == "claude_code.token.usage"
    assert kept["unit"] == "tokens"
    assert [p["asDouble"] for p in kept[kind]["dataPoints"]] == [1]
    assert kept[kind]["aggregationTemporality"] == 1
    assert resource["resource"] == original["resourceMetrics"][0]["resource"]
    assert doc == original


def test_filter_otlp_returns_none_when_nothing_matches() -> None:
    assert filter_otlp(metrics_doc(metric("m", point(OTHER_SESSION, 1))), SESSION) is None


def test_filter_otlp_prunes_empty_scopes_and_resources_in_logs() -> None:
    mine = log_record(SESSION, kv("event.name", "user_prompt"))
    doc: dict[str, Any] = {
        "resourceLogs": [
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeLogs": [
                    {"scope": {"name": "a"}, "logRecords": [mine]},
                    {"scope": {"name": "b"}, "logRecords": [log_record(OTHER_SESSION)]},
                ],
            },
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeLogs": [{"scope": {"name": "c"}, "logRecords": [log_record(OTHER_SESSION)]}],
            },
        ]
    }

    assert filter_otlp(doc, SESSION) == {
        "resourceLogs": [
            {
                "resource": {"attributes": [kv("service.name", "claude-code")]},
                "scopeLogs": [{"scope": {"name": "a"}, "logRecords": [mine]}],
            }
        ]
    }


def test_filter_otlp_keeps_the_sessions_spans_with_their_events() -> None:
    mine = span(SESSION, "claude_code.interaction")
    doc = traces_doc(mine, span(OTHER_SESSION, "claude_code.interaction"))

    out: Any = filter_otlp(doc, SESSION)

    assert out is not None
    assert out["resourceSpans"][0]["scopeSpans"][0]["spans"] == [mine]


def test_filter_otlp_falls_back_to_the_resource_session() -> None:
    record = {"timeUnixNano": "1", "attributes": [kv("event.name", "user_prompt")]}
    doc: dict[str, Any] = {
        "resourceLogs": [
            {
                "resource": {"attributes": [kv("session.id", SESSION)]},
                "scopeLogs": [{"scope": {"name": "a"}, "logRecords": [record]}],
            }
        ]
    }

    assert filter_otlp(doc, SESSION) == doc
    assert filter_otlp(doc, OTHER_SESSION) is None


def test_filter_otlp_rejects_unknown_documents() -> None:
    with pytest.raises(ValueError, match="OTLP"):
        filter_otlp({"something": []}, SESSION)


# Content-bearing telemetry.


@pytest.mark.parametrize(
    ("doc", "kind"),
    [
        (
            logs_doc(log_record(SESSION, kv("event.name", "api_request_body"), kv("body", "x"))),
            "content event api_request_body",
        ),
        (logs_doc(log_record(SESSION, kv("event.name", "system_prompt"))), "content event"),
        (
            logs_doc(
                {
                    "eventName": "claude_code.api_response_body",
                    "attributes": [kv("session.id", SESSION)],
                }
            ),
            "content event api_response_body",
        ),
        (
            logs_doc(log_record(SESSION, kv("event.name", "user_prompt"), kv("prompt", "secret"))),
            "content attribute prompt",
        ),
        (
            traces_doc(span(SESSION, "claude_code.tool", kv("tool_input", '{"cmd": "ls"}'))),
            "content attribute tool_input",
        ),
        (
            traces_doc(
                {
                    "name": "claude_code.tool",
                    "attributes": [kv("session.id", SESSION)],
                    "events": [{"name": "tool.output", "attributes": [kv("output", "secret")]}],
                }
            ),
            "content span event tool.output",
        ),
    ],
)
def test_find_content_reports_content_bearing_telemetry(doc: dict[str, Any], kind: str) -> None:
    findings = find_content(doc)

    assert any(f.kind.startswith(kind) for f in findings), findings
    assert all("secret" not in str(f) for f in findings)


def test_find_content_accepts_redacted_and_empty_values() -> None:
    doc = logs_doc(
        log_record(
            SESSION,
            kv("event.name", "user_prompt"),
            kv("prompt", "<REDACTED>"),
            kv("tool_parameters", ""),
            kv("prompt_length", "12"),
        )
    )

    assert find_content(doc) == []


# Hook records.


def hook(received_at: str, path: str, session: str, event: str) -> dict[str, Any]:
    return {
        "received_at": received_at,
        "path": path,
        "mode": "ok",
        "headers": {"content-type": "application/json", "user-agent": "claude-code"},
        "body": {"session_id": session, "hook_event_name": event},
    }


def test_hook_records_keep_one_session_in_arrival_order_with_offsets_and_transport() -> None:
    records = [
        hook("2026-10-13T01:20:05.000000+00:00", "/hooks/command", SESSION, "SessionStart"),
        hook("2026-10-13T01:20:05.100000+00:00", "/hooks/http", OTHER_SESSION, "Stop"),
        hook("2026-10-13T01:20:06.234567+00:00", "/hooks/http", SESSION, "UserPromptSubmit"),
        hook("2026-10-13T01:20:06.200000+00:00", "/hooks/http", SESSION, "PreToolUse"),
        {
            "received_at": "2026-10-13T01:20:07.000000+00:00",
            "path": "/hooks/http",
            "mode": "ok",
            "headers": {},
            "body": {"_raw": "not json"},
        },
    ]

    assert hook_records(records, SESSION) == [
        {
            "offset_ms": 0,
            "transport": "command",
            "payload": {"session_id": SESSION, "hook_event_name": "SessionStart"},
        },
        {
            "offset_ms": 1200,
            "transport": "http",
            "payload": {"session_id": SESSION, "hook_event_name": "PreToolUse"},
        },
        {
            "offset_ms": 1235,
            "transport": "http",
            "payload": {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"},
        },
    ]
    assert hook_records(records, THIRD_SESSION) == []


def test_hook_records_reject_an_unknown_transport() -> None:
    records = [hook("2026-10-13T01:20:05+00:00", "/elsewhere", SESSION, "Stop")]

    with pytest.raises(ValueError, match="transport"):
        hook_records(records, SESSION)


# The personal data guard.

LITERALS = {"username": USER, "hostname": HOST, "account uuid": ACCOUNT_UUID}


def test_guard_finds_nothing_in_sanitized_data() -> None:
    value: dict[str, Any] = {
        "cwd": "C:\\Users\\user\\projects\\capture\\ws",
        "transcript_path": (
            "C:\\Users\\user\\.claude\\projects\\C--Users-user-projects-capture-ws\\a.jsonl"
        ),
        "temp": "C:\\Users\\user\\AppData\\Local\\Temp\\claude\\C--Users-user-projects-capture-ws",
        "config": "C:/Users/user/.claude.json",
        "home": "C:\\Users\\user",
        "posix": "/home/user/projects/capture/ws",
        "posix_projects_dir": "-home-user-projects-capture-ws",
        "posix_claude_dir": "-home-user--claude-x",
        "msys": "/c/Users/user/projects/capture/repo",
        "words": "nav-home-button and a-Users-list",
        "urls": ["https://api.github.com/users/octocat/repos", "https://example.org/home/a.html"],
        "encoded": "file:///C%3A%5CUsers%5Cuser%5Cprojects",
        "request": "req_EXAMPLE0001",
        "emails": ["user@example.com", "bob@example.com"],
        "numbers": [1, 2.5, True, None],
    }

    assert find_personal_data(value, LITERALS) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("contact jdoe@corp.invalid", "email"),
        ("D:\\Users\\alice\\repo", "user path"),
        ("C:/Users/alice/repo", "user path"),
        ("C:\\\\Users\\\\alice", "user path"),
        ("C:\\Users\\John Smith\\repo", "user path"),
        ("C--Users-alice-repo", "user path"),
        ("-Users-bob-repo", "user path"),
        ("projects/-home-carol-work/a.jsonl", "user path"),
        ("/Users/bob/repo", "user path"),
        ("/c/users/alice/repo", "user path"),
        ("/home/carol", "user path"),
        ("C:\\Users/alice/x", "user path"),
        ("C:/Users\\alice\\x", "user path"),
        ("C:\\Users\\user'Brien\\x", "user path"),
        ("C:\\Users\\user alice\\x", "user path"),
        ("/mnt/c/Users/alice/x", "user path"),
        ("file:///C%3A%5CUsers%5Calice%5Cx", "user path"),
        ("C:/Users/user/projects/capture/../../Documents/x", "path outside workspace"),
        ("/home/user/.claude/../Documents/x", "path outside workspace"),
        ("C:\\Users\\user\\OneDrive - Acme Corp\\x", "path outside workspace"),
        ("C:/Users/user/Documents/x", "path outside workspace"),
        ("/c/Users/user/Desktop/x", "path outside workspace"),
        ("/home/user/Documents/x", "path outside workspace"),
        ("C--Users-user-Documents-x", "path outside workspace"),
        ("-home-user-Documents-x", "path outside workspace"),
        (f"trace {REQ_A}", "request id"),
        ("built on jdoe-laptop", "hostname"),
        ("built on JDOE-LAPTOP2.corp", "hostname"),
        ("hi JDOE", "username"),
        ("Jdoerson-Dev", "username"),
        ("github.com/Jdoerson-Dev/repo", "username"),
        ("jdoe2", "username"),
        (f"account {ACCOUNT_UUID.upper()}", "account uuid"),
    ],
)
def test_guard_reports_personal_data_by_kind_and_location(text: str, kind: str) -> None:
    findings = find_personal_data({"payload": {"items": ["fine", text]}}, LITERALS)

    assert (kind, "$.payload.items[1]") in [(f.kind, f.location) for f in findings]


def test_guard_checks_keys_without_echoing_them() -> None:
    findings = find_personal_data({"outer": {"D:\\Users\\alice": "x"}}, {})

    assert [(f.kind, f.location) for f in findings] == [("user path", "$.outer (key)")]


def test_guard_findings_never_include_the_value() -> None:
    findings = find_personal_data({"k": f"mail {EMAIL} from {HOST}"}, LITERALS)

    assert {f.kind for f in findings} == {"email", "username", "hostname"}
    assert all("jdoe" not in str(f).lower() for f in findings)


# End to end.


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_raw(raw: Path) -> None:
    raw.mkdir()
    cwd = WORKSPACE + "\\ws"
    transcript = (
        "C:\\Users\\jdoe\\.claude\\projects\\C--Users-jdoe-AppData-Local-Temp-scratch-spike-ws\\"
        + SESSION
        + ".jsonl"
    )
    manifest = {
        "claude_code_version": "2.1.296",
        "collector_version": "0.162.0",
        "captured_on": "2026-10-12",
        "platform": "windows",
        "scenarios": [
            {
                "name": "headless-basic",
                "session_id": SESSION,
                "hooks": "hooks-run.jsonl",
                "otlp": True,
                "description": f"Ran in {cwd}.",
            },
            {
                "name": "headless-hooks-only",
                "session_id": OTHER_SESSION,
                "hooks": "hooks-run.jsonl",
                "otlp": False,
                "description": "Hooks only.",
            },
            {
                "name": "endpoint-down",
                "session_id": THIRD_SESSION,
                "hooks": None,
                "otlp": True,
                "description": "No hook server.",
            },
        ],
    }
    (raw / "scenarios.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def body(session: str, event: str, **extra: Any) -> dict[str, Any]:
        return {
            "session_id": session,
            "transcript_path": transcript,
            "cwd": cwd,
            "hook_event_name": event,
            **extra,
        }

    hooks = [
        ("2026-10-13T01:20:05.000000+00:00", "/hooks/command", body(SESSION, "SessionStart")),
        (
            "2026-10-13T01:20:05.500000+00:00",
            "/hooks/http",
            body(OTHER_SESSION, "UserPromptSubmit", prompt="Reply with the word ok."),
        ),
        (
            "2026-10-13T01:20:06.250000+00:00",
            "/hooks/http",
            body(
                SESSION,
                "UserPromptSubmit",
                prompt=f"Read {cwd}\\notes.txt as {USER} (Jdoerson-Dev)",
            ),
        ),
        (
            "2026-10-13T01:20:09.000000+00:00",
            "/hooks/http",
            body(
                SESSION,
                "StopFailure",
                error=f"API error ({REQ_A}) for {EMAIL} via wiki.corp.invalid",
            ),
        ),
        ("2026-10-13T01:20:09.500000+00:00", "/hooks/http", body(THIRD_SESSION, "Stop")),
    ]
    write_jsonl(
        raw / "hooks-run.jsonl",
        [
            {
                "received_at": received,
                "path": path,
                "mode": "ok",
                "headers": {"content-type": "application/json", "user-agent": HOST},
                "body": payload,
            }
            for received, path, payload in hooks
        ],
    )
    metrics = metrics_doc(
        metric("claude_code.token.usage", point(SESSION, 10), point(THIRD_SESSION, 20)),
        metric("claude_code.session.count", point(OTHER_SESSION, 1)),
    )
    for item in metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"]:
        item["attributes"].extend(identity_attributes())
    write_jsonl(raw / "otlp-metrics.jsonl", [metrics])
    write_jsonl(
        raw / "otlp-logs.jsonl",
        [
            logs_doc(
                log_record(
                    SESSION,
                    kv("event.name", "api_request"),
                    kv("request_id", REQ_A),
                    kv("body_ref", f"{HOME}\\.claude\\bodies\\{REQ_A}.json"),
                    *identity_attributes(),
                ),
                log_record(THIRD_SESSION, kv("event.name", "api_request"), kv("request_id", REQ_B)),
            ),
            logs_doc(
                log_record(
                    SESSION,
                    kv("event.name", "plugin_loaded"),
                    kv("plugin.name", "acme-tools"),
                    kv("marketplace.name", "acme-market"),
                )
            ),
        ],
    )
    write_jsonl(
        raw / "otlp-traces.jsonl",
        [
            traces_doc(
                span(SESSION, "claude_code.llm_request", kv("request_id", REQ_A)),
                span(THIRD_SESSION, "claude_code.llm_request", kv("request_id", REQ_B)),
                span(OTHER_SESSION, "claude_code.interaction", kv("host.name", HOST)),
            )
        ],
    )


def cli_args(raw: Path, dest: Path) -> list[str]:
    return [
        "--raw",
        str(raw),
        "--dest",
        str(dest),
        "--workspace-root",
        WORKSPACE,
        "--home",
        HOME,
        "--user",
        USER,
        "--host",
        HOST,
    ]


def test_cli_writes_sanitized_fixtures_per_scenario(tmp_path: Path) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)

    assert sanitize_module.main(cli_args(raw, dest)) == 0

    written = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert written == [
        "hooks/headless-basic.jsonl",
        "hooks/headless-hooks-only.jsonl",
        "manifest.json",
        "otlp/endpoint-down/logs.jsonl",
        "otlp/endpoint-down/metrics.jsonl",
        "otlp/endpoint-down/traces.jsonl",
        "otlp/headless-basic/logs.jsonl",
        "otlp/headless-basic/metrics.jsonl",
        "otlp/headless-basic/traces.jsonl",
    ]
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "claude_code_version": "2.1.296",
        "collector_version": "0.162.0",
        "captured_on": "2026-10-12",
        "platform": "windows",
        "scenarios": [
            {
                "name": "headless-basic",
                "session_id": SESSION,
                "description": "Ran in C:\\Users\\user\\projects\\capture\\ws.",
                "files": [
                    "hooks/headless-basic.jsonl",
                    "otlp/headless-basic/metrics.jsonl",
                    "otlp/headless-basic/logs.jsonl",
                    "otlp/headless-basic/traces.jsonl",
                ],
            },
            {
                "name": "headless-hooks-only",
                "session_id": OTHER_SESSION,
                "description": "Hooks only.",
                "files": ["hooks/headless-hooks-only.jsonl"],
            },
            {
                "name": "endpoint-down",
                "session_id": THIRD_SESSION,
                "description": "No hook server.",
                "files": [
                    "otlp/endpoint-down/metrics.jsonl",
                    "otlp/endpoint-down/logs.jsonl",
                    "otlp/endpoint-down/traces.jsonl",
                ],
            },
        ],
    }

    hooks = read_jsonl(dest / "hooks" / "headless-basic.jsonl")
    assert [(h["offset_ms"], h["transport"], h["payload"]["hook_event_name"]) for h in hooks] == [
        (0, "command", "SessionStart"),
        (1250, "http", "UserPromptSubmit"),
        (4000, "http", "StopFailure"),
    ]
    assert hooks[0]["payload"]["cwd"] == "C:\\Users\\user\\projects\\capture\\ws"
    assert hooks[0]["payload"]["transcript_path"] == (
        "C:\\Users\\user\\.claude\\projects\\C--Users-user-projects-capture-ws\\"
        + SESSION
        + ".jsonl"
    )
    assert hooks[1]["payload"]["prompt"] == (
        "Read C:\\Users\\user\\projects\\capture\\ws\\notes.txt as user (user)"
    )
    assert hooks[2]["payload"]["error"] == (
        "API error (req_EXAMPLE0001) for user@example.com via example.com"
    )

    (basic_log, plugin_log) = (
        record
        for doc in read_jsonl(dest / "otlp/headless-basic/logs.jsonl")
        for record in records_of(doc)
    )
    log_attributes = string_attributes(basic_log["attributes"])
    assert log_attributes["request_id"] == "req_EXAMPLE0001"
    assert log_attributes["body_ref"] == "C:\\Users\\user\\.claude\\bodies\\req_EXAMPLE0001.json"
    assert log_attributes["user.email"] == "user@example.com"
    assert string_attributes(plugin_log["attributes"])["plugin.name"] == "plugin-1"
    (down_log,) = (
        record
        for doc in read_jsonl(dest / "otlp/endpoint-down/logs.jsonl")
        for record in records_of(doc)
    )
    assert string_attributes(down_log["attributes"])["request_id"] == "req_EXAMPLE0002"
    (basic_traces,) = read_jsonl(dest / "otlp/headless-basic/traces.jsonl")
    (basic_span,) = basic_traces["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert string_attributes(basic_span["attributes"])["request_id"] == "req_EXAMPLE0001"
    (basic_metrics,) = read_jsonl(dest / "otlp/headless-basic/metrics.jsonl")
    (token_metric,) = basic_metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    (token_point,) = token_metric["sum"]["dataPoints"]
    assert string_attributes(token_point["attributes"])["user.account_uuid"] == FAKE_ACCOUNT_UUID

    secrets = [
        *(USER, HOST, EMAIL, ACCOUNT_UUID, ORG_ID, ACCOUNT_ID, USER_ID, GROUPS, REQ_A, REQ_B),
        *("corp.invalid", "acme-tools", "acme-market"),
    ]
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        text = path.read_bytes().decode("utf-8")
        assert "\r\n" not in text
        assert text.endswith("\n")
        assert not [s for s in secrets if s.lower() in text.lower()], path
        lines = [text] if path.suffix == ".json" else text.splitlines()
        for line in lines:
            assert find_personal_data(json.loads(line), LITERALS) == [], path


def test_sanitize_fails_without_writing_when_the_guard_finds_something(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    record = hook("2026-10-13T01:20:10+00:00", "/hooks/http", SESSION, "Notification")
    record["body"]["message"] = "ping alice@other.invalid"
    append_jsonl(raw / "hooks-run.jsonl", record)
    monkeypatch.setattr(sanitize_module, "scrub_safety_net", lambda text: text)
    (dest / "hooks").mkdir(parents=True)
    (dest / "hooks" / "previous.jsonl").write_text("{}\n", encoding="utf-8")

    assert sanitize_module.main(cli_args(raw, dest)) == 1

    err = capsys.readouterr().err
    assert "email" in err
    assert "hooks/headless-basic.jsonl" in err
    assert "alice" not in err
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == [
        "hooks",
        "hooks/previous.jsonl",
    ]


@pytest.mark.parametrize(
    ("signal", "doc", "kind"),
    [
        (
            "logs",
            logs_doc(
                log_record(SESSION, kv("event.name", "api_request_body"), kv("body", "secret"))
            ),
            "content event api_request_body",
        ),
        (
            "logs",
            logs_doc(log_record(SESSION, kv("event.name", "user_prompt"), kv("prompt", "secret"))),
            "content attribute prompt",
        ),
        (
            "traces",
            traces_doc(
                {
                    "name": "claude_code.tool",
                    "attributes": [kv("session.id", SESSION)],
                    "events": [{"name": "tool.output", "attributes": [kv("output", "secret")]}],
                }
            ),
            "content span event tool.output",
        ),
    ],
)
def test_sanitize_refuses_content_bearing_telemetry_unless_allowed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    signal: str,
    doc: dict[str, Any],
    kind: str,
) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    append_jsonl(raw / f"otlp-{signal}.jsonl", doc)

    assert sanitize_module.main(cli_args(raw, dest)) == 1

    err = capsys.readouterr().err
    assert kind in err
    assert f"otlp/headless-basic/{signal}.jsonl" in err
    assert "--allow-content" in err
    assert "secret" not in err
    assert not dest.exists()

    assert sanitize_module.main([*cli_args(raw, dest), "--allow-content"]) == 0
    assert (dest / "otlp" / "headless-basic" / f"{signal}.jsonl").exists()


def test_content_in_other_sessions_is_ignored(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_raw(raw)
    other = logs_doc(log_record("44444444-4444-4444-8444-444444444444", kv("prompt", "x")))
    append_jsonl(raw / "otlp-logs.jsonl", other)

    assert sanitize(raw, tmp_path / "fixtures", CTX)


def test_sanitize_raises_content_error(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_raw(raw)
    append_jsonl(
        raw / "otlp-logs.jsonl", logs_doc(log_record(SESSION, kv("tool_parameters", "{}")))
    )

    with pytest.raises(ContentError, match="tool_parameters"):
        sanitize(raw, tmp_path / "fixtures", CTX)


def test_sanitize_replaces_only_the_files_of_the_previous_manifest(tmp_path: Path) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    listed = ["hooks/removed.jsonl", "otlp/removed/logs.jsonl"]
    for name in [*listed, "hooks/notes.txt", "README.md", "other/keep.json"]:
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text("{}\n", encoding="utf-8")
    previous = {"scenarios": [{"name": "removed", "files": listed}]}
    (dest / "manifest.json").write_text(json.dumps(previous), encoding="utf-8")

    sanitize(raw, dest, CTX)

    assert not (dest / "hooks" / "removed.jsonl").exists()
    assert not (dest / "otlp" / "removed").exists()
    assert (dest / "hooks" / "notes.txt").exists()
    assert (dest / "README.md").exists()
    assert (dest / "other" / "keep.json").exists()
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["scenarios"]


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        "not json",
        {"hooks": "pre-commit"},
        {"scenarios": [{"name": "x", "files": [".git/config"]}]},
        {"scenarios": [{"name": "x", "files": ["hooks/../../escape.jsonl"]}]},
    ],
)
def test_sanitize_refuses_a_destination_it_did_not_write(tmp_path: Path, manifest: object) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "dest"
    make_raw(raw)
    (dest / "hooks").mkdir(parents=True)
    (dest / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    if manifest is not None:
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (dest / "manifest.json").write_text(text, encoding="utf-8")
    before = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))

    with pytest.raises(DestinationError, match="--dest"):
        sanitize(raw, dest, CTX)

    assert sanitize_module.main(cli_args(raw, dest)) == 1
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == before
    assert (dest / "hooks" / "pre-commit").read_text(encoding="utf-8") == "#!/bin/sh\n"


def test_manifest_records_the_telemetry_env_of_the_settings_files(tmp_path: Path) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    env = {"OTEL_LOG_USER_PROMPTS": "0", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}
    for name in ("settings-http-8790.json", "settings-command-8790.json"):
        (raw / name).write_text(json.dumps({"env": env, "hooks": {}}), encoding="utf-8")

    sanitize(raw, dest, CTX)

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["telemetry_env"] == env


@pytest.mark.parametrize(
    ("user", "extra", "changed"),
    [
        ("code", None, "changed name at"),
        ("stop", None, "changed hook_event_name at"),
        ("vscode", kv("terminal.type", "vscode"), "changed terminal.type at"),
    ],
)
def test_sanitize_fails_when_redaction_would_change_structure(
    tmp_path: Path, user: str, extra: dict[str, Any] | None, changed: str
) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    if extra is not None:
        append_jsonl(raw / "otlp-logs.jsonl", logs_doc(log_record(SESSION, extra)))
    ctx = dataclasses.replace(CTX, user=user)

    with pytest.raises(StructureError) as excinfo:
        sanitize(raw, dest, ctx)

    message = str(excinfo.value)
    assert changed in message
    assert "(username)" in message
    assert not dest.exists()


def test_cli_refuses_a_conflicting_username(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw, dest = tmp_path / "raw", tmp_path / "fixtures"
    make_raw(raw)
    args = cli_args(raw, dest)
    args[args.index("--user") + 1] = "host"

    assert sanitize_module.main(args) == 1

    assert "username" in capsys.readouterr().err
    assert not dest.exists()


def test_cli_skips_conflicting_git_identity_values_and_accepts_an_empty_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[LocalContext] = []

    def fake_sanitize(
        raw: Path, dest: Path, ctx: LocalContext, *, allow_content: bool = False
    ) -> list[str]:
        seen.append(ctx)
        return []

    monkeypatch.setattr(sanitize_module, "sanitize", fake_sanitize)
    monkeypatch.setattr(sanitize_module, "git_identity", lambda: ["Claude", "one@corp.invalid"])
    args = ["--raw", str(tmp_path), "--dest", str(tmp_path / "f"), "--user", "", "--host", ""]

    assert sanitize_module.main(args) == 0

    (ctx,) = seen
    assert (ctx.user, ctx.host, ctx.forbid) == ("", "", ("one@corp.invalid",))
    assert "git" in capsys.readouterr().err


def test_sanitize_fails_when_a_scenario_has_no_hook_records(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_raw(raw)
    manifest = json.loads((raw / "scenarios.json").read_text(encoding="utf-8"))
    manifest["scenarios"][1]["session_id"] = "44444444-4444-4444-8444-444444444444"
    (raw / "scenarios.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="headless-hooks-only"):
        sanitize(raw, tmp_path / "fixtures", CTX)


def test_sanitize_rejects_unsafe_scenario_names(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_raw(raw)
    manifest = json.loads((raw / "scenarios.json").read_text(encoding="utf-8"))
    manifest["scenarios"][0]["name"] = "../escape"
    (raw / "scenarios.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="scenario name"):
        sanitize(raw, tmp_path / "fixtures", CTX)


def test_sanitize_rejects_duplicate_scenario_names(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_raw(raw)
    manifest = json.loads((raw / "scenarios.json").read_text(encoding="utf-8"))
    manifest["scenarios"][1]["name"] = "headless-basic"
    (raw / "scenarios.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate scenario name"):
        sanitize(raw, tmp_path / "fixtures", CTX)


def test_cli_defaults_to_the_current_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[LocalContext, bool]] = []

    def fake_sanitize(
        raw: Path, dest: Path, ctx: LocalContext, *, allow_content: bool = False
    ) -> list[str]:
        seen.append((ctx, allow_content))
        return []

    monkeypatch.setattr(sanitize_module, "sanitize", fake_sanitize)
    # sanitize.main looks these up on the getpass and socket modules at call time.
    monkeypatch.setattr(getpass, "getuser", lambda: "someone")
    monkeypatch.setattr(socket, "gethostname", lambda: "SOMEHOST")
    monkeypatch.setattr(sanitize_module, "git_identity", lambda: ["Some One", "one@corp.invalid"])
    monkeypatch.setattr(sanitize_module, "short_path", lambda path: "C:\\Users\\SOMEON~1")

    args = ["--raw", str(tmp_path), "--dest", str(tmp_path / "f")]
    assert sanitize_module.main(args) == 0
    assert sanitize_module.main([*args, "--forbid", "acme", "--home-alias", "D:\\Users\\x"]) == 0
    assert seen == [
        (
            LocalContext(
                home=str(Path.home()),
                user="someone",
                host="SOMEHOST",
                workspace_root=None,
                home_aliases=("C:\\Users\\SOMEON~1",),
                forbid=("Some One", "one@corp.invalid"),
            ),
            False,
        ),
        (
            LocalContext(
                home=str(Path.home()),
                user="someone",
                host="SOMEHOST",
                workspace_root=None,
                home_aliases=("D:\\Users\\x", "C:\\Users\\SOMEON~1"),
                forbid=("acme", "Some One", "one@corp.invalid"),
            ),
            False,
        ),
    ]


def test_module_runs_with_python_dash_m() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tools.capture.sanitize", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    for option in ("--workspace-root", "--forbid", "--home-alias", "--allow-content"):
        assert option in result.stdout
