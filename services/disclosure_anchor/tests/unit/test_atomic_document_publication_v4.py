from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PreviousActiveUnitV4,
    PublicationAttemptIdentityV4,
    WholeDocumentPublicationV4Error,
    decode_atomic_publication_request_v4,
    previous_active_units_sha256_v4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
    seal_upstream_publication_evidence_v4,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationManifestV4,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderDocument,
    ProviderPage,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalMaterializationReceiptV4,
    LocalOutputFileV4,
    MaterializationIntentV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_materialization_receipt_v4,
    build_materialization_intent_v4,
    build_resource_reservation_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceReservationInput,
    ResourceCreditVector,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SemanticRouteReceiptRowV3,
    semantic_route_receipt_row_v3_to_payload,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitLocator,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    query_projection,
    structure_hash_aggregate,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
    PublishedOutboxEventV4,
    decode_atomic_publication_winner_v4,
    final_unit_row_sha256_v4,
    final_unit_rows_sha256_v4,
    lineage_row_sha256_v4,
    lineage_rows_sha256_v4,
    processing_run_row_sha256_v4,
    seal_atomic_publication_winner_v4,
    seal_published_outbox_commit_reference_v4,
    seal_published_outbox_event_v4,
    validate_atomic_publication_winner_v4,
    validate_atomic_publication_claim_v4,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4ClaimWitness
from tests.unit._semantic_routes import _fallback_receipt
from tests.unit.test_remote_parse_lifecycle_v4 import _happy_path


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64


