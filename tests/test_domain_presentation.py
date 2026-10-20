"""Presentation rules for the web UI: paths, durations, bays and short descriptions.

Paths in hook payloads hold the OS username (ADR 0006). By product decision P2 the full
``cwd`` never reaches the HTML: the session page shows it with the home directory as
``~`` and the overview shows the project name, so the username must never survive.
"""

from __future__ import annotations

import pytest

from ccobs.domain.alerts import (
    AlertConfig,
    AlertRule,
    DailyCostRule,
    RepeatedFailuresRule,
    SessionCostRule,
    WaitingUserRule,
)
from ccobs.domain.presentation import (
    BAYS,
    RECENT_END_WINDOW_MS,
    Bay,
    StripFacts,
    alerts_invitation,
    bay_of,
    board_sort_key,
    describe_activity,
    describe_agent_stop,
    describe_alert,
    describe_block,
    describe_block_close,
    display_path,
    format_duration,
    is_recent_end,
    project_name,
)
from ccobs.domain.sessions import BlockKind, SessionStatus

# A username that no real path component around it contains.
USER = "alice-secret"

# Paths


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        # Linux
        (f"/home/{USER}/projects/api", "~/projects/api"),
        (f"/home/{USER}", "~"),
        (f"/home/{USER}/", "~"),
        (f"/var/home/{USER}/src/x", "~/src/x"),
        # macOS
        (f"/Users/{USER}/code/site", "~/code/site"),
        (f"/Users/{USER}", "~"),
        # root
        ("/root/work", "~/work"),
        ("/root", "~"),
        # Windows, every spelling of the drive and separator
        (f"C:\\Users\\{USER}\\projects\\api", "~\\projects\\api"),
        (f"c:\\users\\{USER}\\projects\\api", "~\\projects\\api"),
        (f"C:/Users/{USER}/projects/api", "~/projects/api"),
        (f"c:/Users/{USER}/projects", "~/projects"),
        (f"D:\\Users\\{USER}\\x", "~\\x"),
        (f"C:\\Users\\{USER}", "~"),
        (f"C:\\Users\\{USER}\\", "~"),
        # Windows extended-length prefixes
        (f"\\\\?\\C:\\Users\\{USER}\\x", "~\\x"),
        (f"\\\\.\\C:\\Users\\{USER}\\x", "~\\x"),
        # WSL, mounted Windows drive
        (f"/mnt/c/Users/{USER}/projects/api", "~/projects/api"),
        (f"/mnt/d/users/{USER}", "~"),
        # UNC: the server and share are dropped, a home inside them still becomes ~
        (f"\\\\wsl.localhost\\Ubuntu\\home\\{USER}\\proj", "~\\proj"),
        (f"\\\\wsl$\\Ubuntu\\home\\{USER}", "~"),
        (f"\\\\fileserver\\c$\\Users\\{USER}\\proj", "~\\proj"),
        (f"\\\\?\\UNC\\fileserver\\home\\home\\{USER}\\proj", "~\\proj"),
        (f"//fileserver/share/home/{USER}/proj", "~/proj"),
        # Users/<name> below any prefix: hiding too much is the safe direction.
        (f"/Volumes/Mac/Users/{USER}/x", "~/x"),
        (f"D:\\Data\\Users\\{USER}", "~"),
        (f"D:\\Data\\Users\\{USER}\\proj", "~\\proj"),
        (f"/c/Users/{USER}/proj", "~/proj"),
        (f"/cygdrive/c/Users/{USER}/proj", "~/proj"),
        (f"/Users/Shared/Users/{USER}/x", "~/x"),
    ],
)
def test_display_path_replaces_the_home_directory(cwd: str, expected: str) -> None:
    assert display_path(cwd) == expected


@pytest.mark.parametrize(
    "cwd",
    [
        "/srv/app",
        "/opt/project",
        "/",
        "/var/lib/x/",
        "C:\\",
        "C:\\work\\api",
        "D:/src/api",
        "relative/path",
        # Not a home: the prefix must end at a separator.
        "/homes/x/y",
        "/rootfs/x",
        "/Usersx/y",
        "C:\\Usersx\\y",
        # Not a home: no username after the home root.
        "/home/",
        "/home",
        "C:\\Users\\",
        "/mnt/c/Users",
        # Shared, not personal, folders under Users.
        "/Users/Shared/project",
        "C:\\Users\\Public\\Documents",
        "C:\\Users\\Default\\x",
        "C:\\Users\\All Users\\x",
        "/Volumes/Mac/Users/Shared/x",
        "D:\\Data\\Users\\Public",
        # Users must be a whole component.
        "/srv/MyUsers/x",
        # Known limitation: homes elsewhere are not recognised (module docstring).
        "/home2/x",
        "/export/home/x",
        "/usr/home/x",
    ],
)
def test_display_path_keeps_paths_outside_home(cwd: str) -> None:
    assert display_path(cwd) == cwd


