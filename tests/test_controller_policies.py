from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import bugfixer.cli as cli
from bugfixer.api_runner import AgentExecutionError, UsageSummary
from bugfixer.artifacts import ArtifactStore
from bugfixer.cli import MAX_MODEL_CALLS, BugfixController, WorkflowFailure
from bugfixer.config import Settings
from bugfixer.repo_tools import RepoContext
from bugfixer.schemas import (
    EvidenceItem,
    ExplorerOutput,
    FixerOutput,
    RequirementCheck,
    ReviewerOutput,
)
from bugfixer.schemas import TestWriterOutput as WriterOutput
from bugfixer.state import Phase, RunState
from bugfixer.verifier import TEST_FILE, CommandResult


def _command(*, passed: bool, exit_code: int = 0, output: str = "") -> CommandResult:
    return CommandResult(
        command=["test"],
        cwd="worktree",
        started_at="start",
        finished_at="finish",
        exit_code=exit_code,
        stdout=output,
        stderr="",
        timed_out=False,
        passed=passed,
    )


def _explorer(symbol: str = "resolve_override") -> ExplorerOutput:
    return ExplorerOutput(
        root_cause_summary="Falsy values are replaced by defaults.",
        evidence=[
            EvidenceItem(
                file="src/config_service/resolver.py",
                symbol=symbol,
                observation="Uses truthiness instead of a None check.",
            )
        ],
        relevant_tests=["tests/test_config_service.py"],
        uncertainties=[],
    )


def _writer(path: str = "tests/test_bug001_regression.py") -> WriterOutput:
    return WriterOutput.model_construct(
        file_path=path,
        content="def test_explicit_falsy_values_are_preserved():\n    assert False\n",
        summary="Regression test",
    )


def _fixer() -> FixerOutput:
    return FixerOutput(
        file_path="src/config_service/resolver.py",
        content="fixed\n",
        summary="Minimal fix",
        risk_notes=[],
    )


def _reviewer(verdict: str = "approve") -> ReviewerOutput:
    return ReviewerOutput.model_construct(
        verdict=verdict,
        requirement_checks=[
            RequirementCheck(requirement="preserve falsy", status="met", evidence="diff")
        ],
        blockers=[] if verdict == "approve" else ["semantic mismatch"],
        notes=[],
    )


@pytest.fixture
def controller(tmp_path: Path) -> BugfixController:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    target = worktree / "demo_target"
    source = target / "src" / "config_service" / "resolver.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def resolve_override(override, default):\n    return override or default\n",
        encoding="utf-8",
    )
    (target / "tests").mkdir()
    repo.mkdir()
    instance = BugfixController(
        repo_root=repo,
        target_dir=Path("demo_target"),
        issue_path=Path("demo_target/issues/BUG-001.md"),
        settings=Settings(model="test-model"),
        keep_worktree=True,
        inject_invalid_explorer=False,
        input_fn=lambda _: "n",
    )
    instance.state = RunState(
        run_id="run-test",
        controller_version="test",
        issue_id="BUG-001",
        issue_text="issue",
        acceptance_criteria=["criterion"],
        base_sha="base-sha",
        worktree_path=str(worktree),
    )
    instance.store = ArtifactStore.create(repo, "run-test")
    instance.worktree = worktree
    instance.repo_context = RepoContext(target)
    return instance


def _install_fake_call(
    controller: BugfixController, outputs: dict[str, list[Any]], calls: list[str]
) -> None:
    def fake_call(role: str, input_text: str, output_type: type[Any]) -> Any:
        del input_text, output_type
        calls.append(role)
        controller._role_call_counts[role] = controller._role_call_counts.get(role, 0) + 1
        return outputs[role].pop(0)

    controller._call_role = fake_call  # type: ignore[method-assign]


def test_invalid_explorer_never_starts_test_writer(controller: BugfixController) -> None:
    calls: list[str] = []
    _install_fake_call(
        controller,
        {"explorer": [_explorer("invented"), _explorer("still_invented")]},
        calls,
    )
    with pytest.raises(WorkflowFailure, match="does not exist"):
        controller._pipeline("issue", ["criterion"])
    assert calls == ["explorer", "explorer"]


def test_fault_injection_blocks_writer_until_recovery(controller: BugfixController) -> None:
    controller.inject_invalid_explorer = True
    calls: list[str] = []
    _install_fake_call(controller, {"explorer": [_explorer(), _explorer()]}, calls)

    output = controller._explorer("issue", ["criterion"])

    assert output.evidence[0].symbol == "resolve_override"
    assert calls == ["explorer", "explorer"]
    assert controller._require_state().retry_counts == {"explorer": 1}
    assert (controller._require_store().root / "verification/controlled-schema-error.txt").is_file()


