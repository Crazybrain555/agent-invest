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
    # MinerU 3.4 backend vocabulary: pipeline | vlm-engine | hybrid-engine |
    # vlm-http-client | hybrid-http-client. The *-http-client backends need
    # DISCLOSURE_MINERU_SERVER_URL pointing at a mineru-openai-server
    # (GPU box) and shift VLM inference off this machine.
    disclosure_mineru_backend: str = Field(
        default="pipeline",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_BACKEND", "disclosure_mineru_backend"
        ),
    )
    disclosure_mineru_server_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_SERVER_URL", "disclosure_mineru_server_url"
        ),
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
    cninfo_max_qps: float = Field(
        default=1.0,
        gt=0,
        validation_alias=AliasChoices("CNINFO_MAX_QPS", "cninfo_max_qps"),
    )
    cninfo_max_retries: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices("CNINFO_MAX_RETRIES", "cninfo_max_retries"),
    )
    cninfo_overlap_days: int = Field(
        default=7,
        ge=0,
        validation_alias=AliasChoices("CNINFO_OVERLAP_DAYS", "cninfo_overlap_days"),
    )
    # First-sync historical backfill (user decision 2026-07-06: 三年是底线 —
    # three years of financial reports plus the important announcements).
    disclosure_initial_lookback_days: int = Field(
        default=1095,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_INITIAL_LOOKBACK_DAYS", "disclosure_initial_lookback_days"
        ),
    )
    # Parse-layer scope: 'core' = every non-other filing_type + 'other' docs
    # matching config/parse_scope.json prefixes; 'all' = parse everything
    # (user decision 2026-07-06: 全量登记+分层解析).
    # /v1/admin/* are unauthenticated local-ops write endpoints; production
    # deployments must keep them off the L2-facing app (round8 audit blocker).
    disclosure_enable_admin_api: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "DISCLOSURE_ENABLE_ADMIN_API", "disclosure_enable_admin_api"
        ),
    )
    # Backfill backpressure: when the pending-download queue exceeds this cap,
    # never-synced companies are deferred to a later round (batched intake).
    disclosure_backfill_max_pending_downloads: int = Field(
        default=2000,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS",
            "disclosure_backfill_max_pending_downloads",
        ),
    )
    # Processing policy (round21): the ONE scope file — process classes are
    # downloaded AND parsed, everything else registers metadata only.
    # Per-company override lives in watchlist process_classes (cascade).
    disclosure_processing_policy_path: Path = Field(
        default=Path("config/processing_policy.json"),
        validation_alias=AliasChoices(
            "DISCLOSURE_PROCESSING_POLICY", "disclosure_processing_policy_path"
        ),
    )
    cninfo_oversized_kb: int = Field(
        default=10240,
        ge=0,
        validation_alias=AliasChoices("CNINFO_OVERSIZED_KB", "cninfo_oversized_kb"),
    )
    disclosure_max_parse_retries: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_MAX_PARSE_RETRIES", "disclosure_max_parse_retries"
        ),
    )
    disclosure_max_build_retries: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_MAX_BUILD_RETRIES", "disclosure_max_build_retries"
        ),
    )
    disclosure_stale_run_threshold_seconds: int = Field(
        default=3600,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_STALE_RUN_THRESHOLD_SECONDS",
            "disclosure_stale_run_threshold_seconds",
        ),
    )
    disclosure_sync_interval_seconds: int = Field(
        default=86400,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_SYNC_INTERVAL_SECONDS", "disclosure_sync_interval_seconds"
        ),
    )
    worker_batch_sync: int = Field(
        default=5,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_SYNC", "worker_batch_sync"),
    )
    worker_batch_download: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_DOWNLOAD", "worker_batch_download"),
    )
    worker_batch_parse: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_PARSE", "worker_batch_parse"),
    )
    # Parallel parse chains per round (1 = serial). Meant for the
    # *-http-client backends where the GPU server absorbs concurrency;
    # capped to keep local memory/subprocess fan-out bounded.
    worker_parse_concurrency: int = Field(
        default=1,
        ge=1,
        le=16,
        validation_alias=AliasChoices(
            "WORKER_PARSE_CONCURRENCY", "worker_parse_concurrency"
        ),
    )
    worker_batch_build: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_BUILD", "worker_batch_build"),
    )
    worker_batch_publish: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_PUBLISH", "worker_batch_publish"),
    )
    worker_loop_interval_seconds: int = Field(
        default=900,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_LOOP_INTERVAL_SECONDS", "worker_loop_interval_seconds"
        ),
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
