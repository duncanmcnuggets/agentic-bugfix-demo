"""Load and validate project configuration values."""

from collections.abc import Mapping

from config_service.models import RuntimeConfig
from config_service.resolver import resolve_override

DEFAULT_MAX_RETRIES = 3
DEFAULT_FEATURE_ENABLED = True


def _optional_retry_count(config: Mapping[str, object]) -> int | None:
    value = config.get("max_retries")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_retries must be an integer or None")
    return value


def _optional_feature_flag(config: Mapping[str, object]) -> bool | None:
    value = config.get("feature_enabled")
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError("feature_enabled must be a boolean or None")
    return value


def load_runtime_config(project_config: Mapping[str, object]) -> RuntimeConfig:
    """Resolve project configuration into typed runtime values."""

    return RuntimeConfig(
        max_retries=resolve_override(
            _optional_retry_count(project_config), DEFAULT_MAX_RETRIES
        ),
        feature_enabled=resolve_override(
            _optional_feature_flag(project_config), DEFAULT_FEATURE_ENABLED
        ),
    )

