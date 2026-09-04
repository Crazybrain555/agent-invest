from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from typing import TypedDict, cast
import unittest

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationManifestV4,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    FailureReceiptV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
    build_preparation_intent_v4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CleanupResourceEntryV4,
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    LocalCleanupResourceResultV4,
    LocalMaterializationReceiptV4,
    LocalOutputFileV4,
    MaterializationIntentV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_materialization_receipt_v4,
    build_local_cleanup_plan_v4,
    build_local_cleanup_receipt_v4,
    build_materialization_intent_v4,
    local_output_files_sha256_v4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    PrivateProviderCapabilityV4,
    ProviderAckCommandV4,
    V4EvidenceReplayContext,
    V4ClaimWitness,
    seal_provider_ack_command_v4,
    validate_v4_ack_authorization,
    validate_v4_cleanup_authorization,
    validate_v4_materialization_authorization,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    EncodedResourceReservationInput,
    PerAttemptResourceAllowance,
    ResourceCreditVector,
)
from tests.unit.test_provider_document_envelope import (
    _envelope as _provider_document_envelope,
)
from tests.unit.test_remote_parse_lifecycle_v4 import (
    _ack_credit,
    _local_credit,
    _provider_envelope_context,
    _snapshot_credit,
    _submitted_credit,
    _terminal_credit,
)
from tests.unit.test_remote_parse_evidence_v4 import (
    _exact_materialization_reservation_and_allowance,
    _materialization_evidence,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_F = "sha256:" + "f" * 64


class _PortHappyPathFixture(TypedDict):
    reservation: ResourceReservationV4
    reservation_input: EncodedResourceReservationInput
    allowance: PerAttemptResourceAllowance
    token: bytes
    preparation: PreparationIntentV4
    snapshot: SnapshotReceiptV4
    submission: SubmissionIntentV4
    accepted: AcceptedSubmissionReceiptV4
    terminal: TerminalReceiptV4
    intent: MaterializationIntentV4
    manifest: LocalMaterializationManifestV4
    provider_envelope: ProviderDocumentEnvelope
    materialization: LocalMaterializationReceiptV4
    cleanup_plan: LocalCleanupPlanV4
    cleanup_receipt: LocalCleanupReceiptV4
    chain: tuple[RemoteParseCheckpointV4, ...]


class _DuckClaim:
    def validates(self, checkpoint: RemoteParseCheckpointV4) -> bool:
        return True


class _DuckAckCapability:
    capability_purpose = "result_acknowledgement"

    def validates_accepted_submission(
        self,
        accepted: AcceptedSubmissionReceiptV4,
    ) -> bool:
        return True


class _EvilStr(str):
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class StagedProviderParserV4PortTests(unittest.TestCase):
    def test_materialized_provider_read_closes_envelope_manifest_and_receipt(self) -> None:
        value = _materialized_provider_document_v4()
        envelope_bytes = provider_document_envelope_to_bytes(value.provider_envelope)
        self.assertIs(value.provider_document, value.provider_envelope.provider_document)
        self.assertEqual(
            value.artifact_root_relpath,
            value.provider_envelope.parser_artifact_root_relpath,
        )
        self.assertEqual(
            value.manifest.provider_envelope_sha256,
            "sha256:" + hashlib.sha256(envelope_bytes).hexdigest(),
        )
        self.assertEqual(
            value.receipt.output_manifest_sha256,
            value.manifest.sha256,
        )
        self.assertEqual(value.receipt.output_relpath, value.intent.output_relpath)
        self.assertEqual(
            value.artifact_root_relpath,
            value.intent.provider_envelope_context.parser_artifact_root_relpath,
        )
        self.assertNotEqual(value.receipt.output_relpath, value.artifact_root_relpath)

    def test_materialized_provider_read_rejects_cross_chain_drift(self) -> None:
        value = _materialized_provider_document_v4()
        with self.assertRaisesRegex(ValueError, "drifted"):
            replace(
                value,
                manifest=replace(value.manifest, document_id="doc_unrelated"),
            )

        root_drift = replace(
            value.manifest,
            output_relpath="materialization/output-other",
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            MaterializedProviderDocumentV4(
                receipt=_receipt_for_manifest(root_drift),
                intent=value.intent,
                provider_envelope=value.provider_envelope,
                manifest=root_drift,
            )

        envelope_drift = replace(
            value.manifest,
            provider_envelope_sha256=SHA_F,
            payload_files=tuple(
                replace(item, sha256=SHA_F)
                if item.role == "provider_envelope"
                else item
                for item in value.manifest.payload_files
            ),
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            MaterializedProviderDocumentV4(
                receipt=_receipt_for_manifest(envelope_drift),
                intent=value.intent,
                provider_envelope=value.provider_envelope,
                manifest=envelope_drift,
            )

        parser_file = next(
            item for item in value.manifest.payload_files
            if item.role == "parser_artifact"
        )
        parser_drift = replace(
            value.manifest,
            payload_files=tuple(
                replace(item, sha256=SHA_F)
                if item.relpath == parser_file.relpath
                else item
                for item in value.manifest.payload_files
            ),
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            MaterializedProviderDocumentV4(
                receipt=_receipt_for_manifest(parser_drift),
                intent=value.intent,
                provider_envelope=value.provider_envelope,
                manifest=parser_drift,
            )

        foreign_final_root = replace(
            value.provider_envelope,
            artifact_owner_processing_run_id="run-2",
            parser_artifact_root_relpath=(
                "parser_artifacts/cninfo/000001/1225087169/"
                f"run-2/sha256_{'a' * 64}/hybrid_auto"
            ),
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            MaterializedProviderDocumentV4(
                receipt=value.receipt,
                intent=value.intent,
                provider_envelope=foreign_final_root,
                manifest=value.manifest,
            )

    def test_private_capability_is_versioned_bound_and_redacted(self) -> None:
        token = b"private-provider-token"
        capability = PrivateProviderCapabilityV4(
            attempt_id="attempt-1",
            remote_task_identity="task-1",
            provider_protocol_version="mineru-task-protocol.v2",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            capability_purpose="result_download",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        self.assertNotIn(token.decode(), repr(capability))
        accepted = AcceptedSubmissionReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            submission_intent_sha256="sha256:" + "a" * 64,
            remote_task_identity="task-1",
            status_url="https://provider.invalid/task-1",
            result_url="https://provider.invalid/task-1/result",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            token_sha256=capability.token_sha256,
            token_byte_count=len(token),
            provider_protocol_version="mineru-task-protocol.v2",
        )
        self.assertTrue(capability.validates_accepted_submission(accepted))
        self.assertFalse(
            capability.validates_accepted_submission(
                replace(accepted, remote_task_identity="task-other")
            )
        )
        with self.assertRaisesRegex(ValueError, "hash drifted"):
            replace(capability, token_sha256="sha256:" + "0" * 64)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            replace(capability, capability_purpose="arbitrary")
        for field_name in (
            "attempt_id",
            "remote_task_identity",
            "provider_protocol_version",
            "secret_kind",
            "capability_purpose",
            "token_sha256",
        ):
            with (
                self.subTest(field_name=field_name),
                self.assertRaises(ValueError),
            ):
                replace(
                    capability,
                    **{
                        field_name: _EvilStr(
                            cast(str, getattr(capability, field_name))
                        )
                    },
                )

    def test_claim_witness_binds_exact_mutable_head_without_hashing_claim(self) -> None:
        fixture = _happy_path_for_port()
        reservation = fixture["reservation"]
        checkpoint = fixture["chain"][4]
        token = fixture["token"]
        capability = PrivateProviderCapabilityV4(
            attempt_id="attempt-1",
            remote_task_identity="task-1",
            provider_protocol_version="mineru-task-protocol.v2",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            capability_purpose="result_download",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        accepted = fixture["accepted"]
        preparation_intent = fixture["preparation"]
        terminal_receipt = fixture["terminal"]
        intent = fixture["intent"]
        replay_context = _materializing_replay(fixture)
        claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=2,
        )
        for field_name in (
            "attempt_id",
            "fence_identity",
            "state",
            "claim_owner_identity",
            "checkpoint_sha256",
        ):
            with (
                self.subTest(materialization_claim_field=field_name),
                self.assertRaises(ValueError),
            ):
                replace(
                    claim,
                    **{
                        field_name: _EvilStr(
                            cast(str, getattr(claim, field_name))
                        )
                    },
                )
        self.assertTrue(claim.validates(checkpoint))
        self.assertFalse(claim.validates(_happy_path_for_port()["chain"][5]))
        self.assertNotIn(b"claim_owner", checkpoint.canonical_bytes)
        max_int = (1 << 63) - 1
        self.assertEqual(
            replace(
                claim,
                lifecycle_version=max_int,
                claim_generation=max_int,
            ).claim_generation,
            max_int,
        )
        for field_name in ("lifecycle_version", "claim_generation"):
            with (
                self.subTest(bound_field=field_name),
                self.assertRaisesRegex(ValueError, "invalid"),
            ):
                replace(claim, **{field_name: max_int + 1})
        allowance = fixture["allowance"]
        validate_v4_materialization_authorization(
            checkpoint=checkpoint,
            reservation=reservation,
            preparation_intent=preparation_intent,
            intent=intent,
            accepted_submission=accepted,
            terminal_receipt=terminal_receipt,
            provider_capability=capability,
            claim=claim,
            allowance=allowance,
            replay_context=replay_context,
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=claim,
                allowance=allowance,
                replay_context=cast(V4EvidenceReplayContext, object()),
            )
        forged_predecessor = replace(
            checkpoint,
            previous_checkpoint_sha256=SHA_F,
        )
        with self.assertRaisesRegex(ValueError, "history drifted"):
            validate_v4_materialization_authorization(
                checkpoint=forged_predecessor,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=replace(
                    claim,
                    checkpoint_sha256=forged_predecessor.sha256,
                ),
                allowance=allowance,
                replay_context=_materializing_replay(
                    fixture,
                    checkpoint=forged_predecessor,
                ),
            )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=replace(
                    capability,
                    capability_purpose="result_acknowledgement",
                ),
                claim=claim,
                allowance=allowance,
                replay_context=replay_context,
            )

        forged_reservation = replace(
            reservation,
            prepared_submission_identity_sha256=SHA_F,
        )
        with self.assertRaisesRegex(ValueError, "reservation drifted"):
            validate_v4_materialization_authorization(
                checkpoint=checkpoint,
                reservation=forged_reservation,
                preparation_intent=preparation_intent,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=claim,
                allowance=allowance,
                replay_context=replay_context,
            )

        forged_intent = replace(
            intent,
            output_byte_limit=allowance.limits.output_bytes + 1,
        )
        forged_checkpoint = replace(
            checkpoint,
            materialization_intent_sha256=forged_intent.sha256,
        )
        forged_claim = replace(
            claim,
            checkpoint_sha256=forged_checkpoint.sha256,
        )
        with self.assertRaisesRegex(ValueError, "limits exceed exact allowance"):
            validate_v4_materialization_authorization(
                checkpoint=forged_checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=forged_intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=forged_claim,
                allowance=allowance,
                replay_context=_materializing_replay(
                    fixture,
                    checkpoint=forged_checkpoint,
                    intent=forged_intent,
                ),
            )

        forged_held_intent = replace(
            intent,
            held_resource_credit=replace(
                intent.held_resource_credit,
                decoded_bytes=intent.held_resource_credit.decoded_bytes - 1,
            ),
        )
        forged_held_checkpoint = replace(
            checkpoint,
            held_resource_credit=forged_held_intent.held_resource_credit,
            materialization_intent_sha256=forged_held_intent.sha256,
        )
        forged_held_claim = replace(
            claim,
            checkpoint_sha256=forged_held_checkpoint.sha256,
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=forged_held_checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=forged_held_intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=forged_held_claim,
                allowance=allowance,
                replay_context=_materializing_replay(
                    fixture,
                    checkpoint=forged_held_checkpoint,
                    intent=forged_held_intent,
                ),
            )

        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=checkpoint,
                reservation=reservation,
                preparation_intent=replace(
                    preparation_intent,
                    parser_target_sha256=SHA_F,
                ),
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=claim,
                allowance=allowance,
                replay_context=replay_context,
            )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=replace(
                    terminal_receipt,
                    artifact_sha256=SHA_F,
                ),
                provider_capability=capability,
                claim=claim,
                allowance=allowance,
                replay_context=replay_context,
            )

        foreign_context = replace(
            intent.provider_envelope_context,
            document_id="doc-unrelated",
        )
        foreign_document_intent = replace(
            intent,
            document_id=foreign_context.document_id,
            provider_envelope_context=foreign_context,
        )
        foreign_document_checkpoint = replace(
            checkpoint,
            materialization_intent_sha256=foreign_document_intent.sha256,
        )
        foreign_document_claim = replace(
            claim,
            checkpoint_sha256=foreign_document_checkpoint.sha256,
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=foreign_document_checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=foreign_document_intent,
                accepted_submission=accepted,
                terminal_receipt=terminal_receipt,
                provider_capability=capability,
                claim=foreign_document_claim,
                allowance=allowance,
                replay_context=replay_context,
            )

        foreign_submission_accepted = replace(
            accepted,
            submission_intent_sha256=SHA_F,
        )
        foreign_submission_terminal = replace(
            terminal_receipt,
            accepted_submission_receipt_sha256=foreign_submission_accepted.sha256,
        )
        foreign_submission_intent = replace(
            intent,
            terminal_receipt_sha256=foreign_submission_terminal.sha256,
        )
        foreign_submission_checkpoint = replace(
            checkpoint,
            accepted_submission_sha256=foreign_submission_accepted.sha256,
            terminal_receipt_sha256=foreign_submission_terminal.sha256,
            materialization_intent_sha256=foreign_submission_intent.sha256,
        )
        foreign_submission_claim = replace(
            claim,
            checkpoint_sha256=foreign_submission_checkpoint.sha256,
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_materialization_authorization(
                checkpoint=foreign_submission_checkpoint,
                reservation=reservation,
                preparation_intent=preparation_intent,
                intent=foreign_submission_intent,
                accepted_submission=foreign_submission_accepted,
                terminal_receipt=foreign_submission_terminal,
                provider_capability=capability,
                claim=foreign_submission_claim,
                allowance=allowance,
                replay_context=replay_context,
            )

        cleanup_checkpoint = fixture["chain"][7]
        cleanup_claim = V4ClaimWitness(
            attempt_id=cleanup_checkpoint.attempt_id,
            fence_identity=cleanup_checkpoint.fence_identity,
            state=cleanup_checkpoint.state,
            lifecycle_version=cleanup_checkpoint.lifecycle_version,
            checkpoint_sha256=cleanup_checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=2,
        )
        with self.assertRaises(ValueError):
            replace(
                cleanup_claim,
                claim_owner_identity=_EvilStr(
                    cleanup_claim.claim_owner_identity
                ),
            )
        validate_v4_cleanup_authorization(
            checkpoint=cleanup_checkpoint,
            source_checkpoint=fixture["chain"][6],
            reservation=reservation,
            intent=fixture["intent"],
            local_receipt=fixture["materialization"],
            plan=fixture["cleanup_plan"],
            claim=cleanup_claim,
            replay_context=_cleanup_replay(fixture),
        )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_cleanup_authorization(
                checkpoint=cleanup_checkpoint,
                source_checkpoint=fixture["chain"][6],
                reservation=reservation,
                intent=fixture["intent"],
                local_receipt=fixture["materialization"],
                plan=fixture["cleanup_plan"],
                claim=cast(V4ClaimWitness, _DuckClaim()),
                replay_context=_cleanup_replay(fixture),
            )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_v4_cleanup_authorization(
                checkpoint=cleanup_checkpoint,
                source_checkpoint=fixture["chain"][6],
                reservation=reservation,
                intent=cast(MaterializationIntentV4, object()),
                local_receipt=fixture["materialization"],
                plan=fixture["cleanup_plan"],
                claim=cleanup_claim,
                replay_context=_cleanup_replay(fixture),
            )
        with self.assertRaisesRegex(ValueError, "authorization drifted"):
            validate_v4_cleanup_authorization(
                checkpoint=cleanup_checkpoint,
                source_checkpoint=fixture["chain"][6],
                reservation=reservation,
                intent=fixture["intent"],
                local_receipt=cast(LocalMaterializationReceiptV4, object()),
                plan=fixture["cleanup_plan"],
                claim=cleanup_claim,
                replay_context=_cleanup_replay(fixture),
            )

        omitted_resources = tuple(
            item
            for item in fixture["cleanup_plan"].resources
            if item.kind != "spool"
        )
        forged_plan = replace(
            fixture["cleanup_plan"],
            resources=omitted_resources,
            resource_count=len(omitted_resources),
            resources_sha256=_cleanup_resources_sha256(omitted_resources),
        )
        forged_cleanup_checkpoint = replace(
            cleanup_checkpoint,
            cleanup_plan_sha256=forged_plan.sha256,
        )
        forged_cleanup_claim = replace(
            cleanup_claim,
            checkpoint_sha256=forged_cleanup_checkpoint.sha256,
        )
        with self.assertRaisesRegex(ValueError, "intent-owned namespace"):
            validate_v4_cleanup_authorization(
                checkpoint=forged_cleanup_checkpoint,
                source_checkpoint=fixture["chain"][6],
                reservation=reservation,
                intent=fixture["intent"],
                local_receipt=fixture["materialization"],
                plan=forged_plan,
                claim=forged_cleanup_claim,
                replay_context=_cleanup_replay(fixture, plan=forged_plan),
            )

        forged_cleanup_source = replace(
            fixture["chain"][6],
            previous_checkpoint_sha256=SHA_F,
        )
        forged_source_plan = build_local_cleanup_plan_v4(
            reservation=reservation,
            source_checkpoint=forged_cleanup_source,
            outcome="success",
            remote_task_identity=fixture["intent"].remote_task_identity,
            resources=fixture["cleanup_plan"].resources,
            materialization_intent=fixture["intent"],
            local_materialization_receipt=fixture["materialization"],
        )
        forged_source_pending = advance_remote_parse_checkpoint_v4(
            forged_cleanup_source,
            state="cleanup_pending",
            held_resource_credit=forged_cleanup_source.held_resource_credit,
            cleanup_plan_sha256=forged_source_plan.sha256,
        )
        with self.assertRaisesRegex(ValueError, "history drifted"):
            validate_v4_cleanup_authorization(
                checkpoint=forged_source_pending,
                source_checkpoint=forged_cleanup_source,
                reservation=reservation,
                intent=fixture["intent"],
                local_receipt=fixture["materialization"],
                plan=forged_source_plan,
                claim=V4ClaimWitness(
                    attempt_id=forged_source_pending.attempt_id,
                    fence_identity=forged_source_pending.fence_identity,
                    state=forged_source_pending.state,
                    lifecycle_version=forged_source_pending.lifecycle_version,
                    checkpoint_sha256=forged_source_pending.sha256,
                    claim_owner_identity="worker-1",
                    claim_generation=2,
                ),
                replay_context=_cleanup_replay(
                    fixture,
                    plan=forged_source_plan,
                    source_checkpoint=forged_cleanup_source,
                    history=(*fixture["chain"][:6], forged_cleanup_source),
                ),
            )

    def test_ack_command_closes_pending_cleanup_and_provider_request(self) -> None:
        fixture = _happy_path_for_port()
        token = fixture["token"]
        capability = PrivateProviderCapabilityV4(
            attempt_id="attempt-1",
            remote_task_identity="task-1",
            provider_protocol_version="mineru-task-protocol.v2",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            capability_purpose="result_acknowledgement",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        accepted = fixture["accepted"]
        terminal = fixture["terminal"]
        cleanup_plan = fixture["cleanup_plan"]
        cleanup_receipt = fixture["cleanup_receipt"]
        checkpoint = fixture["chain"][8]
        replay_context = _ack_replay(fixture)
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=checkpoint,
            accepted_submission=accepted,
            terminal_receipt=terminal,
            cleanup_plan=cleanup_plan,
            cleanup_receipt=cleanup_receipt,
            replay_context=replay_context,
        )
        self.assertEqual(
            command.cleanup_receipt.sha256,
            checkpoint.cleanup_receipt_sha256,
        )
        self.assertEqual(command.result_owner_identity, "result-owner-1")
        self.assertEqual(
            command.ack_request_sha256,
            "sha256:" + hashlib.sha256(command.ack_request_exact_bytes).hexdigest(),
        )
        self.assertEqual(
            command.request_identity,
            "provider-ack-v4." + command.ack_request_sha256.removeprefix("sha256:"),
        )
        self.assertEqual(
            command,
            seal_provider_ack_command_v4(
                ack_pending_checkpoint=checkpoint,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                cleanup_plan=cleanup_plan,
                cleanup_receipt=cleanup_receipt,
                replay_context=replay_context,
            ),
        )
        with self.assertRaisesRegex(ValueError, "terminal receipt drifted"):
            replace(
                command,
                terminal_receipt=replace(
                    terminal,
                    result_owner_identity="result-owner-other",
                ),
            )
        with self.assertRaisesRegex(ValueError, "evidence drifted"):
            replace(command, terminal_receipt=None)
        with self.assertRaisesRegex(ValueError, "evidence drifted"):
            replace(
                command,
                replay_context=cast(V4EvidenceReplayContext, object()),
            )
        forged_predecessor_receipt = replace(
            cleanup_receipt,
            cleanup_pending_checkpoint_sha256=SHA_F,
        )
        forged_predecessor_checkpoint = replace(
            checkpoint,
            previous_checkpoint_sha256=SHA_F,
            cleanup_receipt_sha256=forged_predecessor_receipt.sha256,
        )
        with self.assertRaisesRegex(ValueError, "cleanup-pending checkpoint"):
            seal_provider_ack_command_v4(
                ack_pending_checkpoint=forged_predecessor_checkpoint,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                cleanup_plan=cleanup_plan,
                cleanup_receipt=forged_predecessor_receipt,
                replay_context=_ack_replay(
                    fixture,
                    checkpoint=forged_predecessor_checkpoint,
                    cleanup_receipt=forged_predecessor_receipt,
                ),
            )
        foreign_plan = replace(
            cleanup_plan,
            attempt_id="attempt-foreign",
            fence_identity="fence-foreign",
            document_id="doc-foreign",
            processing_run_id="run-foreign",
        )
        foreign_receipt = replace(
            cleanup_receipt,
            attempt_id="attempt-foreign",
            fence_identity="fence-foreign",
            document_id="doc-foreign",
            processing_run_id="run-foreign",
            cleanup_plan_sha256=foreign_plan.sha256,
        )
        foreign_checkpoint = replace(
            checkpoint,
            cleanup_plan_sha256=foreign_plan.sha256,
            cleanup_receipt_sha256=foreign_receipt.sha256,
        )
        with self.assertRaisesRegex(ValueError, "evidence drifted"):
            seal_provider_ack_command_v4(
                ack_pending_checkpoint=foreign_checkpoint,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                cleanup_plan=foreign_plan,
                cleanup_receipt=foreign_receipt,
                replay_context=replay_context,
            )
        wrong_outcome_receipt = replace(
            cleanup_receipt,
            outcome="local_failure",
        )
        wrong_outcome_checkpoint = replace(
            checkpoint,
            cleanup_receipt_sha256=wrong_outcome_receipt.sha256,
        )
        with self.assertRaisesRegex(ValueError, "evidence drifted"):
            seal_provider_ack_command_v4(
                ack_pending_checkpoint=wrong_outcome_checkpoint,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                cleanup_plan=cleanup_plan,
                cleanup_receipt=wrong_outcome_receipt,
                replay_context=replay_context,
            )
        claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=2,
        )
        with self.assertRaises(ValueError):
            replace(
                claim,
                checkpoint_sha256=_EvilStr(claim.checkpoint_sha256),
            )
        with self.assertRaises(ValueError):
            replace(
                capability,
                token_sha256=_EvilStr(capability.token_sha256),
            )
        validate_v4_ack_authorization(
            command=command,
            provider_capability=capability,
            claim=claim,
        )
        with self.assertRaisesRegex(ValueError, "authorization drifted"):
            validate_v4_ack_authorization(
                command=command,
                provider_capability=cast(
                    PrivateProviderCapabilityV4,
                    _DuckAckCapability(),
                ),
                claim=claim,
            )
        with self.assertRaisesRegex(ValueError, "authorization drifted"):
            validate_v4_ack_authorization(
                command=command,
                provider_capability=capability,
                claim=cast(V4ClaimWitness, _DuckClaim()),
            )
        with self.assertRaisesRegex(ValueError, "authorization drifted"):
            validate_v4_ack_authorization(
                command=cast(ProviderAckCommandV4, object()),
                provider_capability=capability,
                claim=claim,
            )

        forged_result_credit = replace(
            checkpoint.held_resource_credit,
            provider_result_bytes=999,
        )
        forged_credit_checkpoint = replace(
            checkpoint,
            held_resource_credit=forged_result_credit,
        )
        with self.assertRaisesRegex(ValueError, "credit drifted"):
            seal_provider_ack_command_v4(
                ack_pending_checkpoint=forged_credit_checkpoint,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                cleanup_plan=cleanup_plan,
                cleanup_receipt=cleanup_receipt,
                replay_context=_ack_replay(
                    fixture,
                    checkpoint=forged_credit_checkpoint,
                ),
            )
        with self.assertRaisesRegex(
            ValueError,
            "retains cleaned local credit",
        ):
            replace(
                checkpoint,
                held_resource_credit=replace(
                    checkpoint.held_resource_credit,
                    output_items=1,
                    output_bytes=1,
                    output_pages=1,
                ),
            )

    def test_remote_failure_ack_has_no_terminal_or_result_owner(self) -> None:
        fixture = _happy_path_for_port()
        token = fixture["token"]
        capability = PrivateProviderCapabilityV4(
            attempt_id="attempt-1",
            remote_task_identity="task-1",
            provider_protocol_version="mineru-task-protocol.v2",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            capability_purpose="result_acknowledgement",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        accepted = fixture["accepted"]
        submitted = fixture["chain"][2]
        reservation = fixture["reservation"]
        failure = FailureReceiptV4(
            attempt_id=reservation.attempt_id,
            fence_identity=reservation.fence_identity,
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
        plan = build_local_cleanup_plan_v4(
            reservation=reservation,
            source_checkpoint=submitted,
            outcome="remote_failure",
            remote_task_identity="task-1",
            failure_receipt_sha256=failure.sha256,
            resources=(
                CleanupResourceEntryV4(
                    kind="snapshot",
                    relpath=reservation.snapshot_relpath,
                    ownership_basis_sha256=reservation.sha256,
                    expected_sha256=reservation.source_pdf_sha256,
                    expected_byte_count=reservation.source_byte_count,
                    action="delete",
                ),
            ),
        )
        cleanup_pending = advance_remote_parse_checkpoint_v4(
            submitted,
            state="cleanup_pending",
            held_resource_credit=submitted.held_resource_credit,
            failure_receipt_sha256=failure.sha256,
            cleanup_plan_sha256=plan.sha256,
        )
        cleanup_receipt = build_local_cleanup_receipt_v4(
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
        checkpoint = advance_remote_parse_checkpoint_v4(
            cleanup_pending,
            state="ack_pending",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                provider_tasks=1,
                ack_items=1,
            ),
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
        replay_context = V4EvidenceReplayContext(
            evidence=_encoded_evidence(
                fixture["preparation"],
                fixture["snapshot"],
                fixture["submission"],
                accepted,
                failure,
                plan,
                cleanup_receipt,
            ),
            reservation=reservation,
            resourceful_checkpoint_history=fixture["chain"][:3],
            cleanup_source_checkpoint=submitted,
            cleanup_pending_checkpoint=cleanup_pending,
            ack_pending_checkpoint=checkpoint,
        )
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=checkpoint,
            accepted_submission=accepted,
            terminal_receipt=None,
            cleanup_plan=plan,
            cleanup_receipt=cleanup_receipt,
            replay_context=replay_context,
        )
        self.assertIsNone(command.result_owner_identity)
        self.assertIsNone(command.terminal_receipt)
        self.assertIn(b'"terminal_receipt_sha256":null', command.ack_request_exact_bytes)
        claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=2,
        )
        validate_v4_ack_authorization(
            command=command,
            provider_capability=capability,
            claim=claim,
        )
        forged_result_credit = replace(
            checkpoint.held_resource_credit,
            provider_result_bytes=999,
        )
        with self.assertRaisesRegex(ValueError, "invented provider result"):
            replace(
                checkpoint,
                held_resource_credit=forged_result_credit,
            )


def _happy_path_for_port() -> _PortHappyPathFixture:
    reservation, allowance = _exact_materialization_reservation_and_allowance()
    reservation_input = allowance.reservation_input
    token = b"x" * 32
    token_sha256 = "sha256:" + hashlib.sha256(token).hexdigest()
    provider_context = _provider_envelope_context()
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=provider_context.parser_target_sha256,
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
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    submission = SubmissionIntentV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=reservation.source_pdf_sha256,
        parser_target_sha256=preparation.parser_target_sha256,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
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
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        submission_intent_sha256=submission.sha256,
        remote_task_identity="task-1",
        status_url="https://provider.invalid/tasks/task-1",
        result_url="https://provider.invalid/tasks/task-1/result",
        secret_kind="mineru-task-token.v1",
        secret_version=1,
        token_sha256=token_sha256,
        token_byte_count=len(token),
        provider_protocol_version=submission.provider_protocol_version,
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=_submitted_credit(),
        accepted_submission_sha256=accepted.sha256,
    )
    terminal = TerminalReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        accepted_submission_receipt_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity="result-owner-1",
        artifact_sha256=SHA_B,
        artifact_byte_count=20,
        provider_protocol_version=accepted.provider_protocol_version,
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
        provider_envelope_context=provider_context,
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
        publication_winner_sha256="sha256:" + "e" * 64,
    )
    resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=reservation.snapshot_relpath,
            ownership_basis_sha256=reservation.sha256,
            expected_sha256=reservation.source_pdf_sha256,
            expected_byte_count=reservation.source_byte_count,
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
        CleanupResourceEntryV4(
            kind="output",
            relpath=intent.output_relpath,
            ownership_basis_sha256=materialization.sha256,
            expected_sha256=materialization.output_files_sha256,
            expected_byte_count=materialization.output_byte_count,
            action="transfer",
            target_owner_identity=intent.processing_run_id,
            target_relpath=(
                intent.provider_envelope_context.parser_artifact_root_relpath
            ),
        ),
    )
    cleanup_plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=published,
        outcome="success",
        remote_task_identity=intent.remote_task_identity,
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
                target_owner_identity=intent.processing_run_id,
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
    return {
        "reservation": reservation,
        "reservation_input": reservation_input,
        "allowance": allowance,
        "token": token,
        "preparation": preparation,
        "snapshot": snapshot,
        "submission": submission,
        "accepted": accepted,
        "terminal": terminal,
        "intent": intent,
        "manifest": manifest,
        "provider_envelope": provider_envelope,
        "materialization": materialization,
        "cleanup_plan": cleanup_plan,
        "cleanup_receipt": cleanup_receipt,
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
        ),
    }


