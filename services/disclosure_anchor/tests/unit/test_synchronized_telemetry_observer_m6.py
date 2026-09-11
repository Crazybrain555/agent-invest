"""M6 receipt clock-divergence diagnostics for the synchronized telemetry observer.

``tests/unit/test_synchronized_telemetry_observer.py`` is retained at its
baseline content.  This module independently covers the M6 addition: when the
selected receipt model rejects a run because the wall and monotonic clocks
diverged, the original validation error is preserved and gains exactly one
bounded, content-free diagnostic note computed from the selected model's own
limits.  The failure stays a failure; nothing is sealed.  The divergence root
cause is not diagnosed here.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    ObserverState,
    SynchronizedTelemetryEvidenceError,
    run_synchronized_telemetry_observer,
)
from tests.unit.test_synchronized_telemetry_observer import (
    HASH_A,
    HASH_B,
    HASH_C,
    HASH_D,
    _collector_spec,
    _profile,
)

NOTE_PREFIX = "synchronized telemetry receipt clock diagnostics: "
NOTE_FIELDS = (
    "receipt_model",
    "started_at_utc",
    "finished_at_utc",
    "started_monotonic_ns",
    "finished_monotonic_ns",
    "start_clock_bracket_ns",
    "finish_clock_bracket_ns",
    "wall_elapsed_ns",
    "monotonic_elapsed_ns",
    "clock_divergence_ns",
    "maximum_clock_divergence_fixed_ns",
    "maximum_clock_divergence_ppm",
    "maximum_clock_divergence_ns",
)
INTEGER_FIELDS = NOTE_FIELDS[3:]
RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class JumpingMainThreadClock:
    """Real UTC on lane threads; the observer thread's wall clock jumps after the start pair."""

    def __init__(self, *, jump_after_call: int = 3, jump: timedelta = timedelta(milliseconds=100)) -> None:
        self.jump_after_call = jump_after_call
        self.jump = jump
        self.main_calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        if threading.current_thread() is not threading.main_thread():
            return datetime.now(UTC)
        with self._lock:
            self.main_calls += 1
            call = self.main_calls
        observed = datetime.now(UTC)
        if call > self.jump_after_call:
            observed += self.jump
        return observed


def parse_clock_diagnostic_note(note: str) -> dict[str, str]:
    if not note.startswith(NOTE_PREFIX):
        raise AssertionError(f"unexpected note prefix: {note[:80]!r}")
    fields: dict[str, str] = {}
    for part in note[len(NOTE_PREFIX):].split("; "):
        name, _separator, value = part.partition("=")
        if name in fields:
            raise AssertionError(f"duplicate diagnostic field {name!r}")
        fields[name] = value
    return fields


class ReceiptClockDivergenceDiagnosticsTests(unittest.TestCase):
    def run_observer(self, root: Path, clock: JumpingMainThreadClock, *, duration: float = 0.3) -> object:
        return run_synchronized_telemetry_observer(
            artifact_root=root,
            process_profile=_profile(),
            gpu_collector=_collector_spec(lane="gpu"),
            host_collector=_collector_spec(lane="host"),
            duration_seconds=duration,
            run_id=RUN_ID,
            process_cpu_ns=lambda: 0,
            utc_now=clock,
        )

    def test_receipt_clock_divergence_failure_carries_bounded_v2_diagnostics(self) -> None:
        clock = JumpingMainThreadClock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE") as raised:
                self.run_observer(root, clock)
            cause = raised.exception.__cause__
            self.assertIsInstance(cause, ValidationError)
            self.assertIn("wall and monotonic receipt clocks diverged", str(cause))
            notes = list(getattr(cause, "__notes__", []))
            self.assertEqual(len(notes), 1)
            fields = parse_clock_diagnostic_note(notes[0])
            self.assertEqual(tuple(fields), NOTE_FIELDS)
            self.assertEqual(fields["receipt_model"], "SynchronizedTelemetryReceiptV2")
            numbers = {name: int(fields[name]) for name in INTEGER_FIELDS}
            self.assertEqual(
                numbers["clock_divergence_ns"],
                abs(numbers["wall_elapsed_ns"] - numbers["monotonic_elapsed_ns"]),
            )
            self.assertGreater(numbers["clock_divergence_ns"], numbers["maximum_clock_divergence_ns"])
            self.assertGreaterEqual(numbers["clock_divergence_ns"], 80_000_000)
            self.assertEqual(numbers["maximum_clock_divergence_fixed_ns"], 50_000_000)
            self.assertEqual(numbers["maximum_clock_divergence_ppm"], 50)
            self.assertEqual(
                numbers["maximum_clock_divergence_ns"],
                50_000_000 + numbers["monotonic_elapsed_ns"] * 50 // 1_000_000,
            )
            self.assertEqual(
                numbers["monotonic_elapsed_ns"],
                numbers["finished_monotonic_ns"] - numbers["started_monotonic_ns"],
            )
            self.assertGreater(numbers["monotonic_elapsed_ns"], 250_000_000)
            self.assertLessEqual(numbers["start_clock_bracket_ns"], 10_000_000)
            self.assertLessEqual(numbers["finish_clock_bracket_ns"], 10_000_000)
            started = datetime.fromisoformat(fields["started_at_utc"])
            finished = datetime.fromisoformat(fields["finished_at_utc"])
            self.assertIsNotNone(started.tzinfo)
            self.assertIsNotNone(finished.tzinfo)
            self.assertGreater(finished, started)
            self.assertEqual(int((finished - started).total_seconds() * 1_000_000_000), numbers["wall_elapsed_ns"])
            for secret in (RUN_ID, str(root), HASH_A, HASH_B, HASH_C, HASH_D, "sha256"):
                self.assertNotIn(secret, notes[0])
            self.assertEqual(sorted(path.name for path in root.rglob("receipt*")), [])
            self.assertEqual(sorted(path.name for path in root.rglob("seal*")), [])

    def test_divergence_within_the_receipt_limit_is_recorded_not_diagnosed(self) -> None:
        clock = JumpingMainThreadClock(jump=timedelta(milliseconds=10))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            result = self.run_observer(root, clock)
            self.assertEqual(result.state, ObserverState.SEALED)  # type: ignore[attr-defined]
            observed = result.receipt.observed_clock_divergence_ns  # type: ignore[attr-defined]
            self.assertGreaterEqual(observed, 2_000_000)
            self.assertLessEqual(observed, 40_000_000)
            names = sorted(path.name for path in result.run_directory.iterdir())  # type: ignore[attr-defined]
            self.assertEqual(len(names), 3)
            for prefix in ("frames", "receipt", "seal"):
                self.assertEqual(sum(name.startswith(prefix) for name in names), 1, names)

    def test_non_divergence_failures_gain_no_clock_diagnostic_note(self) -> None:
        with self.subTest(failure="clock callable raises"):

            def broken_clock() -> datetime:
                raise RuntimeError("clock unavailable")

            with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
                SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"
            ) as raised:
                self.run_observer(Path(temporary) / "telemetry", broken_clock)  # type: ignore[arg-type]
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertEqual(list(getattr(raised.exception.__cause__, "__notes__", [])), [])

        with self.subTest(failure="naive wall clock"):

            def naive_clock() -> datetime:
                return datetime.now()

            with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
                SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"
            ) as raised:
                self.run_observer(Path(temporary) / "telemetry", naive_clock)  # type: ignore[arg-type]
            self.assertNotIsInstance(raised.exception.__cause__, ValidationError)
            self.assertEqual(list(getattr(raised.exception.__cause__, "__notes__", [])), [])


if __name__ == "__main__":
    unittest.main()