class AtomicDocumentPublicationV4Tests(unittest.TestCase):
    def test_publication_claim_is_noncanonical_and_binds_the_cas_identity(self) -> None:
        request = _request()
        claim = V4ClaimWitness(
            attempt_id=request.identity.attempt_id,
            fence_identity=request.identity.fence_identity,
            state=request.identity.expected_attempt_state,
            lifecycle_version=request.identity.expected_lifecycle_version,
            checkpoint_sha256=request.identity.expected_checkpoint_sha256,
            claim_owner_identity="worker-1",
            claim_generation=7,
        )
        validate_atomic_publication_claim_v4(request=request, claim=claim)
        winner = _winner(request)
        self.assertNotIn(b"claim_owner", request.canonical_bytes)
        self.assertNotIn(b"claim_generation", request.canonical_bytes)
        self.assertNotIn(b"claim_owner", winner.canonical_bytes)
        self.assertNotIn(b"claim_generation", winner.canonical_bytes)
        for field_name, value in (
            ("attempt_id", "attempt-other"),
            ("fence_identity", "fence-other"),
            ("state", "publish_committed"),
            ("lifecycle_version", claim.lifecycle_version + 1),
            ("checkpoint_sha256", SHA_E),
        ):
            with (
                self.subTest(field_name=field_name),
                self.assertRaisesRegex(ValueError, "drifted"),
            ):
                validate_atomic_publication_claim_v4(
                    request=request,
                    claim=replace(claim, **{field_name: value}),
                )

    def test_publication_lifecycle_versions_fit_exact_bigint_successor(self) -> None:
        request = _request()
        max_int = (1 << 63) - 1
        self.assertEqual(
            replace(
                request.identity,
                expected_lifecycle_version=max_int - 1,
            ).expected_lifecycle_version,
            max_int - 1,
        )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "successor",
        ):
            replace(
                request.identity,
                expected_lifecycle_version=max_int,
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "successor",
        ):
            replace(
                request.upstream_evidence,
                local_materialized_lifecycle_version=max_int,
            )
        winner = _winner(request)
        self.assertEqual(
            replace(
                winner,
                lifecycle_version_before=max_int - 1,
                lifecycle_version_after=max_int,
            ).lifecycle_version_after,
            max_int,
        )
        with self.assertRaisesRegex(ValueError, "successor"):
            replace(
                winner,
                lifecycle_version_before=max_int,
                lifecycle_version_after=max_int + 1,
            )

    def test_pre_id_request_seals_and_round_trips_without_asset_ids(self) -> None:
        request = _request()
        self.assertEqual(decode_atomic_publication_request_v4(request.canonical_bytes), request)
        payload = json.loads(request.canonical_bytes)
        self.assertNotIn("asset_id", request.canonical_bytes.decode("utf-8"))
        self.assertEqual(payload["units"][0]["unit_index"], 1)
        self.assertEqual(
            payload["semantic_route_receipts"][0]["routed_draft_sha256"],
            payload["units"][0]["routed_draft_sha256"],
        )

    def test_sealers_derive_hashes_without_constructing_invalid_drafts(self) -> None:
        request = _request()
        self.assertNotEqual(request.request_sha256, "sha256:" + "0" * 64)
        self.assertNotEqual(
            request.upstream_evidence.evidence_sha256,
            "sha256:" + "0" * 64,
        )
        self.assertNotEqual(request.units[0].routed_draft_sha256, "sha256:" + "0" * 64)

    def test_request_rejects_unknown_noncanonical_and_semantic_drift(self) -> None:
        request = _request()
        payload = json.loads(request.canonical_bytes)
        payload["asset_id"] = "du_01AAAAAAAAAAAAAAAAAAAAAAAA"
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "closed"):
            decode_atomic_publication_request_v4(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "canonical"):
            decode_atomic_publication_request_v4(
                request.canonical_bytes.replace(b'":', b'": ', 1)
            )
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "identity drifted"):
            replace(
                request,
                semantic_route_receipts=(
                    replace(
                        request.semantic_route_receipts[0],
                        provider_locator_sha256=SHA_E,
                    ),
                ),
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error, "upstream evidence"
        ):
            replace(
                request,
                identity=replace(request.identity, attempt_id="attempt-other"),
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error, "upstream evidence"
        ):
            replace(
                request,
                identity=replace(request.identity, attempt_generation=999),
            )
        for field, forged in (
            ("source_pdf_relpath", "raw_documents/cninfo/other.pdf"),
            ("parser_artifact_relpath", "parser_artifacts/foreign/run"),
            ("provider_document_relpath", "derived/provider/foreign.json"),
        ):
            with self.subTest(field=field):
                projection = json.loads(request.processing_run_projection_json)
                projection[field] = forged
                projection_json = json.dumps(
                    projection, sort_keys=True, separators=(",", ":")
                )
                with self.assertRaisesRegex(
                    WholeDocumentPublicationV4Error, "resource path"
                ):
                    replace(
                        request,
                        processing_run_projection_json=projection_json,
                        processing_run_projection_sha256=(
                            "sha256:"
                            + hashlib.sha256(projection_json.encode()).hexdigest()
                        ),
                    )

    def test_pre_id_units_enforce_key_and_locator_invariants(self) -> None:
        request = _request()
        unit = request.units[0]
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "semantic key"):
            replace(unit, semantic_keys=())
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "semantic key"):
            replace(unit, section_keys=("section", "section"))
        with self.assertRaisesRegex(WholeDocumentPublicationV4Error, "locator"):
            replace(unit, provider_locator_sha256=SHA_A)

    def test_upstream_bridge_rejects_cross_attempt_and_fact_drift(self) -> None:
        reservation, checkpoint, intent, receipt, manifest, provider_envelope = (
            _publication_materialized_evidence()
        )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error, "local-materialized evidence"
        ):
            seal_upstream_publication_evidence_v4(
                reservation=reservation,
                checkpoint=checkpoint,
                intent=replace(
                    intent,
                    document_id="doc-other",
                    provider_envelope_context=replace(
                        intent.provider_envelope_context,
                        document_id="doc-other",
                    ),
                ),
                receipt=receipt,
                manifest=manifest,
                provider_envelope=provider_envelope,
            )
        upstream = seal_upstream_publication_evidence_v4(
            reservation=reservation,
            checkpoint=checkpoint,
            intent=intent,
            receipt=receipt,
            manifest=manifest,
            provider_envelope=provider_envelope,
        )
        self.assertEqual(
            upstream.provider_document_id,
            intent.provider_envelope_context.provider_document_id,
        )
        self.assertEqual(
            upstream.provider_envelope_context_sha256,
            intent.provider_envelope_context.sha256,
        )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error, "local-materialized evidence"
        ):
            seal_upstream_publication_evidence_v4(
                reservation=reservation,
                checkpoint=checkpoint,
                intent=intent,
                receipt=replace(receipt, source_page_count=3),
                manifest=manifest,
                provider_envelope=provider_envelope,
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "local-materialized evidence",
        ):
            seal_upstream_publication_evidence_v4(
                reservation=reservation,
                checkpoint=replace(
                    checkpoint,
                    held_resource_credit=replace(
                        checkpoint.held_resource_credit,
                        output_bytes=(
                            checkpoint.held_resource_credit.output_bytes + 1
                        ),
                    ),
                ),
                intent=intent,
                receipt=receipt,
                manifest=manifest,
                provider_envelope=provider_envelope,
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "local-materialized evidence",
        ):
            seal_upstream_publication_evidence_v4(
                reservation=reservation,
                checkpoint=replace(
                    checkpoint,
                    source_byte_count=checkpoint.source_byte_count + 1,
                    held_resource_credit=replace(
                        checkpoint.held_resource_credit,
                        snapshot_bytes=(
                            checkpoint.held_resource_credit.snapshot_bytes + 1
                        ),
                    ),
                ),
                intent=intent,
                receipt=receipt,
                manifest=manifest,
                provider_envelope=provider_envelope,
            )
        forged_artifacts = tuple(
            replace(item, sha256=SHA_E)
            if item.role == "model_json"
            else item
            for item in provider_envelope.provider_document.artifacts
        )
        forged_document = replace(
            provider_envelope.provider_document,
            artifacts=forged_artifacts,
            bundle_sha256=provider_artifact_bundle_sha256(forged_artifacts),
        )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "local-materialized evidence",
        ):
            seal_upstream_publication_evidence_v4(
                reservation=reservation,
                checkpoint=checkpoint,
                intent=intent,
                receipt=receipt,
                manifest=manifest,
                provider_envelope=replace(
                    provider_envelope,
                    provider_document=forged_document,
                ),
            )

    def test_winner_is_immutable_commit_receipt_without_post_commit_time(self) -> None:
        request = _request()
        winner = _winner(request)
        self.assertEqual(decode_atomic_publication_winner_v4(winner.canonical_bytes), winner)
        payload = json.loads(winner.canonical_bytes)
        self.assertNotIn("durable_commit_observed_at", payload)
        self.assertNotIn("committed_at", payload)
        self.assertEqual(payload["publish_precommit_at"], "2026-08-31T00:00:00Z")
        self.assertEqual(payload["unit_assets"][0]["asset_id"], "du_01K00000000000000000000000")
        self.assertEqual(winner.outbox_commit.event_count, 2)
        self.assertEqual(
            [item.event_kind for item in winner.outbox_commit.events],
            ["document_unit_created", "processing_run_published"],
        )
        self.assertEqual(winner.updated_count, 0)
        self.assertEqual(winner.deleted_count, 0)
        self.assertNotEqual(winner.durable_base_commit.durable_base_sha256, SHA_E)
        self.assertEqual(
            winner.unit_assets[0].final_unit_row_sha256,
            final_unit_row_sha256_v4(
                request=request,
                unit_index=1,
                asset_id=winner.unit_assets[0].asset_id,
            ),
        )
        self.assertEqual(
            winner.unit_assets[0].lineage_row_sha256,
            lineage_row_sha256_v4(
                request=request,
                unit_index=1,
                asset_id=winner.unit_assets[0].asset_id,
            ),
        )
        self.assertEqual(
            winner.processing_run_row_sha256,
            processing_run_row_sha256_v4(request),
        )

    def test_winner_rejects_unknown_fields_and_noncontiguous_mapping(self) -> None:
        request = _request()
        winner = _winner(request)
        payload = json.loads(winner.canonical_bytes)
        payload["durable_commit_observed_at"] = "2026-08-31T00:00:01Z"
        with self.assertRaisesRegex(ValueError, "closed"):
            decode_atomic_publication_winner_v4(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
        with self.assertRaisesRegex(ValueError, "contiguous"):
            replace(
                winner,
                unit_assets=(replace(winner.unit_assets[0], unit_index=2),),
            )
        with self.assertRaisesRegex(ValueError, "canonical Unit ID"):
            replace(winner.unit_assets[0], asset_id="anything-not-du")
        with self.assertRaisesRegex(ValueError, "drifted from its request"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(winner, request_sha256=SHA_A),
            )
        with self.assertRaisesRegex(ValueError, "inserted count"):
            replace(winner, inserted_count=0)
        with self.assertRaisesRegex(ValueError, "mutation counts"):
            replace(winner, updated_count=1)
        with self.assertRaisesRegex(ValueError, "mutation counts"):
            replace(winner, deleted_count=1)
        with self.assertRaisesRegex(ValueError, "durable-base identity"):
            replace(
                winner,
                outbox_commit=replace(
                    winner.outbox_commit,
                    processing_run_id="run-other",
                ),
            )
        with self.assertRaisesRegex(ValueError, "outbox commit reference"):
            replace(
                winner.outbox_commit,
                events_sha256=SHA_D,
            )
        created = winner.outbox_commit.events[0]
        forged_payload = json.loads(created.canonical_payload_json)
        forged_payload["content_hash"] = SHA_D
        forged_created = seal_published_outbox_event_v4(
            event_id=created.event_id,
            event_sequence=created.event_sequence,
            event_kind=created.event_kind,
            change_kind=created.change_kind,
            subject_kind=created.subject_kind,
            subject_ref=created.subject_ref,
            document_id=created.document_id,
            processing_run_id=created.processing_run_id,
            asset_id=created.asset_id,
            canonical_payload_json=json.dumps(
                forged_payload, sort_keys=True, separators=(",", ":")
            ),
            occurred_at=created.occurred_at,
        )
        forged_outbox = seal_published_outbox_commit_reference_v4(
            events=(forged_created, winner.outbox_commit.events[1]),
        )
        with self.assertRaisesRegex(ValueError, "created outbox event"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(winner, outbox_commit=forged_outbox),
            )
        published_only = seal_published_outbox_commit_reference_v4(
            events=(winner.outbox_commit.events[1],),
        )
        with self.assertRaisesRegex(ValueError, "diff cardinality"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(winner, outbox_commit=published_only),
            )
        with self.assertRaisesRegex(ValueError, "durable-base projection"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(
                    winner,
                    durable_base_commit=replace(
                        winner.durable_base_commit,
                        durable_base_sha256=SHA_D,
                    ),
                ),
            )
        forged_units = (
            replace(winner.unit_assets[0], final_unit_row_sha256=SHA_D),
        )
        with self.assertRaisesRegex(ValueError, "row projection"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(
                    winner,
                    unit_assets=forged_units,
                    final_units_sha256=final_unit_rows_sha256_v4(forged_units),
                ),
            )
        forged_lineage = (
            replace(winner.unit_assets[0], lineage_row_sha256=SHA_D),
        )
        with self.assertRaisesRegex(ValueError, "row projection"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(
                    winner,
                    unit_assets=forged_lineage,
                    lineage_sha256=lineage_rows_sha256_v4(forged_lineage),
                ),
            )
        with self.assertRaisesRegex(ValueError, "aggregate projection"):
            validate_atomic_publication_winner_v4(
                request=request,
                winner=replace(winner, processing_run_row_sha256=SHA_D),
            )

    def test_previous_inventory_mechanically_closes_no_diff_and_removal(self) -> None:
        initial = _request()
        previous = _previous_active_unit(initial)
        request = _request(
            previous_active_run_id="run-old",
            previous_active_units=(previous,),
        )
        self.assertEqual(
            decode_atomic_publication_request_v4(request.canonical_bytes),
            request,
        )
        no_diff_published = _published_event(
            request,
            event_id="event-1",
            event_sequence=1,
            previous_active_run_id="run-old",
            created_count=0,
            removed_count=0,
            projection_changed_count=0,
            change_kind="observed",
        )
        no_diff_winner = _seal_winner(
            request,
            events=(no_diff_published,),
            previous_active_run_id="run-old",
        )
        self.assertEqual(no_diff_winner.updated_count, 0)
        self.assertEqual(no_diff_winner.deleted_count, 0)
        self.assertEqual(no_diff_winner.outbox_commit.event_count, 1)

        forged_removed = _removed_event(
            request,
            previous,
            event_id="event-forged-1",
            event_sequence=1,
        )
        forged_published = _published_event(
            request,
            event_id="event-forged-2",
            event_sequence=2,
            previous_active_run_id="run-old",
            created_count=0,
            removed_count=1,
            projection_changed_count=0,
            change_kind="materialized",
        )
        with self.assertRaisesRegex(ValueError, "mutation counts|diff cardinality"):
            _seal_winner(
                request,
                events=(forged_removed, forged_published),
                previous_active_run_id="run-old",
            )

        removed_previous = replace(previous, content_hash=SHA_A)
        removal_request = _request(
            previous_active_run_id="run-old",
            previous_active_units=(removed_previous,),
        )
        created = _created_event(
            removal_request,
            event_id="event-omit-1",
            event_sequence=1,
        )
        omitted_published = _published_event(
            removal_request,
            event_id="event-omit-2",
            event_sequence=2,
            previous_active_run_id="run-old",
            created_count=1,
            removed_count=0,
            projection_changed_count=0,
            change_kind="materialized",
        )
        with self.assertRaisesRegex(ValueError, "mutation counts|diff cardinality"):
            _seal_winner(
                removal_request,
                events=(created, omitted_published),
                previous_active_run_id="run-old",
            )

    def test_previous_run_cannot_omit_its_inventory(self) -> None:
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "complete Unit inventory",
        ):
            _request(previous_active_run_id="run-old")

    def test_previous_run_and_inventory_order_are_closed(self) -> None:
        initial = _request()
        previous = _previous_active_unit(initial)
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "cannot be the publication run",
        ):
            _request(
                previous_active_run_id="run-1",
                previous_active_units=(
                    replace(previous, processing_run_id="run-1"),
                ),
            )
        with self.assertRaisesRegex(
            WholeDocumentPublicationV4Error,
            "not contiguous",
        ):
            _request(
                previous_active_run_id="run-old",
                previous_active_units=(replace(previous, order_index=2),),
            )

    def test_previous_inventory_mechanically_derives_projection_change(self) -> None:
        initial = _request()
        previous = _previous_active_unit(initial)
        old_projection = json.loads(previous.canonical_query_projection_json)
        old_projection["applicability"] = "not_applicable"
        old_projection_json = json.dumps(
            old_projection,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = replace(
            previous,
            query_projection_hash=(
                "sha256:"
                + hashlib.sha256(old_projection_json.encode()).hexdigest()
            ),
            canonical_query_projection_json=old_projection_json,
        )
        request = _request(
            previous_active_run_id="run-old",
            previous_active_units=(previous,),
        )
        changed = _changed_event(
            request,
            previous,
            event_id="event-change-1",
            event_sequence=1,
            changed_fields=("applicability",),
        )
        published = _published_event(
            request,
            event_id="event-change-2",
            event_sequence=2,
            previous_active_run_id="run-old",
            created_count=0,
            removed_count=0,
            projection_changed_count=1,
            change_kind="observed",
        )
        winner = _seal_winner(
            request,
            events=(changed, published),
            previous_active_run_id="run-old",
        )
        self.assertEqual(winner.updated_count, 1)
        self.assertEqual(winner.deleted_count, 0)

        forged_changed = _changed_event(
            request,
            previous,
            event_id="event-forged-change-1",
            event_sequence=1,
            changed_fields=("title",),
        )
        with self.assertRaisesRegex(ValueError, "changed outbox event"):
            _seal_winner(
                request,
                events=(forged_changed, published),
                previous_active_run_id="run-old",
            )


def _publication_materialized_evidence() -> tuple[
    ResourceReservationV4,
    RemoteParseCheckpointV4,
    MaterializationIntentV4,
    LocalMaterializationReceiptV4,
    LocalMaterializationManifestV4,
    ProviderDocumentEnvelope,
]:
    lifecycle = _happy_path()
    original_intent = lifecycle["intent"]
    reservation = lifecycle["reservation"]
    original_chain = lifecycle["chain"]
    original_prepared = original_chain[0]
    original_reconciling = original_chain[1]
    original_submitted = original_chain[2]
    original_remote_terminal = original_chain[3]
    assert isinstance(original_intent, MaterializationIntentV4)
    assert isinstance(original_prepared, RemoteParseCheckpointV4)
    assert isinstance(original_reconciling, RemoteParseCheckpointV4)
    assert isinstance(original_submitted, RemoteParseCheckpointV4)
    assert isinstance(original_remote_terminal, RemoteParseCheckpointV4)
    elevated_credit = replace(
        reservation.reserved_credit,
        decoded_bytes=16_384,
        temp_disk_bytes=32_768,
        output_bytes=65_536,
    )
    reservation_input = encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=reservation.source_pdf_sha256,
            source_byte_count=reservation.source_byte_count,
            source_page_count=reservation.source_page_count,
            process_profile_sha256=reservation.process_profile_sha256,
            credit_policy_sha256=reservation.credit_policy_sha256,
            bucket=reservation.reservation_bucket,
            reservation=elevated_credit,
        )
    )
    allowance = PerAttemptResourceAllowance(
        reservation_input_sha256=reservation_input.sha256,
        reservation_input=reservation_input,
        limits=elevated_credit,
    )
    elevated_reservation = build_resource_reservation_v4(
        attempt_id=reservation.attempt_id,
        attempt_generation=reservation.attempt_generation,
        fence_identity=reservation.fence_identity,
        document_id=reservation.document_id,
        processing_run_id=reservation.processing_run_id,
        source_pdf_sha256=reservation.source_pdf_sha256,
        source_byte_count=reservation.source_byte_count,
        source_page_count=reservation.source_page_count,
        prepared_submission_identity_sha256=(
            reservation.prepared_submission_identity_sha256
        ),
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        process_profile_sha256=reservation.process_profile_sha256,
        credit_policy_sha256=reservation.credit_policy_sha256,
        reservation_bucket=reservation.reservation_bucket,
        reservation_input_sha256=reservation_input.sha256,
        reserved_credit=elevated_credit,
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=elevated_reservation,
        preparation_intent_sha256=original_prepared.preparation_intent_sha256,
        snapshot_receipt_sha256=original_prepared.snapshot_receipt_sha256,
        held_resource_credit=original_prepared.held_resource_credit,
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=original_reconciling.held_resource_credit,
        submission_intent_sha256=(
            original_reconciling.submission_intent_sha256
        ),
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=original_submitted.held_resource_credit,
        accepted_submission_sha256=(
            original_submitted.accepted_submission_sha256
        ),
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted,
        state="remote_terminal",
        held_resource_credit=original_remote_terminal.held_resource_credit,
        terminal_receipt_sha256=(
            original_remote_terminal.terminal_receipt_sha256
        ),
    )
    intent = build_materialization_intent_v4(
        reservation=elevated_reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=original_intent.terminal_receipt_sha256,
        remote_task_identity=original_intent.remote_task_identity,
        artifact_owner_identity=original_intent.artifact_owner_identity,
        artifact_sha256=original_intent.artifact_sha256,
        artifact_byte_count=original_intent.artifact_byte_count,
        provider_envelope_context=original_intent.provider_envelope_context,
        allowance_sha256=allowance.sha256,
        provider_capability_kind=original_intent.provider_capability_kind,
        provider_capability_sha256=original_intent.provider_capability_sha256,
        provider_capability_byte_count=(
            original_intent.provider_capability_byte_count
        ),
        output_dir_name=original_intent.output_dir_name,
        provider_envelope_relpath=original_intent.provider_envelope_relpath,
        output_manifest_relpath=original_intent.output_manifest_relpath,
        member_count_limit=10,
        uncompressed_byte_limit=16_384,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal,
        state="materializing",
        held_resource_credit=intent.held_resource_credit,
        materialization_intent_sha256=intent.sha256,
    )
    artifacts = tuple(
        ProviderArtifact(
            role=role,
            relative_path=relpath,
            sha256="sha256:" + digest * 64,
            size_bytes=10,
            media_type="application/json",
        )
        for role, relpath, digest in (
            ("content_list", "a_content_list.json", "1"),
            ("content_list_v2", "b_content_list_v2.json", "2"),
            ("middle_json", "c_middle.json", "3"),
            ("model_json", "d_model.json", "4"),
        )
    )
    provider_document = ProviderDocument(
        source_pdf_sha256=intent.source_pdf_sha256,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=(
            ProviderPage(page_index=0, page_size=(595.0, 842.0), blocks=()),
            ProviderPage(page_index=1, page_size=(595.0, 842.0), blocks=()),
        ),
        physical_table_segments=(),
        artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )
    context = intent.provider_envelope_context
    provider_envelope = ProviderDocumentEnvelope.build(
        document_id=context.document_id,
        artifact_owner_processing_run_id=context.processing_run_id,
        provider=context.provider,
        provider_document_id=context.provider_document_id,
        source_pdf_relpath=context.source_pdf_relpath,
        source_pdf_page_count=context.source_page_count,
        parser_artifact_root_relpath=context.parser_artifact_root_relpath,
        parser_target_identity=context.parser_target_identity,
        provider_document=provider_document,
    )
    provider_envelope_bytes = provider_document_envelope_to_bytes(
        provider_envelope
    )
    provider_envelope_sha256 = (
        "sha256:" + hashlib.sha256(provider_envelope_bytes).hexdigest()
    )
    payload_files = tuple(
        sorted(
            (
                *(
                    LocalMaterializationPayloadFileV4(
                        role="parser_artifact",
                        relpath=item.relative_path,
                        sha256=item.sha256,
                        byte_count=item.size_bytes,
                    )
                    for item in artifacts
                ),
                LocalMaterializationPayloadFileV4(
                    role="provider_envelope",
                    relpath=PROVIDER_DOCUMENT_FILENAME,
                    sha256=provider_envelope_sha256,
                    byte_count=len(provider_envelope_bytes),
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    payload_byte_count = sum(item.byte_count for item in payload_files)
    manifest = seal_local_materialization_manifest_v4(
        attempt_id=intent.attempt_id,
        fence_identity=intent.fence_identity,
        document_id=intent.document_id,
        processing_run_id=intent.processing_run_id,
        materialization_intent_sha256=intent.sha256,
        terminal_receipt_sha256=intent.terminal_receipt_sha256,
        remote_task_identity=intent.remote_task_identity,
        artifact_owner_identity=intent.artifact_owner_identity,
        artifact_sha256=intent.artifact_sha256,
        artifact_byte_count=intent.artifact_byte_count,
        source_pdf_sha256=intent.source_pdf_sha256,
        source_page_count=intent.source_page_count,
        parser_target_sha256=intent.parser_target_sha256,
        spool_relpath=intent.spool_relpath,
        output_relpath=intent.output_relpath,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        provider_envelope_sha256=provider_envelope_sha256,
        provider_envelope_byte_count=len(provider_envelope_bytes),
        observations=LocalMaterializationObservationsV4(
            member_count=len(artifacts),
            uncompressed_byte_count=40,
            decoded_byte_count=40,
            temporary_disk_peak_byte_count=80,
            output_file_count=len(payload_files),
            output_byte_count=payload_byte_count,
        ),
        payload_files=payload_files,
    )
    output_files = tuple(
        sorted(
            (
                LocalOutputFileV4(
                    relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
                    sha256=manifest.sha256,
                    byte_count=len(manifest.canonical_bytes),
                ),
                *(
                    LocalOutputFileV4(
                        relpath=item.relpath,
                        sha256=item.sha256,
                        byte_count=item.byte_count,
                    )
                    for item in payload_files
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    receipt = build_local_materialization_receipt_v4(
        intent=intent,
        manifest=manifest,
        source_page_count=intent.source_page_count,
        output_files=output_files,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count=manifest.observations.member_count,
        uncompressed_byte_count=manifest.observations.uncompressed_byte_count,
        decoded_byte_count=manifest.observations.decoded_byte_count,
        temporary_disk_peak_byte_count=(
            manifest.observations.temporary_disk_peak_byte_count
        ),
        file_fsync_completed=True,
        output_parent_fsync_completed=True,
        marker_removed=True,
        spool_part_absent=True,
        spool_part_owner_absent=True,
        staging_absent=True,
    )
    held_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=materializing.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=intent.artifact_byte_count,
        compressed_bytes=intent.artifact_byte_count,
        output_items=1,
        output_bytes=receipt.output_byte_count,
        output_pages=receipt.source_page_count,
        ack_items=1,
    )
    checkpoint = advance_remote_parse_checkpoint_v4(
        materializing,
        state="local_materialized",
        held_resource_credit=held_credit,
        local_materialization_receipt_sha256=receipt.sha256,
    )
    return (
        elevated_reservation,
        checkpoint,
        intent,
        receipt,
        manifest,
        provider_envelope,
    )


def _request(
    *,
    previous_active_run_id: str | None = None,
    previous_active_units: tuple[PreviousActiveUnitV4, ...] = (),
) -> AtomicPublicationRequestV4:
    reservation, checkpoint, intent, receipt, manifest, provider_envelope = (
        _publication_materialized_evidence()
    )
    upstream = seal_upstream_publication_evidence_v4(
        reservation=reservation,
        checkpoint=checkpoint,
        intent=intent,
        receipt=receipt,
        manifest=manifest,
        provider_envelope=provider_envelope,
    )
    payload = {"text": "hello"}
    hashes = compute_unit_hashes(
        payload_kind="text",
        payload=payload,
        title=None,
        heading_path=[],
        semantic_keys=None,
        section_keys=["section"],
        quality_status="ok",
        applicability="applicable",
        order_index=1,
    )
    locator = ProviderUnitLocator(
        provider_document_sha256=upstream.provider_document_sha256,
        unit_index=0,
        heading_chain=(),
        parts=(),
        evidence_only_block_source_indices=(),
        unbound_table_parts=(),
        evidence_artifacts=(),
        search_targets=(),
    )
    locator_json = json.dumps(
        provider_unit_locator_to_payload(locator),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    unit = seal_pre_id_unit_publication_v4(
        document_id="doc-1",
        processing_run_id="run-1",
        provider_document_id=upstream.provider_document_id,
        unit_index=1,
        payload_kind="text",
        heading_path=(),
        title=None,
        semantic_keys=None,
        section_keys=("section",),
        canonical_payload_json=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ),
        content_hash=hashes.content_hash,
        structure_hash=hashes.structure_hash,
        quality_status="ok",
        applicability="applicable",
        page_no=1,
        page_numbers=(1,),
        query_projection_hash=hashes.query_projection_hash,
        canonical_artifact_locator_json=locator_json,
        provider_locator_sha256=(
            "sha256:" + hashlib.sha256(locator_json.encode()).hexdigest()
        ),
    )
    route = SemanticRouteReceiptRowV3(
        processing_run_id="run-1",
        unit_order_index=1,
        provider_locator_sha256=unit.provider_locator_sha256,
        routed_draft_sha256=unit.routed_draft_sha256,
        receipt=replace(
            _fallback_receipt(1),
            contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
            semantic_keys=(),
            evidence=(),
        ),
    )
    semantic_projection = json.dumps(
        [semantic_route_receipt_row_v3_to_payload(route)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    projection = json.dumps(
        {
            "builder_rules_version": "provider-unit-builder.v1",
            "content_hash_aggregate": content_hash_aggregate([unit.content_hash]),
            "contract_version": "processing-run-publication.v4",
            "document_id": "doc-1",
            "document_units_relpath": (
                "derived/document_unit_snapshots/cninfo/000001/1225087169/"
                "run-1/document_units.v1.jsonl"
            ),
            "is_active": True,
            "parser_artifact_relpath": upstream.parser_artifact_root_relpath,
            "processing_run_id": "run-1",
            "provider_document_id": upstream.provider_document_id,
            "provider_document_relpath": (
                "derived/provider_documents/cninfo/000001/1225087169/"
                "run-1/provider_document.v1.json"
            ),
            "provider_document_sha256": upstream.provider_document_sha256,
            "run_kind": "parse",
            "semantic_route_receipts_contract_version": SEMANTIC_ROUTE_RECEIPT_V3,
            "semantic_route_receipts_sha256": (
                "sha256:" + hashlib.sha256(semantic_projection).hexdigest()
            ),
            "source_pdf_relpath": upstream.source_pdf_relpath,
            "source_pdf_sha256": upstream.source_pdf_sha256,
            "status": "succeeded",
            "structure_hash_aggregate": structure_hash_aggregate(
                [unit.structure_hash]
            ),
            "unit_count": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return seal_atomic_publication_request_v4(
        identity=PublicationAttemptIdentityV4(
            attempt_id="attempt-1",
            document_id="doc-1",
            processing_run_id="run-1",
            provider_document_id=upstream.provider_document_id,
            attempt_generation=1,
            fence_identity="fence-1",
            expected_attempt_state="local_materialized",
            expected_lifecycle_version=checkpoint.lifecycle_version,
            expected_checkpoint_sha256=checkpoint.sha256,
            expected_local_materialization_receipt_sha256=receipt.sha256,
            expected_previous_processing_run_id=previous_active_run_id,
        ),
        upstream_evidence=upstream,
        source_page_count=2,
        processing_run_projection_json=projection,
        processing_run_projection_sha256=(
            "sha256:" + hashlib.sha256(projection.encode()).hexdigest()
        ),
        semantic_route_receipts_contract_version=SEMANTIC_ROUTE_RECEIPT_V3,
        semantic_route_receipts=(route,),
        previous_active_units=previous_active_units,
        previous_active_units_sha256=previous_active_units_sha256_v4(
            previous_active_units
        ),
        units=(unit,),
    )


def _previous_active_unit(
    request: AtomicPublicationRequestV4,
) -> PreviousActiveUnitV4:
    unit = request.units[0]
    payload = json.loads(unit.canonical_payload_json)
    projection = query_projection(
        payload_kind=unit.payload_kind,
        title=unit.title,
        heading_path=list(unit.heading_path),
        semantic_keys=(
            None if unit.semantic_keys is None else list(unit.semantic_keys)
        ),
        section_keys=(
            None if unit.section_keys is None else list(unit.section_keys)
        ),
        quality_status=unit.quality_status,
        applicability=unit.applicability,
        payload=payload,
    )
    projection_json = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PreviousActiveUnitV4(
        asset_id="du_01K00000000000000000000001",
        processing_run_id="run-old",
        order_index=unit.unit_index,
        payload_kind=unit.payload_kind,
        heading_path=unit.heading_path,
        content_hash=unit.content_hash,
        query_projection_hash=(
            "sha256:" + hashlib.sha256(projection_json.encode()).hexdigest()
        ),
        canonical_query_projection_json=projection_json,
    )


def _created_event(
    request: AtomicPublicationRequestV4,
    *,
    event_id: str,
    event_sequence: int,
) -> PublishedOutboxEventV4:
    asset_id = "du_01K00000000000000000000000"
    unit = request.units[0]
    return seal_published_outbox_event_v4(
        event_id=event_id,
        event_sequence=event_sequence,
        event_kind="document_unit_created",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=asset_id,
        document_id=request.identity.document_id,
        processing_run_id=request.identity.processing_run_id,
        asset_id=asset_id,
        canonical_payload_json=json.dumps(
            {
                "content_hash": unit.content_hash,
                "new_asset_id": asset_id,
                "new_heading_path": list(unit.heading_path),
                "new_order_index": unit.unit_index,
                "new_processing_run_id": request.identity.processing_run_id,
                "payload_kind": unit.payload_kind,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _removed_event(
    request: AtomicPublicationRequestV4,
    previous: PreviousActiveUnitV4,
    *,
    event_id: str,
    event_sequence: int,
) -> PublishedOutboxEventV4:
    return seal_published_outbox_event_v4(
        event_id=event_id,
        event_sequence=event_sequence,
        event_kind="document_unit_removed",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=previous.asset_id,
        document_id=request.identity.document_id,
        processing_run_id=previous.processing_run_id,
        asset_id=previous.asset_id,
        canonical_payload_json=json.dumps(
            {
                "content_hash": previous.content_hash,
                "old_asset_id": previous.asset_id,
                "old_heading_path": list(previous.heading_path),
                "old_order_index": previous.order_index,
                "old_processing_run_id": previous.processing_run_id,
                "payload_kind": previous.payload_kind,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _changed_event(
    request: AtomicPublicationRequestV4,
    previous: PreviousActiveUnitV4,
    *,
    event_id: str,
    event_sequence: int,
    changed_fields: tuple[str, ...],
) -> PublishedOutboxEventV4:
    unit = request.units[0]
    asset_id = "du_01K00000000000000000000000"
    return seal_published_outbox_event_v4(
        event_id=event_id,
        event_sequence=event_sequence,
        event_kind="document_unit_projection_changed",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=asset_id,
        document_id=request.identity.document_id,
        processing_run_id=request.identity.processing_run_id,
        asset_id=asset_id,
        canonical_payload_json=json.dumps(
            {
                "changed_fields": list(changed_fields),
                "content_hash": unit.content_hash,
                "new_asset_id": asset_id,
                "new_query_projection_hash": unit.query_projection_hash,
                "old_asset_id": previous.asset_id,
                "old_query_projection_hash": previous.query_projection_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _published_event(
    request: AtomicPublicationRequestV4,
    *,
    event_id: str,
    event_sequence: int,
    previous_active_run_id: str | None,
    created_count: int,
    removed_count: int,
    projection_changed_count: int,
    change_kind: str,
) -> PublishedOutboxEventV4:
    publish_precommit_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
    projection = json.loads(request.processing_run_projection_json)
    return seal_published_outbox_event_v4(
        event_id=event_id,
        event_sequence=event_sequence,
        event_kind="processing_run_published",
        change_kind=change_kind,
        subject_kind="processing_run",
        subject_ref=request.identity.processing_run_id,
        document_id=request.identity.document_id,
        processing_run_id=request.identity.processing_run_id,
        asset_id=None,
        canonical_payload_json=json.dumps(
            {
                "content_hash_aggregate": projection["content_hash_aggregate"],
                "created_count": created_count,
                "previous_processing_run_id": previous_active_run_id,
                "projection_changed_count": projection_changed_count,
                "publish_committed_at": publish_precommit_at.isoformat(),
                "removed_count": removed_count,
                "source_identity": request.upstream_evidence.source_pdf_sha256,
                "source_page_count": request.source_page_count,
                "structure_hash": projection["structure_hash_aggregate"],
                "unit_count": len(request.units),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        occurred_at=publish_precommit_at,
    )


def _seal_winner(
    request: AtomicPublicationRequestV4,
    *,
    events: tuple[PublishedOutboxEventV4, ...],
    previous_active_run_id: str | None,
) -> AtomicPublicationWinnerV4:
    return seal_atomic_publication_winner_v4(
        request=request,
        asset_ids=("du_01K00000000000000000000000",),
        outbox_events=events,
        attempt_id=request.identity.attempt_id,
        fence_identity=request.identity.fence_identity,
        document_id=request.identity.document_id,
        processing_run_id=request.identity.processing_run_id,
        publish_attempt_generation=request.identity.attempt_generation,
        local_checkpoint_sha256=request.identity.expected_checkpoint_sha256,
        lifecycle_version_before=request.identity.expected_lifecycle_version,
        lifecycle_version_after=request.identity.expected_lifecycle_version + 1,
        request_sha256=request.request_sha256,
        upstream_evidence_sha256=request.upstream_evidence.evidence_sha256,
        previous_active_run_id=previous_active_run_id,
        publish_precommit_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def _winner(request: AtomicPublicationRequestV4) -> AtomicPublicationWinnerV4:
    created = _created_event(request, event_id="event-1", event_sequence=1)
    published = _published_event(
        request,
        event_id="event-2",
        event_sequence=2,
        previous_active_run_id=None,
        created_count=1,
        removed_count=0,
        projection_changed_count=0,
        change_kind="materialized",
    )
    return _seal_winner(
        request,
        events=(created, published),
        previous_active_run_id=None,
    )


if __name__ == "__main__":
    unittest.main()
