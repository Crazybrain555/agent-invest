"""Ongoing per-attempt verification over one long-lived sender per verifier role.

A formal M6 run credits an attempt only when its publication, its public
confirmation and its final qualification were all received by the owner before
the deadline. Verifying after the run therefore proves whole-run totals only.
This module composes the existing verifiers as an ongoing supply: it discovers
attempts from the runner's durable spool, dispatches a public confirmation and
then a qualification for each as soon as the owner has acknowledged the
runner's own facts for it, and closes with exactly one terminal drain by the
run's drain role after the whole attempt set is reconciled.

Boundaries kept deliberately small so they can be tested independently:

* ``RunnerSpoolTail`` reads the growing JSONL spool with a bounded partial-line
  buffer. An attempt becomes eligible only when the owner's durable
  ``delivered`` receipt exists for both its ``attempt_admitted`` and its
  ``publication_committed`` fact, so no verifier can outrun the runner at the
  owner. A locally written fact without that receipt waits. Malformed complete
  lines, identity mismatches and an oversized partial line are visible errors;
  a partial line at EOF while the producer is live is not.
* ``VerifierSupervisor`` owns two assemblies (one sender and sequence per role
  for the entire run), dispatches through injected ``confirm``/``qualify``
  callables, and refuses to drain while anything is queued, in flight, failed
  or different from the expected attempt set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, NoReturn, Protocol

import hashlib
import os

from disclosure_anchor.application.contracts.m6_run_events import M6DocumentQualified, M6ProducerEvent, M6PublicConfirmation
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


class SpoolTailError(RuntimeError):
    """The runner spool is not readable as the run's exact fact stream."""


class SupervisorRefused(RuntimeError):
    """A closing step was asked for while its preconditions do not hold; nothing was sent."""


@dataclass(frozen=True, slots=True)
class ReadyAttempt:
    attempt_id: str
    admitted_owner_sequence: int
    committed_owner_sequence: int
    committed_producer_sequence: int


