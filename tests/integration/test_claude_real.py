"""Real Claude Code sessions: opt-in, never in CI (marker ``claude``).

Each test runs ``claude -p`` against the Anthropic API with your own login, so it costs
a little money (cents with Haiku). They are deselected by default (``addopts``) and
skip unless ``CCOBS_IT_CLAUDE=1`` is set and ``claude`` is on PATH:

- ``CCOBS_IT_CLAUDE=1 uv run pytest -m claude --no-cov`` runs both; the one marked
  ``integration`` too starts the ``ccobs-it`` project, so Docker must be running;
- ``CCOBS_IT_CLAUDE=1 uv run pytest -m "claude and not integration" --no-cov`` runs the
  stack-down check alone, without Docker;
- ``CCOBS_IT_CLAUDE=1 uv run pytest -m "claude and integration" --no-cov`` runs the
  cumulative re-export check alone.

Sessions are isolated like ``tools/capture/run_claude.sh`` runs them:
``--setting-sources project,local`` (no user settings, hooks or plugins),
``--strict-mcp-config``, ``--no-chrome``, no auto memory or claude.ai connectors, the
variables a parent Claude Code session exports removed, and an empty workspace that
``tools.capture.workspace`` accepts (no ``CLAUDE.md``, ``CLAUDE.local.md`` or
``.claude/`` in it or above it, no git repository around it). On Windows the temporary
directory is under your home, which has ``.claude/``: point ``CCOBS_IT_CLAUDE_ROOT`` at a
directory outside it, such as ``C:/ccobs-capture``. Your login (``~/.claude``) is read
as it is; ``--no-session-persistence`` keeps the session transcripts out of
``~/.claude/projects``, but Claude Code may still update its own ``~/.claude.json``.
Each run is capped with ``--max-turns`` and ``--max-budget-usd``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import pytest
from app_support import get_json, items, mapping, poll

from ccobs.domain.hook_config import HOOK_PATH, hook_settings
from ccobs.domain.telemetry import TOKEN_USAGE
from ccobs.domain.telemetry_settings import telemetry_env
from tools.capture.workspace import workspace_problems

ENABLE_VARIABLE: Final = "CCOBS_IT_CLAUDE"
ROOT_VARIABLE: Final = "CCOBS_IT_CLAUDE_ROOT"
CLAUDE: Final = shutil.which("claude")

pytestmark = [
    pytest.mark.claude,
    pytest.mark.skipif(
        os.environ.get(ENABLE_VARIABLE) != "1",
        reason=f"real Claude Code sessions cost money; opt in with {ENABLE_VARIABLE}=1",
    ),
    pytest.mark.skipif(CLAUDE is None, reason="claude is not on PATH"),
]

LOOPBACK: Final = "127.0.0.1"
MODEL: Final = "haiku"
# Seconds a whole `claude -p` run may take.
RUN_TIMEOUT: Final = 180
# Variables a parent Claude Code session exports (tools/capture/run_claude.sh): a run
# started from inside Claude Code must not be treated as its child. Every OTEL_*
# variable goes too: the settings' env block sets the ones a test needs.
PARENT_VARIABLES: Final = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
)
ISOLATION_ENV: Final = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
ISOLATION_ARGS: Final = (
    "--setting-sources",
    "project,local",
    "--strict-mcp-config",
    "--no-chrome",
    # No transcript under ~/.claude/projects (print mode only).
    "--no-session-persistence",
)
# Dollars a run may spend (Claude Code's own estimate; it can pass it a little).
MAX_BUDGET_USD: Final = "0.25"

# The stack-down run (ADR 0011).
OK_PROMPT: Final = "Reply with the single word OK."
OK_MAX_TURNS: Final = 2

# The cumulative re-export run (ADR 0013): metrics every 5 s, and one shell command that
# keeps the process alive for several intervals after its token usage stops changing.
EXPORT_INTERVAL_MS: Final = 5000
SLEEP_S: Final = 25
SLEEP_PROMPT: Final = (
    f"Use the Bash tool to run exactly this command, with nothing else: sleep {SLEEP_S}. "
    "When it finishes, reply with the single word DONE."
)
SLEEP_ALLOWED: Final = "Bash(sleep *)"
SLEEP_MAX_TURNS: Final = 4
SLEEP_RUN_TIMEOUT: Final = 300
# Samples in a row with the same value, inside the run, that show an unchanged series
# was re-exported: a 25 s sleep at a 5 s interval spans at least four exports.
MIN_UNCHANGED_SAMPLES: Final = 4
# Seconds for the last exports to reach Prometheus after the run.
INGEST_DEADLINE: Final = 60.0
# Seconds of margin before the run's start in the range selector.
WINDOW_MARGIN_S: Final = 30


def _unused_port() -> int:
    """A loopback port nothing listens on (the OS picks it; it is closed again)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """An empty workspace that loads none of your own Claude Code setup."""
    root = Path(os.environ.get(ROOT_VARIABLE) or tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / f"ccobs-it-claude-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    try:
        problems = workspace_problems(directory)
        if problems:
            pytest.fail(
                "the workspace would load your own Claude Code setup:\n"
                + "\n".join(f"  {problem}" for problem in problems)
                + f"\nSet {ROOT_VARIABLE} to a directory outside it (C:/ccobs-capture on "
                "Windows, /tmp/ccobs-capture elsewhere).",
                pytrace=False,
            )
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _environment() -> dict[str, str]:
    kept = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in PARENT_VARIABLES and not key.upper().startswith("OTEL_")
    }
    return {**kept, **ISOLATION_ENV}


