from __future__ import annotations

import importlib

import pytest

from bugfixer.config import DEFAULT_MODEL, Settings, load_settings


def test_import_does_not_require_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import bugfixer.config as config

    importlib.reload(config)


def test_load_settings_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import bugfixer.config as config

    with pytest.raises(config.ConfigurationError, match="not configured"):
        config.load_settings()


def test_load_settings_returns_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    settings = load_settings()
    assert settings.model == DEFAULT_MODEL
    assert "key" not in settings.__dataclass_fields__


def test_turn_budgets_are_role_specific_and_bounded() -> None:
    settings = Settings(model="test-model")

    assert settings.turn_budget_for("explorer") == 12
    assert settings.turn_budget_for("test_writer") == 8
    assert settings.turn_budget_for("fixer") == 6
    assert settings.turn_budget_for("reviewer") == 2
    with pytest.raises(ValueError, match="Unknown agent role"):
        settings.turn_budget_for("unknown")
