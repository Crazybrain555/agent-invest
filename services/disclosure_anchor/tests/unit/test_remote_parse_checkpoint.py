from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    FailureReceipt,
    LocalMaterializationReceipt,
    LocalMaterializationReceiptV2,
    PreparedMaterializationReceiptV2,
    EncodedTerminalReceipt,
    RemoteParseAttempt,
    RemoteParseResumeSecret,
    TerminalReceipt,
    decode_terminal_receipt,
    encode_terminal_receipt,
    decode_checkpoint_receipt,
    encode_checkpoint_receipt,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    build_staged_credit_envelope,
    credit_shape,
)
from tests.unit.test_mineru_process_profile import _profile

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _receipt() -> TerminalReceipt:
    return TerminalReceipt(
        attempt_identity="attempt_1",
        fence_identity="fence_1",
        source_pdf_sha256=SHA_A,
        artifact_owner_identity="owner_1",
        artifact_byte_count=123,
        artifact_sha256=SHA_B,
        resume_token_sha256=SHA_C,
    )


class RemoteParseCheckpointContractTests(unittest.TestCase):
    def test_v3_attempt_binds_reservation_and_exact_current_shape(self) -> None:
        local_projection = next(
            constraint
            for constraint in models.RemoteParseAttempt.__table__.constraints
            if constraint.name == "ck_remote_parse_attempt_v3_local_projection"
        )
        self.assertIn(
            "local_db_staged_byte_count IS NOT NULL",
            str(local_projection.sqltext),
        )
        envelope = build_staged_credit_envelope(
            profile=_profile(),
            source_pdf_sha256=SHA_A,
            source_byte_count=1024,
            source_page_count=2,
        )
        attempt = RemoteParseAttempt(
            attempt_id="attempt_1",
            processing_run_id="run_1",
            document_id="document_1",
            attempt_generation=1,
            fence_identity="fence_1",
            source_pdf_sha256=SHA_A,
            parser_target_sha256=SHA_B,
            request_sha256=SHA_C,
            runtime_epoch_sha256=SHA_A,
            client_submit_key="submit_1",
            checkpoint_contract_version=3,
            process_profile_sha256=envelope.process_profile_sha256,
            credit_policy_sha256=envelope.credit_policy_sha256,
            reservation_input_bytes=envelope.reservation_input.exact_bytes,
            reservation_input_sha256=envelope.reservation_input.sha256,
            reservation_input_byte_count=envelope.reservation_input.byte_count,
            reservation_source_byte_count=1024,
            reservation_source_page_count=2,
            reservation_bucket=envelope.reservation_input.value.bucket,
            reservation=envelope.reservation,
            current_credits=credit_shape("prepared", CreditShapeFacts()),
        )
        self.assertEqual(attempt.current_credits.documents, 1)
        with self.assertRaisesRegex(ValueError, "current credit projection"):
            replace(
                attempt,
                current_credits=replace(attempt.current_credits, remote_waits=1),
            )
        with self.assertRaisesRegex(ValueError, "reservation evidence"):
            replace(attempt, reservation_source_page_count=3)

    def test_v2_materialization_receipts_are_closed_replayable_evidence(self) -> None:
        prepared = PreparedMaterializationReceiptV2(
            attempt_identity="attempt_1",
            fence_identity="fence_1",
            source_pdf_sha256=SHA_A,
            source_page_count=4,
            terminal_receipt_sha256=SHA_B,
            process_profile_sha256=SHA_C,
            credit_policy_sha256=SHA_A,
            reservation_input_sha256=SHA_B,
            spool_relpath="attempt_1/result.zip",
            spool_sha256=SHA_C,
            spool_byte_count=10,
            compressed_byte_count=10,
            uncompressed_byte_count=40,
            member_count=2,
            temporary_disk_byte_count=50,
            decoded_byte_count=60,
            private_token_sha256=SHA_A,
        )
        encoded_prepared = encode_checkpoint_receipt(prepared)
        self.assertEqual(
            decode_checkpoint_receipt(encoded_prepared.exact_bytes), encoded_prepared
        )
        local = LocalMaterializationReceiptV2(
            attempt_identity="attempt_1",
            fence_identity="fence_1",
            claim_generation=2,
            source_pdf_sha256=SHA_A,
            source_page_count=4,
            parser_target_sha256=SHA_B,
            terminal_receipt_sha256=SHA_C,
            process_profile_sha256=SHA_A,
            credit_policy_sha256=SHA_B,
            reservation_input_sha256=SHA_C,
            prepared_materialization_sha256=encoded_prepared.sha256,
            artifact_owner_identity="owner_1",
            artifact_sha256=SHA_A,
            artifact_byte_count=10,
            output_manifest_sha256=SHA_B,
            output_manifest_relpath="run/manifest.json",
            output_manifest_byte_count=20,
            artifact_root_relpath="run/artifacts",
            provider_envelope_relpath="run/provider.json",
            provider_envelope_sha256=SHA_C,
            provider_envelope_byte_count=30,
            compressed_byte_count=10,
            uncompressed_byte_count=40,
            member_count=2,
            temporary_disk_byte_count=50,
            decoded_byte_count=60,
            db_staged_byte_count=30,
        )
        encoded_local = encode_checkpoint_receipt(local)
        self.assertEqual(decode_checkpoint_receipt(encoded_local.exact_bytes), encoded_local)

    def test_v2_materialization_receipts_reject_unsafe_paths_and_bool_counts(self) -> None:
        base = PreparedMaterializationReceiptV2(
            attempt_identity="attempt_1", fence_identity="fence_1",
            source_pdf_sha256=SHA_A, source_page_count=4,
            terminal_receipt_sha256=SHA_B, process_profile_sha256=SHA_C,
            credit_policy_sha256=SHA_A, reservation_input_sha256=SHA_B,
            spool_relpath="attempt_1/result.zip", spool_sha256=SHA_C,
            spool_byte_count=10, compressed_byte_count=10,
            uncompressed_byte_count=40, member_count=2,
            temporary_disk_byte_count=50,
            decoded_byte_count=60, private_token_sha256=SHA_A,
        )
        for relpath in ("/tmp/result.zip", "../result.zip", "a\\result.zip", "a//b"):
            with self.subTest(relpath=relpath), self.assertRaisesRegex(
                ValueError, "relpath"
            ):
                replace(base, spool_relpath=relpath)
        with self.assertRaisesRegex(ValueError, "source pages"):
            replace(base, source_page_count=True)
        with self.assertRaisesRegex(ValueError, "spool/compressed"):
            replace(base, compressed_byte_count=11)
        with self.assertRaisesRegex(ValueError, "temporary disk peak drifted"):
            replace(base, temporary_disk_byte_count=49)
        with self.assertRaisesRegex(ValueError, "overflowed"):
            replace(
                base,
                spool_byte_count=(1 << 63) - 1,
                compressed_byte_count=(1 << 63) - 1,
                uncompressed_byte_count=1,
                temporary_disk_byte_count=(1 << 63) - 1,
            )

        local = LocalMaterializationReceiptV2(
            attempt_identity="attempt_1", fence_identity="fence_1",
            claim_generation=1, source_pdf_sha256=SHA_A, source_page_count=4,
            parser_target_sha256=SHA_B, terminal_receipt_sha256=SHA_C,
            process_profile_sha256=SHA_A, credit_policy_sha256=SHA_B,
            reservation_input_sha256=SHA_C,
            prepared_materialization_sha256=SHA_A,
            artifact_owner_identity="owner_1", artifact_sha256=SHA_B,
            artifact_byte_count=10, output_manifest_sha256=SHA_C,
            output_manifest_relpath="run/manifest.json",
            output_manifest_byte_count=20, artifact_root_relpath="run/artifacts",
            provider_envelope_relpath="run/provider.json",
            provider_envelope_sha256=SHA_A, provider_envelope_byte_count=30,
            compressed_byte_count=10, uncompressed_byte_count=40,
            member_count=2, temporary_disk_byte_count=50,
            decoded_byte_count=60, db_staged_byte_count=30,
        )
        with self.assertRaisesRegex(ValueError, "temporary disk peak drifted"):
            replace(local, temporary_disk_byte_count=51)

    def test_v3_secret_kinds_are_closed_and_materialization_is_private(self) -> None:
        token = b"private-materialization-token"
        common = dict(
            attempt_id="attempt_1",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
            secret_contract_version=3,
        )
        for kind in (
            "prepared_reconcile", "accepted_submission", "terminal", "materialization"
        ):
            secret = RemoteParseResumeSecret(secret_kind=kind, **common)  # type: ignore[arg-type]
            self.assertNotIn(token.decode(), repr(secret))
        with self.assertRaisesRegex(ValueError, "secret kind"):
            RemoteParseResumeSecret(secret_kind="ack", **common)  # type: ignore[arg-type]

    def test_local_and_failure_receipts_are_closed_canonical_evidence(self) -> None:
        local = LocalMaterializationReceipt(
            attempt_identity="attempt_1", fence_identity="fence_1",
            claim_generation=1, source_pdf_sha256=SHA_A,
            parser_target_sha256=SHA_B, terminal_receipt_sha256=SHA_C,
            artifact_owner_identity="owner_1", artifact_sha256=SHA_A,
            artifact_byte_count=10, output_manifest_sha256=SHA_B,
            output_manifest_relpath="run/manifest.json", output_manifest_byte_count=20,
            artifact_root_relpath="run/artifacts", provider_envelope_relpath="run/provider.json",
            provider_envelope_sha256=SHA_C, provider_envelope_byte_count=30,
            compressed_byte_count=10, uncompressed_byte_count=40, member_count=2,
            disk_byte_count=50, decoded_byte_count=60,
        )
        encoded = encode_checkpoint_receipt(local)
        self.assertEqual(decode_checkpoint_receipt(encoded.exact_bytes), encoded)
        failure = FailureReceipt(
            attempt_identity="attempt_1", fence_identity="fence_1", stage="local",
            accepted=True, ack_required=True, submission_was_attempted=True,
            remote_task_identity="task_1",
            claim_generation=2, terminal_receipt_sha256=SHA_C,
            error_code="invalid_result", error_class="local_materialization",
            error_stage="materialize",
            retryable=False, retry_budget_class="item", message="closed failure",
        )
        self.assertEqual(
            decode_checkpoint_receipt(encode_checkpoint_receipt(failure).exact_bytes).receipt,
            failure,
        )

    def test_failure_receipt_rejects_ambiguous_or_cross_stage_shapes(self) -> None:
        for fields in (
            dict(stage="remote", accepted=False, ack_required=False,
                 submission_was_attempted=True,
                 remote_task_identity=None, terminal_receipt_sha256=None,
                 error_class="pre_submission"),
            dict(stage="remote", accepted=True, ack_required=False,
                 submission_was_attempted=True,
                 remote_task_identity="task", terminal_receipt_sha256=None,
                 error_class="remote_terminal"),
            dict(stage="remote", accepted=True, ack_required=True,
                 submission_was_attempted=True,
                 remote_task_identity="", terminal_receipt_sha256=None,
                 error_class="remote_terminal"),
            dict(stage="local", accepted=True, ack_required=True,
                 submission_was_attempted=True,
                 remote_task_identity="task", terminal_receipt_sha256=SHA_A,
                 error_class="remote_terminal"),
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                FailureReceipt(
                    attempt_identity="attempt_1", fence_identity="fence_1",
                    claim_generation=1, error_code="closed_error", **fields,
                    error_stage="materialize",
                    retryable=False, retry_budget_class="item", message="closed failure",
                )
    def test_receipt_round_trip_binds_exact_canonical_bytes_hash_and_length(self) -> None:
        encoded = encode_terminal_receipt(_receipt())
        replayed = decode_terminal_receipt(encoded.exact_bytes)
        self.assertEqual(replayed, encoded)
        self.assertEqual(encoded.byte_count, len(encoded.exact_bytes))
        self.assertEqual(
            encoded.sha256,
            "sha256:" + hashlib.sha256(encoded.exact_bytes).hexdigest(),
        )

    def test_receipt_rejects_duplicate_noncanonical_nonfinite_bool_and_bad_sha(self) -> None:
        encoded = encode_terminal_receipt(_receipt()).exact_bytes
        duplicate = encoded[:-1] + b',"schema":"remote_parse_terminal_receipt.v1"}'
        with self.assertRaisesRegex(ValueError, "duplicate"):
            decode_terminal_receipt(duplicate)
        with self.assertRaisesRegex(ValueError, "canonical"):
            decode_terminal_receipt(encoded.replace(b'"artifact_byte_count":123', b'"artifact_byte_count": 123'))
        with self.assertRaisesRegex(ValueError, "exact integer"):
            decode_terminal_receipt(encoded.replace(b'"artifact_byte_count":123', b'"artifact_byte_count":true'))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            decode_terminal_receipt(encoded.replace(b'"artifact_byte_count":123', b'"artifact_byte_count":NaN'))
        with self.assertRaisesRegex(ValueError, "canonical artifact SHA"):
            replace(_receipt(), artifact_sha256="sha256:BAD")

    def test_encoded_receipt_cannot_be_forged_from_inconsistent_fields(self) -> None:
        encoded = encode_terminal_receipt(_receipt())
        with self.assertRaisesRegex(ValueError, "SHA differs"):
            replace(encoded, sha256=SHA_A)
        with self.assertRaisesRegex(ValueError, "byte count differs"):
            replace(encoded, byte_count=encoded.byte_count + 1)
        with self.assertRaisesRegex(ValueError, "projection differs"):
            EncodedTerminalReceipt(
                receipt=replace(_receipt(), artifact_owner_identity="other"),
                exact_bytes=encoded.exact_bytes,
                sha256=encoded.sha256,
                byte_count=encoded.byte_count,
            )

    def test_secret_repr_omits_bytes_and_identity_is_exact(self) -> None:
        token = b"opaque-private-resume-token"
        secret = RemoteParseResumeSecret(
            attempt_id="attempt_1",
            secret_kind="terminal",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        self.assertNotIn(token.decode(), repr(secret))
        with self.assertRaisesRegex(ValueError, "differs"):
            RemoteParseResumeSecret(
                attempt_id="attempt_1",
                secret_kind="terminal",
                token_bytes=token,
                token_sha256=SHA_A,
                token_byte_count=len(token),
            )
        for invalid_kind in ("", "other", "submission"):
            with self.subTest(secret_kind=invalid_kind), self.assertRaisesRegex(
                ValueError, "secret kind"
            ):
                RemoteParseResumeSecret(
                    attempt_id="attempt_1",
                    secret_kind=invalid_kind,  # type: ignore[arg-type]
                    token_bytes=token,
                    token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                    token_byte_count=len(token),
                )
        with self.assertRaisesRegex(ValueError, "private envelope"):
            RemoteParseResumeSecret(
                attempt_id="attempt_1",
                secret_kind="terminal",
                token_bytes=bytearray(token),  # type: ignore[arg-type]
                token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                token_byte_count=len(token),
            )
        with self.assertRaisesRegex(ValueError, "identity differs"):
            RemoteParseResumeSecret(
                attempt_id="attempt_1",
                secret_kind="terminal",
                token_bytes=token,
                token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                token_byte_count=True,
            )


if __name__ == "__main__":
    unittest.main()
