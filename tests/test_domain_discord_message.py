"""The Discord message formatter (``ccobs.domain.discord_message``, ADR 0046).

One line per alert, in the alerts page's words (``describe_alert``), with the host
label, the short session ID and a local link; the project name only when asked, with
Discord's markdown and mentions escaped; never the ``cwd``. Up to ten alerts in one
message, cut to Discord's 2000 characters with a last line that counts the rest.
"""

from __future__ import annotations

import json
from typing import Final
from urllib.parse import quote

import pytest

from ccobs.domain.alerts import ALERT_RULES, AlertRule
from ccobs.domain.discord_message import (
    ALERTS_PER_MESSAGE,
    CONTENT_MAX,
    PROJECT_MAX,
    USERNAME,
    DiscordAlert,
    alert_line,
    alerts_payload,
    connection_test_payload,
    escape_markdown,
)
from ccobs.domain.presentation import describe_alert

HOST: Final = "local"
APP_URL: Final = "http://localhost:4300"
SESSION: Final = "0123abcd-0000-4000-8000-000000000000"
CWD: Final = "/home/someone/work/my_project"
PATH_PARTS: Final = ("/home", "someone", "work")


def alert(
    rule: AlertRule = "waiting_user",
    *,
    value: float = 45.0,
    threshold: float = 30.0,
    session_id: str | None = SESSION,
    cwd: str | None = CWD,
) -> DiscordAlert:
    if rule == "daily_cost":
        session_id = None
        cwd = None
    return DiscordAlert(rule=rule, value=value, threshold=threshold, session_id=session_id, cwd=cwd)


def line(item: DiscordAlert, *, include_project: bool = False, host: str = HOST) -> str:
    return alert_line(item, host=host, app_url=APP_URL, include_project=include_project)


def content(items: list[DiscordAlert], *, include_project: bool = False) -> str:
    payload = alerts_payload(items, host=HOST, app_url=APP_URL, include_project=include_project)
    return payload["content"]


# One line per alert.


def test_a_session_alert_says_what_happened_where_and_links_the_session() -> None:
    assert line(alert()) == (
        f"Waiting on you for over 45s. Host local, session 0123abcd: <{APP_URL}/sessions/{SESSION}>"
    )


@pytest.mark.parametrize("rule", ALERT_RULES)
def test_each_rule_uses_the_alerts_page_words(rule: AlertRule) -> None:
    item = alert(rule, value=60.0, threshold=50.0)
    words = describe_alert(rule, 60.0, 50.0, cleared=False)

    assert line(item).startswith(f"{words}. Host local")


def test_a_daily_alert_has_no_session_and_links_the_alerts_page() -> None:
    assert line(alert("daily_cost", value=60.0, threshold=50.0)) == (
        f"The last 24 hours cost $60.00 (limit $50). Host local: <{APP_URL}/alerts>"
    )


def test_the_project_shows_only_when_asked() -> None:
    assert "my" not in line(alert())
    assert line(alert(), include_project=True) == (
        f"Waiting on you for over 45s. Host local, session 0123abcd, project my\\_project: "
        f"<{APP_URL}/sessions/{SESSION}>"
    )


def test_no_project_without_a_cwd() -> None:
    assert line(alert(cwd=None), include_project=True) == line(alert(cwd=None))


@pytest.mark.parametrize("include_project", [False, True])
def test_the_cwd_never_shows(include_project: bool) -> None:
    text = content([alert(), alert("session_cost")], include_project=include_project)

    for part in PATH_PARTS:
        assert part not in text
    assert CWD not in text


def test_the_host_label_is_escaped() -> None:
    assert "Host my\\_laptop" in line(alert(), host="my_laptop")


def test_the_session_link_is_percent_encoded() -> None:
    odd = "a b>c<@everyone"
    text = line(alert(session_id=odd))

    assert f"<{APP_URL}/sessions/a%20b%3Ec%3C%40everyone>" in text
    assert "session a b\\>c\\<\\@e" in text


# Escaping.


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain", "plain"),
        ("a_b", "a\\_b"),
        ("*bold*", "\\*bold\\*"),
        ("~~gone~~", "\\~\\~gone\\~\\~"),
        ("`code`", "\\`code\\`"),
        ("||spoiler||", "\\|\\|spoiler\\|\\|"),
        ("> quote", "\\> quote"),
        ("[link](http://x)", "\\[link\\]\\(http://x\\)"),
        ("back\\slash", "back\\\\slash"),
        ("@everyone", "\\@everyone"),
        ("@here", "\\@here"),
        ("<@123456789012345678>", "\\<\\@123456789012345678\\>"),
    ],
)
def test_markdown_and_mentions_are_escaped(raw: str, escaped: str) -> None:
    assert escape_markdown(raw) == escaped


