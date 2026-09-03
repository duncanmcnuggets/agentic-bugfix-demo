"""Runtime configuration loaded only at the trusted controller boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

DEFAULT_MODEL = "gpt-5.6-terra"


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is unavailable."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret settings used to construct agents."""

    model: str
    max_turns: int = 8
    max_output_tokens: int = 4_096
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = (
        "low"
    )


def load_settings() -> Settings:
    """Load settings and verify that the SDK can obtain an API credential.

    The key value is deliberately neither returned nor logged.
    """

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise ConfigurationError(
            "OPENAI_API_KEY is not configured. Set it in the current shell before a live run."
        )
    model = os.environ.get("BUGFIXER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return Settings(model=model)
