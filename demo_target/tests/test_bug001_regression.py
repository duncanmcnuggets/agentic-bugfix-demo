"""Regression coverage for explicit falsy startup configuration values."""

from config_service.bootstrap import build_startup_plan


def test_explicit_falsy_values_are_preserved() -> None:
    plan = build_startup_plan({"max_retries": 0, "feature_enabled": False})

    assert plan.retry_budget == 0
    assert plan.feature_mode == "off"
