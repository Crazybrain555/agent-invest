"""AES-256-GCM envelope-DEK cipher for private V4 provider secrets.

Python ``bytes`` are immutable, so intermediate DEK/token copies cannot be
honestly zeroized; this adapter therefore makes no wipe claim and instead
never logs, never reprs, and never embeds key material, ids, nonces,
ciphertext, or tokens in errors. ``InvalidTag`` maps to one typed
authentication failure with the exception chain suppressed.
"""

from __future__ import annotations

import hashlib
import os
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    PROVIDER_SECRET_DEK_BYTES,
    PROVIDER_SECRET_KEK_BYTES,
    PROVIDER_SECRET_NONCE_BYTES,
    ProviderSecretBindingV4,
    ProviderSecretPlaintextV4,
    SealedProviderSecretV4,
    provider_secret_data_aad_v4,
    provider_secret_wrap_aad_v4,
    validate_provider_secret_kek_id_v4,
)
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretAuthenticationFailed,
    ProviderSecretCryptoError,
    ProviderSecretEnvelopeInvalid,
    ProviderSecretKeyringInvalid,
    ProviderSecretKeyringPort,
    ProviderSecretSealInvalid,
)

_INITIAL_ENCRYPTION_REVISION = 1
_MAX_ENCRYPTION_REVISION = (1 << 63) - 1


class AesGcmProviderSecretCipher:
    """Seal/open/rewrap against an injected keyring and entropy source."""

    def __init__(
        self,
        *,
        keyring: ProviderSecretKeyringPort,
        rng: Callable[[int], bytes] | None = None,
    ) -> None:
        self._keyring = keyring
        self._rng = os.urandom if rng is None else rng

    def seal(self, plaintext: ProviderSecretPlaintextV4) -> SealedProviderSecretV4:
        if type(plaintext) is not ProviderSecretPlaintextV4:
            raise ProviderSecretSealInvalid(
                "provider secret seal requires the exact plaintext contract"
            )
        binding = plaintext.binding
        dek = self._random(PROVIDER_SECRET_DEK_BYTES)
        data_nonce = self._random(PROVIDER_SECRET_NONCE_BYTES)
        token_ciphertext = AESGCM(dek).encrypt(
            data_nonce,
            plaintext.token,
            provider_secret_data_aad_v4(binding),
        )
        return self._wrap(
            binding=binding,
            encryption_revision=_INITIAL_ENCRYPTION_REVISION,
            dek=dek,
            data_nonce=data_nonce,
            token_ciphertext=token_ciphertext,
        )

    def open(self, sealed: SealedProviderSecretV4) -> ProviderSecretPlaintextV4:
        dek = self._unwrap_dek(sealed)
        binding = sealed.binding
        try:
            token = AESGCM(dek).decrypt(
                sealed.data_nonce,
                sealed.token_ciphertext,
                provider_secret_data_aad_v4(binding),
            )
        except InvalidTag:
            raise ProviderSecretAuthenticationFailed(
                "provider secret token layer failed authentication"
            ) from None
        if (
            len(token) != binding.token_byte_count
            or "sha256:" + hashlib.sha256(token).hexdigest() != binding.token_sha256
        ):
            raise ProviderSecretAuthenticationFailed(
                "provider secret token differs from its binding after open"
            )
        return ProviderSecretPlaintextV4(binding=binding, token=token)

    def rewrap(self, sealed: SealedProviderSecretV4) -> SealedProviderSecretV4:
        if type(sealed) is not SealedProviderSecretV4:
            raise ProviderSecretEnvelopeInvalid(
                "provider secret envelope is not the exact V4 contract"
            )
        if sealed.encryption_revision >= _MAX_ENCRYPTION_REVISION:
            raise ProviderSecretSealInvalid(
                "provider secret encryption revision is exhausted"
            )
        dek = self._unwrap_dek(sealed)
        return self._wrap(
            binding=sealed.binding,
            encryption_revision=sealed.encryption_revision + 1,
            dek=dek,
            data_nonce=sealed.data_nonce,
            token_ciphertext=sealed.token_ciphertext,
        )

    def _wrap(
        self,
        *,
        binding: ProviderSecretBindingV4,
        encryption_revision: int,
        dek: bytes,
        data_nonce: bytes,
        token_ciphertext: bytes,
    ) -> SealedProviderSecretV4:
        kek_id = self._primary_kek_id()
        kek = self._kek_bytes(kek_id)
        wrap_nonce = self._random(PROVIDER_SECRET_NONCE_BYTES)
        wrapped_dek = AESGCM(kek).encrypt(
            wrap_nonce,
            dek,
            provider_secret_wrap_aad_v4(
                binding=binding,
                encryption_revision=encryption_revision,
                kek_id=kek_id,
                data_nonce=data_nonce,
                token_ciphertext=token_ciphertext,
            ),
        )
        return SealedProviderSecretV4(
            binding=binding,
            encryption_revision=encryption_revision,
            kek_id=kek_id,
            wrap_nonce=wrap_nonce,
            wrapped_dek=wrapped_dek,
            data_nonce=data_nonce,
            token_ciphertext=token_ciphertext,
        )

    def _unwrap_dek(self, sealed: SealedProviderSecretV4) -> bytes:
        if type(sealed) is not SealedProviderSecretV4:
            raise ProviderSecretEnvelopeInvalid(
                "provider secret envelope is not the exact V4 contract"
            )
        kek = self._kek_bytes(sealed.kek_id)
        try:
            return AESGCM(kek).decrypt(
                sealed.wrap_nonce,
                sealed.wrapped_dek,
                provider_secret_wrap_aad_v4(
                    binding=sealed.binding,
                    encryption_revision=sealed.encryption_revision,
                    kek_id=sealed.kek_id,
                    data_nonce=sealed.data_nonce,
                    token_ciphertext=sealed.token_ciphertext,
                ),
            )
        except InvalidTag:
            raise ProviderSecretAuthenticationFailed(
                "provider secret key wrap failed authentication"
            ) from None

    def _random(self, count: int) -> bytes:
        try:
            value = self._rng(count)
        except Exception:
            raise ProviderSecretSealInvalid(
                "provider secret entropy source failed"
            ) from None
        if type(value) is not bytes or len(value) != count:
            raise ProviderSecretSealInvalid(
                "provider secret entropy source returned invalid bytes"
            )
        return value

    def _primary_kek_id(self) -> str:
        try:
            kek_id = self._keyring.primary_kek_id()
            validate_provider_secret_kek_id_v4(kek_id)
        except ProviderSecretCryptoError:
            raise
        except Exception:
            raise ProviderSecretKeyringInvalid(
                "provider secret primary KEK identity is invalid"
            ) from None
        return kek_id

    def _kek_bytes(self, kek_id: str) -> bytes:
        try:
            kek = self._keyring.kek_bytes(kek_id)
        except ProviderSecretCryptoError:
            raise
        except Exception:
            raise ProviderSecretKeyringInvalid(
                "provider secret keyring lookup failed"
            ) from None
        if type(kek) is not bytes or len(kek) != PROVIDER_SECRET_KEK_BYTES:
            raise ProviderSecretKeyringInvalid(
                "provider secret KEK is not exactly 32 bytes"
            )
        return kek


__all__ = ["AesGcmProviderSecretCipher"]
