"""Injected keyring/cipher boundary for private V4 provider secrets.

Every failure is a typed member of ``ProviderSecretCryptoError`` so callers
stay fail-closed: absence, unknown key, malformed envelope, and authentication
failure are distinct, and none of them carries key material, nonces,
ciphertext, tokens, or paths in its message.
"""

from __future__ import annotations

from typing import Protocol

from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    ProviderSecretPlaintextV4,
    SealedProviderSecretV4,
)


class ProviderSecretCryptoError(RuntimeError):
    """Base for every V4 provider-secret keyring/cipher failure."""


class ProviderSecretKeyringUnavailable(ProviderSecretCryptoError):
    """V4 secret sealing was requested but no keyring is composed."""


class ProviderSecretKeyringInvalid(ProviderSecretCryptoError):
    """The keyring source failed closed-format or file-safety validation."""


class ProviderSecretKekUnknown(ProviderSecretCryptoError):
    """The referenced KEK id is not present in the composed keyring."""


class ProviderSecretSealInvalid(ProviderSecretCryptoError):
    """Seal/rewrap inputs or entropy failed exact validation."""


class ProviderSecretEnvelopeInvalid(ProviderSecretCryptoError):
    """The presented sealed envelope is not the exact V4 shape."""


class ProviderSecretAuthenticationFailed(ProviderSecretCryptoError):
    """AEAD authentication or post-open token verification failed."""


class ProviderSecretKeyringPort(Protocol):
    """Key custody seam; implementations never log or repr key material."""

    def primary_kek_id(self) -> str: ...

    def kek_bytes(self, kek_id: str) -> bytes: ...


class ProviderSecretCipherPort(Protocol):
    """Seal/open/rewrap for the envelope-DEK sealed provider secret."""

    def seal(
        self, plaintext: ProviderSecretPlaintextV4
    ) -> SealedProviderSecretV4: ...

    def open(
        self, sealed: SealedProviderSecretV4
    ) -> ProviderSecretPlaintextV4: ...

    def rewrap(
        self, sealed: SealedProviderSecretV4
    ) -> SealedProviderSecretV4: ...


__all__ = [
    "ProviderSecretAuthenticationFailed",
    "ProviderSecretCipherPort",
    "ProviderSecretCryptoError",
    "ProviderSecretEnvelopeInvalid",
    "ProviderSecretKekUnknown",
    "ProviderSecretKeyringInvalid",
    "ProviderSecretKeyringPort",
    "ProviderSecretKeyringUnavailable",
    "ProviderSecretSealInvalid",
]
