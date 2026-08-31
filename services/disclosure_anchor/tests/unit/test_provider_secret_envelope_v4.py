from __future__ import annotations

import hashlib
import logging
import unittest
from dataclasses import replace
from types import SimpleNamespace

from disclosure_anchor.adapters.security.provider_secret_cipher import (
    AesGcmProviderSecretCipher,
)
from disclosure_anchor.adapters.security.provider_secret_keyring import (
    StaticProviderSecretKeyring,
)
from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    PROVIDER_SECRET_MAX_TOKEN_BYTES,
    ProviderSecretBindingV4,
    ProviderSecretPlaintextV4,
    SealedProviderSecretV4,
    bind_provider_secret_v4,
    provider_secret_data_aad_v4,
    provider_secret_wrap_aad_v4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
)
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretAuthenticationFailed,
    ProviderSecretEnvelopeInvalid,
    ProviderSecretKekUnknown,
    ProviderSecretKeyringInvalid,
    ProviderSecretSealInvalid,
)

_TOKEN = bytes(range(17))
_TOKEN_SHA = "sha256:" + hashlib.sha256(_TOKEN).hexdigest()
_KEK_PRIMARY = bytes(range(32))
_KEK_LEGACY = bytes(range(32, 64))


def _binding(token: bytes = _TOKEN) -> ProviderSecretBindingV4:
    return ProviderSecretBindingV4(
        attempt_id="attempt-9f",
        fence_identity="fence-3c",
        secret_kind="mineru-task-token.v1",
        provider_secret_version=7,
        token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        token_byte_count=len(token),
    )


def _plaintext(token: bytes = _TOKEN) -> ProviderSecretPlaintextV4:
    return ProviderSecretPlaintextV4(binding=_binding(token), token=token)


def _golden_binding() -> ProviderSecretBindingV4:
    return ProviderSecretBindingV4(
        attempt_id="attempt-9f",
        fence_identity="fence-3c",
        secret_kind="mineru-task-token.v1",
        provider_secret_version=7,
        token_sha256="sha256:" + "a" * 64,
        token_byte_count=17,
    )


def _keyring() -> StaticProviderSecretKeyring:
    return StaticProviderSecretKeyring(
        primary_kek_id="kek-primary",
        keks={"kek-primary": _KEK_PRIMARY, "kek-legacy": _KEK_LEGACY},
    )


def _receipt() -> AcceptedSubmissionReceiptV4:
    return AcceptedSubmissionReceiptV4(
        attempt_id="attempt-9f",
        fence_identity="fence-3c",
        submission_intent_sha256="sha256:" + "b" * 64,
        remote_task_identity="task-77",
        status_url="https://mineru.internal/api/v4/tasks/77",
        result_url="https://mineru.internal/api/v4/tasks/77/result",
        secret_kind="mineru-task-token.v1",
        secret_version=7,
        token_sha256=_TOKEN_SHA,
        token_byte_count=len(_TOKEN),
        provider_protocol_version="mineru-http-staged.v4",
    )


def _flip(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 0x01]) + data[index + 1 :]


class _ScriptedRng:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = list(chunks)

    def __call__(self, count: int) -> bytes:
        return self._chunks.pop(0)  # type: ignore[return-value]


