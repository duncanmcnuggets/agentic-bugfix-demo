from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bugfixer.git_ops import (
    GitOperationError,
    create_branch_and_commit,
    create_detached_worktree,
    ensure_clean,
    get_changed_files,
    get_head_sha,
    remove_worktree,
    verify_repository,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def test_repository_and_clean_checks(repository: Path) -> None:
    assert verify_repository(repository) == repository.resolve()
    ensure_clean(repository)
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(GitOperationError, match="clean"):
        ensure_clean(repository)


def test_worktree_branch_and_commit(repository: Path) -> None:
    base_sha = get_head_sha(repository)
    worktree = create_detached_worktree(repository, base_sha)
    try:
        (worktree / "tracked.txt").write_text("fixed\n", encoding="utf-8")
        assert get_changed_files(worktree) == ["tracked.txt"]
        commit_sha = create_branch_and_commit(
            worktree,
            "agent/BUG-001-test",
            ["tracked.txt"],
            "fix: test",
            user_name="Test User",
            user_email="test@example.com",
        )
        assert commit_sha != base_sha
        assert _git(worktree, "branch", "--show-current") == "agent/BUG-001-test"
    finally:
        remove_worktree(repository, worktree)

