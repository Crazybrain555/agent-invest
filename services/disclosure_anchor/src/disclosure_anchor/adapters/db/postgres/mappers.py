"""Mapping between domain entities and ORM models.

Kept separate from repositories so the field-by-field mapping is easy to audit.
Server-managed columns (timestamps, outbox ``seq``) are copied back onto the
entity after flush when available.
"""

from __future__ import annotations

from typing import Any

from disclosure_anchor.adapters.db.postgres import models as m
from disclosure_anchor.domain import entities as e


def company_to_model(entity: e.Company) -> m.Company:
    return m.Company(
        company_id=entity.company_id,
        legal_name=entity.legal_name,
        unified_social_credit_code=entity.unified_social_credit_code,
    )


def company_to_entity(row: m.Company) -> e.Company:
    return e.Company(
        company_id=row.company_id,
        legal_name=row.legal_name,
        unified_social_credit_code=row.unified_social_credit_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def company_identifier_to_model(entity: e.CompanyIdentifier) -> m.CompanyIdentifier:
    return m.CompanyIdentifier(
        identifier_id=entity.identifier_id,
        company_id=entity.company_id,
        scheme=entity.scheme,
        raw_value=entity.raw_value,
        normalized_value=entity.normalized_value,
        jurisdiction=entity.jurisdiction,
        source_access_id=entity.source_access_id,
        status=entity.status,
        valid_from=entity.valid_from,
        valid_to=entity.valid_to,
        observed_at=entity.observed_at,
    )


def company_identifier_to_entity(row: m.CompanyIdentifier) -> e.CompanyIdentifier:
    return e.CompanyIdentifier(
        identifier_id=row.identifier_id,
        company_id=row.company_id,
        scheme=row.scheme,
        raw_value=row.raw_value,
        normalized_value=row.normalized_value,
        jurisdiction=row.jurisdiction,
        source_access_id=row.source_access_id,
        status=row.status,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        observed_at=row.observed_at,
        created_at=row.created_at,
    )


def security_to_model(entity: e.Security) -> m.Security:
    return m.Security(
        security_id=entity.security_id,
        company_id=entity.company_id,
        security_code=entity.security_code,
        exchange=entity.exchange,
        board=entity.board,
        status=entity.status,
    )


def security_to_entity(row: m.Security) -> e.Security:
    return e.Security(
        security_id=row.security_id,
        company_id=row.company_id,
        security_code=row.security_code,
        exchange=row.exchange,
        board=row.board,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def tracked_company_to_model(entity: e.TrackedCompany) -> m.TrackedCompany:
    return m.TrackedCompany(
        tracked_company_id=entity.tracked_company_id,
        company_id=entity.company_id,
        security_id=entity.security_id,
        status=entity.status,
        lookback=entity.lookback,
        filing_categories=entity.filing_categories,
        sync_frequency=entity.sync_frequency,
    )


def tracked_company_to_entity(row: m.TrackedCompany) -> e.TrackedCompany:
    return e.TrackedCompany(
        tracked_company_id=row.tracked_company_id,
        company_id=row.company_id,
        security_id=row.security_id,
        status=row.status,
        lookback=row.lookback,
        filing_categories=row.filing_categories,
        sync_frequency=row.sync_frequency,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def source_access_to_model(entity: e.SourceAccess) -> m.SourceAccess:
    return m.SourceAccess(
        source_access_id=entity.source_access_id,
        provider=entity.provider,
        provider_interface=entity.provider_interface,
        dataset_key=entity.dataset_key,
        query_params=entity.query_params,
        accessed_at=entity.accessed_at,
        status=entity.status,
        result_hash=entity.result_hash,
        error=entity.error,
        result_snapshot=entity.result_snapshot,
        company_id=entity.company_id,
        security_id=entity.security_id,
    )


def source_access_to_entity(row: m.SourceAccess) -> e.SourceAccess:
    return e.SourceAccess(
        source_access_id=row.source_access_id,
        provider=row.provider,
        provider_interface=row.provider_interface,
        dataset_key=row.dataset_key,
        query_params=row.query_params,
        accessed_at=row.accessed_at,
        status=row.status,
        result_hash=row.result_hash,
        error=row.error,
        result_snapshot=row.result_snapshot,
        company_id=row.company_id,
        security_id=row.security_id,
        created_at=row.created_at,
    )


def source_checkpoint_to_model(entity: e.SourceCheckpoint) -> m.SourceCheckpoint:
    return m.SourceCheckpoint(
        source_checkpoint_id=entity.source_checkpoint_id,
        provider=entity.provider,
        scope_key=entity.scope_key,
        cursor=entity.cursor,
    )


def source_checkpoint_to_entity(row: m.SourceCheckpoint) -> e.SourceCheckpoint:
    return e.SourceCheckpoint(
        source_checkpoint_id=row.source_checkpoint_id,
        provider=row.provider,
        scope_key=row.scope_key,
        cursor=row.cursor,
        updated_at=row.updated_at,
    )


def document_to_model(entity: e.Document) -> m.Document:
    return m.Document(
        document_id=entity.document_id,
        company_id=entity.company_id,
        security_id=entity.security_id,
        source_access_id=entity.source_access_id,
        provider=entity.provider,
        provider_document_id=entity.provider_document_id,
        title=entity.title,
        filing_type=entity.filing_type,
        announcement_date=entity.announcement_date,
        report_period=entity.report_period,
        raw_file_relpath=entity.raw_file_relpath,
        raw_file_hash=entity.raw_file_hash,
        status=entity.status,
        provider_metadata=entity.provider_metadata,
        current_processing_run_id=entity.current_processing_run_id,
        supersedes_document_id=entity.supersedes_document_id,
        correction_of_document_id=entity.correction_of_document_id,
    )


def document_to_entity(row: m.Document) -> e.Document:
    return e.Document(
        document_id=row.document_id,
        company_id=row.company_id,
        security_id=row.security_id,
        source_access_id=row.source_access_id,
        provider=row.provider,
        provider_document_id=row.provider_document_id,
        title=row.title,
        filing_type=row.filing_type,
        announcement_date=row.announcement_date,
        report_period=row.report_period,
        raw_file_relpath=row.raw_file_relpath,
        raw_file_hash=row.raw_file_hash,
        status=row.status,
        provider_metadata=row.provider_metadata,
        current_processing_run_id=row.current_processing_run_id,
        supersedes_document_id=row.supersedes_document_id,
        correction_of_document_id=row.correction_of_document_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def processing_run_to_model(entity: e.ProcessingRun) -> m.ProcessingRun:
    return m.ProcessingRun(
        processing_run_id=entity.processing_run_id,
        document_id=entity.document_id,
        run_kind=entity.run_kind,
        status=entity.status,
        parser_name=entity.parser_name,
        parser_version=entity.parser_version,
        parser_backend=entity.parser_backend,
        parser_method=entity.parser_method,
        parser_language=entity.parser_language,
        input_raw_file_hash=entity.input_raw_file_hash,
        parser_artifact_relpath=entity.parser_artifact_relpath,
        artifact_hash=entity.artifact_hash,
        normalized_ir_relpath=entity.normalized_ir_relpath,
        document_units_relpath=entity.document_units_relpath,
        content_hash_aggregate=entity.content_hash_aggregate,
        structure_hash=entity.structure_hash,
        builder_rules_version=entity.builder_rules_version,
        is_active=entity.is_active,
        unit_build_status=entity.unit_build_status,
        unit_build_error=entity.unit_build_error,
        unit_build_attempt_count=entity.unit_build_attempt_count,
        unit_built_at=entity.unit_built_at,
        started_at=entity.started_at,
        finished_at=entity.finished_at,
        error=entity.error,
    )


def processing_run_to_entity(row: m.ProcessingRun) -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id=row.processing_run_id,
        document_id=row.document_id,
        run_kind=row.run_kind,
        status=row.status,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        parser_backend=row.parser_backend,
        parser_method=row.parser_method,
        parser_language=row.parser_language,
        input_raw_file_hash=row.input_raw_file_hash,
        parser_artifact_relpath=row.parser_artifact_relpath,
        artifact_hash=row.artifact_hash,
        normalized_ir_relpath=row.normalized_ir_relpath,
        document_units_relpath=row.document_units_relpath,
        content_hash_aggregate=row.content_hash_aggregate,
        structure_hash=row.structure_hash,
        builder_rules_version=row.builder_rules_version,
        is_active=row.is_active,
        unit_build_status=row.unit_build_status,
        unit_build_error=row.unit_build_error,
        unit_build_attempt_count=row.unit_build_attempt_count,
        unit_built_at=row.unit_built_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        created_at=row.created_at,
    )


def document_unit_to_model(entity: e.DocumentUnit) -> m.DocumentUnit:
    return m.DocumentUnit(
        asset_id=entity.asset_id,
        document_id=entity.document_id,
        processing_run_id=entity.processing_run_id,
        provider_document_id=entity.provider_document_id,
        payload_kind=entity.payload_kind,
        heading_path=entity.heading_path,
        title=entity.title,
        order_index=entity.order_index,
        semantic_key=entity.semantic_key,
        payload=entity.payload,
        content_hash=entity.content_hash,
        structure_hash=entity.structure_hash,
        quality_status=entity.quality_status,
        query_projection_hash=entity.query_projection_hash,
        artifact_locator=entity.artifact_locator,
    )


def document_unit_to_entity(row: m.DocumentUnit) -> e.DocumentUnit:
    return e.DocumentUnit(
        asset_id=row.asset_id,
        document_id=row.document_id,
        processing_run_id=row.processing_run_id,
        provider_document_id=row.provider_document_id,
        payload_kind=row.payload_kind,
        heading_path=list(row.heading_path or []),
        title=row.title,
        order_index=row.order_index,
        semantic_key=row.semantic_key,
        payload=row.payload,
        content_hash=row.content_hash,
        structure_hash=row.structure_hash,
        quality_status=row.quality_status,
        query_projection_hash=row.query_projection_hash,
        artifact_locator=row.artifact_locator,
        created_at=row.created_at,
    )


def outbox_event_to_model(entity: e.OutboxEvent) -> m.OutboxEvent:
    values: dict[str, Any] = {
        "event_id": entity.event_id,
        "event_kind": entity.event_kind,
        "change_kind": entity.change_kind,
        "subject_kind": entity.subject_kind,
        "subject_ref": entity.subject_ref,
        "document_id": entity.document_id,
        "processing_run_id": entity.processing_run_id,
        "asset_id": entity.asset_id,
        "payload": entity.payload,
    }
    if entity.occurred_at is not None:
        values["occurred_at"] = entity.occurred_at
    return m.OutboxEvent(**values)


def outbox_event_to_entity(row: m.OutboxEvent) -> e.OutboxEvent:
    return e.OutboxEvent(
        event_id=row.event_id,
        event_kind=row.event_kind,
        change_kind=row.change_kind,
        subject_kind=row.subject_kind,
        subject_ref=row.subject_ref,
        seq=row.seq,
        document_id=row.document_id,
        processing_run_id=row.processing_run_id,
        asset_id=row.asset_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )
