from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import asdict, replace

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.provider_document import ProviderPage
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    FailureReceiptV4,
    SnapshotReceiptV4,
    SubmissionAbsenceProofV4,
    SubmissionIntentV4,
    SupersessionReceiptV4,
    TerminalReceiptV4,
    build_preparation_intent_v4,
    decode_remote_parse_evidence_v4,
    encode_remote_parse_evidence_v4,
    validate_durable_remote_parse_evidence_bundle_v4,
    validate_remote_parse_evidence_bundle_v4,
    validate_superseding_checkpoint_seed_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CleanupResourceEntryV4,
    LocalCleanupResourceResultV4,
    LocalOutputFileV4,
    ProviderAckReceiptV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
    build_local_cleanup_receipt_v4,
    build_local_materialization_receipt_v4,
    build_materialization_intent_v4,
    build_resource_free_remote_parse_checkpoint_v4,
    build_resource_reservation_v4,
    local_output_files_sha256_v4,
    provider_ack_request_v4_bytes,
    provider_ack_request_v4_identity,
    validate_local_cleanup_plan_v4,
    validate_materialized_provider_evidence_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceCreditVector,
    encode_resource_reservation_input,
)
from tests.unit.test_provider_document_envelope import (
    _envelope as _provider_document_envelope,
)
from tests.unit.test_remote_parse_lifecycle_v4 import (
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    SHA_E,
    SHA_F,
    _ack_credit,
    _base,
    _happy_path,
    _local_credit,
    _provider_envelope_context,
    _reservation_credit,
    _reservation_input,
    _snapshot_credit,
    _submitted_credit,
    _terminal_credit,
)


class _LyingStr(str):
    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        return b"x"

    def __eq__(self, other: object) -> bool:
        return True


class _LyingInt(int):
    def __eq__(self, other: object) -> bool:
        return True

    def __le__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False


