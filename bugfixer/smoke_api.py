"""One low-cost live API smoke test with strict structured output."""

from __future__ import annotations

from agents import Agent, ModelSettings, flush_traces, trace
from openai.types.shared.reasoning import Reasoning

from bugfixer.api_runner import AgentExecutionError, run_structured_agent
from bugfixer.config import ConfigurationError, load_settings
from bugfixer.schemas import SmokeOutput


def main() -> int:
    settings = load_settings()
    agent: Agent[None] = Agent(
        name="API configuration smoke test",
        instructions="Return status='ok' and a brief confirmation. Do not call tools.",
        model=settings.model,
        model_settings=ModelSettings(
            max_tokens=200,
            reasoning=Reasoning(effort="none"),
            verbosity="low",
            include_usage=True,
            timeout=60.0,
        ),
        output_type=SmokeOutput,
    )
    with trace("agentic-bugfixer-smoke"):
        result = run_structured_agent(
            agent,
            "Confirm that this structured API request is operational.",
            output_type=SmokeOutput,
            run_id="smoke",
            role="smoke",
            model=settings.model,
            max_turns=2,
        )
    flush_traces()
    usage = result.record.usage
    print("API configured: yes")
    print(f"Model: {settings.model}")
    print("Structured response: valid")
    print(f"Elapsed: {result.record.elapsed_seconds:.3f}s")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print("API smoke test: PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, AgentExecutionError) as exc:
        print(f"API smoke test: FAILED - {exc}")
        raise SystemExit(1) from None