def _encoded_evidence(*values):
    return tuple(encode_remote_parse_evidence_v4(value) for value in values)


def _materializing_replay(
    fixture: _PortHappyPathFixture,
    *,
    checkpoint: RemoteParseCheckpointV4 | None = None,
    intent: MaterializationIntentV4 | None = None,
) -> V4EvidenceReplayContext:
    current = fixture["chain"][4] if checkpoint is None else checkpoint
    exact_intent = fixture["intent"] if intent is None else intent
    return V4EvidenceReplayContext(
        evidence=_encoded_evidence(
            fixture["preparation"],
            fixture["snapshot"],
            fixture["submission"],
            fixture["accepted"],
            fixture["terminal"],
            exact_intent,
        ),
        reservation=fixture["reservation"],
        resourceful_checkpoint_history=(*fixture["chain"][:4], current),
    )


def _cleanup_replay(
    fixture: _PortHappyPathFixture,
    *,
    plan: LocalCleanupPlanV4 | None = None,
    source_checkpoint: RemoteParseCheckpointV4 | None = None,
    history: tuple[RemoteParseCheckpointV4, ...] | None = None,
) -> V4EvidenceReplayContext:
    exact_plan = fixture["cleanup_plan"] if plan is None else plan
    exact_source = (
        fixture["chain"][6] if source_checkpoint is None else source_checkpoint
    )
    return V4EvidenceReplayContext(
        evidence=_encoded_evidence(
            fixture["preparation"],
            fixture["snapshot"],
            fixture["submission"],
            fixture["accepted"],
            fixture["terminal"],
            fixture["intent"],
            fixture["materialization"],
            exact_plan,
        ),
        reservation=fixture["reservation"],
        resourceful_checkpoint_history=(
            fixture["chain"][:7] if history is None else history
        ),
        cleanup_source_checkpoint=exact_source,
        local_materialization_manifest=fixture["manifest"],
        provider_envelope=fixture["provider_envelope"],
    )


