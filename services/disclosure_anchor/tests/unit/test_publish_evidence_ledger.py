from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.capacity_progress_relay import (
    ProgressRelayResume,
    encode_anchored_progress_relay_head,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    EncodedProgressRelayCheckpoint,
    DurablePublishSupplementEvidence,
    decode_progress_relay_resume,
)
from disclosure_anchor.application.services.full_host_hour_kpi import reconcile_private_publish_ledger_rows

HASH = "sha256:" + "a" * 64
RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class PublishEvidenceLedgerContractTests(unittest.TestCase):
    def test_explicit_v2_v3_are_preserved_and_mixed_versions_conflict(self) -> None:
        moment = datetime(2026, 9, 7, tzinfo=timezone.utc)
        values = dict(
            supplement_id="pes_" + "0" * 26, processing_run_id="fixture-run",
            source_identity_sha256=HASH, source_page_count=5, publish_precommit_at=moment,
            host_assignment_identity_sha256=HASH, boot_identity_sha256=HASH,
            runtime_bundle_identity_sha256=HASH, process_profile_sha256=HASH,
            observer_run_id=RUN, observer_receipt_sha256=HASH, observer_seal_sha256=HASH,
            publish_durable_observed_at=moment,
        )
        rows = []
        for version in (2, 3):
            contract = f"mineru.synchronized-telemetry-receipt.v{version}"
            supplement = DurablePublishSupplementEvidence(**values, observer_contract_version=contract)
            self.assertEqual(supplement.observer_contract_version, contract)
            row = {**supplement.model_dump(), "source_page_variants": 1,
                   "supplement_source_identity_sha256": HASH, "supplement_source_page_count": 5,
                   "supplement_publish_precommit_at": moment}
            self.assertEqual(reconcile_private_publish_ledger_rows([row])[0].status, "complete")
            rows.append(row)
        self.assertEqual(reconcile_private_publish_ledger_rows(rows)[0].status, "conflict")
        for version in (1, 4):
            with self.assertRaises(ValidationError):
                DurablePublishSupplementEvidence(**values, observer_contract_version=f"mineru.synchronized-telemetry-receipt.v{version}")
            invalid = {**rows[0], "observer_contract_version": f"mineru.synchronized-telemetry-receipt.v{version}"}
            self.assertEqual(reconcile_private_publish_ledger_rows([invalid])[0].status, "conflict")

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
