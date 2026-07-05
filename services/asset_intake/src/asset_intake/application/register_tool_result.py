"""tool_result registration path (protocol §3.7, framework v1.2 P6).

Registers already-obtained web/MCP/agent tool output as
data_asset(asset_kind=tool_result). Same tables, same idempotency tri-state and
outbox semantics as the dataset registrar; no provider port involved — the
submission carries its own provenance. Every returned item MUST have a
non-empty ``locator`` (URL / citation / result id); missing locators fail fast.

Future MCP/web-search sources get their own source catalog under
registry/providers (F12 同一原则); this entry point stays the single writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from asset_intake.application.register_dataset import (
    RegistrationOutcome,
    _canonical_json,
    _new_id,
    _sha256,
)
from asset_intake.db.models import data_asset, outbox_event, processing_run, source_access
from asset_intake.providers.port import ScopeHints
from envelope_kernel import (
    AssetKind,
    PayloadKind,
    SourceTier,
    TraceLevel,
    build_asset_uri,
    validate_combination,
    validate_envelope,
)

RUN_KIND = "tool_result_register"
SERVICE_NAME = "asset_intake"


@dataclass(frozen=True)
class ToolResultSubmission:
    provider: str                       # harness/来源族,如 web_search / mcp
    tool: str                           # 工具名(§3.7 必填)
    adapter: str
    adapter_version: str
    payload_kind: PayloadKind           # search_result / api_response / page_snippet
    query: dict[str, Any]               # 查询/调用参数(§3.7 必填)
    returned_items: list[dict[str, Any]]
    source_tier: SourceTier = SourceTier.TIER_2
    trace_level: TraceLevel = TraceLevel.G2
    locator: str | None = None
    raw_asset_ref: str | None = None
    scope: ScopeHints = field(default_factory=ScopeHints)


def register_tool_result(engine: Engine, submission: ToolResultSubmission) -> RegistrationOutcome:
    validate_combination(AssetKind.TOOL_RESULT, submission.payload_kind)
    missing = [i for i, item in enumerate(submission.returned_items) if not item.get("locator")]
    if missing:
        raise ValueError(
            f"tool_result items missing required 'locator' at indexes {missing} (protocol §3.7)"
        )

    observed_at = datetime.now(UTC)
    run_id = _new_id("run")
    access_id = _new_id("sa")
    params_hash = _sha256(_canonical_json(submission.query))

    with engine.begin() as conn:
        conn.execute(
            processing_run.insert().values(
                run_id=run_id,
                run_kind=RUN_KIND,
                provider=submission.provider,
                adapter=submission.adapter,
                adapter_version=submission.adapter_version,
                params={"tool": submission.tool, "query": submission.query},
                status="running",
                started_at=observed_at,
            )
        )
        conn.execute(
            source_access.insert().values(
                access_id=access_id,
                provider=submission.provider,
                adapter=submission.adapter,
                adapter_version=submission.adapter_version,
                tool=submission.tool,
                query_params=submission.query,
                query_params_hash=params_hash,
                observed_at=observed_at,
                result_status="empty" if not submission.returned_items else "ok",
                result_count=len(submission.returned_items),
                processing_run_id=run_id,
            )
        )

    if not submission.returned_items:
        with engine.begin() as conn:
            conn.execute(
                update(processing_run)
                .where(processing_run.c.run_id == run_id)
                .values(status="succeeded", finished_at=datetime.now(UTC))
            )
        return RegistrationOutcome(status="empty", run_id=run_id, access_id=access_id)

    payload = {
        "tool": submission.tool,
        "query": submission.query,
        "returned_items": submission.returned_items,
    }
    content_hash = _sha256(_canonical_json(submission.returned_items))
    semantic_key = f"tool_result|{submission.tool}|{params_hash}"
    dedup_key = _sha256(f"{semantic_key}||{content_hash}")
    asset_id = _new_id("da")

    envelope = {
        "asset_id": asset_id,
        "asset_kind": AssetKind.TOOL_RESULT,
        "payload_kind": submission.payload_kind,
        "content_hash": content_hash,
        "subject_candidates": submission.scope.subject_candidates,
        "title": submission.scope.title,
        "semantic_key": semantic_key,
        "event_time": submission.scope.event_time,
        "published_at": submission.scope.published_at,
        "report_period": submission.scope.report_period,
        "observed_at": observed_at,
        "source_ref": access_id,
        "provider": submission.provider,
        "adapter": submission.adapter,
        "tool": submission.tool,
        "query_params": submission.query,
        "source_tier": submission.source_tier,
        "trace_level": submission.trace_level,
        "locator": submission.locator,
        "raw_asset_ref": submission.raw_asset_ref,
        "producer_action_ref": run_id,
        "payload": payload,
    }
    validated = validate_envelope(envelope)

    with engine.begin() as conn:
        existing = conn.execute(
            select(data_asset.c.asset_id).where(
                data_asset.c.provider == submission.provider,
                data_asset.c.dedup_key == dedup_key,
            )
        ).first()
        if existing is not None:
            conn.execute(
                outbox_event.insert().values(
                    event_id=_new_id("ev"),
                    event_kind="observed",
                    subject_ref=build_asset_uri(
                        SERVICE_NAME, 1, AssetKind.TOOL_RESULT, existing.asset_id
                    ),
                    asset_id=existing.asset_id,
                    processing_run_id=run_id,
                    payload={"tool": submission.tool, "content_hash": content_hash},
                    occurred_at=datetime.now(UTC),
                )
            )
            conn.execute(
                update(processing_run)
                .where(processing_run.c.run_id == run_id)
                .values(status="succeeded", finished_at=datetime.now(UTC))
            )
            return RegistrationOutcome(
                status="observed", run_id=run_id, access_id=access_id, asset_id=existing.asset_id
            )

        superseded = conn.execute(
            select(data_asset.c.asset_id).where(
                data_asset.c.provider == submission.provider,
                data_asset.c.semantic_key == semantic_key,
                data_asset.c.is_active.is_(True),
            )
        ).first()
        conn.execute(
            data_asset.insert().values(
                asset_id=asset_id,
                asset_kind=validated.asset_kind,
                payload_kind=validated.payload_kind,
                contract_version=validated.contract_version,
                content_hash=content_hash,
                subject_candidates=submission.scope.subject_candidates,
                title=submission.scope.title,
                semantic_key=semantic_key,
                material_type="tool_result",
                event_time=submission.scope.event_time,
                published_at=submission.scope.published_at,
                report_period=submission.scope.report_period,
                observed_at=observed_at,
                source_access_id=access_id,
                provider=submission.provider,
                adapter=submission.adapter,
                tool=submission.tool,
                source_tier=str(submission.source_tier.value),
                trace_level=str(submission.trace_level.value),
                locator=submission.locator,
                raw_asset_ref=submission.raw_asset_ref,
                processing_run_id=run_id,
                payload=payload,
                quality_status="ok",
                is_active=True,
                dedup_key=dedup_key,
            )
        )
        superseded_id: str | None = None
        if superseded is not None:
            superseded_id = superseded.asset_id
            conn.execute(
                update(data_asset)
                .where(data_asset.c.asset_id == superseded_id)
                .values(is_active=False, superseded_by=asset_id, updated_at=datetime.now(UTC))
            )
        conn.execute(
            outbox_event.insert().values(
                event_id=_new_id("ev"),
                event_kind="materialized",
                subject_ref=build_asset_uri(SERVICE_NAME, 1, AssetKind.TOOL_RESULT, asset_id),
                asset_id=asset_id,
                processing_run_id=run_id,
                payload={"tool": submission.tool, "content_hash": content_hash,
                         "superseded_asset_id": superseded_id},
                occurred_at=datetime.now(UTC),
            )
        )
        conn.execute(
            update(processing_run)
            .where(processing_run.c.run_id == run_id)
            .values(status="succeeded", finished_at=datetime.now(UTC))
        )

    return RegistrationOutcome(
        status="materialized", run_id=run_id, access_id=access_id,
        asset_id=asset_id, superseded_asset_id=superseded_id,
    )
