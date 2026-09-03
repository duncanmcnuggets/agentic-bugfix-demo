"""Trusted external oracle for BUG-001."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-src", type=Path, required=True)
    return parser


def _case(name: str, actual: Any, expected: Any) -> bool:
    passed = bool(actual == expected)
    print(f"{name}: {'PASS' if passed else 'FAIL'}; expected={expected!r}; actual={actual!r}")
    return passed


def run(target_src: Path) -> int:
    resolved = target_src.resolve()
    if not resolved.is_dir():
        print(f"target source does not exist: {resolved}")
        return 1
    sys.path.insert(0, str(resolved))
    from config_service.bootstrap import build_startup_plan

    build: Callable[[dict[str, object]], Any] = build_startup_plan
    default = build({})
    none_values = build({"max_retries": None, "feature_enabled": None})
    zero = build({"max_retries": 0})
    false_value = build({"feature_enabled": False})
    truthy = build({"max_retries": 7, "feature_enabled": True})
    results = [
        _case("missing defaults / retries", default.retry_budget, 3),
        _case("missing defaults / feature", default.feature_mode, "on"),
        _case("None defaults / retries", none_values.retry_budget, 3),
        _case("None defaults / feature", none_values.feature_mode, "on"),
        _case("explicit zero", zero.retry_budget, 0),
        _case("explicit false", false_value.feature_mode, "off"),
        _case("truthy retries", truthy.retry_budget, 7),
        _case("truthy feature", truthy.feature_mode, "on"),
    ]
    return 0 if all(results) else 1


def main() -> int:
    return run(_parser().parse_args().target_src)


if __name__ == "__main__":
    raise SystemExit(main())
