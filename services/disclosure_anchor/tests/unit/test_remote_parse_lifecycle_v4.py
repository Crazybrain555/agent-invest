from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import asdict, replace
from itertools import pairwise

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CleanupResourceEntryV4,
    LocalCleanupResourceResultV4,
    LocalOutputFileV4,
    MaterializationIntentV4,
    ProviderAckReceiptV4,
    ProviderEnvelopeContextV4,
    RemoteParseCheckpointV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
    build_local_cleanup_receipt_v4,
    build_local_materialization_receipt_v4,
    build_materialization_intent_v4,
    build_resource_free_remote_parse_checkpoint_v4,
    build_resource_reservation_v4,
    decode_local_cleanup_plan_v4,
    decode_local_cleanup_receipt_v4,
    decode_local_materialization_receipt_v4,
    decode_materialization_intent_v4,
    decode_provider_ack_receipt_v4,
    decode_remote_parse_checkpoint_v4,
    decode_resource_reservation_v4,
    provider_ack_request_v4_bytes,
    provider_ack_request_v4_identity,
    validate_remote_parse_checkpoint_successor_v4,
    validate_resource_reservation_checkpoint_binding_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    STAGED_RESOURCE_STATE_TRANSITIONS,
    CleanupOutcome,
    EncodedResourceReservationInput,
    PerAttemptResourceAllowance,
    ResourceCreditVector,
    ResourceReservationInput,
    encode_resource_reservation_input,
)

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64