class RemoteParseEvidenceV4Tests(unittest.TestCase):
    def test_fixed_evidence_records_round_trip_strictly(self) -> None:
        records = _records()
        for value in records:
            with self.subTest(value=type(value).__name__):
                encoded = encode_remote_parse_evidence_v4(value)
                decoded = decode_remote_parse_evidence_v4(
                    encoded.kind, encoded.exact_bytes
                )
                self.assertEqual(decoded, encoded)
                payload = json.loads(encoded.exact_bytes)
                payload["future"] = True
                with self.assertRaisesRegex(ValueError, "closed"):
                    decode_remote_parse_evidence_v4(
                        encoded.kind,
                        json.dumps(
                            payload, sort_keys=True, separators=(",", ":")
                        ).encode(),
                    )

    def test_preparation_intent_is_write_ahead_and_closes_snapshot_namespace(self) -> None:
        reservation = _base()["reservation"]
        intent = build_preparation_intent_v4(
            reservation=reservation,
            parser_target_sha256=SHA_A,
        )
        self.assertEqual(intent.snapshot_relpath, reservation.snapshot_relpath)
        self.assertEqual(
            intent.snapshot_part_owner_relpath,
            reservation.snapshot_part_owner_relpath,
        )
        self.assertNotIn(b"claim", intent.canonical_bytes)
        self.assertNotIn(b"lease", intent.canonical_bytes)

    def test_ambiguous_submission_needs_exact_404_absence_proof(self) -> None:
        values = _failure_values()
        with self.assertRaisesRegex(ValueError, "404 absence proof"):
            FailureReceiptV4(**values)
        proof = SubmissionAbsenceProofV4(
            client_submit_key="submit-1",
            lookup_request_sha256=SHA_A,
            provider_protocol_version="mineru-task-protocol.v2",
            http_status=404,
            response_sha256=SHA_B,
            response_byte_count=2,
        )
        failure = FailureReceiptV4(
            **{**values, "submission_absence_proof": proof}
        )
        self.assertEqual(
            decode_remote_parse_evidence_v4(
                "failure_receipt", failure.canonical_bytes
            ).value,
            failure,
        )
        with self.assertRaisesRegex(ValueError, "exact HTTP 404"):
            replace(proof, http_status=410)
        with self.assertRaisesRegex(ValueError, "remote failure evidence"):
            FailureReceiptV4(
                **{
                    **values,
                    "outcome": "remote_failure",
                    "submission_absence_proof": None,
                    "accepted_submission_receipt_sha256": SHA_C,
                    "terminal_receipt_sha256": SHA_D,
                }
            )

    def test_accepted_failures_require_submission_attempt(self) -> None:
        values = _failure_values()
        for outcome, extras in (
            (
                "remote_failure",
                {
                    "accepted_submission_receipt_sha256": SHA_C,
                },
            ),
            (
                "local_failure",
                {
                    "accepted_submission_receipt_sha256": SHA_C,
                    "terminal_receipt_sha256": SHA_D,
                },
            ),
        ):
            with (
                self.subTest(outcome=outcome),
                self.assertRaisesRegex(
                    ValueError,
                    f"{outcome.split('_')[0]} failure evidence",
                ),
            ):
                FailureReceiptV4(
                    **{
                        **values,
                        "outcome": outcome,
                        "submission_was_attempted": False,
                        **extras,
                    }
                )

    def test_preparation_failure_has_exact_resource_free_source_state(self) -> None:
        failure = FailureReceiptV4(
            **{
                **_failure_values(),
                "outcome": "preparation_failure",
                "source_state": "not_prepared",
                "source_lifecycle_version": 0,
                "source_checkpoint_sha256": None,
                "submission_was_attempted": False,
            }
        )
        self.assertEqual(failure.source_state, "not_prepared")
        with self.assertRaisesRegex(ValueError, "source state is not resource-free"):
            replace(failure, source_state="FORGED_UNKNOWN_STATE")

    def test_resource_free_supersession_cannot_cite_source_checkpoint(self) -> None:
        reservation = _base()["reservation"]
        (
            superseding_reservation,
            superseding_preparation,
            superseding_snapshot,
            superseding_checkpoint,
        ) = _superseding_checkpoint_seed()
        receipt = SupersessionReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            source_document_id="doc-1",
            source_attempt_generation=1,
            source_state="not_prepared",
            source_lifecycle_version=0,
            source_checkpoint_sha256=None,
            superseding_attempt_id="attempt-2",
            superseding_attempt_generation=2,
            superseding_document_id="doc-1",
            superseding_checkpoint_sha256=superseding_checkpoint.sha256,
            reason_code="newer_attempt",
        )
        checkpoint = build_resource_free_remote_parse_checkpoint_v4(
            state="superseded",
            attempt_id="attempt-1",
            attempt_generation=1,
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            source_pdf_sha256=SHA_A,
            source_byte_count=100,
            source_page_count=2,
            request_sha256=reservation.request_sha256,
            runtime_epoch_sha256=reservation.runtime_epoch_sha256,
            process_profile_sha256=reservation.process_profile_sha256,
            credit_policy_sha256=reservation.credit_policy_sha256,
            reservation_input_sha256=reservation.reservation_input_sha256,
            supersession_receipt_sha256=receipt.sha256,
        )
        encoded = (encode_remote_parse_evidence_v4(receipt),)
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=encoded,
            reservation=None,
            superseding_checkpoint=superseding_checkpoint,
            superseding_reservation=superseding_reservation,
            superseding_preparation_intent=superseding_preparation,
            superseding_snapshot_receipt=superseding_snapshot,
        )
        with self.assertRaisesRegex(ValueError, "invented a source reservation"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )

        drifted = replace(receipt, source_checkpoint_sha256=SHA_A)
        drifted_checkpoint = replace(
            checkpoint,
            supersession_receipt_sha256=drifted.sha256,
        )
        with self.assertRaisesRegex(ValueError, "resource-free supersession"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=drifted_checkpoint,
                evidence=(encode_remote_parse_evidence_v4(drifted),),
                reservation=None,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )
        forged_state = replace(receipt, source_state="FORGED_UNKNOWN_STATE")
        with self.assertRaisesRegex(
            ValueError,
            "resource-free supersession source state",
        ):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    supersession_receipt_sha256=forged_state.sha256,
                ),
                evidence=(encode_remote_parse_evidence_v4(forged_state),),
                reservation=None,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )
        with self.assertRaisesRegex(ValueError, "supersede itself"):
            replace(receipt, superseding_attempt_id=receipt.attempt_id)
        with self.assertRaisesRegex(ValueError, "generation did not advance"):
            replace(receipt, superseding_attempt_generation=1)
        with self.assertRaisesRegex(ValueError, "crossed the document chain"):
            replace(receipt, superseding_document_id="other-document")
        with self.assertRaisesRegex(ValueError, "drifted or reused"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=None,
                superseding_checkpoint=replace(
                    superseding_checkpoint,
                    attempt_id="attempt-3",
                ),
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )

        fake_generation = replace(
            superseding_checkpoint,
            attempt_generation=999,
        )
        fake_generation_receipt = replace(
            receipt,
            superseding_attempt_generation=999,
            superseding_checkpoint_sha256=fake_generation.sha256,
        )
        with self.assertRaisesRegex(ValueError, "checkpoint immutable facts"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    supersession_receipt_sha256=fake_generation_receipt.sha256,
                ),
                evidence=(encode_remote_parse_evidence_v4(fake_generation_receipt),),
                reservation=None,
                superseding_checkpoint=fake_generation,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )
        source_preparation, source_snapshot = _records()[:2]
        with self.assertRaisesRegex(ValueError, "checkpoint immutable facts"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=None,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=reservation,
                superseding_preparation_intent=source_preparation,
                superseding_snapshot_receipt=source_snapshot,
            )

        resource_free_seed = build_resource_free_remote_parse_checkpoint_v4(
            state="preparation_failed",
            attempt_id=superseding_reservation.attempt_id,
            attempt_generation=superseding_reservation.attempt_generation,
            fence_identity=superseding_reservation.fence_identity,
            document_id=superseding_reservation.document_id,
            processing_run_id=superseding_reservation.processing_run_id,
            source_pdf_sha256=superseding_reservation.source_pdf_sha256,
            source_byte_count=superseding_reservation.source_byte_count,
            source_page_count=superseding_reservation.source_page_count,
            request_sha256=superseding_reservation.request_sha256,
            runtime_epoch_sha256=superseding_reservation.runtime_epoch_sha256,
            process_profile_sha256=(
                superseding_reservation.process_profile_sha256
            ),
            credit_policy_sha256=superseding_reservation.credit_policy_sha256,
            reservation_input_sha256=(
                superseding_reservation.reservation_input_sha256
            ),
            failure_receipt_sha256=SHA_F,
        )
        with self.assertRaisesRegex(ValueError, "resource-free"):
            validate_superseding_checkpoint_seed_evidence_v4(
                checkpoint=resource_free_seed,
                reservation=superseding_reservation,
                preparation_intent=superseding_preparation,
                snapshot_receipt=superseding_snapshot,
            )
        for extra in (
            {"resourceful_checkpoint_history": (superseding_checkpoint,)},
            {"cleanup_source_checkpoint": superseding_checkpoint},
        ):
            with (
                self.subTest(extra=tuple(extra)),
                self.assertRaisesRegex(ValueError, "resource-free lifecycle"),
            ):
                validate_durable_remote_parse_evidence_bundle_v4(
                    checkpoint=checkpoint,
                    evidence=encoded,
                    reservation=None,
                    superseding_checkpoint=superseding_checkpoint,
                    superseding_reservation=superseding_reservation,
                    superseding_preparation_intent=superseding_preparation,
                    superseding_snapshot_receipt=superseding_snapshot,
                    **extra,
                )

    def test_resource_free_preparation_failure_has_no_source_reservation(
        self,
    ) -> None:
        reservation = _base()["reservation"]
        failure = FailureReceiptV4(
            **{
                **_failure_values(),
                "outcome": "preparation_failure",
                "source_state": "not_prepared",
                "source_lifecycle_version": 0,
                "source_checkpoint_sha256": None,
                "submission_was_attempted": False,
            }
        )
        checkpoint = build_resource_free_remote_parse_checkpoint_v4(
            state="preparation_failed",
            attempt_id="attempt-1",
            attempt_generation=1,
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            source_pdf_sha256=SHA_A,
            source_byte_count=100,
            source_page_count=2,
            request_sha256=reservation.request_sha256,
            runtime_epoch_sha256=reservation.runtime_epoch_sha256,
            process_profile_sha256=reservation.process_profile_sha256,
            credit_policy_sha256=reservation.credit_policy_sha256,
            reservation_input_sha256=reservation.reservation_input_sha256,
            failure_receipt_sha256=failure.sha256,
        )
        evidence = (encode_remote_parse_evidence_v4(failure),)
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=evidence,
            reservation=None,
        )
        with self.assertRaisesRegex(ValueError, "invented a source reservation"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=evidence,
                reservation=reservation,
            )

    def test_durable_validator_omits_filesystem_proof_without_weakening_full(
        self,
    ) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            _manifest,
            _provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        evidence = tuple(
            encode_remote_parse_evidence_v4(value) for value in values
        )
        common = {
            "checkpoint": checkpoint,
            "evidence": evidence,
            "reservation": reservation,
            "cleanup_source_checkpoint": cleanup_source,
            "resourceful_checkpoint_history": history,
            "cleanup_pending_checkpoint": cleanup_pending,
            "ack_pending_checkpoint": ack_pending,
        }
        validate_durable_remote_parse_evidence_bundle_v4(**common)
        with self.assertRaisesRegex(ValueError, "exact manifest or envelope"):
            validate_remote_parse_evidence_bundle_v4(**common)

        prepared = history[0]
        with self.assertRaisesRegex(ValueError, "lacks exact reservation"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=prepared,
                evidence=evidence[:2],
                reservation=None,
                resourceful_checkpoint_history=(prepared,),
            )

    def test_checkpoint_rejects_future_evidence(self) -> None:
        prepared = _base()["prepared"]
        with self.assertRaisesRegex(ValueError, "future or conflicting"):
            replace(prepared, ack_receipt_sha256=SHA_A)

    def test_accepted_submission_rejects_cross_origin_resume_urls(self) -> None:
        accepted = _records()[3]
        with self.assertRaisesRegex(ValueError, "closed HTTP origin"):
            replace(
                accepted,
                result_url="https://other.invalid/task-1/result",
            )
        with self.assertRaisesRegex(ValueError, "closed HTTP origin"):
            replace(
                accepted,
                result_url="https://provider.invalid/task-1/result?token=secret",
            )

    def test_accepted_submission_closes_secret_envelope_bounds(self) -> None:
        accepted = _records()[3]
        rejected = (
            {"secret_kind": "x" * 129},
            {"secret_kind": "界" * 43},
            {"secret_kind": _LyingStr("界" * 1_000)},
            {"token_byte_count": 65_537},
            {"token_byte_count": _LyingInt(10**30)},
            {"token_sha256": _LyingStr(SHA_E)},
            {"contract_version": _LyingStr("not-v4")},
        )
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    replace(accepted, **overrides)

    def test_prepared_bundle_binds_typed_preparation_and_snapshot(self) -> None:
        reservation = _base()["reservation"]
        preparation, snapshot = _records()[:2]
        checkpoint = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=preparation.sha256,
            snapshot_receipt_sha256=snapshot.sha256,
            held_resource_credit=_base()["prepared"].held_resource_credit,
        )
        encoded = tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (preparation, snapshot)
        )
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=encoded,
            reservation=reservation,
            resourceful_checkpoint_history=(checkpoint,),
        )
        with self.assertRaisesRegex(ValueError, "lacks exact checkpoint history"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
            )
        with self.assertRaisesRegex(ValueError, "hash drifted"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(checkpoint, snapshot_receipt_sha256=SHA_F),
                evidence=encoded,
                reservation=reservation,
                resourceful_checkpoint_history=(checkpoint,),
            )
        forged_preparation = replace(preparation, reservation_sha256=SHA_F)
        forged_checkpoint = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=forged_preparation.sha256,
            held_resource_credit=_snapshot_credit(),
        )
        with self.assertRaisesRegex(ValueError, "drifted from reservation"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=forged_checkpoint,
                evidence=(encode_remote_parse_evidence_v4(forged_preparation),),
                reservation=reservation,
                resourceful_checkpoint_history=(forged_checkpoint,),
            )

    def test_claimed_preflight_can_durably_add_first_snapshot(self) -> None:
        reservation = _base()["reservation"]
        preparation, snapshot, submission = _records()[:3]
        prepared = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=preparation.sha256,
            held_resource_credit=_snapshot_credit(),
        )
        reconciling = advance_remote_parse_checkpoint_v4(
            prepared,
            state="reconciling",
            held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
            snapshot_receipt_sha256=snapshot.sha256,
            submission_intent_sha256=submission.sha256,
        )
        evidence = tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (preparation, snapshot, submission)
        )
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=reconciling,
            evidence=evidence,
            reservation=reservation,
            resourceful_checkpoint_history=(prepared, reconciling),
        )

        with self.assertRaisesRegex(ValueError, "kind set drifted"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=reconciling,
                evidence=(evidence[0], evidence[2]),
                reservation=reservation,
                resourceful_checkpoint_history=(prepared, reconciling),
            )

    def test_bundle_rejects_tuple_subclass_with_flipping_iteration(self) -> None:
        reservation = _base()["reservation"]
        preparation, snapshot, submission, accepted = _records()[:4]
        foreign_accepted = replace(
            accepted,
            attempt_id="foreign-attempt",
            fence_identity="foreign-fence",
        )
        prepared = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=preparation.sha256,
            snapshot_receipt_sha256=snapshot.sha256,
            held_resource_credit=_snapshot_credit(),
        )
        reconciling = advance_remote_parse_checkpoint_v4(
            prepared,
            state="reconciling",
            held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
            submission_intent_sha256=submission.sha256,
        )
        submitted = advance_remote_parse_checkpoint_v4(
            reconciling,
            state="submitted",
            held_resource_credit=_submitted_credit(),
            accepted_submission_sha256=foreign_accepted.sha256,
        )
        encoded = tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (preparation, snapshot, submission, foreign_accepted)
        )
        with self.assertRaisesRegex(ValueError, "attempt drifted"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=submitted,
                evidence=encoded,
                reservation=reservation,
            )

        class FlippingTuple(tuple):
            def __new__(cls, values):
                item = super().__new__(cls, values)
                item.calls = 0
                return item

            def __iter__(self):
                self.calls += 1
                if self.calls <= 2:
                    return super().__iter__()
                return iter(())

        flipping = FlippingTuple(encoded)
        with self.assertRaisesRegex(ValueError, "bundle type is invalid"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=submitted,
                evidence=flipping,
                reservation=reservation,
            )
        self.assertEqual(flipping.calls, 0)

    def test_materializing_and_local_credit_close_exact_evidence(self) -> None:
        (
            _,
            reservation,
            values,
            _,
            manifest,
            provider_envelope,
            _,
            _,
            _,
        ) = _typed_happy_bundle()
        preparation, snapshot, submission, accepted, terminal, intent, local = values[:7]
        prepared = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=preparation.sha256,
            snapshot_receipt_sha256=snapshot.sha256,
            held_resource_credit=_snapshot_credit(),
        )
        reconciling = advance_remote_parse_checkpoint_v4(
            prepared,
            state="reconciling",
            held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
            submission_intent_sha256=submission.sha256,
        )
        submitted = advance_remote_parse_checkpoint_v4(
            reconciling,
            state="submitted",
            held_resource_credit=_submitted_credit(),
            accepted_submission_sha256=accepted.sha256,
        )
        remote_terminal = advance_remote_parse_checkpoint_v4(
            submitted,
            state="remote_terminal",
            held_resource_credit=_terminal_credit(),
            terminal_receipt_sha256=terminal.sha256,
        )
        materializing = advance_remote_parse_checkpoint_v4(
            remote_terminal,
            state="materializing",
            held_resource_credit=intent.held_resource_credit,
            materialization_intent_sha256=intent.sha256,
        )
        materializing_evidence = tuple(
            encode_remote_parse_evidence_v4(value) for value in values[:6]
        )
        materializing_history = (
            prepared,
            reconciling,
            submitted,
            remote_terminal,
            materializing,
        )
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=materializing,
            evidence=materializing_evidence,
            reservation=reservation,
            resourceful_checkpoint_history=materializing_history,
        )
        with self.assertRaisesRegex(ValueError, "lacks exact checkpoint history"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=materializing,
                evidence=materializing_evidence,
                reservation=reservation,
            )
        forged_predecessor = replace(
            materializing,
            previous_checkpoint_sha256=SHA_F,
        )
        with self.assertRaisesRegex(ValueError, "history drifted from exact replay"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=forged_predecessor,
                evidence=materializing_evidence,
                reservation=reservation,
                resourceful_checkpoint_history=(
                    *materializing_history[:-1],
                    forged_predecessor,
                ),
            )
        with self.assertRaisesRegex(ValueError, "credit drifted from exact evidence"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    materializing,
                    held_resource_credit=replace(
                        materializing.held_resource_credit,
                        decoded_bytes=materializing.held_resource_credit.decoded_bytes - 1,
                    ),
                ),
                evidence=materializing_evidence,
                reservation=reservation,
                resourceful_checkpoint_history=materializing_history,
            )
        local_checkpoint = advance_remote_parse_checkpoint_v4(
            materializing,
            state="local_materialized",
            held_resource_credit=_local_credit(local.output_byte_count),
            local_materialization_receipt_sha256=local.sha256,
        )
        local_evidence = tuple(
            encode_remote_parse_evidence_v4(value) for value in values[:7]
        )
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=local_checkpoint,
            evidence=local_evidence,
            reservation=reservation,
            resourceful_checkpoint_history=(*materializing_history, local_checkpoint),
            local_materialization_manifest=manifest,
            provider_envelope=provider_envelope,
        )
        published = advance_remote_parse_checkpoint_v4(
            local_checkpoint,
            state="publish_committed",
            held_resource_credit=local_checkpoint.held_resource_credit,
            publication_winner_sha256=SHA_E,
        )
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=published,
            evidence=local_evidence,
            reservation=reservation,
            resourceful_checkpoint_history=(
                *materializing_history,
                local_checkpoint,
                published,
            ),
            local_materialization_manifest=manifest,
            provider_envelope=provider_envelope,
        )
        with self.assertRaisesRegex(ValueError, "credit drifted from exact evidence"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    local_checkpoint,
                    held_resource_credit=replace(
                        local_checkpoint.held_resource_credit,
                        output_bytes=local_checkpoint.held_resource_credit.output_bytes + 1,
                    ),
                ),
                evidence=local_evidence,
                reservation=reservation,
                resourceful_checkpoint_history=(
                    *materializing_history,
                    local_checkpoint,
                ),
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_full_success_bundle_closes_cleanup_ack_and_final_checkpoint(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        encoded = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=encoded,
            reservation=reservation,
            cleanup_source_checkpoint=cleanup_source,
            resourceful_checkpoint_history=history,
            cleanup_pending_checkpoint=cleanup_pending,
            ack_pending_checkpoint=ack_pending,
            local_materialization_manifest=manifest,
            provider_envelope=provider_envelope,
        )
        with self.assertRaisesRegex(ValueError, "lacks exact manifest"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
            )
        with self.assertRaisesRegex(ValueError, "lacks its exact source"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

        ack = values[-1]
        bad_request = provider_ack_request_v4_bytes(
            accepted_submission_sha256=ack.accepted_submission_sha256,
            ack_pending_checkpoint_sha256=ack.ack_pending_checkpoint_sha256,
            attempt_id=ack.attempt_id,
            cleanup_plan_sha256=ack.cleanup_plan_sha256,
            cleanup_receipt_sha256=ack.cleanup_receipt_sha256,
            document_id=ack.document_id,
            fence_identity=ack.fence_identity,
            outcome=ack.outcome,
            processing_run_id=ack.processing_run_id,
            provider_protocol_version="other-protocol.v1",
            remote_task_identity=ack.remote_task_identity,
            result_owner_identity=ack.result_owner_identity,
            terminal_receipt_sha256=ack.terminal_receipt_sha256,
        )
        bad_request_sha256 = "sha256:" + hashlib.sha256(bad_request).hexdigest()
        bad_ack = replace(
            ack,
            provider_protocol_version="other-protocol.v1",
            ack_request_sha256=bad_request_sha256,
            request_identity=provider_ack_request_v4_identity(
                bad_request_sha256
            ),
        )
        bad_encoded = (*encoded[:-1], encode_remote_parse_evidence_v4(bad_ack))
        with self.assertRaisesRegex(ValueError, "provider ACK evidence chain"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    ack_receipt_sha256=bad_encoded[-1].sha256,
                ),
                evidence=bad_encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_materialized_evidence_rejects_self_consistent_five_byte_manifest(self) -> None:
        _, _, values, _, manifest, provider_envelope, _, _, _ = (
            _typed_happy_bundle()
        )
        intent = values[5]
        receipt = values[6]
        forged_files = tuple(
            replace(item, sha256=SHA_F, byte_count=5)
            if item.relpath == LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME
            else item
            for item in receipt.output_files
        )
        forged_receipt = replace(
            receipt,
            output_files=forged_files,
            output_byte_count=sum(item.byte_count for item in forged_files),
            output_files_sha256=local_output_files_sha256_v4(forged_files),
            output_manifest_sha256=SHA_F,
            output_manifest_byte_count=5,
        )
        with self.assertRaisesRegex(ValueError, "manifest bytes do not close"):
            validate_materialized_provider_evidence_v4(
                intent=intent,
                receipt=forged_receipt,
                manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_final_bundle_requires_exact_pending_checkpoint_witnesses(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        encoded = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        common = {
            "checkpoint": checkpoint,
            "evidence": encoded,
            "reservation": reservation,
            "cleanup_source_checkpoint": cleanup_source,
            "resourceful_checkpoint_history": history,
            "local_materialization_manifest": manifest,
            "provider_envelope": provider_envelope,
        }
        with self.assertRaisesRegex(ValueError, "cleanup-receipt evidence"):
            validate_remote_parse_evidence_bundle_v4(
                **common,
                ack_pending_checkpoint=ack_pending,
            )
        with self.assertRaisesRegex(ValueError, "accepted cleanup chain"):
            validate_remote_parse_evidence_bundle_v4(
                **common,
                cleanup_pending_checkpoint=cleanup_pending,
            )

        ack = values[-1]
        forged_ack_pending_sha256 = "sha256:" + "9" * 64
        forged_ack_request = provider_ack_request_v4_bytes(
            accepted_submission_sha256=ack.accepted_submission_sha256,
            ack_pending_checkpoint_sha256=forged_ack_pending_sha256,
            attempt_id=ack.attempt_id,
            cleanup_plan_sha256=ack.cleanup_plan_sha256,
            cleanup_receipt_sha256=ack.cleanup_receipt_sha256,
            document_id=ack.document_id,
            fence_identity=ack.fence_identity,
            outcome=ack.outcome,
            processing_run_id=ack.processing_run_id,
            provider_protocol_version=ack.provider_protocol_version,
            remote_task_identity=ack.remote_task_identity,
            result_owner_identity=ack.result_owner_identity,
            terminal_receipt_sha256=ack.terminal_receipt_sha256,
        )
        forged_ack_request_sha256 = (
            "sha256:" + hashlib.sha256(forged_ack_request).hexdigest()
        )
        forged_ack = replace(
            ack,
            ack_pending_checkpoint_sha256=forged_ack_pending_sha256,
            ack_request_sha256=forged_ack_request_sha256,
            request_identity=provider_ack_request_v4_identity(
                forged_ack_request_sha256
            ),
        )
        with self.assertRaisesRegex(ValueError, "provider ACK evidence"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    previous_checkpoint_sha256=forged_ack_pending_sha256,
                    ack_receipt_sha256=forged_ack.sha256,
                ),
                evidence=(*encoded[:-1], encode_remote_parse_evidence_v4(forged_ack)),
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

        cleanup_receipt = values[-2]
        forged_cleanup_receipt = replace(
            cleanup_receipt,
            cleanup_pending_checkpoint_sha256="sha256:" + "8" * 64,
        )
        forged_cleanup_ack_request = provider_ack_request_v4_bytes(
            accepted_submission_sha256=ack.accepted_submission_sha256,
            ack_pending_checkpoint_sha256=ack.ack_pending_checkpoint_sha256,
            attempt_id=ack.attempt_id,
            cleanup_plan_sha256=ack.cleanup_plan_sha256,
            cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
            document_id=ack.document_id,
            fence_identity=ack.fence_identity,
            outcome=ack.outcome,
            processing_run_id=ack.processing_run_id,
            provider_protocol_version=ack.provider_protocol_version,
            remote_task_identity=ack.remote_task_identity,
            result_owner_identity=ack.result_owner_identity,
            terminal_receipt_sha256=ack.terminal_receipt_sha256,
        )
        forged_cleanup_request_sha256 = (
            "sha256:" + hashlib.sha256(forged_cleanup_ack_request).hexdigest()
        )
        forged_cleanup_ack = replace(
            ack,
            cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
            ack_request_sha256=forged_cleanup_request_sha256,
            request_identity=provider_ack_request_v4_identity(
                forged_cleanup_request_sha256
            ),
        )
        with self.assertRaisesRegex(ValueError, "exact cleanup-pending"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
                    ack_receipt_sha256=forged_cleanup_ack.sha256,
                ),
                evidence=(
                    *encoded[:-2],
                    encode_remote_parse_evidence_v4(forged_cleanup_receipt),
                    encode_remote_parse_evidence_v4(forged_cleanup_ack),
                ),
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_remote_failure_final_replays_zero_result_credit(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_remote_failure_bundle(provider_result_bytes=0)
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=tuple(
                encode_remote_parse_evidence_v4(value) for value in values
            ),
            reservation=reservation,
            cleanup_source_checkpoint=cleanup_source,
            resourceful_checkpoint_history=history,
            cleanup_pending_checkpoint=cleanup_pending,
            ack_pending_checkpoint=ack_pending,
        )

        with self.assertRaisesRegex(ValueError, "invented provider result"):
            _typed_remote_failure_bundle(provider_result_bytes=999)

    def test_cleanup_pending_replays_source_credit(self) -> None:
        (
            _,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            _,
            _,
            history,
        ) = _typed_happy_bundle()
        forged_source = replace(
            cleanup_source,
            held_resource_credit=replace(
                cleanup_source.held_resource_credit,
                output_bytes=(
                    cleanup_source.held_resource_credit.output_bytes + 1
                ),
            ),
        )
        cleanup_plan = values[-3]
        forged_plan = replace(
            cleanup_plan,
            source_checkpoint_sha256=forged_source.sha256,
        )
        forged_cleanup_pending = advance_remote_parse_checkpoint_v4(
            forged_source,
            state="cleanup_pending",
            held_resource_credit=forged_source.held_resource_credit,
            cleanup_plan_sha256=forged_plan.sha256,
        )
        cleanup_receipt = values[-2]
        forged_cleanup_receipt = build_local_cleanup_receipt_v4(
            plan=forged_plan,
            cleanup_pending_checkpoint=forged_cleanup_pending,
            results=cleanup_receipt.results,
        )
        forged_ack_pending = advance_remote_parse_checkpoint_v4(
            forged_cleanup_pending,
            state="ack_pending",
            held_resource_credit=_ack_credit(),
            cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
        )
        ack = values[-1]
        forged_ack_request = provider_ack_request_v4_bytes(
            accepted_submission_sha256=ack.accepted_submission_sha256,
            ack_pending_checkpoint_sha256=forged_ack_pending.sha256,
            attempt_id=ack.attempt_id,
            cleanup_plan_sha256=forged_plan.sha256,
            cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
            document_id=ack.document_id,
            fence_identity=ack.fence_identity,
            outcome=ack.outcome,
            processing_run_id=ack.processing_run_id,
            provider_protocol_version=ack.provider_protocol_version,
            remote_task_identity=ack.remote_task_identity,
            result_owner_identity=ack.result_owner_identity,
            terminal_receipt_sha256=ack.terminal_receipt_sha256,
        )
        forged_ack_request_sha256 = (
            "sha256:" + hashlib.sha256(forged_ack_request).hexdigest()
        )
        forged_ack = replace(
            ack,
            ack_pending_checkpoint_sha256=forged_ack_pending.sha256,
            cleanup_plan_sha256=forged_plan.sha256,
            cleanup_receipt_sha256=forged_cleanup_receipt.sha256,
            ack_request_sha256=forged_ack_request_sha256,
            request_identity=provider_ack_request_v4_identity(
                forged_ack_request_sha256
            ),
        )
        forged_final = advance_remote_parse_checkpoint_v4(
            forged_ack_pending,
            state="acked",
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=forged_ack.sha256,
        )
        with self.assertRaisesRegex(ValueError, "checkpoint history"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=forged_final,
                evidence=tuple(
                    encode_remote_parse_evidence_v4(value)
                    for value in (
                        *values[:7],
                        forged_plan,
                        forged_cleanup_receipt,
                        forged_ack,
                    )
                ),
                reservation=reservation,
                cleanup_source_checkpoint=forged_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=forged_cleanup_pending,
                ack_pending_checkpoint=forged_ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_full_dag_rejects_forged_cleanup_source_predecessor(self) -> None:
        (
            _,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            _,
            _,
            history,
        ) = _typed_happy_bundle()
        forged_source = replace(
            cleanup_source,
            previous_checkpoint_sha256=SHA_F,
        )
        (
            forged_final,
            forged_values,
            forged_cleanup_pending,
            forged_ack_pending,
        ) = _reclose_happy_cleanup_dag(
            values=values,
            cleanup_source=forged_source,
        )
        with self.assertRaisesRegex(ValueError, "history drifted from exact replay"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=forged_final,
                evidence=tuple(
                    encode_remote_parse_evidence_v4(value)
                    for value in forged_values
                ),
                reservation=reservation,
                cleanup_source_checkpoint=forged_source,
                resourceful_checkpoint_history=(*history[:-1], forged_source),
                cleanup_pending_checkpoint=forged_cleanup_pending,
                ack_pending_checkpoint=forged_ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_cleanup_plan_requires_exact_resourceful_history(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        encoded = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        common = {
            "checkpoint": checkpoint,
            "evidence": encoded,
            "reservation": reservation,
            "cleanup_source_checkpoint": cleanup_source,
            "cleanup_pending_checkpoint": cleanup_pending,
            "ack_pending_checkpoint": ack_pending,
            "local_materialization_manifest": manifest,
            "provider_envelope": provider_envelope,
        }
        with self.assertRaisesRegex(ValueError, "lacks exact checkpoint history"):
            validate_remote_parse_evidence_bundle_v4(**common)

    def test_cleanup_history_is_complete_and_ordered(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        common = {
            "checkpoint": checkpoint,
            "evidence": tuple(
                encode_remote_parse_evidence_v4(value) for value in values
            ),
            "reservation": reservation,
            "cleanup_source_checkpoint": cleanup_source,
            "cleanup_pending_checkpoint": cleanup_pending,
            "ack_pending_checkpoint": ack_pending,
            "local_materialization_manifest": manifest,
            "provider_envelope": provider_envelope,
        }
        cases = (
            (history[:-1], "length drifted"),
            ((*history, cleanup_source), "length drifted"),
            (
                (*history[:3], history[4], history[3], *history[5:]),
                "exact replay",
            ),
        )
        for candidate, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                validate_remote_parse_evidence_bundle_v4(
                    **common,
                    resourceful_checkpoint_history=candidate,
                )

    def test_cleanup_history_is_foreign_root_and_winner_bound(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        common = {
            "checkpoint": checkpoint,
            "evidence": tuple(
                encode_remote_parse_evidence_v4(value) for value in values
            ),
            "reservation": reservation,
            "cleanup_source_checkpoint": cleanup_source,
            "cleanup_pending_checkpoint": cleanup_pending,
            "ack_pending_checkpoint": ack_pending,
            "local_materialization_manifest": manifest,
            "provider_envelope": provider_envelope,
        }
        foreign_checkpoint = _superseding_checkpoint_seed()[-1]
        cases = (
            ((foreign_checkpoint, *history[1:]), "exact replay"),
            (
                (
                    replace(history[0], snapshot_receipt_sha256=SHA_F),
                    *history[1:],
                ),
                "exact replay",
            ),
            (
                (*history[:-1], replace(history[-1], publication_winner_sha256=SHA_F)),
                "exact replay",
            ),
        )
        for candidate, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                validate_remote_parse_evidence_bundle_v4(
                    **common,
                    resourceful_checkpoint_history=candidate,
                )

    def test_cleanup_validator_rejects_self_consistent_omitted_snapshot(self) -> None:
        _, reservation, _, cleanup_source, _, _ = (
            _typed_pre_submission_failure_bundle()
        )
        omitted = CleanupResourceEntryV4(
            kind="snapshot_part",
            relpath=reservation.snapshot_part_relpath,
            ownership_basis_sha256=reservation.sha256,
            expected_sha256=None,
            expected_byte_count=None,
            action="delete",
        )
        resource_bytes = json.dumps(
            [asdict(omitted)],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        forged = replace(
            _typed_pre_submission_failure_bundle()[2][-2],
            resources=(omitted,),
            resource_count=1,
            resources_sha256=(
                "sha256:" + hashlib.sha256(resource_bytes).hexdigest()
            ),
        )
        with self.assertRaisesRegex(ValueError, "intent-owned namespace"):
            validate_local_cleanup_plan_v4(
                plan=forged,
                reservation=reservation,
                source_checkpoint=cleanup_source,
                materialization_intent=None,
                local_receipt=None,
            )

    def test_full_bundle_rejects_missing_cleanup_receipt(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            manifest,
            provider_envelope,
            cleanup_pending,
            ack_pending,
            history,
        ) = _typed_happy_bundle()
        encoded = tuple(
            encode_remote_parse_evidence_v4(value)
            for index, value in enumerate(values)
            if index != len(values) - 2
        )
        with self.assertRaisesRegex(ValueError, "kind set drifted"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )

    def test_pre_submission_failure_bundle_closes_without_provider_ack(self) -> None:
        checkpoint, reservation, values, cleanup_source, cleanup_pending, history = (
            _typed_pre_submission_failure_bundle()
        )
        encoded = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=encoded,
            reservation=reservation,
            cleanup_source_checkpoint=cleanup_source,
            resourceful_checkpoint_history=history,
            cleanup_pending_checkpoint=cleanup_pending,
        )
        self.assertNotIn("ack_receipt", {item.kind for item in encoded})
        with self.assertRaisesRegex(ValueError, "non-ACK final"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    previous_checkpoint_sha256=SHA_F,
                ),
                evidence=encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
            )
        failure_index = next(
            index
            for index, value in enumerate(values)
            if type(value) is FailureReceiptV4
        )
        failure = values[failure_index]
        bad_failure = replace(
            failure,
            submission_absence_proof=replace(
                failure.submission_absence_proof,
                client_submit_key="another-submit",
            ),
        )
        bad_values = list(values)
        bad_values[failure_index] = bad_failure
        bad_encoded = tuple(
            encode_remote_parse_evidence_v4(value) for value in bad_values
        )
        with self.assertRaisesRegex(ValueError, "absence proof drifted"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    failure_receipt_sha256=bad_encoded[failure_index].sha256,
                ),
                evidence=bad_encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
            )

    def test_resourceful_supersession_without_accepted_task_closes_without_ack(self) -> None:
        (
            checkpoint,
            reservation,
            values,
            cleanup_source,
            superseding_seed,
            cleanup_pending,
            history,
        ) = _typed_unaccepted_supersession_bundle()
        (
            superseding_checkpoint,
            superseding_reservation,
            superseding_preparation,
            superseding_snapshot,
        ) = superseding_seed
        encoded = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=encoded,
            reservation=reservation,
            cleanup_source_checkpoint=cleanup_source,
            resourceful_checkpoint_history=history,
            cleanup_pending_checkpoint=cleanup_pending,
            superseding_checkpoint=superseding_checkpoint,
            superseding_reservation=superseding_reservation,
            superseding_preparation_intent=superseding_preparation,
            superseding_snapshot_receipt=superseding_snapshot,
        )
        with self.assertRaisesRegex(ValueError, "lacks exact reservation"):
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=None,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )
        with self.assertRaisesRegex(ValueError, "lacks its exact superseding"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=checkpoint,
                evidence=encoded,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                cleanup_pending_checkpoint=cleanup_pending,
            )
        self.assertNotIn("accepted_submission", {item.kind for item in encoded})
        self.assertNotIn("ack_receipt", {item.kind for item in encoded})
        supersession = values[3]
        forged = replace(
            supersession,
            source_document_id="other-document",
            superseding_document_id="other-document",
        )
        forged_values = values[:3] + (forged,) + values[4:]
        with self.assertRaisesRegex(ValueError, "source attempt chain drifted"):
            validate_remote_parse_evidence_bundle_v4(
                checkpoint=replace(
                    checkpoint,
                    supersession_receipt_sha256=forged.sha256,
                ),
                evidence=tuple(
                    encode_remote_parse_evidence_v4(value)
                    for value in forged_values
                ),
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=history,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )


def _records():
    reservation = _base()["reservation"]
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=SHA_B,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=SHA_A,
        snapshot_byte_count=100,
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    submission = SubmissionIntentV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=SHA_A,
        parser_target_sha256=SHA_B,
        request_sha256=SHA_C,
        runtime_epoch_sha256=SHA_D,
        client_submit_key="submit-1",
        submission_epoch_unix=1,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    accepted = AcceptedSubmissionReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        submission_intent_sha256=submission.sha256,
        remote_task_identity="task-1",
        status_url="https://provider.invalid/task-1",
        result_url="https://provider.invalid/task-1/result",
        secret_kind="mineru-task-token.v1",
        secret_version=1,
        token_sha256=SHA_E,
        token_byte_count=32,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    terminal = TerminalReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        accepted_submission_receipt_sha256=accepted.sha256,
        remote_task_identity="task-1",
        result_owner_identity="owner-1",
        artifact_sha256=SHA_F,
        artifact_byte_count=20,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    failure = FailureReceiptV4(
        **{
            **_failure_values(),
            "submission_was_attempted": False,
        }
    )
    supersession = SupersessionReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        source_document_id="doc-1",
        source_attempt_generation=1,
        source_state="prepared",
        source_lifecycle_version=0,
        source_checkpoint_sha256=SHA_A,
        superseding_attempt_id="attempt-2",
        superseding_attempt_generation=2,
        superseding_document_id="doc-1",
        superseding_checkpoint_sha256=SHA_B,
        reason_code="newer_attempt",
    )
    lifecycle = _happy_path()
    return (
        preparation,
        snapshot,
        submission,
        accepted,
        terminal,
        failure,
        supersession,
        lifecycle["intent"],
        lifecycle["materialization"],
        lifecycle["cleanup_plan"],
        lifecycle["cleanup_receipt"],
        lifecycle["ack_receipt"],
    )


def _failure_values() -> dict[str, object]:
    return {
        "attempt_id": "attempt-1",
        "fence_identity": "fence-1",
        "outcome": "pre_submission_failure",
        "source_state": "reconciling",
        "source_lifecycle_version": 1,
        "source_checkpoint_sha256": SHA_A,
        "submission_was_attempted": True,
        "submission_absence_proof": None,
        "accepted_submission_receipt_sha256": None,
        "terminal_receipt_sha256": None,
        "materialization_intent_sha256": None,
        "local_materialization_receipt_sha256": None,
        "error_code": "submit_failed",
        "error_stage": "submit",
        "error_class": "TimeoutError",
        "retryable": True,
        "retry_budget_class": "network",
        "message": "submit failed",
    }


def _typed_happy_bundle():
    reservation, allowance = _exact_materialization_reservation_and_allowance()
    provider_envelope_context = _provider_envelope_context()
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=provider_envelope_context.parser_target_sha256,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=SHA_A,
        snapshot_byte_count=100,
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    submission = SubmissionIntentV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=SHA_A,
        parser_target_sha256=provider_envelope_context.parser_target_sha256,
        request_sha256=SHA_C,
        runtime_epoch_sha256=SHA_D,
        client_submit_key="submit-1",
        submission_epoch_unix=1,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    accepted = AcceptedSubmissionReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        submission_intent_sha256=submission.sha256,
        remote_task_identity="task-1",
        status_url="https://provider.invalid/task-1",
        result_url="https://provider.invalid/task-1/result",
        secret_kind="mineru-task-token.v1",
        secret_version=1,
        token_sha256=SHA_D,
        token_byte_count=32,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=_submitted_credit(),
        accepted_submission_sha256=accepted.sha256,
    )
    terminal = TerminalReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        accepted_submission_receipt_sha256=accepted.sha256,
        remote_task_identity="task-1",
        result_owner_identity="result-owner-1",
        artifact_sha256=SHA_B,
        artifact_byte_count=20,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted,
        state="remote_terminal",
        held_resource_credit=_terminal_credit(),
        terminal_receipt_sha256=terminal.sha256,
    )
    intent = build_materialization_intent_v4(
        reservation=reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=terminal.sha256,
        remote_task_identity=terminal.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity,
        artifact_sha256=terminal.artifact_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        provider_envelope_context=provider_envelope_context,
        allowance_sha256=allowance.sha256,
        provider_capability_kind=accepted.secret_kind,
        provider_capability_sha256=accepted.token_sha256,
        provider_capability_byte_count=accepted.token_byte_count,
        output_dir_name="output-1",
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count_limit=10,
        uncompressed_byte_limit=100,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal,
        state="materializing",
        held_resource_credit=intent.held_resource_credit,
        materialization_intent_sha256=intent.sha256,
    )
    manifest, output_files, provider_envelope = _materialization_evidence(intent)
    materialization = build_local_materialization_receipt_v4(
        intent=intent,
        manifest=manifest,
        source_page_count=2,
        output_files=output_files,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count=manifest.observations.member_count,
        uncompressed_byte_count=30,
        decoded_byte_count=20,
        temporary_disk_peak_byte_count=50,
        file_fsync_completed=True,
        output_parent_fsync_completed=True,
        marker_removed=True,
        spool_part_absent=True,
        spool_part_owner_absent=True,
        staging_absent=True,
    )
    local = advance_remote_parse_checkpoint_v4(
        materializing,
        state="local_materialized",
        held_resource_credit=_local_credit(materialization.output_byte_count),
        local_materialization_receipt_sha256=materialization.sha256,
    )
    published = advance_remote_parse_checkpoint_v4(
        local,
        state="publish_committed",
        held_resource_credit=_local_credit(materialization.output_byte_count),
        publication_winner_sha256=SHA_E,
    )
    resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=reservation.snapshot_relpath,
            ownership_basis_sha256=reservation.sha256,
            expected_sha256=SHA_A,
            expected_byte_count=100,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="spool",
            relpath=intent.spool_relpath,
            ownership_basis_sha256=intent.sha256,
            expected_sha256=SHA_B,
            expected_byte_count=20,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="output",
            relpath=intent.output_relpath,
            ownership_basis_sha256=materialization.sha256,
            expected_sha256=materialization.output_files_sha256,
            expected_byte_count=materialization.output_byte_count,
            action="transfer",
            target_owner_identity="run-1",
            target_relpath=(
                intent.provider_envelope_context.parser_artifact_root_relpath
            ),
        ),
    )
    cleanup_plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=published,
        outcome="success",
        remote_task_identity="task-1",
        resources=resources,
        materialization_intent=intent,
        local_materialization_receipt=materialization,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        published,
        state="cleanup_pending",
        held_resource_credit=published.held_resource_credit,
        cleanup_plan_sha256=cleanup_plan.sha256,
    )
    cleanup_receipt = build_local_cleanup_receipt_v4(
        plan=cleanup_plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
            LocalCleanupResourceResultV4(
                kind="spool",
                relpath=intent.spool_relpath,
                disposition="absent",
            ),
            LocalCleanupResourceResultV4(
                kind="output",
                relpath=intent.output_relpath,
                disposition="transferred",
                target_owner_identity="run-1",
                target_relpath=(
                    intent.provider_envelope_context.parser_artifact_root_relpath
                ),
            ),
        ),
    )
    ack_pending = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="ack_pending",
        held_resource_credit=_ack_credit(),
        cleanup_receipt_sha256=cleanup_receipt.sha256,
    )
    ack_request_bytes = provider_ack_request_v4_bytes(
        accepted_submission_sha256=accepted.sha256,
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        attempt_id="attempt-1",
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        document_id="doc-1",
        fence_identity="fence-1",
        outcome="success",
        processing_run_id="run-1",
        provider_protocol_version="mineru-task-protocol.v2",
        remote_task_identity="task-1",
        result_owner_identity="result-owner-1",
        terminal_receipt_sha256=terminal.sha256,
    )
    ack_request_sha256 = "sha256:" + hashlib.sha256(ack_request_bytes).hexdigest()
    ack_receipt = ProviderAckReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        document_id="doc-1",
        processing_run_id="run-1",
        outcome="success",
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        ack_pending_lifecycle_version=ack_pending.lifecycle_version,
        accepted_submission_sha256=accepted.sha256,
        remote_task_identity="task-1",
        result_owner_identity="result-owner-1",
        terminal_receipt_sha256=terminal.sha256,
        failure_receipt_sha256=None,
        supersession_receipt_sha256=None,
        local_materialization_receipt_sha256=materialization.sha256,
        publication_winner_sha256=SHA_E,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        provider_protocol_version="mineru-task-protocol.v2",
        request_identity=provider_ack_request_v4_identity(ack_request_sha256),
        ack_request_sha256=ack_request_sha256,
        ack_kind="consumed",
        http_status=200,
        provider_response_sha256=SHA_B,
        provider_response_byte_count=2,
        provider_receipt_identity="consume-1",
    )
    acked = advance_remote_parse_checkpoint_v4(
        ack_pending,
        state="acked",
        held_resource_credit=ResourceCreditVector(),
        ack_receipt_sha256=ack_receipt.sha256,
    )
    return acked, reservation, (
        preparation,
        snapshot,
        submission,
        accepted,
        terminal,
        intent,
        materialization,
        cleanup_plan,
        cleanup_receipt,
        ack_receipt,
    ), published, manifest, provider_envelope, cleanup_pending, ack_pending, (
        prepared,
        reconciling,
        submitted,
        remote_terminal,
        materializing,
        local,
        published,
    )


