"""Durable spool and off-lane sender that turn lifecycle facts into M6 owner events.

``M6LifecycleSpool`` is the facts port: every fact becomes one canonical
``M6ProducerEvent`` whose bytes and producer sequence are appended to a local
JSONL spool with fsync before anything else happens. Recording never raises
for storage failure; it flips a visible ``failed`` state instead, so a claim
that already exists durably is never lost to notification trouble.

``M6E2EAssemblyWorker`` drains the spool on its own thread through an owner
client it constructs itself (the client asserts its creating thread). Exact
bytes and sequence are retried after transport faults; any owner conflict or
rejection is a failure of this run, never a delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptAdmitted, M6AttemptFinal, M6EventPayload, M6ProducerEvent, M6ProducerKind, M6PublicationCommitted,
    M6RemoteAccepted,
)
from disclosure_anchor.application.ports.staged_lifecycle_facts import (
    AttemptAdmittedFact, AttemptFinalFact, LifecycleFact, PublicationCommittedFact, RemoteAcceptedFact,
)
from disclosure_anchor.adapters.runtime.m6_owner_protocol import M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected

SPOOL_FILENAME = "spool.jsonl"
SPOOL_CONTRACT = "m6.e2e-assembly-spool.v1"
_MAX_FACTS_BOUND = 1_000_000


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    producer_sequence: int
    event_kind: str
    attempt_id: str
    fact_sha256: str
    event_utf8: str


class M6LifecycleSpool:
    """Facts port backed by an append-only, fsynced JSONL file."""

    def __init__(
        self, directory: Path, *, run_id: str, spec_sha256: str, producer_epoch_sha256: str, max_facts: int,
        producer_kind: M6ProducerKind = "e2e_runner",
    ) -> None:
        if type(max_facts) is not int or not 1 <= max_facts <= _MAX_FACTS_BOUND:
            raise ValueError("assembly spool fact bound is out of range")
        if producer_kind not in {"e2e_runner", "service_runner", "public_verifier", "quality_verifier"}:
            raise ValueError("assembly spool producer kind is not a producer role")
        self._producer_kind: M6ProducerKind = producer_kind
        self._path = directory / SPOOL_FILENAME
        if self._path.exists() or self._path.is_symlink():
            # Resuming an interrupted run's assembly is not supported here; a
            # fresh run must not silently continue another run's sequence.
            raise FileExistsError(f"assembly spool already exists: {self._path}")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        self._run_id, self._spec_sha256, self._epoch = run_id, spec_sha256, producer_epoch_sha256
        self._max_facts = max_facts
        self._lock = threading.Lock()
        self._sequence = 0
        self._entries: list[SpoolEntry] = []
        self._index: dict[tuple[str, str], str] = {}
        self._delivered: dict[int, int] = {}
        self._refused: dict[int, str] = {}
        self._failed = False
        self._failures: list[str] = []
        self._lost = 0
        self._conflicts = 0
        self._transport_retries = 0
        self._transport_errors: list[str] = []
        self._closed = False
        self._write({"kind": "spool_start", "contract_version": SPOOL_CONTRACT, "run_id": run_id,
                     "spec_sha256": spec_sha256, "producer_kind": producer_kind,
                     "producer_epoch_sha256": producer_epoch_sha256, "max_facts": max_facts})

    # -- facts port ---------------------------------------------------------
    def attempt_admitted(self, fact: AttemptAdmittedFact) -> None:
        self._record("attempt_admitted", fact.attempt_id, fact)

    def remote_accepted(self, fact: RemoteAcceptedFact) -> None:
        self._record("remote_accepted", fact.attempt_id, fact)

    def publication_committed(self, fact: PublicationCommittedFact) -> None:
        self._record("publication_committed", fact.attempt_id, fact)

    def attempt_final(self, fact: AttemptFinalFact) -> None:
        self._record("attempt_final", fact.attempt_id, fact)

    def record_event(self, payload: M6EventPayload, *, attempt_id: str) -> None:
        """Verifier producers spool a ready payload; the dedup key is (payload kind, attempt id)."""
        self._record(payload.kind, attempt_id, payload)

    def fact_unavailable(self, kind: str, attempt_id: str, reason: str) -> None:
        with self._lock:
            self._lost += 1
            self._fail(f"{kind}:{attempt_id}:unavailable:{reason}")
            self._write({"kind": "lost", "event_kind": kind, "attempt_id": attempt_id, "reason": reason})

    # -- worker side ----------------------------------------------------------
    def pending(self) -> tuple[SpoolEntry, ...]:
        with self._lock:
            return tuple(
                entry for entry in self._entries
                if entry.producer_sequence not in self._delivered and entry.producer_sequence not in self._refused
            )

    def mark_delivered(self, producer_sequence: int, owner_sequence: int, *, retries: int = 0) -> None:
        with self._lock:
            self._delivered[producer_sequence] = owner_sequence
            record: dict[str, Any] = {"kind": "delivered", "producer_sequence": producer_sequence,
                                      "owner_sequence": owner_sequence}
            if retries:
                # A delivery that needed exact replays says so; success never
                # erases the faults it recovered from.
                record["recovered_after_retries"] = retries
            self._write(record)

    def note_transport_retry(self, producer_sequence: int, *, attempt: int, error: str) -> None:
        """A bounded exact replay is safe, but the transport fault it recovers from stays on record.

        This is not a failure of the run: the same bytes and sequence are sent
        again and the owner deduplicates. It is durable evidence that a channel
        was lost or a reply timed out, with the sequence, the attempt number
        and the error text, so a later success does not hide the fault.
        """
        with self._lock:
            self._transport_retries += 1
            if len(self._transport_errors) < 32:
                self._transport_errors.append(f"sequence {producer_sequence} attempt {attempt}: {error[:200]}")
            self._write({"kind": "transport_retry", "producer_sequence": producer_sequence, "attempt": attempt,
                         "error": error[:300]})

    def mark_refused(self, producer_sequence: int, error_code: str, detail: str | None = None) -> None:
        with self._lock:
            self._refused[producer_sequence] = error_code
            self._fail(f"sequence {producer_sequence}:{error_code}")
            self._write({"kind": "refused", "producer_sequence": producer_sequence, "error_code": error_code,
                         "detail": detail})

    def note_failure(self, reason: str) -> None:
        with self._lock:
            self._fail(reason)
            self._write({"kind": "worker_failure", "reason": reason})

    @property
    def failed(self) -> bool:
        return self._failed

    def attempt_ids(self, event_kind: str) -> tuple[str, ...]:
        """Sorted attempt ids that have a recorded fact of ``event_kind``."""
        with self._lock:
            return tuple(sorted(entry.attempt_id for entry in self._entries if entry.event_kind == event_kind))

    def last_delivered_sequence(self) -> int:
        with self._lock:
            return max(self._delivered, default=0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = sum(
                1 for entry in self._entries
                if entry.producer_sequence not in self._delivered and entry.producer_sequence not in self._refused
            )
            return {
                "contract_version": SPOOL_CONTRACT, "path": str(self._path), "recorded": len(self._entries),
                "delivered": len(self._delivered), "refused": len(self._refused), "pending": pending,
                "lost_facts": self._lost, "conflicts": self._conflicts, "failed": self._failed,
                "failures": list(self._failures[:32]),
                "transport_retries": self._transport_retries, "transport_errors": list(self._transport_errors),
            }

    def close(self) -> dict[str, Any]:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    os.fsync(self._fd)
                finally:
                    os.close(self._fd)
        return self.status()

    # -- internals ------------------------------------------------------------
    def _record(self, kind: str, attempt_id: str, fact: LifecycleFact | M6EventPayload) -> None:
        with self._lock:
            if self._closed:
                self._lost += 1
                self._fail(f"{kind}:{attempt_id}:spool closed")
                return
            try:
                payload = _payload(fact)
                fact_sha256 = "sha256:" + hashlib.sha256(payload.canonical_bytes()).hexdigest()
            except Exception as exc:  # noqa: BLE001 - visible as a lost fact, never a supply failure
                self._lost += 1
                self._fail(f"{kind}:{attempt_id}:payload:{type(exc).__name__}:{exc}")
                self._write({"kind": "lost", "event_kind": kind, "attempt_id": attempt_id, "reason": repr(exc)[:300]})
                return
            key = (kind, attempt_id)
            previous = self._index.get(key)
            if previous is not None:
                if previous == fact_sha256:
                    return  # exact replay of an already recorded fact
                self._conflicts += 1
                self._fail(f"{kind}:{attempt_id}:conflicting fact")
                self._write({"kind": "conflict_fact", "event_kind": kind, "attempt_id": attempt_id,
                             "previous_sha256": previous, "fact_sha256": fact_sha256})
                return
            if len(self._entries) >= self._max_facts:
                self._lost += 1
                self._fail(f"{kind}:{attempt_id}:fact bound exceeded")
                self._write({"kind": "lost", "event_kind": kind, "attempt_id": attempt_id, "reason": "fact bound"})
                return
            sequence = self._sequence + 1
            try:
                event = M6ProducerEvent(
                    run_id=self._run_id, spec_sha256=self._spec_sha256, producer_kind=self._producer_kind,
                    producer_epoch_sha256=self._epoch, producer_sequence=sequence, payload=payload,
                )
                event_utf8 = event.canonical_bytes().decode("utf-8")
            except Exception as exc:  # noqa: BLE001 - visible as a lost fact
                self._lost += 1
                self._fail(f"{kind}:{attempt_id}:event:{type(exc).__name__}:{exc}")
                self._write({"kind": "lost", "event_kind": kind, "attempt_id": attempt_id, "reason": repr(exc)[:300]})
                return
            entry = SpoolEntry(sequence, kind, attempt_id, fact_sha256, event_utf8)
            if not self._write({"kind": "fact", "producer_sequence": sequence, "event_kind": kind,
                                "attempt_id": attempt_id, "fact_sha256": fact_sha256, "event_utf8": event_utf8}):
                self._lost += 1
                return
            self._sequence = sequence
            self._entries.append(entry)
            self._index[key] = fact_sha256

    def _write(self, record: dict[str, Any]) -> bool:
        """Append one whole line and fsync it; anything less is a visible failure.

        ``os.write`` may write fewer bytes than asked without raising. The
        line is completed in a bounded loop; a short write that makes no
        progress or fails part-way leaves a truncated tail on disk, which is
        reported and never counted as a durable record.
        """
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        written = 0
        stalls = 0
        try:
            while written < len(line):
                count = os.write(self._fd, line[written:])
                if count <= 0:
                    stalls += 1
                    if stalls >= 8:
                        raise OSError("spool write made no progress")
                    continue
                written += count
            os.fsync(self._fd)
        except OSError as exc:
            self._fail(f"spool write:{type(exc).__name__}:{written}/{len(line)} bytes")
            if 0 < written < len(line):
                self._fail("spool holds a truncated record tail")
            return False
        return True

    def _fail(self, reason: str) -> None:
        self._failed = True
        if len(self._failures) < 256:
            self._failures.append(reason)


def _payload(fact: LifecycleFact | M6EventPayload) -> M6EventPayload:
    if not isinstance(fact, (AttemptAdmittedFact, RemoteAcceptedFact, PublicationCommittedFact, AttemptFinalFact)):
        return fact  # an already closed producer payload (verifier events)
    if isinstance(fact, AttemptAdmittedFact):
        return M6AttemptAdmitted(
            attempt_id=fact.attempt_id, fence_identity=fact.fence_identity, document_id=fact.document_id,
            processing_run_id=fact.processing_run_id, source_pdf_sha256=fact.source_pdf_sha256,
            source_byte_count=fact.source_byte_count, source_page_count=fact.source_page_count,
            process_profile_sha256=fact.process_profile_sha256,
        )
    if isinstance(fact, RemoteAcceptedFact):
        return M6RemoteAccepted(
            attempt_id=fact.attempt_id, remote_task_identity_sha256=fact.remote_task_identity_sha256,
            acceptance_receipt_sha256=fact.acceptance_receipt_sha256,
        )
    if isinstance(fact, PublicationCommittedFact):
        return M6PublicationCommitted(
            attempt_id=fact.attempt_id, processing_run_id=fact.processing_run_id, document_id=fact.document_id,
            source_pdf_sha256=fact.source_pdf_sha256, source_page_count=fact.source_page_count,
            ledger_seq=fact.ledger_seq, winner_sha256=fact.winner_sha256,
            durable_base_sha256=fact.durable_base_sha256,
        )
    if isinstance(fact, AttemptFinalFact):
        return M6AttemptFinal(
            attempt_id=fact.attempt_id, outcome=fact.outcome, remote_disposition=fact.remote_disposition,
            remote_receipt_sha256=fact.remote_receipt_sha256,
            remote_task_identity_sha256=fact.remote_task_identity_sha256,
            cleanup_receipt_sha256=fact.cleanup_receipt_sha256,
        )
    raise TypeError("lifecycle fact type is not closed")


class M6E2EAssemblyWorker:
    """Single sender thread: exact bytes, exact sequence, bounded retries, no dedup by conflict."""

    def __init__(
        self, spool: M6LifecycleSpool, *, client_factory: Callable[[], M6OwnerClient],
        retry_limit: int = 5, backoff_seconds: float = 1.0, poll_seconds: float = 0.5,
        maximum_wire_bytes: int = 65536, lease_refresh_seconds: float | None = None,
        continuous_ns: Callable[[], int] | None = None,
    ) -> None:
        if not callable(client_factory):
            raise ValueError("assembly worker requires an owner client factory")
        if type(retry_limit) is not int or not 0 <= retry_limit <= 100:
            raise ValueError("assembly retry limit is out of range")
        if not 0.05 <= float(backoff_seconds) <= 60 or not 0.05 <= float(poll_seconds) <= 10:
            raise ValueError("assembly worker timing is out of range")
        if lease_refresh_seconds is not None and not 0.1 <= float(lease_refresh_seconds) <= 30:
            raise ValueError("assembly lease refresh interval is out of range")
        if continuous_ns is not None and not callable(continuous_ns):
            raise ValueError("assembly continuous clock must be callable")
        self._spool, self._factory = spool, client_factory
        self._retry_limit, self._backoff, self._poll = retry_limit, float(backoff_seconds), float(poll_seconds)
        self._maximum = maximum_wire_bytes
        self._lease_refresh = None if lease_refresh_seconds is None else float(lease_refresh_seconds)
        self._clock = continuous_ns
        # The lease is never cached as permission: the client keeps the granted
        # deadline, and any thread compares it with the same continuous clock.
        self._client: M6OwnerClient | None = None
        self._exited = False
        self._after_drain: Callable[[M6OwnerClient], Any] | None = None
        self._after_drain_result: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("assembly worker already started")
        self._thread = threading.Thread(target=self._run, name="m6-e2e-assembly", daemon=True)
        self._thread.start()

    def admission_allowed(self) -> bool:
        """Readable from any thread: a live, unfailed sender holding an unexpired lease.

        The deadline is the client's own granted lease compared with the same
        continuous clock, so a blocked exchange cannot keep admission open past
        it, and a sender that failed or exited never keeps a granted lease visible.
        """
        if self.failed or self._exited:
            return False
        if self._lease_refresh is None:
            return self._thread is not None and self._thread.is_alive()
        client = self._client
        if client is None:
            return False
        # The deadline lives on the client's continuous clock; an explicit clock
        # must be that same clock, otherwise the client's own is used.
        clock = self._clock or client.continuous_clock
        return clock() < client.lease_until_ns

    def close(
        self, deadline_seconds: float, *, after_drain: Callable[[M6OwnerClient], Any] | None = None,
    ) -> dict[str, Any]:
        """Stop after the spool drains or the deadline passes; report what remains.

        ``after_drain`` runs on the worker thread with its own client once every
        recorded fact was delivered (the runner's closure sequence); it is
        skipped, and reported as such, when anything remains undelivered.
        """
        if after_drain is not None and not callable(after_drain):
            raise ValueError("after_drain must be callable")
        self._after_drain = after_drain
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, float(deadline_seconds)))
            if self._thread.is_alive():
                self._spool.note_failure("assembly worker did not finish before its close deadline")
        status = self._spool.status()
        if status["pending"]:
            self._spool.note_failure(f"{status['pending']} recorded fact(s) were never delivered")
        status = self._spool.status()
        status["worker_error"] = self._error
        status["after_drain"] = self._after_drain_result
        return status

    @property
    def failed(self) -> bool:
        return self._spool.failed or self._error is not None

    def _refresh_lease(self, client: M6OwnerClient, last_refresh: float) -> float:
        if self._lease_refresh is None:
            return last_refresh
        now = time.monotonic()
        if now - last_refresh < self._lease_refresh:
            return last_refresh
        try:
            client.refresh_admission()
        except (M6OwnerProtocolError, M6OwnerRejected, EOFError, OSError, RuntimeError, TimeoutError) as exc:
            # A failed refresh never extends the lease the client already holds;
            # it is recorded and the sender stops, which closes admission.
            self._spool.note_failure("lease refresh: " + f"{type(exc).__name__}:{exc}"[:200])
            raise
        return now

    def _run(self) -> None:
        client: M6OwnerClient | None = None
        try:
            client = self._factory()
            self._client = client
            attempts: dict[int, int] = {}
            last_refresh = self._refresh_lease(client, float("-inf"))
            while True:
                last_refresh = self._refresh_lease(client, last_refresh)
                pending = self._spool.pending()
                if not pending:
                    if self._stop.is_set():
                        if self._after_drain is not None and not self._spool.failed:
                            self._after_drain_result = self._after_drain(client)
                        return
                    time.sleep(self._poll)
                    continue
                entry = pending[0]
                event = M6ProducerEvent.from_canonical_bytes(entry.event_utf8.encode("utf-8"), maximum_bytes=self._maximum)
                try:
                    record = client.append(event)
                except M6OwnerRejected as exc:
                    # Every conflict or rejection is this run's failure; the
                    # owner journals the first variant and never re-accepts.
                    self._spool.mark_refused(entry.producer_sequence, str(exc.reply.error_code or exc.reply.outcome),
                                             detail=exc.reply.outcome)
                    return
                except (M6OwnerProtocolError, EOFError, OSError, RuntimeError, TimeoutError) as exc:
                    # Lost channel (EOF), timeout, protocol or IO fault: retry the
                    # identical sequence and bytes a bounded number of times.
                    count = attempts.get(entry.producer_sequence, 0) + 1
                    attempts[entry.producer_sequence] = count
                    error_text = f"{type(exc).__name__}:{exc}"[:300]
                    if count > self._retry_limit:
                        self._spool.mark_refused(entry.producer_sequence, "transport_exhausted", detail=error_text)
                        return
                    self._spool.note_transport_retry(entry.producer_sequence, attempt=count, error=error_text)
                    time.sleep(self._backoff)
                    continue
                self._spool.mark_delivered(entry.producer_sequence, record.stamp.sequence,
                                           retries=attempts.get(entry.producer_sequence, 0))
        except Exception as exc:  # noqa: BLE001 - recorded visibly; the thread must not die silently
            self._error = f"{type(exc).__name__}:{exc}"[:300]
            self._spool.note_failure("assembly worker error: " + self._error)
        finally:
            # Order matters for cross-thread readers: admission is closed before
            # the client releases anything, whatever path brought us here.
            self._exited = True
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - closing evidence only
                    self._spool.note_failure("assembly client close: " + f"{type(exc).__name__}:{exc}"[:200])


__all__ = ["M6E2EAssemblyWorker", "M6LifecycleSpool", "SPOOL_CONTRACT", "SPOOL_FILENAME", "SpoolEntry"]
