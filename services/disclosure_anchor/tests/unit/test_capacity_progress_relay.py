from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.runtime.capacity_progress_relay import (
    ProgressRelayResume,
    encode_capacity_progress_jsonl,
    read_progress_relay_checkpoint,
    write_progress_relay_checkpoint,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    BlockedProgressEvent,
    DurablePageCommitEvent,
)
from disclosure_anchor.application.services.capacity_progress_replay import replay_capacity_progress


HASH = "sha256:" + "a" * 64
PROFILE = "sha256:" + "b" * 64
CLOCK = "sha256:" + "c" * 64
RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _commit(sequence: int, source: str, pages: int, cumulative: int) -> DurablePageCommitEvent:
    return DurablePageCommitEvent(
        run_id=RUN,
        sequence=sequence,
        process_epoch_sha256=HASH,
        process_profile_sha256=PROFILE,
        clock_domain_identity_sha256=CLOCK,
        observed_at_utc=NOW,
        monotonic_ns=sequence + 10,
        source_identity_sha256=source,
        committed_source_pages=pages,
        cumulative_unique_source_pages=cumulative,
        commit_latency_ns=1,
    )


class CapacityProgressRelayTests(unittest.TestCase):
    def test_content_free_replay_and_restart_state(self) -> None:
        events = (
            BlockedProgressEvent(
                run_id=RUN,
                sequence=0,
                process_epoch_sha256=HASH,
                process_profile_sha256=PROFILE,
                clock_domain_identity_sha256=CLOCK,
                observed_at_utc=NOW,
                monotonic_ns=5,
                blocked_reason="gpu_input_starved",
                blocked_interval_started_monotonic_ns=2,
                blocked_duration_ns=3,
            ),
            _commit(1, "sha256:" + "d" * 64, 7, 7),
        )
        payload, resume = encode_capacity_progress_jsonl(
            events, runtime_bundle_identity_sha256=HASH
        )
        self.assertNotIn(b"document", payload)
        self.assertNotIn(b"security", payload)
        self.assertEqual(resume.next_sequence, 2)
        self.assertEqual(replay_capacity_progress(events).durable_unique_pages, 7)
        continuation = _commit(2, "sha256:" + "e" * 64, 3, 10)
        _, resumed = encode_capacity_progress_jsonl(
            (continuation,), runtime_bundle_identity_sha256=HASH, resume=resume
        )
        self.assertEqual(resumed.cumulative_unique_source_pages, 10)

    def test_gap_overlap_duplicate_and_unproven_restart_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "sequence"):
            replay_capacity_progress(
                (
                    _commit(1, "sha256:" + "d" * 64, 1, 1),
                    _commit(3, "sha256:" + "e" * 64, 1, 2),
                )
            )
        with self.assertRaisesRegex(ValueError, "restart continuity"):
            encode_capacity_progress_jsonl(
                (_commit(1, "sha256:" + "d" * 64, 1, 1),),
                runtime_bundle_identity_sha256=HASH,
                resume=None,
            )
        wrong = ProgressRelayResume(
            run_id=RUN,
            process_epoch_sha256=HASH,
            runtime_bundle_identity_sha256=HASH,
            process_profile_sha256=PROFILE,
            clock_domain_identity_sha256=CLOCK,
            next_sequence=3,
            cumulative_unique_source_pages=1,
            durable_sources=(("sha256:" + "d" * 64, PROFILE, 1),),
        )
        with self.assertRaisesRegex(ValueError, "continue exactly"):
            encode_capacity_progress_jsonl(
                (_commit(1, "sha256:" + "d" * 64, 1, 1),),
                runtime_bundle_identity_sha256=HASH,
                resume=wrong,
            )

    def test_private_checkpoint_round_trip_and_file_contract(self) -> None:
        resume = ProgressRelayResume(
            run_id=RUN,
            process_epoch_sha256=HASH,
            runtime_bundle_identity_sha256=HASH,
            process_profile_sha256=PROFILE,
            clock_domain_identity_sha256=CLOCK,
            next_sequence=0,
            cumulative_unique_source_pages=0,
            durable_sources=(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "resume.json"
            expected = write_progress_relay_checkpoint(checkpoint, resume)
            self.assertEqual(read_progress_relay_checkpoint(checkpoint), resume)
            self.assertEqual(oct(checkpoint.stat().st_mode & 0o777), "0o600")
            self.assertTrue(expected.startswith("sha256:"))
            os.chmod(checkpoint, 0o644)
            with self.assertRaisesRegex(ValueError, "private single-link"):
                read_progress_relay_checkpoint(checkpoint)

    def test_checkpoint_rejects_duplicate_and_noncanonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "resume.json"
            checkpoint.write_bytes(b'{"run_id":"a","run_id":"b"}')
            checkpoint.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                read_progress_relay_checkpoint(checkpoint)


if __name__ == "__main__":
    unittest.main()