def _reclose_happy_cleanup_dag(*, values, cleanup_source):
    cleanup_plan = replace(
        values[-3],
        source_checkpoint_sha256=cleanup_source.sha256,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        cleanup_source,
        state="cleanup_pending",
        held_resource_credit=cleanup_source.held_resource_credit,
        cleanup_plan_sha256=cleanup_plan.sha256,
    )
    cleanup_receipt = build_local_cleanup_receipt_v4(
        plan=cleanup_plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=values[-2].results,
    )
    ack_pending = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="ack_pending",
        held_resource_credit=_ack_credit(),
        cleanup_receipt_sha256=cleanup_receipt.sha256,
    )
    ack = values[-1]
    request = provider_ack_request_v4_bytes(
        accepted_submission_sha256=ack.accepted_submission_sha256,
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        attempt_id=ack.attempt_id,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        document_id=ack.document_id,
        fence_identity=ack.fence_identity,
        outcome=ack.outcome,
        processing_run_id=ack.processing_run_id,
        provider_protocol_version=ack.provider_protocol_version,
        remote_task_identity=ack.remote_task_identity,
        result_owner_identity=ack.result_owner_identity,
        terminal_receipt_sha256=ack.terminal_receipt_sha256,
    )
    request_sha256 = "sha256:" + hashlib.sha256(request).hexdigest()
    ack = replace(
        ack,
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        ack_request_sha256=request_sha256,
        request_identity=provider_ack_request_v4_identity(request_sha256),
    )
    final = advance_remote_parse_checkpoint_v4(
        ack_pending,
        state="acked",
        held_resource_credit=ResourceCreditVector(),
        ack_receipt_sha256=ack.sha256,
    )
    return (
        final,
        (*values[:-3], cleanup_plan, cleanup_receipt, ack),
        cleanup_pending,
        ack_pending,
    )


