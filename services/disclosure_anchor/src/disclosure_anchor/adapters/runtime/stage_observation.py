"""Bounded local JSONL sinks for stage notes and coordinator progress.

Not a diagnostic platform: one events file, one progress file, one summary.
Every loss is counted sticky and reported; a lost record or a dead writer
never turns into a complete-looking measurement. Business execution is never
blocked by the sinks and no sink method raises into business callbacks; the
only coupling offered is ``writer_failed``, which a caller may use to stop
admitting new work while accepted work drains.

Closure protocol: ``close`` first refuses further notes, then tries to enqueue
the closing record with a bounded wait, then raises a separate stop flag and
joins the real writer thread. The writer drains every accepted record after
the flag and closes the events descriptor itself when it exits; a join that
times out is recorded as ``join_timeout`` and leaves the descriptor with the
thread, so a blocked writer that recovers later still exits on its own.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

from disclosure_anchor.application.ports.staged_execution import StageNote, StageObserverPort
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorSnapshot


SUMMARY_CONTRACT_VERSION = "staged-observation-summary.v1"
_EVENTS_FILENAME = "stage-events.jsonl"
_PROGRESS_FILENAME = "progress.jsonl"
_SUMMARY_FILENAME = "observation-summary.json"
_MAX_FAILURE_TYPES = 16
# Compatibility stop token: the writer also exits when it dequeues this object.
_SENTINEL = object()


def _open_new(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


class JsonlStageObserver(StageObserverPort):
    """Thread-safe bounded queue drained by one real, non-daemon writer thread.

    ``max_events`` is the queue capacity (burst bound between producer threads
    and the writer); the total written volume is bounded by ``max_bytes``.
    """

    def __init__(
        self, directory: Path, *, max_events: int, max_bytes: int,
        flush_interval_seconds: float = 1.0, monotonic_ns: Any = time.monotonic_ns,
    ) -> None:
        if type(max_events) is not int or not 1 <= max_events <= 10_000_000:
            raise ValueError("stage observation queue capacity must be in 1..10000000")
        if type(max_bytes) is not int or not 4096 <= max_bytes <= 2**31:
            raise ValueError("stage observation byte bound must be in 4096..2^31")
        if type(flush_interval_seconds) not in (int, float) or not 0 < flush_interval_seconds <= 60:
            raise ValueError("stage observation flush interval is invalid")
        self._directory = directory
        self._max_events, self._max_bytes = max_events, max_bytes
        self._flush_interval = float(flush_interval_seconds)
        self._clock = monotonic_ns
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_events)
        self._lock = threading.Lock()
        self._counts = {"events_written": 0, "bytes_written": 0, "dropped": 0, "late_notes": 0,
                        "note_errors": 0, "guard_failures": 0, "writer_errors": 0, "truncated": 0,
                        "join_timeout": 0, "summary_write_error": 0}
        self._failure_types: list[str] = []
        self._writer_failed = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._started_ns = int(monotonic_ns())
        self._fd = _open_new(directory / _EVENTS_FILENAME)
        self._thread = threading.Thread(target=self._run, name="stage-observation-writer", daemon=False)
        self._thread.start()

    # -- StageObserverPort ------------------------------------------------------

    def note(self, record: StageNote) -> None:
        try:
            if self._closed:
                self._bump("late_notes")
                return
            if type(record) is not StageNote:
                raise TypeError("stage observer requires an exact StageNote")
            payload = {"attempt_id": record.attempt_id, "lane": record.lane, "kind": record.kind,
                       "monotonic_ns": record.monotonic_ns, "scalars": dict(record.scalars)}
            self._queue.put_nowait(_encode(payload))
        except queue.Full:
            self._bump("dropped")
        except Exception as exc:  # noqa: BLE001 - measurement failure is counted, never raised
            self._bump("note_errors", type(exc).__name__)

    def record_failure(self, error: BaseException) -> None:
        self._bump("guard_failures", type(error).__name__)

    # -- state -----------------------------------------------------------------

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed.is_set()

    @property
    def closed(self) -> bool:
        return self._closed

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def _bump(self, name: str, failure_type: str | None = None) -> None:
        with self._lock:
            self._counts[name] += 1
            if failure_type is not None and failure_type not in self._failure_types and len(self._failure_types) < _MAX_FAILURE_TYPES:
                self._failure_types.append(failure_type)

    # -- writer thread -----------------------------------------------------------

    def _run(self) -> None:
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._flush_interval)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    item = None
                if item is _SENTINEL:
                    break
                if item is not None:
                    self._write(item)
                if time.monotonic() - last_flush >= self._flush_interval:
                    os.fsync(self._fd)
                    last_flush = time.monotonic()
                if self._stop.is_set() and self._queue.empty():
                    break
        except BaseException as exc:  # noqa: BLE001 - the thread must record why it died
            self._bump("writer_errors", type(exc).__name__)
            self._writer_failed.set()
            raise
        finally:
            # The writer owns the descriptor: flush and close it on every exit.
            try:
                os.fsync(self._fd)
            except OSError as exc:
                self._bump("writer_errors", type(exc).__name__)
                self._writer_failed.set()
            try:
                os.close(self._fd)
            except OSError as exc:
                self._bump("writer_errors", type(exc).__name__)
                self._writer_failed.set()

    def _write(self, line: bytes) -> None:
        with self._lock:
            if self._counts["bytes_written"] + len(line) > self._max_bytes:
                self._counts["truncated"] += 1
                self._writer_failed.set()
                return
        view = memoryview(line)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        with self._lock:
            self._counts["events_written"] += 1
            self._counts["bytes_written"] += len(line)

    # -- closure ---------------------------------------------------------------

    def close(self, *, join_timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Refuse new notes, drain accepted ones through the real writer, seal the summary.

        Never raises for measurement I/O problems; each is counted and the
        status becomes ``partial`` or ``invalid``. A second close is a caller error.
        """

        if self._closed:
            raise RuntimeError("stage observer is already closed")
        self._closed = True
        closed_ns = int(self._clock())
        try:
            self._queue.put(_encode({"attempt_id": None, "lane": None, "kind": "observation_closed",
                                     "monotonic_ns": closed_ns, "scalars": {}}), timeout=join_timeout_seconds)
        except queue.Full:
            self._bump("dropped")
        # The stop signal never depends on queue space.
        self._stop.set()
        self._thread.join(timeout=join_timeout_seconds)
        if self._thread.is_alive():
            self._bump("join_timeout")
            self._writer_failed.set()
        counts = self.counts()
        if counts["writer_errors"] or counts["join_timeout"]:
            status = "invalid"
        elif (counts["dropped"] or counts["truncated"] or counts["note_errors"]
              or counts["guard_failures"] or counts["late_notes"]):
            status = "partial"
        else:
            status = "complete"
        summary: dict[str, Any] = {
            "contract_version": SUMMARY_CONTRACT_VERSION, "measurement_status": status,
            **counts, "queue_capacity": self._max_events, "max_bytes": self._max_bytes,
            "started_monotonic_ns": self._started_ns, "closed_monotonic_ns": closed_ns,
            "failure_types": list(self._failure_types), "events_file": _EVENTS_FILENAME,
            "writer_thread_alive": self._thread.is_alive(),
        }
        try:
            fd = _open_new(self._directory / _SUMMARY_FILENAME)
            try:
                os.write(fd, json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            self._bump("summary_write_error", type(exc).__name__)
            summary["measurement_status"] = "invalid"
            summary["summary_write_error"] = self.counts()["summary_write_error"]
            summary["failure_types"] = list(self._failure_types)
        return summary


class ProgressRecorder:
    """Write coordinator snapshots on change or heartbeat from the calling thread.

    Write failures are sticky counters; nothing here raises into the
    coordinator or publication callbacks that own the calling thread.
    """

    def __init__(self, directory: Path, *, heartbeat_seconds: float = 5.0, max_lines: int = 100_000,
                 monotonic: Any = time.monotonic) -> None:
        if type(heartbeat_seconds) not in (int, float) or not 0 < heartbeat_seconds <= 3600:
            raise ValueError("progress heartbeat must be in (0, 3600] seconds")
        if type(max_lines) is not int or max_lines < 1:
            raise ValueError("progress line bound must be positive")
        self._fd = _open_new(directory / _PROGRESS_FILENAME)
        self._heartbeat, self._max_lines, self._monotonic = float(heartbeat_seconds), max_lines, monotonic
        self._lock = threading.Lock()
        self._last: CoordinatorSnapshot | None = None
        self._last_written = float("-inf")
        self._lines = 0
        self._dropped = 0
        self._write_errors = 0
        self._failure_types: list[str] = []
        self._failed = False
        self._closed = False

    @property
    def failed(self) -> bool:
        return self._failed

    def record(self, snapshot: CoordinatorSnapshot) -> None:
        try:
            now = float(self._monotonic())
            with self._lock:
                if snapshot == self._last and now - self._last_written < self._heartbeat:
                    return
                self._last, self._last_written = snapshot, now
                payload = {"kind": "snapshot", "monotonic_ns": time.monotonic_ns(), **asdict(snapshot)}
                payload["credits_in_use"] = snapshot.credits_in_use.nonzero()
                payload["credits_limit"] = snapshot.credits_limit.nonzero()
                payload["queued"] = dict(snapshot.queued)
                payload["in_flight"] = dict(snapshot.in_flight)
                payload["credit_blocked_by_lane"] = {lane: list(dimensions) for lane, dimensions in snapshot.credit_blocked_by_lane}
                self._append(payload)
        except Exception as exc:  # noqa: BLE001 - never into the coordinator thread
            self._record_error(exc)

    def prune_signal(self, replaced: bool) -> None:
        try:
            with self._lock:
                self._append({"kind": "prune_signal", "monotonic_ns": time.monotonic_ns(), "replaced": bool(replaced),
                              "note": "idempotent pruning generation signal; not a publication fact"})
        except Exception as exc:  # noqa: BLE001 - never into the publication callback
            self._record_error(exc)

    def _record_error(self, exc: BaseException) -> None:
        with self._lock:
            self._write_errors += 1
            self._failed = True
            name = type(exc).__name__
            if name not in self._failure_types and len(self._failure_types) < _MAX_FAILURE_TYPES:
                self._failure_types.append(name)

    def _append(self, payload: dict[str, Any]) -> None:
        if self._closed or self._failed:
            self._dropped += 1
            return
        if self._lines >= self._max_lines:
            self._dropped += 1
            return
        line = _encode(payload)
        view = memoryview(line)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        self._lines += 1

    def close(self) -> dict[str, Any]:
        """Flush and release the file; I/O failures are counted, never raised."""

        with self._lock:
            if not self._closed:
                self._closed = True
                for operation in (os.fsync, os.close):
                    try:
                        operation(self._fd)
                    except OSError as exc:
                        self._write_errors += 1
                        self._failed = True
                        name = type(exc).__name__
                        if name not in self._failure_types and len(self._failure_types) < _MAX_FAILURE_TYPES:
                            self._failure_types.append(name)
            # A dropped snapshot is missing evidence, never a complete record.
            if self._failed:
                status = "invalid"
            elif self._dropped:
                status = "partial"
            else:
                status = "complete"
            return {"progress_lines": self._lines, "progress_dropped": self._dropped,
                    "progress_write_errors": self._write_errors,
                    "progress_status": status,
                    "progress_failure_types": list(self._failure_types)}


__all__ = ["JsonlStageObserver", "ProgressRecorder", "SUMMARY_CONTRACT_VERSION"]
