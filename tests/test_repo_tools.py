from __future__ import annotations

from pathlib import Path

import pytest

from bugfixer.repo_tools import RepoContext, RepositoryPolicyError


@pytest.fixture
def repo(tmp_path: Path) -> RepoContext:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text("def useful():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    return RepoContext(tmp_path)


def test_read_and_search_are_rooted(repo: RepoContext) -> None:
    assert "def useful" in repo.read_file("src/sample.py")
    assert repo.search_code("useful") == ["src/sample.py:1: def useful():"]


@pytest.mark.parametrize("path", ["../secret.txt", "C:/secret.txt", ".env", ".git/config"])
def test_forbidden_paths_are_rejected(repo: RepoContext, path: str) -> None:
    with pytest.raises(RepositoryPolicyError):
        repo.read_file(path)


def test_forbidden_files_are_not_listed(repo: RepoContext) -> None:
    assert repo.list_files() == ["src/sample.py"]

