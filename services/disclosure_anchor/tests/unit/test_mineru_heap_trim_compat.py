"""Exact-source regressions for the MinerU 3.4.4 heap-return image patch."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_phase_trace import (
    PHASE_TRACE_PREFIX,
    parse_phase_trace_line,
    summarize_complete_phase_trace,
    validate_complete_phase_trace,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    parse_phase_trace_capture,
    summarize_phase_trace_capture,
)
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    BASE_IMAGE_DIGEST,
    TARGET_PREIMAGE_SHA256,
    patch_source,
)


_VLM_GUARDS = '''@contextmanager
def predictor_execution_guard(predictor: MinerUClient):
    lock = getattr(predictor, "_mineru_execution_lock", None)
    if lock is None:
        yield
        return
    with lock:
        yield


@asynccontextmanager
async def aio_predictor_execution_guard(predictor: MinerUClient):
    lock = getattr(predictor, "_mineru_execution_lock", None)
    if lock is None:
        yield
        return
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()
'''


_RUNTIME_COMPATIBILITY_SHA256 = "sha256:" + "9" * 64


def _candidate_profile_json() -> str:
    return json.dumps(
        {
            "inner_inference_concurrency": 7,
            "max_document_pages": 10000,
            "max_resident_pages": 16,
            "max_source_pdf_bytes": 1024 * 1024 * 1024,
            "min_document_pages": 9,
            "pipeline_depth": 1,
            "profile_id": "rtx5080-w8-d1-c7-s128-v1",
            "schema": "mineru-execution-profile.v2",
            "vllm_max_num_seqs": 128,
            "window_size": 8,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _capacity_catalog_environment(root: Path, raw_profile: str) -> dict[str, str]:
    profile = json.loads(raw_profile)
    profile_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    catalog = {
        "commissioning_evaluator_sha256": "sha256:" + "7" * 64,
        "commissioning_receipt_sha256": "sha256:" + "8" * 64,
        "profile_id": profile["profile_id"],
        "profile_sha256": profile_sha256,
        "runtime_compatibility_sha256": _RUNTIME_COMPATIBILITY_SHA256,
        "schema": "mineru-capacity-catalog.v1",
    }
    encoded = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    path = root / "capacity-catalog.v1.json"
    path.write_bytes(encoded)
    return {
        "MINERU_CAPACITY_CATALOG_PATH": str(path),
        "MINERU_CAPACITY_CATALOG_SHA256": (
            "sha256:" + hashlib.sha256(encoded).hexdigest()
        ),
        "MINERU_CAPACITY_RUNTIME_COMPATIBILITY_SHA256": (
            _RUNTIME_COMPATIBILITY_SHA256
        ),
    }


def _vlm_document_fixture(*, asynchronous: bool) -> str:
    render = (
        "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
        "                    pdf_bytes,\n"
        "                    start_page_id=window_start,\n"
        "                    end_page_id=window_end,\n"
        "                    image_type=ImageType.PIL,\n"
        "                )\n"
        if asynchronous
        else
        "                images_list = load_images_from_pdf_doc(\n"
        "                    pdf_doc,\n"
        "                    start_page_id=window_start,\n"
        "                    end_page_id=window_end,\n"
        "                    image_type=ImageType.PIL,\n"
        "                    pdf_bytes=pdf_bytes,\n"
        "                )\n"
    )
    finalize = (
        "        if not client_side_output_generation:\n"
        "            await asyncio.to_thread(finalize_middle_json, middle_json[\"pdf_info\"])\n"
        if asynchronous
        else
        "        if not client_side_output_generation:\n"
        "            finalize_middle_json(middle_json[\"pdf_info\"])\n"
    )
    guard = (
        "                    async with aio_predictor_execution_guard(predictor):\n"
        "                        pass\n"
        if asynchronous
        else
        "                    with predictor_execution_guard(predictor):\n"
        "                        pass\n"
    )
    return (
        "    results = []\n    doc_closed = False\n    try:\n"
        "        logger.info(\n"
        "            f'VLM processing-window run. page_count={page_count}, '\n"
        "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
        "        )\n\n"
        "        infer_start = time.time()\n"
        "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
        "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
        + render
        + "                try:\n"
        + guard
        + "                    append_page_blocks_to_middle_json(\n"
        "                        middle_json,\n"
        "                        progress_bar=progress_bar,\n"
        "                    )\n"
        "                    last_append_end_time = time.time()\n"
        "                finally:\n"
        "                    _close_images(images_list)\n"
        + finalize
        + "        close_pdfium_document(pdf_doc)\n"
        "        doc_closed = True\n        return middle_json, results\n"
        "    finally:\n"
        "        if not doc_closed:\n"
        "            close_pdfium_document(pdf_doc)\n"
    )


def _hybrid_document_fixture(*, asynchronous: bool) -> str:
    render = (
        "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
        "                    pdf_bytes,\n"
        "                    start_page_id=window_start,\n"
        "                    end_page_id=window_end,\n"
        "                    image_type=ImageType.PIL,\n"
        "                )\n"
        if asynchronous
        else
        "                images_list = load_images_from_pdf_doc(\n"
        "                    pdf_doc,\n"
        "                    start_page_id=window_start,\n"
        "                    end_page_id=window_end,\n"
        "                    image_type=ImageType.PIL,\n"
        "                    pdf_bytes=pdf_bytes,\n"
        "                )\n"
    )
    layout = (
        "                    images_layout_res, hybrid_pipeline_model = await asyncio.to_thread(\n"
        if asynchronous
        else
        "                    images_layout_res, hybrid_pipeline_model = _predict_layout_for_window(\n"
    )
    finalize = (
        "        if client_side_output_generation:\n"
        "            await asyncio.to_thread(\n"
        if asynchronous
        else
        "        if client_side_output_generation:\n"
        "            apply_server_side_postprocess(\n"
    )
    work = (
        "                    if effort == \"medium\":\n"
        "                        async with aio_predictor_execution_guard(predictor):\n"
        "                            pass\n"
        "                        optimize_hybrid_formula_number_blocks(window_model_list)\n"
        "                        if _ocr_enable:\n"
        "                            await asyncio.to_thread(\n"
        "                                _apply_vlm_ocr_det_sidecars_for_window,\n"
        "                            )\n"
        "                        else:\n"
        "                            window_model_list = await asyncio.to_thread(\n"
        "                                _process_ocr_and_formulas,\n"
        "                            )\n"
        "                    elif effort == \"high\":\n"
        "                        if _ocr_enable:\n"
        "                            async with aio_predictor_execution_guard(predictor):\n"
        "                                pass\n"
        "                            await asyncio.to_thread(\n"
        "                                _apply_vlm_ocr_det_sidecars_for_window,\n"
        "                            )\n"
        "                        else:\n"
        "                            async with aio_predictor_execution_guard(predictor):\n"
        "                                pass\n"
        "                            window_model_list = await asyncio.to_thread(\n"
        "                                _process_ocr_and_formulas,\n"
        "                            )\n"
        "                    await asyncio.to_thread(\n"
        "                        _apply_layout_title_split,\n"
        "                        window_model_list,\n"
        "                        images_layout_res,\n"
        "                        page_sizes,\n"
        "                    )\n"
        "                    model_list.extend(window_model_list)\n"
        if asynchronous
        else
        "                    if effort == \"medium\":\n"
        "                        with predictor_execution_guard(predictor):\n"
        "                            pass\n"
        "                        optimize_hybrid_formula_number_blocks(window_model_list)\n"
        "                        if _ocr_enable:\n"
        "                            _apply_vlm_ocr_det_sidecars_for_window(\n"
        "                            )\n"
        "                        else:\n"
        "                            window_model_list = _process_ocr_and_formulas(\n"
        "                            )\n"
        "                    elif effort == \"high\":\n"
        "                        if _ocr_enable:\n"
        "                            with predictor_execution_guard(predictor):\n"
        "                                pass\n"
        "                            _apply_vlm_ocr_det_sidecars_for_window(\n"
        "                            )\n"
        "                        else:\n"
        "                            with predictor_execution_guard(predictor):\n"
        "                                pass\n"
        "                            window_model_list = _process_ocr_and_formulas(\n"
        "                            )\n"
        "                    _apply_layout_title_split(\n"
        "                        window_model_list,\n"
        "                        images_layout_res,\n"
        "                        page_sizes,\n"
        "                    )\n"
        "                    model_list.extend(window_model_list)\n"
    )
    return (
        "    model_list = []\n"
        "    doc_closed = False\n"
        "    hybrid_pipeline_model = None\n"
        "        configured_window_size = get_processing_window_size(default=64)\n"
        "        effective_window_size = min(page_count, configured_window_size) if page_count else 0\n"
        "        logger.info(\n"
        "            f'Hybrid processing-window run. page_count={page_count}, '\n"
        "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
        "        )\n\n"
        "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n\n"
        "        infer_start = time.time()\n"
        "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
        "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
        + render
        + "                try:\n"
        + layout
        + "                        _ocr_enable,\n"
        "                    )\n"
        + work
        + "                    append_page_model_list_to_middle_json(\n"
        "                        middle_json,\n"
        "                        progress_bar=progress_bar,\n"
        "                    )\n"
        "                    last_append_end_time = time.time()\n"
        "                finally:\n"
        "                    _close_images(images_list)\n"
        + finalize
        + "        close_pdfium_document(pdf_doc)\n"
        "        doc_closed = True\n"
        "        clean_memory(device)\n"
        "        return middle_json, model_list\n"
        "    finally:\n"
        "        if not doc_closed:\n"
        "            close_pdfium_document(pdf_doc)\n"
    )


class MinerUHeapTrimCompatibilityTests(unittest.TestCase):
    def test_preimages_match_the_reproduced_deployed_344_sources(self) -> None:
        self.assertEqual(
            TARGET_PREIMAGE_SHA256,
            {
                "mineru/backend/vlm/vlm_analyze.py": (
                    "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
                ),
                "mineru/backend/hybrid/hybrid_analyze.py": (
                    "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
                ),
                "mineru/utils/model_utils.py": (
                    "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
                ),
            },
        )

    def test_model_utils_hook_is_explicit_guarded_and_fail_visible(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        patched = patch_source("mineru/utils/model_utils.py", source)

        self.assertIn("MINERU_MALLOC_TRIM must be explicitly configured", patched)
        self.assertIn("if not is_heap_trim_enabled():", patched)
        self.assertIn("raise RuntimeError(\"glibc malloc_trim is unavailable\")", patched)
        self.assertNotIn("except Exception", patched)
        with self.assertRaisesRegex(RuntimeError, "anchor count drifted"):
            patch_source("mineru/utils/model_utils.py", patched)

    def test_vlm_and_hybrid_trim_every_window_and_document(self) -> None:
        vlm = (
            "from ...utils.config_reader import get_device, get_processing_window_size\n\n"
            "from ...utils.enum_class import ImageType\n"
            + _VLM_GUARDS
            + _vlm_document_fixture(asynchronous=False)
            + _vlm_document_fixture(asynchronous=True)
        )
        hybrid = (
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n"
            + _hybrid_document_fixture(asynchronous=False)
            + "async def aio_doc_analyze(\n"
            + _hybrid_document_fixture(asynchronous=True)
        )

        patched_vlm = patch_source("mineru/backend/vlm/vlm_analyze.py", vlm)
        patched_hybrid = patch_source(
            "mineru/backend/hybrid/hybrid_analyze.py", hybrid
        )

        self.assertEqual(patched_vlm.count("trim_process_heap()"), 4)
        self.assertEqual(patched_hybrid.count("trim_process_heap()"), 6)
        self.assertEqual(patched_vlm.count('"window_vlm",'), 2)
        self.assertEqual(patched_hybrid.count('"window_layout",'), 4)
        self.assertEqual(patched_hybrid.count('"window_postprocess",'), 4)
        self.assertEqual(patched_hybrid.count('"window_total",'), 3)
        self.assertIn("except CapacityCandidateFallback:", patched_hybrid)
        self.assertIn('middle_json.get("pdf_info")', patched_hybrid)
        self.assertIn("allow_auto_fallback=allow_auto_fallback", patched_hybrid)
        capacity_helper = patched_hybrid[
            patched_hybrid.index("async def _aio_run_hybrid_capacity_pipeline(") :
            patched_hybrid.index("async def aio_doc_analyze(")
        ]
        self.assertLess(
            capacity_helper.index("fallback_gate.close_before_output()"),
            capacity_helper.index("model_list.extend(window_model_list)"),
        )
        self.assertIn(
            'credit_lease is None\n                    or (\n'
            '                        resources_released\n'
            '                        and credit_lease.state == "released"',
            capacity_helper,
        )

    def test_phase_trace_is_default_off_content_free_and_strictly_closed(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        disabled_output = io.StringIO()
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "0"}), redirect_stderr(
            disabled_output
        ):
            disabled = namespace["new_phase_trace"](
                backend="hybrid", page_count=2, window_size=16, total_windows=1
            )
            disabled.document_started()
            disabled.document_completed()
        self.assertEqual(disabled_output.getvalue(), "")

        output = io.StringIO()
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "1"}), redirect_stderr(
            output
        ):
            execution_profile = namespace["legacy_capacity_execution_profile"](16)
            trace = namespace["new_phase_trace"](
                backend="hybrid",
                page_count=2,
                window_size=16,
                total_windows=1,
                execution_profile=execution_profile,
                source_pdf_bytes=1234,
            )
            trace.document_started()
            window = trace.window(
                window_index=0, page_start=0, page_end_exclusive=2
            )
            window_started_ns = trace.start()
            for phase in (
                "window_render",
                "window_layout",
                "window_vlm",
                "window_postprocess",
                "window_append",
            ):
                started_ns = trace.start()
                trace.complete(
                    phase,
                    started_ns,
                    window=window,
                    append_index=0 if phase == "window_append" else None,
                )
            trace.complete("window_total", window_started_ns, window=window)
            finalize_started_ns = trace.start()
            trace.complete("document_finalize", finalize_started_ns)
            trace.document_completed()

        lines = output.getvalue().splitlines()
        self.assertTrue(all(line.startswith(PHASE_TRACE_PREFIX) for line in lines))
        self.assertNotIn("source_pdf_sha256", output.getvalue().lower())
        self.assertNotIn("pdf_path", output.getvalue().lower())
        self.assertNotIn("document_id", output.getvalue().lower())
        events = tuple(parse_phase_trace_line(line) for line in lines)
        self.assertEqual(
            validate_complete_phase_trace(
                events,
                expected_profile_sha256=execution_profile.profile_sha256,
            ),
            events,
        )
        with self.assertRaisesRegex(ValueError, "runtime attestation"):
            validate_complete_phase_trace(
                events,
                expected_profile_sha256="sha256:" + "f" * 64,
            )

        payload = json.loads(lines[0].removeprefix(PHASE_TRACE_PREFIX))
        payload["source_pdf_sha256"] = "sha256:" + "a" * 64
        with self.assertRaisesRegex(ValueError, "fields drifted"):
            parse_phase_trace_line(
                PHASE_TRACE_PREFIX
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "sometimes"}):
            with self.assertRaisesRegex(RuntimeError, "invalid value"):
                namespace["new_phase_trace"](
                    backend="hybrid",
                    page_count=2,
                    window_size=16,
                    total_windows=1,
                )

    def test_auto_capacity_pipeline_is_ordered_overlapped_and_two_resident(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )
        with patch.dict(os.environ, {"MINERU_CAPACITY_MODE": "legacy"}):
            self.assertFalse(namespace["capacity_pipeline_enabled"]())
            self.assertEqual(namespace["capacity_active_window_size"](16), 16)
        with patch.dict(
            os.environ,
            {
                "MINERU_CAPACITY_MODE": "candidate",
                "MINERU_CAPACITY_PROFILE_JSON": _candidate_profile_json(),
            },
        ):
            self.assertTrue(namespace["capacity_pipeline_enabled"]())
            self.assertEqual(namespace["capacity_active_window_size"](16), 8)

        async def exercise() -> tuple[list[str], int, int, list[int]]:
            timeline: list[str] = []
            resident = 0
            max_resident = 0
            committed: list[int] = []

            async def prepare(item: int) -> dict[str, object]:
                nonlocal resident, max_resident
                timeline.append(f"prepare-start-{item}")
                resident += 1
                max_resident = max(max_resident, resident)
                await asyncio.sleep(0.002)
                timeline.append(f"prepare-end-{item}")
                return {"item": item, "released": False}

            async def infer(
                prepared: dict[str, object], inference_started: asyncio.Event
            ) -> int:
                item = int(prepared["item"])
                timeline.append(f"infer-start-{item}")
                inference_started.set()
                await asyncio.sleep(0.008)
                timeline.append(f"infer-end-{item}")
                return item

            async def release(prepared: dict[str, object]) -> None:
                nonlocal resident
                if prepared["released"]:
                    return
                prepared["released"] = True
                resident -= 1

            async def commit(prepared: dict[str, object], result: int) -> None:
                timeline.append(f"commit-start-{result}")
                await asyncio.sleep(0.004)
                committed.append(result)
                await release(prepared)
                timeline.append(f"commit-end-{result}")

            await namespace["run_bounded_ordered_pipeline"](
                range(3),
                prepare=prepare,
                infer=infer,
                commit=commit,
                release=release,
            )
            return timeline, resident, max_resident, committed

        timeline, resident, max_resident, committed = asyncio.run(exercise())
        self.assertEqual(resident, 0)
        self.assertEqual(max_resident, 2)
        self.assertEqual(committed, [0, 1, 2])
        self.assertLess(
            timeline.index("prepare-start-1"), timeline.index("infer-end-0")
        )
        self.assertLess(
            timeline.index("infer-start-1"), timeline.index("commit-end-0")
        )

    def test_depth_one_phase_trace_proves_order_and_both_overlaps(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        output = io.StringIO()

        async def exercise() -> None:
            profile = namespace["CapacityExecutionProfile"](
                profile_id="candidate-w2-d1",
                profile_sha256="sha256:" + "a" * 64,
                pipeline_mode="depth1",
                pipeline_depth=1,
                window_size=2,
                max_resident_pages=4,
                max_source_pdf_bytes=10000,
                min_document_pages=3,
                max_document_pages=100,
                inner_inference_concurrency=2,
                vllm_max_num_seqs=8,
            )
            bank = namespace["CapacityCreditBank"](profile)
            trace = namespace["new_phase_trace"](
                backend="hybrid",
                page_count=6,
                window_size=2,
                total_windows=3,
                execution_profile=profile,
                source_pdf_bytes=1234,
            )
            trace.document_started()

            async def prepare(item: int) -> dict[str, object]:
                window = trace.window(
                    window_index=item,
                    page_start=item * 2,
                    page_end_exclusive=item * 2 + 2,
                )
                total_started_ns = trace.start()
                credit_wait_started_ns = trace.start()
                lease = await bank.acquire(2)
                trace.complete(
                    "window_credit_wait",
                    credit_wait_started_ns,
                    window=window,
                    credit_lease=lease,
                )
                render_started_ns = trace.start()
                await asyncio.sleep(0.002)
                trace.complete(
                    "window_render",
                    render_started_ns,
                    window=window,
                    credit_lease=lease,
                )
                bank.record_actual_decoded_bytes(lease, 1024)
                layout_started_ns = trace.start()
                await asyncio.sleep(0.001)
                trace.complete(
                    "window_layout",
                    layout_started_ns,
                    window=window,
                    credit_lease=lease,
                )
                return {
                    "item": item,
                    "lease": lease,
                    "released": False,
                    "ready_ns": trace.start(),
                    "total_started_ns": total_started_ns,
                    "window": window,
                }

            async def infer(
                prepared: dict[str, object], inference_started: asyncio.Event
            ) -> int:
                trace.complete(
                    "window_b_queue_wait",
                    prepared["ready_ns"],
                    window=prepared["window"],
                    credit_lease=prepared["lease"],
                )
                started_ns = trace.start()
                inference_started.set()
                await asyncio.sleep(0.008)
                trace.complete(
                    "window_vlm",
                    started_ns,
                    window=prepared["window"],
                    credit_lease=prepared["lease"],
                )
                return int(prepared["item"])

            async def release(prepared: dict[str, object]) -> None:
                if prepared["released"]:
                    return
                prepared["released"] = True
                release_started_ns = trace.start()
                await bank.release(prepared["lease"])
                trace.complete(
                    "window_release",
                    release_started_ns,
                    window=prepared["window"],
                    credit_lease=prepared["lease"],
                )

            async def commit(prepared: dict[str, object], result: int) -> None:
                postprocess_started_ns = trace.start()
                await asyncio.sleep(0.004)
                trace.complete(
                    "window_postprocess",
                    postprocess_started_ns,
                    window=prepared["window"],
                    credit_lease=prepared["lease"],
                )
                append_started_ns = trace.start()
                trace.complete(
                    "window_append",
                    append_started_ns,
                    window=prepared["window"],
                    append_index=result,
                    credit_lease=prepared["lease"],
                )
                await release(prepared)
                trace.complete(
                    "window_total",
                    prepared["total_started_ns"],
                    window=prepared["window"],
                    credit_lease=prepared["lease"],
                )

            await namespace["run_bounded_ordered_pipeline"](
                range(3),
                prepare=prepare,
                infer=infer,
                commit=commit,
                release=release,
            )
            bank.assert_fully_released()
            finalize_started_ns = trace.start()
            trace.complete("document_finalize", finalize_started_ns)
            trace.document_completed()

        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "1"}), redirect_stderr(
            output
        ):
            asyncio.run(exercise())
        events = tuple(
            parse_phase_trace_line(line) for line in output.getvalue().splitlines()
        )
        self.assertEqual(
            validate_complete_phase_trace(
                events,
                expected_profile_sha256="sha256:" + "a" * 64,
                require_pipeline_overlap=True,
            ),
            events,
        )
        summary = summarize_complete_phase_trace(
            events,
            expected_profile_sha256="sha256:" + "a" * 64,
            require_pipeline_overlap=True,
        )
        self.assertEqual(summary["schema"], "mineru-phase-trace-summary.v1")
        self.assertEqual(summary["pipeline_mode"], "depth1")
        self.assertEqual(summary["page_count"], 6)
        self.assertGreater(summary["a_b_overlap_ns"], 0)
        self.assertGreater(summary["b_c_overlap_ns"], 0)
        self.assertGreater(summary["max_observed_resident_pages"], 0)
        self.assertGreater(summary["max_actual_decoded_bytes"], 0)

        layout_index = next(
            index
            for index, event in enumerate(events)
            if event.window_index == 0 and event.phase == "window_layout"
        )
        drifted = list(events)
        drifted[layout_index] = replace(
            drifted[layout_index],
            actual_decoded_bytes=2048,
        )
        with self.assertRaisesRegex(ValueError, "reconciliation drifted"):
            validate_complete_phase_trace(
                drifted,
                expected_profile_sha256="sha256:" + "a" * 64,
            )

        lines = output.getvalue().splitlines()
        trace_text = "".join(f"{line}\n" for line in lines)
        capture_payload = {
            "active_profile_sha256": "sha256:" + "a" * 64,
            "capacity_mode": "candidate",
            "collected_at_utc": "2026-08-27T01:10:00+00:00",
            "collector_path": (
                r"C:\ProgramData\agent-invest\mineru-runtime-v6"
                r"\collect_mineru_runtime.ps1"
            ),
            "collector_sha256": "sha256:" + "b" * 64,
            "container": {
                "health": "healthy",
                "id": "c" * 64,
                "image": "agent-invest/mineru-api:test",
                "image_id": "sha256:" + "d" * 64,
                "name": "mineru-api",
                "oom_killed": False,
                "restart_count": 0,
                "running": True,
                "started_at_utc": "2026-08-27T00:00:00+00:00",
                "status": "running",
            },
            "line_count": len(lines),
            "lines": lines,
            "schema": "mineru-phase-trace-capture.v1",
            "since_utc": "2026-08-27T01:00:00+00:00",
            "trace_bytes": len(trace_text.encode("utf-8")),
            "trace_lines_sha256": (
                "sha256:" + hashlib.sha256(trace_text.encode("utf-8")).hexdigest()
            ),
            "until_utc": "2026-08-27T01:09:00+00:00",
            "windows_node_identity_sha256": "sha256:" + "e" * 64,
        }
        capture = parse_phase_trace_capture(
            json.dumps(capture_payload, sort_keys=True, separators=(",", ":"))
        )
        capture_summary = summarize_phase_trace_capture(
            capture,
            expected_profile_sha256="sha256:" + "a" * 64,
            expected_capacity_mode="candidate",
            expected_collector_sha256="sha256:" + "b" * 64,
            expected_windows_node_identity_sha256="sha256:" + "e" * 64,
            expected_container_id="c" * 64,
            require_pipeline_overlap=True,
        )
        self.assertEqual(capture_summary["document_count"], 1)
        self.assertEqual(capture_summary["page_count"], 6)

    def test_pipeline_failure_before_b_start_releases_the_prepared_owner(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        async def exercise() -> tuple[int, list[int]]:
            resident = 0
            released: list[int] = []

            async def prepare(item: int) -> dict[str, object]:
                nonlocal resident
                resident += 1
                return {"item": item, "released": False}

            async def infer(
                _prepared: dict[str, object], _inference_started: asyncio.Event
            ) -> int:
                raise RuntimeError("B failed before owner acquisition")

            async def release(prepared: dict[str, object]) -> None:
                nonlocal resident
                if prepared["released"]:
                    return
                prepared["released"] = True
                resident -= 1
                released.append(int(prepared["item"]))

            async def commit(_prepared: dict[str, object], _result: int) -> None:
                raise AssertionError("commit must not run")

            with self.assertRaisesRegex(RuntimeError, "before owner acquisition"):
                await namespace["run_bounded_ordered_pipeline"](
                    range(2),
                    prepare=prepare,
                    infer=infer,
                    commit=commit,
                    release=release,
                )
            return resident, released

        resident, released = asyncio.run(exercise())
        self.assertEqual(resident, 0)
        self.assertEqual(released, [0])

    def test_execution_profile_selection_is_strict_immutable_and_fail_closed(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )
        select = namespace["select_capacity_execution_profile"]

        with patch.dict(os.environ, {"MINERU_CAPACITY_MODE": "legacy"}, clear=False):
            legacy = select(
                configured_window_size=16,
                page_count=100,
                source_pdf_bytes=1000,
            )
        self.assertEqual(legacy.pipeline_mode, "legacy")
        self.assertEqual(legacy.window_size, 16)
        self.assertRegex(legacy.profile_sha256, r"^sha256:[a-f0-9]{64}$")

        with patch.dict(
            os.environ,
            {
                "MINERU_CAPACITY_MODE": "candidate",
                "MINERU_CAPACITY_PROFILE_JSON": _candidate_profile_json(),
            },
            clear=False,
        ):
            candidate = select(
                configured_window_size=16,
                page_count=100,
                source_pdf_bytes=1000,
            )
        self.assertEqual(candidate.pipeline_mode, "depth1")
        self.assertEqual(candidate.max_resident_pages, 16)
        self.assertEqual(candidate.window_size, 8)
        with patch.dict(
            os.environ,
            {
                "MINERU_CAPACITY_MODE": "candidate",
                "MINERU_CAPACITY_PROFILE_JSON": _candidate_profile_json(),
            },
            clear=False,
        ):
            runtime_status = namespace["capacity_runtime_status"](16)
        self.assertEqual(runtime_status["schema"], "mineru-capacity-runtime.v1")
        self.assertTrue(runtime_status["nonlegacy_admission_enabled"])
        self.assertEqual(
            runtime_status["legacy_profile_sha256"], legacy.profile_sha256
        )
        self.assertEqual(
            runtime_status["candidate_profile"]["profile_sha256"],
            candidate.profile_sha256,
        )
        self.assertEqual(
            runtime_status["candidate_profile"]["max_resident_pages"], 16
        )

        with patch.dict(
            os.environ,
            {
                "MINERU_CAPACITY_MODE": "auto",
                "MINERU_CAPACITY_PROFILE_JSON": _candidate_profile_json(),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "catalog path"):
                select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )

        with tempfile.TemporaryDirectory() as tmp:
            raw_profile = _candidate_profile_json()
            auto_environment = {
                "MINERU_CAPACITY_MODE": "auto",
                "MINERU_CAPACITY_PROFILE_JSON": raw_profile,
                **_capacity_catalog_environment(Path(tmp), raw_profile),
            }
            with patch.dict(os.environ, auto_environment, clear=False):
                too_small = select(
                    configured_window_size=16,
                    page_count=8,
                    source_pdf_bytes=1000,
                )
                commissioned = select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )
        self.assertEqual(too_small.pipeline_mode, "legacy")
        self.assertEqual(commissioned.pipeline_mode, "depth1")
        self.assertNotEqual(commissioned.profile_sha256, legacy.profile_sha256)

        invalid_payload = json.loads(_candidate_profile_json())
        invalid_payload["max_resident_pages"] = 17
        with patch.dict(
            os.environ,
            {
                "MINERU_CAPACITY_MODE": "candidate",
                "MINERU_CAPACITY_PROFILE_JSON": json.dumps(invalid_payload),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "legacy owner envelope"):
                select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )

    def test_vector_credit_bank_is_atomic_bounded_and_closes(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        async def exercise() -> None:
            with patch.dict(
                os.environ,
                {
                    "MINERU_CAPACITY_MODE": "candidate",
                    "MINERU_CAPACITY_PROFILE_JSON": _candidate_profile_json(),
                },
                clear=False,
            ):
                profile = namespace["select_capacity_execution_profile"](
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )
            bank = namespace["CapacityCreditBank"](profile)
            first = await bank.acquire(8)
            second = await bank.acquire(8)
            self.assertEqual(second.resident_pages_after_acquire, 16)
            third_task = asyncio.create_task(bank.acquire(1))
            await asyncio.sleep(0.01)
            self.assertFalse(third_task.done())

            bank.record_actual_decoded_bytes(first, 1024)
            with self.assertRaisesRegex(RuntimeError, "exceed"):
                bank.record_actual_decoded_bytes(
                    second,
                    second.reserved_decoded_bytes + 1,
                )
            await bank.release(first)
            third = await third_task
            self.assertEqual(third.page_count, 1)
            await bank.release(second)
            await bank.release(third)
            bank.assert_fully_released()
            with self.assertRaisesRegex(RuntimeError, "release state"):
                await bank.release(first)

        asyncio.run(exercise())

    def test_auto_catalog_is_canonical_and_hash_bound(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )
        select = namespace["select_capacity_execution_profile"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_profile = _candidate_profile_json()
            base_environment = {
                "MINERU_CAPACITY_MODE": "auto",
                "MINERU_CAPACITY_PROFILE_JSON": raw_profile,
                **_capacity_catalog_environment(root, raw_profile),
            }
            with patch.dict(os.environ, base_environment, clear=False):
                profile = select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )
                status = namespace["capacity_runtime_status"](16)
            self.assertEqual(profile.pipeline_mode, "depth1")
            self.assertEqual(
                status["candidate_profile"]["auto_catalog_sha256"],
                base_environment["MINERU_CAPACITY_CATALOG_SHA256"],
            )

            mismatched_runtime = dict(base_environment)
            mismatched_runtime[
                "MINERU_CAPACITY_RUNTIME_COMPATIBILITY_SHA256"
            ] = "sha256:" + "6" * 64
            with (
                patch.dict(os.environ, mismatched_runtime, clear=False),
                self.assertRaisesRegex(RuntimeError, "identity drifted"),
            ):
                select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )

            catalog_path = Path(base_environment["MINERU_CAPACITY_CATALOG_PATH"])
            catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")
            with (
                patch.dict(os.environ, base_environment, clear=False),
                self.assertRaisesRegex(RuntimeError, "hash drifted"),
            ):
                select(
                    configured_window_size=16,
                    page_count=100,
                    source_pdf_bytes=1000,
                )

    def test_fallback_gate_closes_atomically_before_candidate_output(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )
        fallback_type = namespace["CapacityCandidateFallback"]
        gate = namespace["CapacityFallbackGate"](True)
        claimed = gate.claim("pre-append failure", RuntimeError("boom"))
        self.assertIsInstance(claimed, fallback_type)
        with self.assertRaises(fallback_type) as raised:
            gate.close_before_output()
        self.assertIs(raised.exception, claimed)

        closed_gate = namespace["CapacityFallbackGate"](True)
        closed_gate.close_before_output()
        self.assertFalse(closed_gate.is_open)
        self.assertIsNone(
            closed_gate.claim("too late", RuntimeError("after append boundary"))
        )

    def test_render_failure_releases_credit_before_whole_document_fallback(
        self,
    ) -> None:
        model_utils_source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        runtime: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", model_utils_source),
                "patched-model-utils.py",
                "exec",
            ),
            runtime,
        )
        hybrid_source = (
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n"
            + _hybrid_document_fixture(asynchronous=False)
            + "async def aio_doc_analyze(\n"
            + _hybrid_document_fixture(asynchronous=True)
        )
        patched_hybrid = patch_source(
            "mineru/backend/hybrid/hybrid_analyze.py",
            hybrid_source,
        )
        helper_start = patched_hybrid.index(
            "async def _aio_run_hybrid_capacity_pipeline("
        )
        helper_end = patched_hybrid.index(
            "async def aio_doc_analyze(", helper_start
        )

        acquired = []
        released = []
        render_states: list[str] = []
        closed_images: list[object] = []
        base_bank = runtime["CapacityCreditBank"]

        class RecordingCreditBank(base_bank):
            async def acquire(self, page_count):
                lease = await super().acquire(page_count)
                acquired.append(lease)
                return lease

            async def release(self, lease):
                await super().release(lease)
                released.append(lease)

        async def fail_render(*_args, **_kwargs):
            render_states.append(acquired[-1].state)
            raise RuntimeError("render failed after credit acquisition")

        class PhaseTraceStub:
            def start(self):
                return 1

            def window(self, **kwargs):
                return kwargs

            def complete(self, *_args, **_kwargs):
                return None

        class ImageTypeStub:
            PIL = object()

        helper_namespace = dict(runtime)
        helper_namespace.update(
            {
                "CapacityCreditBank": RecordingCreditBank,
                "ImageType": ImageTypeStub,
                "_close_images": closed_images.append,
                "aio_load_images_from_pdf_bytes_range": fail_render,
            }
        )
        exec(
            compile(
                patched_hybrid[helper_start:helper_end],
                "patched-hybrid-capacity-helper.py",
                "exec",
            ),
            helper_namespace,
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw_profile = _candidate_profile_json()
            with patch.dict(
                os.environ,
                {
                    "MINERU_CAPACITY_MODE": "auto",
                    "MINERU_CAPACITY_PROFILE_JSON": raw_profile,
                    **_capacity_catalog_environment(Path(tmp), raw_profile),
                },
                clear=False,
            ):
                profile = runtime["select_capacity_execution_profile"](
                    configured_window_size=16,
                    page_count=9,
                    source_pdf_bytes=1000,
                )

        async def exercise() -> None:
            fallback_type = runtime["CapacityCandidateFallback"]
            with self.assertRaises(fallback_type) as raised:
                await helper_namespace["_aio_run_hybrid_capacity_pipeline"](
                    pdf_bytes=b"pdf",
                    pdf_doc=object(),
                    image_writer=object(),
                    predictor=object(),
                    middle_json={},
                    page_count=9,
                    effective_window_size=8,
                    phase_trace=PhaseTraceStub(),
                    inline_formula_enable=False,
                    batch_ratio=1,
                    ocr_enable=False,
                    effort="medium",
                    effective_image_analysis=False,
                    native_owner=asyncio.Lock(),
                    execution_profile=profile,
                    allow_auto_fallback=True,
                )
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)

        asyncio.run(exercise())
        self.assertEqual(render_states, ["leased"])
        self.assertEqual(len(acquired), 1)
        self.assertEqual(released, acquired)
        self.assertEqual(acquired[0].state, "released")
        self.assertEqual(closed_images, [])

    def test_async_owned_factory_is_not_created_while_waiting_for_owner(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        async def exercise() -> int:
            native_owner = asyncio.Lock()
            await native_owner.acquire()
            created = 0

            async def operation() -> str:
                return "ok"

            def factory():
                nonlocal created
                created += 1
                return operation()

            task = asyncio.create_task(
                namespace["run_async_owned"](native_owner, factory)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            native_owner.release()
            return created

        self.assertEqual(asyncio.run(exercise()), 0)

    def test_owned_native_operation_drains_before_cleanup_and_next_owner(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                patch_source("mineru/utils/model_utils.py", source),
                "patched-model-utils.py",
                "exec",
            ),
            namespace,
        )

        native_started = threading.Event()
        native_finish = threading.Event()
        next_owner_entered = threading.Event()
        cleaned: list[str] = []

        def slow_native() -> str:
            native_started.set()
            if not native_finish.wait(timeout=2):
                raise RuntimeError("test native owner timed out")
            return "image-window"

        async def exercise() -> None:
            owner = asyncio.Lock()
            task = asyncio.create_task(
                namespace["run_native_owned"](
                    owner,
                    slow_native,
                    on_cancel_result=cleaned.append,
                )
            )
            while not native_started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            next_task = asyncio.create_task(
                namespace["run_native_owned"](
                    owner,
                    next_owner_entered.set,
                )
            )
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertFalse(next_owner_entered.is_set())
            self.assertEqual(cleaned, [])

            native_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(cleaned, ["image-window"])
            await next_task
            self.assertTrue(next_owner_entered.is_set())

        asyncio.run(exercise())

    def test_dockerfile_pins_base_and_enables_the_closed_policy(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "mineru_heap_trim_compat"
            / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(f"FROM mineru@{BASE_IMAGE_DIGEST}", dockerfile)
        self.assertIn("ENV MINERU_MALLOC_TRIM=1", dockerfile)
        self.assertIn("ENV MINERU_PHASE_TRACE=0", dockerfile)
        self.assertIn("ENV MINERU_CAPACITY_MODE=legacy", dockerfile)
        self.assertIn(
            'io.agent-invest.mineru.capacity-policy="bounded-two-window-capacity-pipeline.v2"',
            dockerfile,
        )
        self.assertIn("COMPAT_PATCHER_SHA256", dockerfile)
        self.assertIn("COMPAT_DOCKERFILE_SHA256", dockerfile)
        self.assertNotIn("latest", dockerfile.lower())

    def test_windows_installer_and_collector_bind_the_derived_image(self) -> None:
        root = Path(__file__).resolve().parents[2]
        installer = (
            root / "scripts" / "windows" / "install_mineru_fixed_api.ps1"
        ).read_text(encoding="utf-8")
        collector = (
            root / "scripts" / "windows" / "collect_mineru_runtime.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[Parameter(Mandatory = $true)][string]$CompatDockerfileSource", installer)
        self.assertIn("[Parameter(Mandatory = $true)][string]$CompatPatcherSource", installer)
        self.assertIn("[switch]$ReuseCurrentPublishedImage", installer)
        self.assertIn('[string]$CampaignApiCompatImageId = ""', installer)
        self.assertIn(
            "reuse mode requires one canonical campaign API compatibility image ID",
            installer,
        )
        self.assertIn("function Get-StableServiceEpochs", installer)
        self.assertIn("Get-Command docker.exe -CommandType Application", installer)
        self.assertIn("Get-Command docker.exe -CommandType Application", collector)
        helper_start = "# BEGIN MINERU NATIVE PROCESS V1"
        helper_end = "# END MINERU NATIVE PROCESS V1"
        installer_helper = installer[
            installer.index(helper_start) : installer.index(helper_end) + len(helper_end)
        ]
        collector_helper = collector[
            collector.index(helper_start) : collector.index(helper_end) + len(helper_end)
        ]
        self.assertEqual(installer_helper, collector_helper)
        self.assertIn("function Invoke-NativeProcess", installer_helper)
        self.assertIn("Diagnostics.ProcessStartInfo", installer_helper)
        self.assertIn("$process.ExitCode", installer_helper)
        self.assertIn("$process.StandardOutput.ReadToEndAsync()", installer_helper)
        self.assertIn("$process.StandardError.ReadToEndAsync()", installer_helper)
        self.assertLess(
            installer_helper.index("$process.StandardOutput.ReadToEndAsync()"),
            installer_helper.index("$process.WaitForExit()"),
        )
        self.assertLess(
            installer_helper.index("$process.StandardError.ReadToEndAsync()"),
            installer_helper.index("$process.WaitForExit()"),
        )
        self.assertIn("$process.StandardInput.BaseStream.Write", installer_helper)
        self.assertIn("[Text.Encoding]::UTF8.GetBytes($StandardInput)", installer_helper)
        self.assertIn("# Cleanup is best-effort; preserve the original process error.", installer_helper)
        self.assertIn("throw $originalError", installer_helper)
        self.assertIn("ConvertTo-WindowsCommandLineArgument", installer_helper)
        self.assertIn("function Assert-NativeProcessArguments", installer_helper)
        self.assertIn("$null -eq $argument", installer_helper)
        self.assertIn("$argument.GetType() -ne [string]", installer_helper)
        self.assertNotIn("[string[]]$Arguments", installer_helper)
        native_signature = installer_helper[
            installer_helper.index("function Invoke-NativeProcess") :
            installer_helper.index("$startInfo = New-Object Diagnostics.ProcessStartInfo")
        ]
        docker_process_signature = installer_helper[
            installer_helper.index("function Invoke-DockerProcess") :
            installer_helper.index("$result = Invoke-NativeProcess")
        ]
        docker_signature = installer_helper[
            installer_helper.index("function Invoke-Docker {") :
            installer_helper.index("$result = Invoke-DockerProcess -Arguments $Arguments")
        ]
        self.assertIn(
            "[AllowEmptyCollection()][AllowEmptyString()][object[]]$Arguments",
            native_signature,
        )
        self.assertNotIn("[AllowEmptyCollection()]", docker_process_signature)
        self.assertIn(
            "[AllowEmptyString()][object[]]$Arguments", docker_process_signature
        )
        self.assertNotIn("[AllowEmptyCollection()]", docker_signature)
        self.assertIn("[AllowEmptyString()][object[]]$Arguments", docker_signature)
        self.assertLess(
            installer_helper.index(
                "Assert-NativeProcessArguments",
                installer_helper.index("function Invoke-NativeProcess"),
            ),
            installer_helper.index("$startInfo = New-Object"),
        )
        self.assertLess(
            installer_helper.index(
                "Assert-NativeProcessArguments",
                installer_helper.index("function Invoke-DockerProcess"),
            ),
            installer_helper.index("$result = Invoke-NativeProcess"),
        )
        self.assertLess(
            installer_helper.index(
                "Assert-NativeProcessArguments",
                installer_helper.index("function Invoke-Docker {"),
            ),
            installer_helper.index("$result = Invoke-DockerProcess"),
        )
        self.assertNotIn(".ArgumentList", installer_helper)
        self.assertNotIn("Start-Process", installer_helper)
        self.assertNotIn("$LASTEXITCODE", installer)
        self.assertNotIn("$LASTEXITCODE", collector)
        self.assertNotIn("& $DockerCommand", installer)
        self.assertNotIn("& $DockerCommand", collector)
        self.assertNotIn("& docker ", installer)
        self.assertNotIn("& docker ", collector)
        self.assertIn("function Assert-StableServiceEpochs", installer)
        self.assertIn("Get-ValidatedPublishedApiCompatImage", installer)
        self.assertIn('"build", "--pull=false", "--provenance=false"', installer)
        self.assertIn('"--tag", $ApiCompatBuildTag', installer)
        self.assertIn("$OldApiCompatImageId = Get-OptionalImageId", installer)
        self.assertIn(
            ") -AllowedExitCodes @(0, 1)",
            installer,
        )
        self.assertIn("No such image: .+$", installer)
        self.assertIn(
            "cannot inspect optional Docker image reference $($Reference):",
            installer,
        )
        self.assertIn(") -AllowedExitCodes @(42)", installer)
        self.assertIn(") -AllowedExitCodes @(42)", collector)
        self.assertIn("-StandardInput $probeCode", collector)
        self.assertIn("-StandardInput $profileProbeCode", collector)
        self.assertIn("-StandardInput $compatProbeCode", collector)
        self.assertIn(
            "MinerU phase trace drifted from the exact stderr stream",
            collector,
        )
        self.assertIn(
            "ConvertFrom-NativeProcessText -Value $logResult.StandardError",
            collector,
        )
        self.assertIn("function Restore-ApiCompatTag", installer)
        self.assertIn('"tag", $OldApiCompatImageId, $ApiCompatImage', installer)
        self.assertIn("Remove-CompatBuildTag", installer)
        self.assertIn(
            '"up", "--detach", "--no-build", "--no-deps", "--force-recreate",',
            installer,
        )
        self.assertIn(
            "stable MinerU service $name $field changed during API-only deployment",
            installer,
        )
        self.assertIn("campaign image tag drifted during API-only rollback", installer)
        reuse_selection = (
            "if ($ReuseCurrentPublishedImage) {\n"
            "        $compatImage = Get-ValidatedPublishedApiCompatImage\n"
            "        Get-ValidatedRuntime | Out-Null\n"
            "        Assert-StableServiceEpochs -Expected $StableServiceEpochs\n"
            "    }\n"
            "    else {\n"
            "        $OldApiCompatImageId = Get-OptionalImageId -Reference $ApiCompatImage\n"
            "        $compatImage = Build-ValidatedApiCompatImage\n"
            "    }"
        )
        self.assertIn(reuse_selection, installer)
        self.assertLess(
            installer.index("$MutationStarted = $true"),
            installer.index('"tag", $ExpectedApiCompatImageId, $ApiCompatImage'),
        )
        self.assertIn('schema = "mineru-windows-install-receipt.v2"', installer)
        self.assertIn("mineru-runtime-v6", installer)
        self.assertNotIn("versioned v4 evidence paths", installer)
        self.assertIn('schema = "mineru-windows-runtime-observation.v3"', collector)
        self.assertIn('schema = "mineru-phase-trace-capture.v1"', collector)
        self.assertIn("$traceLines.Count -gt $MaxTraceLines", collector)
        self.assertIn("$traceByteCount -gt $MaxTraceBytes", collector)
        self.assertIn("active_profile_sha256", collector)
        self.assertIn("mineru-capacity-v1/compatibility.json", collector)
        self.assertIn("actual_source_sha256", collector)
        self.assertIn("heap_trim_enabled", collector)


if __name__ == "__main__":
    unittest.main()