@pytest.mark.parametrize("name", ["@everyone", "**loud**", "a_b`c`", "x\ny", "[a](b)"])
def test_a_project_name_is_escaped_and_kept_on_one_line(name: str) -> None:
    text = line(alert(cwd=f"/home/someone/{name}"), include_project=True)

    assert "\n" not in text
    assert "@everyone" not in text.replace("\\@everyone", "")
    assert "**" not in text


def test_a_long_project_name_is_shortened() -> None:
    name = "p" * (PROJECT_MAX + 50)
    text = line(alert(cwd=f"/home/someone/{name}"), include_project=True)

    assert f"project {'p' * (PROJECT_MAX - 1)}…: " in text


# Characters that break a line or reorder or hide text: categories Cc (controls), Cf
# (format: bidi overrides, zero-width), Zl and Zp (line and paragraph separators).
INVISIBLE: Final = (
    "\n",
    "\r",
    "\t",
    "\x00",
    "\x85",
    "\u2028",
    "\u2029",
    "\u202e",
    "\u2066",
    "\u200b",
    "\u200d",
    "\ufeff",
    "\u00ad",
)


@pytest.mark.parametrize("char", INVISIBLE, ids=lambda char: f"U+{ord(char):04X}")
def test_a_project_name_loses_every_invisible_character(char: str) -> None:
    text = line(alert(cwd=f"/home/someone/ab{char}cd"), include_project=True)

    assert char not in text
    assert "project ab cd: " in text


@pytest.mark.parametrize("char", INVISIBLE, ids=lambda char: f"U+{ord(char):04X}")
def test_a_session_id_loses_every_invisible_character_in_the_text(char: str) -> None:
    odd = f"ab{char}cdefgh-rest"
    text = line(alert(session_id=odd))

    # Shown cleaned; the link percent-encodes it as it is.
    assert char not in text
    assert "session ab cdefg: " in text
    assert f"/sessions/ab{quote(char, safe='')}cdefgh-rest>" in text


def test_the_host_label_loses_every_invisible_character() -> None:
    text = line(alert(), host="my\u202elaptop\u2028")

    assert "Host my laptop , session" in text


# The message.


def test_the_payload_names_the_app_and_never_mentions_anyone() -> None:
    payload = alerts_payload([alert()], host=HOST, app_url=APP_URL, include_project=False)

    assert payload == {
        "username": USERNAME,
        "content": line(alert()),
        "allowed_mentions": {"parse": []},
    }
    assert USERNAME == "cc-observatory"
    json.dumps(payload)


def test_alerts_are_coalesced_one_line_each_in_order() -> None:
    items = [alert(), alert("repeated_failures", value=3, threshold=3), alert("daily_cost")]

    assert content(items).split("\n") == [line(item) for item in items]


def test_up_to_ten_alerts_go_in_one_message() -> None:
    assert ALERTS_PER_MESSAGE == 10
    items = [alert(value=float(n)) for n in range(ALERTS_PER_MESSAGE)]

    assert len(content(items).split("\n")) == ALERTS_PER_MESSAGE
    with pytest.raises(ValueError, match="at most 10"):
        content([*items, alert()])
    with pytest.raises(ValueError, match="at least one"):
        content([])


def test_a_long_message_is_cut_with_a_count_of_the_rest() -> None:
    assert CONTENT_MAX == 2000
    name = "n" * PROJECT_MAX
    items = [alert(value=float(n), cwd=f"/home/someone/{name}") for n in range(10)]
    lines = [line(item, include_project=True) for item in items]
    assert len("\n".join(lines)) > CONTENT_MAX

    text = content(items, include_project=True)

    assert len(text) <= CONTENT_MAX
    *kept, last = text.split("\n")
    assert kept == lines[: len(kept)]
    assert last == f"…and {len(items) - len(kept)} more"
    # As many whole lines as fit.
    longer = "\n".join(lines[: len(kept) + 1]) + f"\n…and {len(items) - len(kept) - 1} more"
    assert len(longer) > CONTENT_MAX


def test_a_first_line_too_long_leaves_only_the_count() -> None:
    huge = alert(session_id="s" * CONTENT_MAX)

    assert content([huge, alert()]) == "…and 2 more"


def test_a_message_that_fits_has_no_count() -> None:
    assert "more" not in content([alert()] * 10)


def test_the_test_message_names_the_host() -> None:
    assert connection_test_payload("my_laptop") == {
        "username": USERNAME,
        "content": ("cc-observatory test message from host my\\_laptop. Alerts will show up here."),
        "allowed_mentions": {"parse": []},
    }
