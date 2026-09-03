"""Deterministic controller for the API-driven multi-agent bug-fixing workflow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, cast

from agents import Agent, flush_traces, gen_trace_id, trace
from pydantic import BaseModel, ValidationError

from bugfixer import __version__
from bugfixer.agents import (
    PROMPT_DIR,
    create_explorer_agent,
    create_fixer_agent,
    create_reviewer_agent,
    create_test_writer_agent,
)
from bugfixer.api_runner import AgentCallResult, AgentExecutionError, run_structured_agent
from bugfixer.artifacts import ArtifactStore
from bugfixer.config import ConfigurationError, Settings, load_settings
from bugfixer.git_ops import (
    GitOperationError,
    create_branch_and_commit,
    create_detached_worktree,
    ensure_clean,
    get_changed_files,
    get_diff,
    get_head_sha,
    remove_worktree,
    verify_repository,
)
from bugfixer.repo_tools import RepoContext
from bugfixer.schemas import ExplorerOutput, FixerOutput, ReviewerOutput, TestWriterOutput
from bugfixer.state import Phase, RunState, utc_now
from bugfixer.verifier import (
    SOURCE_FILE,
    TEST_FILE,
    CommandResult,
    VerificationError,
    all_checks_pass,
    enforce_changed_file_policy,
    file_sha256,
    run_final_verifier,
    run_red_gate,
    validate_candidate_python,
    validate_explorer_grounding,
    verifier_report,
)

OutputT = TypeVar("OutputT", bound=BaseModel)
MAX_MODEL_CALLS = 6
GIT_USER_NAME = "duncanmcnuggets"
GIT_USER_EMAIL = "1610864nab@gmail.com"


class WorkflowFailure(RuntimeError):
    """Expected fail-closed termination with a stable failure class."""

    def __init__(self, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def make_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:8]}"


def extract_acceptance_criteria(issue_text: str) -> list[str]:
    """Extract Markdown bullets from the Expected section."""

    match = re.search(
        r"^##\s+Expected\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        issue_text,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise WorkflowFailure("invalid_issue", "Issue is missing an Expected section")
    criteria = [
        line.lstrip("-* ").strip()
        for line in match.group("body").splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    if not criteria:
        raise WorkflowFailure("invalid_issue", "Issue contains no acceptance criteria")
    return criteria


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.candidate")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _command_artifact(result: CommandResult) -> str:
    command = " ".join(result.command)
    return (
        f"COMMAND: {command}\n"
        f"CWD: {result.cwd}\n"
        f"STARTED: {result.started_at}\n"
        f"FINISHED: {result.finished_at}\n"
        f"EXIT CODE: {result.exit_code}\n"
        f"TIMED OUT: {result.timed_out}\n"
        f"PASSED: {result.passed}\n\n"
        f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n"
    )


class BugfixController:
    """Trusted owner of routing, writes, verification, retries, and side effects."""

    def __init__(
        self,
        *,
        repo_root: Path,
        target_dir: Path,
        issue_path: Path,
        settings: Settings,
        keep_worktree: bool,
        inject_invalid_explorer: bool,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.target_dir = target_dir
        self.issue_path = issue_path
        self.settings = settings
        self.keep_worktree = keep_worktree
        self.inject_invalid_explorer = inject_invalid_explorer
        self.input_fn = input_fn
        self.state: RunState | None = None
        self.store: ArtifactStore | None = None
        self.worktree: Path | None = None
        self.repo_context: RepoContext | None = None
        self._role_call_counts: dict[str, int] = {}

    def _require_state(self) -> RunState:
        if self.state is None:
            raise RuntimeError("Controller state is not initialized")
        return self.state

    def _require_store(self) -> ArtifactStore:
        if self.store is None:
            raise RuntimeError("Artifact store is not initialized")
        return self.store

    def _require_worktree(self) -> Path:
        if self.worktree is None:
            raise RuntimeError("Worktree is not initialized")
        return self.worktree

    def _transition(self, phase: Phase) -> None:
        state = self._require_state()
        state.transition(phase)
        self._require_store().save_state(state)
        print(f"[{phase.value}]")

    def _event(self, event: str, **data: object) -> None:
        self._require_store().append_event(event, phase=self._require_state().phase.value, **data)

    def _record_agent_result(self, role: str, result: AgentCallResult[BaseModel]) -> None:
        state = self._require_state()
        count = self._role_call_counts[role]
        artifact_role = role if count == 1 else f"{role}-retry-{count - 1}"
        record = result.record.to_dict()
        state.agent_results[artifact_role] = record
        usage = result.record.usage
        state.usage["agent_calls"] = int(state.usage["agent_calls"]) + 1
        state.usage["input_tokens"] = int(state.usage["input_tokens"]) + usage.input_tokens
        state.usage["cached_input_tokens"] = (
            int(state.usage["cached_input_tokens"]) + usage.cached_input_tokens
        )
        state.usage["output_tokens"] = int(state.usage["output_tokens"]) + usage.output_tokens
        state.usage["reasoning_tokens"] = (
            int(state.usage["reasoning_tokens"]) + usage.reasoning_tokens
        )
        state.usage["total_latency_seconds"] = round(
            float(state.usage["total_latency_seconds"]) + result.record.elapsed_seconds, 3
        )
        self._require_store().write_json(f"agents/{artifact_role}.json", record)
        self._require_store().save_state(state)

    def _agent_for_role(self, role: str) -> Agent[None]:
        repo = self.repo_context
        if role == "reviewer":
            return create_reviewer_agent(self.settings)
        if repo is None:
            raise RuntimeError("Repository tools are not initialized")
        if role == "explorer":
            return create_explorer_agent(self.settings, repo)
        if role == "test_writer":
            return create_test_writer_agent(self.settings, repo)
        if role == "fixer":
            return create_fixer_agent(self.settings, repo)
        raise ValueError(f"Unknown role: {role}")

    def _call_role(
        self,
        role: str,
        input_text: str,
        output_type: type[OutputT],
    ) -> OutputT:
        state = self._require_state()
        current_calls = int(state.usage["agent_calls"])
        if current_calls >= MAX_MODEL_CALLS:
            raise WorkflowFailure("model_call_budget", "Maximum model-call budget reached")
        self._role_call_counts[role] = self._role_call_counts.get(role, 0) + 1
        count = self._role_call_counts[role]
        prompt_name = role if count == 1 else f"{role}-retry-{count - 1}"
        self._require_store().write_text(f"prompts/{prompt_name}-input.txt", input_text)
        self._event("agent_started", role=role, attempt=count, model=self.settings.model)
        print(f"[{role.upper()}] API call started (attempt {count})")
        try:
            result = run_structured_agent(
                self._agent_for_role(role),
                input_text,
                output_type=output_type,
                run_id=state.run_id,
                role=role,
                model=self.settings.model,
                max_turns=self.settings.max_turns,
            )
        except AgentExecutionError as exc:
            self._event(
                "agent_failed",
                role=role,
                attempt=count,
                failure_class=exc.failure_class,
                error=str(exc),
            )
            raise
        self._record_agent_result(role, cast(AgentCallResult[BaseModel], result))
        self._event(
            "agent_finished",
            role=role,
            attempt=count,
            elapsed_seconds=result.record.elapsed_seconds,
        )
        print(f"[{role.upper()}] Structured output received")
        return result.output

    def _retry(self, role: str, reason: str) -> None:
        state = self._require_state()
        if state.retry_counts.get(role, 0) >= 1:
            raise WorkflowFailure(f"{role}_retry_exhausted", reason)
        state.record_retry(role)
        self._event("retry_started", role=role, reason=reason)
        self._require_store().save_state(state)
        print(f"[RETRY] {role}: {reason}")

    def _explorer(self, issue: str, criteria: list[str]) -> ExplorerOutput:
        self._transition(Phase.EXPLORER)
        diagnostic = ""
        while True:
            prompt = (
                "BUG REPORT\n"
                f"{issue}\n\nACCEPTANCE CRITERIA\n{json.dumps(criteria, indent=2)}\n"
                f"{diagnostic}"
            )
            try:
                output = self._call_role("explorer", prompt, ExplorerOutput)
            except AgentExecutionError as exc:
                if exc.failure_class not in {"structured_output_failure", "temporary_api_error"}:
                    raise WorkflowFailure(exc.failure_class, str(exc)) from exc
                self._retry("explorer", str(exc))
                diagnostic = f"\nRETRY DIAGNOSTIC\n{exc}\n"
                continue

            if self.inject_invalid_explorer and self._role_call_counts["explorer"] == 1:
                self._event("fault_injection_started", kind="controlled invalid Explorer schema")
                self._require_store().write_json(
                    "agents/explorer-original-before-injection.json", output.model_dump(mode="json")
                )
                corrupted = output.model_dump(mode="json")
                corrupted["evidence"] = "not-a-list"
                try:
                    ExplorerOutput.model_validate(corrupted)
                except ValidationError as exc:
                    error = str(exc)
                    self._require_store().write_text(
                        "verification/controlled-schema-error.txt", error
                    )
                    self._event("gate_failed", gate="schema", controlled_fault_injection=True)
                    print("[SCHEMA_ERROR] Controlled fault injection rejected")
                    print("[GATE] Test Writer was not started")
                    self._retry("explorer", error)
                    diagnostic = f"\nRETRY DIAGNOSTIC\nSchema validation failed:\n{error}\n"
                    continue
                raise RuntimeError("Fault injection unexpectedly passed schema validation")

            self._transition(Phase.GROUNDING_GATE)
            try:
                grounded = validate_explorer_grounding(self.repo_context.target_root, output)  # type: ignore[union-attr]
            except VerificationError as exc:
                self._event("gate_failed", gate="grounding", error=str(exc))
                self._retry("explorer", str(exc))
                diagnostic = f"\nRETRY DIAGNOSTIC\nGrounding failed: {exc}\n"
                self._transition(Phase.EXPLORER)
                continue
            self._event("gate_passed", gate="grounding", evidence=grounded)
            print(f"[GROUNDING] PASSED: {', '.join(grounded)}")
            return output

    def _test_writer(
        self, issue: str, criteria: list[str], explorer: ExplorerOutput
    ) -> tuple[TestWriterOutput, str, CommandResult]:
        diagnostic = ""
        target_root = self.repo_context.target_root  # type: ignore[union-attr]
        test_path = target_root / "tests" / "test_bug001_regression.py"
        while True:
            self._transition(Phase.TEST_WRITER)
            prompt = (
                "BUG REPORT\n"
                f"{issue}\n\nACCEPTANCE CRITERIA\n{json.dumps(criteria, indent=2)}\n\n"
                f"VALIDATED EXPLORER OUTPUT\n{explorer.model_dump_json(indent=2)}\n{diagnostic}"
            )
            try:
                output = self._call_role("test_writer", prompt, TestWriterOutput)
            except AgentExecutionError as exc:
                if exc.failure_class not in {"structured_output_failure", "temporary_api_error"}:
                    raise WorkflowFailure(exc.failure_class, str(exc)) from exc
                self._retry("test_writer", str(exc))
                diagnostic = f"\nRETRY DIAGNOSTIC\n{exc}\n"
                continue

            self._transition(Phase.TEST_POLICY_GATE)
            if output.file_path != "tests/test_bug001_regression.py":
                raise WorkflowFailure("policy_violation", "Test Writer returned a forbidden path")
            if test_path.exists():
                raise WorkflowFailure("policy_violation", "Regression test path already exists")
            try:
                validate_candidate_python(
                    output.content,
                    allowed_import_roots={"config_service", "pytest"},
                    required_symbol="test_explicit_falsy_values_are_preserved",
                )
            except VerificationError as exc:
                raise WorkflowFailure("policy_violation", str(exc)) from exc
            _atomic_write(test_path, output.content)
            try:
                changed = enforce_changed_file_policy(
                    self._require_worktree(), {TEST_FILE}, require_exact=True
                )
            except VerificationError as exc:
                raise WorkflowFailure("policy_violation", str(exc)) from exc
            self._require_state().changed_files = changed
            self._event("gate_passed", gate="test_write_policy", changed_files=changed)

            self._transition(Phase.RED_GATE)
            red = run_red_gate(self._require_worktree())
            self._require_store().write_text("verification/test-before.txt", _command_artifact(red))
            self._require_state().verifier_results["test-before"] = red.to_dict()
            self._require_store().save_state(self._require_state())
            if red.passed:
                self._event("gate_passed", gate="red_test", exit_code=red.exit_code)
                print("[RED_GATE] PASSED: assertion failure reproduced")
                return output, file_sha256(test_path), red

            self._event("gate_failed", gate="red_test", exit_code=red.exit_code)
            test_path.unlink(missing_ok=False)
            self._retry("test_writer", red.combined_output[-4_000:])
            diagnostic = (
                "\nRETRY DIAGNOSTIC\nThe previous test did not produce an accepted assertion "
                f"failure. Exact output:\n{red.combined_output[-4_000:]}\n"
            )

    def _save_verifier_results(
        self, results: dict[str, CommandResult], *, attempt: int, final_names: bool
    ) -> None:
        state = self._require_state()
        for name, result in results.items():
            artifact_name = name if final_names else f"{name}-attempt-{attempt}"
            self._require_store().write_text(
                f"verification/{artifact_name}.txt", _command_artifact(result)
            )
            state.verifier_results[artifact_name] = result.to_dict()
        self._require_store().save_state(state)

    def _fixer(
        self,
        issue: str,
        criteria: list[str],
        explorer: ExplorerOutput,
        red: CommandResult,
        test_hash: str,
    ) -> tuple[FixerOutput, dict[str, CommandResult]]:
        target_root = self.repo_context.target_root  # type: ignore[union-attr]
        source_path = target_root / "src" / "config_service" / "resolver.py"
        test_path = target_root / "tests" / "test_bug001_regression.py"
        original_source = source_path.read_text(encoding="utf-8")
        diagnostic = ""
        attempt = 0
        while True:
            attempt += 1
            self._transition(Phase.FIXER)
            prompt = (
                "BUG REPORT\n"
                f"{issue}\n\nACCEPTANCE CRITERIA\n{json.dumps(criteria, indent=2)}\n\n"
                f"VALIDATED EXPLORER OUTPUT\n{explorer.model_dump_json(indent=2)}\n\n"
                f"EXACT RED TEST OUTPUT\n{red.combined_output[-6_000:]}\n\n"
                f"CURRENT resolver.py\n{source_path.read_text(encoding='utf-8')}\n{diagnostic}"
            )
            try:
                output = self._call_role("fixer", prompt, FixerOutput)
            except AgentExecutionError as exc:
                if exc.failure_class not in {"structured_output_failure", "temporary_api_error"}:
                    raise WorkflowFailure(exc.failure_class, str(exc)) from exc
                self._retry("fixer", str(exc))
                diagnostic = f"\nRETRY DIAGNOSTIC\n{exc}\n"
                continue

            self._transition(Phase.FIX_POLICY_GATE)
            if output.file_path != "src/config_service/resolver.py":
                raise WorkflowFailure("policy_violation", "Fixer returned a forbidden path")
            try:
                validate_candidate_python(
                    output.content,
                    allowed_import_roots={"typing"},
                    required_symbol="resolve_override",
                )
            except VerificationError as exc:
                raise WorkflowFailure("policy_violation", str(exc)) from exc
            _atomic_write(source_path, output.content)
            if file_sha256(test_path) != test_hash:
                raise WorkflowFailure("policy_violation", "Regression test changed after Fixer")
            try:
                changed = enforce_changed_file_policy(
                    self._require_worktree(), {TEST_FILE, SOURCE_FILE}, require_exact=True
                )
            except VerificationError as exc:
                raise WorkflowFailure("policy_violation", str(exc)) from exc
            self._require_state().changed_files = changed
            self._event("gate_passed", gate="fix_write_policy", changed_files=changed)

            self._transition(Phase.VERIFY)
            results = run_final_verifier(self._require_worktree(), self.repo_root)
            passed = all_checks_pass(results)
            state = self._require_state()
            if state.first_pass_verifier is None:
                state.first_pass_verifier = passed
            state.final_verifier = passed
            self._save_verifier_results(results, attempt=attempt, final_names=passed)
            if passed:
                self._event("gate_passed", gate="final_verifier")
                print("[VERIFY] PASSED: targeted, suite, ruff, mypy, diff, trusted acceptance")
                return output, results

            report = verifier_report(results)
            self._event("gate_failed", gate="final_verifier", report=report)
            _atomic_write(source_path, original_source)
            if file_sha256(test_path) != test_hash:
                raise WorkflowFailure("policy_violation", "Regression test hash changed")
            self._retry("fixer", report)
            diagnostic = f"\nRETRY DIAGNOSTIC\nFinal verification failed:\n{report}\n"

    def _reviewer(
        self,
        issue: str,
        criteria: list[str],
        diff: str,
        results: dict[str, CommandResult],
    ) -> ReviewerOutput:
        self._transition(Phase.REVIEWER)
        diagnostic = ""
        while True:
            prompt = (
                "BUG REPORT\n"
                f"{issue}\n\nACCEPTANCE CRITERIA\n{json.dumps(criteria, indent=2)}\n\n"
                f"BASE-TO-CANDIDATE DIFF\n{diff}\n\n"
                f"EXACT VERIFIER REPORT\n{verifier_report(results)}\n{diagnostic}"
            )
            try:
                output = self._call_role("reviewer", prompt, ReviewerOutput)
            except AgentExecutionError as exc:
                if exc.failure_class not in {"structured_output_failure", "temporary_api_error"}:
                    raise WorkflowFailure(exc.failure_class, str(exc)) from exc
                self._retry("reviewer", str(exc))
                diagnostic = f"\nRETRY DIAGNOSTIC\n{exc}\n"
                continue
            self._require_state().reviewer_verdict = output.verdict
            if output.verdict != "approve":
                self._event("gate_failed", gate="reviewer", blockers=output.blockers)
                raise WorkflowFailure(
                    "reviewer_rejection", f"Independent reviewer rejected: {output.blockers}"
                )
            self._event("gate_passed", gate="reviewer")
            print("[REVIEWER] APPROVE")
            return output

    def _write_decision_package(
        self,
        red: CommandResult,
        results: dict[str, CommandResult],
        reviewer: ReviewerOutput,
        diff: str,
    ) -> None:
        state = self._require_state()
        changed = "\n".join(f"- `{path}`" for path in state.changed_files)
        checks = "\n".join(
            f"- {name}: {'PASS' if result.passed else 'FAIL'} (exit {result.exit_code})"
            for name, result in results.items()
        )
        package = f"""# Decision package — {state.run_id}

