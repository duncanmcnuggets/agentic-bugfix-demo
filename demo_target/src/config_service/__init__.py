"""Configuration resolution example used by the bug-fixer demo."""

from config_service.bootstrap import build_startup_plan
from config_service.models import StartupPlan

__all__ = ["StartupPlan", "build_startup_plan"]

