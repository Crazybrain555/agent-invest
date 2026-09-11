"""Bounded, authenticated M6 control client and conservative admission guard.

No business queue, retries, clocks selected globally, or cleanup assertions.
The producer owns its immutable pending envelope and may retry those exact
bytes after an ambiguous response. Transport loss latches admission closed;
drain/reconciliation remains possible with this client.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import threading
from typing import Any, Literal, Protocol
from uuid import uuid4

from disclosure_anchor.application.contracts.m6_owner import (
    M6AdmissionClosedAck, M6AppendObservation, M6BindOwner, M6OwnerAnchor, M6OwnerCommand, M6OwnerControl,
    M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import M6ProducerEvent, M6RunEvent


M6CallerRole = Literal["controller", "e2e_runner", "service_runner", "public_verifier", "quality_verifier"]


class M6OwnerTransport(Protocol):
    def exchange(self, request: bytes) -> bytes: ...

    def close(self) -> None: ...


class M6OwnerProtocolError(ValueError):
    """Untrusted response or invalid control identity; never infer success."""


class M6OwnerRejected(RuntimeError):
    def __init__(self, reply: M6OwnerReply) -> None:
        super().__init__("M6 owner " + reply.outcome + ": " + str(reply.error_code))
        self.reply = reply  # A conflict retains the actual durable stamp.


@dataclass(frozen=True, slots=True)
class M6LeasePolicy:
    # Derived by the selected runner from its worst bounded in-flight claim,
    # observation append, actual admission-closed ACK and control polling. It
    # is required explicitly; a margin guessed by the client is not this bound.
    stop_propagation_reserve_ns: int
    maximum_lease_ns: int = 1_000_000_000
    uncertainty_margin_ns: int = 10_000_000
    maximum_clock_drift_ppm: int = 1000

    def __post_init__(self) -> None:
        if (type(self.maximum_lease_ns) is not int or not 0 < self.maximum_lease_ns <= 30_000_000_000
                or type(self.uncertainty_margin_ns) is not int
                or not 0 < self.uncertainty_margin_ns < self.maximum_lease_ns
                or type(self.maximum_clock_drift_ppm) is not int
                or not 1 <= self.maximum_clock_drift_ppm <= 100_000
                or type(self.stop_propagation_reserve_ns) is not int or self.stop_propagation_reserve_ns <= 0):
            raise ValueError("M6 admission lease guard bounds invalid")


class M6OwnerClient:
    def __init__(
        self, *, anchor: M6OwnerAnchor, spec: M6RunSpec, transport: M6OwnerTransport,
        caller_role: M6CallerRole, producer_epoch_sha256: str,
        continuous_ns: Callable[[], int], lease_policy: M6LeasePolicy,
        maximum_wire_bytes: int = 65536,
    ) -> None:
        anchor.assert_spec(spec)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", producer_epoch_sha256):
            raise ValueError("M6 producer epoch invalid")
        if caller_role not in {"controller", "service_runner", "e2e_runner", "public_verifier", "quality_verifier"}:
            raise ValueError("M6 producer role invalid")
        if type(maximum_wire_bytes) is not int or not 4096 <= maximum_wire_bytes <= 65536:
            raise ValueError("M6 native owner wire bound invalid")
        self.anchor, self.spec = anchor, spec
        self._spec_sha = spec.canonical_sha256()
        self._anchor_sha = anchor.canonical_sha256()
        self._transport, self._clock = transport, continuous_ns
        self._role, self._epoch = caller_role, producer_epoch_sha256
        self._policy, self._maximum = lease_policy, maximum_wire_bytes
        budget_ns = spec.resources.stop_admission_budget_ticks * 1_000_000_000 // spec.clock.qpc_frequency_hz
        self._maximum_grant_ns = min(lease_policy.maximum_lease_ns, budget_ns - lease_policy.stop_propagation_reserve_ns)
        if self._maximum_grant_ns <= lease_policy.uncertainty_margin_ns:
            raise ValueError("M6 stop budget cannot cover the selected runner propagation bound and lease")
        self._thread = threading.get_ident()
        self._last_local_ns: int | None = None
        self._last_qpc = anchor.t0_ticks
        self._last_owner_sequence = 0
        self._state_rank = 0
        self._lease_until_ns = 0
        self._admission_halted = False
        self._closed = False

    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int or now < 0 or (self._last_local_ns is not None and now < self._last_local_ns):
            self._admission_halted = True
            self._lease_until_ns = 0
            raise M6OwnerProtocolError("M6 local continuous clock regressed or is invalid")
        self._last_local_ns = now
        return now

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._thread or self._closed:
            raise RuntimeError("M6 control client closed or used outside its owner thread")

    def _status(self, status: M6OwnerStatus) -> None:
        if (status.run_id != self.spec.run_id or status.spec_sha256 != self._spec_sha
                or status.anchor_sha256 != self._anchor_sha
                or status.owner_process_epoch_sha256 != self.anchor.owner_process_epoch_sha256):
            raise M6OwnerProtocolError("M6 owner run/spec/anchor/incarnation changed")
        if status.observed_qpc_ticks < self._last_qpc or status.last_sequence < self._last_owner_sequence:
            raise M6OwnerProtocolError("M6 owner receipt clock/sequence regressed")
        ranks = {"bound": 0, "open": 1, "stopping": 2, "draining": 3, "closed": 4, "failed": 5}
        rank = ranks[status.state]
        if rank < self._state_rank:
            raise M6OwnerProtocolError("M6 owner control state regressed")
        if status.admission_valid_until_ticks is not None and status.admission_valid_until_ticks > self.spec.deadline_ticks:
            raise M6OwnerProtocolError("M6 admission lease exceeds original deadline")
        self._last_qpc, self._last_owner_sequence = status.observed_qpc_ticks, status.last_sequence
        self._state_rank = rank
        if rank >= 2 or status.observed_qpc_ticks >= self.spec.deadline_ticks:
            self._admission_halted = True

    def request(self, command: M6OwnerCommand) -> M6OwnerReply:
        self._assert_owner()
        runner = "service_runner" if self.spec.mode == "service_diagnostic" else "e2e_runner"
        permitted = {
            "status": {"controller", runner, "public_verifier", "quality_verifier"},
            "lease": {runner}, "bind": {"controller"}, "open": {"controller"},
            "stop": {"controller", runner}, "admission_closed": {runner},
            "close": {"controller"}, "append": {runner, "public_verifier", "quality_verifier"},
        }
        if self._role not in permitted[command.kind]:
            self._lease_until_ns = 0
            raise M6OwnerProtocolError("M6 caller role cannot issue this control action")
        if isinstance(command, M6AdmissionClosedAck) and command.runner_epoch_sha256 != self._epoch:
            self._lease_until_ns = 0
            raise M6OwnerProtocolError("M6 admission closure belongs to another runner incarnation")
        if command.kind == "lease":
            self._lease_until_ns = 0
        if command.kind in {"stop", "admission_closed", "close"}:
            self._admission_halted = True
            self._lease_until_ns = 0
        request = M6OwnerRequest(run_id=self.spec.run_id, spec_sha256=self._spec_sha,
                                 request_id=uuid4().hex, command=command)
        if isinstance(command, M6AppendObservation):
            if (command.event.producer_kind != self._role or command.event.producer_epoch_sha256 != self._epoch
                    or self._role == "controller"):
                self._lease_until_ns = 0
                raise M6OwnerProtocolError("M6 caller cannot submit another producer/owner observation")
        raw = request.canonical_bytes()
        if len(raw) > self._maximum:
            self._admission_halted = True
            raise M6OwnerProtocolError("M6 control request exceeds wire bound")
        sent_ns = self._now()
        try:
            reply = M6OwnerReply.from_canonical_bytes(self._transport.exchange(raw), maximum_bytes=self._maximum)
            received_ns = self._now()
            if reply.request_sha256 != request.canonical_sha256():
                raise M6OwnerProtocolError("M6 reply does not bind this control request")
            self._status(reply.status)
            if reply.record is not None:
                stamp = reply.record.stamp
                if (stamp.boot_identity_sha256 != self.spec.clock.boot_identity_sha256
                        or stamp.received_qpc_ticks < self.anchor.t0_ticks):
                    raise M6OwnerProtocolError("M6 stamped observation belongs to another boot or precedes T0")
                # An exact retry may legitimately return a predecessor owner's
                # old stamp after explicit same-boot recovery. The status binds
                # the current owner; offline journal replay verifies the chain.
            if isinstance(command, M6AppendObservation) and reply.outcome != "rejected":
                if reply.record is None or reply.record.stamp.producer_event_sha256 != command.event.canonical_sha256():
                    raise M6OwnerProtocolError("M6 owner acknowledged different producer bytes")
            if reply.outcome != "ok":
                raise M6OwnerRejected(reply)
            if command.kind == "lease" and reply.status.admission_valid_until_ticks is not None:
                duration_ticks = reply.status.admission_valid_until_ticks - reply.status.observed_qpc_ticks
                duration_ns = duration_ticks * 1_000_000_000 // self.spec.clock.qpc_frequency_hz
                if duration_ns > self._maximum_grant_ns:
                    raise M6OwnerProtocolError("M6 owner lease exceeds guard or stop propagation budget")
                # Earliest local point in the exchange, never response receipt.
                # Clock drift shortens the grant; suspend is included by _clock.
                conservative = duration_ns * 1_000_000 // (1_000_000 + self._policy.maximum_clock_drift_ppm)
                until = sent_ns + conservative - self._policy.uncertainty_margin_ns
                if until > received_ns and not self._admission_halted:
                    self._lease_until_ns = until
            return reply
        except BaseException:
            self._admission_halted = True
            self._lease_until_ns = 0
            raise

    def bind(self) -> M6OwnerReply:
        return self.request(M6BindOwner(anchor_sha256=self._anchor_sha))

    def refresh_admission(self) -> bool:
        self.request(M6OwnerControl(kind="lease"))
        return self.admission_allowed()

    def admission_allowed(self) -> bool:
        self._assert_owner()
        return not self._admission_halted and self._now() < self._lease_until_ns

    def append(self, event: M6ProducerEvent) -> M6RunEvent:
        reply = self.request(M6AppendObservation(event=event))
        assert reply.record is not None  # request() checks this before success.
        return reply.record

    def close(self) -> None:
        if self._closed:
            return
        self._assert_owner()
        self._admission_halted = True
        self._lease_until_ns = 0
        self._transport.close()  # Local transport closure is not remote owner closure.
        self._closed = True


class M6LineOwnerTransport:
    """One persistent SSH direct-tcpip channel; no command or port forwarding daemon.

    Each request is an ASCII credential header then canonical JSON, both LF
    terminated. Responses are a single bounded canonical JSON line. Credentials
    never enter request/reply artifacts or exceptions. The opener must establish
    the authenticated pinned SSH session to the configured loopback owner port.
    """

    def __init__(
        self, *, token: str, open_channel: Callable[[float], Any], close_session: Callable[[], None],
        continuous_ns: Callable[[], int], timeout_ns: int = 5_000_000_000, maximum_wire_bytes: int = 65536,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("M6 private caller token must contain exactly 32 random bytes")
        if type(timeout_ns) is not int or not 0 < timeout_ns <= 30_000_000_000:
            raise ValueError("M6 control transport timeout invalid")
        if type(maximum_wire_bytes) is not int or not 4096 <= maximum_wire_bytes <= 65536:
            raise ValueError("M6 control transport wire bound invalid")
        self._header = b"M6-AUTH/1 " + token.encode("ascii") + b"\n"
        self._opener, self._close_session, self._clock = open_channel, close_session, continuous_ns
        self._timeout, self._maximum = timeout_ns, maximum_wire_bytes
        self._channel: Any = None
        self._channel_poisoned = False
        self._closed = False
        self._thread = threading.get_ident()

    def _assert_owner(self) -> None:
        if self._closed or threading.get_ident() != self._thread:
            raise RuntimeError("M6 transport closed or used outside its owner thread")

    def exchange(self, request: bytes) -> bytes:
        self._assert_owner()
        if not isinstance(request, bytes) or not request or len(request) > self._maximum or b"\n" in request or b"\r" in request:
            raise ValueError("M6 request violates line framing bound")
        start = self._clock()
        if type(start) is not int or start < 0:
            raise ValueError("M6 transport continuous clock invalid")
        deadline, previous = start + self._timeout, start

        def remaining() -> float:
            nonlocal previous
            now = self._clock()
            if type(now) is not int or now < previous:
                raise ValueError("M6 transport continuous clock regressed")
            previous = now
            if now >= deadline:
                raise TimeoutError("M6 control exchange deadline; remote outcome requires reconciliation")
            return (deadline - now) / 1_000_000_000

        try:
            if self._channel_poisoned:
                self._close_channel()  # Retain failed cleanup ownership, never reuse it.
            if self._channel is None:
                self._channel = self._opener(remaining())
            self._channel.settimeout(remaining())
            self._channel.sendall(self._header + request + b"\n")
            response = bytearray()
            while True:
                self._channel.settimeout(remaining())
                part = self._channel.recv(min(8192, self._maximum + 1 - len(response)))
                remaining()
                if not part:
                    raise EOFError("M6 owner response EOF; remote outcome requires reconciliation")
                response.extend(part)
                if len(response) > self._maximum + 1:
                    raise M6OwnerProtocolError("M6 owner response exceeds wire bound")
                if b"\n" in response:
                    if response[-1] != 10 or response.count(10) != 1 or b"\r" in response:
                        raise M6OwnerProtocolError("M6 owner response violates line framing")
                    return bytes(response[:-1])
                if len(response) > self._maximum:
                    raise M6OwnerProtocolError("M6 owner response exceeds wire bound")
        except BaseException as primary:
            self._channel_poisoned = True
            try:
                self._close_channel()
            except BaseException as cleanup:
                raise BaseExceptionGroup("M6 exchange and channel cleanup failed", [primary, cleanup])
            raise

    def _close_channel(self) -> None:
        if self._channel is not None:
            self._channel_poisoned = True
            self._channel.close()
            self._channel = None
        self._channel_poisoned = False

    def close(self) -> None:
        if self._closed:
            return
        self._assert_owner()
        errors: list[BaseException] = []
        for close in (self._close_channel, self._close_session):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("M6 transport closure not verified", errors)
        self._closed = True
