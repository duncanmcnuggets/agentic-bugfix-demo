"""Show a real partial fix accepted by verifier v0 and rejected by verifier v1."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bugfixer.git_ops import (
    create_detached_worktree,
    ensure_clean,
    get_head_sha,
    remove_worktree,
    verify_repository,
)
from bugfixer.verifier import CommandResult, run_command

PARTIAL_FIX = """\
\"\"\"Primitive override resolution.\"\"\"

from typing import TypeVar

T = TypeVar(\"T\")


def resolve_override(override: T | None, default: T) -> T:
    \"\"\"Return an explicit override, or the default when no override is supplied.\"\"\"

    if override is False:
        return override
    return override or default
"""

WEAK_TEST = """\
\"\"\"Intentionally weak regression oracle for the controlled eval.\"\"\"

from config_service.bootstrap import build_startup_plan


def test_explicit_false_is_preserved() -> None:
    plan = build_startup_plan({\"feature_enabled\": False})
    assert plan.feature_mode == \"off\"
"""


def apply_controlled_candidate(worktree: Path) -> None:
    source = worktree / "demo_target" / "src" / "config_service" / "resolver.py"
    test = worktree / "demo_target" / "tests" / "test_bug001_regression.py"
    source.write_text(PARTIAL_FIX, encoding="utf-8")
    test.write_text(WEAK_TEST, encoding="utf-8")


def verifier_v0(worktree: Path) -> dict[str, CommandResult]:
    diff_prepare = run_command(
        [
            "git",
            "add",
            "-N",
            "--",
            "demo_target/tests/test_bug001_regression.py",
            "demo_target/src/config_service/resolver.py",
        ],
        worktree,
    )
    commands = {
        "weak-regression": [
            sys.executable,
            "-m",
            "pytest",
            "demo_target/tests/test_bug001_regression.py",
            "-q",
        ],
        "existing-public-tests": [
            sys.executable,
            "-m",
            "pytest",
            "demo_target/tests/test_config_service.py",
            "-q",
        ],
        "ruff": [sys.executable, "-m", "ruff", "check", "demo_target"],
        "mypy": [sys.executable, "-m", "mypy", "demo_target/src"],
        "diff-check": ["git", "diff", "--check"],
    }
    results = {name: run_command(command, worktree) for name, command in commands.items()}
    results["diff-index-prepare"] = diff_prepare
    return results


def trusted_acceptance(worktree: Path, repo_root: Path) -> CommandResult:
    return run_command(
        [
            sys.executable,
            str(repo_root / "bugfixer" / "acceptance" / "check_bug001.py"),
            "--target-src",
            str(worktree / "demo_target" / "src"),
        ],
        worktree,
    )


def _save_result(root: Path, name: str, result: CommandResult) -> None:
    (root / f"{name}.txt").write_text(
        f"COMMAND: {' '.join(result.command)}\n"
        f"EXIT CODE: {result.exit_code}\n"
        f"PASSED: {result.passed}\n\n"
        f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n",
        encoding="utf-8",
    )


def run(repo_root: Path) -> int:
    repo = verify_repository(repo_root)
    ensure_clean(repo)
    base_sha = get_head_sha(repo)
    eval_id = f"eval-missed-case-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    artifact_root = repo / "artifacts" / eval_id
    artifact_root.mkdir(parents=True, exist_ok=False)
    worktree = create_detached_worktree(repo, base_sha)
    try:
        apply_controlled_candidate(worktree)
        v0 = verifier_v0(worktree)
        v0_accepted = all(result.passed for result in v0.values())
        for name, result in v0.items():
            _save_result(artifact_root, f"v0-{name}", result)

        acceptance = trusted_acceptance(worktree, repo)
        _save_result(artifact_root, "v1-trusted-acceptance", acceptance)
        v1_rejected = v0_accepted and not acceptance.passed and "explicit zero" in acceptance.stdout

        report = {
            "kind": "CONTROLLED ADVERSARIAL REGRESSION CASE",
            "base_sha": base_sha,
            "candidate": "preserves False but still loses 0",
            "verifier_v0_accepted": v0_accepted,
            "verifier_v1_rejected": v1_rejected,
            "v0": {name: result.to_dict() for name, result in v0.items()},
            "v1_trusted_acceptance": acceptance.to_dict(),
        }
        (artifact_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("CONTROLLED ADVERSARIAL REGRESSION CASE")
        print("Candidate: preserves False; still loses 0")
        print(f"Verifier v0: {'ACCEPTED' if v0_accepted else 'UNEXPECTED REJECTION'}")
        print(f"Verifier v1: {'REJECTED' if v1_rejected else 'UNEXPECTED ACCEPTANCE'}")
        print(f"Artifacts: {artifact_root}")
        return 0 if v0_accepted and v1_rejected else 1
    finally:
        remove_worktree(repo, worktree)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv).repo)


if __name__ == "__main__":
    raise SystemExit(main())
