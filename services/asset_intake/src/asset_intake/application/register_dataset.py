"""Deterministic dataset_snapshot registrar (framework v1.2 §2, §6).

The only writer of processing_run / source_access / data_asset / outbox_event.
Adapters never touch the DB; LLMs never enter this path. Idempotency tri-state:

    same provider + semantic_key + content_hash  -> observed (no new asset)
    same provider + semantic_key, new content    -> materialized + supersede old active
    provider error                               -> source_access(error), no asset
    empty result                                 -> source_access(empty), no asset (§3.9)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from asset_intake.db.models import data_asset, outbox_event, processing_run, source_access
from asset_intake.providers.port import DatasetProvider, DatasetRequest, ProviderError
from asset_intake.providers.registry import DatasetEntry, validate_request
from envelope_kernel import build_asset_uri, validate_envelope
from envelope_kernel.kinds import AssetKind

RUN_KIND = "dataset_fetch"
SERVICE_NAME = "asset_intake"


@dataclass(frozen=True)
class RegistrationOutcome:
    status: str  # materialized | observed | empty | error
    run_id: str
    access_id: str | None = None
    asset_id: str | None = None
    superseded_asset_id: str | None = None
    error: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compute_semantic_key(entry: DatasetEntry, params: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in entry.dedup.semantic_key_fields:
        if name == "dataset_key":
            parts.append(entry.dataset_key)
        else:
            parts.append(f"{name}={params.get(name)}")
    return "|".join(parts)


def compute_content_hash(entry: DatasetEntry, records: list[dict[str, Any]]) -> str:
    spec = entry.dedup.content_hash
    projected = [
        {name: record.get(name) for name in spec.include_fields if name in record}
        for record in records
    ]
    projected.sort(key=lambda r: tuple(str(r.get(k)) for k in spec.sort_by))
    return _sha256(_canonical_json(projected))


def register_dataset_snapshot(
    engine: Engine,
    entry: DatasetEntry,
    provider: DatasetProvider,
    request: DatasetRequest,
) -> RegistrationOutcome:
    params = validate_request(entry, request.query_params)
    normalized_request = DatasetRequest(dataset_key=entry.dataset_key, query_params=params)
    params_hash = _sha256(_canonical_json(params))
    run_id = _new_id("run")
    observed_at = datetime.now(UTC)

    with engine.begin() as conn:
        conn.execute(
            processing_run.insert().values(
                run_id=run_id,
                run_kind=RUN_KIND,
                provider=provider.provider_name,
                adapter=provider.adapter_name,
                adapter_version=provider.adapter_version,
                params={"dataset_key": entry.dataset_key, "query_params": params},
                status="running",
                started_at=observed_at,
            )
        )

    access_id = _new_id("sa")
    dataset_key_column = entry.dataset_key

    try:
        result = provider.fetch(normalized_request)
    except ProviderError as exc:
        with engine.begin() as conn:
            conn.execute(
                source_access.insert().values(
                    access_id=access_id,
                    provider=provider.provider_name,
                    adapter=provider.adapter_name,
                    adapter_version=provider.adapter_version,
                    dataset_key=dataset_key_column,
                    query_params=params,
                    query_params_hash=params_hash,
                    observed_at=observed_at,
                    result_status="error",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    processing_run_id=run_id,
                )
            )
            conn.execute(
                update(processing_run)
                .where(processing_run.c.run_id == run_id)
                .values(status="failed", finished_at=datetime.now(UTC),
                        error={"type": type(exc).__name__, "message": str(exc)})
            )
        return RegistrationOutcome(status="error", run_id=run_id, access_id=access_id, error=str(exc))

    result_status = "empty" if not result.records else "ok"
    with engine.begin() as conn:
        conn.execute(
            source_access.insert().values(
                access_id=access_id,
                provider=provider.provider_name,
                adapter=provider.adapter_name,
                adapter_version=provider.adapter_version,
                dataset_key=dataset_key_column,
                query_params=params,
                query_params_hash=params_hash,
                provider_as_of=result.provider_as_of,
                observed_at=observed_at,
                result_status=result_status,
                result_count=len(result.records),
                processing_run_id=run_id,
            )
        )

    if result_status == "empty":
        with engine.begin() as conn:
            conn.execute(
                update(processing_run)
                .where(processing_run.c.run_id == run_id)
                .values(status="succeeded", finished_at=datetime.now(UTC))
            )
        return RegistrationOutcome(status="empty", run_id=run_id, access_id=access_id)

    semantic_key = compute_semantic_key(entry, params)
    content_hash = compute_content_hash(entry, result.records)
    dedup_key = _sha256(f"{semantic_key}||{content_hash}")
    payload = {"records": result.records, "returned_fields": result.returned_fields}

    subject_candidates: list[str] | None = result.scope.subject_candidates
    if subject_candidates is None:
        derived = [
            f"{spec.subject_kind}:{params[spec.field]}"
            for spec in entry.semantic_contract.subject_semantics.subject_candidates_from
            if spec.field in params
        ]
        subject_candidates = derived or None

    asset_id = _new_id("da")
    envelope = {
        "asset_id": asset_id,
        "asset_kind": entry.semantic_contract.asset_kind,
        "payload_kind": entry.semantic_contract.payload_kind,
        "content_hash": content_hash,
        "subject_candidates": subject_candidates,
        "title": result.scope.title,
        "semantic_key": semantic_key,
        "order_index": None,
        "material_type": entry.semantic_contract.material_type,
        "event_time": result.scope.event_time,
        "published_at": result.scope.published_at,
        "report_period": result.scope.report_period,
        "observed_at": observed_at,
        "source_ref": access_id,
        "provider": provider.provider_name,
        "adapter": provider.adapter_name,
        "query_params": params,
        "source_tier": provider.source_tier,
        "trace_level": provider.trace_level,
        "locator": result.locator,
        "raw_asset_ref": result.raw_asset_ref,
        "producer_action_ref": run_id,
        "payload": payload,
    }
    validated = validate_envelope(envelope)

    with engine.begin() as conn:
        existing = conn.execute(
            select(data_asset.c.asset_id).where(
                data_asset.c.provider == provider.provider_name,
                data_asset.c.dedup_key == dedup_key,
            )
        ).first()
        if existing is not None:
            conn.execute(
                outbox_event.insert().values(
                    event_id=_new_id("ev"),
                    event_kind="observed",
                    subject_ref=build_asset_uri(
                        SERVICE_NAME, 1, AssetKind.DATASET_SNAPSHOT, existing.asset_id
                    ),
                    asset_id=existing.asset_id,
                    processing_run_id=run_id,
                    payload={"dataset_key": entry.dataset_key, "content_hash": content_hash},
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
                data_asset.c.provider == provider.provider_name,
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
                subject_candidates=subject_candidates,
                title=result.scope.title,
                semantic_key=semantic_key,
                material_type=entry.semantic_contract.material_type,
                event_time=result.scope.event_time,
                published_at=result.scope.published_at,
                report_period=result.scope.report_period,
                observed_at=observed_at,
                source_access_id=access_id,
                provider=provider.provider_name,
                adapter=provider.adapter_name,
                source_tier=str(provider.source_tier.value),
                trace_level=str(provider.trace_level.value),
                locator=result.locator,
                raw_asset_ref=result.raw_asset_ref,
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
                subject_ref=build_asset_uri(SERVICE_NAME, 1, AssetKind.DATASET_SNAPSHOT, asset_id),
                asset_id=asset_id,
                processing_run_id=run_id,
                payload={
                    "dataset_key": entry.dataset_key,
                    "content_hash": content_hash,
                    "superseded_asset_id": superseded_id,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        conn.execute(
            update(processing_run)
            .where(processing_run.c.run_id == run_id)
            .values(status="succeeded", finished_at=datetime.now(UTC))
        )

    return RegistrationOutcome(
        status="materialized",
        run_id=run_id,
        access_id=access_id,
        asset_id=asset_id,
        superseded_asset_id=superseded_id,
    )