def _typed_pre_submission_failure_bundle():
    reservation = _base()["reservation"]
    preparation, snapshot, submission = _records()[:3]
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    failure = FailureReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        outcome="pre_submission_failure",
        source_state=reconciling.state,
        source_lifecycle_version=reconciling.lifecycle_version,
        source_checkpoint_sha256=reconciling.sha256,
        submission_was_attempted=True,
        submission_absence_proof=SubmissionAbsenceProofV4(
            client_submit_key=submission.client_submit_key,
            lookup_request_sha256=SHA_A,
            provider_protocol_version=submission.provider_protocol_version,
            http_status=404,
            response_sha256=SHA_B,
            response_byte_count=2,
        ),
        accepted_submission_receipt_sha256=None,
        terminal_receipt_sha256=None,
        materialization_intent_sha256=None,
        local_materialization_receipt_sha256=None,
        error_code="submit_not_started",
        error_stage="submit",
        error_class="LocalAdmissionError",
        retryable=True,
        retry_budget_class="local",
        message="submission did not start",
    )
    resource = CleanupResourceEntryV4(
        kind="snapshot",
        relpath=reservation.snapshot_relpath,
        ownership_basis_sha256=reservation.sha256,
        expected_sha256=SHA_A,
        expected_byte_count=100,
        action="delete",
    )
    plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=reconciling,
        outcome="pre_submission_failure",
        failure_receipt_sha256=failure.sha256,
        resources=(resource,),
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="cleanup_pending",
        held_resource_credit=reconciling.held_resource_credit,
        failure_receipt_sha256=failure.sha256,
        cleanup_plan_sha256=plan.sha256,
    )
    receipt = build_local_cleanup_receipt_v4(
        plan=plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
        ),
    )
    final = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="pre_submission_failed",
        held_resource_credit=ResourceCreditVector(),
        cleanup_receipt_sha256=receipt.sha256,
    )
    return final, reservation, (
        preparation,
        snapshot,
        submission,
        failure,
        plan,
        receipt,
    ), reconciling, cleanup_pending, (prepared, reconciling)


