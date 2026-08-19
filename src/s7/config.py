"""Settings loaded from the environment. No model strings or credentials belong
anywhere else in the codebase — every stage and provider reads them from here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised when a stage needs a key or setting that was never provided.

    Caught at the CLI/UI boundary and shown verbatim — the message must name
    the missing setting and the stage that needed it: the app has to start
    and run S0-S1 with no keys at all, then fail gracefully, one stage at a
    time, as each key is actually needed.
    """


class ModelSpec(BaseModel):
    """A provider + model string. Never hardcode either in a stage module."""

    provider: str  # "anthropic" | "openai"
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # --- Extend: parsing (S2) and classification (S3) only ---
    # No classifier processor id/version here -- S3 sends the taxonomy
    # (s7.taxonomy) as an inline classify config on every call rather than
    # referencing a saved Extend classifier, so this repo works from a
    # fresh clone with nothing to provision. See providers/extend.py's
    # create_classify_run docstring for why.
    extend_api_key: str | None = Field(default=None, validation_alias="EXTEND_API_KEY")
    extend_rate_limit_per_sec: float = Field(
        default=5.0, validation_alias="S7_EXTEND_RATE_LIMIT_PER_SEC"
    )

    # --- LLM providers ---
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-5", validation_alias="S7_ANTHROPIC_MODEL"
    )
    openai_model: str = Field(default="gpt-5", validation_alias="S7_OPENAI_MODEL")

    # --- Article / supplement discovery ---
    unpaywall_email: str | None = Field(default=None, validation_alias="UNPAYWALL_EMAIL")

    # --- Storage ---
    data_dir: Path = Field(
        default=Path("./data"),
        validation_alias=AliasChoices("S7_DATA_DIR", "data_dir"),
    )
    db_path: Path = Field(
        default=Path("./data/s7.db"),
        validation_alias=AliasChoices("S7_DB_PATH", "db_path"),
    )

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def parsed_raw_dir(self) -> Path:
        """Verbatim Extend responses, content-addressed alongside the normalized form."""
        return self.data_dir / "parsed_raw"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def quarantine_dir(self) -> Path:
        """needs_review records -- published separately from the main
        dataset, never mixed in.
        """
        return self.data_dir / "quarantine"

    @property
    def data_dictionary_path(self) -> Path:
        return self.data_dir / "data_dictionary.md"

    @property
    def coverage_report_path(self) -> Path:
        return self.data_dir / "coverage_report.json"

    @property
    def anthropic_spec(self) -> ModelSpec:
        return ModelSpec(provider="anthropic", model=self.anthropic_model)

    @property
    def openai_spec(self) -> ModelSpec:
        return ModelSpec(provider="openai", model=self.openai_model)

    def require(self, field_name: str, *, stage: str) -> str:
        """Return a required setting, or raise a ConfigError naming both the
        missing setting and the stage that needed it.
        """
        value: str | None = getattr(self, field_name, None)
        if not value:
            raise ConfigError(
                f"{stage} needs `{field_name}` but it is not set. "
                f"Add it to .env (see .env.example) and try again."
            )
        return value

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_raw_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
