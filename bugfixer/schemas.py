"""Strict structured-output contracts shared across agent boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects unexpected fields from untrusted output."""

    model_config = ConfigDict(extra="forbid")


class EvidenceItem(StrictModel):
    file: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    observation: str = Field(min_length=1)


class ExplorerOutput(StrictModel):
    root_cause_summary: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(min_length=1)
    relevant_tests: list[str]
    uncertainties: list[str]


class TestWriterOutput(StrictModel):
    file_path: Literal["tests/test_bug001_regression.py"]
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class FixerOutput(StrictModel):
    file_path: Literal["src/config_service/resolver.py"]
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    risk_notes: list[str]


class RequirementCheck(StrictModel):
    requirement: str = Field(min_length=1)
    status: Literal["met", "not_met", "uncertain"]
    evidence: str = Field(min_length=1)


class ReviewerOutput(StrictModel):
    verdict: Literal["approve", "reject"]
    requirement_checks: list[RequirementCheck] = Field(min_length=1)
    blockers: list[str]
    notes: list[str]


class SmokeOutput(StrictModel):
    status: Literal["ok"]
    message: str = Field(min_length=1, max_length=80)