class RemoteParseLifecycleV4Tests(unittest.TestCase):
    def test_reservation_input_hash_closes_exact_credit_and_profile_inputs(self) -> None:
        reservation = _base()["reservation"]
        with self.assertRaisesRegex(ValueError, "input hash does not close"):
            replace(reservation, process_profile_sha256=SHA_D)
        with self.assertRaisesRegex(ValueError, "input hash does not close"):
            replace(
                reservation,
                reserved_credit=replace(
                    reservation.reserved_credit,
                    output_bytes=reservation.reserved_credit.output_bytes + 1,
                ),
            )

    def test_receipt_requires_exact_typed_manifest_and_payload_closure(self) -> None:
        fixture = _happy_path()
        intent = fixture["intent"]
        manifest = fixture["manifest"]
        receipt = fixture["materialization"]
        with self.assertRaisesRegex(ValueError, "drifted from intent"):
            build_local_materialization_receipt_v4(
                intent=intent,
                manifest=replace(manifest, document_id="other-document"),
                source_page_count=receipt.source_page_count,
                output_files=receipt.output_files,
                provider_envelope_relpath=receipt.provider_envelope_relpath,
                output_manifest_relpath=receipt.output_manifest_relpath,
                member_count=receipt.member_count,
                uncompressed_byte_count=receipt.uncompressed_byte_count,
                decoded_byte_count=receipt.decoded_byte_count,
                temporary_disk_peak_byte_count=(
                    receipt.temporary_disk_peak_byte_count
                ),
                file_fsync_completed=True,
                output_parent_fsync_completed=True,
                marker_removed=True,
                spool_part_absent=True,
                spool_part_owner_absent=True,
                staging_absent=True,
            )

    def test_ack_receipt_request_hash_and_accepted_receipt_are_not_forgeable(self) -> None:
        ack = _happy_path()["ack_receipt"]
        with self.assertRaisesRegex(ValueError, "request identity does not close"):
            replace(ack, request_identity="unrelated-request")
        with self.assertRaisesRegex(ValueError, "request identity does not close"):
            replace(ack, accepted_submission_sha256=SHA_D)

    def test_happy_path_is_one_honest_v0_to_v9_chain(self) -> None:
        fixture = _happy_path()
        chain = fixture["chain"]
        self.assertEqual(
            [checkpoint.state for checkpoint in chain],
            [
                "prepared",
                "reconciling",
                "submitted",
                "remote_terminal",
                "materializing",
                "local_materialized",
                "publish_committed",
                "cleanup_pending",
                "ack_pending",
                "acked",
            ],
        )
        self.assertEqual(
            [checkpoint.lifecycle_version for checkpoint in chain], list(range(10))
        )
        for previous, current in pairwise(chain):
            self.assertEqual(current.previous_checkpoint_sha256, previous.sha256)
        self.assertNotIn("materialization_staged", STAGED_RESOURCE_STATE_TRANSITIONS)
        self.assertNotIn("cleanup_committed", STAGED_RESOURCE_STATE_TRANSITIONS)
        self.assertEqual(chain[-1].held_resource_credit, ResourceCreditVector())

    def test_prepared_cannot_reappear_after_lifecycle_zero(self) -> None:
        prepared = _base()["prepared"]
        assert isinstance(prepared, RemoteParseCheckpointV4)
        with self.assertRaisesRegex(ValueError, "lifecycle-zero states"):
            replace(
                prepared,
                lifecycle_version=1,
                previous_checkpoint_sha256=prepared.sha256,
            )

        payload = json.loads(prepared.canonical_bytes)
        payload["lifecycle_version"] = 1
        payload["previous_checkpoint_sha256"] = prepared.sha256
        with self.assertRaisesRegex(ValueError, "lifecycle-zero states"):
            decode_remote_parse_checkpoint_v4(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

    def test_preparation_failed_cannot_be_resourceful(self) -> None:
        prepared = _base()["prepared"]
        assert isinstance(prepared, RemoteParseCheckpointV4)
        with self.assertRaisesRegex(ValueError, "lifecycle-zero states"):
            replace(
                prepared,
                state="preparation_failed",
                lifecycle_version=1,
                previous_checkpoint_sha256=prepared.sha256,
                held_resource_credit=ResourceCreditVector(),
                failure_receipt_sha256=SHA_D,
            )

        payload = json.loads(prepared.canonical_bytes)
        payload.update(
            {
                "state": "preparation_failed",
                "lifecycle_version": 1,
                "previous_checkpoint_sha256": prepared.sha256,
                "held_resource_credit": asdict(ResourceCreditVector()),
                "failure_receipt_sha256": SHA_D,
            }
        )
        with self.assertRaisesRegex(ValueError, "lifecycle-zero states"):
            decode_remote_parse_checkpoint_v4(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

    def test_every_canonical_record_round_trips_and_rejects_unknown_fields(self) -> None:
        fixture = _happy_path()
        pairs = (
            (fixture["reservation"], decode_resource_reservation_v4),
            (fixture["intent"], decode_materialization_intent_v4),
            (fixture["materialization"], decode_local_materialization_receipt_v4),
            (fixture["cleanup_plan"], decode_local_cleanup_plan_v4),
            (fixture["cleanup_receipt"], decode_local_cleanup_receipt_v4),
            (fixture["ack_receipt"], decode_provider_ack_receipt_v4),
            (fixture["chain"][-1], decode_remote_parse_checkpoint_v4),
        )
        for value, decoder in pairs:
            with self.subTest(value=type(value).__name__):
                self.assertEqual(decoder(value.canonical_bytes), value)
                payload = json.loads(value.canonical_bytes)
                payload["unknown"] = True
                with self.assertRaisesRegex(ValueError, "closed"):
                    decoder(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                with self.assertRaisesRegex(ValueError, "canonical"):
                    decoder(value.canonical_bytes.replace(b'":', b'": ', 1))

    def test_provider_envelope_context_closes_target_and_final_paths(self) -> None:
        intent = _happy_path()["intent"]
        context = intent.provider_envelope_context
        expected_target = "sha256:" + hashlib.sha256(
            json.dumps(
                context.parser_target_identity.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(intent.parser_target_sha256, expected_target)
        self.assertEqual(context.parser_target_sha256, expected_target)
        self.assertEqual(context.document_id, intent.document_id)
        self.assertEqual(context.processing_run_id, intent.processing_run_id)
        self.assertEqual(context.source_pdf_sha256, intent.source_pdf_sha256)
        self.assertEqual(context.source_page_count, intent.source_page_count)

        with self.assertRaisesRegex(ValueError, "context drifted"):
            replace(
                intent,
                provider_envelope_context=replace(context, document_id="doc-other"),
            )
        with self.assertRaisesRegex(ValueError, "context drifted"):
            replace(intent, parser_target_sha256=SHA_C)
        with self.assertRaisesRegex(ValueError, "source path identity"):
            replace(
                context,
                source_pdf_relpath=context.source_pdf_relpath.replace(
                    "/1225087169/", "/other-document/"
                ),
            )
        with self.assertRaisesRegex(ValueError, "artifact path identity"):
            replace(
                context,
                parser_artifact_root_relpath=(
                    context.parser_artifact_root_relpath.replace(
                        "/run-1/", "/run-other/"
                    )
                ),
            )

    def test_materialization_metadata_paths_are_distinct_and_fixed(self) -> None:
        intent = _happy_path()["intent"]
        for values in (
            {"output_manifest_relpath": intent.provider_envelope_relpath},
            {"provider_envelope_relpath": intent.staging_marker_relpath},
            {"output_manifest_relpath": intent.staging_marker_relpath},
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, "metadata paths collide"),
            ):
                replace(intent, **values)
        with self.assertRaisesRegex(ValueError, "envelope filename is not fixed"):
            replace(intent, provider_envelope_relpath="provider.json")
        with self.assertRaisesRegex(ValueError, "manifest filename is not fixed"):
            replace(intent, output_manifest_relpath="manifest.json")

    def test_materialization_intent_decoder_closes_nested_context_and_target(self) -> None:
        intent = _happy_path()["intent"]
        for nested_key in ("provider_envelope_context", "parser_target_identity"):
            payload = json.loads(intent.canonical_bytes)
            context = payload["provider_envelope_context"]
            target = context["parser_target_identity"]
            (context if nested_key == "provider_envelope_context" else target)[
                "future"
            ] = True
            exact = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            with (
                self.subTest(nested_key=nested_key),
                self.assertRaisesRegex(ValueError, "closed"),
            ):
                decode_materialization_intent_v4(exact)

    def test_claim_and_lease_are_not_part_of_checkpoint_or_receipt_hashes(self) -> None:
        fixture = _happy_path()
        for value in (
            fixture["chain"][4],
            fixture["intent"],
            fixture["materialization"],
            fixture["cleanup_plan"],
            fixture["cleanup_receipt"],
            fixture["ack_receipt"],
        ):
            payload = value.canonical_bytes
            self.assertNotIn(b"claim_owner", payload)
            self.assertNotIn(b"claim_generation", payload)
            self.assertNotIn(b"lease", payload)
            self.assertNotIn(b"token_bytes", payload)

    def test_materialization_intent_predicts_paths_not_future_tree_bytes(self) -> None:
        fixture = _happy_path()
        payload = json.loads(fixture["intent"].canonical_bytes)
        self.assertIn("staging_relpath", payload)
        self.assertIn("output_relpath", payload)
        self.assertNotIn("staging_sha256", payload)
        self.assertNotIn("output_files_sha256", payload)
        self.assertEqual(
            fixture["materialization"].spool_sha256,
            fixture["intent"].artifact_sha256,
        )

    def test_illegal_edges_and_evidence_replacement_fail_closed(self) -> None:
        prepared = _happy_path()["chain"][0]
        with self.assertRaisesRegex(ValueError, "transition"):
            advance_remote_parse_checkpoint_v4(
                prepared,
                state="remote_terminal",
                held_resource_credit=prepared.held_resource_credit,
            )
        reconciling = advance_remote_parse_checkpoint_v4(
            prepared,
            state="reconciling",
            held_resource_credit=replace(
                prepared.held_resource_credit, remote_waits=1
            ),
            submission_intent_sha256=SHA_C,
        )
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            advance_remote_parse_checkpoint_v4(
                reconciling,
                state="submitted",
                held_resource_credit=prepared.held_resource_credit,
                submission_intent_sha256=SHA_D,
                accepted_submission_sha256=SHA_E,
            )

    def test_pre_submission_failure_requires_cleanup_and_no_accepted_task(self) -> None:
        fixture = _base()
        prepared = fixture["prepared"]
        reconciling = advance_remote_parse_checkpoint_v4(
            prepared,
            state="reconciling",
            held_resource_credit=replace(
                prepared.held_resource_credit, remote_waits=1
            ),
            submission_intent_sha256=SHA_C,
        )
        resource = CleanupResourceEntryV4(
            kind="snapshot",
            relpath=fixture["reservation"].snapshot_relpath,
            ownership_basis_sha256=fixture["reservation"].sha256,
            expected_sha256=SHA_A,
            expected_byte_count=100,
            action="delete",
        )
        plan = build_local_cleanup_plan_v4(
            reservation=fixture["reservation"],
            source_checkpoint=reconciling,
            outcome="pre_submission_failure",
            failure_receipt_sha256=SHA_D,
            resources=(resource,),
        )
        cleanup_pending = advance_remote_parse_checkpoint_v4(
            reconciling,
            state="cleanup_pending",
            held_resource_credit=reconciling.held_resource_credit,
            failure_receipt_sha256=SHA_D,
            cleanup_plan_sha256=plan.sha256,
        )
        receipt = build_local_cleanup_receipt_v4(
            plan=plan,
            cleanup_pending_checkpoint=cleanup_pending,
            results=(
                LocalCleanupResourceResultV4(
                    kind="snapshot",
                    relpath=fixture["reservation"].snapshot_relpath,
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
        self.assertIsNone(final.accepted_submission_sha256)
        with self.assertRaisesRegex(ValueError, "cannot own a provider task"):
            replace(plan, remote_task_identity="task-1")

    def test_resourceful_supersession_uses_cleanup_and_ack_when_task_exists(self) -> None:
        fixture = _happy_path()
        remote_terminal = fixture["chain"][3]
        supersession = SHA_F
        plan = build_local_cleanup_plan_v4(
            reservation=fixture["reservation"],
            source_checkpoint=remote_terminal,
            outcome="superseded",
            remote_task_identity="task-1",
            supersession_receipt_sha256=supersession,
            resources=(
                CleanupResourceEntryV4(
                    kind="snapshot",
                    relpath=fixture["reservation"].snapshot_relpath,
                    ownership_basis_sha256=fixture["reservation"].sha256,
                    expected_sha256=SHA_A,
                    expected_byte_count=100,
                    action="delete",
                ),
            ),
        )
        pending = advance_remote_parse_checkpoint_v4(
            remote_terminal,
            state="cleanup_pending",
            held_resource_credit=remote_terminal.held_resource_credit,
            supersession_receipt_sha256=supersession,
            cleanup_plan_sha256=plan.sha256,
        )
        receipt = build_local_cleanup_receipt_v4(
            plan=plan,
            cleanup_pending_checkpoint=pending,
            results=tuple(
                LocalCleanupResourceResultV4(
                    kind=entry.kind,
                    relpath=entry.relpath,
                    disposition="absent",
                )
                for entry in plan.resources
            ),
        )
        ack_pending = advance_remote_parse_checkpoint_v4(
            pending,
            state="ack_pending",
            held_resource_credit=_ack_credit(),
            cleanup_receipt_sha256=receipt.sha256,
        )
        with self.assertRaisesRegex(ValueError, "ACK evidence"):
            advance_remote_parse_checkpoint_v4(
                ack_pending,
                state="superseded",
                held_resource_credit=ResourceCreditVector(),
            )
        request_identity, request_sha256 = _ack_request_identity(
            checkpoint=ack_pending,
            accepted_submission_sha256=SHA_E,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=receipt.sha256,
            outcome="superseded",
            result_owner_identity="result-owner-1",
            terminal_receipt_sha256=SHA_E,
        )
        ack = ProviderAckReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            outcome="superseded",
            ack_pending_checkpoint_sha256=ack_pending.sha256,
            ack_pending_lifecycle_version=ack_pending.lifecycle_version,
            accepted_submission_sha256=SHA_E,
            remote_task_identity="task-1",
            result_owner_identity="result-owner-1",
            terminal_receipt_sha256=SHA_E,
            failure_receipt_sha256=None,
            supersession_receipt_sha256=supersession,
            local_materialization_receipt_sha256=None,
            publication_winner_sha256=None,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=receipt.sha256,
            provider_protocol_version="mineru-task-protocol.v2",
            request_identity=request_identity,
            ack_request_sha256=request_sha256,
            ack_kind="absent",
            http_status=404,
            provider_response_sha256=SHA_B,
            provider_response_byte_count=2,
            provider_receipt_identity=None,
        )
        final = advance_remote_parse_checkpoint_v4(
            ack_pending,
            state="superseded",
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=ack.sha256,
        )
        self.assertEqual(final.state, "superseded")

    def test_non_success_cleanup_cannot_drain_a_committed_publication(self) -> None:
        fixture = _happy_path()
        published = fixture["chain"][6]
        with self.assertRaisesRegex(ValueError, "committed publication"):
            build_local_cleanup_plan_v4(
                reservation=fixture["reservation"],
                source_checkpoint=published,
                outcome="superseded",
                remote_task_identity="task-1",
                supersession_receipt_sha256=SHA_F,
                materialization_intent=fixture["intent"],
                local_materialization_receipt=fixture["materialization"],
                resources=fixture["cleanup_plan"].resources,
            )

    def test_cleanup_plan_rejects_recursive_source_and_mixed_outcomes(self) -> None:
        fixture = _happy_path()
        plan = fixture["cleanup_plan"]
        with self.assertRaisesRegex(ValueError, "source state"):
            replace(plan, source_state="cleanup_pending")
        with self.assertRaisesRegex(ValueError, "supersession evidence"):
            replace(plan, supersession_receipt_sha256=SHA_F)
        with self.assertRaisesRegex(ValueError, "failure evidence"):
            replace(plan, failure_receipt_sha256=SHA_F)
        with self.assertRaisesRegex(ValueError, "committed publication"):
            replace(
                plan,
                outcome="pre_submission_failure",
                remote_task_identity=None,
                terminal_receipt_sha256=None,
                materialization_intent_sha256=None,
                local_materialization_receipt_sha256=None,
                failure_receipt_sha256=SHA_F,
            )
        with self.assertRaisesRegex(ValueError, "mix failure and supersession"):
            replace(
                fixture["chain"][-1],
                failure_receipt_sha256=SHA_F,
                supersession_receipt_sha256=SHA_E,
            )
        with self.assertRaisesRegex(ValueError, "cannot retain a winner"):
            replace(
                fixture["chain"][-1],
                state="local_failed",
                failure_receipt_sha256=SHA_F,
                supersession_receipt_sha256=None,
            )

    def test_cleanup_reservation_binds_all_checkpoint_immutable_facts(self) -> None:
        fixture = _happy_path()
        reservation = fixture["reservation"]
        source_checkpoint = fixture["chain"][6]
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=source_checkpoint,
        )
        policy_input = encode_resource_reservation_input(
            replace(_reservation_input().value, credit_policy_sha256=SHA_A)
        )
        profile_input = encode_resource_reservation_input(
            replace(_reservation_input().value, process_profile_sha256=SHA_A)
        )
        for field_name, forged in (
            ("request_sha256", replace(reservation, request_sha256=SHA_F)),
            ("runtime_epoch_sha256", replace(reservation, runtime_epoch_sha256=SHA_F)),
            (
                "process_profile_sha256",
                replace(
                    reservation,
                    process_profile_sha256=SHA_A,
                    reservation_input_sha256=profile_input.sha256,
                ),
            ),
            (
                "credit_policy_sha256",
                replace(
                    reservation,
                    credit_policy_sha256=SHA_A,
                    reservation_input_sha256=policy_input.sha256,
                ),
            ),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(
                    ValueError,
                    "checkpoint immutable facts",
                ):
                    validate_resource_reservation_checkpoint_binding_v4(
                        reservation=forged,
                        checkpoint=source_checkpoint,
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "checkpoint immutable facts",
                ):
                    build_local_cleanup_plan_v4(
                        reservation=forged,
                        source_checkpoint=source_checkpoint,
                        outcome="success",
                        resources=fixture["cleanup_plan"].resources,
                        materialization_intent=fixture["intent"],
                        local_materialization_receipt=fixture["materialization"],
                        remote_task_identity="task-1",
                    )

    def test_identity_and_paths_reject_noncanonical_text(self) -> None:
        fixture = _happy_path()
        with self.assertRaisesRegex(ValueError, "identity"):
            replace(fixture["reservation"], attempt_id=" attempt-1")
        with self.assertRaisesRegex(ValueError, "path"):
            replace(fixture["materialization"].output_files[0], relpath="a//b")

    def test_ack_absence_is_exact_404_and_consumed_is_200_or_204(self) -> None:
        fixture = _happy_path()
        ack = fixture["ack_receipt"]
        self.assertEqual(ack.ack_kind, "consumed")
        for status in (200, 204):
            self.assertEqual(replace(ack, http_status=status).http_status, status)
        with self.assertRaisesRegex(ValueError, "200 or 204"):
            replace(ack, http_status=404)
        absent = replace(
            ack,
            ack_kind="absent",
            http_status=404,
            provider_receipt_identity=None,
        )
        self.assertEqual(absent.ack_kind, "absent")
        with self.assertRaisesRegex(ValueError, "exact HTTP 404"):
            replace(absent, http_status=410)
        with self.assertRaisesRegex(ValueError, "remote-failure ACK"):
            replace(
                ack,
                outcome="remote_failure",
                result_owner_identity=None,
                failure_receipt_sha256=SHA_C,
                publication_winner_sha256=None,
            )
        with self.assertRaisesRegex(ValueError, "local-failure ACK"):
            replace(
                ack,
                outcome="local_failure",
                failure_receipt_sha256=SHA_C,
            )

    def test_resource_free_finals_have_no_predecessor_or_credit(self) -> None:
        failed = build_resource_free_remote_parse_checkpoint_v4(
            state="preparation_failed",
            attempt_id="attempt-1",
            attempt_generation=1,
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            source_pdf_sha256=SHA_A,
            source_byte_count=100,
            source_page_count=2,
            request_sha256=SHA_B,
            runtime_epoch_sha256=SHA_C,
            process_profile_sha256=SHA_D,
            credit_policy_sha256=SHA_E,
            reservation_input_sha256=SHA_F,
            failure_receipt_sha256=SHA_A,
        )
        self.assertEqual(failed.held_resource_credit, ResourceCreditVector())
        self.assertIsNone(failed.previous_checkpoint_sha256)
        superseded = build_resource_free_remote_parse_checkpoint_v4(
            state="superseded",
            attempt_id="attempt-1",
            attempt_generation=1,
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            source_pdf_sha256=SHA_A,
            source_byte_count=100,
            source_page_count=2,
            request_sha256=SHA_B,
            runtime_epoch_sha256=SHA_C,
            process_profile_sha256=SHA_D,
            credit_policy_sha256=SHA_E,
            reservation_input_sha256=SHA_F,
            supersession_receipt_sha256=SHA_B,
        )
        self.assertEqual(superseded.state, "superseded")
        self.assertIsNone(superseded.previous_checkpoint_sha256)
        with self.assertRaisesRegex(ValueError, "exact failure evidence"):
            build_resource_free_remote_parse_checkpoint_v4(
                state="preparation_failed",
                attempt_id="attempt-1",
                attempt_generation=1,
                fence_identity="fence-1",
                document_id="doc-1",
                processing_run_id="run-1",
                source_pdf_sha256=SHA_A,
                source_byte_count=100,
                source_page_count=2,
                request_sha256=SHA_B,
                runtime_epoch_sha256=SHA_C,
                process_profile_sha256=SHA_D,
                credit_policy_sha256=SHA_E,
                reservation_input_sha256=SHA_F,
            )

    def test_remote_failure_drains_cleanup_ack_and_all_credit(self) -> None:
        fixture = _happy_path()
        submitted = fixture["chain"][2]
        plan = build_local_cleanup_plan_v4(
            reservation=fixture["reservation"],
            source_checkpoint=submitted,
            outcome="remote_failure",
            remote_task_identity="task-1",
            failure_receipt_sha256=SHA_A,
            resources=(
                CleanupResourceEntryV4(
                    kind="snapshot",
                    relpath=fixture["reservation"].snapshot_relpath,
                    ownership_basis_sha256=fixture["reservation"].sha256,
                    expected_sha256=SHA_A,
                    expected_byte_count=100,
                    action="delete",
                ),
            ),
        )
        cleanup_pending = advance_remote_parse_checkpoint_v4(
            submitted,
            state="cleanup_pending",
            held_resource_credit=submitted.held_resource_credit,
            failure_receipt_sha256=SHA_A,
            cleanup_plan_sha256=plan.sha256,
        )
        cleanup_receipt = build_local_cleanup_receipt_v4(
            plan=plan,
            cleanup_pending_checkpoint=cleanup_pending,
            results=(
                LocalCleanupResourceResultV4(
                    kind="snapshot",
                    relpath=fixture["reservation"].snapshot_relpath,
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
                ack_items=1,
            ),
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
        request_identity, request_sha256 = _ack_request_identity(
            checkpoint=ack_pending,
            accepted_submission_sha256=SHA_E,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
            outcome="remote_failure",
            result_owner_identity=None,
            terminal_receipt_sha256=None,
        )
        ack = ProviderAckReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            outcome="remote_failure",
            ack_pending_checkpoint_sha256=ack_pending.sha256,
            ack_pending_lifecycle_version=ack_pending.lifecycle_version,
            accepted_submission_sha256=SHA_E,
            remote_task_identity="task-1",
            result_owner_identity=None,
            terminal_receipt_sha256=None,
            failure_receipt_sha256=SHA_A,
            supersession_receipt_sha256=None,
            local_materialization_receipt_sha256=None,
            publication_winner_sha256=None,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
            provider_protocol_version="mineru-task-protocol.v2",
            request_identity=request_identity,
            ack_request_sha256=request_sha256,
            ack_kind="consumed",
            http_status=204,
            provider_response_sha256=SHA_C,
            provider_response_byte_count=0,
            provider_receipt_identity=None,
        )
        final = advance_remote_parse_checkpoint_v4(
            ack_pending,
            state="remote_failed",
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=ack.sha256,
        )
        self.assertEqual(final.state, "remote_failed")
        self.assertEqual(final.held_resource_credit, ResourceCreditVector())

    def test_local_failure_drains_completed_spool_before_ack(self) -> None:
        fixture = _happy_path()
        intent = fixture["intent"]
        materializing = fixture["chain"][4]
        resources = _local_failure_resources(fixture)
        plan = build_local_cleanup_plan_v4(
            reservation=fixture["reservation"],
            source_checkpoint=materializing,
            outcome="local_failure",
            materialization_intent=intent,
            remote_task_identity="task-1",
            failure_receipt_sha256=SHA_A,
            resources=resources,
        )
        cleanup_pending = advance_remote_parse_checkpoint_v4(
            materializing,
            state="cleanup_pending",
            held_resource_credit=materializing.held_resource_credit,
            failure_receipt_sha256=SHA_A,
            cleanup_plan_sha256=plan.sha256,
        )
        cleanup_receipt = build_local_cleanup_receipt_v4(
            plan=plan,
            cleanup_pending_checkpoint=cleanup_pending,
            results=tuple(
                LocalCleanupResourceResultV4(
                    kind=item.kind,
                    relpath=item.relpath,
                    disposition="absent",
                )
                for item in plan.resources
            ),
        )
        ack_pending = advance_remote_parse_checkpoint_v4(
            cleanup_pending,
            state="ack_pending",
            held_resource_credit=_ack_credit(),
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
        request_identity, request_sha256 = _ack_request_identity(
            checkpoint=ack_pending,
            accepted_submission_sha256=SHA_E,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
            outcome="local_failure",
            result_owner_identity="result-owner-1",
            terminal_receipt_sha256=SHA_F,
        )
        ack = ProviderAckReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            document_id="doc-1",
            processing_run_id="run-1",
            outcome="local_failure",
            ack_pending_checkpoint_sha256=ack_pending.sha256,
            ack_pending_lifecycle_version=ack_pending.lifecycle_version,
            accepted_submission_sha256=SHA_E,
            remote_task_identity="task-1",
            result_owner_identity="result-owner-1",
            terminal_receipt_sha256=SHA_F,
            failure_receipt_sha256=SHA_A,
            supersession_receipt_sha256=None,
            local_materialization_receipt_sha256=None,
            publication_winner_sha256=None,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
            provider_protocol_version="mineru-task-protocol.v2",
            request_identity=request_identity,
            ack_request_sha256=request_sha256,
            ack_kind="absent",
            http_status=404,
            provider_response_sha256=SHA_C,
            provider_response_byte_count=2,
            provider_receipt_identity=None,
        )
        final = advance_remote_parse_checkpoint_v4(
            ack_pending,
            state="local_failed",
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=ack.sha256,
        )
        self.assertEqual(final.state, "local_failed")
        self.assertIn("spool", {item.kind for item in plan.resources})

    def test_prepared_is_write_ahead_and_rejects_zero_credit(self) -> None:
        reservation = _base()["reservation"]
        prepared = build_initial_remote_parse_checkpoint_v4(
            reservation=reservation,
            preparation_intent_sha256=SHA_B,
            held_resource_credit=_snapshot_credit(),
        )
        self.assertIsNone(prepared.snapshot_receipt_sha256)
        with self.assertRaisesRegex(ValueError, "snapshot credit"):
            replace(prepared, held_resource_credit=ResourceCreditVector())

    def test_resourceful_cleanup_plan_cannot_be_empty_or_release_credit_early(self) -> None:
        prepared = _base()["prepared"]
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            build_local_cleanup_plan_v4(
                reservation=_base()["reservation"],
                source_checkpoint=prepared,
                outcome="pre_submission_failure",
                failure_receipt_sha256=SHA_D,
                resources=(),
            )
        with self.assertRaisesRegex(ValueError, "must equal"):
            advance_remote_parse_checkpoint_v4(
                prepared,
                state="cleanup_pending",
                held_resource_credit=replace(
                    prepared.held_resource_credit,
                    provider_tasks=1,
                ),
                failure_receipt_sha256=SHA_D,
                cleanup_plan_sha256=SHA_E,
            )
        fixture = _happy_path()
        intent = fixture["intent"]
        materializing = fixture["chain"][4]
        resources = (
            CleanupResourceEntryV4(
                kind="snapshot",
                relpath=fixture["reservation"].snapshot_relpath,
                ownership_basis_sha256=fixture["reservation"].sha256,
                expected_sha256=SHA_A,
                expected_byte_count=100,
                action="delete",
            ),
            CleanupResourceEntryV4(
                kind="spool",
                relpath=intent.spool_relpath,
                ownership_basis_sha256=intent.sha256,
                expected_sha256=intent.artifact_sha256,
                expected_byte_count=intent.artifact_byte_count,
                action="delete",
            ),
            *tuple(
                CleanupResourceEntryV4(
                    kind=kind,
                    relpath=getattr(intent, f"{kind}_relpath"),
                    ownership_basis_sha256=intent.sha256,
                    expected_sha256=None,
                    expected_byte_count=None,
                    action="delete",
                )
                for kind in (
                    "spool_part",
                    "spool_part_owner",
                    "staging",
                    "staging_marker",
                )
            ),
        )
        plan = build_local_cleanup_plan_v4(
            reservation=fixture["reservation"],
            source_checkpoint=materializing,
            outcome="local_failure",
            materialization_intent=intent,
            remote_task_identity="task-1",
            failure_receipt_sha256=SHA_A,
            resources=resources,
        )
        self.assertIn("spool", {item.kind for item in plan.resources})
        with self.assertRaisesRegex(ValueError, "intent-owned namespace"):
            build_local_cleanup_plan_v4(
                reservation=fixture["reservation"],
                source_checkpoint=materializing,
                outcome="local_failure",
                materialization_intent=intent,
                remote_task_identity="task-1",
                failure_receipt_sha256=SHA_A,
                resources=tuple(
                    item for item in resources if item.kind != "spool"
                ),
            )

    def test_exported_successor_rejects_cleanup_credit_loss_like_builder(self) -> None:
        submitted = _happy_path()["chain"][2]
        assert isinstance(submitted, RemoteParseCheckpointV4)
        forged = replace(
            submitted,
            state="cleanup_pending",
            lifecycle_version=submitted.lifecycle_version + 1,
            previous_checkpoint_sha256=submitted.sha256,
            held_resource_credit=_snapshot_credit(),
            failure_receipt_sha256=SHA_A,
            cleanup_plan_sha256=SHA_B,
        )
        with self.assertRaisesRegex(ValueError, "must equal"):
            validate_remote_parse_checkpoint_successor_v4(submitted, forged)
        with self.assertRaisesRegex(ValueError, "must equal"):
            advance_remote_parse_checkpoint_v4(
                submitted,
                state="cleanup_pending",
                held_resource_credit=_snapshot_credit(),
                failure_receipt_sha256=SHA_A,
                cleanup_plan_sha256=SHA_B,
            )

    def test_ack_pending_provider_result_credit_matches_terminal_receipt(self) -> None:
        fixture = _happy_path()
        cleanup_pending = fixture["chain"][-3]
        assert isinstance(cleanup_pending, RemoteParseCheckpointV4)

        with self.assertRaisesRegex(ValueError, "invented provider result"):
            replace(
                cleanup_pending,
                state="ack_pending",
                lifecycle_version=cleanup_pending.lifecycle_version + 1,
                previous_checkpoint_sha256=cleanup_pending.sha256,
                terminal_receipt_sha256=None,
                cleanup_receipt_sha256=SHA_A,
                held_resource_credit=ResourceCreditVector(
                    documents=1,
                    provider_tasks=1,
                    provider_result_bytes=1,
                    ack_items=1,
                ),
            )

    def test_cleanup_and_ack_states_require_one_exact_outcome(self) -> None:
        prepared = _base()["prepared"]
        assert isinstance(prepared, RemoteParseCheckpointV4)

        with self.assertRaisesRegex(ValueError, "exactly one outcome"):
            advance_remote_parse_checkpoint_v4(
                prepared,
                state="cleanup_pending",
                held_resource_credit=prepared.held_resource_credit,
                cleanup_plan_sha256=SHA_D,
            )

        cleanup_pending = advance_remote_parse_checkpoint_v4(
            prepared,
            state="cleanup_pending",
            held_resource_credit=prepared.held_resource_credit,
            failure_receipt_sha256=SHA_D,
            cleanup_plan_sha256=SHA_E,
        )
        with self.assertRaisesRegex(ValueError, "exactly one outcome"):
            replace(
                cleanup_pending,
                state="ack_pending",
                lifecycle_version=cleanup_pending.lifecycle_version + 1,
                previous_checkpoint_sha256=cleanup_pending.sha256,
                accepted_submission_sha256=SHA_A,
                cleanup_receipt_sha256=SHA_F,
                failure_receipt_sha256=None,
                held_resource_credit=_ack_credit(),
            )

    def test_terminal_failure_and_supersession_ack_shapes_are_closed(self) -> None:
        prepared = _base()["prepared"]
        assert isinstance(prepared, RemoteParseCheckpointV4)
        failure_pending = advance_remote_parse_checkpoint_v4(
            prepared,
            state="cleanup_pending",
            held_resource_credit=prepared.held_resource_credit,
            failure_receipt_sha256=SHA_D,
            cleanup_plan_sha256=SHA_E,
        )
        with self.assertRaisesRegex(ValueError, "accepted-task or ACK"):
            advance_remote_parse_checkpoint_v4(
                failure_pending,
                state="pre_submission_failed",
                held_resource_credit=ResourceCreditVector(),
                cleanup_receipt_sha256=SHA_F,
                ack_receipt_sha256=SHA_A,
            )

        supersession_pending = advance_remote_parse_checkpoint_v4(
            prepared,
            state="cleanup_pending",
            held_resource_credit=prepared.held_resource_credit,
            supersession_receipt_sha256=SHA_D,
            cleanup_plan_sha256=SHA_E,
        )
        with self.assertRaisesRegex(ValueError, "accepted-task and ACK evidence disagree"):
            advance_remote_parse_checkpoint_v4(
                supersession_pending,
                state="superseded",
                held_resource_credit=ResourceCreditVector(),
                cleanup_receipt_sha256=SHA_F,
                ack_receipt_sha256=SHA_A,
            )

    def test_successor_allows_only_edge_specific_new_evidence(self) -> None:
        fixture = _happy_path()
        remote_terminal = fixture["chain"][3]
        assert isinstance(remote_terminal, RemoteParseCheckpointV4)

        with self.assertRaisesRegex(ValueError, "introduced unexpected evidence"):
            advance_remote_parse_checkpoint_v4(
                remote_terminal,
                state="cleanup_pending",
                held_resource_credit=remote_terminal.held_resource_credit,
                failure_receipt_sha256=SHA_A,
                cleanup_plan_sha256=SHA_B,
                local_materialization_receipt_sha256=SHA_C,
            )

        cleanup_pending = advance_remote_parse_checkpoint_v4(
            remote_terminal,
            state="cleanup_pending",
            held_resource_credit=remote_terminal.held_resource_credit,
            supersession_receipt_sha256=SHA_A,
            cleanup_plan_sha256=SHA_B,
        )
        with self.assertRaisesRegex(ValueError, "introduced unexpected evidence"):
            advance_remote_parse_checkpoint_v4(
                cleanup_pending,
                state="superseded",
                held_resource_credit=ResourceCreditVector(),
                cleanup_receipt_sha256=SHA_C,
                ack_receipt_sha256=SHA_D,
            )

    def test_success_cleanup_target_is_derived_from_provider_context(self) -> None:
        fixture = _happy_path()
        intent = fixture["intent"]
        plan = fixture["cleanup_plan"]
        expected_target = (
            intent.provider_envelope_context.parser_artifact_root_relpath
        )
        transfers = tuple(
            item for item in plan.resources if item.action == "transfer"
        )
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].target_relpath, expected_target)
        forged_resources = tuple(
            replace(item, target_relpath="parser_artifacts/forged")
            if item.action == "transfer"
            else item
            for item in plan.resources
        )
        with self.assertRaisesRegex(ValueError, "not the published run"):
            build_local_cleanup_plan_v4(
                reservation=fixture["reservation"],
                source_checkpoint=fixture["chain"][6],
                outcome="success",
                resources=forged_resources,
                materialization_intent=intent,
                local_materialization_receipt=fixture["materialization"],
                remote_task_identity="task-1",
            )


def _snapshot_credit() -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
    )


def _submitted_credit() -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        remote_waits=1,
        provider_tasks=1,
        ack_items=1,
    )


def _terminal_credit() -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        provider_tasks=1,
        provider_result_bytes=20,
        ack_items=1,
    )


def _materializing_credit() -> ResourceCreditVector:
    return ResourceCreditVector(
        **{
            **asdict(_terminal_credit()),
            "materialization_items": 1,
            "compressed_bytes": 20,
            "decoded_bytes": 30,
            "temp_disk_bytes": 8192,
        }
    )


def _reservation_credit() -> ResourceCreditVector:
    return replace(
        _materializing_credit(),
        remote_waits=1,
        output_items=1,
        output_bytes=4096,
        output_pages=2,
    )


def _local_credit(output_bytes: int = 12) -> ResourceCreditVector:
    return ResourceCreditVector(
        **{
            **asdict(_terminal_credit()),
            "compressed_bytes": 20,
            "output_items": 1,
            "output_bytes": output_bytes,
            "output_pages": 2,
        }
    )


def _reservation_input() -> EncodedResourceReservationInput:
    return encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=SHA_A,
            source_byte_count=100,
            source_page_count=2,
            process_profile_sha256=SHA_E,
            credit_policy_sha256=SHA_F,
            bucket="regular",
            reservation=_reservation_credit(),
        )
    )


