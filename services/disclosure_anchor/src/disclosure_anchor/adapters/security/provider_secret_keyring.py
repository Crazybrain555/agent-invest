"""KEK custody for private V4 provider secrets.

The file loader pins one fd via ``os.open`` with ``O_NOFOLLOW`` (plus
``O_CLOEXEC``/``O_NONBLOCK`` where available) and validates that exact fd with
``fstat``. ``O_NOFOLLOW`` only rejects a symlink at the final path component;
this loader makes no hostile-ancestor-directory claim. Errors never carry key
material, KEK ids, or filesystem paths, and nothing here logs.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    PROVIDER_SECRET_KEK_BYTES,
    validate_provider_secret_kek_id_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretKekUnknown,
    ProviderSecretKeyringInvalid,
    ProviderSecretKeyringUnavailable,
)
from disclosure_anchor.settings import Settings

PROVIDER_SECRET_KEYRING_FORMAT = "disclosure-v4-secret-keyring.v1"

_MAX_KEYRING_FILE_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 65536
_KEK_HEX = re.compile(r"[0-9a-f]{64}\Z")


class StaticProviderSecretKeyring:
    """Injected in-memory KEK mapping: one primary plus decrypt-only KEKs."""

    def __init__(self, *, primary_kek_id: str, keks: Mapping[str, bytes]) -> None:
        if not isinstance(keks, Mapping) or not keks:
            raise ProviderSecretKeyringInvalid(
                "provider secret keyring requires at least one KEK"
            )
        validated: dict[str, bytes] = {}
        for kek_id, key in keks.items():
            try:
                validate_provider_secret_kek_id_v4(kek_id)
            except ValueError:
                raise ProviderSecretKeyringInvalid(
                    "provider secret KEK identity is invalid"
                ) from None
            if type(key) is not bytes or len(key) != PROVIDER_SECRET_KEK_BYTES:
                raise ProviderSecretKeyringInvalid(
                    "provider secret KEK is not exactly 32 bytes"
                )
            validated[kek_id] = key
        if primary_kek_id not in validated:
            raise ProviderSecretKeyringInvalid(
                "provider secret primary KEK is absent from the keyring"
            )
        self._primary_kek_id = primary_kek_id
        self._keks = validated

    def primary_kek_id(self) -> str:
        return self._primary_kek_id

    def kek_bytes(self, kek_id: str) -> bytes:
        key = self._keks.get(kek_id)
        if key is None:
            raise ProviderSecretKekUnknown("provider secret KEK is unknown")
        return key


def load_provider_secret_keyring_file(path: Path) -> StaticProviderSecretKeyring:
    """Load the closed-JSON keyring file once through one validated fd."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise ProviderSecretKeyringInvalid(
            "provider secret keyring path must be absolute"
        )
    flags = os.O_RDONLY
    for flag_name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, "unknown")
        raise ProviderSecretKeyringInvalid(
            f"provider secret keyring file open failed ({code})"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProviderSecretKeyringInvalid(
                "provider secret keyring is not a regular file"
            )
        if info.st_uid != os.geteuid():
            raise ProviderSecretKeyringInvalid(
                "provider secret keyring is not owned by the current user"
            )
        if info.st_mode & 0o7177:
            raise ProviderSecretKeyringInvalid(
                "provider secret keyring permissions are broader than 0600"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_KEYRING_FILE_BYTES:
                raise ProviderSecretKeyringInvalid(
                    "provider secret keyring file exceeds its size bound"
                )
            chunks.append(chunk)
    except ProviderSecretKeyringInvalid:
        raise
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, "unknown")
        raise ProviderSecretKeyringInvalid(
            f"provider secret keyring file read failed ({code})"
        ) from None
    finally:
        try:
            os.close(fd)
        except OSError:
            # A read-only fd has no buffered writes to lose.  A close failure
            # must not replace the typed validation result or expose details.
            pass
    return _decode_keyring_payload(b"".join(chunks))


def load_provider_secret_keyring_from_settings(
    settings: Settings,
) -> StaticProviderSecretKeyring:
    """Fail closed when V4 sealing is requested without a configured keyring."""

    path = settings.disclosure_v4_secret_keyring_file
    if path is None:
        raise ProviderSecretKeyringUnavailable(
            "DISCLOSURE_V4_SECRET_KEYRING_FILE is not configured"
        )
    return load_provider_secret_keyring_file(path)


def _decode_keyring_payload(raw: bytes) -> StaticProviderSecretKeyring:
    try:
        payload = strict_json_loads(raw)
    except ValueError:
        raise ProviderSecretKeyringInvalid(
            "provider secret keyring file is not strict JSON"
        ) from None
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "primary_kek_id",
        "keks",
    }:
        raise ProviderSecretKeyringInvalid(
            "provider secret keyring fields are not closed"
        )
    if payload["format"] != PROVIDER_SECRET_KEYRING_FORMAT:
        raise ProviderSecretKeyringInvalid(
            "provider secret keyring format is unsupported"
        )
    primary_kek_id = payload["primary_kek_id"]
    if not isinstance(primary_kek_id, str):
        raise ProviderSecretKeyringInvalid(
            "provider secret primary KEK id must be a string"
        )
    raw_keks = payload["keks"]
    if not isinstance(raw_keks, dict) or not raw_keks:
        raise ProviderSecretKeyringInvalid(
            "provider secret keyring requires at least one KEK"
        )
    keks: dict[str, bytes] = {}
    for kek_id, value in raw_keks.items():
        if not isinstance(value, str) or _KEK_HEX.fullmatch(value) is None:
            raise ProviderSecretKeyringInvalid(
                "provider secret KEK must be exactly 64 lowercase hex characters"
            )
        keks[kek_id] = bytes.fromhex(value)
    return StaticProviderSecretKeyring(
        primary_kek_id=primary_kek_id,
        keks=keks,
    )


__all__ = [
    "PROVIDER_SECRET_KEYRING_FORMAT",
    "StaticProviderSecretKeyring",
    "load_provider_secret_keyring_file",
    "load_provider_secret_keyring_from_settings",
]
