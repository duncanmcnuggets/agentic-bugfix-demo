from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bugfixer.schemas import EvidenceItem, ExplorerOutput
from bugfixer.verifier import (
    VerificationError,
    enforce_changed_file_policy,
    run_command,
    run_red_gate,
    validate_candidate_python,
    validate_explorer_grounding,
)


def test_grounding_checks_file_and_ast_symbol(tmp_path: Path) -> None:
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("def actual_symbol():\n    return 1\n", encoding="utf-8")
    output = ExplorerOutput(
        root_cause_summary="summary",
        evidence=[
            EvidenceItem(file="src/module.py", symbol="actual_symbol", observation="found")
        ],
        relevant_tests=[],
        uncertainties=[],
    )
    assert validate_explorer_grounding(tmp_path, output) == ["src/module.py:actual_symbol"]


def test_grounding_rejects_hallucinated_symbol(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def real():\n    return 1\n", encoding="utf-8")
    output = ExplorerOutput(
        root_cause_summary="summary",
        evidence=[EvidenceItem(file="module.py", symbol="invented", observation="claim")],
        relevant_tests=[],
        uncertainties=[],
    )
    with pytest.raises(VerificationError, match="does not exist"):
        validate_explorer_grounding(tmp_path, output)


def test_subprocess_does_not_receive_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("CUSTOM_SERVICE_TOKEN", "must-also-be-removed")
    result = run_command(
        [
            sys.executable,
            "-c",
            (
                "import os; print('present' if os.getenv('OPENAI_API_KEY') "
                "or os.getenv('CUSTOM_SERVICE_TOKEN') else 'absent')"
            ),
        ],
        tmp_path,
    )
    assert result.passed
    assert result.stdout.strip() == "absent"


def test_candidate_policy_rejects_dangerous_import_before_execution() -> None:
    with pytest.raises(VerificationError, match="forbidden modules"):
        validate_candidate_python(
            "import os\n\ndef test_explicit_falsy_values_are_preserved():\n    assert True\n",
            allowed_import_roots={"config_service", "pytest"},
            required_symbol="test_explicit_falsy_values_are_preserved",
        )


def test_candidate_policy_accepts_narrow_regression_test() -> None:
    validate_candidate_python(
        (
            "from config_service.bootstrap import build_startup_plan\n\n"
            "def test_explicit_falsy_values_are_preserved():\n"
            "    plan = build_startup_plan({'max_retries': 0})\n"
            "    assert plan.retry_budget == 0\n"
        ),
        allowed_import_roots={"config_service", "pytest"},
        required_symbol="test_explicit_falsy_values_are_preserved",
    )


def test_red_gate_requires_real_assertion_failure(tmp_path: Path) -> None:
    test_path = tmp_path / "demo_target" / "tests" / "test_bug001_regression.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_regression():\n    assert 1 == 2\n", encoding="utf-8")
    result = run_red_gate(tmp_path)
    assert result.passed
    assert result.exit_code == 1


def test_red_gate_rejects_collection_error(tmp_path: Path) -> None:
    test_path = tmp_path / "demo_target" / "tests" / "test_bug001_regression.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("this is not python\n", encoding="utf-8")
    assert not run_red_gate(tmp_path).passed


def test_changed_file_policy_rejects_unexpected_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    (repo / "unexpected.txt").write_text("change\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="Unexpected"):
        enforce_changed_file_policy(repo, {"does-not-exist"})
