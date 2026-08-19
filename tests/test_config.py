import pytest

from s7.config import ConfigError, Settings


def test_settings_load_with_no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXTEND_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key is None
    assert settings.extend_api_key is None


def test_require_raises_config_error_naming_stage_and_field() -> None:
    settings = Settings(_env_file=None, anthropic_api_key=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="s5_contract.*anthropic_api_key"):
        settings.require("anthropic_api_key", stage="s5_contract")


def test_require_returns_value_when_set() -> None:
    settings = Settings(_env_file=None, anthropic_api_key="sk-test")  # type: ignore[call-arg]
    assert settings.require("anthropic_api_key", stage="s5_contract") == "sk-test"
