"""No content, no path and no session ID leaks from the app, in the isolated ``ccobs-it``
compose project (ADRs 0006, 0023 and 0029).

Needs Docker: ``uv run pytest -m integration --no-cov``. Every hook fixture is replayed
with canaries in every content field (``tools/stack/replay.py``), and
``interactive-permission-compact`` also with its telemetry, under the same session ID
and canary marker, so that the reconciler queries Loki and closes the dialog denied with
Esc; then two hooks the app drops carry a canary too (another Content-Type, and a body
that is not JSON). The canaries must then be nowhere: not in the API's answers, the
metrics, the app's container logs, Loki's logs or the SQLite database on the volume (its
``-wal`` included). Loki
logs every query with its LogQL at ``info``, so no session ID of the app may appear in
its logs, and the app's logs must hold no path (``cwd`` holds the OS username).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from app_support import (
    API,
    APP,
    LINKED_SESSION_PREFIX,
    PAGE,
    detail,
    events,
    get,
    items,
    mapping,
    poll,
    request,
    samples,
    sessions,
)

from ccobs.domain.app_metrics import HOOK_EVENTS_DROPPED
from ccobs.domain.denials import DECISION_EVENT, REJECT
from ccobs.domain.grafana_links import SERVICE_NAME_LABEL, SESSION_ID_KEY
from ccobs.domain.telemetry import DEFAULT_HOST, HOST_LABEL, SERVICE_NAME, quote
from tools.stack.replay import (
    HOOKS_PATH,
    Prepared,
    PreparedHooks,
    load_scenario,
    prepare,
    prepare_hooks,
    scenario_names,
    send,
    send_hooks,
)

pytestmark = pytest.mark.integration

LOKI: Final = "loki"
SCENARIO: Final = "interactive-permission-compact"
DATABASE_DIR: Final = "/var/lib/ccobs"
DATABASE_FILES: Final = ("ccobs.db", "ccobs.db-wal", "ccobs.db-shm")
# Seconds for the telemetry close (see test_app_reconcile.py) and for the writer.
CLOSE_DEADLINE: Final = 30.0
STORED_DEADLINE: Final = 15.0
# A Windows or POSIX path with a user directory, or a drive.
PATH_PATTERN: Final = re.compile(r"\\Users\\|/home/|/Users/|[A-Za-z]:\\", re.IGNORECASE)
# The dropped hooks: a session ID no replay uses, and the drop reasons they are counted
# under (ADR 0027).
DROPPED_SESSION: Final = "00000000-0000-4000-8000-0000000000d1"
DROP_REASONS_SENT: Final = ("bad_content_type", "invalid")
# What the app logs at info when it starts (ccobs.server.writer).
STARTUP_LINE: Final = "writer loaded"
UUID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


@dataclass(frozen=True)
class Replay:
    telemetry: Prepared
    runs: tuple[PreparedHooks, ...]

    @property
    def marker(self) -> str:
        marker = self.telemetry.canary_marker
        assert marker is not None
        return marker


def _denied_block(app_url: str, session_id: str) -> dict[str, object] | None:
    found = detail(app_url, session_id)
    if found is None:
        return None
    denied = [
        mapping(item, "block")
        for item in items(found["blocks"], "blocks")
        if mapping(item, "block")["close_reason"] == "denied"
    ]
    return denied[0] if denied else None


@pytest.fixture(scope="module")
def replay(app_url: str, app_metrics_url: str, otlp_http_url: str) -> Replay:
    scenario = load_scenario(SCENARIO)
    telemetry = prepare(scenario, now_ns=time.time_ns(), canaries=True)
    marker = telemetry.canary_marker
    compact = prepare_hooks(
        scenario, session_id=telemetry.session_id, canaries=True, canary_marker=marker
    )
    send(otlp_http_url, telemetry)
    names = [line.payload.get("hook_event_name") for line in compact.lines]
    split = names.index("PreCompact")
    send_hooks(app_url, compact.lines[:split])
    closed = poll(
        lambda: _denied_block(app_url, telemetry.session_id),
        lambda block: block is not None,
        CLOSE_DEADLINE,
    )
    assert closed is not None, "the denied block did not close from telemetry"
    assert closed["close_source"] == "telemetry", closed
    send_hooks(app_url, compact.lines[split:])
    others = tuple(
        prepare_hooks(load_scenario(name), canaries=True, canary_marker=marker)
        for name in scenario_names(with_hooks=True)
        if name != SCENARIO
    )
    for run in others:
        send_hooks(app_url, run.lines)
    runs = (compact, *others)
    expected = {run.session_id: len(run.lines) for run in runs}
    counts = poll(
        lambda: {session_id: len(events(app_url, session_id)) for session_id in expected},
        lambda found: found == expected,
        STORED_DEADLINE,
    )
    # Every canary payload was accepted and stored, none dropped as invalid.
    assert counts == expected
    assert all(run.canary_values for run in runs)
    _send_dropped_payloads(app_url, app_metrics_url, str(marker))
    return Replay(telemetry, runs)


def _dropped(app_metrics_url: str) -> dict[str, float]:
    found = samples(get(app_metrics_url, "/metrics").decode())
    return {
        dict(labels)["reason"]: value
        for (name, labels), value in found.items()
        if name == f"{HOOK_EVENTS_DROPPED.name}_total" or name == HOOK_EVENTS_DROPPED.name
    }


def _send_dropped_payloads(app_url: str, app_metrics_url: str, marker: str) -> None:
    """Two hooks the app drops, each carrying a canary: one with another Content-Type,
    dropped by the route, and one whose body is not JSON, dropped by the writer. Both are
    answered with an empty 204, and neither may reach a log."""
    before = _dropped(app_metrics_url)
    payload = json.dumps({"session_id": DROPPED_SESSION, "hook_event_name": "Stop", "x": marker})
    for content_type, body in (
        ("text/plain", payload.encode()),
        ("application/json", f"not json {marker}".encode()),
    ):
        answer = request(
            app_url, "POST", HOOKS_PATH, headers={"Content-Type": content_type}, body=body
        )
        assert (answer.status, answer.body) == (204, b"")
    counted = poll(
        lambda: _dropped(app_metrics_url),
        lambda found: all(
            found.get(reason, 0) > before.get(reason, 0) for reason in DROP_REASONS_SENT
        ),
        STORED_DEADLINE,
    )
    for reason in DROP_REASONS_SENT:
        assert counted.get(reason, 0) > before.get(reason, 0), reason


def test_the_api_and_the_metrics_serve_no_canary(
    app_url: str, app_metrics_url: str, replay: Replay
) -> None:
    assert replay.marker.encode() not in get(app_metrics_url, "/metrics")
    answers = [get(app_url, API, {"limit": PAGE})]
    for session in sessions(app_url):
        session_id = str(session["session_id"])
        answers.append(get(app_url, f"{API}/{session_id}"))
        answers.append(get(app_url, f"{API}/{session_id}/events", {"limit": PAGE}))
    served = b"\n".join(answers)
    for run in replay.runs:
        assert run.session_id.encode() in served
    assert replay.marker.encode() not in served


def test_the_app_logs_no_canary_and_no_path(
    service_logs: Callable[[str], str],
    service_started: Callable[[str], bool],
    replay: Replay,
) -> None:
    logs = service_logs(APP)
    # The control: the logs are the app's, start-up included (test_app_ingest.py
    # restarts it, so it started in this session unless that module was left out).
    if service_started(APP):
        assert STARTUP_LINE in logs, "the app's start-up line is missing"
    assert replay.marker not in logs
    assert DROPPED_SESSION not in logs
    paths = [line for line in logs.splitlines() if PATH_PATTERN.search(line)]
    assert not paths, f"{len(paths)} log lines hold a path"
    cwds = {
        str(line.payload["cwd"])
        for run in replay.runs
        for line in run.lines
        if "cwd" in line.payload
    }
    assert cwds
    assert not [cwd for cwd in cwds if cwd in logs]
    # Nor a session ID: the app logs counts and type names only.
    assert not UUID_PATTERN.search(logs)


def test_loki_logs_no_canary_and_no_session_id_of_the_app(
    service_logs: Callable[[str], str], app_url: str, replay: Replay
) -> None:
    logs = service_logs(LOKI)
    # The control: Loki logs each query with its LogQL, the app's included.
    assert "tool_decision" in logs, "Loki logged no query of the app"
    assert "reject" in logs, "Loki logged no query of the app"
    assert replay.marker not in logs
    session_ids = [str(session["session_id"]) for session in sessions(app_url)]
    assert replay.telemetry.session_id in session_ids
    # test_app_ui.py follows the Logs link of its linked sessions, as a user would: every
    # line that names one must be that link's query, never one of the app's.
    linked = [
        session_id for session_id in session_ids if session_id.startswith(LINKED_SESSION_PREFIX)
    ]
    for session_id in linked:
        for line in logs.splitlines():
            if session_id in line:
                assert _is_logs_link_query(line, session_id), line
    others = [session_id for session_id in session_ids if session_id not in linked]
    assert not [session_id for session_id in others if session_id in logs]


def _is_logs_link_query(line: str, session_id: str) -> bool:
    """Whether a Loki log line is a query of a session page's Logs link
    (``grafana_links.session_events_query``) and not one of the reconciler's. Loki logs
    the LogQL in logfmt, so its quotes come escaped: backslashes and spaces are ignored."""
    loose = re.sub(r"[\\\s]", "", line)
    matchers = (
        f"{SERVICE_NAME_LABEL}={quote(SERVICE_NAME)}",
        f"{HOST_LABEL}={quote(DEFAULT_HOST)}",
        f"{SESSION_ID_KEY}={quote(session_id)}",
    )
    return all(matcher in loose for matcher in matchers) and not (
        DECISION_EVENT in line or REJECT in line
    )


def test_the_database_holds_no_canary(
    compose: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path, replay: Replay
) -> None:
    copied: dict[str, bytes] = {}
    for name in DATABASE_FILES:
        target = tmp_path / name
        result = compose("cp", f"{APP}:{DATABASE_DIR}/{name}", str(target), check=False)
        if result.returncode == 0 and target.exists():
            copied[name] = target.read_bytes()
    assert "ccobs.db" in copied, "the database could not be copied out of the app"
    stored = b"".join(copied.values())
    # The control: the replayed sessions are in there.
    for run in replay.runs:
        assert run.session_id.encode() in stored, run.scenario
    assert replay.marker.encode() not in stored