def _typed_remote_failure_bundle(*, provider_result_bytes: int):
    reservation = _base()["reservation"]
    preparation, snapshot, submission, accepted = _records()[:4]
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=_submitted_credit(),
        accepted_submission_sha256=accepted.sha256,
    )
    failure = FailureReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        outcome="remote_failure",
        source_state=submitted.state,
        source_lifecycle_version=submitted.lifecycle_version,
        source_checkpoint_sha256=submitted.sha256,
        submission_was_attempted=True,
        submission_absence_proof=None,
        accepted_submission_receipt_sha256=accepted.sha256,
        terminal_receipt_sha256=None,
        materialization_intent_sha256=None,
        local_materialization_receipt_sha256=None,
        error_code="provider_failed",
        error_stage="poll",
        error_class="ProviderError",
        retryable=True,
        retry_budget_class="network",
        message="provider failed",
    )
    resource = CleanupResourceEntryV4(
        kind="snapshot",
        relpath=reservation.snapshot_relpath,
        ownership_basis_sha256=reservation.sha256,
        expected_sha256=reservation.source_pdf_sha256,
        expected_byte_count=reservation.source_byte_count,
        action="delete",
    )
    cleanup_plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=submitted,
        outcome="remote_failure",
        resources=(resource,),
        remote_task_identity=accepted.remote_task_identity,
        failure_receipt_sha256=failure.sha256,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        submitted,
        state="cleanup_pending",
        held_resource_credit=submitted.held_resource_credit,
        failure_receipt_sha256=failure.sha256,
        cleanup_plan_sha256=cleanup_plan.sha256,
    )
    cleanup_receipt = build_local_cleanup_receipt_v4(
        plan=cleanup_plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
        ),
    )
    ack_pending = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="ack_pending",
        held_resource_credit=ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=provider_result_bytes,
            ack_items=1,
        ),
        cleanup_receipt_sha256=cleanup_receipt.sha256,
    )
    ack_request = provider_ack_request_v4_bytes(
        accepted_submission_sha256=accepted.sha256,
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        attempt_id=reservation.attempt_id,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        document_id=reservation.document_id,
        fence_identity=reservation.fence_identity,
        outcome="remote_failure",
        processing_run_id=reservation.processing_run_id,
        provider_protocol_version=accepted.provider_protocol_version,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=None,
        terminal_receipt_sha256=None,
    )
    ack_request_sha256 = "sha256:" + hashlib.sha256(ack_request).hexdigest()
    ack_receipt = ProviderAckReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        document_id=reservation.document_id,
        processing_run_id=reservation.processing_run_id,
        outcome="remote_failure",
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        ack_pending_lifecycle_version=ack_pending.lifecycle_version,
        accepted_submission_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=None,
        terminal_receipt_sha256=None,
        failure_receipt_sha256=failure.sha256,
        supersession_receipt_sha256=None,
        local_materialization_receipt_sha256=None,
        publication_winner_sha256=None,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        provider_protocol_version=accepted.provider_protocol_version,
        request_identity=provider_ack_request_v4_identity(
            ack_request_sha256
        ),
        ack_request_sha256=ack_request_sha256,
        ack_kind="consumed",
        http_status=200,
        provider_response_sha256=SHA_B,
        provider_response_byte_count=2,
        provider_receipt_identity="consume-1",
    )
    final = advance_remote_parse_checkpoint_v4(
        ack_pending,
        state="remote_failed",
        held_resource_credit=ResourceCreditVector(),
        ack_receipt_sha256=ack_receipt.sha256,
    )
    return final, reservation, (
        preparation,
        snapshot,
        submission,
        accepted,
        failure,
        cleanup_plan,
        cleanup_receipt,
        ack_receipt,
    ), submitted, cleanup_pending, ack_pending, (prepared, reconciling, submitted)


