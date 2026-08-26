"""Runtime settings for disclosure_anchor.

Only this module should read process environment for service configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SENTINEL_NAME = "MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL"


def _validate_mineru_endpoint(
    label: str,
    value: str,
    *,
    allowed_paths: frozenset[str],
) -> None:
    """Reject ambiguous or credential-bearing operational endpoints."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label} must be an HTTP URL without credentials, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    if port is None:
        raise ValueError(f"{label} must pin an explicit port")
    if parsed.path not in allowed_paths:
        raise ValueError(f"{label} has an unsupported path")


class SemanticProviderConfig(BaseModel):
    """Secret-free provider composition parsed from one closed JSON value."""

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]{1,63}$")
    kind: Literal["codex_cli", "claude_cli"]
    provider: Literal["openai", "anthropic"]
    executable: Path
    canonical_model: str = Field(min_length=1, pattern=r"^\S+$")
    profile: Literal["low", "medium", "high"] = "low"
    timeout_seconds: int = Field(default=600, ge=1)
    max_concurrency: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def _validate_adapter_identity(self) -> "SemanticProviderConfig":
        if self.kind == "codex_cli" and self.provider != "openai":
            raise ValueError("codex_cli semantic provider must be openai")
        if self.kind == "claude_cli":
            if self.provider != "anthropic":
                raise ValueError("claude_cli semantic provider must be anthropic")
            if self.canonical_model != "claude-sonnet-5":
                raise ValueError("claude_cli requires canonical_model=claude-sonnet-5")
        return self


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
        validation_alias=AliasChoices(
            "DISCLOSURE_SHARED_ROOT", "disclosure_shared_root"
        )
    )
    disclosure_runtime_root: Path = Field(
        validation_alias=AliasChoices(
            "DISCLOSURE_RUNTIME_ROOT", "disclosure_runtime_root"
        )
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
    # Keep this as an integer range instead of Literal[16]: pydantic-settings
    # receives environment values as strings and does not coerce an integer
    # Literal, which would reject the valid `MINERU_PROCESSING_WINDOW_SIZE=16`.
    mineru_processing_window_size: int = Field(
        default=16,
        ge=16,
        le=16,
        validation_alias=AliasChoices(
            "MINERU_PROCESSING_WINDOW_SIZE",
            "mineru_processing_window_size",
        ),
    )
    hf_home: Path = Field(validation_alias=AliasChoices("HF_HOME", "hf_home"))
    modelscope_cache: Path = Field(
        validation_alias=AliasChoices("MODELSCOPE_CACHE", "modelscope_cache")
    )
    # These legacy-named values now describe an expected-duration envelope,
    # not a correctness deadline. Page count is a useful scheduling/alerting
    # proxy, but OCR/table/image complexity makes it unsafe to kill a healthy
    # whole-document parse merely because that estimate elapsed.
    disclosure_parse_timeout_seconds: int = Field(
        default=3600,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_PARSE_TIMEOUT_SECONDS",
            "disclosure_parse_timeout_seconds",
        ),
    )
    disclosure_parse_timeout_per_page_seconds: int = Field(
        default=12,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_PARSE_TIMEOUT_PER_PAGE_SECONDS",
            "disclosure_parse_timeout_per_page_seconds",
        ),
    )
    disclosure_parse_timeout_max_seconds: int = Field(
        default=14400,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_PARSE_TIMEOUT_MAX_SECONDS",
            "disclosure_parse_timeout_max_seconds",
        ),
    )
    # Last-resort process-liveness guard. MinerU 3.4 has no durable page-level
    # checkpoint or progress API, so normal long documents must be allowed to
    # finish. This bound is deliberately remote from the expected envelope:
    # it protects against a child that remains alive but never returns.
    disclosure_parse_runaway_timeout_seconds: int = Field(
        default=86400,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_PARSE_RUNAWAY_TIMEOUT_SECONDS",
            "disclosure_parse_runaway_timeout_seconds",
        ),
    )
    disclosure_mineru_bin: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices("DISCLOSURE_MINERU_BIN", "disclosure_mineru_bin"),
    )
    # Sole writer contract: exact MinerU 3.4.4 Hybrid-medium over the remote
    # OpenAI-compatible backend. Alternative lanes are DB-free diagnostics.
    disclosure_mineru_backend: Literal["hybrid-http-client"] = Field(
        default="hybrid-http-client",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_BACKEND", "disclosure_mineru_backend"
        ),
    )
    # Three different network namespaces participate in the fixed MinerU API
    # topology.  They are intentionally separate: a Mac-reachable endpoint is
    # not evidence that the Windows orchestrator can resolve the same URL.
    disclosure_mineru_api_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_URL", "disclosure_mineru_api_url"
        ),
    )
    disclosure_mineru_observability_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_OBSERVABILITY_URL",
            "disclosure_mineru_observability_url",
        ),
    )
    disclosure_mineru_inference_upstream_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL",
            "disclosure_mineru_inference_upstream_url",
        ),
    )
    # These are service-side limits of the attested Windows orchestrator.
    # They are not CLI fan-out controls: MinerU ignores the CLI's unknown
    # --max-concurrency option when --api-url selects an existing API.  The
    # task-slot value is machine-profile configuration, but it is accepted
    # only when the exact runtime manifest, live health and load receipts bind
    # the same value.  The 30 GiB Windows Docker profile defaults to one after
    # a real mixed annual-report run proved that three can OOM the API.
    disclosure_mineru_api_task_slots: int = Field(
        default=1,
        ge=1,
        le=3,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_TASK_SLOTS",
            "disclosure_mineru_api_task_slots",
        ),
    )
    disclosure_mineru_api_inference_concurrency: int = Field(
        default=7,
        ge=7,
        le=7,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY",
            "disclosure_mineru_api_inference_concurrency",
        ),
    )
    disclosure_mineru_api_task_retention_seconds: int = Field(
        default=600,
        ge=600,
        le=600,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_TASK_RETENTION_SECONDS",
            "disclosure_mineru_api_task_retention_seconds",
        ),
    )
    disclosure_mineru_api_cleanup_interval_seconds: int = Field(
        default=30,
        ge=30,
        le=30,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_CLEANUP_INTERVAL_SECONDS",
            "disclosure_mineru_api_cleanup_interval_seconds",
        ),
    )
    disclosure_mineru_api_drain_timeout_seconds: int = Field(
        default=86400,
        ge=60,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_API_DRAIN_TIMEOUT_SECONDS",
            "disclosure_mineru_api_drain_timeout_seconds",
        ),
    )
    # vLLM queue/KV metrics are not GPU compute utilization. Only an explicit
    # GPU exporter may populate utilization in the worker progress contract.
    # The generic URL supports DCGM on Linux and the pinned nvidia-smi exporter
    # on Windows; the older DCGM-only name remains a compatibility fallback.
    disclosure_gpu_metrics_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_GPU_METRICS_URL",
            "disclosure_gpu_metrics_url",
        ),
    )
    disclosure_dcgm_metrics_url: Optional[str] = Field(
        default=None,
        pattern=r"^https?://",
        validation_alias=AliasChoices(
            "DISCLOSURE_DCGM_METRICS_URL",
            "disclosure_dcgm_metrics_url",
        ),
    )
    disclosure_gpu_expected_uuid: Optional[str] = Field(
        default=None,
        pattern=(
            r"^(?:GPU-)?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
        ),
        validation_alias=AliasChoices(
            "DISCLOSURE_GPU_EXPECTED_UUID",
            "disclosure_gpu_expected_uuid",
        ),
    )
    worker_progress_metrics_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        validation_alias=AliasChoices(
            "WORKER_PROGRESS_METRICS_TIMEOUT_SECONDS",
            "worker_progress_metrics_timeout_seconds",
        ),
    )
    # Operator/provider-attested immutable identity for the complete remote
    # MinerU image, model set, and configuration.  The value is already a
    # digest; callers must never turn a mutable label into an apparent hash.
    disclosure_mineru_runtime_bundle_identity_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256",
            "disclosure_mineru_runtime_bundle_identity_sha256",
        ),
    )
    disclosure_mineru_smoke_receipt: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_SMOKE_RECEIPT",
            "disclosure_mineru_smoke_receipt",
        ),
    )
    disclosure_mineru_canary_cache: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_CANARY_CACHE",
            "disclosure_mineru_canary_cache",
        ),
    )
    disclosure_mineru_staged_load_receipt: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_STAGED_LOAD_RECEIPT",
            "disclosure_mineru_staged_load_receipt",
        ),
    )
    disclosure_mineru_staged_load_confirmation_receipt: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_STAGED_LOAD_CONFIRMATION_RECEIPT",
            "disclosure_mineru_staged_load_confirmation_receipt",
        ),
    )
    # Hash of the canonical evidence for the ordered, heterogeneous real-PDF
    # corpus used by both independent 4/8/16 deployment rehearsals.  This is a
    # new contract: legacy single-PDF receipt hashes are deliberately rejected.
    disclosure_mineru_staged_corpus_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_STAGED_CORPUS_SHA256",
            "disclosure_mineru_staged_corpus_sha256",
        ),
    )
    # Operator-calibrated free-memory reserve for the current Docker VM.  Zero
    # means unconfigured and is rejected by the parse-capable deployment gate.
    disclosure_mineru_docker_memory_reserve_bytes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_DOCKER_MEMORY_RESERVE_BYTES",
            "disclosure_mineru_docker_memory_reserve_bytes",
        ),
    )
    disclosure_mineru_canary_max_age_seconds: int = Field(
        default=2592000,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_CANARY_MAX_AGE_SECONDS",
            "disclosure_mineru_canary_max_age_seconds",
        ),
    )
    disclosure_mineru_live_probe_interval_seconds: int = Field(
        default=300,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_MINERU_LIVE_PROBE_INTERVAL_SECONDS",
            "disclosure_mineru_live_probe_interval_seconds",
        ),
    )
    # Unit semantic routing uses Codex only as a closed-vocabulary chooser.
    # The deterministic router owns candidates and exact-title routes; the
    # model may select candidate IDs or abstain, and Publish never calls it.
    disclosure_semantic_codex_bin: Path = Field(
        default=Path("codex"),
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_CODEX_BIN",
            "disclosure_semantic_codex_bin",
        ),
    )
    disclosure_semantic_model: str = Field(
        default="gpt-5.6-luna",
        min_length=1,
        pattern=r"^\S+$",
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_MODEL",
            "disclosure_semantic_model",
        ),
    )
    disclosure_semantic_reasoning_effort: Literal["low", "medium", "high"] = Field(
        default="low",
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_REASONING_EFFORT",
            "disclosure_semantic_reasoning_effort",
        ),
    )
    disclosure_semantic_timeout_seconds: int = Field(
        default=600,
        ge=1,
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_TIMEOUT_SECONDS",
            "disclosure_semantic_timeout_seconds",
        ),
    )
    disclosure_semantic_batch_size: int = Field(
        default=16,
        ge=1,
        le=32,
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_BATCH_SIZE",
            "disclosure_semantic_batch_size",
        ),
    )
    disclosure_semantic_providers_json: Optional[tuple[SemanticProviderConfig, ...]] = (
        Field(
            default=None,
            validation_alias=AliasChoices(
                "DISCLOSURE_SEMANTIC_PROVIDERS_JSON",
                "disclosure_semantic_providers_json",
            ),
        )
    )
    disclosure_semantic_failover_policy: Literal["availability_only.v1"] = Field(
        default="availability_only.v1",
        validation_alias=AliasChoices(
            "DISCLOSURE_SEMANTIC_FAILOVER_POLICY",
            "disclosure_semantic_failover_policy",
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
    # Worker index-sync channel. "api" is the credentialed WebAPI; "web" uses
    # the public website index (no per-API allowance) while company profiles
    # stay on the API channel — the escape hatch when the listing API's
    # account allowance walls off a large backfill.
    disclosure_sync_channel: Literal["api", "web"] = Field(
        default="api",
        validation_alias=AliasChoices(
            "DISCLOSURE_SYNC_CHANNEL", "disclosure_sync_channel"
        ),
    )
    cninfo_overlap_days: int = Field(
        default=7,
        ge=0,
        validation_alias=AliasChoices("CNINFO_OVERLAP_DAYS", "cninfo_overlap_days"),
    )
    # First-sync historical backfill.  The scale-rollout decision supersedes
    # the old three-year floor: use 3.5 years plus a small leap-day/calendar
    # buffer so a days-based provider API cannot silently undershoot 42 months.
    disclosure_initial_lookback_days: int = Field(
        default=1280,
        ge=0,
        validation_alias=AliasChoices(
            "DISCLOSURE_INITIAL_LOOKBACK_DAYS", "disclosure_initial_lookback_days"
        ),
    )
    # Parse-layer scope now comes from config/processing_policy.json
    # (loaded via load_processing_policy; the old parse_scope.json was
    # retired — comment fixed 2026-07-14, user decision 2026-07-06 stands:
    # 全量登记+分层解析).
    # /v1/admin/* are token-guarded local-ops write endpoints (user decision
    # 2026-07-14, supersedes the round8 "unauthenticated + default-off"
    # stance): enabling the admin surface additionally requires
    # DISCLOSURE_ADMIN_TOKEN — without a token the router refuses to mount
    # (fail-closed). Production deployments still keep admin off the
    # L2-facing app.
    disclosure_enable_admin_api: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "DISCLOSURE_ENABLE_ADMIN_API", "disclosure_enable_admin_api"
        ),
    )
    disclosure_admin_token: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DISCLOSURE_ADMIN_TOKEN", "disclosure_admin_token"
        ),
    )
    # Backfill backpressure: when pending-download + downloaded/pending-parse
    # work reaches this watermark, never-synced companies are deferred.  The
    # env key keeps its original name for compatibility.
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
    # Legacy env name retained for compatibility. This is now a scheduling
    # isolation threshold over the archived raw byte_count, never a parse
    # eligibility limit and never a unit assumption about CNINFO F005N.
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
    # Sync-stage time box: the stage yields to download/parse after this many
    # seconds even mid-batch, so a long first-sync sweep can never starve the
    # GPU; remaining companies stay due for the next round.
    worker_sync_stage_seconds: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices(
            "WORKER_SYNC_STAGE_SECONDS", "worker_sync_stage_seconds"
        ),
    )
    # Acquisition pump window: sync+download passes repeat inside one round
    # for up to this many seconds (each pass still bounded by the batch
    # limits and the sync time box; the pump exits early when a full pass
    # makes no successful progress). 0 restores one pass per round.
    worker_acquisition_seconds: int = Field(
        default=3600,
        ge=0,
        validation_alias=AliasChoices(
            "WORKER_ACQUISITION_SECONDS", "worker_acquisition_seconds"
        ),
    )
    worker_batch_sync: int = Field(
        default=13,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_SYNC", "worker_batch_sync"),
    )
    worker_batch_download: int = Field(
        default=50,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_DOWNLOAD", "worker_batch_download"),
    )
    worker_batch_parse: int = Field(
        default=50,
        ge=0,
        validation_alias=AliasChoices("WORKER_BATCH_PARSE", "worker_batch_parse"),
    )
    # Parallel parse chains per round (1 = serial). Meant for the
    # *-http-client backends where the GPU server absorbs concurrency;
    # capped to keep local memory/subprocess fan-out bounded (raised 8→16
    # per user decision 2026-07-17 for the vllm continuous-batching server).
    worker_parse_concurrency: int = Field(
        default=1,
        ge=1,
        le=16,
        validation_alias=AliasChoices(
            "WORKER_PARSE_CONCURRENCY", "worker_parse_concurrency"
        ),
    )
    # Local whole-document clients that may be submitted but not terminal.
    # PostgreSQL/local admission retains the backlog. MinerU has no remote
    # cancel endpoint, so this must not exceed active API task slots: otherwise
    # queued work can begin after a host-safety failure closes admission.
    # Huge/unknown PDFs are further forced to exclusive admission.
    worker_mineru_client_outstanding_window: int = Field(
        default=1,
        ge=1,
        le=16,
        validation_alias=AliasChoices(
            "WORKER_MINERU_CLIENT_OUTSTANDING_WINDOW",
            "worker_mineru_client_outstanding_window",
        ),
    )
    # One document-level slot fans out into many page/block HTTP requests
    # inside MinerU. Bound the aggregate request fan-out independently from
    # document concurrency; production divides this budget across slots.
    worker_gpu_request_budget: int = Field(
        default=7,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_GPU_REQUEST_BUDGET", "worker_gpu_request_budget"
        ),
    )
    worker_gpu_max_sequences: int = Field(
        default=128,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_GPU_MAX_SEQUENCES", "worker_gpu_max_sequences"
        ),
    )
    # When regular and heavyweight PDFs are both queued, only this many
    # heavyweight documents receive the saturated share. Idle capacity is
    # still borrowable when no regular work exists.
    worker_parse_heavy_page_threshold: int = Field(
        default=80,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_PARSE_HEAVY_PAGE_THRESHOLD",
            "worker_parse_heavy_page_threshold",
        ),
    )
    worker_parse_heavy_saturated_share: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_PARSE_HEAVY_SATURATED_SHARE",
            "worker_parse_heavy_saturated_share",
        ),
    )
    worker_parse_huge_page_threshold: int = Field(
        default=500,
        ge=2,
        validation_alias=AliasChoices(
            "WORKER_PARSE_HUGE_PAGE_THRESHOLD",
            "worker_parse_huge_page_threshold",
        ),
    )
    worker_parse_huge_saturated_share: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_PARSE_HUGE_SATURATED_SHARE",
            "worker_parse_huge_saturated_share",
        ),
    )
    worker_parse_candidate_window: int = Field(
        default=1000,
        ge=1,
        le=2000,
        validation_alias=AliasChoices(
            "WORKER_PARSE_CANDIDATE_WINDOW",
            "worker_parse_candidate_window",
        ),
    )
    worker_finalize_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        validation_alias=AliasChoices(
            "WORKER_FINALIZE_CONCURRENCY", "worker_finalize_concurrency"
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
    # Observability cadence for the resident parse dispatcher. This rotates
    # an immutable report snapshot while parse/finalize pools keep running;
    # it is never an admission deadline or a document timeout.
    worker_report_interval_seconds: int = Field(
        default=300,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_REPORT_INTERVAL_SECONDS",
            "worker_report_interval_seconds",
        ),
    )
    worker_loop_interval_seconds: int = Field(
        default=900,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_LOOP_INTERVAL_SECONDS", "worker_loop_interval_seconds"
        ),
    )
    worker_loop_max_interval_seconds: int = Field(
        default=1800,
        ge=1,
        validation_alias=AliasChoices(
            "WORKER_LOOP_MAX_INTERVAL_SECONDS",
            "worker_loop_max_interval_seconds",
        ),
    )
    # Wedge watchdog: exit loudly (launchd relaunches) when the coordinator
    # stops reporting ownership progress for this long. Healthy parse futures
    # heartbeat every 30 seconds, independently of their wall-clock duration;
    # the separate extreme parse lease handles live-but-never-return futures.
    # 0 disables.
    worker_wedge_timeout_seconds: int = Field(
        default=2700,
        ge=0,
        validation_alias=AliasChoices(
            "WORKER_WEDGE_TIMEOUT_SECONDS", "worker_wedge_timeout_seconds"
        ),
    )

    @model_validator(mode="after")
    def _validate_parallel_parser_backend(self) -> "Settings":
        if (
            self.disclosure_gpu_metrics_url is not None
            and self.disclosure_dcgm_metrics_url is not None
            and self.disclosure_gpu_metrics_url != self.disclosure_dcgm_metrics_url
        ):
            raise ValueError(
                "DISCLOSURE_GPU_METRICS_URL and legacy "
                "DISCLOSURE_DCGM_METRICS_URL disagree"
            )
        if self.disclosure_gpu_metrics_url is not None:
            gpu_metrics = urlsplit(self.disclosure_gpu_metrics_url)
            if (
                gpu_metrics.scheme != "http"
                or gpu_metrics.hostname != "127.0.0.1"
                or gpu_metrics.port != 30004
                or gpu_metrics.path != "/metrics"
                or gpu_metrics.username is not None
                or gpu_metrics.password is not None
                or gpu_metrics.query
                or gpu_metrics.fragment
            ):
                raise ValueError(
                    "DISCLOSURE_GPU_METRICS_URL must be the audited "
                    "http://127.0.0.1:30004/metrics SSH forward"
                )
        mineru_urls = {
            "DISCLOSURE_MINERU_API_URL": self.disclosure_mineru_api_url,
            "DISCLOSURE_MINERU_OBSERVABILITY_URL": (
                self.disclosure_mineru_observability_url
            ),
            "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": (
                self.disclosure_mineru_inference_upstream_url
            ),
        }
        configured_url_count = sum(value is not None for value in mineru_urls.values())
        if configured_url_count not in {0, len(mineru_urls)}:
            raise ValueError("MinerU fixed-API topology requires all three URLs")
        endpoint_paths = {
            "DISCLOSURE_MINERU_API_URL": frozenset({"", "/"}),
            "DISCLOSURE_MINERU_OBSERVABILITY_URL": frozenset({"/v1", "/v1/"}),
            "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": frozenset({"/v1", "/v1/"}),
        }
        for label, value in mineru_urls.items():
            if value is not None:
                _validate_mineru_endpoint(
                    label,
                    value,
                    allowed_paths=endpoint_paths[label],
                )
        if self.disclosure_mineru_api_url is not None:
            api_host = (urlsplit(self.disclosure_mineru_api_url).hostname or "").lower()
            if api_host != "127.0.0.1":
                raise ValueError(
                    "DISCLOSURE_MINERU_API_URL must use a loopback SSH forward"
                )
        if self.disclosure_mineru_observability_url is not None:
            observability_host = (
                urlsplit(self.disclosure_mineru_observability_url).hostname or ""
            ).lower()
            if observability_host != "127.0.0.1":
                raise ValueError(
                    "DISCLOSURE_MINERU_OBSERVABILITY_URL must use a loopback SSH forward"
                )
        if self.disclosure_mineru_inference_upstream_url is not None:
            upstream_host = (
                urlsplit(self.disclosure_mineru_inference_upstream_url).hostname or ""
            ).lower()
            if (
                not upstream_host
                or "." in upstream_host
                or upstream_host in {"localhost", "host.docker.internal"}
                or not all(
                    character.islower() or character.isdigit() or character == "-"
                    for character in upstream_host
                )
            ):
                raise ValueError(
                    "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL must use a "
                    "single-label internal Docker service name"
                )
        if (
            self.disclosure_parse_timeout_max_seconds
            < self.disclosure_parse_timeout_seconds
        ):
            raise ValueError(
                "DISCLOSURE_PARSE_TIMEOUT_MAX_SECONDS must be greater than "
                "or equal to DISCLOSURE_PARSE_TIMEOUT_SECONDS"
            )
        if (
            self.disclosure_parse_runaway_timeout_seconds
            < self.disclosure_parse_timeout_max_seconds
        ):
            raise ValueError(
                "DISCLOSURE_PARSE_RUNAWAY_TIMEOUT_SECONDS must be greater "
                "than or equal to DISCLOSURE_PARSE_TIMEOUT_MAX_SECONDS"
            )
        if self.worker_gpu_request_budget > self.worker_gpu_max_sequences:
            raise ValueError(
                "WORKER_GPU_REQUEST_BUDGET must not exceed WORKER_GPU_MAX_SEQUENCES"
            )
        if (
            self.worker_gpu_request_budget
            != self.mineru_effective_inference_request_upper_bound
        ):
            raise ValueError(
                "WORKER_GPU_REQUEST_BUDGET must equal the attested "
                "MinerU API task-slots x inference-concurrency envelope"
            )
        if (
            self.worker_mineru_client_outstanding_window
            > self.disclosure_mineru_api_task_slots
        ):
            raise ValueError(
                "WORKER_MINERU_CLIENT_OUTSTANDING_WINDOW must not exceed "
                "the attested MinerU API task slots while remote cancellation "
                "is unavailable"
            )
        if self.worker_parse_concurrency > 1:
            if not self.disclosure_mineru_backend.endswith("-http-client"):
                raise ValueError(
                    "WORKER_PARSE_CONCURRENCY > 1 requires a MinerU "
                    "*-http-client backend; local parallel MinerU is unsafe"
                )
            if configured_url_count != len(mineru_urls):
                raise ValueError(
                    "WORKER_PARSE_CONCURRENCY > 1 requires the complete "
                    "MinerU fixed-API topology"
                )
        if (
            self.worker_parse_huge_page_threshold
            <= self.worker_parse_heavy_page_threshold
        ):
            raise ValueError(
                "WORKER_PARSE_HUGE_PAGE_THRESHOLD must exceed "
                "WORKER_PARSE_HEAVY_PAGE_THRESHOLD"
            )
        if self.worker_parse_candidate_window < self.worker_parse_concurrency:
            raise ValueError(
                "WORKER_PARSE_CANDIDATE_WINDOW must be at least "
                "WORKER_PARSE_CONCURRENCY"
            )
        return self

    @property
    def agent_system_root(self) -> Path:
        """Return the multi-service agent_system root inferred from data root."""

        if self.disclosure_data_root.parent.name == "services":
            return self.disclosure_data_root.parent.parent
        return self.disclosure_data_root.parent

    @property
    def mineru_http_request_concurrency(self) -> int:
        """Attested service-side VLM cap for one active API task."""

        return self.disclosure_mineru_api_inference_concurrency

    @property
    def mineru_effective_inference_request_upper_bound(self) -> int:
        """Conservative active request envelope; submitted documents may queue."""

        return (
            self.disclosure_mineru_api_task_slots
            * self.disclosure_mineru_api_inference_concurrency
        )

    @property
    def semantic_provider_configs(self) -> tuple[SemanticProviderConfig, ...]:
        """Return the explicit chain or the subscription-CLI default chain."""

        if self.disclosure_semantic_providers_json is not None:
            if not self.disclosure_semantic_providers_json:
                raise ValueError("DISCLOSURE_SEMANTIC_PROVIDERS_JSON cannot be empty")
            provider_ids = tuple(
                item.id for item in self.disclosure_semantic_providers_json
            )
            if len(provider_ids) != len(set(provider_ids)):
                raise ValueError("semantic provider configuration repeats an id")
            return self.disclosure_semantic_providers_json
        return (
            SemanticProviderConfig(
                id="luna-primary",
                kind="codex_cli",
                provider="openai",
                executable=self.disclosure_semantic_codex_bin,
                canonical_model=self.disclosure_semantic_model,
                profile=self.disclosure_semantic_reasoning_effort,
                timeout_seconds=self.disclosure_semantic_timeout_seconds,
                max_concurrency=1,
            ),
            SemanticProviderConfig(
                id="sonnet-backup",
                kind="claude_cli",
                provider="anthropic",
                executable=Path("claude"),
                canonical_model="claude-sonnet-5",
                profile="low",
                timeout_seconds=self.disclosure_semantic_timeout_seconds,
                max_concurrency=1,
            ),
        )

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
