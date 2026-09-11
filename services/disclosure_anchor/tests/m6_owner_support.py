"""Independently authored fakes for the M6 owner control client and line transport.

A scripted owner is not a Windows owner: it composes whatever reply bytes the
test asks for, bound to the request it actually received. Nothing here
qualifies a host, a journal or a lease. Tokens are synthetic hex placeholders.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from disclosure_anchor.adapters.runtime.m6_owner_protocol import (
    M6LeasePolicy, M6OwnerClient, M6OwnerTransport,
)
from disclosure_anchor.application.contracts.m6_owner import (
    M6OwnerAnchor, M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import (
    M6EventPayload, M6OwnerStamp, M6ProducerEvent, M6ProducerKind, M6RunEvent,
)

from tests.m6_support import OWNER_EPOCH, RUNNER_EPOCH, digest


NS = 1_000_000_000
SYNTHETIC_TOKEN = "0123456789abcdef" * 4  # 64 lowercase hex characters, not a real credential


def anchor_for(spec: M6RunSpec, *, owner_epoch: str = OWNER_EPOCH, **overrides: object) -> M6OwnerAnchor:
    values: dict[str, object] = {
        "run_id": spec.run_id, "clock": spec.clock, "owner_process_epoch_sha256": owner_epoch,
        "owner_source_sha256": spec.runtime.owner_source_sha256,
        "gpu_device_identity_sha256": spec.runtime.gpu_device_identity_sha256,
        "t0_ticks": spec.t0_ticks, "planned_seconds": spec.planned_seconds,
        "deadline_ticks": spec.deadline_ticks, "max_close_ticks": spec.max_close_ticks,
        "resources": spec.resources,
    }
    values.update(overrides)
    return M6OwnerAnchor(**values)  # type: ignore[arg-type]


def policy(**overrides: int) -> M6LeasePolicy:
    values: dict[str, int] = {"stop_propagation_reserve_ns": 200_000_000}
    values.update(overrides)
    return M6LeasePolicy(**values)


class ManualClock:
    """Injected sleep-inclusive clock stand-in; the test moves it explicitly."""

    def __init__(self, value: int = NS) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.value

    def advance(self, delta_ns: int) -> None:
        self.value += delta_ns


@dataclass
class ScriptedOwner:
    """Transport double that records requests and answers from a FIFO of handlers."""

    spec: M6RunSpec
    anchor: M6OwnerAnchor
    clock: ManualClock
    owner_epoch: str = OWNER_EPOCH
    maximum_wire_bytes: int = 65536
    handlers: list[Callable[[M6OwnerRequest], bytes]] = field(default_factory=list)
    requests: list[M6OwnerRequest] = field(default_factory=list)
    raw_requests: list[bytes] = field(default_factory=list)
    close_calls: int = 0
    close_error: BaseException | None = None

    # -- M6OwnerTransport --
    def exchange(self, request: bytes) -> bytes:
        self.raw_requests.append(request)
        parsed = M6OwnerRequest.from_canonical_bytes(request, maximum_bytes=self.maximum_wire_bytes)
        self.requests.append(parsed)
        if not self.handlers:
            raise AssertionError("scripted owner received an unscripted request: " + parsed.command.kind)
        return self.handlers.pop(0)(parsed)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    # -- scripting --
    def status(
        self, *, observed: int, state: str = "open", last_sequence: int = 0, lease: int | None = None,
        epoch: str | None = None, **overrides: object,
    ) -> M6OwnerStatus:
        values: dict[str, object] = {
            "run_id": self.spec.run_id, "spec_sha256": self.spec.canonical_sha256(),
            "anchor_sha256": self.anchor.canonical_sha256(),
            "owner_process_epoch_sha256": epoch if epoch is not None else self.owner_epoch,
            "observed_qpc_ticks": observed, "state": state, "last_sequence": last_sequence,
            "admission_valid_until_ticks": lease,
        }
        values.update(overrides)
        return M6OwnerStatus(**values)  # type: ignore[arg-type]

    def reply(
        self, request: M6OwnerRequest, status: M6OwnerStatus, *, outcome: str = "ok",
        record: M6RunEvent | None = None, error_code: str | None = None,
    ) -> bytes:
        return M6OwnerReply(
            request_sha256=request.canonical_sha256(), outcome=outcome,  # type: ignore[arg-type]
            status=status, record=record, error_code=error_code,
        ).canonical_bytes()

    def answer(
        self, status: M6OwnerStatus, *, outcome: str = "ok", record: M6RunEvent | None = None,
        error_code: str | None = None, rtt_ns: int = 0,
        record_for: Callable[[M6OwnerRequest], M6RunEvent] | None = None,
    ) -> None:
        """Queue one reply. `rtt_ns` advances the injected clock during the exchange."""

        def handler(request: M6OwnerRequest) -> bytes:
            self.clock.advance(rtt_ns)
            stamped = record_for(request) if record_for is not None else record
            return self.reply(request, status, outcome=outcome, record=stamped, error_code=error_code)

        self.handlers.append(handler)

    def answer_raw(self, producer: Callable[[M6OwnerRequest], bytes]) -> None:
        self.handlers.append(producer)

    def raise_on_exchange(self, error: BaseException) -> None:
        def handler(request: M6OwnerRequest) -> bytes:
            raise error
        self.handlers.append(handler)

    def stamp(
        self, event: M6ProducerEvent, *, sequence: int, tick: int, owner_epoch: str | None = None,
        boot: str | None = None, producer_sha: str | None = None,
    ) -> M6RunEvent:
        return M6RunEvent(event=event, stamp=M6OwnerStamp(
            sequence=sequence, received_qpc_ticks=tick,
            boot_identity_sha256=boot if boot is not None else self.spec.clock.boot_identity_sha256,
            owner_process_epoch_sha256=owner_epoch if owner_epoch is not None else self.owner_epoch,
            producer_event_sha256=producer_sha if producer_sha is not None else event.canonical_sha256(),
        ))


def producer_event(
    spec: M6RunSpec, payload: M6EventPayload, *, kind: M6ProducerKind = "e2e_runner",
    epoch: str = RUNNER_EPOCH, sequence: int = 1,
) -> M6ProducerEvent:
    return M6ProducerEvent(
        run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), producer_kind=kind,
        producer_epoch_sha256=epoch, producer_sequence=sequence, payload=payload,
    )


def client_for(
    owner: ScriptedOwner, *, role: str = "e2e_runner", epoch: str = RUNNER_EPOCH,
    lease_policy: M6LeasePolicy | None = None, recovery_chain: tuple[M6RunEvent, ...] = (),
    maximum_wire_bytes: int = 65536, transport: M6OwnerTransport | None = None,
) -> M6OwnerClient:
    return M6OwnerClient(
        anchor=owner.anchor, spec=owner.spec, transport=transport or owner,
        caller_role=role, producer_epoch_sha256=epoch,  # type: ignore[arg-type]
        continuous_ns=owner.clock, lease_policy=lease_policy or policy(),
        maximum_wire_bytes=maximum_wire_bytes, recovery_chain=recovery_chain,
    )


# --- line transport fakes ---------------------------------------------------

class FakeChannel:
    """Scripted direct-tcpip channel double for M6LineOwnerTransport."""

    def __init__(self, parts: list[bytes] | None = None, *, on_recv: Callable[[], None] | None = None) -> None:
        self.parts = list(parts or [])
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.recv_sizes: list[int] = []
        self.close_calls = 0
        self.close_errors: list[BaseException] = []
        self.on_recv = on_recv

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, size: int) -> bytes:
        self.recv_sizes.append(size)
        if self.on_recv is not None:
            self.on_recv()
        if not self.parts:
            return b""
        part = self.parts.pop(0)
        if len(part) > size:
            self.parts.insert(0, part[size:])  # unread bytes stay in the stream
        return part[:size]

    def close(self) -> None:
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)


class FakeOpener:
    def __init__(self, channels: list[FakeChannel]) -> None:
        self.channels = list(channels)
        self.timeouts: list[float] = []
        self.session_closes = 0
        self.session_close_error: BaseException | None = None

    def __call__(self, timeout: float) -> FakeChannel:
        self.timeouts.append(timeout)
        if not self.channels:
            raise AssertionError("opener asked for more channels than scripted")
        return self.channels.pop(0)

    def close_session(self) -> None:
        self.session_closes += 1
        if self.session_close_error is not None:
            raise self.session_close_error


def synthetic_reply_line(label: str = "reply") -> bytes:
    """Opaque single-line body; the transport does not interpret it."""
    return b'{"synthetic":"' + digest(label).encode("ascii") + b'"}'