def _allowance() -> PerAttemptResourceAllowance:
    reservation_input = _reservation_input()
    return PerAttemptResourceAllowance(
        reservation_input_sha256=reservation_input.sha256,
        reservation_input=reservation_input,
        limits=reservation_input.value.reservation,
    )


def _ack_request_identity(
    *,
    checkpoint: RemoteParseCheckpointV4,
    accepted_submission_sha256: str,
    cleanup_plan_sha256: str,
    cleanup_receipt_sha256: str,
    outcome: CleanupOutcome,
    result_owner_identity: str | None,
    terminal_receipt_sha256: str | None,
) -> tuple[str, str]:
    request_bytes = provider_ack_request_v4_bytes(
        accepted_submission_sha256=accepted_submission_sha256,
        ack_pending_checkpoint_sha256=checkpoint.sha256,
        attempt_id=checkpoint.attempt_id,
        cleanup_plan_sha256=cleanup_plan_sha256,
        cleanup_receipt_sha256=cleanup_receipt_sha256,
        document_id=checkpoint.document_id,
        fence_identity=checkpoint.fence_identity,
        outcome=outcome,
        processing_run_id=checkpoint.processing_run_id,
        provider_protocol_version="mineru-task-protocol.v2",
        remote_task_identity="task-1",
        result_owner_identity=result_owner_identity,
        terminal_receipt_sha256=terminal_receipt_sha256,
    )
    request_sha256 = "sha256:" + hashlib.sha256(request_bytes).hexdigest()
    return provider_ack_request_v4_identity(request_sha256), request_sha256


