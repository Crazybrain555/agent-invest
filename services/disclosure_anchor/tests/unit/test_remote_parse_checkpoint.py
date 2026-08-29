from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    EncodedTerminalReceipt,
    RemoteParseResumeSecret,
    TerminalReceipt,
    decode_terminal_receipt,
    encode_terminal_receipt,
)

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
        for invalid_kind in ("", "other"):
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
