from __future__ import annotations

import unittest

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.capacity_progress_relay import (
    ProgressRelayResume,
    encode_anchored_progress_relay_head,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    EncodedProgressRelayCheckpoint,
    decode_progress_relay_resume,
)

HASH = "sha256:" + "a" * 64
RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class PublishEvidenceLedgerContractTests(unittest.TestCase):
    def _resume(self, **changes: object) -> ProgressRelayResume:
        values: dict[str, object] = {
            "run_id": RUN,
            "process_epoch_sha256": HASH,
            "runtime_bundle_identity_sha256": HASH,
            "process_profile_sha256": HASH,
            "clock_domain_identity_sha256": HASH,
            "next_sequence": 1,
            "cumulative_unique_source_pages": 5,
            "durable_sources": ((HASH, HASH, 5),),
        }
        values.update(changes)
        return ProgressRelayResume.model_validate(values)

    def test_head_binds_stream_bytes_hash_and_predecessor(self) -> None:
        resume = self._resume()
        relay_id = f"{RUN}:{HASH}"
        head = encode_anchored_progress_relay_head(
            relay_id=relay_id, row_version=0, resume=resume
        )
        self.assertEqual(head.checkpoint_byte_count, len(head.checkpoint_bytes))
        payload = head.model_dump()
        payload["checkpoint_bytes"] = b"{}"
        with self.assertRaises(ValidationError):
            EncodedProgressRelayCheckpoint.model_validate(payload)
        with self.assertRaises((ValidationError, ValueError)):
            decode_progress_relay_resume(b"{}")

    def test_resume_rejects_duplicate_unsorted_and_sum_mismatch(self) -> None:
        other = "sha256:" + "b" * 64
        invalid = (
            {"durable_sources": ((HASH, HASH, 5), (HASH, HASH, 5)), "cumulative_unique_source_pages": 10},
            {"durable_sources": ((other, HASH, 1), (HASH, HASH, 5)), "cumulative_unique_source_pages": 6},
            {"cumulative_unique_source_pages": 6},
        )
        for changes in invalid:
            with self.assertRaises(ValidationError):
                self._resume(**changes)

    def test_noncanonical_run_or_wrong_stream_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._resume(run_id="not-a-uuid")
        with self.assertRaises(ValidationError):
            encode_anchored_progress_relay_head(
                relay_id=f"{RUN}:{'sha256:' + 'b' * 64}",
                row_version=0,
                resume=self._resume(),
            )

    def test_resume_byte_budget_matches_persisted_head_limit(self) -> None:
        def sources(count: int) -> tuple[tuple[str, str, int], ...]:
            return tuple(
                (f"sha256:{index:064x}", HASH, 1) for index in range(count)
            )

        just_under = self._resume(
            durable_sources=sources(6800),
            cumulative_unique_source_pages=6800,
        )
        encode_anchored_progress_relay_head(
            relay_id=f"{RUN}:{HASH}", row_version=0, resume=just_under
        )
        with self.assertRaises(ValidationError):
            self._resume(
                durable_sources=sources(7000),
                cumulative_unique_source_pages=7000,
            )


if __name__ == "__main__":
    unittest.main()