class RunnerSpoolTail:
    """Incremental reader of ``M6LifecycleSpool`` output; yields attempts whose runner facts the owner acknowledged."""

    def __init__(self, path: Path, *, run_id: str, spec_sha256: str, max_line_bytes: int = 1_048_576) -> None:
        self._path = path
        self._run_id, self._spec_sha256 = run_id, spec_sha256
        if type(max_line_bytes) is not int or max_line_bytes < 256:
            raise ValueError("spool line bound must be an integer of at least 256 bytes")
        self._max_line = max_line_bytes
        self._offset = 0            # byte boundary of the last complete line consumed
        self._partial_bytes = 0     # unterminated tail still on disk after the boundary
        self._started = False
        self._producer_kind: str | None = None
        self._producer_epoch: str | None = None
        self._facts: dict[int, tuple[str, str]] = {}          # producer_sequence -> (event_kind, attempt_id)
        self._delivered: dict[int, int] = {}                   # producer_sequence -> owner_sequence
        self._admitted: dict[str, int] = {}                    # attempt_id -> producer_sequence
        self._committed: dict[str, int] = {}
        self._released: set[str] = set()
        self._records = 0
        self._failed: str | None = None

    @property
    def failed(self) -> str | None:
        return self._failed

    def poll(self) -> tuple[ReadyAttempt, ...]:
        """Consume newly appended complete lines; return attempts that became eligible, each exactly once.

        Reads resume at the last complete-line boundary in bounded chunks, so an
        unterminated tail stays on disk rather than in memory, a complete or
        partial line beyond the bound is a visible failure, and a file that
        shrank below the consumed boundary (truncated or replaced) is refused.
        """
        if self._failed is not None:
            raise SpoolTailError(self._failed)
        try:
            with self._path.open("rb") as handle:
                if os.fstat(handle.fileno()).st_size < self._offset:
                    self._fail("spool shrank below the consumed boundary; truncated or replaced")
                handle.seek(self._offset)
                buffer = b""
                while True:
                    chunk = handle.read(self._max_line + 1)
                    if not chunk:
                        break
                    buffer += chunk
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            break
                        line, buffer = buffer[:newline], buffer[newline + 1:]
                        if len(line) > self._max_line:
                            self._fail(f"complete spool line of {len(line)} bytes exceeds the {self._max_line}-byte bound")
                        self._offset += newline + 1
                        if line:
                            self._apply(line)
                    if len(buffer) > self._max_line:
                        self._fail(f"partial spool line of {len(buffer)} bytes exceeds the {self._max_line}-byte bound")
                self._partial_bytes = len(buffer)
        except FileNotFoundError:
            # Waiting for the runner's first write is normal; a spool that was
            # already read and then vanished is not an empty run.
            if self._started or self._offset:
                self._fail("runner spool disappeared after it had been read")
            return ()
        return self._eligible()

    def finalize(self) -> dict[str, Any]:
        """After the producer is known to have closed, the tail must be a complete, started spool.

        A source that never became readable, or never carried its ``spool_start``
        record, is a missing spool rather than an empty run: nothing about the
        runner's facts is known, so the closed tail is a visible failure. A
        leftover partial line is one as well.
        """
        if not self._started:
            self._fail("runner spool was never readable as a started spool" if self._records == 0
                       else "runner spool has no spool_start record")
        if self._partial_bytes:
            self._fail(f"spool ends with a partial record of {self._partial_bytes} bytes")
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self._path), "bytes_consumed": self._offset, "partial_bytes": self._partial_bytes,
            "records": self._records, "facts": len(self._facts), "delivered": len(self._delivered),
            "admitted": len(self._admitted), "committed": len(self._committed), "released": len(self._released),
            "awaiting_receipt": sorted(set(self._committed) - self._released), "failed": self._failed,
        }

    def committed_attempts(self) -> frozenset[str]:
        """Attempts with a publication fact in the spool, acknowledged or not."""
        return frozenset(self._committed)

    def released_attempts(self) -> frozenset[str]:
        """Attempts whose admission and publication the owner durably acknowledged; the reconciliation set."""
        return frozenset(self._released)

    # -- internals ------------------------------------------------------------
    def _fail(self, reason: str) -> NoReturn:
        self._failed = reason
        raise SpoolTailError(reason)

    def _apply(self, line: bytes) -> None:
        try:
            parsed = strict_json_loads(line)
        except Exception as exc:  # noqa: BLE001 - a malformed complete line is never skipped
            self._fail(f"malformed spool record {self._records + 1}: {type(exc).__name__}")
        if not isinstance(parsed, dict) or type(parsed.get("kind")) is not str:
            self._fail(f"spool record {self._records + 1} has no kind")
        record: dict[str, Any] = parsed
        self._records += 1
        kind = record["kind"]
        if not self._started:
            if kind != "spool_start":
                self._fail("spool does not start with spool_start")
            if record.get("run_id") != self._run_id or record.get("spec_sha256") != self._spec_sha256:
                self._fail("spool belongs to another run or spec")
            if record.get("producer_kind") not in {"e2e_runner", "service_runner"}:
                self._fail("spool is not a runner's")
            if type(record.get("producer_epoch_sha256")) is not str:
                self._fail("spool start names no producer epoch")
            self._producer_kind, self._producer_epoch = record["producer_kind"], record["producer_epoch_sha256"]
            self._started = True
            return
        if kind == "fact":
            sequence, event_kind, attempt_id = record.get("producer_sequence"), record.get("event_kind"), record.get("attempt_id")
            event_utf8, fact_sha256 = record.get("event_utf8"), record.get("fact_sha256")
            if (type(sequence) is not int or type(event_kind) is not str or type(attempt_id) is not str
                    or type(event_utf8) is not str or type(fact_sha256) is not str):
                self._fail(f"spool fact record {self._records} is not closed")
            if sequence in self._facts:
                self._fail(f"spool repeats producer sequence {sequence}")
            self._verify_embedded_event(event_utf8, sequence=sequence, event_kind=event_kind, attempt_id=attempt_id,
                                        fact_sha256=fact_sha256)
            self._facts[sequence] = (event_kind, attempt_id)
            if event_kind == "attempt_admitted":
                self._admitted.setdefault(attempt_id, sequence)
            elif event_kind == "publication_committed":
                self._committed.setdefault(attempt_id, sequence)
        elif kind == "delivered":
            sequence, owner_sequence = record.get("producer_sequence"), record.get("owner_sequence")
            if type(sequence) is not int or type(owner_sequence) is not int:
                self._fail(f"spool delivered record {self._records} is not closed")
            if sequence not in self._facts:
                self._fail(f"spool delivered sequence {sequence} precedes its fact")
            self._delivered.setdefault(sequence, owner_sequence)
        elif kind in {"refused", "conflict_fact", "worker_failure"}:
            # The runner's own failure is the runner's; the supervisor only stops
            # releasing attempts whose receipts will never come.
            return
        # transport_retry, lost and other informational kinds carry no eligibility.

    def _verify_embedded_event(
        self, event_utf8: str, *, sequence: int, event_kind: str, attempt_id: str, fact_sha256: str,
    ) -> None:
        """The embedded producer event must be the run's, this producer's, this sequence's, and hash-bound."""
        try:
            event = M6ProducerEvent.from_canonical_bytes(event_utf8.encode("utf-8"), maximum_bytes=self._max_line)
        except Exception as exc:  # noqa: BLE001 - a fact that is not a closed event is never eligible
            self._fail(f"spool fact {sequence} embeds no canonical producer event: {type(exc).__name__}")
        payload = event.payload
        if (event.run_id != self._run_id or event.spec_sha256 != self._spec_sha256
                or event.producer_kind != self._producer_kind or event.producer_epoch_sha256 != self._producer_epoch):
            self._fail(f"spool fact {sequence} belongs to another run, spec or producer")
        if event.producer_sequence != sequence or payload.kind != event_kind:
            self._fail(f"spool fact {sequence} disagrees with its embedded event")
        if getattr(payload, "attempt_id", None) != attempt_id:
            self._fail(f"spool fact {sequence} names another attempt than its event")
        if "sha256:" + hashlib.sha256(payload.canonical_bytes()).hexdigest() != fact_sha256:
            self._fail(f"spool fact {sequence} hash differs from its payload")

    def _eligible(self) -> tuple[ReadyAttempt, ...]:
        ready: list[ReadyAttempt] = []
        for attempt_id, committed_sequence in self._committed.items():
            if attempt_id in self._released:
                continue
            admitted_sequence = self._admitted.get(attempt_id)
            if admitted_sequence is None:
                continue
            admitted_owner = self._delivered.get(admitted_sequence)
            committed_owner = self._delivered.get(committed_sequence)
            if admitted_owner is None or committed_owner is None:
                continue
            ready.append(ReadyAttempt(attempt_id, admitted_owner, committed_owner, committed_sequence))
        ready.sort(key=lambda item: item.committed_owner_sequence)
        for item in ready:
            self._released.add(item.attempt_id)
        return tuple(ready)


