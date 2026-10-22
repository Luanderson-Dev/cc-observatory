"""The alert settings routes (ADRs 0042 and 0045).

``GET /ui/alerts/settings`` renders each setting's effective value and its source. The
two POST routes change them through the writer, only for a same-origin htmx request
(``Sec-Fetch-Site``, ``Origin``, ``HX-Request``, the form media type): a save changes
only the fields the form holds whose value differs from the one in force, after checking
every submitted one, and saves nothing on an error, whose message names the field and
its range but never the value, while the input shows the text typed; a reset drops one
saved value or all of them. The writer applies each change and reloads the configuration
the next tick uses; with no answer in time, the route answers 503.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.exc import OperationalError

from ccobs.domain.alerts import ALERT_SETTINGS, setting_error
from ccobs.domain.telemetry import DEFAULT_HOST
from ccobs.server import app as server_app
from ccobs.server.alert_settings import FORM_MAX_BYTES, RESET_PATH, SETTINGS_PATH
from ccobs.server.config import Settings
from ccobs.server.db.repository import ALERT_SETTING_TYPES, AlertSettingValue, Repository
from ccobs.server.guards import CONTENT_SECURITY_POLICY
from ccobs.server.ingest import HookQueue
from ccobs.server.writer import (
    AlertSettingsResult,
    AlertSettingsUnavailableError,
    Writer,
    WriterObservers,
)

create_app: Final = functools.partial(
    server_app.create_app, serve_metrics=False, sweeps=False, reconcile=False
)
GOOD_HOST: Final = "127.0.0.1:4300"
ORIGIN: Final = f"http://{GOOD_HOST}"
# What htmx's hx-post sends from a page of the app: the XHR's Fetch Metadata and Origin,
# its HX-Request header and the form media type (httpx sets it for ``data=``).
HTMX: Final = {
    "origin": ORIGIN,
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "hx-request": "true",
}
FORM_TYPE: Final = "application/x-www-form-urlencoded"
CANARY: Final = "CANARY-7f3a"
# A change that took tells the page the alerts may read differently (the history's
# empty state names the rules on): htmx dispatches the event, which bubbles to body.
REFRESH_ALERTS: Final = "ccobs:alerts"
HOST: Final = DEFAULT_HOST


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in list(os.environ):
        if name.upper().startswith("CCOBS_"):
            monkeypatch.delenv(name)
    # The environment sets two settings; the others are the built-in defaults.
    monkeypatch.setenv("CCOBS_ALERT_COOLDOWN_S", "60")
    monkeypatch.setenv("CCOBS_ALERT_SESSION_COST_ENABLED", "true")
    return Settings(data_dir=tmp_path / "data")


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"http://{GOOD_HOST}")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with _client(app) as test_client:
        yield test_client


def repository(client: TestClient) -> Repository:
    app = client.app
    assert isinstance(app, FastAPI)
    repo: Repository = app.state.repository
    return repo


def writer(client: TestClient) -> Writer:
    app = client.app
    assert isinstance(app, FastAPI)
    found: Writer = app.state.writer
    return found


def saved(client: TestClient) -> dict[str, AlertSettingValue]:
    return repository(client).alert_settings(HOST)


def post(
    client: TestClient,
    path: str,
    data: Sequence[tuple[str, str]] | Mapping[str, str],
    headers: Mapping[str, str] = HTMX,
) -> httpx2.Response:
    """A form POST as htmx sends it, the fields in order (a name may repeat)."""
    pairs = list(data.items()) if isinstance(data, Mapping) else list(data)
    return client.post(
        path,
        content=urlencode(pairs).encode(),
        headers={**headers, "content-type": FORM_TYPE},
    )


def assert_alerts_refreshed(response: httpx2.Response) -> None:
    assert response.headers.get_list("hx-trigger") == [REFRESH_ALERTS]


def assert_alerts_not_refreshed(response: httpx2.Response) -> None:
    assert "hx-trigger" not in response.headers


def field_block(html: str, name: str) -> str:
    """The markup of one setting: from its input's id to the next setting's block."""
    start = html.index(f'id="{name}"')
    end = html.find('class="alert-settings__field"', start)
    return html[start : end if end != -1 else len(html)]


# GET: the fragment.


def test_the_fragment_shows_every_setting_with_its_value_and_source(
    client: TestClient,
) -> None:
    repository(client).save_alert_setting(HOST, "alert_waiting_threshold_s", 90, updated_at_ms=1)

    response = client.get(SETTINGS_PATH)

    assert response.status_code == 200
    html = response.text
    for name in ALERT_SETTINGS:
        assert f'name="{name}"' in html, name
    threshold = field_block(html, "alert_waiting_threshold_s")
    assert 'value="90"' in threshold
    assert "Saved here" in threshold
    cooldown = field_block(html, "alert_cooldown_s")
    assert 'value="60"' in cooldown
    assert "From the environment" in cooldown
    window = field_block(html, "alert_failures_window_s")
    assert 'value="600"' in window
    assert ">Default<" in window
    assert "checked" in field_block(html, "alert_session_cost_enabled")
    assert "checked" not in field_block(html, "alert_daily_cost_enabled")
    assert 'value="10"' in field_block(html, "alert_session_cost_usd")


def test_the_fragment_is_an_htmx_form_with_a_reset_per_saved_setting(client: TestClient) -> None:
    repository(client).save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=1)

    html = client.get(SETTINGS_PATH).text

    assert f'hx-post="{SETTINGS_PATH}"' in html
    assert html.count(f'hx-post="{RESET_PATH}"') == 2  # the saved one, and all
    assert 'name="field" value="alert_cooldown_s"' in html
    # A switch sends false when unchecked: a hidden false before the checkbox's true.
    assert '<input type="hidden" name="alert_waiting_enabled" value="false">' in html


def test_an_invalid_saved_value_shows_the_environments_value(client: TestClient) -> None:
    # The repository checks types only; 99999 s is out of bounds: the env value holds.
    repository(client).save_alert_setting(HOST, "alert_cooldown_s", 99_999, updated_at_ms=1)

    cooldown = field_block(client.get(SETTINGS_PATH).text, "alert_cooldown_s")

    assert 'value="60"' in cooldown
    assert "From the environment" in cooldown


def test_a_failed_read_answers_503_with_a_neutral_fragment(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)

    def fail(host: str) -> dict[str, AlertSettingValue]:
        raise OperationalError("SELECT value", None, Exception(CANARY))

    app.state.read_repository.alert_settings = fail
    response = client.get(SETTINGS_PATH)

    assert response.status_code == 503
    assert "could not read" in response.text
    assert CANARY not in response.text


def test_a_busy_read_gate_answers_503(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)

    class Busy:
        async def run(self, *args: object) -> object:
            raise HTTPException(status_code=503, detail="database busy")

    app.state.reads = Busy()
    response = client.get(SETTINGS_PATH)

    assert response.status_code == 503
    assert "busy" in response.text


def legends(html: str) -> list[str]:
    return re.findall(r"<legend[^>]*>([^<]+)</legend>", html)


def test_the_settings_are_grouped_by_rule_in_plain_words(client: TestClient) -> None:
    html = client.get(SETTINGS_PATH).text

    assert legends(html) == [
        "When a session waits on you",
        "When API requests keep failing",
        "When a session costs more than",
        "When the last 24 hours cost more than",
        "Between two alerts of the same kind",
    ]
    groups = html.split("<fieldset")[1:]
    inputs = re.compile(r'<input type="(?:checkbox|text)" id="([a-z_]+)"')
    names = [inputs.findall(group) for group in groups]
    assert names == [
        ["alert_waiting_enabled", "alert_waiting_threshold_s"],
        ["alert_failures_enabled", "alert_failures_count", "alert_failures_window_s"],
        ["alert_session_cost_enabled", "alert_session_cost_usd"],
        ["alert_daily_cost_enabled", "alert_daily_cost_usd"],
        ["alert_cooldown_s"],
    ]
    # Each value has its unit next to the input, named by the input's description.
    threshold = field_block(html, "alert_waiting_threshold_s")
    unit = '<span id="alert_waiting_threshold_s-unit" class="alert-settings__unit">seconds</span>'
    assert unit in threshold
    described = "alert_waiting_threshold_s-unit alert_waiting_threshold_s-source"
    assert f'aria-describedby="{described}"' in threshold
    assert ">failures<" in field_block(html, "alert_failures_count")
    assert ">US dollars<" in field_block(html, "alert_daily_cost_usd")
    # Cost is an estimate, said once under the cost rules.
    assert html.count("Cost is Claude Code&#39;s estimate, not your bill.") == 1
    assert html.index("estimate, not your bill") > html.index('id="alert_daily_cost_usd"')
    assert html.index("estimate, not your bill") < html.index('id="alert_cooldown_s"')
    # Plain user language: no setting name or environment variable shown as text.
    text = re.sub(r"<[^>]+>", "", html)
    assert "alert_" not in text
    assert "CCOBS_" not in text
    assert ">Save changes</button>" in html
    assert ">Reset all</button>" in html


def test_a_reset_button_shows_only_for_a_saved_value(client: TestClient) -> None:
    response = client.get(SETTINGS_PATH)
    html = response.text

    assert_alerts_not_refreshed(response)
    assert 'name="field"' not in html
    assert html.count(f'hx-post="{RESET_PATH}"') == 1


# POST: saves.


def test_an_htmx_post_saves_only_the_fields_it_holds(client: TestClient) -> None:
    repository(client).save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=1)

    response = post(client, SETTINGS_PATH, {"alert_waiting_threshold_s": "45"})

    assert response.status_code == 200
    assert saved(client) == {"alert_cooldown_s": 30, "alert_waiting_threshold_s": 45}
    threshold = field_block(response.text, "alert_waiting_threshold_s")
    assert 'value="45"' in threshold
    assert "Saved here" in threshold
    assert "Saved." in response.text
    assert_alerts_refreshed(response)
    # In force for the next tick.
    assert writer(client).alert_config().waiting_user.threshold_s == 45
    assert writer(client).alert_config().cooldown_s == 30
    # The CSP is the same as everywhere: htmx's XHR is connect-src 'self'.
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "form-action 'none'" in CONTENT_SECURITY_POLICY


def test_every_setting_type_is_parsed(client: TestClient) -> None:
    data = [
        ("alert_waiting_enabled", "false"),
        ("alert_failures_count", " 5 "),
        ("alert_daily_cost_enabled", "false"),
        ("alert_daily_cost_enabled", "true"),
        ("alert_daily_cost_usd", "12.75"),
        ("alert_session_cost_usd", "20"),
        ("not_a_setting", CANARY),
    ]

    response = post(client, SETTINGS_PATH, data)

    assert response.status_code == 200
    assert saved(client) == {
        "alert_waiting_enabled": False,
        "alert_failures_count": 5,
        "alert_daily_cost_enabled": True,
        "alert_daily_cost_usd": 12.75,
        "alert_session_cost_usd": 20.0,
    }
    config = writer(client).alert_config()
    assert not config.waiting_user.enabled
    assert config.daily_cost.enabled
    assert config.daily_cost.limit_usd == 12.75
    assert CANARY not in response.text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("alert_waiting_threshold_s", "4"),
        ("alert_waiting_threshold_s", "123456789"),
        ("alert_waiting_threshold_s", "30.5"),
        ("alert_waiting_threshold_s", "-30"),
        ("alert_waiting_threshold_s", ""),
        ("alert_waiting_threshold_s", CANARY),
        ("alert_session_cost_usd", "0"),
        ("alert_session_cost_usd", "1e3"),
        ("alert_session_cost_usd", "nan"),
        ("alert_session_cost_usd", "9" * 40),
        ("alert_waiting_enabled", "on"),
        ("alert_waiting_enabled", "True"),
    ],
)
def test_an_invalid_field_saves_nothing_and_names_the_field_without_its_value(
    client: TestClient, name: str, value: str
) -> None:
    data = {name: value, "alert_cooldown_s": "30"}

    response = post(client, SETTINGS_PATH, data)

    assert response.status_code == 200
    assert saved(client) == {}
    assert writer(client).alert_config().cooldown_s == 60
    block = field_block(response.text, name)
    assert "Must be " in block
    error = response.text.split(f'id="{name}-error"', 1)[1].split("</span>", 1)[0]
    assert name not in error
    assert 'aria-invalid="true"' in block
    assert "Nothing was saved" in response.text
    assert_alerts_not_refreshed(response)
    if len(value) > 2:  # shorter ones may be digits of the range itself
        assert value not in error
    # Every submitted field shows what was typed, so the user sees what to fix.
    if ALERT_SETTING_TYPES[name] is not bool:
        assert f'value="{escape(value)}"' in block
    assert 'value="30"' in field_block(response.text, "alert_cooldown_s")


def test_an_invalid_value_is_refilled_escaped_and_kept_out_of_the_error(
    client: TestClient,
) -> None:
    typed = """2<b>"&'x"""

    response = post(client, SETTINGS_PATH, {"alert_waiting_threshold_s": typed})

    assert saved(client) == {}
    html = response.text
    block = field_block(html, "alert_waiting_threshold_s")
    assert 'value="2&lt;b&gt;&#34;&amp;&#39;x"' in block
    assert typed not in html
    assert "<b>" not in html
    error = html.split('id="alert_waiting_threshold_s-error"', 1)[1].split("</span>", 1)[0]
    message = "Must be a whole number of seconds from 5 to 86400."
    assert error == f' class="alert-settings__error">{message}'


