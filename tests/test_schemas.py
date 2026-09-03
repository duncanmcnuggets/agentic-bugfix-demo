from __future__ import annotations

import pytest
from pydantic import ValidationError

from bugfixer.schemas import EvidenceItem, ExplorerOutput
from bugfixer.schemas import TestWriterOutput as WriterOutput


def test_schemas_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvidenceItem(file="src/x.py", symbol="f", observation="found", surprise=True)


def test_explorer_requires_evidence_list() -> None:
    with pytest.raises(ValidationError):
        ExplorerOutput.model_validate(
            {
                "root_cause_summary": "summary",
                "evidence": "not-a-list",
                "relevant_tests": [],
                "uncertainties": [],
            }
        )


def test_test_writer_path_is_literal() -> None:
    with pytest.raises(ValidationError):
        WriterOutput(
            file_path="../bugfixer/cli.py",
            content="def test_x(): pass",
            summary="bad path",
        )
