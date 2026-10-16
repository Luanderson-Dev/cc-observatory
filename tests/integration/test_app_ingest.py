"""The app's hook ingestion in the isolated ``ccobs-it`` compose project.

Needs Docker: ``uv run pytest -m integration --no-cov``. Every hook fixture is POSTed twice to
the app's published port, each time with a fresh session ID (``tools/stack/replay.py``),
back to back after an untimed warm-up, each answered with an empty 204 (ADR 0027). The
state the API then serves for each session must be the one the reducer gives the same
lines in process (ADR 0024):
status, agents, and blocks with their close reasons. The hook route must answer fast
from the client's side too, the state must survive a restart of the app, and the
database on the volume must run in WAL mode (ADR 0005).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pytest
from app_support import (
    APP,
    SessionShape,
    detail,
    events,
    expected_shape,
    get,
    get_json,
    items,
    mapping,
    percentile,
    poll,
    samples,
    shapes,
)

from ccobs.domain.app_metrics import DATABASE_WAL
from ccobs.domain.telemetry import DEFAULT_HOST
from tools.stack.replay import (
    PreparedHooks,
    load_scenario,
    prepare_hooks,
    scenario_names,
    send_hooks,
)

pytestmark = pytest.mark.integration

# Seconds for the writer to store what the route queued (it batches every 50 ms), and
# for the app to answer again after a restart (uvicorn stops at once on SIGTERM; the
# migrations and the writer's load take well under a second).
STORED_DEADLINE: Final = 15.0
RESTART_DEADLINE: Final = 60.0
# The hook route's target (CLAUDE.md), measured here from the client: connecting through
# Docker's port publishing included.
P95_LIMIT_S: Final = 0.050
# Sent first and not timed, so that the first connections and the app's first requests
# do not count; then every fixture is timed this many times.
WARM_UP_SCENARIO: Final = "headless-edit-commit-tasks"
TIMED_ROUNDS: Final = 2
# One case written out by hand, from tests/test_domain_sessions.py's replay of it: the
# approved dialog closes by its PostToolUse, the one denied with Esc is superseded by
# PreCompact when no telemetry closes it, and the session ends.
COMPACT_SCENARIO: Final = "interactive-permission-compact"
COMPACT_STATUS: Final = "ended"
COMPACT_BLOCKS: Final = [("permission", "tool_completed"), ("permission", "superseded")]
DATABASE: Final = "/var/lib/ccobs/ccobs.db"
JOURNAL_MODE: Final = (
    "import sqlite3; "
    f"print(sqlite3.connect('file:{DATABASE}?mode=ro', uri=True)"
    ".execute('PRAGMA journal_mode').fetchone()[0])"
)


@dataclass(frozen=True)
class Replay:
    """The hook scenarios sent, what the reducer expects of each, and the time each POST
    took, in seconds."""

    runs: tuple[PreparedHooks, ...]
    expected: dict[str, SessionShape]
    durations: tuple[float, ...]


@pytest.fixture(scope="module")
def replay(app_url: str) -> Replay:
    """A warm-up scenario, not timed, then every hook fixture twice, each run with a fresh
    session ID: about 260 timed POSTs."""
    names = scenario_names(with_hooks=True)
    assert names, "no fixture scenario has hooks"
    warm_up = prepare_hooks(load_scenario(WARM_UP_SCENARIO))
    send_hooks(app_url, warm_up.lines)
    timed = tuple(prepare_hooks(load_scenario(name)) for _ in range(TIMED_ROUNDS) for name in names)
    durations: list[float] = []
    for run in timed:
        durations += send_hooks(app_url, run.lines)
    runs = (warm_up, *timed)
    expected = {run.session_id: expected_shape(run.lines) for run in runs}
    return Replay(runs, expected, tuple(durations))


@pytest.fixture(scope="module")
def stored(app_url: str, replay: Replay) -> dict[str, SessionShape | None]:
    """Each replayed session's shape in the API, once every one matches or the deadline
    has passed (the tests then report the difference)."""
    return poll(
        lambda: shapes(app_url, replay.expected),
        lambda found: found == replay.expected,
        STORED_DEADLINE,
    )


def test_the_app_is_healthy(service_health: Callable[[str], str], app_url: str) -> None:
    assert service_health(APP) == "healthy"
    assert get_json(app_url, "/healthz") == {"status": "ok"}


def test_every_hook_fixture_gives_the_reducers_state(
    app_url: str, replay: Replay, stored: dict[str, SessionShape | None]
) -> None:
    for run in replay.runs:
        assert stored[run.session_id] == replay.expected[run.session_id], run.scenario
        # Every line was stored as one hook event, none dropped as invalid.
        assert len(events(app_url, run.session_id)) == len(run.lines), run.scenario
    # The fixtures cover what matters: blocks closed in several ways and subagents.
    reasons = {block[6] for shape in replay.expected.values() for block in shape.blocks}
    assert {"tool_completed", "superseded"} <= reasons, reasons
    assert any(shape.agents for shape in replay.expected.values())


def test_the_permission_compact_fixture_ends_as_written_out(
    app_url: str, replay: Replay, stored: dict[str, SessionShape | None]
) -> None:
    compact = [run for run in replay.runs if run.scenario == COMPACT_SCENARIO]
    assert len(compact) == TIMED_ROUNDS
    for run in compact:
        assert stored[run.session_id] is not None
        found = detail(app_url, run.session_id)
        assert found is not None
        assert found["status"] == COMPACT_STATUS
        blocks = [mapping(block, "block") for block in items(found["blocks"], "blocks")]
        assert [(block["kind"], block["close_reason"]) for block in blocks] == COMPACT_BLOCKS
        assert {block["close_source"] for block in blocks} == {"hook"}


def test_the_hook_route_answers_within_50_ms_at_the_95th_percentile(replay: Replay) -> None:
    p95 = percentile(replay.durations, 0.95)
    print(
        f"POST /hooks over {len(replay.durations)} requests: p50 "
        f"{percentile(replay.durations, 0.5) * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms, "
        f"max {max(replay.durations) * 1000:.1f} ms"
    )
    assert p95 < P95_LIMIT_S, f"p95 {p95 * 1000:.1f} ms"


def test_the_database_on_the_volume_runs_in_wal_mode(
    compose: Callable[..., subprocess.CompletedProcess[str]], app_metrics_url: str
) -> None:
    result = compose("exec", "-T", APP, "python", "-c", JOURNAL_MODE)
    assert result.stdout.strip() == "wal"
    exposition = get(app_metrics_url, "/metrics").decode()
    found = samples(exposition)
    assert found[(DATABASE_WAL.name, frozenset({("host", DEFAULT_HOST)}))] == 1


# Last in this module: it restarts the app.


def test_the_state_survives_a_restart_of_the_app(
    compose: Callable[..., subprocess.CompletedProcess[str]],
    app_url: str,
    replay: Replay,
    stored: dict[str, SessionShape | None],
) -> None:
    assert stored == replay.expected
    before = {session_id: detail(app_url, session_id) for session_id in replay.expected}

    compose("restart", APP)

    healthy = poll(lambda: _healthz(app_url), lambda ok: ok, RESTART_DEADLINE)
    assert healthy, "the app did not answer after its restart"
    after = {session_id: detail(app_url, session_id) for session_id in replay.expected}
    assert after == before
    assert shapes(app_url, replay.expected) == replay.expected


def _healthz(app_url: str) -> bool:
    try:
        return get_json(app_url, "/healthz") == {"status": "ok"}
    except (OSError, AssertionError, ValueError):
        return False
