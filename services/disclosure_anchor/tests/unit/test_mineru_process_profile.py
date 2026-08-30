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
    identity = "sha256:" + "1" * 64
    return MineruProcessProfile(
        contract_version=PROCESS_PROFILE_CONTRACT,
        runtime_bundle_identity_sha256=identity,
        orchestrator_image_identity_sha256="sha256:" + "2" * 64,
        inference_image_identity_sha256="sha256:" + "3" * 64,
        model_snapshot_identity_sha256="sha256:" + "4" * 64,
        host_runtime_identity_sha256="sha256:" + "5" * 64,
        vllm_engine_args_sha256="sha256:" + "6" * 64,
        api_task_slots=2,
        api_max_pending_tasks=4,
        registry_nonterminal_cap=8,
        registry_terminal_cap=64,
        processing_window_size=32,
        raster_stage_slots=4,
        layout_stage_slots=4,
        postprocess_stage_slots=4,
        native_owner_slots=2,
        cpu_worker_threads=16,
        omp_thread_count=8,
        requested_hybrid_batch_ratio=4,
        effective_hybrid_batch_ratio=4,
        hybrid_ocr_override=False,
        inference_concurrency=7,
        vllm_max_num_seqs=128,
        vllm_max_model_len=8192,
        vllm_max_num_batched_tokens=32768,
        vllm_gpu_memory_utilization_millionths=900_000,
        vllm_tensor_parallel_size=1,
        vllm_pipeline_parallel_size=1,
        vllm_mm_processor_cache_bytes=0,
        vllm_enforce_eager=False,
        vllm_enable_prefix_caching=False,
        pipeline_inference_locks=True,
        finalizer_slots=2,
        result_reservation_bytes=256 * 1024 * 1024,
        max_unacked_result_bytes=2 * 1024 * 1024 * 1024,
        source_pdf_bytes_limit=2 * 1024 * 1024 * 1024,
        resident_pages_limit=256,
        rasterized_page_bytes_limit=4 * 1024 * 1024 * 1024,
        decoded_payload_bytes_limit=4 * 1024 * 1024 * 1024,
        cpu_working_set_bytes_limit=24 * 1024 * 1024 * 1024,
        gpu_allocated_bytes_limit=14 * 1024 * 1024 * 1024,
        gpu_request_slots=16,
        reorder_buffer_bytes_limit=4 * 1024 * 1024 * 1024,
        terminal_output_bytes_limit=4 * 1024 * 1024 * 1024,
        temporary_disk_bytes_limit=64 * 1024 * 1024 * 1024,
        db_staged_bytes_limit=8 * 1024 * 1024 * 1024,
        unpublished_pages_limit=4096,
        container_memory_limit_bytes=32 * 1024 * 1024 * 1024,
        host_runtime_memory_limit_bytes=48 * 1024 * 1024 * 1024,
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
            {"api_max_pending_tasks": 9},
            {"inference_concurrency": 129},
            {"gpu_request_slots": 129},
            {"finalizer_slots": 5},
            {"result_reservation_bytes": 600 * 1024 * 1024},
            {"task_cleanup_interval_seconds": 601},
            {"vllm_gpu_memory_utilization_millionths": 1_000_001},
            {"container_memory_limit_bytes": 49 * 1024 * 1024 * 1024},
            {"cpu_working_set_bytes_limit": 33 * 1024 * 1024 * 1024},
            {"resident_pages_limit": 1 << 31},
            {"temporary_disk_bytes_limit": 1 << 63},
            {"vllm_mm_processor_cache_bytes": -1},
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

    def test_all_execution_surfaces_are_part_of_identity(self) -> None:
        profile = _profile()
        changes = (
            {"runtime_bundle_identity_sha256": "sha256:" + "a" * 64},
            {"registry_nonterminal_cap": 9},
            {"raster_stage_slots": 5},
            {"omp_thread_count": 9},
            {"vllm_engine_args_sha256": "sha256:" + "b" * 64},
            {"vllm_max_num_batched_tokens": 65536},
            {"decoded_payload_bytes_limit": 5 * 1024 * 1024 * 1024},
            {"unpublished_pages_limit": 8192},
            {"host_runtime_memory_limit_bytes": 49 * 1024 * 1024 * 1024},
        )
        for update in changes:
            with self.subTest(update=update):
                self.assertNotEqual(replace(profile, **update).sha256, profile.sha256)

    def test_hash_and_boolean_fields_fail_closed(self) -> None:
        profile = _profile()
        invalid = (
            {"runtime_bundle_identity_sha256": "1" * 64},
            {"inference_image_identity_sha256": "sha256:" + "A" * 64},
            {"vllm_enforce_eager": 1},
            {"vllm_enable_prefix_caching": 0},
            {"pipeline_inference_locks": 1},
        )
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                replace(profile, **update)


if __name__ == "__main__":
    unittest.main()
