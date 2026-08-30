from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from disclosure_anchor.adapters.runtime.mineru_process_profile import (
    load_mineru_process_profile,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    PROCESS_PROFILE_CONTRACT,
)


def _profile() -> MineruProcessProfile:
    hashes = tuple("sha256:" + character * 64 for character in "123456")
    return MineruProcessProfile(
        contract_version=PROCESS_PROFILE_CONTRACT,
        runtime_bundle_identity_sha256=hashes[0],
        orchestrator_image_identity_sha256=hashes[1],
        inference_image_identity_sha256=hashes[2],
        model_snapshot_identity_sha256=hashes[3],
        host_runtime_identity_sha256=hashes[4],
        vllm_engine_args_sha256=hashes[5],
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


class MineruProcessProfileLoaderTests(unittest.TestCase):
    def test_load_preserves_exact_bytes_and_expected_hash(self) -> None:
        profile = _profile()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "profile.json"
            path.write_bytes(profile.exact_bytes)
            loaded = load_mineru_process_profile(
                path,
                expected_sha256=profile.sha256,
                expected_owner_uid=os.getuid(),
            )
        self.assertEqual(loaded.profile, profile)
        self.assertEqual(loaded.exact_bytes, profile.exact_bytes)
        self.assertEqual(loaded.sha256, profile.sha256)

    def test_hash_mismatch_and_noncanonical_bytes_fail_closed(self) -> None:
        profile = _profile()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "profile.json"
            path.write_bytes(profile.exact_bytes)
            with self.assertRaisesRegex(ValueError, "differs"):
                load_mineru_process_profile(
                    path,
                    expected_sha256=replace(profile, api_task_slots=1).sha256,
                    expected_owner_uid=os.getuid(),
                )
            path.write_bytes(profile.exact_bytes.replace(b'":', b'": ', 1))
            with self.assertRaisesRegex(ValueError, "not canonical"):
                load_mineru_process_profile(
                    path,
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )

    def test_symlink_and_oversized_file_fail_closed(self) -> None:
        profile = _profile()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target.json"
            target.write_bytes(profile.exact_bytes)
            link = root / "profile.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "opened safely"):
                load_mineru_process_profile(
                    link,
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )
            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (64 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "closed envelope"):
                load_mineru_process_profile(
                    oversized,
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )

    def test_same_size_in_place_change_during_read_fails_closed(self) -> None:
        initial = _profile()
        final = replace(initial, api_task_slots=3, raster_stage_slots=3)
        self.assertEqual(len(initial.exact_bytes), len(final.exact_bytes))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "profile.json"
            path.write_bytes(initial.exact_bytes)
            real_read = os.read
            changed = False

            def mutate_after_read(descriptor: int, count: int) -> bytes:
                nonlocal changed
                result = real_read(descriptor, count)
                if not changed:
                    changed = True
                    path.write_bytes(final.exact_bytes)
                return result

            with mock.patch("os.read", side_effect=mutate_after_read):
                with self.assertRaisesRegex(ValueError, "changed while"):
                    load_mineru_process_profile(
                        path,
                        expected_sha256=initial.sha256,
                        expected_owner_uid=os.getuid(),
                    )

    def test_parent_symlink_hardlink_and_writable_mode_fail_closed(self) -> None:
        profile = _profile()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real_parent = root / "real"
            real_parent.mkdir()
            profile_path = real_parent / "profile.json"
            profile_path.write_bytes(profile.exact_bytes)
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "traversed safely"):
                load_mineru_process_profile(
                    linked_parent / "profile.json",
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )

            hardlink = real_parent / "hardlink.json"
            hardlink.hardlink_to(profile_path)
            with self.assertRaisesRegex(ValueError, "one hard link"):
                load_mineru_process_profile(
                    profile_path,
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )
            hardlink.unlink()
            profile_path.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "writable"):
                load_mineru_process_profile(
                    profile_path,
                    expected_sha256=profile.sha256,
                    expected_owner_uid=os.getuid(),
                )


if __name__ == "__main__":
    unittest.main()