def _materialization_manifest(intent: MaterializationIntentV4):
    payload_files = (
        LocalMaterializationPayloadFileV4(
            role="provider_envelope",
            relpath=PROVIDER_DOCUMENT_FILENAME,
            sha256=SHA_C,
            byte_count=7,
        ),
        LocalMaterializationPayloadFileV4(
            role="parser_artifact",
            relpath="result/content.md",
            sha256=SHA_E,
            byte_count=11,
        ),
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
        provider_envelope_sha256=SHA_C,
        provider_envelope_byte_count=7,
        observations=LocalMaterializationObservationsV4(
            member_count=2,
            uncompressed_byte_count=30,
            decoded_byte_count=20,
            temporary_disk_peak_byte_count=50,
            output_file_count=2,
            output_byte_count=18,
        ),
        payload_files=payload_files,
    )
    output_files = (
        LocalOutputFileV4(
            LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
            manifest.sha256,
            len(manifest.canonical_bytes),
        ),
        LocalOutputFileV4(PROVIDER_DOCUMENT_FILENAME, SHA_C, 7),
        LocalOutputFileV4("result/content.md", SHA_E, 11),
    )
    return manifest, output_files


def _ack_credit() -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        provider_tasks=1,
        provider_result_bytes=20,
        ack_items=1,
    )