class ProviderSecretBindingTests(unittest.TestCase):
    def test_binding_maps_exactly_from_accepted_receipt(self) -> None:
        receipt = _receipt()
        binding = bind_provider_secret_v4(receipt)
        self.assertEqual(binding.attempt_id, receipt.attempt_id)
        self.assertEqual(binding.fence_identity, receipt.fence_identity)
        self.assertEqual(binding.secret_kind, receipt.secret_kind)
        self.assertEqual(binding.provider_secret_version, receipt.secret_version)
        self.assertEqual(binding.token_sha256, receipt.token_sha256)
        self.assertEqual(binding.token_byte_count, receipt.token_byte_count)

    def test_binding_rejects_non_receipt_input(self) -> None:
        lookalike = SimpleNamespace(
            attempt_id="attempt-9f",
            fence_identity="fence-3c",
            secret_kind="mineru-task-token.v1",
            secret_version=7,
            token_sha256=_TOKEN_SHA,
            token_byte_count=len(_TOKEN),
        )
        for value in (None, lookalike):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    bind_provider_secret_v4(value)  # type: ignore[arg-type]

    def test_binding_field_validation(self) -> None:
        rejected = (
            {"attempt_id": ""},
            {"attempt_id": 123},
            {"attempt_id": " padded "},
            {"fence_identity": "bad\x00fence"},
            {"secret_kind": ""},
            {"secret_kind": "x" * 129},
            {"provider_secret_version": 0},
            {"provider_secret_version": True},
            {"token_sha256": "sha256:" + "A" * 64},
            {"token_sha256": "md5:" + "a" * 64},
            {"token_byte_count": 0},
            {"token_byte_count": PROVIDER_SECRET_MAX_TOKEN_BYTES + 1},
            {"contract_version": "provider-secret-binding.v3"},
        )
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    replace(_golden_binding(), **overrides)

    def test_binding_canonical_bytes_golden(self) -> None:
        expected = (
            b'{"attempt_id":"attempt-9f",'
            b'"contract_version":"provider-secret-binding.v4",'
            b'"fence_identity":"fence-3c",'
            b'"provider_secret_version":7,'
            b'"secret_kind":"mineru-task-token.v1",'
            b'"token_byte_count":17,'
            b'"token_sha256":"sha256:' + b"a" * 64 + b'"}'
        )
        self.assertEqual(_golden_binding().canonical_bytes, expected)
        self.assertEqual(
            _golden_binding().sha256,
            "sha256:" + hashlib.sha256(expected).hexdigest(),
        )


class ProviderSecretAadTests(unittest.TestCase):
    def test_data_aad_golden_bytes(self) -> None:
        expected = (
            b'{"aad_contract":"provider-secret-data-aad.v4",'
            b'"attempt_id":"attempt-9f",'
            b'"fence_identity":"fence-3c",'
            b'"provider_secret_version":7,'
            b'"secret_kind":"mineru-task-token.v1",'
            b'"token_byte_count":17,'
            b'"token_sha256":"sha256:' + b"a" * 64 + b'"}'
        )
        self.assertEqual(provider_secret_data_aad_v4(_golden_binding()), expected)

    def test_wrap_aad_golden_bytes(self) -> None:
        token_ciphertext = b"\x5a" * 33
        ciphertext_sha = (
            "sha256:" + hashlib.sha256(token_ciphertext).hexdigest()
        ).encode("utf-8")
        expected = (
            b'{"aad_contract":"provider-secret-wrap-aad.v4",'
            b'"attempt_id":"attempt-9f",'
            b'"data_nonce_hex":"000102030405060708090a0b",'
            b'"encryption_revision":3,'
            b'"fence_identity":"fence-3c",'
            b'"kek_id":"kek-2026a",'
            b'"provider_secret_version":7,'
            b'"secret_kind":"mineru-task-token.v1",'
            b'"token_byte_count":17,'
            b'"token_ciphertext_sha256":"' + ciphertext_sha + b'",'
            b'"token_sha256":"sha256:' + b"a" * 64 + b'"}'
        )
        observed = provider_secret_wrap_aad_v4(
            binding=_golden_binding(),
            encryption_revision=3,
            kek_id="kek-2026a",
            data_nonce=bytes.fromhex("000102030405060708090a0b"),
            token_ciphertext=token_ciphertext,
        )
        self.assertEqual(observed, expected)

    def test_wrap_aad_rejects_invalid_inputs(self) -> None:
        valid = {
            "binding": _golden_binding(),
            "encryption_revision": 1,
            "kek_id": "kek-2026a",
            "data_nonce": bytes(12),
            "token_ciphertext": bytes(33),
        }
        rejected = (
            {"binding": SimpleNamespace()},
            {"encryption_revision": 0},
            {"encryption_revision": True},
            {"kek_id": "KEK-2026A"},
            {"kek_id": "-starts-with-dash"},
            {"data_nonce": bytes(11)},
            {"data_nonce": bytearray(12)},
            {"token_ciphertext": bytes(32)},
        )
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    provider_secret_wrap_aad_v4(**{**valid, **overrides})

    def test_data_aad_requires_exact_binding(self) -> None:
        with self.assertRaises(ValueError):
            provider_secret_data_aad_v4(SimpleNamespace())  # type: ignore[arg-type]