def test_a_grouped_home_layout_hides_the_group_not_the_user() -> None:
    # Known limitation (module docstring): /home/<group>/<user> is read as the group's home.
    assert display_path(f"/home/staff/{USER}/x") == f"~/{USER}/x"
    assert project_name(f"/home/staff/{USER}") == USER
    # /home/users/<user> is caught by the Users rule, which ignores letter case.
    assert display_path(f"/home/users/{USER}/x") == "~/x"


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("\\\\fileserver\\share\\team\\api", "…\\team\\api"),
        ("\\\\fileserver\\share\\team\\api\\", "…\\team\\api"),
        ("\\\\fileserver\\share", "…"),
        ("\\\\fileserver\\share\\", "…"),
        ("\\\\fileserver", "…"),
        ("//fileserver/share/team", "…/team"),
        ("\\\\?\\UNC\\fileserver\\share\\team", "…\\team"),
    ],
)
def test_display_path_hides_the_server_and_share_of_unc_paths(cwd: str, expected: str) -> None:
    # The server is a hostname and the share is often the username: both are dropped.
    assert display_path(cwd) == expected


@pytest.mark.parametrize("cwd", [None, ""])
def test_display_path_of_nothing_is_none(cwd: str | None) -> None:
    assert display_path(cwd) is None


HOME_SHAPES = [
    "/home/{u}",
    "/home/{u}/",
    "/home/{u}/a/b",
    "/var/home/{u}/a",
    "/Users/{u}",
    "/Users/{u}/Library/x",
    "/USERS/{u}/x",
    "C:\\Users\\{u}",
    "C:\\Users\\{u}\\a\\b",
    "c:\\users\\{u}\\a",
    "C:/Users/{u}/a",
    "z:/USERS/{u}",
    "\\\\?\\C:\\Users\\{u}\\a",
    "\\\\.\\c:\\users\\{u}",
    "/mnt/c/Users/{u}/a",
    "/mnt/C/users/{u}",
    "\\\\wsl.localhost\\Ubuntu\\home\\{u}\\a",
    "\\\\wsl$\\Debian\\home\\{u}",
    "\\\\server\\c$\\Users\\{u}\\a",
    "//server/share/Users/{u}/a",
    "\\\\server\\{u}",
    "\\\\server\\{u}\\a\\b",
    "\\\\{u}\\share\\a",
    "\\\\?\\UNC\\{u}\\{u}\\a",
    "\\home\\{u}\\a",
    "/Volumes/Mac/Users/{u}/a",
    "D:\\Data\\Users\\{u}",
    "/cygdrive/c/Users/{u}/a",
]


@pytest.mark.parametrize("shape", HOME_SHAPES)
@pytest.mark.parametrize("user", [USER, "bob", "Jane Doe", "j.doe", "DOMAIN.user", "ü"])
def test_the_username_never_survives(shape: str, user: str) -> None:
    cwd = shape.format(u=user)
    shown = display_path(cwd)
    name = project_name(cwd)
    assert shown is not None
    assert name is not None
    assert user not in shown
    assert user not in name


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/srv/app", "app"),
        ("/srv/app/", "app"),
        ("/srv/app//", "app"),
        (f"/home/{USER}/projects/api", "api"),
        (f"C:\\Users\\{USER}\\projects\\api", "api"),
        (f"C:\\Users\\{USER}\\projects\\api\\", "api"),
        ("C:/work/mixed\\api", "api"),
        ("C:\\work\\mixed/api", "api"),
        ("C:\\", "C:"),
        ("C:/", "C:"),
        ("C:", "C:"),
        ("/", "/"),
        ("\\", "\\"),
        ("//", "/"),
        ("api", "api"),
        # The home directory itself is never named by its username.
        (f"/home/{USER}", "~"),
        (f"/Users/{USER}/", "~"),
        (f"C:\\Users\\{USER}", "~"),
        ("/root", "~"),
        ("\\\\fileserver\\share", "…"),
        ("\\\\fileserver\\share\\team\\api", "api"),
    ],
)
def test_project_name_is_the_last_component(cwd: str, expected: str) -> None:
    assert project_name(cwd) == expected


