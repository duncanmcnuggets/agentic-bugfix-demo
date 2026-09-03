"""Role-specific OpenAI Agents SDK definitions."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from agents import Agent, ModelSettings
from openai.types.shared.reasoning import Reasoning

from bugfixer.config import Settings
from bugfixer.repo_tools import RepoContext, build_read_tools
from bugfixer.schemas import ExplorerOutput, FixerOutput, ReviewerOutput, TestWriterOutput

OutputT = TypeVar("OutputT")
PROMPT_DIR = Path(__file__).with_name("prompts")


def load_prompt(name: str) -> str:
    """Load a version-controlled runtime prompt."""

    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _model_settings(settings: Settings) -> ModelSettings:
    return ModelSettings(
        max_tokens=settings.max_output_tokens,
        reasoning=Reasoning(effort=settings.reasoning_effort),
        verbosity="low",
        parallel_tool_calls=False,
        include_usage=True,
        timeout=120.0,
    )


def create_explorer_agent(settings: Settings, repo: RepoContext) -> Agent[None]:
    return Agent(
        name="Explorer",
        instructions=load_prompt("explorer"),
        model=settings.model,
        model_settings=_model_settings(settings),
        tools=build_read_tools(repo),
        output_type=ExplorerOutput,
    )


def create_test_writer_agent(settings: Settings, repo: RepoContext) -> Agent[None]:
    return Agent(
        name="Implementer: Test Writer",
        instructions=load_prompt("test_writer"),
        model=settings.model,
        model_settings=_model_settings(settings),
        tools=build_read_tools(repo),
        output_type=TestWriterOutput,
    )


def create_fixer_agent(settings: Settings, repo: RepoContext) -> Agent[None]:
    return Agent(
        name="Implementer: Fixer",
        instructions=load_prompt("fixer"),
        model=settings.model,
        model_settings=_model_settings(settings),
        tools=build_read_tools(repo),
        output_type=FixerOutput,
    )


def create_reviewer_agent(settings: Settings) -> Agent[None]:
    return Agent(
        name="Independent Reviewer",
        instructions=load_prompt("reviewer"),
        model=settings.model,
        model_settings=_model_settings(settings),
        tools=[],
        output_type=ReviewerOutput,
    )

