from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    PROCESS_PROFILE_CONTRACT,
    decode_mineru_process_profile,
    encode_mineru_process_profile,
)


def _profile() -> MineruProcessProfile:
    return MineruProcessProfile(
        contract_version=PROCESS_PROFILE_CONTRACT,
        api_task_slots=2,
        api_max_pending_tasks=4,
        processing_window_size=32,
        requested_hybrid_batch_ratio=4,
        effective_hybrid_batch_ratio=4,
        hybrid_ocr_override=False,
        inference_concurrency=7,
        vllm_max_num_seqs=128,
        vllm_max_model_len=8192,
        pipeline_inference_locks=True,
        finalizer_slots=2,
        result_reservation_bytes=256 * 1024 * 1024,
        max_unacked_result_bytes=2 * 1024 * 1024 * 1024,
        task_retention_seconds=600,
        task_cleanup_interval_seconds=30,
    )


class MineruProcessProfileTests(unittest.TestCase):
    def test_nonserial_profile_roundtrips_with_stable_identity(self) -> None:
        profile = _profile()
        encoded = encode_mineru_process_profile(profile)
        self.assertEqual(decode_mineru_process_profile(encoded), profile)
        self.assertEqual(len(profile.sha256), 71)
        self.assertTrue(profile.sha256.startswith("sha256:"))

    def test_contract_is_closed_and_canonical(self) -> None:
        encoded = _profile().exact_bytes
        with self.assertRaisesRegex(ValueError, "strict UTF-8 JSON"):
            decode_mineru_process_profile(
                encoded[:-1] + b',"api_task_slots":2}'
            )
        with self.assertRaisesRegex(ValueError, "not canonical"):
            decode_mineru_process_profile(encoded.replace(b'":', b'": ', 1))
        with self.assertRaisesRegex(ValueError, "fields are not closed"):
            decode_mineru_process_profile(encoded[:-1] + b',"legacy_serial":true}')

    def test_integer_and_relation_constraints_fail_closed(self) -> None:
        profile = _profile()
        invalid = (
            {"api_task_slots": True},
            {"api_task_slots": 5},
            {"inference_concurrency": 129},
            {"finalizer_slots": 5},
            {"result_reservation_bytes": 3 * 1024 * 1024 * 1024},
            {"task_cleanup_interval_seconds": 601},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(profile, **changes)

    def test_hybrid_requested_effective_and_ocr_override_are_exact(self) -> None:
        profile = _profile()
        with self.assertRaisesRegex(ValueError, "drifted"):
            replace(profile, effective_hybrid_batch_ratio=2)
        with self.assertRaisesRegex(ValueError, "requires effective batch ratio 1"):
            replace(profile, hybrid_ocr_override=True)
        overridden = replace(
            profile,
            hybrid_ocr_override=True,
            effective_hybrid_batch_ratio=1,
        )
        self.assertEqual(overridden.effective_hybrid_batch_ratio, 1)


if __name__ == "__main__":
    unittest.main()
