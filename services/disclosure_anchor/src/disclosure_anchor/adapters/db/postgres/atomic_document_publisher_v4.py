"""PostgreSQL transaction-P publisher for one complete V4 document."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PreviousActiveUnitV4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactsReadyV4,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    DurablePublishBaseEvidence,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    ProviderEnvelopeContextV4,
    advance_remote_parse_checkpoint_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationCommitResponseLost,
    AtomicPublicationUniqueConflict,
    AtomicPublicationWinnerV4,
    PublishedOutboxEventV4,
    build_atomic_publication_outbox_events_v4,
    final_unit_row_sha256_v4,
    processing_run_row_sha256_v4,
    seal_atomic_publication_winner_v4,
    seal_published_outbox_event_v4,
    validate_atomic_publication_claim_v4,
    validate_atomic_publication_artifacts_ready_v4,
    validate_atomic_publication_winner_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4SuccessorAppend,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4ClaimWitness
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import query_projection


class PostgresAtomicWholeDocumentPublisherV4:
    """Select and persist one immutable whole-document publication winner."""

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    def commit_whole_document(
        self,
        request: AtomicPublicationRequestV4,
        *,
        claim: V4ClaimWitness,
        artifacts_ready: AtomicPublicationArtifactsReadyV4,
    ) -> AtomicPublicationWinnerV4:
        if type(request) is not AtomicPublicationRequestV4:
            raise ValueError("transaction-P request must be exact V4")
        validate_atomic_publication_claim_v4(request=request, claim=claim)
        validate_atomic_publication_artifacts_ready_v4(
            request=request,
            artifacts_ready=artifacts_ready,
        )

        commit_state = _CommitState()
        with _classify_commit_boundary(commit_state), SqlAlchemyUnitOfWork(
            engine=self._engine
        ) as uow:
            session = uow.session
            identity = request.identity
            acquire_document_xact_lock(session, identity.document_id)

            # Response-loss replay precedes the mutable lease check.  A caller
            # must be able to recover a durably committed winner after the old
            # lease has expired or the lifecycle has advanced to cleanup.
            authority = uow.remote_parse_v4.load(identity.attempt_id)
            if authority.publication_winner is not None:
                winner = authority.publication_winner
                try:
                    validate_atomic_publication_winner_v4(
                        request=request,
                        winner=winner,
                    )
                    if (
                        winner.winner_row_version != 2
                        or winner.artifact_readiness != artifacts_ready.reference
                    ):
                        raise ValueError(
                            "winner does not bind supplied artifact readiness"
                        )
                except ValueError as exc:
                    raise AtomicPublicationUniqueConflict(
                        "a different immutable transaction-P winner exists"
                    ) from exc
                self._require_committed_closure(
                    session,
                    winner=winner,
                    request=request,
                    context=self._materialization_context(authority),
                )
                return winner

            authority = uow.remote_parse_v4.reload_claimed(
                claim,
                lock_for_transition=True,
            )
            context = self._require_local_materialized_authority(
                authority=authority,
                request=request,
            )

            document = session.execute(
                sa.select(models.Document)
                .where(models.Document.document_id == identity.document_id)
                .with_for_update()
            ).scalar_one_or_none()
            if document is None:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P document disappeared"
                )
            projection = _object_json(request.processing_run_projection_json)

            runs = tuple(
                session.execute(
                    sa.select(models.ProcessingRun)
                    .where(
                        models.ProcessingRun.document_id == identity.document_id,
                        sa.or_(
                            models.ProcessingRun.processing_run_id
                            == identity.processing_run_id,
                            models.ProcessingRun.is_active.is_(True),
                        ),
                    )
                    .order_by(models.ProcessingRun.processing_run_id)
                    .with_for_update()
                ).scalars()
            )
            by_id = {row.processing_run_id: row for row in runs}
            candidate = by_id.get(identity.processing_run_id)
            if candidate is None:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P candidate run disappeared"
                )
            active = tuple(row for row in runs if row.is_active)
            previous_run_id = identity.expected_previous_processing_run_id
            self._require_currentness(
                document=document,
                candidate=candidate,
                active=active,
                previous_run_id=previous_run_id,
                request=request,
                projection=projection,
                context=context,
            )

            locked_run_ids = tuple(sorted(by_id))
            unit_rows = tuple(
                session.execute(
                    sa.select(models.DocumentUnit)
                    .where(models.DocumentUnit.processing_run_id.in_(locked_run_ids))
                    .order_by(
                        models.DocumentUnit.processing_run_id,
                        models.DocumentUnit.order_index,
                        models.DocumentUnit.asset_id,
                    )
                    .with_for_update()
                ).scalars()
            )
            candidate_units = tuple(
                row
                for row in unit_rows
                if row.processing_run_id == identity.processing_run_id
            )
            if candidate_units:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P candidate already contains Unit rows"
                )
            previous_units = tuple(
                row
                for row in unit_rows
                if previous_run_id is not None
                and row.processing_run_id == previous_run_id
            )
            if self._previous_inventory(previous_units) != request.previous_active_units:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P previous active Unit inventory drifted"
                )

            publish_at = datetime.now(UTC)
            asset_ids = tuple(
                item.asset_id
                for item in artifacts_ready.preparation.unit_bindings
            )
            persisted_units = [
                _document_unit(unit, asset_id=asset_id)
                for unit, asset_id in zip(request.units, asset_ids, strict=True)
            ]
            uow.document_units.add_many(persisted_units)

            if previous_run_id is not None:
                by_id[previous_run_id].is_active = False
                session.flush()
            self._apply_processing_projection(
                candidate,
                projection=projection,
                publish_at=publish_at,
            )
            document.current_processing_run_id = identity.processing_run_id
            document.status = "published"
            session.flush()

            domain_events = build_atomic_publication_outbox_events_v4(
                request=request,
                asset_ids=asset_ids,
                occurred_at=publish_at,
            )
            published_events = tuple(
                _published_outbox_event(uow.outbox.add(event))
                for event in domain_events
            )
            uow.publish_evidence.add_base(
                DurablePublishBaseEvidence(
                    processing_run_id=identity.processing_run_id,
                    document_id=identity.document_id,
                    source_identity_sha256=request.upstream_evidence.source_pdf_sha256,
                    source_page_count=request.source_page_count,
                    publish_precommit_at=publish_at,
                )
            )

            winner = seal_atomic_publication_winner_v4(
                request=request,
                asset_ids=asset_ids,
                outbox_events=published_events,
                attempt_id=identity.attempt_id,
                fence_identity=identity.fence_identity,
                document_id=identity.document_id,
                processing_run_id=identity.processing_run_id,
                publish_attempt_generation=identity.attempt_generation,
                local_checkpoint_sha256=identity.expected_checkpoint_sha256,
                lifecycle_version_before=identity.expected_lifecycle_version,
                lifecycle_version_after=identity.expected_lifecycle_version + 1,
                request_sha256=request.request_sha256,
                upstream_evidence_sha256=request.upstream_evidence.evidence_sha256,
                previous_active_run_id=previous_run_id,
                publish_precommit_at=publish_at,
                artifact_readiness=artifacts_ready.reference,
                winner_row_version=2,
            )
            successor = advance_remote_parse_checkpoint_v4(
                authority.checkpoint,
                state="publish_committed",
                held_resource_credit=authority.checkpoint.held_resource_credit,
                publication_winner_sha256=winner.sha256,
            )
            uow.remote_parse_v4.append_successor(
                V4SuccessorAppend(
                    claim=claim,
                    successor=successor,
                    publication_winner=winner,
                )
            )
            session.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
            self._require_committed_closure(
                session,
                winner=winner,
                request=request,
                context=context,
            )
            commit_state.attempted = True
            uow.commit()
            commit_state.acknowledged = True
            return winner

    def reload_commit_winner(
        self,
        *,
        processing_run_id: str,
        attempt_id: str,
    ) -> AtomicPublicationWinnerV4 | None:
        _identity(processing_run_id, "processing run")
        _identity(attempt_id, "attempt")
        with SqlAlchemyUnitOfWork(engine=self._engine) as uow:
            authority = uow.remote_parse_v4.load(attempt_id)
            winner = authority.publication_winner
            if winner is None:
                return None
            if winner.processing_run_id != processing_run_id:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P winner belongs to another processing run"
                )
            self._require_committed_closure(
                uow.session,
                winner=winner,
                request=None,
                context=self._materialization_context(authority),
            )
            return winner

    def reload_commit_winner_by_processing_run_id(
        self,
        *,
        processing_run_id: str,
    ) -> AtomicPublicationWinnerV4 | None:
        """Read the sole committed winner for one processing run."""

        _identity(processing_run_id, "processing run")
        with self._engine.connect() as conn:
            attempt_ids = tuple(
                conn.execute(
                    sa.select(models.AtomicPublicationWinnerV4.attempt_id)
                    .where(
                        models.AtomicPublicationWinnerV4.processing_run_id
                        == processing_run_id
                    )
                    .order_by(models.AtomicPublicationWinnerV4.attempt_id)
                    .limit(2)
                ).scalars()
            )
        if not attempt_ids:
            return None
        if len(attempt_ids) != 1:
            raise AtomicPublicationUniqueConflict(
                "processing run has multiple transaction-P winners"
            )
        return self.reload_commit_winner(
            processing_run_id=processing_run_id,
            attempt_id=attempt_ids[0],
        )

    @staticmethod
    def _require_local_materialized_authority(
        *,
        authority: RemoteParseV4Authority,
        request: AtomicPublicationRequestV4,
    ) -> ProviderEnvelopeContextV4:
        identity = request.identity
        checkpoint = authority.checkpoint
        if (
            authority.state != "local_materialized"
            or authority.attempt_id != identity.attempt_id
            or authority.fence_identity != identity.fence_identity
            or authority.document_id != identity.document_id
            or authority.processing_run_id != identity.processing_run_id
            or authority.attempt_generation != identity.attempt_generation
            or authority.source_pdf_sha256
            != request.upstream_evidence.source_pdf_sha256
            or authority.parser_target_sha256
            != request.upstream_evidence.parser_target_sha256
            or authority.request_sha256 != request.upstream_evidence.request_sha256
            or checkpoint.runtime_epoch_sha256
            != request.upstream_evidence.runtime_epoch_sha256
            or checkpoint.process_profile_sha256
            != request.upstream_evidence.process_profile_sha256
            or checkpoint.source_page_count != request.source_page_count
            or checkpoint.local_materialization_receipt_sha256
            != identity.expected_local_materialization_receipt_sha256
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P local-materialized authority drifted"
            )
        by_kind = {item.kind: item.value for item in authority.evidence}
        intent = by_kind.get("materialization_intent")
        receipt = by_kind.get("local_materialization_receipt")
        if (
            type(intent) is not MaterializationIntentV4
            or type(receipt) is not LocalMaterializationReceiptV4
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P durable materialization evidence is incomplete"
            )
        upstream = request.upstream_evidence
        context = intent.provider_envelope_context
        if (
            upstream.provider != context.provider
            or upstream.provider_document_id != context.provider_document_id
            or upstream.source_pdf_relpath != context.source_pdf_relpath
            or upstream.parser_artifact_root_relpath
            != context.parser_artifact_root_relpath
            or upstream.provider_envelope_context_json
            != context.canonical_bytes.decode("utf-8")
            or upstream.provider_envelope_context_sha256 != context.sha256
            or upstream.terminal_receipt_sha256
            != intent.terminal_receipt_sha256
            or upstream.materialization_intent_sha256 != intent.sha256
            or upstream.local_materialization_receipt_sha256 != receipt.sha256
            or upstream.output_files_sha256 != receipt.output_files_sha256
            or upstream.output_file_count != receipt.output_file_count
            or upstream.output_total_byte_count != receipt.output_byte_count
            or upstream.output_manifest_sha256 != receipt.output_manifest_sha256
            or upstream.output_manifest_relpath != receipt.output_manifest_relpath
            or upstream.output_manifest_byte_count
            != receipt.output_manifest_byte_count
            or upstream.provider_envelope_sha256
            != receipt.provider_envelope_sha256
            or upstream.provider_envelope_relpath
            != receipt.provider_envelope_relpath
            or upstream.provider_envelope_byte_count
            != receipt.provider_envelope_byte_count
            or upstream.provider_document_sha256
            != receipt.provider_envelope_sha256
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P upstream evidence drifted from durable materialization"
            )
        return context

    @staticmethod
    def _materialization_context(
        authority: RemoteParseV4Authority,
    ) -> ProviderEnvelopeContextV4:
        intent = next(
            (
                item.value
                for item in authority.evidence
                if item.kind == "materialization_intent"
            ),
            None,
        )
        if type(intent) is not MaterializationIntentV4:
            raise AtomicPublicationUniqueConflict(
                "transaction-P durable materialization intent is missing"
            )
        return intent.provider_envelope_context

    @staticmethod
    def _require_currentness(
        *,
        document: models.Document,
        candidate: models.ProcessingRun,
        active: tuple[models.ProcessingRun, ...],
        previous_run_id: str | None,
        request: AtomicPublicationRequestV4,
        projection: dict[str, Any],
        context: ProviderEnvelopeContextV4,
    ) -> None:
        if candidate.is_active or candidate.status != "succeeded":
            raise AtomicPublicationUniqueConflict(
                "transaction-P candidate is not succeeded and inactive"
            )
        if (
            document.provider != context.provider
            or document.provider_document_id != context.provider_document_id
            or document.raw_file_relpath != context.source_pdf_relpath
            or document.raw_file_hash != context.source_pdf_sha256
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P locked document provenance drifted"
            )
        if (
            candidate.document_id != request.identity.document_id
            or candidate.run_kind != projection["run_kind"]
            or candidate.artifact_owner_processing_run_id
            != projection["artifact_owner_processing_run_id"]
            or candidate.artifact_owner_processing_run_id
            != candidate.processing_run_id
            or candidate.normalized_ir_relpath is not None
            or candidate.parser_name != projection["parser_name"]
            or candidate.parser_version != projection["parser_version"]
            or candidate.parser_backend != projection["parser_backend"]
            or candidate.parser_method != projection["parser_method"]
            or candidate.parser_language != projection["parser_language"]
            or candidate.parser_target_identity
            != projection["parser_target_identity"]
            or candidate.input_raw_file_hash
            != request.upstream_evidence.source_pdf_sha256
            or candidate.artifact_hash
            != request.upstream_evidence.provider_document_sha256
            or candidate.unit_build_status
            != request.expected_unit_build_status_before
            or candidate.unit_build_attempt_count
            != request.expected_unit_build_attempt_count_before
            or candidate.unit_build_error is not None
            or candidate.document_units_relpath is not None
            or candidate.semantic_route_receipts_hash is not None
            or candidate.semantic_route_receipts_relpath is not None
            or candidate.semantic_route_receipts_contract_version is not None
            or candidate.semantic_adjudication_status is not None
            or candidate.semantic_degraded_unit_count is not None
            or candidate.semantic_failover_group_count is not None
            or candidate.semantic_adjudication_summary is not None
            or candidate.content_hash_aggregate is not None
            or candidate.structure_hash is not None
            or candidate.builder_rules_version is not None
            or candidate.unit_built_at is not None
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P candidate provenance drifted"
            )
        if previous_run_id is None:
            if active or document.current_processing_run_id is not None:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P initial publication found an active run"
                )
        elif (
            len(active) != 1
            or active[0].processing_run_id != previous_run_id
            or document.current_processing_run_id != previous_run_id
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P previous active pointer drifted"
            )

    @staticmethod
    def _previous_inventory(
        rows: tuple[models.DocumentUnit, ...],
    ) -> tuple[PreviousActiveUnitV4, ...]:
        result: list[PreviousActiveUnitV4] = []
        for row in rows:
            projection = query_projection(
                payload_kind=row.payload_kind,
                title=row.title,
                heading_path=list(row.heading_path),
                semantic_keys=(
                    None if row.semantic_keys is None else list(row.semantic_keys)
                ),
                section_keys=(
                    None if row.section_keys is None else list(row.section_keys)
                ),
                quality_status=row.quality_status,
                applicability=row.applicability,
                payload=cast(dict[str, Any], row.payload),
            )
            canonical = _canonical_json_text(projection)
            if row.query_projection_hash != _digest(canonical.encode("utf-8")):
                raise AtomicPublicationUniqueConflict(
                    "transaction-P stored active Unit projection drifted"
                )
            result.append(
                PreviousActiveUnitV4(
                    asset_id=row.asset_id,
                    processing_run_id=row.processing_run_id,
                    order_index=row.order_index,
                    payload_kind=row.payload_kind,
                    heading_path=tuple(row.heading_path),
                    content_hash=row.content_hash,
                    query_projection_hash=cast(str, row.query_projection_hash),
                    canonical_query_projection_json=canonical,
                )
            )
        return tuple(result)

    @staticmethod
    def _apply_processing_projection(
        run: models.ProcessingRun,
        *,
        projection: dict[str, Any],
        publish_at: datetime,
    ) -> None:
        run.artifact_owner_processing_run_id = cast(
            str,
            projection["artifact_owner_processing_run_id"],
        )
        run.status = cast(str, projection["status"])
        run.normalized_ir_relpath = cast(str | None, projection["normalized_ir_relpath"])
        run.parser_name = cast(str, projection["parser_name"])
        run.parser_version = cast(str, projection["parser_version"])
        run.parser_backend = cast(str, projection["parser_backend"])
        run.parser_method = cast(str, projection["parser_method"])
        run.parser_language = cast(str, projection["parser_language"])
        run.parser_target_identity = cast(
            dict[str, Any],
            projection["parser_target_identity"],
        )
        run.parser_artifact_relpath = cast(
            str,
            projection["parser_artifact_relpath"],
        )
        run.provider_document_relpath = cast(
            str,
            projection["provider_document_relpath"],
        )
        run.document_units_relpath = cast(
            str,
            projection["document_units_relpath"],
        )
        run.semantic_route_receipts_hash = cast(
            str,
            projection["semantic_route_receipts_sha256"],
        )
        run.semantic_route_receipts_contract_version = cast(
            str,
            projection["semantic_route_receipts_contract_version"],
        )
        run.semantic_route_receipts_relpath = cast(
            str,
            projection["semantic_route_receipts_relpath"],
        )
        run.content_hash_aggregate = cast(
            str,
            projection["content_hash_aggregate"],
        )
        run.structure_hash = cast(str, projection["structure_hash_aggregate"])
        run.builder_rules_version = cast(str, projection["builder_rules_version"])
        run.semantic_adjudication_status = cast(
            str,
            projection["semantic_adjudication_status"],
        )
        run.semantic_degraded_unit_count = cast(
            int,
            projection["semantic_degraded_unit_count"],
        )
        run.semantic_failover_group_count = cast(
            int,
            projection["semantic_failover_group_count"],
        )
        run.semantic_adjudication_summary = cast(
            dict[str, Any],
            projection["semantic_adjudication_summary"],
        )
        run.is_active = True
        run.unit_build_status = cast(str, projection["unit_build_status"])
        run.unit_build_error = None
        run.unit_build_attempt_count = cast(
            int,
            projection["unit_build_attempt_count"],
        )
        run.unit_built_at = publish_at

    @staticmethod
    def _require_committed_closure(
        session: Session,
        *,
        winner: AtomicPublicationWinnerV4,
        request: AtomicPublicationRequestV4 | None,
        context: ProviderEnvelopeContextV4,
    ) -> None:
        document = session.get(models.Document, winner.document_id)
        run = session.get(models.ProcessingRun, winner.processing_run_id)
        if (
            document is None
            or run is None
            or document.status != "published"
            or document.current_processing_run_id != winner.processing_run_id
            or document.provider != context.provider
            or document.provider_document_id != context.provider_document_id
            or document.raw_file_relpath != context.source_pdf_relpath
            or document.raw_file_hash != context.source_pdf_sha256
            or not run.is_active
            or run.status != "succeeded"
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P committed active projection is incomplete"
            )
        units = tuple(
            session.execute(
                sa.select(models.DocumentUnit)
                .where(
                    models.DocumentUnit.processing_run_id
                    == winner.processing_run_id
                )
                .order_by(models.DocumentUnit.order_index)
            ).scalars()
        )
        if (
            len(units) != len(winner.unit_assets)
            or tuple(row.asset_id for row in units)
            != tuple(item.asset_id for item in winner.unit_assets)
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P committed Unit inventory is incomplete"
            )
        for row, asset in zip(units, winner.unit_assets, strict=True):
            stored_row_sha = _stored_final_unit_row_sha256(row)
            if stored_row_sha != asset.final_unit_row_sha256:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P committed Unit row hash drifted"
                )
            if request is not None:
                expected_row_sha = final_unit_row_sha256_v4(
                    request=request,
                    unit_index=asset.unit_index,
                    asset_id=asset.asset_id,
                )
                if expected_row_sha != asset.final_unit_row_sha256:
                    raise AtomicPublicationUniqueConflict(
                        "transaction-P expected Unit row hash drifted"
                    )

        outbox_rows = tuple(
            session.execute(
                sa.select(models.OutboxEvent)
                .where(
                    models.OutboxEvent.event_id.in_(
                        tuple(item.event_id for item in winner.outbox_commit.events)
                    )
                )
                .order_by(models.OutboxEvent.seq)
            ).scalars()
        )
        if len(outbox_rows) != len(winner.outbox_commit.events):
            raise AtomicPublicationUniqueConflict(
                "transaction-P committed outbox rows are incomplete"
            )
        for outbox_row, expected in zip(
            outbox_rows,
            winner.outbox_commit.events,
            strict=True,
        ):
            if _published_outbox_event(_outbox_entity(outbox_row)) != expected:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P committed outbox row drifted"
                )

        base = session.get(models.DurablePublishBase, winner.processing_run_id)
        durable = winner.durable_base_commit
        if (
            base is None
            or base.document_id != durable.document_id
            or base.source_identity_sha256 != durable.source_identity_sha256
            or base.source_page_count != durable.source_page_count
            or base.publish_precommit_at != durable.publish_precommit_at
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P committed durable base drifted"
            )

        stored_projection = _processing_projection(document, run, len(units))
        if (
            _digest(_canonical_json_text(stored_projection).encode("utf-8"))
            != winner.processing_run_row_sha256
        ):
            raise AtomicPublicationUniqueConflict(
                "transaction-P committed processing row hash drifted"
            )
        if request is not None:
            validate_atomic_publication_winner_v4(request=request, winner=winner)
            expected_projection = _object_json(
                request.processing_run_projection_json
            )
            if stored_projection != expected_projection:
                raise AtomicPublicationUniqueConflict(
                    "transaction-P committed processing projection drifted"
                )
            if (
                processing_run_row_sha256_v4(request)
                != winner.processing_run_row_sha256
            ):
                raise AtomicPublicationUniqueConflict(
                    "transaction-P committed processing row hash drifted"
                )


def _document_unit(unit: Any, *, asset_id: str) -> e.DocumentUnit:
    payload = _object_json(unit.canonical_payload_json)
    locator = _object_json(unit.canonical_artifact_locator_json)
    return e.DocumentUnit(
        asset_id=asset_id,
        document_id=unit.document_id,
        processing_run_id=unit.processing_run_id,
        provider_document_id=unit.provider_document_id,
        payload_kind=unit.payload_kind,
        heading_path=list(unit.heading_path),
        title=unit.title,
        order_index=unit.unit_index,
        semantic_keys=(
            None if unit.semantic_keys is None else list(unit.semantic_keys)
        ),
        section_keys=(
            None if unit.section_keys is None else list(unit.section_keys)
        ),
        payload=payload,
        content_hash=unit.content_hash,
        structure_hash=unit.structure_hash,
        quality_status=unit.quality_status,
        applicability=unit.applicability,
        page_no=unit.page_no,
        query_projection_hash=unit.query_projection_hash,
        artifact_locator=locator,
    )


def _published_outbox_event(event: e.OutboxEvent) -> PublishedOutboxEventV4:
    if (
        event.seq is None
        or event.document_id is None
        or event.processing_run_id is None
        or event.payload is None
        or event.occurred_at is None
    ):
        raise AtomicPublicationUniqueConflict(
            "transaction-P outbox insert did not return an exact row"
        )
    return seal_published_outbox_event_v4(
        event_id=event.event_id,
        event_sequence=event.seq,
        event_kind=event.event_kind,
        change_kind=event.change_kind,
        subject_kind=event.subject_kind,
        subject_ref=event.subject_ref,
        document_id=event.document_id,
        processing_run_id=event.processing_run_id,
        asset_id=event.asset_id,
        canonical_payload_json=_canonical_json_text(event.payload),
        occurred_at=event.occurred_at,
    )


def _outbox_entity(row: models.OutboxEvent) -> e.OutboxEvent:
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
        payload=cast(dict[str, Any] | None, row.payload),
        occurred_at=row.occurred_at.astimezone(UTC),
        created_at=row.created_at,
    )


def _processing_projection(
    document: models.Document,
    run: models.ProcessingRun,
    unit_count: int,
) -> dict[str, Any]:
    return {
        "artifact_owner_processing_run_id": run.artifact_owner_processing_run_id,
        "builder_rules_version": run.builder_rules_version,
        "content_hash_aggregate": run.content_hash_aggregate,
        "contract_version": "processing-run-publication.v4",
        "document_id": document.document_id,
        "document_units_relpath": run.document_units_relpath,
        "is_active": run.is_active,
        "normalized_ir_relpath": run.normalized_ir_relpath,
        "parser_artifact_relpath": run.parser_artifact_relpath,
        "parser_backend": run.parser_backend,
        "parser_language": run.parser_language,
        "parser_method": run.parser_method,
        "parser_name": run.parser_name,
        "parser_target_identity": run.parser_target_identity,
        "parser_version": run.parser_version,
        "processing_run_id": run.processing_run_id,
        "provider_document_id": document.provider_document_id,
        "provider_document_relpath": run.provider_document_relpath,
        "provider_document_sha256": run.artifact_hash,
        "run_kind": run.run_kind,
        "semantic_adjudication_status": run.semantic_adjudication_status,
        "semantic_adjudication_summary": run.semantic_adjudication_summary,
        "semantic_degraded_unit_count": run.semantic_degraded_unit_count,
        "semantic_failover_group_count": run.semantic_failover_group_count,
        "semantic_route_receipts_contract_version": (
            run.semantic_route_receipts_contract_version
        ),
        "semantic_route_receipts_relpath": run.semantic_route_receipts_relpath,
        "semantic_route_receipts_sha256": run.semantic_route_receipts_hash,
        "source_pdf_relpath": document.raw_file_relpath,
        "source_pdf_sha256": run.input_raw_file_hash,
        "status": run.status,
        "structure_hash_aggregate": run.structure_hash,
        "unit_build_attempt_count": run.unit_build_attempt_count,
        "unit_build_status": run.unit_build_status,
        "unit_count": unit_count,
    }


def _stored_final_unit_row_sha256(row: models.DocumentUnit) -> str:
    return _digest(
        _canonical_json_text(
            {
                "applicability": row.applicability,
                "artifact_locator": row.artifact_locator,
                "asset_id": row.asset_id,
                "content_hash": row.content_hash,
                "document_id": row.document_id,
                "heading_path": row.heading_path,
                "order_index": row.order_index,
                "page_no": row.page_no,
                "payload": row.payload,
                "payload_kind": row.payload_kind,
                "processing_run_id": row.processing_run_id,
                "provider_document_id": row.provider_document_id,
                "quality_status": row.quality_status,
                "query_projection_hash": row.query_projection_hash,
                "section_keys": row.section_keys,
                "semantic_keys": row.semantic_keys,
                "structure_hash": row.structure_hash,
                "title": row.title,
            }
        ).encode("utf-8")
    )


def _object_json(value: str) -> dict[str, Any]:
    decoded = strict_json_loads(value.encode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("transaction-P canonical JSON must be an object")
    return cast(dict[str, Any], decoded)


def _canonical_json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _identity(value: str, label: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"transaction-P {label} is invalid")


def _commit_response_is_unknown(exc: DBAPIError) -> bool:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return bool(exc.connection_invalidated) or (
        isinstance(sqlstate, str) and sqlstate.startswith("08")
    )


@dataclass(slots=True)
class _CommitState:
    attempted: bool = False
    acknowledged: bool = False


@contextmanager
def _classify_commit_boundary(state: _CommitState) -> Iterator[None]:
    try:
        yield
    except AtomicPublicationCommitResponseLost:
        raise
    except BaseException as exc:
        if state.acknowledged:
            raise AtomicPublicationCommitResponseLost(
                "transaction-P committed but UOW cleanup failed; reload winner"
            ) from exc
        if not isinstance(exc, DBAPIError):
            raise
        if state.attempted and _commit_response_is_unknown(exc):
            raise AtomicPublicationCommitResponseLost(
                "transaction-P commit response was lost; reload winner"
            ) from exc
        raise


__all__ = ["PostgresAtomicWholeDocumentPublisherV4"]
