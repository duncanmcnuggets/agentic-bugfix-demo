"""Narrow Git operations used by the trusted controller."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class GitOperationError(RuntimeError):
    """Raised when a required Git invariant or command fails."""


@dataclass(frozen=True, slots=True)
class GitCommand:
    args: list[str]
    stdout: str
    stderr: str


def _git(repo: Path, *args: str, check: bool = True) -> GitCommand:
    command = ["git", "-C", str(repo), *args]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if check and completed.returncode != 0:
        raise GitOperationError(
            f"Git command failed ({' '.join(args)}): {completed.stderr.strip()}"
        )
    return GitCommand(command, completed.stdout, completed.stderr)


def verify_repository(repo: Path) -> Path:
    root = repo.resolve()
    result = _git(root, "rev-parse", "--show-toplevel")
    discovered = Path(result.stdout.strip()).resolve()
    if discovered != root:
        raise GitOperationError(f"Expected repository root {root}, found {discovered}")
    return root


def ensure_clean(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
    if status:
        raise GitOperationError("Main checkout must be clean before a run")


def get_head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def create_detached_worktree(repo: Path, base_sha: str) -> Path:
    temporary_root = Path(tempfile.mkdtemp(prefix="agentic-bugfix-"))
    worktree = temporary_root / "checkout"
    try:
        _git(repo, "worktree", "add", "--detach", str(worktree), base_sha)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    return worktree.resolve()


def get_diff(worktree: Path, base_sha: str) -> str:
    _git(worktree, "add", "-N", "--", ".")
    return _git(worktree, "diff", "--binary", base_sha, "--").stdout


def status_entries(worktree: Path) -> list[tuple[str, str]]:
    output = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:].replace("\\", "/")
        entries.append((status, path))
    return entries


def get_changed_files(worktree: Path) -> list[str]:
    return sorted(path for _, path in status_entries(worktree))


def create_branch_and_commit(
    worktree: Path,
    branch_name: str,
    paths: list[str],
    message: str,
    *,
    user_name: str,
    user_email: str,
) -> str:
    if _git(worktree, "branch", "--list", branch_name).stdout.strip():
        raise GitOperationError(f"Branch already exists: {branch_name}")
    _git(worktree, "switch", "-c", branch_name)
    _git(worktree, "add", "--", *paths)
    _git(
        worktree,
        "-c",
        f"user.name={user_name}",
        "-c",
        f"user.email={user_email}",
        "commit",
        "-m",
        message,
    )
    return get_head_sha(worktree)


def remove_worktree(repo: Path, worktree: Path) -> None:
    resolved = worktree.resolve()
    temporary_root = resolved.parent
    temp_directory = Path(tempfile.gettempdir()).resolve()
    try:
        temporary_root.relative_to(temp_directory)
    except ValueError as exc:
        raise GitOperationError("Refusing to remove a worktree outside the temp directory") from exc
    if not temporary_root.name.startswith("agentic-bugfix-") or resolved.name != "checkout":
        raise GitOperationError("Refusing to remove an unrecognized worktree path")
    _git(repo, "worktree", "remove", "--force", str(resolved))
    shutil.rmtree(temporary_root, ignore_errors=True)
