"""Filesystem transaction-P preparation/readiness implementation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, cast

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    decode_atomic_publication_request_v4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict,
    AtomicPublicationArtifactPreparationV1,
    AtomicPublicationArtifactReadinessError,
    AtomicPublicationArtifactsReadyV4,
    AtomicPublicationFileResourceV1,
    AtomicPublicationParserOutputPlanV1,
    AtomicPublicationReadinessManifestV1,
    AtomicPublicationReadinessReferenceV1,
    AtomicPublicationUnitBindingV4,
    decode_atomic_publication_preparation_v1,
    decode_atomic_publication_readiness_v1,
    document_unit_snapshot_file_bytes_v1,
    final_unit_bindings_sha256_v4,
    _issue_atomic_publication_artifacts_ready_v4,
    lineage_bindings_sha256_v4,
    publication_semantic_route_receipts_file_bytes_v3,
    readiness_resource_values_sha256_v1,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_CONTRACT_VERSION,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.publication_envelope_policy import (
    PUBLICATION_ENVELOPE_POLICY_V1,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
    seal_unit_asset_winners_v4,
    validate_atomic_publication_winner_v4,
)
from disclosure_anchor.application.ports.atomic_publication_artifact_readiness_v4 import (
    ImmutableArtifactStorePort,
    MaterializedOutputPromotionV4Port,
    PublicationWriteSpacePort,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.staged_execution import note_stage
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimGuard,
    V4ClaimWitness,
    V4StageGuard,
)
from disclosure_anchor.application.services.publication_envelope_plan_v4 import (
    PublicationPlanMeasurementV1,
    measure_atomic_publication_plan_v4,
)
from disclosure_anchor.domain.ids import new_asset_id


@dataclass(frozen=True, slots=True)
class _PublicationPlan:
    """One measured plan: the readiness bytes written last, and the preparation's identity."""

    preparation_sha256: str
    preparation_byte_count: int
    manifest_bytes: bytes
    reference: AtomicPublicationReadinessReferenceV1
    measurement: PublicationPlanMeasurementV1


