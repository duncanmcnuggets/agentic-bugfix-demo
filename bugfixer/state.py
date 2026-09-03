"""Durable workflow state with atomic persistence."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Phase(StrEnum):
    START = "START"
    CREATE_WORKTREE = "CREATE_WORKTREE"
    EXPLORER = "EXPLORER"
    GROUNDING_GATE = "GROUNDING_GATE"
    TEST_WRITER = "TEST_WRITER"
    TEST_POLICY_GATE = "TEST_POLICY_GATE"
    RED_GATE = "RED_GATE"
    FIXER = "FIXER"
    FIX_POLICY_GATE = "FIX_POLICY_GATE"
    VERIFY = "VERIFY"
    REVIEWER = "REVIEWER"
    HUMAN_GATE = "HUMAN_GATE"
    PREPARED = "PREPARED"
    END = "END"
    FAILED = "FAILED"


class RunState(BaseModel):
    """Inspectable state object for one controller run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    controller_version: str
    issue_id: str
    issue_text: str
    acceptance_criteria: list[str]
    base_sha: str = ""
    worktree_path: str = ""
    phase: Phase = Phase.START
    changed_files: list[str] = Field(default_factory=list)
    agent_results: dict[str, dict[str, object]] = Field(default_factory=dict)
    verifier_results: dict[str, dict[str, object]] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, int | float] = Field(
        default_factory=lambda: {
            "agent_calls": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_latency_seconds": 0.0,
        }
    )
    started_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None
    human_decision: str | None = None
    branch_name: str | None = None
    trace_id: str | None = None
    reviewer_verdict: str | None = None
    first_pass_verifier: bool | None = None
    final_verifier: bool | None = None
    failure_class: str | None = None
    error: str | None = None

    def transition(self, phase: Phase) -> None:
        self.phase = phase

    def record_retry(self, role: str) -> None:
        self.retry_counts[role] = self.retry_counts.get(role, 0) + 1

    def save_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)