class _Assembly(Protocol):
    """The slice of ``M6VerifierAssembly`` the supervisor relies on."""

    @property
    def failed(self) -> bool: ...
    @property
    def is_drain_role(self) -> bool: ...
    def record(self, payload: Any, *, attempt_id: str) -> None: ...
    def complete(self, *, deadline_seconds: float = ...) -> dict[str, Any]: ...
    def finish(self, drain_receipt_sha256: str, *, deadline_seconds: float = ...) -> dict[str, Any]: ...
    def abort(self, reason: str, *, deadline_seconds: float = ...) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PublicOutcome:
    """Result of one public confirmation; ``payload`` is the owner event, ``receipt`` feeds the quality step."""

    payload: M6PublicConfirmation | None
    receipt: Any | None
    error: str | None = None
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualityOutcome:
    payload: M6DocumentQualified | None
    error: str | None = None
    files: tuple[str, ...] = ()


@dataclass(slots=True)
class AttemptRecord:
    attempt: ReadyAttempt
    public: PublicOutcome | None = None
    quality: QualityOutcome | None = None
    started_ns: int = 0
    finished_ns: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt.attempt_id, "admitted_owner_sequence": self.attempt.admitted_owner_sequence,
            "committed_owner_sequence": self.attempt.committed_owner_sequence,
            "public": None if self.public is None else {"confirmed": self.public.payload is not None,
                                                        "error": self.public.error, "files": list(self.public.files)},
            "quality": None if self.quality is None else {"qualified": self.quality.payload is not None,
                                                          "error": self.quality.error, "files": list(self.quality.files)},
            "started_ns": self.started_ns, "finished_ns": self.finished_ns,
        }