class FilesystemAtomicPublicationArtifactReadinessV4:
    """Create/replay exact artifacts and issue readiness only after reread."""

    def __init__(
        self,
        *,
        paths: FileStorePathPort,
        immutable_store: ImmutableArtifactStorePort,
        output_promotion: MaterializedOutputPromotionV4Port,
        write_space: PublicationWriteSpacePort | None = None,
    ) -> None:
        self._paths = paths
        self._store = immutable_store
        self._promotion = output_promotion
        # A storage-bound worker promises the new readiness files against the
        # work volume's live free floor; read-only verifiers write nothing.
        self._write_space = write_space

    def prepare_or_replay(
        self,
        *,
        request: AtomicPublicationRequestV4,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationReadinessReferenceV1:
        stage_guard.checkpoint()
        self._validate_input_evidence(
            request=request,
            checkpoint=checkpoint,
            materialized=materialized,
            claim=claim,
        )
        preparation_relpath, readiness_relpath = self._authority_paths(request)
        preparation = self.load_preparation(request=request)
        create_preparation = preparation is None
        if preparation is None:
            preparation, payloads = self._build_preparation_with_payloads(
                request=request, materialized=materialized,
            )
        else:
            payloads = self._artifact_payloads(
                request=request, materialized=materialized, preparation=preparation,
            )
        # Every record of the plan, including a bound of the winner P can
        # write, is encoded and measured before the first write: a record
        # outside the envelope is a capacity fact of this document with no
        # effect on disk.
        plan, preparation_bytes = self._plan(
            request=request,
            preparation=preparation,
            payloads=payloads,
            preparation_relpath=preparation_relpath,
            readiness_relpath=readiness_relpath,
        )
        note_stage(stage_guard, "publication_plan_measured", **plan.measurement.note_scalars())
        # The whole-plan encoding may outlive this stage.
        stage_guard.checkpoint()
        with self._space_for_missing_files(
            request=request,
            preparation=preparation,
            plan=plan,
            preparation_relpath=preparation_relpath,
            readiness_relpath=readiness_relpath,
        ):
            if create_preparation:
                try:
                    stage_guard.checkpoint()
                    self._store.create_or_verify(
                        relpath=preparation_relpath,
                        payload=preparation_bytes,
                    )
                except AtomicPublicationArtifactConflict:
                    loaded = self.load_preparation(request=request)
                    if loaded is None:
                        raise
                    # Another producer's IDs own the files: its plan is the
                    # one measured, written and verified from here on.
                    preparation = loaded
                    payloads = self._artifact_payloads(
                        request=request, materialized=materialized, preparation=loaded,
                    )
                    plan, preparation_bytes = self._plan(
                        request=request,
                        preparation=loaded,
                        payloads=payloads,
                        preparation_relpath=preparation_relpath,
                        readiness_relpath=readiness_relpath,
                    )
            del preparation_bytes
            self._require_same_request(preparation=preparation, request=request)
            exact_preparation = self._store.read_exact(
                relpath=preparation_relpath,
                expected_sha256=plan.preparation_sha256,
                expected_byte_count=plan.preparation_byte_count,
                max_byte_count=PUBLICATION_ENVELOPE_POLICY_V1.preparation_bytes,
            )
            preparation = decode_atomic_publication_preparation_v1(exact_preparation)
            del exact_preparation

            # The parser tree is installed first; each derived file follows using
            # immutable create-or-verify.  Readiness is the sole last write.
            stage_guard.checkpoint()
            self._promotion.promote_or_replay(
                checkpoint=checkpoint,
                materialized=materialized,
                published_relpath=preparation.parser_output_plan.published_relpath,
                claim=claim,
                claim_guard=claim_guard,
                stage_guard=stage_guard,
            )
            for resource, payload in (
                (preparation.provider_document_plan, payloads["provider_document"]),
                (preparation.document_unit_snapshot_plan, payloads["document_unit_snapshot"]),
                (preparation.semantic_route_receipts_plan, payloads["semantic_route_receipts"]),
            ):
                if _digest(payload) != resource.sha256 or len(payload) != resource.byte_count:
                    raise AtomicPublicationArtifactReadinessError(
                        f"{resource.role} bytes drifted from preparation"
                    )
                stage_guard.checkpoint()
                self._store.create_or_verify(
                    relpath=Path(resource.relpath),
                    payload=payload,
                )
            del payloads
            self._verify_resources(preparation)
            stage_guard.checkpoint()
            self._store.create_or_verify(
                relpath=readiness_relpath,
                payload=plan.manifest_bytes,
            )
        reference = plan.reference
        # Only the reference crosses into the from-disk replay verification.
        del plan, preparation
        self.verify_ready(reference=reference, expected_request=request)
        return reference

    def _plan(
        self,
        *,
        request: AtomicPublicationRequestV4,
        preparation: AtomicPublicationArtifactPreparationV1,
        payloads: dict[str, bytes],
        preparation_relpath: Path,
        readiness_relpath: Path,
    ) -> tuple[_PublicationPlan, bytes]:
        """Encode the preparation and readiness once and measure the whole plan.

        The preparation bytes are returned beside the plan so the caller can
        drop them as soon as they are on disk.
        """

        preparation_bytes = preparation.canonical_bytes
        manifest_bytes = self._build_readiness(
            preparation=preparation,
            preparation_relpath=preparation_relpath,
            preparation_bytes=preparation_bytes,
        ).canonical_bytes
        reference = AtomicPublicationReadinessReferenceV1(
            manifest_relpath=readiness_relpath.as_posix(),
            manifest_sha256=_digest(manifest_bytes),
            manifest_byte_count=len(manifest_bytes),
        )
        measurement = measure_atomic_publication_plan_v4(
            request=request,
            preparation=preparation,
            preparation_byte_count=len(preparation_bytes),
            snapshot_byte_count=len(payloads["document_unit_snapshot"]),
            semantic_byte_count=len(payloads["semantic_route_receipts"]),
            readiness=reference,
            policy=PUBLICATION_ENVELOPE_POLICY_V1,
        )
        plan = _PublicationPlan(
            preparation_sha256=_digest(preparation_bytes),
            preparation_byte_count=len(preparation_bytes),
            manifest_bytes=manifest_bytes,
            reference=reference,
            measurement=measurement,
        )
        return plan, preparation_bytes

    @contextmanager
    def _space_for_missing_files(
        self,
        *,
        request: AtomicPublicationRequestV4,
        preparation: AtomicPublicationArtifactPreparationV1,
        plan: _PublicationPlan,
        preparation_relpath: Path,
        readiness_relpath: Path,
    ) -> Iterator[None]:
        """Promise space only for the planned files not yet on disk.

        The plan was encoded and measured first, so an envelope refusal is a
        capacity fact of this document found before the first write. Present
        files are verified by create-or-verify, never promised again; promoting
        the parser tree is a rename and needs no space.
        """

        planned = (
            (preparation_relpath, plan.preparation_byte_count),
            (Path(preparation.provider_document_plan.relpath), preparation.provider_document_plan.byte_count),
            (Path(preparation.document_unit_snapshot_plan.relpath), preparation.document_unit_snapshot_plan.byte_count),
            (Path(preparation.semantic_route_receipts_plan.relpath),
             preparation.semantic_route_receipts_plan.byte_count),
            (readiness_relpath, len(plan.manifest_bytes)),
        )
        if self._write_space is None:
            yield
            return
        missing = sum(
            byte_count for relpath, byte_count in planned
            if not os.path.lexists(self._paths.data_path(relpath))
        )
        with self._write_space.publication_write_space(
            attempt_id=request.identity.attempt_id, byte_count=missing,
        ):
            yield

    def load_preparation(
        self,
        *,
        request: AtomicPublicationRequestV4,
    ) -> AtomicPublicationArtifactPreparationV1 | None:
        preparation_relpath, _ = self._authority_paths(request)
        raw = self._read_untrusted_regular(
            preparation_relpath,
            max_byte_count=PUBLICATION_ENVELOPE_POLICY_V1.preparation_bytes,
        )
        if raw is None:
            return None
        value = decode_atomic_publication_preparation_v1(raw)
        self._require_same_request(preparation=value, request=request)
        return value

    def reopen_prepared_request(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> AtomicPublicationRequestV4 | None:
        """Recover the canonical request without retaining a process object."""

        if (
            type(checkpoint) is not RemoteParseCheckpointV4
            or checkpoint.state != "local_materialized"
            or type(materialized) is not MaterializedProviderDocumentV4
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication request reopen authority is invalid"
            )
        context = materialized.intent.provider_envelope_context
        preparation_relpath, _ = self._authority_paths_from_values(
            provider=context.provider,
            source_pdf_relpath=context.source_pdf_relpath,
            provider_document_id=context.provider_document_id,
            processing_run_id=checkpoint.processing_run_id,
        )
        raw = self._read_untrusted_regular(
            preparation_relpath,
            max_byte_count=PUBLICATION_ENVELOPE_POLICY_V1.preparation_bytes,
        )
        if raw is None:
            return None
        preparation = decode_atomic_publication_preparation_v1(raw)
        request = decode_atomic_publication_request_v4(
            preparation.canonical_request_json.encode("utf-8")
        )
        self._validate_reopened_request(
            request=request,
            checkpoint=checkpoint,
            materialized=materialized,
        )
        expected_preparation_relpath, _ = self._authority_paths(request)
        if preparation_relpath != expected_preparation_relpath:
            raise AtomicPublicationArtifactReadinessError(
                "publication preparation path drifted from durable input"
            )
        return request

    def verify_ready(
        self,
        *,
        reference: AtomicPublicationReadinessReferenceV1,
        expected_request: AtomicPublicationRequestV4 | None = None,
        expected_winner: AtomicPublicationWinnerV4 | None = None,
    ) -> AtomicPublicationArtifactsReadyV4:
        if type(reference) is not AtomicPublicationReadinessReferenceV1:
            raise AtomicPublicationArtifactReadinessError(
                "readiness verification requires an exact reference"
            )
        manifest_bytes = self._store.read_exact(
            relpath=Path(reference.manifest_relpath),
            expected_sha256=reference.manifest_sha256,
            expected_byte_count=reference.manifest_byte_count,
            max_byte_count=PUBLICATION_ENVELOPE_POLICY_V1.readiness_bytes,
        )
        manifest = decode_atomic_publication_readiness_v1(manifest_bytes)
        preparation_bytes = self._store.read_exact(
            relpath=Path(manifest.preparation_relpath),
            expected_sha256=manifest.preparation_sha256,
            expected_byte_count=manifest.preparation_byte_count,
            max_byte_count=PUBLICATION_ENVELOPE_POLICY_V1.preparation_bytes,
        )
        preparation = decode_atomic_publication_preparation_v1(preparation_bytes)
        request = decode_atomic_publication_request_v4(
            preparation.canonical_request_json.encode("utf-8")
        )
        # Both requests re-derived their SHA-256 from their full canonical
        # payload when built, so the content addresses compare without two
        # more whole-request encodings.
        if expected_request is not None and request.request_sha256 != expected_request.request_sha256:
            raise AtomicPublicationArtifactReadinessError(
                "readiness references another publication request"
            )
        self._verify_resources(preparation)
        if expected_winner is not None:
            self._validate_winner(
                winner=expected_winner,
                preparation=preparation,
                reference=reference,
                request=request,
            )
        return _issue_atomic_publication_artifacts_ready_v4(
            preparation=preparation,
            manifest=manifest,
            reference=reference,
            request=request,
        )

    def _build_preparation(
        self,
        *,
        request: AtomicPublicationRequestV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> AtomicPublicationArtifactPreparationV1:
        return self._build_preparation_with_payloads(request=request, materialized=materialized)[0]

    @staticmethod
    def _artifact_payloads(
        *,
        request: AtomicPublicationRequestV4,
        materialized: MaterializedProviderDocumentV4,
        preparation: AtomicPublicationArtifactPreparationV1,
    ) -> dict[str, bytes]:
        return {
            "provider_document": provider_document_envelope_to_bytes(materialized.provider_envelope),
            "document_unit_snapshot": document_unit_snapshot_file_bytes_v1(
                request=request, bindings=preparation.unit_bindings,
            ),
            "semantic_route_receipts": publication_semantic_route_receipts_file_bytes_v3(
                request.semantic_route_receipts
            ),
        }

    def _build_preparation_with_payloads(
        self,
        *,
        request: AtomicPublicationRequestV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> tuple[AtomicPublicationArtifactPreparationV1, dict[str, bytes]]:
        assets = seal_unit_asset_winners_v4(
            request=request,
            asset_ids=tuple(new_asset_id() for _ in request.units),
        )
        bindings = tuple(
            AtomicPublicationUnitBindingV4(**asdict(item)) for item in assets
        )
        projection = cast(
            dict[str, Any],
            strict_json_loads(request.processing_run_projection_json.encode("utf-8")),
        )
        provider_bytes = provider_document_envelope_to_bytes(
            materialized.provider_envelope
        )
        snapshot_bytes = document_unit_snapshot_file_bytes_v1(
            request=request,
            bindings=bindings,
        )
        semantic_bytes = publication_semantic_route_receipts_file_bytes_v3(
            request.semantic_route_receipts
        )
        # One canonical encoding of the request serves both its text and length.
        request_bytes = request.canonical_bytes
        parser = AtomicPublicationParserOutputPlanV1(
            source_relpath=materialized.intent.output_relpath,
            published_relpath=request.upstream_evidence.parser_artifact_root_relpath,
            inventory_sha256=materialized.receipt.output_files_sha256,
            file_count=materialized.receipt.output_file_count,
            byte_count=materialized.receipt.output_byte_count,
        )
        provider = AtomicPublicationFileResourceV1(
            role="provider_document",
            relpath=cast(str, projection["provider_document_relpath"]),
            sha256=_digest(provider_bytes),
            byte_count=len(provider_bytes),
            resource_contract_version=PROVIDER_DOCUMENT_CONTRACT_VERSION,
        )
        snapshot = AtomicPublicationFileResourceV1(
            role="document_unit_snapshot",
            relpath=cast(str, projection["document_units_relpath"]),
            sha256=_digest(snapshot_bytes),
            byte_count=len(snapshot_bytes),
            resource_contract_version="document_units.v1",
        )
        semantic = AtomicPublicationFileResourceV1(
            role="semantic_route_receipts",
            relpath=cast(str, projection["semantic_route_receipts_relpath"]),
            sha256=_digest(semantic_bytes),
            byte_count=len(semantic_bytes),
            resource_contract_version=SEMANTIC_ROUTE_RECEIPT_V3,
        )
        preparation = AtomicPublicationArtifactPreparationV1(
            attempt_id=request.identity.attempt_id,
            attempt_generation=request.identity.attempt_generation,
            fence_identity=request.identity.fence_identity,
            document_id=request.identity.document_id,
            processing_run_id=request.identity.processing_run_id,
            provider_document_id=request.identity.provider_document_id,
            canonical_request_json=request_bytes.decode("utf-8"),
            request_sha256=request.request_sha256,
            request_byte_count=len(request_bytes),
            artifact_owner_processing_run_id=request.identity.processing_run_id,
            parser_target_sha256=request.upstream_evidence.parser_target_sha256,
            provider_envelope_context_sha256=(
                request.upstream_evidence.provider_envelope_context_sha256
            ),
            unit_bindings=bindings,
            final_units_sha256=final_unit_bindings_sha256_v4(bindings),
            lineage_sha256=lineage_bindings_sha256_v4(bindings),
            parser_output_plan=parser,
            provider_document_plan=provider,
            document_unit_snapshot_plan=snapshot,
            semantic_route_receipts_plan=semantic,
        )
        payloads = {
            "provider_document": provider_bytes,
            "document_unit_snapshot": snapshot_bytes,
            "semantic_route_receipts": semantic_bytes,
        }
        return preparation, payloads

    @staticmethod
    def _build_readiness(
        *,
        preparation: AtomicPublicationArtifactPreparationV1,
        preparation_relpath: Path,
        preparation_bytes: bytes,
    ) -> AtomicPublicationReadinessManifestV1:
        resources_sha256 = readiness_resource_values_sha256_v1(
            parser_output=preparation.parser_output_plan,
            provider_document=preparation.provider_document_plan,
            document_unit_snapshot=preparation.document_unit_snapshot_plan,
            semantic_route_receipts=preparation.semantic_route_receipts_plan,
        )
        return AtomicPublicationReadinessManifestV1(
            attempt_id=preparation.attempt_id,
            attempt_generation=preparation.attempt_generation,
            fence_identity=preparation.fence_identity,
            document_id=preparation.document_id,
            processing_run_id=preparation.processing_run_id,
            provider_document_id=preparation.provider_document_id,
            request_sha256=preparation.request_sha256,
            artifact_owner_processing_run_id=(
                preparation.artifact_owner_processing_run_id
            ),
            parser_target_sha256=preparation.parser_target_sha256,
            provider_envelope_context_sha256=(
                preparation.provider_envelope_context_sha256
            ),
            preparation_relpath=preparation_relpath.as_posix(),
            preparation_sha256=_digest(preparation_bytes),
            preparation_byte_count=len(preparation_bytes),
            unit_bindings=preparation.unit_bindings,
            final_units_sha256=preparation.final_units_sha256,
            lineage_sha256=preparation.lineage_sha256,
            parser_output=preparation.parser_output_plan,
            provider_document=preparation.provider_document_plan,
            document_unit_snapshot=preparation.document_unit_snapshot_plan,
            semantic_route_receipts=preparation.semantic_route_receipts_plan,
            resources_sha256=resources_sha256,
        )

    def _verify_resources(
        self,
        preparation: AtomicPublicationArtifactPreparationV1,
    ) -> None:
        parser = preparation.parser_output_plan
        self._promotion.verify_published(
            published_relpath=parser.published_relpath,
            expected_inventory_sha256=parser.inventory_sha256,
            expected_file_count=parser.file_count,
            expected_byte_count=parser.byte_count,
        )
        for plan in (
            preparation.provider_document_plan,
            preparation.document_unit_snapshot_plan,
            preparation.semantic_route_receipts_plan,
        ):
            self._store.read_exact(
                relpath=Path(plan.relpath),
                expected_sha256=plan.sha256,
                expected_byte_count=plan.byte_count,
                max_byte_count=max(plan.byte_count, 1),
            )

    def _authority_paths(
        self,
        request: AtomicPublicationRequestV4,
    ) -> tuple[Path, Path]:
        return self._authority_paths_from_values(
            provider=request.upstream_evidence.provider,
            source_pdf_relpath=request.upstream_evidence.source_pdf_relpath,
            provider_document_id=request.identity.provider_document_id,
            processing_run_id=request.identity.processing_run_id,
        )

    def _authority_paths_from_values(
        self,
        *,
        provider: str,
        source_pdf_relpath: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> tuple[Path, Path]:
        source_parts = source_pdf_relpath.split("/")
        if len(source_parts) != 6:
            raise AtomicPublicationArtifactReadinessError(
                "publication source path topology drifted"
            )
        values = {
            "provider": provider,
            "security_code": source_parts[2],
            "provider_document_id": provider_document_id,
            "processing_run_id": processing_run_id,
        }
        return (
            self._paths.atomic_publication_preparation_relpath(**values),
            self._paths.atomic_publication_readiness_relpath(**values),
        )

    def _read_untrusted_regular(
        self,
        relpath: Path,
        *,
        max_byte_count: int,
    ) -> bytes | None:
        path = self._paths.data_path(relpath)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AtomicPublicationArtifactConflict(
                "publication authority file identity drifted"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 1 <= before.st_size <= max_byte_count
            ):
                raise AtomicPublicationArtifactConflict(
                    "publication authority file identity drifted"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "publication authority file cannot be read"
            ) from exc
        finally:
            os.close(descriptor)
        if (
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or len(raw) != before.st_size
        ):
            raise AtomicPublicationArtifactConflict(
                "publication authority file changed while read"
            )
        return raw

    @staticmethod
    def _require_same_request(
        *,
        preparation: AtomicPublicationArtifactPreparationV1,
        request: AtomicPublicationRequestV4,
    ) -> None:
        if preparation.canonical_request_json.encode("utf-8") != request.canonical_bytes:
            raise AtomicPublicationArtifactConflict(
                "publication preparation already belongs to another request"
            )

    @staticmethod
    def _validate_input_evidence(
        *,
        request: AtomicPublicationRequestV4,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        claim: V4ClaimWitness,
    ) -> None:
        if (
            type(request) is not AtomicPublicationRequestV4
            or type(checkpoint) is not RemoteParseCheckpointV4
            or type(materialized) is not MaterializedProviderDocumentV4
            or type(claim) is not V4ClaimWitness
            or not claim.validates(checkpoint)
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication readiness input evidence drifted"
            )
        FilesystemAtomicPublicationArtifactReadinessV4._validate_reopened_request(
            request=request,
            checkpoint=checkpoint,
            materialized=materialized,
        )

    @staticmethod
    def _validate_reopened_request(
        *,
        request: AtomicPublicationRequestV4,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> None:
        evidence = request.upstream_evidence
        context = materialized.intent.provider_envelope_context
        if (
            checkpoint.state != "local_materialized"
            or (
                checkpoint.attempt_id,
                checkpoint.attempt_generation,
                checkpoint.fence_identity,
                checkpoint.document_id,
                checkpoint.processing_run_id,
                checkpoint.sha256,
                checkpoint.local_materialization_receipt_sha256,
            )
            != (
                request.identity.attempt_id,
                request.identity.attempt_generation,
                request.identity.fence_identity,
                request.identity.document_id,
                request.identity.processing_run_id,
                evidence.local_materialized_checkpoint_sha256,
                evidence.local_materialization_receipt_sha256,
            )
            or (
                evidence.provider,
                evidence.provider_document_id,
                evidence.source_pdf_relpath,
                evidence.provider_envelope_context_sha256,
            )
            != (
                context.provider,
                context.provider_document_id,
                context.source_pdf_relpath,
                context.sha256,
            )
            or materialized.receipt.sha256
            != evidence.local_materialization_receipt_sha256
            or materialized.intent.sha256 != evidence.materialization_intent_sha256
            or materialized.receipt.output_files_sha256
            != evidence.output_files_sha256
            or materialized.receipt.output_file_count != evidence.output_file_count
            or materialized.receipt.output_byte_count
            != evidence.output_total_byte_count
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication readiness input evidence drifted"
            )

    @staticmethod
    def _validate_winner(
        *,
        winner: AtomicPublicationWinnerV4,
        preparation: AtomicPublicationArtifactPreparationV1,
        reference: AtomicPublicationReadinessReferenceV1,
        request: AtomicPublicationRequestV4,
    ) -> None:
        try:
            validate_atomic_publication_winner_v4(
                request=request,
                winner=winner,
            )
        except ValueError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "publication winner drifted from its request"
            ) from exc
        winner_reference = getattr(winner, "artifact_readiness", None)
        winner_bindings = tuple(
            AtomicPublicationUnitBindingV4(**asdict(item))
            for item in winner.unit_assets
        )
        if (
            winner_reference != reference
            or winner.request_sha256 != preparation.request_sha256
            or winner_bindings != preparation.unit_bindings
            or winner.final_units_sha256 != preparation.final_units_sha256
            or winner.lineage_sha256 != preparation.lineage_sha256
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication winner drifted from artifact readiness"
            )


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["FilesystemAtomicPublicationArtifactReadinessV4"]
