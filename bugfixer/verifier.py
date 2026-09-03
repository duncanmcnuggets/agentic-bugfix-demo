"""Deterministic grounding, policy, and subprocess verification gates."""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bugfixer.git_ops import get_changed_files, status_entries
from bugfixer.schemas import ExplorerOutput

TEST_FILE = "demo_target/tests/test_bug001_regression.py"
SOURCE_FILE = "demo_target/src/config_service/resolver.py"
ALLOWED_FINAL_FILES = {TEST_FILE, SOURCE_FILE}
STRIPPED_ENV_VARS = {
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "DATABASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}
SECRET_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}


class VerificationError(RuntimeError):
    """Raised when untrusted output violates a deterministic policy."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: list[str]
    cwd: str
    started_at: str
    finished_at: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


def sanitized_subprocess_env() -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        upper_name = name.upper()
        if name in STRIPPED_ENV_VARS or any(marker in upper_name for marker in SECRET_ENV_MARKERS):
            environment.pop(name, None)
    return environment


def run_command(args: Sequence[str], cwd: Path, timeout_seconds: float = 120.0) -> CommandResult:
    started_at = _now()
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=sanitized_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        return CommandResult(
            command=list(args),
            cwd=str(cwd),
            started_at=started_at,
            finished_at=_now(),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            passed=completed.returncode == 0,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(
            command=list(args),
            cwd=str(cwd),
            started_at=started_at,
            finished_at=_now(),
            exit_code=-1,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            passed=False,
        )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_candidate_python(
    content: str,
    *,
    allowed_import_roots: set[str],
    required_symbol: str,
) -> None:
    """Reject obviously unsafe or malformed model-generated Python before execution.

    This is a narrow demo policy, not a replacement for an OS sandbox.
    """

    if "```" in content:
        raise VerificationError("Candidate Python must not contain Markdown fences")
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        raise VerificationError(
            f"Candidate Python has invalid syntax at line {exc.lineno}"
        ) from exc

    symbols = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    if required_symbol not in symbols:
        raise VerificationError(f"Candidate Python is missing required symbol: {required_symbol}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
            forbidden = roots - allowed_import_roots
            if forbidden:
                raise VerificationError(f"Candidate imports forbidden modules: {sorted(forbidden)}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", maxsplit=1)[0]
            if node.level or root not in allowed_import_roots:
                raise VerificationError(f"Candidate imports forbidden module: {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                raise VerificationError(f"Candidate uses forbidden call: {node.func.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise VerificationError("Candidate uses forbidden dunder attribute access")


def _resolve_target_file(target_root: Path, relative_path: str) -> Path:
    normalized = relative_path.replace("\\", "/")
    has_drive_prefix = len(normalized) >= 2 and normalized[1] == ":"
    if (
        Path(relative_path).is_absolute()
        or normalized.startswith("/")
        or has_drive_prefix
        or ".." in Path(normalized).parts
    ):
        raise VerificationError(f"Invalid evidence path: {relative_path}")
    candidate = (target_root.resolve() / relative_path).resolve()
    try:
        candidate.relative_to(target_root.resolve())
    except ValueError as exc:
        raise VerificationError(f"Evidence path escapes target root: {relative_path}") from exc
    if not candidate.is_file():
        raise VerificationError(f"Evidence file does not exist: {relative_path}")
    return candidate


def _python_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise VerificationError(f"Cannot parse evidence file: {path.name}") from exc
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def validate_explorer_grounding(target_root: Path, output: ExplorerOutput) -> list[str]:
    validated: list[str] = []
    for item in output.evidence:
        path = _resolve_target_file(target_root, item.file)
        if path.suffix != ".py":
            raise VerificationError(f"Evidence is not Python source: {item.file}")
        if item.symbol not in _python_symbols(path):
            raise VerificationError(
                f"Evidence symbol does not exist: {item.file}:{item.symbol}"
            )
        validated.append(f"{item.file}:{item.symbol}")
    if not validated:
        raise VerificationError("Explorer returned no grounded evidence")
    return validated


def enforce_changed_file_policy(
    worktree: Path, allowed: set[str], *, require_exact: bool = False
) -> list[str]:
    entries = status_entries(worktree)
    changed = {path for _, path in entries}
    if any("D" in status for status, _ in entries):
        raise VerificationError("File deletions are forbidden")
    unexpected = changed - allowed
    if unexpected:
        raise VerificationError(f"Unexpected changed files: {sorted(unexpected)}")
    if require_exact and changed != allowed:
        raise VerificationError(
            f"Changed files must be exactly {sorted(allowed)}, found {sorted(changed)}"
        )
    return sorted(changed)


def run_red_gate(worktree: Path) -> CommandResult:
    result = run_command(
        [sys.executable, "-m", "pytest", "demo_target/tests/test_bug001_regression.py", "-q"],
        worktree,
    )
    output = result.combined_output.casefold()
    forbidden = ("syntaxerror", "importerror", "collection error", "error collecting")
    passed = (
        result.exit_code == 1
        and "failed" in output
        and ("assert" in output or "assertionerror" in output)
        and not any(marker in output for marker in forbidden)
    )
    return replace(result, passed=passed)


def run_final_verifier(worktree: Path, trusted_repo_root: Path) -> dict[str, CommandResult]:
    acceptance_script = trusted_repo_root / "bugfixer" / "acceptance" / "check_bug001.py"
    diff_prepare = run_command(
        ["git", "add", "-N", "--", TEST_FILE, SOURCE_FILE], worktree, 60.0
    )
    checks: list[tuple[str, list[str], float]] = [
        (
            "targeted-after",
            [
                sys.executable,
                "-m",
                "pytest",
                "demo_target/tests/test_bug001_regression.py",
                "-q",
            ],
            120.0,
        ),
        ("full-suite", [sys.executable, "-m", "pytest", "demo_target/tests", "-q"], 120.0),
        ("ruff", [sys.executable, "-m", "ruff", "check", "demo_target"], 120.0),
        ("mypy", [sys.executable, "-m", "mypy", "demo_target/src"], 120.0),
        ("diff-check", ["git", "diff", "--check"], 60.0),
        (
            "trusted-acceptance",
            [
                sys.executable,
                str(acceptance_script),
                "--target-src",
                str(worktree / "demo_target" / "src"),
            ],
            120.0,
        ),
    ]
    results = {
        name: run_command(command, worktree, timeout)
        for name, command, timeout in checks
    }
    results["diff-index-prepare"] = diff_prepare
    return results


def all_checks_pass(results: dict[str, CommandResult]) -> bool:
    return bool(results) and all(result.passed for result in results.values())


def verifier_report(results: dict[str, CommandResult]) -> str:
    lines: list[str] = []
    for name, result in results.items():
        lines.append(f"{name}: {'PASS' if result.passed else 'FAIL'} (exit={result.exit_code})")
        if not result.passed:
            lines.append(result.combined_output[-4_000:])
    return "\n".join(lines)


def current_changed_files(worktree: Path) -> list[str]:
    return get_changed_files(worktree)
