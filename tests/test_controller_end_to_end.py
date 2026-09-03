from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import bugfixer.cli as cli
from bugfixer.api_runner import AgentCallRecord, AgentCallResult, UsageSummary
from bugfixer.cli import BugfixController
from bugfixer.config import Settings
from bugfixer.git_ops import remove_worktree
from bugfixer.schemas import (
    EvidenceItem,
    ExplorerOutput,
    FixerOutput,
    RequirementCheck,
    ReviewerOutput,
)
from bugfixer.schemas import TestWriterOutput as WriterOutput
from bugfixer.state import Phase

REGRESSION_TEST = '''\
"""Regression coverage for BUG-001 through the public API."""

from config_service.bootstrap import build_startup_plan


def test_explicit_falsy_values_are_preserved() -> None:
    plan = build_startup_plan({"max_retries": 0, "feature_enabled": False})
    assert plan.retry_budget == 0
    assert plan.feature_mode == "off"
'''

FIXED_RESOLVER = '''\
"""Primitive override resolution."""

from typing import TypeVar

T = TypeVar("T")


def resolve_override(override: T | None, default: T) -> T:
    """Return an explicit override, or the default when no override is supplied."""

    return default if override is None else override
'''


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _copy_as_clean_repository(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    shutil.copytree(
        source,
        repo,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "artifacts",
            "*.egg-info",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        ),
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test baseline")
    return repo


def _fake_output(output_type: type[BaseModel]) -> BaseModel:
    if output_type is ExplorerOutput:
        return ExplorerOutput(
            root_cause_summary="Truthiness incorrectly replaces explicit falsy values.",
            evidence=[
                EvidenceItem(
                    file="src/config_service/resolver.py",
                    symbol="resolve_override",
                    observation="The fallback uses boolean-or semantics.",
                )
            ],
            relevant_tests=["tests/test_config_service.py"],
            uncertainties=[],
        )
    if output_type is WriterOutput:
        return WriterOutput(
            file_path="tests/test_bug001_regression.py",
            content=REGRESSION_TEST,
            summary="Covers explicit zero and false through the public entry point.",
        )
    if output_type is FixerOutput:
        return FixerOutput(
            file_path="src/config_service/resolver.py",
            content=FIXED_RESOLVER,
            summary="Use the default only when the override is None.",
            risk_notes=[],
        )
    if output_type is ReviewerOutput:
        return ReviewerOutput(
            verdict="approve",
            requirement_checks=[
                RequirementCheck(
                    requirement="Explicit falsy values are preserved",
                    status="met",
                    evidence="The diff uses an explicit None check and all gates pass.",
                )
            ],
            blockers=[],
            notes=["Change is minimal."],
        )
    raise AssertionError(f"Unexpected output type: {output_type}")


def test_full_controller_path_with_mocked_api_and_real_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _copy_as_clean_repository(tmp_path)
    roles: list[str] = []
    turn_budgets: list[tuple[str, int]] = []
    reviewer_inputs: list[str] = []

    def fake_run_structured_agent(
        agent: object,
        input_text: str,
        *,
        output_type: type[BaseModel],
        run_id: str,
        role: str,
        model: str,
        max_turns: int,
    ) -> AgentCallResult[Any]:
        del agent, run_id
        roles.append(role)
        turn_budgets.append((role, max_turns))
        if role == "reviewer":
            reviewer_inputs.append(input_text)
        output = _fake_output(output_type)
        return AgentCallResult(
            output=output,
            record=AgentCallRecord(
                role=role,
                model=model,
                elapsed_seconds=0.01,
                usage=UsageSummary(requests=1, input_tokens=10, output_tokens=5),
                final_output=output.model_dump(mode="json"),
            ),
        )

    monkeypatch.setattr(cli, "run_structured_agent", fake_run_structured_agent)
    monkeypatch.setattr(cli, "trace", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(cli, "flush_traces", lambda: None)
    monkeypatch.setattr(cli, "gen_trace_id", lambda: "trace_test")
    controller = BugfixController(
        repo_root=repo,
        target_dir=Path("demo_target"),
        issue_path=Path("demo_target/issues/BUG-001.md"),
        settings=Settings(model="test-model"),
        keep_worktree=True,
        inject_invalid_explorer=False,
        input_fn=lambda _: "y",
    )

    try:
        assert controller.run() == 0
        state = controller._require_state()
        store = controller._require_store()
        worktree = controller._require_worktree()
        assert roles == ["explorer", "test_writer", "fixer", "reviewer"]
        assert turn_budgets == [
            ("explorer", 12),
            ("test_writer", 8),
            ("fixer", 6),
            ("reviewer", 2),
        ]
        assert len(reviewer_inputs) == 1
        assert "EXACT RED-BEFORE EVIDENCE" in reviewer_inputs[0]
        assert "exit_code: 1" in reviewer_inputs[0]
        assert "red_gate_accepted: True" in reviewer_inputs[0]
        assert "AssertionError" in reviewer_inputs[0]
        assert state.phase == Phase.END
        assert state.human_decision == "approved"
        assert state.final_verifier is True
        assert state.changed_files == [
            "demo_target/src/config_service/resolver.py",
            "demo_target/tests/test_bug001_regression.py",
        ]
        assert state.branch_name is not None
        assert _git(worktree, "branch", "--show-current") == state.branch_name
        assert (store.root / "verification/test-before.txt").is_file()
        assert (store.root / "verification/trusted-acceptance.txt").is_file()
        assert (store.root / "decision-package.md").is_file()
        assert (store.root / "pr-body.md").is_file()
    finally:
        if controller.worktree is not None and controller.worktree.exists():
            remove_worktree(repo, controller.worktree)