def _superseding_checkpoint_seed():
    source = _base()["reservation"]
    reservation = build_resource_reservation_v4(
        attempt_id="attempt-2",
        attempt_generation=2,
        fence_identity="fence-2",
        document_id=source.document_id,
        processing_run_id="run-2",
        source_pdf_sha256=source.source_pdf_sha256,
        source_byte_count=source.source_byte_count,
        source_page_count=source.source_page_count,
        prepared_submission_identity_sha256=(
            source.prepared_submission_identity_sha256
        ),
        request_sha256=source.request_sha256,
        runtime_epoch_sha256=source.runtime_epoch_sha256,
        process_profile_sha256=source.process_profile_sha256,
        credit_policy_sha256=source.credit_policy_sha256,
        reservation_bucket=source.reservation_bucket,
        reservation_input_sha256=source.reservation_input_sha256,
        reserved_credit=source.reserved_credit,
    )
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=SHA_B,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=reservation.source_pdf_sha256,
        snapshot_byte_count=reservation.source_byte_count,
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    checkpoint = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    return reservation, preparation, snapshot, checkpoint


def _typed_unaccepted_supersession_bundle():
    reservation = _base()["reservation"]
    preparation, snapshot, submission = _records()[:3]
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    (
        superseding_reservation,
        superseding_preparation,
        superseding_snapshot,
        superseding_checkpoint,
    ) = _superseding_checkpoint_seed()
    supersession = SupersessionReceiptV4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        source_document_id="doc-1",
        source_attempt_generation=1,
        source_state=reconciling.state,
        source_lifecycle_version=reconciling.lifecycle_version,
        source_checkpoint_sha256=reconciling.sha256,
        superseding_attempt_id="attempt-2",
        superseding_attempt_generation=2,
        superseding_document_id="doc-1",
        superseding_checkpoint_sha256=superseding_checkpoint.sha256,
        reason_code="newer_attempt",
    )
    resource = CleanupResourceEntryV4(
        kind="snapshot",
        relpath=reservation.snapshot_relpath,
        ownership_basis_sha256=reservation.sha256,
        expected_sha256=SHA_A,
        expected_byte_count=100,
        action="delete",
    )
    plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=reconciling,
        outcome="superseded",
        supersession_receipt_sha256=supersession.sha256,
        resources=(resource,),
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="cleanup_pending",
        held_resource_credit=reconciling.held_resource_credit,
        supersession_receipt_sha256=supersession.sha256,
        cleanup_plan_sha256=plan.sha256,
    )
    receipt = build_local_cleanup_receipt_v4(
        plan=plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
        ),
    )
    final = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="superseded",
        held_resource_credit=ResourceCreditVector(),
        cleanup_receipt_sha256=receipt.sha256,
    )
    return final, reservation, (
        preparation,
        snapshot,
        submission,
        supersession,
        plan,
        receipt,
    ), reconciling, (
        superseding_checkpoint,
        superseding_reservation,
        superseding_preparation,
        superseding_snapshot,
    ), cleanup_pending, (prepared, reconciling)


