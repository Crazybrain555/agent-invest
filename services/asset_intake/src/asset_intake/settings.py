"""Runtime settings for asset_intake.

Only this module should read process environment for service configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_ROOT = Path("/Volumes/AgentSSD/agent_system/services/asset_intake")


class Settings(BaseSettings):
    """Environment-backed service settings."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    data_root: Path = Field(
        default=DEFAULT_DATA_ROOT,
        validation_alias=AliasChoices("ASSET_INTAKE_DATA_ROOT", "data_root"),
    )
    database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("ASSET_INTAKE_DATABASE_URL", "database_url"),
    )
    migration_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASSET_INTAKE_MIGRATION_DATABASE_URL", "migration_database_url"
        ),
    )
    reader_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASSET_INTAKE_READER_DATABASE_URL", "reader_database_url"
        ),
    )
    admin_database_url: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASSET_INTAKE_ADMIN_DATABASE_URL", "admin_database_url"
        ),
    )
    tushare_token: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("TUSHARE_TOKEN", "tushare_token"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
