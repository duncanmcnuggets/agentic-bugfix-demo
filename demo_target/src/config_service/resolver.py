"""Primitive override resolution."""

from typing import TypeVar

T = TypeVar("T")


def resolve_override(override: T | None, default: T) -> T:
    """Return an explicit override, or the default when no override is supplied."""
    return default if override is None else override
