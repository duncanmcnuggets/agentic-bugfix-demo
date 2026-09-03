"""Reproduce BUG-001 through the target application's public entry point."""

from __future__ import annotations

import sys
from pathlib import Path

TARGET_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(TARGET_SRC))

from config_service.bootstrap import build_startup_plan  # noqa: E402


def main() -> int:
    plan = build_startup_plan({"max_retries": 0, "feature_enabled": False})
    expected = {"retry_budget": 0, "feature_mode": "off"}
    actual = {"retry_budget": plan.retry_budget, "feature_mode": plan.feature_mode}

    print(f"Expected: {expected}")
    print(f"Actual:   {actual}")
    if actual != expected:
        print("BUG-001 reproduced")
        return 1
    print("BUG-001 not reproduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

