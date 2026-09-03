"""Thin, observable wrapper around one isolated Agents SDK call."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import monotonic
from typing import Generic, TypeVar

from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, ModelTimeoutError
from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentExecutionError(RuntimeError):
    """Sanitized agent failure suitable for controller state and artifacts."""

    def __init__(self, role: str, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.role = role
        self.failure_class = failure_class


@dataclass(frozen=True, slots=True)
class UsageSummary:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentCallRecord:
    role: str
    model: str
    elapsed_seconds: float
    usage: UsageSummary
    final_output: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentCallResult(Generic[OutputT]):
    output: OutputT
    record: AgentCallRecord


def _usage_summary(result: object) -> UsageSummary:
    context_wrapper = getattr(result, "context_wrapper", None)
    usage = getattr(context_wrapper, "usage", None)
    if usage is None:
        return UsageSummary()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return UsageSummary(
        requests=int(getattr(usage, "requests", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        reasoning_tokens=int(getattr(output_details, "reasoning_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
    )


def run_structured_agent(
    agent: Agent[None],
    input_text: str,
    *,
    output_type: type[OutputT],
    run_id: str,
    role: str,
    model: str,
    max_turns: int = 8,
) -> AgentCallResult[OutputT]:
    """Run one agent in an independent context and return validated output plus metrics."""

    del run_id  # Used by the surrounding trace metadata; never placed in model output.
    started = monotonic()
    try:
        result = Runner.run_sync(agent, input_text, max_turns=max_turns)
        output = result.final_output_as(output_type, raise_if_incorrect_type=True)
    except ModelBehaviorError as exc:
        raise AgentExecutionError(
            role, "structured_output_failure", "Model output violated the structured contract"
        ) from exc
    except MaxTurnsExceeded as exc:
        raise AgentExecutionError(
            role, "max_turns_exceeded", "Agent exceeded its turn budget"
        ) from exc
    except ModelTimeoutError as exc:
        raise AgentExecutionError(role, "temporary_api_error", "Model request timed out") from exc
    except AgentExecutionError:
        raise
    except Exception as exc:
        raise AgentExecutionError(
            role,
            "api_error",
            f"Agent call failed safely ({type(exc).__name__}); sensitive details omitted",
        ) from exc

    elapsed = monotonic() - started
    record = AgentCallRecord(
        role=role,
        model=model,
        elapsed_seconds=round(elapsed, 3),
        usage=_usage_summary(result),
        final_output=output.model_dump(mode="json"),
    )
    return AgentCallResult(output=output, record=record)