def test_failed_red_gate_never_starts_fixer(
    controller: BugfixController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _install_fake_call(controller, {"test_writer": [_writer(), _writer()]}, calls)
    monkeypatch.setattr(controller, "_explorer", lambda issue, criteria: _explorer())
    monkeypatch.setattr(cli, "enforce_changed_file_policy", lambda *args, **kwargs: [TEST_FILE])
    monkeypatch.setattr(
        cli,
        "run_red_gate",
        lambda worktree: _command(passed=False, output="test passed unexpectedly"),
    )

    with pytest.raises(WorkflowFailure, match="unexpectedly"):
        controller._pipeline("issue", ["criterion"])
    assert calls == ["test_writer", "test_writer"]
    assert "fixer" not in calls


def test_policy_violation_has_no_retry(controller: BugfixController) -> None:
    calls: list[str] = []
    _install_fake_call(controller, {"test_writer": [_writer("../bugfixer/cli.py")]}, calls)

    with pytest.raises(WorkflowFailure, match="forbidden path") as caught:
        controller._test_writer("issue", ["criterion"], _explorer())
    assert caught.value.failure_class == "policy_violation"
    assert controller._require_state().retry_counts == {}


def test_model_call_budget_is_fail_closed(controller: BugfixController) -> None:
    controller._model_call_attempts = MAX_MODEL_CALLS
    with pytest.raises(WorkflowFailure) as caught:
        controller._call_role("explorer", "input", ExplorerOutput)
    assert caught.value.failure_class == "model_call_budget"


def test_failed_agent_usage_is_retained(controller: BugfixController) -> None:
    controller._role_call_counts["explorer"] = 1
    error = AgentExecutionError(
        "explorer",
        "max_turns_exceeded",
        "Agent exceeded its 12-turn execution budget",
        elapsed_seconds=2.5,
        usage=UsageSummary(requests=12, input_tokens=100, output_tokens=25, total_tokens=125),
        max_turns=12,
    )

    controller._record_agent_failure("explorer", error)

    state = controller._require_state()
    assert state.usage["model_requests"] == 12
    assert state.usage["input_tokens"] == 100
    assert state.usage["output_tokens"] == 25
    assert state.usage["total_latency_seconds"] == 2.5
    assert state.agent_results["explorer-failure"]["failure"] == {
        "class": "max_turns_exceeded",
        "message": "Agent exceeded its 12-turn execution budget",
        "max_turns": 12,
    }
    assert (controller._require_store().root / "agents/explorer-failure.json").is_file()


def test_failed_verifier_never_starts_reviewer(
    controller: BugfixController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(controller, "_explorer", lambda issue, criteria: _explorer())
    monkeypatch.setattr(
        controller,
        "_test_writer",
        lambda *args: (
            _writer(),
            "hash",
            _command(passed=True, exit_code=1),
        ),
    )

    def fail_fixer(*args: object) -> tuple[FixerOutput, dict[str, CommandResult]]:
        calls.append("fixer")
        raise WorkflowFailure("fixer_retry_exhausted", "verification failed")

    monkeypatch.setattr(controller, "_fixer", fail_fixer)
    monkeypatch.setattr(controller, "_reviewer", lambda *args: calls.append("reviewer"))

    with pytest.raises(WorkflowFailure, match="verification failed"):
        controller._pipeline("issue", ["criterion"])
    assert calls == ["fixer"]


def test_reviewer_rejection_stops_before_human_gate(controller: BugfixController) -> None:
    calls: list[str] = []
    _install_fake_call(controller, {"reviewer": [_reviewer("reject")]}, calls)
    with pytest.raises(WorkflowFailure) as caught:
        controller._reviewer(
            "issue",
            ["criterion"],
            "diff",
            _command(passed=True, exit_code=1, output="AssertionError"),
            {"check": _command(passed=True)},
        )
    assert caught.value.failure_class == "reviewer_rejection"
    assert controller._require_state().phase == Phase.REVIEWER


def _mock_successful_gates(
    controller: BugfixController, monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    def explorer(*args: object) -> ExplorerOutput:
        events.append("explorer")
        return _explorer()

    def test_writer(*args: object) -> tuple[WriterOutput, str, CommandResult]:
        events.append("test_writer")
        return _writer(), "hash", _command(passed=True, exit_code=1)

    def fixer(*args: object) -> tuple[FixerOutput, dict[str, CommandResult]]:
        events.append("fixer")
        return _fixer(), {"all": _command(passed=True)}

    def reviewer(*args: object) -> ReviewerOutput:
        events.append("reviewer")
        return _reviewer()

    monkeypatch.setattr(controller, "_explorer", explorer)
    monkeypatch.setattr(controller, "_test_writer", test_writer)
    monkeypatch.setattr(controller, "_fixer", fixer)
    monkeypatch.setattr(controller, "_reviewer", reviewer)
    monkeypatch.setattr(
        controller, "_write_decision_package", lambda *args: events.append("package")
    )
    monkeypatch.setattr(controller, "_finalize_decision_package", lambda: None)
    monkeypatch.setattr(cli, "get_diff", lambda *args: "diff")


def test_human_no_does_not_create_branch(
    controller: BugfixController, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _mock_successful_gates(controller, monkeypatch, events)
    controller.input_fn = lambda _: "n"

    def create_branch(*args: object, **kwargs: object) -> str:
        events.append("branch")
        return "sha"

    monkeypatch.setattr(cli, "create_branch_and_commit", create_branch)

    controller._pipeline("issue", ["criterion"])

    assert controller._require_state().human_decision == "declined"
    assert "branch" not in events


def test_branch_is_created_only_after_all_gates_and_human_yes(
    controller: BugfixController, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _mock_successful_gates(controller, monkeypatch, events)
    controller.input_fn = lambda _: "y"
    monkeypatch.setattr(cli, "get_head_sha", lambda repo: "base-sha")
    def create_branch(*args: object, **kwargs: object) -> str:
        events.append("branch")
        return "commit-sha"

    monkeypatch.setattr(cli, "create_branch_and_commit", create_branch)
    monkeypatch.setattr(controller, "_write_pr_body", lambda: events.append("pr-body"))

    controller._pipeline("issue", ["criterion"])

    assert events == [
        "explorer",
        "test_writer",
        "fixer",
        "reviewer",
        "package",
        "branch",
        "pr-body",
    ]
    assert controller._require_state().human_decision == "approved"
    assert controller._require_state().phase == Phase.END
