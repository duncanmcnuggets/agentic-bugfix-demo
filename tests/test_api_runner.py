from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from agents import Agent
from agents.exceptions import MaxTurnsExceeded
from agents.usage import InputTokensDetails, OutputTokensDetails, Usage

from bugfixer.api_runner import AgentExecutionError, run_structured_agent
from bugfixer.schemas import SmokeOutput


class FakeResult:
    def __init__(self, output: SmokeOutput) -> None:
        self.output = output
        self.context_wrapper = SimpleNamespace(
            usage=Usage(
                requests=1,
                input_tokens=10,
                input_tokens_details=InputTokensDetails(
                    cache_write_tokens=0, cached_tokens=4
                ),
                output_tokens=3,
                output_tokens_details=OutputTokensDetails(reasoning_tokens=1),
                total_tokens=13,
            )
        )

    def final_output_as(
        self, output_type: type[SmokeOutput], raise_if_incorrect_type: bool = False
    ) -> SmokeOutput:
        assert raise_if_incorrect_type
        assert output_type is SmokeOutput
        return self.output


def test_run_structured_agent_extracts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_result = FakeResult(SmokeOutput(status="ok", message="ready"))
    monkeypatch.setattr("bugfixer.api_runner.Runner.run_sync", lambda *args, **kwargs: fake_result)
    agent = cast(Agent[None], object())

    result = run_structured_agent(
        agent,
        "input",
        output_type=SmokeOutput,
        run_id="run-test",
        role="smoke",
        model="test-model",
    )

    assert result.output.status == "ok"
    assert result.record.usage.cached_input_tokens == 4
    assert result.record.usage.reasoning_tokens == 1


def test_run_structured_agent_sanitizes_unknown_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("secret-bearing upstream message")

    monkeypatch.setattr("bugfixer.api_runner.Runner.run_sync", fail)
    with pytest.raises(AgentExecutionError, match="sensitive details omitted") as caught:
        run_structured_agent(
            cast(Agent[None], object()),
            "input",
            output_type=SmokeOutput,
            run_id="run-test",
            role="smoke",
            model="test-model",
        )
    assert "secret-bearing" not in str(caught.value)


def test_max_turn_failure_preserves_safe_usage_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = Usage(
        requests=8,
        input_tokens=120,
        output_tokens=40,
        total_tokens=160,
    )
    upstream = MaxTurnsExceeded("Max turns (8) exceeded")
    upstream.run_data = SimpleNamespace(context_wrapper=SimpleNamespace(usage=usage))

    def fail(*args: object, **kwargs: object) -> object:
        raise upstream

    monkeypatch.setattr("bugfixer.api_runner.Runner.run_sync", fail)
    with pytest.raises(AgentExecutionError, match="12-turn") as caught:
        run_structured_agent(
            cast(Agent[None], object()),
            "input",
            output_type=SmokeOutput,
            run_id="run-test",
            role="explorer",
            model="test-model",
            max_turns=12,
        )

    assert caught.value.failure_class == "max_turns_exceeded"
    assert caught.value.max_turns == 12
    assert caught.value.usage.requests == 8
    assert caught.value.usage.total_tokens == 160