@pytest.mark.parametrize("cwd", [None, ""])
def test_project_name_of_nothing_is_none(cwd: str | None) -> None:
    assert project_name(cwd) is None


# Durations


SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (0, "0s"),
        (999, "0s"),
        (SECOND, "1s"),
        (42 * SECOND, "42s"),
        (42 * SECOND + 999, "42s"),
        (59 * SECOND, "59s"),
        (MINUTE, "1m 00s"),
        (4 * MINUTE + 12 * SECOND, "4m 12s"),
        (4 * MINUTE + 5 * SECOND, "4m 05s"),
        (59 * MINUTE + 59 * SECOND, "59m 59s"),
        (HOUR, "1h 00m"),
        (HOUR + 3 * MINUTE + 59 * SECOND, "1h 03m"),
        (23 * HOUR + 59 * MINUTE, "23h 59m"),
        (DAY, "1d 00h"),
        (2 * DAY + 4 * HOUR + 30 * MINUTE, "2d 04h"),
        (400 * DAY + 23 * HOUR, "400d 23h"),
        # Clock skew never shows a negative time.
        (-1, "0s"),
        (-5 * MINUTE, "0s"),
    ],
)
def test_format_duration(ms: int, expected: str) -> None:
    assert format_duration(ms) == expected


# Bays


def test_bays_come_in_board_order_with_sentence_case_headings() -> None:
    assert BAYS == (Bay.WAITING_USER, Bay.WORKING, Bay.IDLE, Bay.ENDED, Bay.STALE)
    assert [bay.heading for bay in BAYS] == [
        "Waiting on you",
        "Working",
        "Idle",
        "Ended in the last 24 hours",
        "Stale",
    ]


@pytest.mark.parametrize("bay", list(Bay))
def test_each_bay_is_its_session_status(bay: Bay) -> None:
    assert bay_of(bay.value) is bay
    assert str(bay) == bay.value


def test_bay_of_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="not a session status"):
        bay_of("paused")


def _facts(
    session_id: str,
    status: SessionStatus,
    *,
    last: int = 0,
    ended: int | None = None,
    oldest_block: int | None = None,
) -> StripFacts:
    return StripFacts(
        session_id=session_id,
        status=status,
        last_event_at_ms=last,
        ended_at_ms=ended,
        oldest_open_block_at_ms=oldest_block,
    )


def test_board_sort_key_orders_bays_then_within_each() -> None:
    strips = [
        _facts("stale-old", "stale", last=10),
        _facts("ended-old", "ended", last=500, ended=500),
        _facts("idle-recent", "idle", last=900),
        _facts("waiting-short", "waiting_user", last=990, oldest_block=980),
        _facts("working-old", "working", last=100),
        _facts("ended-recent", "ended", last=800, ended=800),
        _facts("waiting-long", "waiting_user", last=999, oldest_block=200),
        _facts("idle-old", "idle", last=300),
        _facts("working-recent", "working", last=950),
        _facts("stale-recent", "stale", last=40),
        # No open block known yet: the wait counts from its last event.
        _facts("waiting-unknown", "waiting_user", last=500),
        # No end time: its last event stands in.
        _facts("ended-no-time", "ended", last=600),
    ]
    ordered = [strip.session_id for strip in sorted(strips, key=board_sort_key)]
    assert ordered == [
        "waiting-long",
        "waiting-unknown",
        "waiting-short",
        "working-recent",
        "working-old",
        "idle-recent",
        "idle-old",
        "ended-recent",
        "ended-no-time",
        "ended-old",
        "stale-recent",
        "stale-old",
    ]


def test_board_sort_key_takes_a_zero_time_as_a_time() -> None:
    strips = [
        _facts("waiting-at-100", "waiting_user", last=999, oldest_block=100),
        _facts("waiting-at-0", "waiting_user", last=999, oldest_block=0),
        _facts("ended-at-0", "ended", last=999, ended=0),
        _facts("ended-at-100", "ended", last=50, ended=100),
    ]
    assert [strip.session_id for strip in sorted(strips, key=board_sort_key)] == [
        "waiting-at-0",
        "waiting-at-100",
        "ended-at-100",
        "ended-at-0",
    ]


def test_board_sort_key_breaks_ties_by_session_id() -> None:
    strips = [_facts("b", "idle", last=5), _facts("a", "idle", last=5)]
    assert [strip.session_id for strip in sorted(strips, key=board_sort_key)] == ["a", "b"]


NOW = 10 * DAY


