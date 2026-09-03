"""Domain models for startup configuration."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Resolved internal configuration."""

    max_retries: int
    feature_enabled: bool


@dataclass(frozen=True, slots=True)
class StartupPlan:
    """Public startup plan consumed by the application bootstrap."""

    retry_budget: int
    feature_mode: Literal["on", "off"]