def _exact_materialization_reservation_and_allowance():
    reserved_credit = replace(_reservation_credit(), output_bytes=16_384)
    reservation_input = encode_resource_reservation_input(
        replace(_reservation_input().value, reservation=reserved_credit)
    )
    allowance = PerAttemptResourceAllowance(
        reservation_input_sha256=reservation_input.sha256,
        reservation_input=reservation_input,
        limits=reserved_credit,
    )
    reservation = build_resource_reservation_v4(
        attempt_id="attempt-1",
        attempt_generation=1,
        fence_identity="fence-1",
        document_id="doc-1",
        processing_run_id="run-1",
        source_pdf_sha256=SHA_A,
        source_byte_count=100,
        source_page_count=2,
        prepared_submission_identity_sha256=SHA_B,
        request_sha256=SHA_C,
        runtime_epoch_sha256=SHA_D,
        process_profile_sha256=SHA_E,
        credit_policy_sha256=SHA_F,
        reservation_bucket="regular",
        reservation_input_sha256=reservation_input.sha256,
        reserved_credit=reserved_credit,
    )
    return reservation, allowance


def _provider_envelope_for_intent(intent) -> ProviderDocumentEnvelope:
    base = _provider_document_envelope()
    provider_document = replace(
        base.provider_document,
        source_pdf_sha256=intent.source_pdf_sha256,
        pages=(
            ProviderPage(page_index=0, page_size=(595.0, 842.0), blocks=()),
            ProviderPage(page_index=1, page_size=(595.0, 842.0), blocks=()),
        ),
        physical_table_segments=(),
    )
    context = intent.provider_envelope_context
    return ProviderDocumentEnvelope.build(
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


def _materialization_evidence(intent):
    provider_envelope = _provider_envelope_for_intent(intent)
    provider_envelope_bytes = provider_document_envelope_to_bytes(provider_envelope)
    provider_envelope_sha256 = (
        "sha256:" + hashlib.sha256(provider_envelope_bytes).hexdigest()
    )
    payload_files = tuple(
        sorted(
            (
                *(
                    LocalMaterializationPayloadFileV4(
                        role="parser_artifact",
                        relpath=artifact.relative_path,
                        sha256=artifact.sha256,
                        byte_count=artifact.size_bytes,
                    )
                    for artifact in provider_envelope.provider_document.artifacts
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
            member_count=len(provider_envelope.provider_document.artifacts),
            uncompressed_byte_count=30,
            decoded_byte_count=20,
            temporary_disk_peak_byte_count=50,
            output_file_count=len(payload_files),
            output_byte_count=sum(item.byte_count for item in payload_files),
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
    return manifest, output_files, provider_envelope


if __name__ == "__main__":
    unittest.main()
