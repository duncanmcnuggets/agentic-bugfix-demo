from __future__ import annotations

import json
from pathlib import Path

from bugfixer.state import Phase, RunState


def test_state_is_saved_atomically(tmp_path: Path) -> None:
    state = RunState(
        run_id="run-test",
        controller_version="1",
        issue_id="BUG-001",
        issue_text="issue",
        acceptance_criteria=["criterion"],
    )
    target = tmp_path / "state.json"
    state.transition(Phase.EXPLORER)
    state.save_atomic(target)

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["phase"] == "EXPLORER"
    assert not (tmp_path / ".state.json.tmp").exists()


def test_retry_counts_are_role_specific() -> None:
    state = RunState(
        run_id="run-test",
        controller_version="1",
        issue_id="BUG-001",
        issue_text="issue",
        acceptance_criteria=[],
    )
    state.record_retry("explorer")
    state.record_retry("explorer")
    assert state.retry_counts == {"explorer": 2}

