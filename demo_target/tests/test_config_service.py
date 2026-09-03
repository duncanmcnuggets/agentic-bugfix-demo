"""Public behavior tests that intentionally omit BUG-001's falsy cases."""

import pytest
from config_service.bootstrap import build_startup_plan


def test_missing_values_use_defaults() -> None:
    plan = build_startup_plan({})
    assert plan.retry_budget == 3
    assert plan.feature_mode == "on"


def test_none_values_use_defaults() -> None:
    plan = build_startup_plan({"max_retries": None, "feature_enabled": None})
    assert plan.retry_budget == 3
    assert plan.feature_mode == "on"


def test_truthy_overrides_are_preserved() -> None:
    plan = build_startup_plan({"max_retries": 5, "feature_enabled": True})
    assert plan.retry_budget == 5
    assert plan.feature_mode == "on"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"max_retries": "5"}, "max_retries"),
        ({"feature_enabled": 1}, "feature_enabled"),
    ],
)
def test_invalid_types_are_rejected(config: dict[str, object], message: str) -> None:
    with pytest.raises(TypeError, match=message):
        build_startup_plan(config)