## Provenance

- Base SHA: `{state.base_sha}`
- Controller version: `{state.controller_version}`
- Model: `{self.settings.model}`
- Agents SDK trace ID: `{state.trace_id}`

## Changed files

{changed}

## Red-before evidence

- Accepted assertion failure: {red.passed}
- Exit code: {red.exit_code}
- Artifact: `verification/test-before.txt`

## Green-after verification

{checks}

## Independent review

- Verdict: **{reviewer.verdict.upper()}**
- Blockers: {reviewer.blockers}
- Notes: {reviewer.notes}

## Usage and latency

```json
{json.dumps(state.usage, indent=2)}
```

## Human decision

Pending. The controller cannot merge or deploy.
"""
        self._require_store().write_text("decision-package.md", package)
        self._require_store().write_text("diff.patch", diff)

    def _write_pr_body(self) -> None:
        state = self._require_state()
        body = f"""<!-- agent-run-id: {state.run_id} -->

## Issue

BUG-001: Explicit falsy configuration values are ignored.

## Changes

- Added a public-behavior regression test for explicit `0` and `False` values.
- Changed default resolution so only `None` is treated as missing.

## Red-before evidence

- Regression test failed by assertion on base SHA `{state.base_sha}`.
- Evidence: `artifacts/{state.run_id}/verification/test-before.txt`.