@dataclass(frozen=True)
class Run:
    code: int
    stdout: str
    stderr: str
    started: float
    ended: float

    def result(self) -> dict[str, object]:
        """The ``--output-format json`` result object."""
        return mapping(json.loads(self.stdout), "result")

    def describe(self) -> str:
        return f"exit {self.code}\nstdout: {self.stdout[-2000:]}\nstderr: {self.stderr[-2000:]}"


def _run_claude(
    settings: dict[str, object],
    workspace: Path,
    settings_dir: Path,
    prompt: str,
    *extra: str,
    timeout: float = RUN_TIMEOUT,
) -> Run:
    assert CLAUDE is not None
    # A timeout kills claude itself; through the npm claude.cmd shim on Windows that is
    # cmd.exe, so the node process may stay behind (the native claude.exe does not).
    settings_file = settings_dir / "claude-settings.json"
    settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")
    command: Sequence[str] = [
        CLAUDE,
        "-p",
        prompt,
        "--settings",
        str(settings_file),
        *ISOLATION_ARGS,
        "--output-format",
        "json",
        "--model",
        MODEL,
        "--max-budget-usd",
        MAX_BUDGET_USD,
        *extra,
    ]
    started = time.time()
    # S603 cannot see that this is safe: claude comes from shutil.which and every
    # argument is fixed by this module.
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=workspace,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return Run(completed.returncode, completed.stdout, completed.stderr, started, time.time())


def test_claude_completes_with_the_stack_down(workspace: Path, tmp_path: Path) -> None:
    """ADR 0011: with nothing listening on the hook URL or the OTLP endpoint, a session
    still completes; the hook errors only show in the transcript."""
    app_port, otlp_port = _unused_port(), _unused_port()
    settings: dict[str, object] = {
        **hook_settings(
            f"http://{LOOPBACK}:{app_port}{HOOK_PATH}", windows=sys.platform == "win32"
        ),
        "env": telemetry_env(f"http://{LOOPBACK}:{otlp_port}"),
    }

    run = _run_claude(settings, workspace, tmp_path, OK_PROMPT, "--max-turns", str(OK_MAX_TURNS))

    assert run.code == 0, run.describe()
    result = run.result()
    assert result.get("is_error") is False, run.describe()
    assert "OK" in str(result.get("result", "")).upper(), run.describe()


# The cumulative re-export check (ADR 0013).


def _series(
    prometheus_url: str, selector: str, start: float, end: float
) -> list[dict[str, object]]:
    """Every raw sample of the series ``selector`` names between the fixed times
    ``start`` and ``end``: a range vector evaluated at ``end``, so Prometheus fills in no
    sample of its own and the window does not move while the test polls."""
    window_s = int(end - start) + 1
    document = mapping(
        get_json(
            prometheus_url,
            "/api/v1/query",
            {"query": f"{selector}[{window_s}s]", "time": f"{end:.3f}"},
        ),
        "query",
    )
    assert document.get("status") == "success", document
    data = mapping(document["data"], "data")
    assert data["resultType"] == "matrix", data
    return [mapping(series, "series") for series in items(data["result"], "result")]


def _samples(series: dict[str, object]) -> list[tuple[float, float]]:
    """The series' ``(timestamp, value)`` samples in time order."""
    pairs = [items(pair, "sample") for pair in items(series["values"], "values")]
    return sorted((float(str(stamp)), float(str(value))) for stamp, value in pairs)


def _values_within(series: dict[str, object], start: float, end: float) -> list[float]:
    """The series' sample values whose timestamps fall in ``[start, end]``, in order."""
    return [value for stamp, value in _samples(series) if start <= stamp <= end]


