"""Read-only repository tools exposed to selected model roles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath

from agents import Tool, function_tool

MAX_FILE_BYTES = 100_000
MAX_SEARCH_MATCHES = 100
FORBIDDEN_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class RepositoryPolicyError(ValueError):
    """Raised when a repository read would cross the trusted boundary."""


@dataclass(frozen=True, slots=True)
class RepoContext:
    """A filesystem view rooted strictly at one target directory."""

    target_root: Path

    def __post_init__(self) -> None:
        root = self.target_root.resolve()
        if not root.is_dir():
            raise RepositoryPolicyError(f"Target root does not exist: {root}")
        object.__setattr__(self, "target_root", root)

    @staticmethod
    def _validate_parts(relative_path: str) -> None:
        normalized = relative_path.replace("\\", "/")
        has_drive_prefix = len(normalized) >= 2 and normalized[1] == ":"
        if (
            not relative_path
            or Path(relative_path).is_absolute()
            or normalized.startswith("/")
            or has_drive_prefix
        ):
            raise RepositoryPolicyError("A non-empty relative path is required")
        parts = PurePath(normalized).parts
        if any(part == ".." for part in parts):
            raise RepositoryPolicyError("Path traversal is forbidden")
        for part in parts:
            folded = part.casefold()
            if folded in FORBIDDEN_PARTS or folded.startswith(".env"):
                raise RepositoryPolicyError(f"Forbidden path component: {part}")

    def resolve_file(self, relative_path: str) -> Path:
        self._validate_parts(relative_path)
        candidate = (self.target_root / relative_path).resolve()
        try:
            candidate.relative_to(self.target_root)
        except ValueError as exc:
            raise RepositoryPolicyError("Path escapes the target root") from exc
        if not candidate.is_file():
            raise RepositoryPolicyError(f"Target file does not exist: {relative_path}")
        return candidate

    def list_files(self) -> list[str]:
        files: list[str] = []
        for candidate in sorted(self.target_root.rglob("*")):
            if not candidate.is_file():
                continue
            try:
                candidate.resolve().relative_to(self.target_root)
            except ValueError:
                continue
            relative = candidate.relative_to(self.target_root)
            try:
                self._validate_parts(relative.as_posix())
            except RepositoryPolicyError:
                continue
            files.append(relative.as_posix())
        return files

    def read_file(self, relative_path: str) -> str:
        candidate = self.resolve_file(relative_path)
        if candidate.stat().st_size > MAX_FILE_BYTES:
            raise RepositoryPolicyError(f"File exceeds {MAX_FILE_BYTES} bytes")
        return candidate.read_text(encoding="utf-8")

    def search_code(self, query: str) -> list[str]:
        query = query.strip()
        if not query or len(query) > 200:
            raise RepositoryPolicyError("Search query must contain 1 to 200 characters")
        matches: list[str] = []
        for relative_path in self.list_files():
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
            candidate = self.resolve_file(relative_path)
            if candidate.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query.casefold() in line.casefold():
                    matches.append(f"{relative_path}:{line_number}: {line.strip()}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
        return matches


def build_read_tools(context: RepoContext) -> list[Tool]:
    """Create SDK function tools bound to a single target root."""

    @function_tool
    def list_target_files() -> list[str]:
        """List readable files relative to the target repository root."""

        return context.list_files()

    @function_tool
    def read_target_file(relative_path: str) -> str:
        """Read one UTF-8 file using a path relative to the target root."""

        return context.read_file(relative_path)

    @function_tool
    def search_target_code(query: str) -> list[str]:
        """Search readable target files and return path, line, and matching text."""

        return context.search_code(query)

    return [list_target_files, read_target_file, search_target_code]
