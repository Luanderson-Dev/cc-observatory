"""Tests for the capture workspace checks, on directories under tmp_path only."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.capture import workspace as workspace_module
from tools.capture.workspace import (
    SYNTHETIC_INSTRUCTIONS,
    main,
    prepare_workspace,
    root_problems,
    workspace_problems,
)


@pytest.fixture(autouse=True)
def stay_under_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Look for memory files and repositories only up to tmp_path, never in the real home
    (which has a .claude/ directory) or a repository around the temp directory."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH; the workspace checks run git rev-parse")
    if any(part.casefold() == "scratchpad" for part in tmp_path.parts):
        pytest.skip(
            "pytest's basetemp is inside a Claude Code scratchpad, which the workspace"
            " checks refuse by design; run with --basetemp outside it"
        )

    def ancestors(path: Path) -> list[Path]:
        return [
            parent for parent in path.parents if parent == tmp_path or tmp_path in parent.parents
        ]

    monkeypatch.setattr(workspace_module, "ancestors", ancestors)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def fake_repository(directory: Path) -> None:
    """The smallest .git that git recognizes, without running git init."""
    (directory / ".git" / "objects").mkdir(parents=True)
    (directory / ".git" / "refs").mkdir()
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def new_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ccobs-capture" / "run-1" / "headless-x"
    workspace.mkdir(parents=True)
    return workspace


def test_the_synthetic_instructions_are_one_neutral_english_line() -> None:
    assert SYNTHETIC_INSTRUCTIONS == "Synthetic project instructions for the capture workspace.\n"


def test_prepare_writes_the_synthetic_instructions_in_a_clean_workspace(tmp_path: Path) -> None:
    workspace = new_workspace(tmp_path)

    assert prepare_workspace(workspace) == []
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == SYNTHETIC_INSTRUCTIONS
    assert prepare_workspace(workspace) == []
    assert sorted(path.name for path in workspace.iterdir()) == ["CLAUDE.md"]


@pytest.mark.parametrize("entry", ["CLAUDE.md", "CLAUDE.local.md", ".claude"])
@pytest.mark.parametrize("level", ["ccobs-capture", "ccobs-capture/run-1"])
def test_memory_files_above_the_workspace_are_refused(
    tmp_path: Path, entry: str, level: str
) -> None:
    workspace = new_workspace(tmp_path)
    found = tmp_path / level / entry
    if entry == ".claude":
        found.mkdir()
    else:
        found.write_text("# personal instructions\n", encoding="utf-8")

    problems = prepare_workspace(workspace)

    assert len(problems) == 1
    assert str(found) in problems[0]
    assert not (workspace / "CLAUDE.md").exists()


@pytest.mark.parametrize("entry", ["CLAUDE.local.md", ".claude"])
def test_local_memory_or_project_settings_in_the_workspace_are_refused(
    tmp_path: Path, entry: str
) -> None:
    workspace = new_workspace(tmp_path)
    if entry == ".claude":
        (workspace / entry).mkdir()
    else:
        (workspace / entry).write_text("x\n", encoding="utf-8")

    problems = workspace_problems(workspace)

    assert [entry in problem for problem in problems] == [True]


def test_a_workspace_claude_md_other_than_the_synthetic_one_is_refused(tmp_path: Path) -> None:
    workspace = new_workspace(tmp_path)
    (workspace / "CLAUDE.md").write_text("# my notes\n", encoding="utf-8")

    problems = prepare_workspace(workspace)

    assert [("CLAUDE.md" in problem) for problem in problems] == [True]
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == "# my notes\n"


def test_a_workspace_inside_a_claude_code_scratchpad_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "claude" / "C--Projects-repo" / "abc" / "scratchpad" / "ws"
    workspace.mkdir(parents=True)

    problems = workspace_problems(workspace)

    assert [("scratchpad" in problem) for problem in problems] == [True]


def test_a_git_repository_around_the_workspace_is_refused(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    workspace = parent / "ws"
    workspace.mkdir(parents=True)
    fake_repository(parent)

    problems = workspace_problems(workspace)

    assert [("git repository" in problem) for problem in problems] == [True]


def test_a_git_repository_of_the_workspace_itself_is_fine(tmp_path: Path) -> None:
    workspace = new_workspace(tmp_path)
    fake_repository(workspace)

    assert workspace_problems(workspace) == []


def test_without_git_the_repository_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = new_workspace(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    problems = workspace_problems(workspace)

    assert [("git" in problem) for problem in problems] == [True]


def test_a_clean_root_passes(tmp_path: Path) -> None:
    root = tmp_path / "ccobs-capture" / "run-1"
    root.mkdir(parents=True)

    assert root_problems(root) == []


@pytest.mark.parametrize("where", ["root", "parent"])
def test_a_root_with_memory_files_in_it_or_above_it_is_refused(tmp_path: Path, where: str) -> None:
    root = tmp_path / "ccobs-capture" / "run-1"
    root.mkdir(parents=True)
    found = (root if where == "root" else root.parent) / "CLAUDE.md"
    found.write_text("x\n", encoding="utf-8")

    problems = root_problems(root)

    assert len(problems) == 1
    assert str(found) in problems[0]


@pytest.mark.parametrize("repository", ["root", "parent"])
def test_a_root_in_a_git_repository_is_refused(tmp_path: Path, repository: str) -> None:
    root = tmp_path / "parent" / "run-1"
    root.mkdir(parents=True)
    fake_repository(root if repository == "root" else root.parent)

    problems = root_problems(root)

    assert [("git repository" in problem) for problem in problems] == [True]


def test_cli_prepare_writes_the_instructions_or_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)

    assert main(["prepare", str(workspace)]) == 0
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == SYNTHETIC_INSTRUCTIONS

    (tmp_path / "ccobs-capture" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    other = tmp_path / "ccobs-capture" / "run-1" / "other"
    other.mkdir()
    assert main(["prepare", str(other)]) == 1
    assert "refusing" in capsys.readouterr().err
    assert not (other / "CLAUDE.md").exists()


def test_cli_exploratory_prepare_only_refuses_a_scratchpad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    (tmp_path / "ccobs-capture" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    scratchpad = tmp_path / "scratchpad" / "ws"
    scratchpad.mkdir(parents=True)

    assert main(["prepare", "--exploratory", str(workspace)]) == 0
    assert not (workspace / "CLAUDE.md").exists()
    assert main(["prepare", "--exploratory", str(scratchpad)]) == 1
    assert "scratchpad" in capsys.readouterr().err


def test_cli_check_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "ccobs-capture" / "run-1"
    root.mkdir(parents=True)

    assert main(["check-root", str(root)]) == 0
    fake_repository(tmp_path / "ccobs-capture")
    assert main(["check-root", str(root)]) == 1
    assert "git repository" in capsys.readouterr().err