def _ack_replay(
    fixture: _PortHappyPathFixture,
    *,
    checkpoint: RemoteParseCheckpointV4 | None = None,
    cleanup_receipt: LocalCleanupReceiptV4 | None = None,
) -> V4EvidenceReplayContext:
    current = fixture["chain"][8] if checkpoint is None else checkpoint
    exact_receipt = (
        fixture["cleanup_receipt"]
        if cleanup_receipt is None
        else cleanup_receipt
    )
    return V4EvidenceReplayContext(
        evidence=_encoded_evidence(
            fixture["preparation"],
            fixture["snapshot"],
            fixture["submission"],
            fixture["accepted"],
            fixture["terminal"],
            fixture["intent"],
            fixture["materialization"],
            fixture["cleanup_plan"],
            exact_receipt,
        ),
        reservation=fixture["reservation"],
        resourceful_checkpoint_history=fixture["chain"][:7],
        cleanup_source_checkpoint=fixture["chain"][6],
        cleanup_pending_checkpoint=fixture["chain"][7],
        ack_pending_checkpoint=current,
        local_materialization_manifest=fixture["manifest"],
        provider_envelope=fixture["provider_envelope"],
    )


def _materialized_provider_document_v4() -> MaterializedProviderDocumentV4:
    base_intent = _happy_path_for_port()["intent"]
    assert isinstance(base_intent, MaterializationIntentV4)
    # The provider envelope is deliberately realistic (rather than the tiny
    # authorization fixture), so its sealed evidence must carry ceilings that
    # can honestly contain the canonical envelope and parser artifacts.
    intent = replace(
        base_intent,
        member_count_limit=64,
        uncompressed_byte_limit=64 * 1024,
        decoded_byte_limit=64 * 1024,
        temporary_disk_byte_limit=128 * 1024,
        output_byte_limit=64 * 1024,
    )
    envelope = _provider_envelope_for_intent(intent)
    envelope_bytes = provider_document_envelope_to_bytes(envelope)
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
                    for artifact in envelope.provider_document.artifacts
                ),
                LocalMaterializationPayloadFileV4(
                    role="provider_envelope",
                    relpath=PROVIDER_DOCUMENT_FILENAME,
                    sha256=(
                        "sha256:" + hashlib.sha256(envelope_bytes).hexdigest()
                    ),
                    byte_count=len(envelope_bytes),
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    payload_bytes = sum(item.byte_count for item in payload_files)
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
        provider_envelope_relpath=intent.provider_envelope_relpath,
        provider_envelope_sha256=(
            "sha256:" + hashlib.sha256(envelope_bytes).hexdigest()
        ),
        provider_envelope_byte_count=len(envelope_bytes),
        observations=LocalMaterializationObservationsV4(
            member_count=len(envelope.provider_document.artifacts),
            uncompressed_byte_count=payload_bytes,
            decoded_byte_count=payload_bytes,
            temporary_disk_peak_byte_count=payload_bytes * 2,
            output_file_count=len(payload_files),
            output_byte_count=payload_bytes,
        ),
        payload_files=payload_files,
    )
    return MaterializedProviderDocumentV4(
        receipt=_receipt_for_manifest(manifest),
        intent=intent,
        provider_envelope=envelope,
        manifest=manifest,
    )


