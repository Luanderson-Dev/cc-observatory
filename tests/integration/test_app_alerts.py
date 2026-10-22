"""Alerts on the real app container (ADRs 0041 to 0046).

Needs Docker: ``uv run pytest -m integration --no-cov``. Against the isolated
``ccobs-it`` project:

- a session waiting on a permission fires a ``waiting_user`` alert end to end: the
  waiting threshold is lowered to its minimum through the settings form's guarded POST
  (with the headers htmx sends from our own page), and reset when the test ends, so no
  saved setting is left behind; the alert shows in ``/api/v1/alerts`` (project name, never
  the ``cwd``, no Discord field) and on ``/alerts``, an open stream gets the fixed alerts
  ping (never an alert, session or value), and it clears once the session moves on;
- the guard of the state-changing routes (ADR 0045) in the real container: a POST
  without ``Origin`` and ``HX-Request``, or from another site, is refused and changes
  nothing, while the hook route still answers its neutral empty 204;
- the diagnostics route, ``ccobs status --json`` and ``ccobs doctor --json`` report the
  alerts, with Discord not configured (the project has no webhook): an info, not a
  failure (ADR 0046).

Last, in a compose project of its own, ``ccobs-it-discord`` (the app and a sink only,
``tests/integration/compose.discord-sink.yaml``, with no route out and no published
port): the app opens no connection to discord.com while no webhook is set, and once a
fake one is (its host resolves to the sink), the sink sees a TLS ClientHello that names
discord.com. The fake token never reaches the app's logs. That project starts the app
twice and never touches ``ccobs-it``.

Each test replays a recorded hook fixture under a fresh session ID, so none depends on
the order of the tests or of the other integration files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlencode, urlsplit

import pytest
from app_support import (
    HTTP_TIMEOUT,
    Answer,
    EventStream,
    detail,
    get,
    get_json,
    items,
    mapping,
    open_stream,
    poll,
    request,
)

from ccobs.cli import main
from ccobs.domain.alerts import WAITING_BOUNDS
from ccobs.domain.presentation import project_name
from ccobs.server.alert_settings import RESET_PATH, SETTINGS_PATH
from ccobs.server.live import ALERTS_EVENT
from tools.stack.isolated import ENV_FILE, compose_environment
from tools.stack.replay import (
    HOOKS_PATH,
    HookLine,
    PreparedHooks,
    load_scenario,
    prepare_hooks,
    send_hooks,
)

pytestmark = pytest.mark.integration

ROOT: Final = Path(__file__).resolve().parents[2]
ALERTS_API: Final = "/api/v1/alerts"
DIAGNOSTICS_API: Final = "/api/v1/diagnostics"
ALERTS_PAGE: Final = "/alerts"
PAGE: Final = 500
# A session that waits on a Bash permission, then goes on and ends (compacting on the
# way); cut right after its first PermissionRequest it is waiting on you.
SCENARIO: Final = "interactive-permission-compact"
CUT: Final = "PermissionRequest"
THRESHOLD_FIELD: Final = "alert_waiting_threshold_s"
THRESHOLD_S: Final = WAITING_BOUNDS.minimum
RULE: Final = "waiting_user"
# What an alert of the API holds, and nothing of its Discord delivery (ADR 0044).
ALERT_FIELDS: Final = frozenset(
    {
        "id",
        "rule",
        "subject_kind",
        "session_id",
        "project",
        "fired_at_ms",
        "cleared_at_ms",
        "value",
        "threshold",
    }
)
# The alert ticker runs every 5 s (ccobs.server.sweeps): the threshold, a tick, and a
# generous margin for a loaded runner.
FIRE_DEADLINE: Final = 45.0
CLEAR_DEADLINE: Final = 30.0
# The ping comes within about 0.3 s of the writer's change (ADR 0044); generous.
PING_LIMIT_S: Final = 10.0
COUNTS_DEADLINE: Final = 15.0
SHORT_ID: Final = 8
DAY_MS: Final = 86_400_000
# A Windows or POSIX path with a user directory, or a drive (test_app_privacy.py).
PATH_PATTERN: Final = re.compile(r"\\Users\\|/home/|/Users/|[A-Za-z]:\\", re.IGNORECASE)
PING: Final = re.compile(rb"event: alerts\n")
PING_BLOCK: Final = re.compile(rb"event: alerts\n(.*?)\n\n", re.DOTALL)
FORBIDDEN: Final = b"forbidden\n"
FORM_TYPE: Final = "application/x-www-form-urlencoded"
NO_DISCORD: Final = "Discord is not configured; alerts show in the browser only."


# The settings form, posted as htmx does from our own page.


def _same_origin(app_url: str) -> dict[str, str]:
    """The headers of an htmx POST from the app's own page: ``http.client`` sends
    ``Host: 127.0.0.1:<port>``, which ``Origin`` must repeat (ADR 0045)."""
    return {
        "Origin": app_url,
        "HX-Request": "true",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": FORM_TYPE,
    }


def _post_form(
    app_url: str, path: str, form: Mapping[str, str], headers: Mapping[str, str]
) -> Answer:
    return request(app_url, "POST", path, headers=headers, body=urlencode(form).encode("ascii"))


def _input_value(fragment: str, name: str) -> str:
    """The value of the form's input ``name`` (the hidden switch inputs come first, so
    the last one with that name)."""
    found = re.findall(rf'<input[^>]*\bname="{re.escape(name)}"[^>]*>', fragment)
    assert found, f"no input {name}"
    value = re.search(r'\bvalue="([^"]*)"', found[-1])
    assert value is not None, found[-1]
    return value[1]


@pytest.fixture
def low_threshold(app_url: str) -> Iterator[None]:
    """The waiting threshold at its minimum, saved through the guarded form; the saved
    value is dropped when the test ends, so the environment's value applies again. The
    save sits inside the try: a save that landed but answered unexpectedly is still
    reset, so no saved value outlives the test (CCOBS_IT_KEEP reuses the volume)."""
    try:
        answer = _post_form(
            app_url, SETTINGS_PATH, {THRESHOLD_FIELD: str(THRESHOLD_S)}, _same_origin(app_url)
        )
        assert answer.status == 200, (answer.status, answer.body[:300])
        saved = b"Saved." in answer.body or b"No changes to save." in answer.body
        assert saved, answer.body[:500]
        yield
    finally:
        reset = _post_form(app_url, RESET_PATH, {"field": THRESHOLD_FIELD}, _same_origin(app_url))
        assert reset.status == 200, (reset.status, reset.body[:300])
        assert b"Reset." in reset.body


# The alerts.


def _alerts(app_url: str) -> list[dict[str, object]]:
    found = [
        mapping(item, "alert")
        for item in items(get_json(app_url, ALERTS_API, {"limit": PAGE}), "alerts")
    ]
    assert len(found) < PAGE, "more alerts than one page holds"
    return found


def _session_alerts(app_url: str, session_id: str) -> list[dict[str, object]]:
    return [alert for alert in _alerts(app_url) if alert["session_id"] == session_id]


def _cut(run: PreparedHooks) -> int:
    """How many of ``run``'s lines lead to its first ``CUT``, that one included."""
    names = [line.payload.get("hook_event_name") for line in run.lines]
    return names.index(CUT) + 1