def test_a_submitted_switch_is_refilled_after_an_invalid_save(client: TestClient) -> None:
    data = {"alert_daily_cost_enabled": "true", "alert_cooldown_s": "soon"}

    response = post(client, SETTINGS_PATH, data)

    assert saved(client) == {}
    assert "checked" in field_block(response.text, "alert_daily_cost_enabled")
    assert 'value="soon"' in field_block(response.text, "alert_cooldown_s")


def full_form(client: TestClient) -> list[tuple[str, str]]:
    """Every field as the browser submits the form: the hidden false of each switch,
    then its checkbox's true when checked, and each number's text."""
    html = client.get(SETTINGS_PATH).text
    data: list[tuple[str, str]] = []
    for name in ALERT_SETTINGS:
        block = field_block(html, name)
        if ALERT_SETTING_TYPES[name] is bool:
            data.append((name, "false"))
            if "checked" in block:
                data.append((name, "true"))
        else:
            found = re.search(r'value="([^"]*)"', block)
            assert found is not None
            data.append((name, found.group(1)))
    return data


def replaced(data: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    return [(key, value if key == name else text) for key, text in data]


def test_a_full_form_with_one_change_saves_only_that_field(client: TestClient) -> None:
    data = replaced(full_form(client), "alert_waiting_threshold_s", "45")
    calls = spy_saves(client)

    response = post(client, SETTINGS_PATH, data)

    assert response.status_code == 200
    assert calls == [{"alert_waiting_threshold_s": 45}]
    assert saved(client) == {"alert_waiting_threshold_s": 45}
    assert "Saved." in response.text
    html = response.text
    assert "Saved here" in field_block(html, "alert_waiting_threshold_s")
    assert "From the environment" in field_block(html, "alert_cooldown_s")
    assert ">Default<" in field_block(html, "alert_failures_window_s")
    assert html.count('name="field"') == 1


def spy_saves(client: TestClient) -> list[Mapping[str, AlertSettingValue]]:
    """The values of each save the route hands to the writer, from now on."""
    calls: list[Mapping[str, AlertSettingValue]] = []
    real = writer(client).submit_alert_settings_save

    def spy(
        values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> Future[AlertSettingsResult]:
        calls.append(dict(values))
        return real(values, updated_at_ms=updated_at_ms)

    app = client.app
    assert isinstance(app, FastAPI)
    app.state.writer.submit_alert_settings_save = spy
    return calls


def test_an_unchanged_form_writes_nothing(client: TestClient) -> None:
    calls = spy_saves(client)

    response = post(client, SETTINGS_PATH, full_form(client))

    assert response.status_code == 200
    assert calls == []
    assert saved(client) == {}
    assert "No changes to save." in response.text
    assert "Saved." not in response.text
    assert_alerts_not_refreshed(response)
    assert "From the environment" in field_block(response.text, "alert_cooldown_s")


def test_a_saved_field_submitted_unchanged_stays_saved(client: TestClient) -> None:
    repository(client).save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=1)
    data = replaced(full_form(client), "alert_failures_count", "5")
    calls = spy_saves(client)

    response = post(client, SETTINGS_PATH, data)

    assert calls == [{"alert_failures_count": 5}]
    assert saved(client) == {"alert_cooldown_s": 30, "alert_failures_count": 5}
    assert "Saved here" in field_block(response.text, "alert_cooldown_s")


def test_a_value_equal_to_the_one_in_force_is_not_a_change(client: TestClient) -> None:
    # Different text, same value: 060 seconds is 60, 50.0 dollars is 50.
    data = {"alert_cooldown_s": "060", "alert_daily_cost_usd": "50.0"}

    response = post(client, SETTINGS_PATH, data)

    assert saved(client) == {}
    assert "No changes to save." in response.text


def test_errors_from_parsing_and_from_the_bounds_are_reported_together(
    client: TestClient,
) -> None:
    data = {"alert_failures_count": "two", "alert_failures_window_s": "5", "alert_cooldown_s": "1"}

    response = post(client, SETTINGS_PATH, data)

    assert saved(client) == {}
    html = response.text
    assert "Must be a whole number of failures from 2 to 100." in field_block(
        html, "alert_failures_count"
    )
    assert "Must be a whole number of seconds from 60 to 86400." in field_block(
        html, "alert_failures_window_s"
    )
    assert 'id="alert_cooldown_s-error"' not in html


def test_a_form_too_large_is_refused_with_413(client: TestClient) -> None:
    data = {"alert_cooldown_s": "30", "padding": "x" * FORM_MAX_BYTES}

    response = post(client, SETTINGS_PATH, data)

    assert response.status_code == 413
    assert saved(client) == {}
    assert_alerts_not_refreshed(response)


@pytest.mark.parametrize("content", ["alert_cooldown_s=30&é=1".encode(), b"&".join([b"a=1"] * 100)])
def test_a_form_that_does_not_parse_is_refused_with_400(client: TestClient, content: bytes) -> None:
    response = client.post(
        SETTINGS_PATH, content=content, headers={**HTMX, "content-type": FORM_TYPE}
    )

    assert response.status_code == 400
    assert saved(client) == {}
    assert_alerts_not_refreshed(response)


# POST: resets.


def test_a_reset_of_one_field_falls_back_to_the_environment(client: TestClient) -> None:
    post(client, SETTINGS_PATH, {"alert_cooldown_s": "30", "alert_failures_count": "5"})

    response = post(client, RESET_PATH, {"field": "alert_cooldown_s"})

    assert response.status_code == 200
    assert saved(client) == {"alert_failures_count": 5}
    cooldown = field_block(response.text, "alert_cooldown_s")
    assert 'value="60"' in cooldown
    assert "From the environment" in cooldown
    assert writer(client).alert_config().cooldown_s == 60
    assert "Reset." in response.text
    assert_alerts_refreshed(response)


def test_a_reset_without_a_field_resets_all(client: TestClient) -> None:
    post(client, SETTINGS_PATH, {"alert_cooldown_s": "30", "alert_failures_count": "5"})

    response = post(client, RESET_PATH, {"alert_cooldown_s": "45"})

    assert response.status_code == 200
    assert saved(client) == {}
    assert writer(client).alert_config().cooldown_s == 60
    assert writer(client).alert_config().repeated_failures.count == 3
    assert "Reset." in response.text
    assert_alerts_refreshed(response)


def test_a_reset_of_an_unknown_field_is_refused_without_its_name(client: TestClient) -> None:
    post(client, SETTINGS_PATH, {"alert_cooldown_s": "30"})

    response = post(client, RESET_PATH, {"field": CANARY})

    assert response.status_code == 400
    assert CANARY not in response.text
    assert saved(client) == {"alert_cooldown_s": 30}
    assert_alerts_not_refreshed(response)


def test_a_reset_form_too_large_is_refused_with_413(client: TestClient) -> None:
    response = post(client, RESET_PATH, {"field": "x" * FORM_MAX_BYTES})

    assert response.status_code == 413
    assert_alerts_not_refreshed(response)


# The guard (ADR 0045), through the whole app.


@pytest.mark.parametrize(
    "headers",
    [
        {**HTMX, "sec-fetch-site": "cross-site", "origin": "http://evil.example"},
        # Another localhost port is the same site.
        {**HTMX, "sec-fetch-site": "same-site", "origin": "http://localhost:4330"},
        {**HTMX, "origin": "null"},
        {key: value for key, value in HTMX.items() if key != "origin"},
        {**HTMX, "origin": "http://127.0.0.1:4330"},
        {key: value for key, value in HTMX.items() if key != "hx-request"},
        # A plain HTML form of our own page: a navigation without HX-Request.
        {
            "origin": ORIGIN,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        },
    ],
    ids=[
        "cross-site",
        "same-site",
        "origin-null",
        "no-origin",
        "origin-other-port",
        "no-hx-request",
        "plain-form",
    ],
)
@pytest.mark.parametrize("path", [SETTINGS_PATH, RESET_PATH])
def test_a_post_that_is_not_a_same_origin_htmx_request_is_refused(
    client: TestClient, headers: dict[str, str], path: str
) -> None:
    repository(client).save_alert_setting(HOST, "alert_cooldown_s", 30, updated_at_ms=1)

    response = post(client, path, {"alert_cooldown_s": "45"}, headers=headers)

    assert response.status_code == 403
    assert response.content == b"forbidden\n"
    assert_alerts_not_refreshed(response)
    assert saved(client) == {"alert_cooldown_s": 30}


@pytest.mark.parametrize("content_type", ["text/plain", "application/json", "multipart/form-data"])
def test_a_same_origin_post_of_another_media_type_gets_415(
    client: TestClient, content_type: str
) -> None:
    response = client.post(
        SETTINGS_PATH,
        content=b"alert_cooldown_s=45",
        headers={**HTMX, "content-type": content_type},
    )

    assert response.status_code == 415
    assert saved(client) == {}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_methods_still_get_405(client: TestClient, method: str) -> None:
    for path in (SETTINGS_PATH, RESET_PATH):
        response = client.request(method, path, headers=HTMX)

        assert response.status_code == 405


def test_get_on_the_reset_route_gets_405(client: TestClient) -> None:
    assert client.get(RESET_PATH).status_code == 405


# The writer: a change it does not apply in time, or cannot take.


class StuckWriter(Writer):
    """A writer whose settings changes never complete."""

    futures: list[Future[AlertSettingsResult]]

    def submit_alert_settings_save(
        self, values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> Future[AlertSettingsResult]:
        future: Future[AlertSettingsResult] = Future()
        self.futures.append(future)
        return future

    def submit_alert_settings_reset(self, field: str | None) -> Future[AlertSettingsResult]:
        raise AlertSettingsUnavailableError("too many settings changes waiting")


def stuck_app(settings: Settings) -> FastAPI:
    def make(queue: HookQueue, repo: Repository, observers: WriterObservers) -> Writer:
        stuck = StuckWriter(queue, repo, settings.host_label, stale_after_ms=60_000)
        stuck.futures = []
        return stuck

    return create_app(settings, writer_factory=make, settings_timeout_s=0.05)


def test_a_change_without_an_answer_in_time_gets_503_and_is_given_up(
    settings: Settings,
) -> None:
    with _client(stuck_app(settings)) as client:
        response = post(client, SETTINGS_PATH, {"alert_cooldown_s": "30"})
        stuck = writer(client)
        assert isinstance(stuck, StuckWriter)
        (future,) = stuck.futures

    assert response.status_code == 503
    assert "not saved" in response.text
    assert_alerts_not_refreshed(response)
    # Cancelled before the writer took it: it will never be applied.
    assert future.cancelled()


def test_a_change_the_writer_cannot_take_gets_503(settings: Settings) -> None:
    with _client(stuck_app(settings)) as client:
        response = post(client, RESET_PATH, {"field": "alert_cooldown_s"})

    assert response.status_code == 503
    assert "not saved" in response.text
    assert_alerts_not_refreshed(response)


class RefusingWriter(Writer):
    """A writer that refuses every save with an error, as when the configuration it
    checks against changed since the route read it."""

    def submit_alert_settings_save(
        self, values: Mapping[str, AlertSettingValue], *, updated_at_ms: int
    ) -> Future[AlertSettingsResult]:
        future: Future[AlertSettingsResult] = Future()
        errors = tuple(setting_error(name) for name in values)
        future.set_result(AlertSettingsResult(saved={}, errors=errors))
        return future


def test_errors_from_the_writer_save_nothing_and_refill_the_form(settings: Settings) -> None:
    def make(queue: HookQueue, repo: Repository, observers: WriterObservers) -> Writer:
        return RefusingWriter(queue, repo, settings.host_label, stale_after_ms=60_000)

    app = create_app(settings, writer_factory=make)
    with _client(app) as client:
        response = post(client, SETTINGS_PATH, {"alert_cooldown_s": "120"})

    assert response.status_code == 200
    assert "Nothing was saved" in response.text
    block = field_block(response.text, "alert_cooldown_s")
    assert 'value="120"' in block
    assert 'aria-invalid="true"' in block
    assert "Saved here" not in block
    assert_alerts_not_refreshed(response)


def test_an_invalid_field_with_a_failed_read_answers_503(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)

    def fail(host: str) -> dict[str, AlertSettingValue]:
        raise OperationalError("SELECT value", None, Exception(CANARY))

    app.state.read_repository.alert_settings = fail
    response = post(client, SETTINGS_PATH, {"alert_cooldown_s": "soon"})

    assert response.status_code == 503
    assert CANARY not in response.text
    assert saved(client) == {}
    assert_alerts_not_refreshed(response)