def _local_failure_resources(fixture: dict[str, object]):
    reservation = fixture["reservation"]
    intent = fixture["intent"]
    return (
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
            expected_sha256=intent.artifact_sha256,
            expected_byte_count=intent.artifact_byte_count,
            action="delete",
        ),
        *tuple(
            CleanupResourceEntryV4(
                kind=kind,
                relpath=getattr(intent, f"{kind}_relpath"),
                ownership_basis_sha256=intent.sha256,
                expected_sha256=None,
                expected_byte_count=None,
                action="delete",
            )
            for kind in (
                "spool_part",
                "spool_part_owner",
                "staging",
                "staging_marker",
            )
        ),
    )


def _base() -> dict[str, object]:
    reservation_input = _reservation_input()
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
        reserved_credit=_reservation_credit(),
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=SHA_B,
        snapshot_receipt_sha256=SHA_C,
        held_resource_credit=_snapshot_credit(),
    )
    return {
        "reservation": reservation,
        "reservation_input": reservation_input,
        "prepared": prepared,
    }


def _provider_envelope_context() -> ProviderEnvelopeContextV4:
    return ProviderEnvelopeContextV4(
        document_id="doc-1",
        processing_run_id="run-1",
        provider="cninfo",
        provider_document_id="1225087169",
        source_pdf_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        source_pdf_sha256=SHA_A,
        source_page_count=2,
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/1225087169/"
            f"run-1/sha256_{'a' * 64}/hybrid_auto"
        ),
        parser_target_identity=ParserTargetIdentity(
            name="MinerU",
            package_version="3.4.4",
            backend="hybrid-http-client",
            method="auto",
            language="ch",
            formula=True,
            table=True,
            effort="medium",
            runtime_bundle_identity_sha256=SHA_F,
        ),
    )