## Verification

- Targeted regression test: PASS
- Full target suite: PASS
- Ruff: PASS
- Mypy: PASS
- Git diff check: PASS
- Trusted acceptance: PASS

## Independent review

Verdict: **APPROVE**

## Human decision

Approved for draft PR publication. Merge remains a human decision.
"""
        self._require_store().write_text("pr-body.md", body)

    def _finalize_decision_package(self) -> None:
        state = self._require_state()
        path = self._require_store().root / "decision-package.md"
        content = path.read_text(encoding="utf-8")
        replacement = (
            "Approved local branch preparation. Merge and deployment remain human decisions."
            if state.human_decision == "approved"
            else "Declined local branch preparation. Evidence was retained without a branch."
        )
        self._require_store().write_text(
            "decision-package.md",
            content.replace("Pending. The controller cannot merge or deploy.", replacement),
        )

    def _write_metrics(self) -> None:
        state = self._require_state()
        metrics = {
            **state.usage,
            "retries": sum(state.retry_counts.values()),
            "retry_counts": state.retry_counts,
            "first_pass_verifier": state.first_pass_verifier,
            "final_verifier": state.final_verifier,
            "reviewer_verdict": state.reviewer_verdict,
            "human_decision": state.human_decision,
            "failure_class": state.failure_class,
        }
        self._require_store().write_json("metrics.json", metrics)

    def _pipeline(self, issue: str, criteria: list[str]) -> None:
        state = self._require_state()
        explorer = self._explorer(issue, criteria)
        _, test_hash, red = self._test_writer(issue, criteria, explorer)
        _, results = self._fixer(issue, criteria, explorer, red, test_hash)
        diff = get_diff(self._require_worktree(), state.base_sha)
        reviewer = self._reviewer(issue, criteria, diff, results)
        self._write_decision_package(red, results, reviewer, diff)

        self._transition(Phase.HUMAN_GATE)
        decision = self.input_fn("Prepare local branch for draft PR? [y/N]: ").strip().casefold()
        approved = decision in {"y", "yes"}
        state.human_decision = "approved" if approved else "declined"
        self._event("human_decision", decision=state.human_decision)
        self._finalize_decision_package()
        if not approved:
            state.finished_at = utc_now()
            self._transition(Phase.END)
            return

        if get_head_sha(self.repo_root) != state.base_sha:
            raise WorkflowFailure("stale_base", "Main HEAD changed during the run")
        branch_name = f"agent/BUG-001-{state.run_id[-8:]}"
        create_branch_and_commit(
            self._require_worktree(),
            branch_name,
            [TEST_FILE, SOURCE_FILE],
            "fix: preserve explicit falsy configuration values",
            user_name=GIT_USER_NAME,
            user_email=GIT_USER_EMAIL,
        )
        state.branch_name = branch_name
        self._write_pr_body()
        self._event("branch_created", branch=branch_name)
        self._transition(Phase.PREPARED)
        state.finished_at = utc_now()
        self._transition(Phase.END)
        print(f"Branch created: {branch_name}")
        print(f"Worktree retained: {self._require_worktree()}")
        print(f"PR body: {self._require_store().root / 'pr-body.md'}")

    def run(self) -> int:
        repo = verify_repository(self.repo_root)
        ensure_clean(repo)
        issue_absolute = (repo / self.issue_path).resolve()
        target_relative = self.target_dir
        if not issue_absolute.is_file():
            raise WorkflowFailure("invalid_issue", f"Issue not found: {issue_absolute}")
        issue = issue_absolute.read_text(encoding="utf-8")
        criteria = extract_acceptance_criteria(issue)
        run_id = make_run_id()
        self.state = RunState(
            run_id=run_id,
            controller_version=__version__,
            issue_id=issue_absolute.stem,
            issue_text=issue,
            acceptance_criteria=criteria,
        )
        self.store = ArtifactStore.create(repo, run_id)
        self.store.copy_runtime_prompts(PROMPT_DIR)
        self.store.save_state(self.state)
        self._event("run_started", run_id=run_id)
        print(f"[START] Run ID: {run_id}")

        success = False
        try:
            self.state.base_sha = get_head_sha(repo)
            self._transition(Phase.CREATE_WORKTREE)
            self.worktree = create_detached_worktree(repo, self.state.base_sha)
            target_root = (self.worktree / target_relative).resolve()
            self.repo_context = RepoContext(target_root)
            self.state.worktree_path = str(self.worktree)
            self.store.save_state(self.state)
            self._event(
                "worktree_created", base_sha=self.state.base_sha, path=str(self.worktree)
            )
            print(f"[GIT] Base SHA: {self.state.base_sha}")
            print(f"[GIT] Detached worktree: {self.worktree}")

            trace_id = gen_trace_id()
            self.state.trace_id = trace_id
            self.store.save_state(self.state)
            with trace(
                "agentic-bugfixer-workflow",
                trace_id=trace_id,
                group_id=run_id,
                metadata={"issue_id": self.state.issue_id, "controller_version": __version__},
            ):
                self._pipeline(issue, criteria)
            success = True
            self._event("run_finished", status="success")
            return 0
        except WorkflowFailure as exc:
            self.state.failure_class = exc.failure_class
            self.state.error = str(exc)
            self.state.finished_at = utc_now()
            self.state.transition(Phase.FAILED)
            self.store.save_state(self.state)
            self._event(
                "gate_failed", gate=self.state.phase.value, failure_class=exc.failure_class
            )
            self._event("run_finished", status="failed", failure_class=exc.failure_class)
            print(f"[FAILED] {exc.failure_class}: {exc}", file=sys.stderr)
            return 1
        except (GitOperationError, VerificationError) as exc:
            self.state.failure_class = "controller_or_environment_error"
            self.state.error = str(exc)
            self.state.finished_at = utc_now()
            self.state.transition(Phase.FAILED)
            self.store.save_state(self.state)
            self._event(
                "run_finished", status="failed", failure_class=self.state.failure_class
            )
            print(f"[FAILED] {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            self.state.failure_class = "unexpected_controller_error"
            self.state.error = f"Unexpected controller failure ({type(exc).__name__})"
            self.state.finished_at = utc_now()
            self.state.transition(Phase.FAILED)
            self.store.save_state(self.state)
            self._event(
                "run_finished", status="failed", failure_class=self.state.failure_class
            )
            print(f"[FAILED] {self.state.error}", file=sys.stderr)
            return 1
        finally:
            try:
                flush_traces()
            except Exception as exc:
                if self.store is not None and self.state is not None:
                    self._event("trace_flush_failed", error_type=type(exc).__name__)
            if self.state is not None and self.store is not None:
                if self.worktree is not None:
                    current_changes = get_changed_files(self.worktree)
                    if current_changes:
                        self.state.changed_files = current_changes
                self.store.save_state(self.state)
                self._write_metrics()
            if self.worktree is not None and not self.keep_worktree and not (
                success and self._require_state().human_decision == "approved"
            ):
                remove_worktree(repo, self.worktree)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--target-dir", type=Path, default=Path("demo_target"))
    parser.add_argument("--issue", type=Path, default=Path("demo_target/issues/BUG-001.md"))
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument("--inject-invalid-explorer", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        controller = BugfixController(
            repo_root=args.repo,
            target_dir=args.target_dir,
            issue_path=args.issue,
            settings=settings,
            keep_worktree=args.keep_worktree,
            inject_invalid_explorer=args.inject_invalid_explorer,
        )
        return controller.run()
    except (ConfigurationError, GitOperationError, WorkflowFailure) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