def _provider_envelope_for_intent(
    intent: MaterializationIntentV4,
) -> ProviderDocumentEnvelope:
    base = _provider_document_envelope()
    first_page = base.provider_document.pages[0]
    provider_document = replace(
        base.provider_document,
        source_pdf_sha256=intent.source_pdf_sha256,
        pages=(first_page, replace(first_page, page_index=1, blocks=())),
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


def _receipt_for_manifest(
    manifest: LocalMaterializationManifestV4,
) -> LocalMaterializationReceiptV4:
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
                    for item in manifest.payload_files
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    return LocalMaterializationReceiptV4(
        attempt_id=manifest.attempt_id,
        fence_identity=manifest.fence_identity,
        document_id=manifest.document_id,
        processing_run_id=manifest.processing_run_id,
        materialization_intent_sha256=manifest.materialization_intent_sha256,
        terminal_receipt_sha256=manifest.terminal_receipt_sha256,
        source_pdf_sha256=manifest.source_pdf_sha256,
        source_page_count=manifest.source_page_count,
        parser_target_sha256=manifest.parser_target_sha256,
        spool_relpath=manifest.spool_relpath,
        spool_sha256=manifest.artifact_sha256,
        spool_byte_count=manifest.artifact_byte_count,
        member_count=manifest.observations.member_count,
        uncompressed_byte_count=manifest.observations.uncompressed_byte_count,
        decoded_byte_count=manifest.observations.decoded_byte_count,
        temporary_disk_peak_byte_count=(
            manifest.observations.temporary_disk_peak_byte_count
        ),
        output_relpath=manifest.output_relpath,
        output_files=output_files,
        output_file_count=len(output_files),
        output_byte_count=sum(item.byte_count for item in output_files),
        output_files_sha256=local_output_files_sha256_v4(output_files),
        provider_envelope_relpath=manifest.provider_envelope_relpath,
        provider_envelope_sha256=manifest.provider_envelope_sha256,
        provider_envelope_byte_count=manifest.provider_envelope_byte_count,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        output_manifest_sha256=manifest.sha256,
        output_manifest_byte_count=len(manifest.canonical_bytes),
        file_fsync_completed=True,
        output_parent_fsync_completed=True,
        marker_removed=True,
        spool_part_absent=True,
        spool_part_owner_absent=True,
        staging_absent=True,
    )


def _cleanup_resources_sha256(
    resources: tuple[CleanupResourceEntryV4, ...],
) -> str:
    exact = json.dumps(
        [asdict(item) for item in resources],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(exact).hexdigest()


if __name__ == "__main__":
    unittest.main()