class SealedEnvelopeShapeTests(unittest.TestCase):
    def _sealed(self) -> SealedProviderSecretV4:
        return SealedProviderSecretV4(
            binding=_golden_binding(),
            encryption_revision=1,
            kek_id="kek-primary",
            wrap_nonce=bytes(12),
            wrapped_dek=bytes(48),
            data_nonce=bytes(12),
            token_ciphertext=bytes(33),
        )

    def test_envelope_shape_rejections(self) -> None:
        rejected = (
            {"binding": SimpleNamespace()},
            {"encryption_revision": 0},
            {"encryption_revision": True},
            {"kek_id": "Bad Id"},
            {"wrap_nonce": bytes(11)},
            {"wrap_nonce": bytearray(12)},
            {"wrapped_dek": bytes(47)},
            {"wrapped_dek": bytes(49)},
            {"data_nonce": bytes(13)},
            {"token_ciphertext": bytes(32)},
            {"token_ciphertext": bytes(34)},
            {"contract_version": "sealed-provider-secret.v3"},
        )
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    replace(self._sealed(), **overrides)

    def test_plaintext_validation(self) -> None:
        self.assertEqual(_plaintext().token, _TOKEN)
        with self.assertRaises(ValueError):
            ProviderSecretPlaintextV4(binding=_binding(), token=_TOKEN + b"\x00")
        with self.assertRaises(ValueError):
            ProviderSecretPlaintextV4(
                binding=_binding(), token=_flip(_TOKEN, 0)
            )
        with self.assertRaises(ValueError):
            ProviderSecretPlaintextV4(
                binding=_binding(), token=bytearray(_TOKEN)  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            ProviderSecretPlaintextV4(
                binding=SimpleNamespace(),  # type: ignore[arg-type]
                token=_TOKEN,
            )

    def test_repr_excludes_secret_material(self) -> None:
        sealed = self._sealed()
        for text in (repr(sealed), str(sealed)):
            self.assertNotIn("kek-primary", text)
            self.assertNotIn(bytes(12).hex(), text)
            self.assertNotIn(repr(bytes(48)), text)
            self.assertNotIn(repr(bytes(33)), text)
        plaintext_text = repr(_plaintext())
        self.assertNotIn(repr(_TOKEN), plaintext_text)
        self.assertNotIn(_TOKEN.hex(), plaintext_text)


class AesGcmProviderSecretCipherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = _keyring()
        self.cipher = AesGcmProviderSecretCipher(keyring=self.keyring)

    def test_roundtrip_one_byte_token(self) -> None:
        token = b"\x7f"
        sealed = self.cipher.seal(_plaintext(token))
        self.assertEqual(len(sealed.token_ciphertext), 1 + 16)
        self.assertEqual(self.cipher.open(sealed).token, token)

    def test_roundtrip_max_token(self) -> None:
        token = bytes(
            index % 251 for index in range(PROVIDER_SECRET_MAX_TOKEN_BYTES)
        )
        sealed = self.cipher.seal(_plaintext(token))
        self.assertEqual(
            len(sealed.token_ciphertext), PROVIDER_SECRET_MAX_TOKEN_BYTES + 16
        )
        self.assertEqual(self.cipher.open(sealed).token, token)

    def test_seal_starts_at_revision_one_under_primary_kek(self) -> None:
        sealed = self.cipher.seal(_plaintext())
        self.assertEqual(sealed.encryption_revision, 1)
        self.assertEqual(sealed.kek_id, "kek-primary")
        self.assertEqual(sealed.binding, _binding())

    def test_every_binding_field_tamper_fails_closed(self) -> None:
        sealed = self.cipher.seal(_plaintext())
        tampers = {
            "attempt_id": "attempt-zz",
            "fence_identity": "fence-zz",
            "secret_kind": "mineru-task-token.v2",
            "provider_secret_version": 8,
            "token_sha256": "sha256:" + "c" * 64,
        }
        for field_name, bad_value in tampers.items():
            with self.subTest(field=field_name):
                mutated = replace(
                    sealed,
                    binding=replace(sealed.binding, **{field_name: bad_value}),
                )
                with self.assertRaises(ProviderSecretAuthenticationFailed):
                    self.cipher.open(mutated)
        with self.subTest(field="token_byte_count"):
            with self.assertRaises(ValueError):
                replace(
                    sealed,
                    binding=replace(
                        sealed.binding, token_byte_count=len(_TOKEN) + 1
                    ),
                )

    def test_crypto_field_tampers_fail_authentication(self) -> None:
        sealed = self.cipher.seal(_plaintext())
        tampers = {
            "data_nonce": replace(
                sealed, data_nonce=_flip(sealed.data_nonce, 0)
            ),
            "wrap_nonce": replace(
                sealed, wrap_nonce=_flip(sealed.wrap_nonce, 0)
            ),
            "ciphertext_body": replace(
                sealed, token_ciphertext=_flip(sealed.token_ciphertext, 0)
            ),
            "ciphertext_tag": replace(
                sealed,
                token_ciphertext=_flip(
                    sealed.token_ciphertext, len(sealed.token_ciphertext) - 1
                ),
            ),
            "wrapped_dek": replace(
                sealed, wrapped_dek=_flip(sealed.wrapped_dek, 0)
            ),
            "encryption_revision": replace(
                sealed, encryption_revision=sealed.encryption_revision + 1
            ),
            "kek_id_known_other_key": replace(sealed, kek_id="kek-legacy"),
        }
        for label, mutated in tampers.items():
            with self.subTest(tamper=label):
                with self.assertRaises(ProviderSecretAuthenticationFailed):
                    self.cipher.open(mutated)

    def test_unknown_kek_and_wrong_key_bytes(self) -> None:
        sealed = self.cipher.seal(_plaintext())
        with self.assertRaises(ProviderSecretKekUnknown):
            self.cipher.open(replace(sealed, kek_id="kek-ghost"))
        wrong_key_ring = StaticProviderSecretKeyring(
            primary_kek_id="kek-primary",
            keks={"kek-primary": _KEK_LEGACY},
        )
        with self.assertRaises(ProviderSecretAuthenticationFailed):
            AesGcmProviderSecretCipher(keyring=wrong_key_ring).open(sealed)

    def test_open_and_rewrap_require_exact_envelope_type(self) -> None:
        for value in (None, SimpleNamespace(), _plaintext()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProviderSecretEnvelopeInvalid):
                    self.cipher.open(value)  # type: ignore[arg-type]
                with self.assertRaises(ProviderSecretEnvelopeInvalid):
                    self.cipher.rewrap(value)  # type: ignore[arg-type]

    def test_seal_requires_exact_plaintext_type(self) -> None:
        with self.assertRaises(ProviderSecretSealInvalid):
            self.cipher.seal(SimpleNamespace())  # type: ignore[arg-type]

    def test_rewrap_preserves_data_layer_and_advances_revision(self) -> None:
        sealed = self.cipher.seal(_plaintext())
        rewrapped = self.cipher.rewrap(sealed)
        self.assertEqual(rewrapped.encryption_revision, 2)
        self.assertEqual(rewrapped.binding, sealed.binding)
        self.assertEqual(rewrapped.data_nonce, sealed.data_nonce)
        self.assertEqual(rewrapped.token_ciphertext, sealed.token_ciphertext)
        self.assertNotEqual(rewrapped.wrapped_dek, sealed.wrapped_dek)
        self.assertEqual(self.cipher.open(rewrapped).token, _TOKEN)
        self.assertEqual(self.cipher.rewrap(rewrapped).encryption_revision, 3)

    def test_rewrap_switches_to_primary_and_new_key_only_opens(self) -> None:
        old_ring = StaticProviderSecretKeyring(
            primary_kek_id="kek-old", keks={"kek-old": _KEK_PRIMARY}
        )
        sealed = AesGcmProviderSecretCipher(keyring=old_ring).seal(_plaintext())
        rotated_ring = StaticProviderSecretKeyring(
            primary_kek_id="kek-new",
            keks={"kek-new": _KEK_LEGACY, "kek-old": _KEK_PRIMARY},
        )
        rewrapped = AesGcmProviderSecretCipher(keyring=rotated_ring).rewrap(sealed)
        self.assertEqual(rewrapped.kek_id, "kek-new")
        new_only = AesGcmProviderSecretCipher(
            keyring=StaticProviderSecretKeyring(
                primary_kek_id="kek-new", keks={"kek-new": _KEK_LEGACY}
            )
        )
        self.assertEqual(new_only.open(rewrapped).token, _TOKEN)
        with self.assertRaises(ProviderSecretKekUnknown):
            new_only.open(sealed)

    def test_deterministic_rng_injection(self) -> None:
        script = [b"\x11" * 32, b"\x22" * 12, b"\x33" * 12]
        first = AesGcmProviderSecretCipher(
            keyring=self.keyring, rng=_ScriptedRng(script)
        ).seal(_plaintext())
        second = AesGcmProviderSecretCipher(
            keyring=self.keyring, rng=_ScriptedRng(script)
        ).seal(_plaintext())
        self.assertEqual(first, second)
        self.assertEqual(first.data_nonce, b"\x22" * 12)
        self.assertEqual(first.wrap_nonce, b"\x33" * 12)
        self.assertEqual(self.cipher.open(first).token, _TOKEN)

    def test_rng_length_failures_fail_closed(self) -> None:
        scripts = (
            [b"\x11" * 31],
            ["x" * 32],
            [b"\x11" * 32, b"\x22" * 11],
            [b"\x11" * 32, b"\x22" * 12, b"\x33" * 13],
        )
        for script in scripts:
            with self.subTest(chunks=len(script)):
                cipher = AesGcmProviderSecretCipher(
                    keyring=self.keyring, rng=_ScriptedRng(script)
                )
                with self.assertRaises(ProviderSecretSealInvalid):
                    cipher.seal(_plaintext())
        sealed = self.cipher.seal(_plaintext())
        rewrap_cipher = AesGcmProviderSecretCipher(
            keyring=self.keyring, rng=_ScriptedRng([b"\x99" * 11])
        )
        with self.assertRaises(ProviderSecretSealInvalid):
            rewrap_cipher.rewrap(sealed)

    def test_rng_exception_fails_closed_without_exception_chain(self) -> None:
        def failing_rng(_count: int) -> bytes:
            raise RuntimeError("sensitive entropy backend detail")

        cipher = AesGcmProviderSecretCipher(
            keyring=self.keyring,
            rng=failing_rng,
        )
        with self.assertRaises(ProviderSecretSealInvalid) as caught:
            cipher.seal(_plaintext())
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_keyring_port_outputs_fail_closed(self) -> None:
        class _InvalidKeyring:
            def __init__(self, *, kek_id: object, kek: object) -> None:
                self._kek_id = kek_id
                self._kek = kek

            def primary_kek_id(self) -> str:
                return self._kek_id  # type: ignore[return-value]

            def kek_bytes(self, _kek_id: str) -> bytes:
                return self._kek  # type: ignore[return-value]

        for keyring in (
            _InvalidKeyring(kek_id="Bad Key", kek=_KEK_PRIMARY),
            _InvalidKeyring(kek_id="kek-primary", kek=b"short"),
        ):
            with self.subTest(kek_id=keyring._kek_id):
                cipher = AesGcmProviderSecretCipher(keyring=keyring)
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    cipher.seal(_plaintext())

    def test_rewrap_rejects_revision_exhaustion(self) -> None:
        sealed = replace(
            self.cipher.seal(_plaintext()),
            encryption_revision=(1 << 63) - 1,
        )
        with self.assertRaises(ProviderSecretSealInvalid):
            self.cipher.rewrap(sealed)

    def test_errors_and_logs_exclude_secret_material(self) -> None:
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            sealed = self.cipher.seal(_plaintext())
            self.cipher.open(sealed)
            self.cipher.rewrap(sealed)
            with self.assertRaises(ProviderSecretAuthenticationFailed) as tampered:
                self.cipher.open(
                    replace(sealed, wrapped_dek=_flip(sealed.wrapped_dek, 0))
                )
            with self.assertRaises(ProviderSecretKekUnknown) as unknown:
                self.cipher.open(replace(sealed, kek_id="kek-ghost"))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertEqual(
            [
                record.name
                for record in captured
                if record.name.startswith("disclosure_anchor")
            ],
            [],
        )
        for message in (str(tampered.exception), str(unknown.exception)):
            self.assertNotIn("kek-primary", message)
            self.assertNotIn("kek-ghost", message)
            self.assertNotIn(sealed.data_nonce.hex(), message)
            self.assertNotIn(_TOKEN.hex(), message)
            self.assertNotIn("/", message)
        self.assertIsNone(tampered.exception.__cause__)
        self.assertTrue(tampered.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