def _longest_unchanged(values: Sequence[float]) -> int:
    """The most samples in a row that hold the same value above zero. A run of zeros
    counts for nothing: it shows nothing about re-exporting a counted value."""
    longest = current = 0
    previous: float | None = None
    for value in values:
        if value <= 0:
            current, previous = 0, None
            continue
        current = current + 1 if value == previous else 1
        longest = max(longest, current)
        previous = value
    return longest


def _longest_in_run(series: dict[str, object], start: float, end: float) -> int:
    """The most samples in a row, inside ``[start, end]``, that hold the same value above
    zero."""
    return _longest_unchanged(_values_within(series, start, end))


def _describe(series: list[dict[str, object]], start: float, end: float) -> str:
    return "; ".join(
        f"{mapping(item['metric'], 'metric').get('type')}"
        f"/{mapping(item['metric'], 'metric').get('query_source')}: "
        f"{len(_samples(item))} samples, {len(_values_within(item, start, end))} in the run, "
        f"{_longest_in_run(item, start, end)} unchanged in a row above zero"
        for item in series
    )


@pytest.mark.integration
def test_cumulative_metrics_reexport_unchanged_series(
    workspace: Path, tmp_path: Path, otlp_http_url: str, prometheus_url: str
) -> None:
    """ADR 0013 expects Claude Code in cumulative mode to export every series at each
    interval, changed or not. The session sleeps through five 5 s intervals after its
    first request, so inside the run one of its token series must hold the same value
    above zero for at least four samples in a row. Only that last assertion failing is
    evidence for a new ADR on lookback_delta; every earlier one is a setup failure."""
    otlp_port = urlsplit(otlp_http_url).port
    assert otlp_port is not None
    env = {
        **telemetry_env(f"http://{LOOPBACK}:{otlp_port}"),
        "OTEL_METRIC_EXPORT_INTERVAL": str(EXPORT_INTERVAL_MS),
    }

    run = _run_claude(
        {"env": env},
        workspace,
        tmp_path,
        SLEEP_PROMPT,
        "--allowedTools",
        SLEEP_ALLOWED,
        "--max-turns",
        str(SLEEP_MAX_TURNS),
        timeout=SLEEP_RUN_TIMEOUT,
    )

    # Setup checks: a run that never slept shows nothing about ADR 0013.
    assert run.code == 0, f"setup: claude -p failed, not ADR 0013 evidence\n{run.describe()}"
    result = run.result()
    assert result.get("is_error") is False, (
        f"setup: the run ended in an error, not ADR 0013 evidence\n{run.describe()}"
    )
    assert not result.get("permission_denials"), (
        f"setup: the sleep was denied, not ADR 0013 evidence\n{run.describe()}"
    )
    duration = run.ended - run.started
    assert duration >= SLEEP_S, (
        f"setup: the run took {duration:.1f} s, under the {SLEEP_S} s sleep, so it never "
        f"slept; not ADR 0013 evidence\n{run.describe()}"
    )

    session_id = result.get("session_id")
    if isinstance(session_id, str) and session_id:
        selector = f'{TOKEN_USAGE.name}{{session_id="{session_id}"}}'
    else:
        # No session ID in the result: every series with samples in the run's window.
        # Other integration tests that replay token usage meanwhile would add theirs,
        # which is why the docstring runs this test alone (-m "claude and integration").
        selector = TOKEN_USAGE.name
    # Fixed bounds: the query covers the whole run however long the polling takes.
    query_start = run.started - WINDOW_MARGIN_S
    query_end = run.ended + INGEST_DEADLINE

    def longest(found: list[dict[str, object]]) -> int:
        return max((_longest_in_run(item, run.started, run.ended) for item in found), default=0)

    series = poll(
        lambda: _series(prometheus_url, selector, query_start, query_end),
        lambda found: longest(found) >= MIN_UNCHANGED_SAMPLES,
        INGEST_DEADLINE,
    )

    assert series, (
        f"setup: no {TOKEN_USAGE.name} series for {selector} reached Prometheus, "
        "not ADR 0013 evidence"
    )
    description = _describe(series, run.started, run.ended)
    observed = longest(series)
    assert observed >= MIN_UNCHANGED_SAMPLES, (
        f"at most {observed} samples in a row inside the run held the same value above "
        f"zero (expected at least {MIN_UNCHANGED_SAMPLES}): {description}. Only this "
        "failure is evidence for a new ADR on lookback_delta (ADR 0013)."
    )
    print(f"cumulative re-export observed: {description}")
