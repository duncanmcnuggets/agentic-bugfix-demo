"""Public application bootstrap entry point."""

from collections.abc import Mapping
from typing import Literal

from config_service.loader import load_runtime_config
from config_service.models import StartupPlan


def build_startup_plan(project_config: Mapping[str, object]) -> StartupPlan:
    """Build the externally visible startup plan."""

    runtime = load_runtime_config(project_config)
    mode: Literal["on", "off"] = "on" if runtime.feature_enabled else "off"
    return StartupPlan(retry_budget=runtime.max_retries, feature_mode=mode)
