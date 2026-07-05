"""Runtime settings for disclosure_anchor.

Only this module should read process environment for service configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


SENTINEL_NAME = "MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL"


class Settings(BaseSettings):
    """Environment-backed service settings."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    disclosure_data_root: Path = Field(
        validation_alias=AliasChoices("DISCLOSURE_DATA_ROOT", "disclosure_data_root")
    )
    disclosure_shared_root: Path = Field(
        validation_alias=AliasChoices("DISCLOSURE_SHARED_ROOT", "disclosure_shared_root")
    )
    disclosure_runtime_root: Path = Field(
        validation_alias=AliasChoices("DISCLOSURE_RUNTIME_ROOT", "disclosure_runtime_root")
    )
    database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    disclosure_reader_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_READER_DATABASE_URL", "disclosure_reader_database_url"
        ),
    )
    disclosure_admin_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_ADMIN_DATABASE_URL", "disclosure_admin_database_url"
        ),
    )
    disclosure_migration_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_MIGRATION_DATABASE_URL", "disclosure_migration_database_url"
        ),
    )
    mineru_model_cache: Path = Field(
        validation_alias=AliasChoices("MINERU_MODEL_CACHE", "mineru_model_cache")
    )
    hf_home: Path = Field(validation_alias=AliasChoices("HF_HOME", "hf_home"))
    modelscope_cache: Path = Field(
        validation_alias=AliasChoices("MODELSCOPE_CACHE", "modelscope_cache")
    )
    disclosure_parse_timeout_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices(
            "DISCLOSURE_PARSE_TIMEOUT_SECONDS",
            "disclosure_parse_timeout_seconds",
        ),
    )
    disclosure_mineru_bin: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices("DISCLOSURE_MINERU_BIN", "disclosure_mineru_bin"),
    )
    cninfo_access_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("CNINFO_ACCESS_KEY", "cninfo_access_key"),
    )
    cninfo_access_secret: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("CNINFO_ACCESS_SECRET", "cninfo_access_secret"),
    )
    cninfo_access_token: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("CNINFO_ACCESS_TOKEN", "cninfo_access_token"),
    )

    @property
    def agent_system_root(self) -> Path:
        """Return the multi-service agent_system root inferred from data root."""

        if self.disclosure_data_root.parent.name == "services":
            return self.disclosure_data_root.parent.parent
        return self.disclosure_data_root.parent

    @property
    def sentinel_path(self) -> Path:
        return self.agent_system_root / SENTINEL_NAME

    @property
    def model_cache_paths(self) -> tuple[Path, Path, Path]:
        return (self.mineru_model_cache, self.hf_home, self.modelscope_cache)


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings 从环境变量填充


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
