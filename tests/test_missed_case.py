from __future__ import annotations

from pathlib import Path

from bugfixer.evals.missed_case import PARTIAL_FIX, WEAK_TEST, apply_controlled_candidate


def test_controlled_candidate_is_partial_and_weak(tmp_path: Path) -> None:
    source = tmp_path / "demo_target" / "src" / "config_service" / "resolver.py"
    test = tmp_path / "demo_target" / "tests" / "test_bug001_regression.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("baseline\n", encoding="utf-8")

    apply_controlled_candidate(tmp_path)

    assert source.read_text(encoding="utf-8") == PARTIAL_FIX
    assert test.read_text(encoding="utf-8") == WEAK_TEST
    assert "override is False" in PARTIAL_FIX
    assert "max_retries" not in WEAK_TEST