@pytest.mark.parametrize(
    ("ended_at_ms", "expected"),
    [
        (NOW, True),
        (NOW - HOUR, True),
        (NOW - RECENT_END_WINDOW_MS, True),
        (NOW - RECENT_END_WINDOW_MS - 1, False),
        (NOW - 3 * DAY, False),
        # A receive clock ahead of now is still recent.
        (NOW + MINUTE, True),
        (None, False),
    ],
)
def test_is_recent_end(ended_at_ms: int | None, expected: bool) -> None:
    assert RECENT_END_WINDOW_MS == DAY
    assert is_recent_end(ended_at_ms, NOW) is expected


# Descriptions


@pytest.mark.parametrize(
    ("kind", "tool_name", "mcp_server_name", "expected"),
    [
        ("permission", "Bash", None, "Permission: Bash"),
        (
            "permission",
            "mcp__spike-elicit__pick_color",
            "spike-elicit",
            "Permission: mcp__spike-elicit__pick_color",
        ),
        ("permission", None, None, "Permission"),
        ("permission", "", None, "Permission"),
        ("permission", "AskUserQuestion", None, "Question from Claude"),
        ("permission", "ExitPlanMode", None, "Plan approval"),
        (
            "elicitation",
            "mcp__spike-elicit__pick_color",
            "spike-elicit",
            "Question from spike-elicit",
        ),
        ("elicitation", None, None, "Question from an MCP server"),
        ("elicitation", None, "", "Question from an MCP server"),
    ],
)
def test_describe_block(
    kind: BlockKind, tool_name: str | None, mcp_server_name: str | None, expected: str
) -> None:
    assert describe_block(kind, tool_name, mcp_server_name) == expected


@pytest.mark.parametrize(
    ("status", "compacting", "running_subagents", "idle_notified", "end_reason", "expected"),
    [
        ("working", False, 0, False, None, "In a turn"),
        ("working", False, 1, False, None, "1 subagent running"),
        ("working", False, 3, False, None, "3 subagents running"),
        ("working", True, 2, False, None, "Compacting"),
        ("idle", False, 0, False, None, "Turn finished"),
        ("idle", False, 0, True, None, "Waiting for your next prompt"),
        ("idle", False, 2, True, None, "2 subagents running"),
        ("idle", True, 0, False, None, "Compacting"),
        ("ended", False, 0, False, None, "Ended"),
        ("ended", False, 0, False, "", "Ended"),
        ("stale", False, 0, False, None, "No recent events"),
        ("waiting_user", False, 1, False, None, "Waiting on you"),
    ],
)
def test_describe_activity(
    status: SessionStatus,
    compacting: bool,
    running_subagents: int,
    idle_notified: bool,
    end_reason: str | None,
    expected: str,
) -> None:
    described = describe_activity(
        status,
        compacting=compacting,
        running_subagents=running_subagents,
        idle_notified=idle_notified,
        end_reason=end_reason,
    )
    assert described == expected


def test_describe_activity_defaults() -> None:
    assert describe_activity("working") == "In a turn"
    assert describe_activity("idle") == "Turn finished"


@pytest.mark.parametrize(
    ("status", "idle_notified", "running_subagents", "expected"),
    [
        # A session that never had a prompt is ready, not finished.
        ("idle", False, 0, "Ready"),
        ("idle", True, 0, "Ready"),
        ("idle", False, 1, "1 subagent running"),
        ("working", False, 0, "In a turn"),
    ],
)
def test_describe_activity_before_the_first_prompt(
    status: SessionStatus, idle_notified: bool, running_subagents: int, expected: str
) -> None:
    described = describe_activity(
        status,
        idle_notified=idle_notified,
        running_subagents=running_subagents,
        prompted=False,
    )
    assert described == expected


@pytest.mark.parametrize(
    ("end_reason", "expected"),
    [
        ("prompt_input_exit", "Exited"),
        ("clear", "Cleared"),
        ("resume", "Resumed"),
        ("logout", "Logged out"),
        ("bypass_permissions_disabled", "Bypass permissions disabled"),
        ("other", "Ended"),
        ("something_new", "Ended"),
        (None, "Ended"),
        ("", "Ended"),
    ],
)
def test_describe_activity_words_the_end_reason(end_reason: str | None, expected: str) -> None:
    assert describe_activity("ended", end_reason=end_reason) == expected