def _ping_blocks(stream: EventStream) -> list[bytes]:
    return [match[1] for match in PING_BLOCK.finditer(stream.body)]


def _diagnostics_alerts(app_url: str) -> dict[str, object]:
    document = mapping(get_json(app_url, DIAGNOSTICS_API), "diagnostics")
    return mapping(document["alerts"], "alerts")


def _counts(app_url: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """The alerts on and fired in the last 24 hours, as the diagnostics route counts
    them and as the API lists them."""
    reported = _diagnostics_alerts(app_url)
    now_ms = int(time.time() * 1000)
    listed = _alerts(app_url)
    on = sum(1 for alert in listed if alert["cleared_at_ms"] is None)
    fired = sum(1 for alert in listed if int(str(alert["fired_at_ms"])) > now_ms - DAY_MS)
    return (int(str(reported["open"])), int(str(reported["fired_24h"]))), (on, fired)


def _consistent_counts(app_url: str) -> tuple[int, int]:
    """The diagnostics' counts once they agree with the API (an alert of another
    session can fire between the two reads)."""
    reported, listed = poll(
        lambda: _counts(app_url), lambda found: found[0] == found[1], COUNTS_DEADLINE
    )
    assert reported == listed, (reported, listed)
    return reported


def _send_rest(app_url: str, lines: Sequence[HookLine]) -> None:
    if lines:
        send_hooks(app_url, lines)


def test_a_session_waiting_on_you_fires_an_alert_that_clears(
    app_url: str, low_threshold: None
) -> None:
    run = prepare_hooks(load_scenario(SCENARIO))
    cut = _cut(run)
    stream = open_stream(app_url, "/ui/stream")
    rest: Sequence[HookLine] = run.lines[cut:]
    try:
        send_hooks(app_url, run.lines[:cut])
        session = poll(
            lambda: detail(app_url, run.session_id),
            lambda found: found is not None and found["status"] == RULE,
            HTTP_TIMEOUT,
        )
        assert session is not None
        assert session["status"] == RULE, session
        cwd = str(session["cwd"])

        found = poll(
            lambda: _session_alerts(app_url, run.session_id),
            lambda alerts: bool(alerts),
            FIRE_DEADLINE,
        )
        assert len(found) == 1, found
        alert = found[0]
        assert set(alert) == ALERT_FIELDS, sorted(alert)
        assert alert["rule"] == RULE
        assert alert["subject_kind"] == "session"
        assert alert["project"] == project_name(cwd) == SCENARIO
        assert alert["cleared_at_ms"] is None
        assert alert["threshold"] == THRESHOLD_S
        assert float(str(alert["value"])) >= THRESHOLD_S
        document = json.dumps(found)
        assert cwd not in document
        assert not PATH_PATTERN.search(document), document

        # The open stream got the alerts ping: the fixed marker, nothing about the alert.
        assert stream.next_match(PING, PING_LIMIT_S) is not None, stream.body[-300:]
        blocks = _ping_blocks(stream)
        assert blocks
        assert set(blocks) == {b"data: alerts"}, blocks
        assert ALERTS_EVENT in stream.body

        # Diagnostics count it with the API's alerts.
        on, fired = _consistent_counts(app_url)
        assert on >= 1, on
        assert fired >= 1, fired

        # The alerts page shows the project name and the short ID, never the cwd.
        page = get(app_url, ALERTS_PAGE).decode("utf-8")
        strip = re.search(
            rf'<a class="strip__project" href="/sessions/{re.escape(quote(run.session_id))}">'
            r'([^<]*)</a>\s*<code class="strip__id">([^<]*)</code>',
            page,
        )
        assert strip is not None, "no strip for the session's alert"
        assert strip[1] == SCENARIO
        assert strip[2] == run.session_id[:SHORT_ID]
        assert cwd not in page
        assert not PATH_PATTERN.search(page), PATH_PATTERN.search(page)

        # The session goes on and ends: the alert clears, and the stream hears of it.
        send_hooks(app_url, rest)
        rest = ()
        cleared = poll(
            lambda: _session_alerts(app_url, run.session_id),
            lambda alerts: bool(alerts) and all(a["cleared_at_ms"] is not None for a in alerts),
            CLEAR_DEADLINE,
        )
        assert len(cleared) == 1, cleared
        assert cleared[0]["id"] == alert["id"]
        assert int(str(cleared[0]["cleared_at_ms"])) >= int(str(alert["fired_at_ms"]))
        assert stream.next_match(PING, PING_LIMIT_S) is not None, stream.body[-300:]
        assert set(_ping_blocks(stream)) == {b"data: alerts"}
    finally:
        stream.close()
        # Never leave the session waiting, whatever failed above.
        _send_rest(app_url, rest)


# The guard of the state-changing routes (ADR 0045).


def test_the_settings_form_refuses_requests_that_are_not_our_pages(app_url: str) -> None:
    before = get(app_url, SETTINGS_PATH).decode("utf-8")
    value = _input_value(before, "alert_cooldown_s")
    changed = str(int(value) + 7)
    form = {"alert_cooldown_s": changed}
    plain = {"Content-Type": FORM_TYPE}
    cross_site = {**_same_origin(app_url), "Sec-Fetch-Site": "cross-site"}
    other_origin = {**_same_origin(app_url), "Origin": "http://evil.example"}
    no_htmx = {key: item for key, item in _same_origin(app_url).items() if key != "HX-Request"}
    for headers in (plain, cross_site, other_origin, no_htmx):
        answer = _post_form(app_url, SETTINGS_PATH, form, headers)
        assert answer.status == 403, (headers, answer.status)
        assert answer.body == FORBIDDEN, headers
        reset = _post_form(app_url, RESET_PATH, {}, headers)
        assert reset.status == 403, (headers, reset.status)
    # A form of another type: 415, also before the body is read.
    typed = {**_same_origin(app_url), "Content-Type": "text/plain"}
    assert _post_form(app_url, SETTINGS_PATH, form, typed).status == 415
    # Nothing was saved.
    after = get(app_url, SETTINGS_PATH).decode("utf-8")
    assert _input_value(after, "alert_cooldown_s") == value
    assert after.count("Saved here") == before.count("Saved here")
    # The hook route is not behind the guard: its neutral empty answer, whatever the
    # headers.
    line = prepare_hooks(load_scenario(SCENARIO)).lines[0]
    body = json.dumps(line.payload).encode()
    for headers in (
        {"Content-Type": "application/json"},
        {
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "cross-site",
            "Origin": "http://evil.example",
        },
    ):
        answer = request(app_url, "POST", HOOKS_PATH, headers=headers, body=body)
        assert answer.status == 204, (headers, answer.status)
        assert answer.body == b"", headers


# Diagnostics, ccobs status and ccobs doctor (ADR 0046).


@pytest.fixture
def cli_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Claude Code configuration directory; HOME, USERPROFILE,
    CLAUDE_CONFIG_DIR and the working directory point at temporary ones, so the CLI
    reads no real ~/.claude, project .claude/ or clone .env (test_cli_flow.py)."""
    isolation = tmp_path_factory.mktemp("alerts-cli")
    home = isolation / "home"
    home.mkdir()
    workdir = isolation / "work"
    workdir.mkdir()
    config = isolation / "claude-config"
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolation / "env-config"))
    for name in list(os.environ):
        if name.upper().startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(workdir)
    return config


def _port(url: str) -> str:
    port = urlsplit(url).port
    assert port is not None, url
    return str(port)


def _cli(
    capsys: pytest.CaptureFixture[str], command: str, config: Path, ports: Sequence[str]
) -> dict[str, object]:
    app, otlp, grafana = ports
    capsys.readouterr()
    main(
        [
            command,
            "--config-dir",
            str(config),
            "--app-port",
            app,
            "--otlp-port",
            otlp,
            "--grafana-port",
            grafana,
            "--json",
        ]
    )
    return mapping(json.loads(capsys.readouterr().out), command)


def test_diagnostics_status_and_doctor_report_the_alerts(
    app_url: str,
    otlp_http_url: str,
    grafana_url: str,
    cli_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reported = _diagnostics_alerts(app_url)
    assert RULE in items(reported["rules"], "rules")
    discord = mapping(reported["discord"], "discord")
    assert discord == {
        "configured": False,
        "suspended_status": None,
        "pending": 0,
        "failed_24h": 0,
        "expired_24h": 0,
    }
    ports = (_port(app_url), _port(otlp_http_url), _port(grafana_url))

    def status_counts() -> tuple[tuple[int, int], tuple[int, int]]:
        alerts = mapping(_cli(capsys, "status", cli_home, ports)["alerts"], "alerts")
        shown = (int(str(alerts["open"])), int(str(alerts["fired_24h"])))
        return shown, _consistent_counts(app_url)

    # What status shows is what the route and the API count (an alert of another
    # session may fire or clear between the reads, hence the poll).
    shown, counted = poll(status_counts, lambda found: found[0] == found[1], COUNTS_DEADLINE)
    assert shown == counted, (shown, counted)
    status = _cli(capsys, "status", cli_home, ports)
    alerts = mapping(status["alerts"], "alerts")
    assert alerts["available"] is True, alerts
    assert RULE in items(alerts["rules"], "rules")
    assert mapping(alerts["discord"], "discord")["configured"] is False

    doctor = _cli(capsys, "doctor", cli_home, ports)
    checks = {
        str(check["id"]): check
        for check in (mapping(item, "check") for item in items(doctor["checks"], "checks"))
    }
    check = checks["alerts"]
    assert check["status"] == "info", check
    assert check["title"] == "alerts show in the browser only", check
    assert NO_DISCORD in str(check["detail"]), check
    assert "rules on:" in str(check["detail"]), check
    assert RULE in str(check["detail"]), check


# Discord egress, in a project of its own.

DISCORD_PROJECT: Final = "ccobs-it-discord"
DISCORD_OVERRIDE: Final = Path(__file__).resolve().parent / "compose.discord-sink.yaml"
SINK: Final = "discord-sink"
APP: Final = "app"
# Its app's port is not published (edge is internal), but compose.yaml's port mapping
# still names one: moved off the user's stack and ccobs-it's.
DISCORD_PORTS: Final = {"CCOBS_APP_PORT": 24302, "CCOBS_GRAFANA_PORT": 24332}
# A webhook URL of the documented shape, on discord.com, which only the sink answers.
FAKE_TOKEN: Final = f"ccobs-it-fake-token-{uuid.uuid4().hex}"
FAKE_WEBHOOK: Final = f"https://discord.com/api/webhooks/123456789012345678/{FAKE_TOKEN}"
UP_TIMEOUT: Final = 900.0
DOWN_TIMEOUT: Final = 300.0
COMMAND_TIMEOUT: Final = 120.0
WAIT_TIMEOUT: Final = 180
# Seconds for the sender (every 5 s) to post an alert after it fires; and how long to
# watch the sink while no webhook is set: well past several sender intervals.
SEND_DEADLINE: Final = 60.0
QUIET_S: Final = 15.0
CONNECTION_LINE: Final = "sink: connection to"
# Inside the app's container: POST each JSON line of stdin to the hook route, and GET a
# path, both on the app's own port, with no proxy.
POST_HOOKS: Final = (
    "import sys, urllib.request as u\n"
    "opener = u.build_opener(u.ProxyHandler({}))\n"
    "for line in sys.stdin:\n"
    "    if line.strip():\n"
    "        r = u.Request('http://127.0.0.1:4300/hooks', data=line.encode(),\n"
    "                      headers={'Content-Type': 'application/json'}, method='POST')\n"
    "        opener.open(r, timeout=5).read()\n"
)
GET_PATH: Final = (
    "import sys, urllib.request as u\n"
    "opener = u.build_opener(u.ProxyHandler({}))\n"
    "answer = opener.open('http://127.0.0.1:4300' + sys.argv[1], timeout=5)\n"
    "sys.stdout.write(answer.read().decode())\n"
)


class DiscordProject:
    """The ``ccobs-it-discord`` project: compose.yaml and the Discord sink override,
    with ``.env.example`` and no inherited ``CCOBS_*`` or ``COMPOSE_*`` variable."""

    def __init__(self, docker: str) -> None:
        self.docker = docker
        self.base_env = {
            **compose_environment(os.environ, DISCORD_PORTS),
            "CCOBS_ALERT_WAITING_THRESHOLD_S": str(THRESHOLD_S),
            # No Loki in this project: nothing to reconcile.
            "CCOBS_TELEMETRY_RECONCILE": "false",
        }
        self.env = dict(self.base_env)

    def run(
        self, *args: str, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self.docker,
            "compose",
            "--project-name",
            DISCORD_PROJECT,
            "--env-file",
            str(ROOT / ENV_FILE),
            "--file",
            str(ROOT / "compose.yaml"),
            "--file",
            str(DISCORD_OVERRIDE),
            *args,
        ]
        # S603: docker comes from shutil.which and every argument is fixed here.
        return subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def checked(
        self, *args: str, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT
    ) -> str:
        result = self.run(*args, stdin=stdin, timeout=timeout)
        # Never the command's environment or `config`: the webhook URL is a secret.
        assert result.returncode == 0, (
            f"`docker compose {args[0]}` exited with {result.returncode}:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
        return result.stdout

    def up(self, webhook: str | None) -> None:
        """Start (or recreate) the app with ``webhook`` as its Discord webhook URL."""
        self.env = dict(self.base_env)
        if webhook is not None:
            self.env["CCOBS_DISCORD_WEBHOOK_URL"] = webhook
        self.checked(
            "up",
            "--detach",
            "--build",
            "--wait",
            "--wait-timeout",
            str(WAIT_TIMEOUT),
            APP,
            timeout=UP_TIMEOUT,
        )

    def get_json(self, path: str) -> object:
        document: object = json.loads(
            self.checked("exec", "-T", APP, "python", "-c", GET_PATH, path)
        )
        return document

    def send(self, lines: Sequence[HookLine]) -> None:
        payloads = "".join(json.dumps(line.payload) + "\n" for line in lines)
        self.checked("exec", "-T", APP, "python", "-c", POST_HOOKS, stdin=payloads)

    def logs(self, service: str) -> str:
        result = self.run("logs", "--no-color", service)
        return result.stdout + result.stderr

    def down(self) -> None:
        result = self.run("down", "--volumes", "--remove-orphans", timeout=DOWN_TIMEOUT)
        if result.returncode != 0:
            pytest.fail(
                f"removing {DISCORD_PROJECT} failed; run: docker compose -p {DISCORD_PROJECT} "
                f"down -v --remove-orphans\n{result.stderr[-2000:]}"
            )


@pytest.fixture(scope="module")
def discord_project() -> Iterator[DiscordProject]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("the integration tests need Docker on PATH", pytrace=False)
    project = DiscordProject(docker)
    # A session killed before its teardown leaves the project up: its sink's log would
    # still hold the earlier run's connections and its app volume the earlier alerts.
    project.down()
    try:
        yield project
    finally:
        project.down()


def _fired(project: DiscordProject, session_id: str) -> list[dict[str, object]]:
    listed = items(project.get_json(f"{ALERTS_API}?limit={PAGE}"), "alerts")
    return [
        alert
        for alert in (mapping(item, "alert") for item in listed)
        if alert["session_id"] == session_id and alert["rule"] == RULE
    ]


def _wait_for_alert(project: DiscordProject) -> str:
    """Replay a waiting session in the project's app until its alert fires."""
    run = prepare_hooks(load_scenario(SCENARIO))
    project.send(run.lines[: _cut(run)])
    fired = poll(lambda: _fired(project, run.session_id), lambda found: bool(found), FIRE_DEADLINE)
    assert fired, "no waiting alert fired"
    return run.session_id


def _connections(project: DiscordProject) -> list[str]:
    return [line for line in project.logs(SINK).splitlines() if CONNECTION_LINE in line]


def test_alerts_reach_discord_com_only_when_a_webhook_is_set(
    discord_project: DiscordProject,
) -> None:
    # No webhook: an alert fires, and nothing connects to discord.com.
    discord_project.up(webhook=None)
    diagnostics = mapping(discord_project.get_json(DIAGNOSTICS_API), "diagnostics")
    discord = mapping(mapping(diagnostics["alerts"], "alerts")["discord"], "discord")
    assert discord["configured"] is False
    _wait_for_alert(discord_project)
    time.sleep(QUIET_S)
    assert _connections(discord_project) == [], discord_project.logs(SINK)

    # A webhook on discord.com: the next alert goes out, and the sink, standing in for
    # discord.com, sees its TLS ClientHello with that server name.
    discord_project.up(webhook=FAKE_WEBHOOK)
    diagnostics = mapping(discord_project.get_json(DIAGNOSTICS_API), "diagnostics")
    discord = mapping(mapping(diagnostics["alerts"], "alerts")["discord"], "discord")
    assert discord["configured"] is True
    _wait_for_alert(discord_project)
    logs = poll(
        lambda: discord_project.logs(SINK),
        lambda text: CONNECTION_LINE in text and "discord.com" in text,
        SEND_DEADLINE,
    )
    connections = [line for line in logs.splitlines() if CONNECTION_LINE in line]
    assert connections, logs
    assert all(":443 " in line for line in connections), connections
    # The bytes the sink logs (each line under "sink:   "): the server name is there.
    sent = [line for line in logs.splitlines() if "sink:   " in line]
    assert any("discord.com" in line for line in sent), logs
    # The ClientHello carries the server name only: the path, with its token, would be
    # inside the TLS session that never started.
    assert FAKE_TOKEN not in logs
    assert FAKE_TOKEN not in discord_project.logs(APP)
