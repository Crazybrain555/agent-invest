"""Operational stop control shared by every worker execution plane.

A public fault (a failed-closed semantic adjudication, a coordinator circuit,
an unreconciled ownership or deadline loss, a fatal maintenance or startup
recovery error) must stop the worker as a whole and stay stopped until an
operator explicitly releases it. This module is the application boundary:
a closed, content-free first-cause value and a small latch protocol. How the
cause is made durable is an adapter concern.

The cause never carries exception free text, prompts, model output, Unit text,
environment values or credentials. Unknown values are ``None`` rather than
guessed; an unknown exception is identified by its class and a one-way
fingerprint only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Protocol


PUBLIC_STOP_KINDS = frozenset(
    {
        # A typed semantic adjudication failure that is neither a closed
        # availability reason nor a cancellation.
        "semantic_failed_closed",
        # Any other unexpected failure of one bounded backend stage call.
        "stage_fault",
        # A bounded stage or claim deadline actually expired.
        "deadline_exhausted",
        # Claim renewal/reconciliation or process ownership was lost.
        "ownership_lost",
        # A coordinator-detected circuit (contract violation, retry budget
        # exhaustion, unavailable credit grant, stream pressure, admission).
        "coordinator_circuit",
        # An exception escaped the coordinator's synchronous controller.
        "coordinator_fault",
        # An exception escaped the resident maintenance plane.
        "maintenance_fatal",
        # Startup recovery or staged startup verification failed fatally.
        "startup_fatal",
    }
)
PUBLIC_STOP_ORIGINS = frozenset(
    {"stage_call", "coordinator", "resident", "maintenance", "startup_recovery"}
)
MAX_STOP_PROVIDER_ATTEMPTS = 16

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,159}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT_KEYS = (
    "adapter_kind",
    "adapter_version",
    "cache_key",
    "canonical_model",
    "inference_profile",
    "ordinal",
    "outcome",
    "provider",
    "provider_id",
    "reason_code",
)
_CAUSE_KEYS = (
    "attempt_id",
    "claim_generation",
    "claim_owner_identity",
    "exception_class",
    "exception_fingerprint",
    "kind",
    "lane",
    "lifecycle_version_at_dispatch",
    "origin",
    "provider_attempts",
    "reason_code",
    "state_at_dispatch",
)


def stop_token(value: object) -> str | None:
    """Return a closed lower-case token, or ``None`` when it is not one."""

    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else None


def stop_identifier(value: object) -> str | None:
    """Return a bounded opaque identifier, or ``None`` when it is not one."""

    return value if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) else None


def stop_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def stop_sha256(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def exception_class_name(error: BaseException) -> str:
    kind = type(error)
    name = f"{kind.__module__}.{kind.__qualname__}"
    return name if _CLASS_RE.fullmatch(name) else "builtins.BaseException"


def exception_fingerprint(error: BaseException) -> str:
    """Hash class and message so logs can be matched without storing text."""

    try:
        message = str(error)
    except Exception:  # noqa: BLE001 - a broken __str__ still gets a fingerprint
        message = "<unprintable>"
    raw = f"{exception_class_name(error)}\n{message}".encode("utf-8", "replace")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_optional(value: object, check: Callable[[object], object], label: str) -> None:
    if value is not None and check(value) is None:
        raise ValueError(f"public stop {label} is invalid")


@dataclass(frozen=True, slots=True)
class StopProviderAttempt:
    """Whitelisted identity/outcome of one provider attempt; never payloads."""

    ordinal: int
    outcome: str
    provider_id: str | None = None
    provider: str | None = None
    adapter_kind: str | None = None
    adapter_version: str | None = None
    canonical_model: str | None = None
    inference_profile: str | None = None
    reason_code: str | None = None
    cache_key: str | None = None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("public stop provider attempt ordinal is invalid")
        if stop_token(self.outcome) is None:
            raise ValueError("public stop provider attempt outcome is invalid")
        for value, label in (
            (self.provider_id, "provider id"),
            (self.provider, "provider"),
            (self.adapter_kind, "adapter kind"),
            (self.adapter_version, "adapter version"),
            (self.canonical_model, "canonical model"),
            (self.inference_profile, "inference profile"),
        ):
            _require_optional(value, stop_identifier, f"provider attempt {label}")
        _require_optional(self.reason_code, stop_token, "provider reason code")
        if self.cache_key is not None and (
            not isinstance(self.cache_key, str) or not _SHA256_RE.fullmatch(self.cache_key)
        ):
            raise ValueError("public stop provider cache key is invalid")

    def to_payload(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in _ATTEMPT_KEYS}

    @classmethod
    def from_payload(cls, payload: object) -> StopProviderAttempt:
        if not isinstance(payload, Mapping) or tuple(sorted(payload)) != _ATTEMPT_KEYS:
            raise ValueError("public stop provider attempt shape is not closed")
        return cls(**{key: payload[key] for key in _ATTEMPT_KEYS})


@dataclass(frozen=True, slots=True)
class PublicStopCause:
    """The immutable first cause of one worker-wide public stop."""

    kind: str
    reason_code: str
    origin: str
    exception_class: str | None = None
    exception_fingerprint: str | None = None
    attempt_id: str | None = None
    lane: str | None = None
    state_at_dispatch: str | None = None
    lifecycle_version_at_dispatch: int | None = None
    claim_generation: int | None = None
    claim_owner_identity: str | None = None
    provider_attempts: tuple[StopProviderAttempt, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in PUBLIC_STOP_KINDS:
            raise ValueError("public stop kind is outside the closed vocabulary")
        if stop_token(self.reason_code) is None:
            raise ValueError("public stop reason code is invalid")
        if self.origin not in PUBLIC_STOP_ORIGINS:
            raise ValueError("public stop origin is outside the closed vocabulary")
        if self.exception_class is not None and (
            not isinstance(self.exception_class, str)
            or not _CLASS_RE.fullmatch(self.exception_class)
        ):
            raise ValueError("public stop exception class is invalid")
        if self.exception_fingerprint is not None and (
            not isinstance(self.exception_fingerprint, str)
            or not _SHA256_RE.fullmatch(self.exception_fingerprint)
        ):
            raise ValueError("public stop exception fingerprint is invalid")
        _require_optional(self.attempt_id, stop_identifier, "attempt id")
        _require_optional(self.lane, stop_token, "lane")
        _require_optional(self.state_at_dispatch, stop_token, "dispatch state")
        _require_optional(self.lifecycle_version_at_dispatch, stop_count, "lifecycle version")
        _require_optional(self.claim_generation, stop_count, "claim generation")
        _require_optional(self.claim_owner_identity, stop_identifier, "claim owner")
        if (
            type(self.provider_attempts) is not tuple
            or len(self.provider_attempts) > MAX_STOP_PROVIDER_ATTEMPTS
            or any(type(item) is not StopProviderAttempt for item in self.provider_attempts)
        ):
            raise ValueError("public stop provider attempts are not closed")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            key: getattr(self, key) for key in _CAUSE_KEYS if key != "provider_attempts"
        }
        payload["provider_attempts"] = [item.to_payload() for item in self.provider_attempts]
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> PublicStopCause:
        if not isinstance(payload, Mapping) or tuple(sorted(payload)) != _CAUSE_KEYS:
            raise ValueError("public stop cause shape is not closed")
        attempts = payload["provider_attempts"]
        if not isinstance(attempts, list):
            raise ValueError("public stop provider attempts must be a list")
        values = {key: payload[key] for key in _CAUSE_KEYS if key != "provider_attempts"}
        return cls(
            **values,
            provider_attempts=tuple(StopProviderAttempt.from_payload(item) for item in attempts),
        )

    def summary(self) -> str:
        """One content-free operator line."""

        parts = [f"kind={self.kind}", f"reason={self.reason_code}", f"origin={self.origin}"]
        if self.attempt_id is not None:
            parts.append(f"attempt={self.attempt_id}")
        if self.lane is not None:
            parts.append(f"lane={self.lane}")
        if self.exception_class is not None:
            parts.append(f"class={self.exception_class}")
        return " ".join(parts)


class WorkerStopControlPort(Protocol):
    """One process-wide first-cause latch shared by every execution plane.

    ``trip`` records the first cause, halts every plane and returns ``True``
    only for that first call; later causes never overwrite it and never wait
    behind durable persistence before the halt is visible. Implementations
    must not raise after accepting a valid cause.
    """

    def trip(self, cause: PublicStopCause) -> bool: ...

    def is_tripped(self) -> bool: ...

    def first_cause(self) -> PublicStopCause | None: ...


class WakeableWorkerStopControl(WorkerStopControlPort, Protocol):
    """A control that can also wake idle planes when it trips."""

    def add_wake_callback(self, callback: Callable[[], None]) -> None: ...


class WorkerOperationalStopError(RuntimeError):
    """A persisted or in-process stop forbids composing worker business work.

    ``state`` is the closed operational state that refused the start and
    ``active_sha256`` the raw-byte digest of the active stop record when one
    exists. The message names neither absolute paths nor record contents.
    """

    def __init__(self, *, state: str, detail: str, active_sha256: str | None = None) -> None:
        self.state = state
        self.detail = detail
        self.active_sha256 = active_sha256
        suffix = "" if active_sha256 is None else f" ({active_sha256})"
        super().__init__(f"worker operational control is {state}{suffix}: {detail}")


__all__ = [
    "MAX_STOP_PROVIDER_ATTEMPTS",
    "PUBLIC_STOP_KINDS",
    "PUBLIC_STOP_ORIGINS",
    "PublicStopCause",
    "StopProviderAttempt",
    "WakeableWorkerStopControl",
    "WorkerOperationalStopError",
    "WorkerStopControlPort",
    "exception_class_name",
    "exception_fingerprint",
    "stop_count",
    "stop_identifier",
    "stop_sha256",
    "stop_token",
]