def _happy_path() -> dict[str, object]:
    base = _base()
    reservation = base["reservation"]
    prepared = base["prepared"]
    assert not isinstance(reservation, dict)
    assert isinstance(prepared, RemoteParseCheckpointV4)
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=SHA_D,
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=_submitted_credit(),
        accepted_submission_sha256=SHA_E,
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted,
        state="remote_terminal",
        held_resource_credit=_terminal_credit(),
        terminal_receipt_sha256=SHA_F,
    )
    intent = build_materialization_intent_v4(
        reservation=reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=SHA_F,
        remote_task_identity="task-1",
        artifact_owner_identity="result-owner-1",
        artifact_sha256=SHA_B,
        artifact_byte_count=20,
        provider_envelope_context=_provider_envelope_context(),
        allowance_sha256=_allowance().sha256,
        provider_capability_kind="mineru-task-token.v1",
        provider_capability_sha256=SHA_D,
        provider_capability_byte_count=32,
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
    manifest, output_files = _materialization_manifest(intent)
    materialization = build_local_materialization_receipt_v4(
        intent=intent,
        manifest=manifest,
        source_page_count=2,
        output_files=output_files,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count=2,
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
        accepted_submission_sha256=SHA_E,
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
        terminal_receipt_sha256=SHA_F,
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
        accepted_submission_sha256=SHA_E,
        remote_task_identity="task-1",
        result_owner_identity="result-owner-1",
        terminal_receipt_sha256=SHA_F,
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
    return {
        **base,
        "intent": intent,
        "materialization": materialization,
        "manifest": manifest,
        "cleanup_plan": cleanup_plan,
        "cleanup_receipt": cleanup_receipt,
        "ack_receipt": ack_receipt,
        "chain": (
            prepared,
            reconciling,
            submitted,
            remote_terminal,
            materializing,
            local,
            published,
            cleanup_pending,
            ack_pending,
            acked,
        ),
    }


if __name__ == "__main__":
    unittest.main()
