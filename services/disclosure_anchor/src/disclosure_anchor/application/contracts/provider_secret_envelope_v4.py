"""Private V4 provider-secret binding, canonical AADs, and sealed envelope.

The sealed shape is envelope-DEK: each secret row carries one random 32-byte
DEK and a 12-byte data nonce for AES-256-GCM over the raw provider token; a
versioned 32-byte KEK wraps that DEK with its own 12-byte nonce and a wrap AAD.
``provider_secret_version`` is the immutable accepted-capability version taken
from ``AcceptedSubmissionReceiptV4.secret_version``; ``encryption_revision`` is
a separate append-only counter starting at 1 whose maximum is the current wrap.
Rewrap changes only the KEK id, wrap nonce, wrapped DEK, and revision; token
ciphertext, data nonce, and the data AAD stay byte-identical.

The data AAD binds only immutable evidence facts plus this schema layer. The
wrap AAD additionally binds the revision, the KEK id, and the sealed payload
identity (data nonce and token-ciphertext digest) so a wrapped DEK cannot be
grafted onto another row's ciphertext.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
)

PROVIDER_SECRET_BINDING_V4_CONTRACT = "provider-secret-binding.v4"
SEALED_PROVIDER_SECRET_V4_CONTRACT = "sealed-provider-secret.v4"
PROVIDER_SECRET_DATA_AAD_V4_CONTRACT = "provider-secret-data-aad.v4"
PROVIDER_SECRET_WRAP_AAD_V4_CONTRACT = "provider-secret-wrap-aad.v4"

PROVIDER_SECRET_DEK_BYTES = 32
PROVIDER_SECRET_KEK_BYTES = 32
PROVIDER_SECRET_NONCE_BYTES = 12
PROVIDER_SECRET_TAG_BYTES = 16
PROVIDER_SECRET_WRAPPED_DEK_BYTES = (
    PROVIDER_SECRET_DEK_BYTES + PROVIDER_SECRET_TAG_BYTES
)
PROVIDER_SECRET_MAX_TOKEN_BYTES = 65536

_MAX_AAD_BYTES = 1024 * 1024
_MAX_INT = (1 << 63) - 1
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_KEK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class ProviderSecretBindingV4:
    """Immutable evidence identity every sealed token is bound to."""

    attempt_id: str
    fence_identity: str
    secret_kind: str
    provider_secret_version: int
    token_sha256: str
    token_byte_count: int
    contract_version: str = PROVIDER_SECRET_BINDING_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, PROVIDER_SECRET_BINDING_V4_CONTRACT)
        _identity(self.attempt_id, max_bytes=1024)
        _identity(self.fence_identity, max_bytes=1024)
        _identity(self.secret_kind, max_bytes=128)
        _positive(self.provider_secret_version, "provider secret version")
        _sha(self.token_sha256, "provider token")
        _positive(self.token_byte_count, "provider token byte count")
        if self.token_byte_count > PROVIDER_SECRET_MAX_TOKEN_BYTES:
            raise ValueError("provider token byte count is outside the envelope")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


def bind_provider_secret_v4(
    accepted: AcceptedSubmissionReceiptV4,
) -> ProviderSecretBindingV4:
    """Map the accepted-submission receipt onto the immutable secret binding."""

    if type(accepted) is not AcceptedSubmissionReceiptV4:
        raise ValueError("provider secret binding requires the exact V4 receipt")
    return ProviderSecretBindingV4(
        attempt_id=accepted.attempt_id,
        fence_identity=accepted.fence_identity,
        secret_kind=accepted.secret_kind,
        provider_secret_version=accepted.secret_version,
        token_sha256=accepted.token_sha256,
        token_byte_count=accepted.token_byte_count,
    )


@dataclass(frozen=True, slots=True)
class ProviderSecretPlaintextV4:
    """Raw provider token whose bytes must match the immutable binding."""

    binding: ProviderSecretBindingV4
    token: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _binding(self.binding)
        if type(self.token) is not bytes:
            raise ValueError("provider token must be exact bytes")
        if len(self.token) != self.binding.token_byte_count:
            raise ValueError("provider token length differs from its binding")
        if _digest(self.token) != self.binding.token_sha256:
            raise ValueError("provider token hash differs from its binding")


@dataclass(frozen=True, slots=True)
class SealedProviderSecretV4:
    """Envelope-DEK row shape; only wrap fields may change across revisions."""

    binding: ProviderSecretBindingV4
    encryption_revision: int
    kek_id: str = field(repr=False)
    wrap_nonce: bytes = field(repr=False)
    wrapped_dek: bytes = field(repr=False)
    data_nonce: bytes = field(repr=False)
    token_ciphertext: bytes = field(repr=False)
    contract_version: str = SEALED_PROVIDER_SECRET_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, SEALED_PROVIDER_SECRET_V4_CONTRACT)
        _binding(self.binding)
        _positive(self.encryption_revision, "encryption revision")
        _kek_identity(self.kek_id)
        _exact_bytes(self.wrap_nonce, PROVIDER_SECRET_NONCE_BYTES, "wrap nonce")
        _exact_bytes(
            self.wrapped_dek,
            PROVIDER_SECRET_WRAPPED_DEK_BYTES,
            "wrapped DEK",
        )
        _exact_bytes(self.data_nonce, PROVIDER_SECRET_NONCE_BYTES, "data nonce")
        _exact_bytes(
            self.token_ciphertext,
            self.binding.token_byte_count + PROVIDER_SECRET_TAG_BYTES,
            "token ciphertext",
        )


def provider_secret_data_aad_v4(binding: ProviderSecretBindingV4) -> bytes:
    """Canonical data-layer AAD: immutable evidence facts plus schema layer."""

    _binding(binding)
    return _canonical(
        {
            "aad_contract": PROVIDER_SECRET_DATA_AAD_V4_CONTRACT,
            "attempt_id": binding.attempt_id,
            "fence_identity": binding.fence_identity,
            "provider_secret_version": binding.provider_secret_version,
            "secret_kind": binding.secret_kind,
            "token_byte_count": binding.token_byte_count,
            "token_sha256": binding.token_sha256,
        }
    )


def provider_secret_wrap_aad_v4(
    *,
    binding: ProviderSecretBindingV4,
    encryption_revision: int,
    kek_id: str,
    data_nonce: bytes,
    token_ciphertext: bytes,
) -> bytes:
    """Canonical wrap-layer AAD binding revision, KEK, and sealed payload."""

    _binding(binding)
    _positive(encryption_revision, "encryption revision")
    _kek_identity(kek_id)
    _exact_bytes(data_nonce, PROVIDER_SECRET_NONCE_BYTES, "data nonce")
    _exact_bytes(
        token_ciphertext,
        binding.token_byte_count + PROVIDER_SECRET_TAG_BYTES,
        "token ciphertext",
    )
    return _canonical(
        {
            "aad_contract": PROVIDER_SECRET_WRAP_AAD_V4_CONTRACT,
            "attempt_id": binding.attempt_id,
            "data_nonce_hex": data_nonce.hex(),
            "encryption_revision": encryption_revision,
            "fence_identity": binding.fence_identity,
            "kek_id": kek_id,
            "provider_secret_version": binding.provider_secret_version,
            "secret_kind": binding.secret_kind,
            "token_byte_count": binding.token_byte_count,
            "token_ciphertext_sha256": _digest(token_ciphertext),
            "token_sha256": binding.token_sha256,
        }
    )


def validate_provider_secret_kek_id_v4(value: str) -> None:
    """One shared KEK-id grammar for envelopes and keyring sources."""

    _kek_identity(value)


def _binding(value: ProviderSecretBindingV4) -> None:
    if type(value) is not ProviderSecretBindingV4:
        raise ValueError("provider secret binding is not exact")


def _canonical(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= _MAX_AAD_BYTES:
        raise ValueError("provider secret canonical bytes are outside envelope")
    return encoded


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _contract(observed: str, expected: str) -> None:
    if observed != expected:
        raise ValueError("provider secret contract is unsupported")


def _identity(value: str, *, max_bytes: int) -> None:
    if not isinstance(value, str):
        raise ValueError("provider secret identity is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("provider secret identity is invalid") from None
    if (
        not value
        or value != value.strip()
        or len(encoded) > max_bytes
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("provider secret identity is invalid")


def _kek_identity(value: str) -> None:
    if not isinstance(value, str) or _KEK_ID.fullmatch(value) is None:
        raise ValueError("provider secret KEK identity is invalid")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} hash is not canonical")


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be positive")


def _exact_bytes(value: bytes, length: int, label: str) -> None:
    if type(value) is not bytes or len(value) != length:
        raise ValueError(f"{label} bytes are not exact")


__all__ = [
    "PROVIDER_SECRET_BINDING_V4_CONTRACT",
    "PROVIDER_SECRET_DATA_AAD_V4_CONTRACT",
    "PROVIDER_SECRET_DEK_BYTES",
    "PROVIDER_SECRET_KEK_BYTES",
    "PROVIDER_SECRET_MAX_TOKEN_BYTES",
    "PROVIDER_SECRET_NONCE_BYTES",
    "PROVIDER_SECRET_TAG_BYTES",
    "PROVIDER_SECRET_WRAP_AAD_V4_CONTRACT",
    "PROVIDER_SECRET_WRAPPED_DEK_BYTES",
    "SEALED_PROVIDER_SECRET_V4_CONTRACT",
    "ProviderSecretBindingV4",
    "ProviderSecretPlaintextV4",
    "SealedProviderSecretV4",
    "bind_provider_secret_v4",
    "provider_secret_data_aad_v4",
    "provider_secret_wrap_aad_v4",
    "validate_provider_secret_kek_id_v4",
]