@dataclass(slots=True)
class SupervisorStatus:
    processed: list[AttemptRecord] = field(default_factory=list)
    failed_reason: str | None = None


class VerifierSupervisor:
    """Dispatch public then quality verification per eligible attempt; drain once, only after reconciliation."""

    def __init__(
        self, *, public: _Assembly, quality: _Assembly, source: Callable[[], tuple[ReadyAttempt, ...]],
        confirm: Callable[[ReadyAttempt], PublicOutcome], qualify: Callable[[ReadyAttempt, PublicOutcome], QualityOutcome],
        max_attempts: int, monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("supervisor attempt bound must be a positive integer")
        if not public.is_drain_role or quality.is_drain_role:
            raise ValueError("the supervisor's public assembly must be the run's drain role and quality must not be")
        self._public, self._quality = public, quality
        self._source, self._confirm, self._qualify = source, confirm, qualify
        self._max_attempts = max_attempts
        self._clock = monotonic_ns
        self._queue: list[ReadyAttempt] = []
        self._records: dict[str, AttemptRecord] = {}
        self._closed = False
        self._failed: str | None = None
        self._cleanup_error: str | None = None

    # -- observation ----------------------------------------------------------
    @property
    def failed(self) -> str | None:
        return self._failed or ("public sender failed" if self._public.failed else None) \
            or ("quality sender failed" if self._quality.failed else None)

    def processed_attempts(self) -> frozenset[str]:
        return frozenset(self._records)

    def status(self) -> dict[str, Any]:
        return {
            "queued": [item.attempt_id for item in self._queue], "processed": len(self._records),
            "confirmed": sum(1 for r in self._records.values() if r.public is not None and r.public.payload is not None),
            "qualified": sum(1 for r in self._records.values() if r.quality is not None and r.quality.payload is not None),
            "failed": self.failed, "closed": self._closed, "max_attempts": self._max_attempts,
            "cleanup_error": self._cleanup_error,
            "attempts": [record.as_dict() for record in self._records.values()],
        }

    # -- ongoing dispatch -----------------------------------------------------
    def step(self) -> int:
        """Poll the source once and verify every queued attempt in order; returns how many were processed."""
        if self._closed:
            raise SupervisorRefused("supervisor is closed")
        if self.failed is not None:
            raise SupervisorRefused("supervisor stopped: " + self.failed)
        for item in self._source():
            if item.attempt_id in self._records or any(q.attempt_id == item.attempt_id for q in self._queue):
                self._fail(f"attempt {item.attempt_id} was released twice")
            if len(self._records) + len(self._queue) >= self._max_attempts:
                self._fail("attempt bound exceeded")
            self._queue.append(item)
        processed = 0
        while self._queue and self.failed is None:
            item = self._queue.pop(0)
            self._process(item)
            processed += 1
        return processed

    def run_until(self, stop: Callable[[], bool], *, poll_seconds: float, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            self.step()
            if stop():
                self.step()  # one last look after the stop condition became true
                return
            sleep(poll_seconds)

    def _process(self, item: ReadyAttempt) -> None:
        """Confirm, then qualify. A missing payload or an exception is recorded and makes the run failed.

        Business observations (a review-pending qualification is one) are
        evidence and flow normally; an exception or an outcome without an owner
        event is not success and can never be closed as one. The record stays.
        """
        record = AttemptRecord(item, started_ns=self._clock())
        self._records[item.attempt_id] = record
        try:
            public = self._confirm(item)
        except Exception as exc:  # noqa: BLE001 - recorded on the attempt and as the run's failure
            public = PublicOutcome(None, None, f"{type(exc).__name__}:{exc}"[:300])
        record.public = public
        if public.payload is None:
            record.finished_ns = self._clock()
            self._failed = f"attempt {item.attempt_id}: public confirmation failed: {public.error}"
            return
        self._public.record(public.payload, attempt_id=item.attempt_id)
        try:
            quality = self._qualify(item, public)
        except Exception as exc:  # noqa: BLE001 - recorded on the attempt and as the run's failure
            quality = QualityOutcome(None, f"{type(exc).__name__}:{exc}"[:300])
        record.quality = quality
        record.finished_ns = self._clock()
        if quality.payload is None:
            self._failed = f"attempt {item.attempt_id}: qualification failed: {quality.error}"
            return
        self._quality.record(quality.payload, attempt_id=item.attempt_id)

    def _fail(self, reason: str) -> NoReturn:
        self._failed = reason
        raise SupervisorRefused(reason)

    # -- closing --------------------------------------------------------------
    def close(
        self, *, expected_attempts: frozenset[str], write_receipt: Callable[[bytes], str], runner_complete: bool,
        deadline_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Complete quality, then drain public once; refuse while anything is pending or mismatched."""
        if self._closed:
            raise SupervisorRefused("supervisor already closed")
        if self._queue:
            raise SupervisorRefused("attempts are still queued")
        if not runner_complete:
            raise SupervisorRefused("runner assembly has not completed")
        if self.processed_attempts() != expected_attempts:
            raise SupervisorRefused("processed attempt set differs from the expected set")
        self._closed = True
        try:
            quality_status = self._quality.complete(deadline_seconds=deadline_seconds)
        except BaseException as exc:
            # The other owned role must not stay live; the quality failure stays the reported error.
            self._abort_public("downstream quality close failed: " + f"{type(exc).__name__}:{exc}"[:200])
            raise
        if quality_status["status"] != "complete" or self.failed is not None:
            public_status = self._abort_public("downstream quality did not complete; no drain claimed")
            return {"status": "failed", "quality": quality_status, "public": public_status,
                    "drain_receipt_sha256": None, "cleanup_error": self._cleanup_error,
                    "attempts": [r.as_dict() for r in self._records.values()]}
        receipt = json.dumps({
            "contract_version": "m6.verifier-drain-receipt.v1", "producer_kind": "public_verifier",
            "attempt_set": sorted(expected_attempts), "attempts": [r.as_dict() for r in self._records.values()],
            "downstream": {"producer_kind": "quality_verifier", "status": quality_status["status"],
                           "spool": quality_status.get("spool")},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            digest = write_receipt(receipt)
        except BaseException:
            self._abort_public("drain receipt not written; no drain claimed")
            raise
        public_status = self._public.finish(digest, deadline_seconds=deadline_seconds)
        status = "complete" if public_status["status"] == "complete" else "failed"
        return {"status": status, "quality": quality_status, "public": public_status,
                "drain_receipt_sha256": digest, "cleanup_error": self._cleanup_error,
                "attempts": [r.as_dict() for r in self._records.values()]}

    def _abort_public(self, reason: str) -> dict[str, Any]:
        """Abort the public sender without letting a cleanup failure replace the failure being reported."""
        try:
            return self._public.abort(reason)
        except Exception as exc:  # noqa: BLE001 - recorded beside the original failure, never in its place
            self._cleanup_error = f"public abort failed: {type(exc).__name__}:{exc}"[:300]
            return {"status": "failed", "abort_reason": reason, "abort_error": self._cleanup_error}


def write_receipt_file(path: Path) -> Callable[[bytes], str]:
    """A ``write_receipt`` that persists the exact bytes with O_EXCL and returns their digest."""
    from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
    import hashlib

    def write(payload: bytes) -> str:
        write_new_exact(path, payload)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    return write


__all__ = [
    "AttemptRecord", "PublicOutcome", "QualityOutcome", "ReadyAttempt", "RunnerSpoolTail", "SpoolTailError",
    "SupervisorRefused", "VerifierSupervisor", "write_receipt_file",
]