@pytest.mark.parametrize(
    ("kind", "reason", "expected"),
    [
        ("permission", "tool_completed", "Allowed, the tool ran"),
        ("permission", "tool_failed", "Allowed, the tool failed"),
        ("elicitation", "tool_completed", "Answered, the tool ran"),
        ("elicitation", "tool_failed", "Answered, the tool failed"),
        ("permission", "denied", "Denied"),
        ("permission", "superseded", "Closed by later activity"),
        ("elicitation", "elicitation_accept", "Accepted"),
        ("elicitation", "elicitation_decline", "Declined"),
        ("elicitation", "elicitation_cancel", "Cancelled"),
        ("elicitation", "elicitation_other", "Answered"),
        ("permission", "agent_stopped", "The subagent stopped"),
        ("elicitation", "session_ended", "The session ended"),
        ("permission", "stale", "Closed after hours without events"),
        ("permission", "something_new", "Closed"),
        ("elicitation", None, "Closed"),
    ],
)
def test_describe_block_close(kind: BlockKind, reason: str | None, expected: str) -> None:
    assert describe_block_close(kind, reason) == expected


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stopped", "Stopped"),
        ("session_ended", "Stopped when the session ended"),
        ("stale", "Stopped after hours without events"),
        ("something_new", "Stopped"),
        (None, "Stopped"),
    ],
)
def test_describe_agent_stop(reason: str | None, expected: str) -> None:
    assert describe_agent_stop(reason) == expected


# Alerts


@pytest.mark.parametrize(
    ("rule", "value", "threshold", "expected"),
    [
        ("waiting_user", 134.4, 30.0, "Waited on you for 2m 14s"),
        ("waiting_user", 31.5, 30.0, "Waited on you for 31s"),
        ("repeated_failures", 3.0, 3.0, "API requests failed 3 times"),
        ("session_cost", 12.4, 10.0, "This session cost $12.40 (limit $10)"),
        ("session_cost", 0.5, 0.25, "This session cost $0.5000 (limit $0.25)"),
        ("daily_cost", 52.1, 50.0, "The last 24 hours cost $52.10 (limit $50)"),
        ("daily_cost", 1234.5, 1000.5, "The last 24 hours cost $1,234.50 (limit $1,000.50)"),
    ],
)
def test_an_alert_says_what_happened_in_words(
    rule: AlertRule, value: float, threshold: float, expected: str
) -> None:
    assert describe_alert(rule, value, threshold, cleared=True) == expected


@pytest.mark.parametrize(
    ("rule", "value", "threshold", "expected"),
    [
        # The wait is not over: the value is how long it had lasted when the alert fired.
        ("waiting_user", 134.4, 30.0, "Waiting on you for over 2m 14s"),
        ("waiting_user", 31.5, 30.0, "Waiting on you for over 31s"),
        ("repeated_failures", 3.0, 3.0, "API requests failed 3 times"),
        ("session_cost", 12.4, 10.0, "This session cost $12.40 (limit $10)"),
        ("daily_cost", 52.1, 50.0, "The last 24 hours cost $52.10 (limit $50)"),
    ],
)
def test_an_alert_still_on_says_what_is_happening(
    rule: AlertRule, value: float, threshold: float, expected: str
) -> None:
    assert describe_alert(rule, value, threshold, cleared=False) == expected


def test_the_invitation_names_the_rules_on_by_default() -> None:
    assert alerts_invitation(AlertConfig()) == (
        "No alerts yet. The app warns you here when a session waits on you longer than"
        " 30s, or when API requests fail 3 times within 10m 00s."
    )


def test_the_invitation_names_the_cost_rules_only_when_they_are_on() -> None:
    config = AlertConfig(
        waiting_user=WaitingUserRule(threshold_s=90),
        session_cost=SessionCostRule(enabled=True, limit_usd=10.0),
        daily_cost=DailyCostRule(enabled=True, limit_usd=52.5),
    )

    assert alerts_invitation(config) == (
        "No alerts yet. The app warns you here when a session waits on you longer than"
        " 1m 30s, when API requests fail 3 times within 10m 00s, when a session costs"
        " more than $10, or when the last 24 hours cost more than $52.50."
    )


def test_the_invitation_with_one_rule_on() -> None:
    config = AlertConfig(
        waiting_user=WaitingUserRule(enabled=False),
        repeated_failures=RepeatedFailuresRule(count=5, window_s=60),
    )

    assert alerts_invitation(config) == (
        "No alerts yet. The app warns you here when API requests fail 5 times within 1m 00s."
    )


def test_the_invitation_with_every_rule_off_says_so() -> None:
    config = AlertConfig(
        waiting_user=WaitingUserRule(enabled=False),
        repeated_failures=RepeatedFailuresRule(enabled=False),
    )

    assert alerts_invitation(config) == (
        "No alerts yet, and every alert is off. Turn one on under Settings."
    )
