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

_HYBRID_COORDINATOR_FIXTURE = '''def _apply_medium_table_orientation_labels():
    try:
        rotate_labels = table_orientation_cls_model.batch_predict(
            table_inputs,
            det_batch_size=max(1, batch_ratio * OCR_DET_BASE_BATCH_SIZE),
            tqdm_enable=True,
        )
    except Exception:
        return None


def get_batch_ratio(device):
    return 1


def _close_images(images_list):
    return None
'''


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


def _http_client_fixture() -> str:
    return '''import asyncio


class HTTPMethod(str, Enum):
    pass


class HttpVlmClient:
    async def _aio_client(self):
        return self.client

    async def aio_predict(self, image, prompt="", sampling_params=None, priority=None):
        image, image_format = image, "png"
        request_body = {}
        if self.debug:
            pass
        client = await self._aio_client()
        response = await client.post(self.chat_url, json=request_body)
        response_data = self.get_response_data(response)
        if self.debug:
            pass
        return self.get_response_content(response_data)
'''


def _cross_page_fixture() -> str:
    return '''def _apply_merge_results(results, tasks, responses):
    if len(tasks) != len(responses):
        logger.warning(
            "Task/response count mismatch: {} tasks but {} responses, skipping merge results",
            len(tasks), len(responses),
        )
        return
    for task, response in zip(tasks, responses):
        pass


def detect_cross_page_cell_merge(results, batch_predict_fn):
    tasks = [object()]
    prompts = [t.prompt for t in tasks]
    try:
        responses = batch_predict_fn(prompts)
    except Exception as e:
        logger.warning("VLM batch predict failed for cross-page table merge: {}", e)
        return

    _apply_merge_results(results, tasks, responses)


async def aio_detect_cross_page_cell_merge(results, aio_batch_predict_fn):
    tasks = [object()]
    prompts = [t.prompt for t in tasks]
    try:
        responses = await aio_batch_predict_fn(prompts)
    except Exception as e:
        logger.warning("VLM batch predict failed for cross-page table merge: {}", e)
        return

    _apply_merge_results(results, tasks, responses)
'''


def _fast_api_fixture() -> str:
    return '''_configured_max_concurrent_requests = 1


def get_max_concurrent_requests() -> int:
    return _configured_max_concurrent_requests


def get_task_retention_seconds() -> int:
    return 0


class AsyncTaskManager:
    def __init__(self, fastapi_app: FastAPI):
        self.app = fastapi_app
        self.tasks = {}
        self.task_events = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.dispatcher_task = None
        self.cleanup_task = None
        self.active_tasks = set()
        self.last_worker_error = None
        self.is_shutting_down = False
        self.task_retention_seconds = get_task_retention_seconds()
        self.task_cleanup_interval_seconds = get_task_cleanup_interval_seconds()
        self.manager_wakeup = asyncio.Event()
        self._next_submit_order = 1

    async def start(self):
        self.is_shutting_down = False
        self.dispatcher_task = asyncio.create_task(self._dispatcher_loop())

    async def shutdown(self) -> None:
        self.is_shutting_down = True
        self._wake_waiters()
        if self.dispatcher_task is not None:
            self.dispatcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.dispatcher_task
            self.dispatcher_task = None
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
            self.cleanup_task = None

        pending = list(self.active_tasks)
        for processor in pending:
            processor.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.active_tasks.clear()

    async def submit(self, task: AsyncParseTask) -> None:
        task.submit_order = self._next_submit_order
        self._next_submit_order += 1
        self.tasks[task.task_id] = task
        self.task_events[task.task_id] = asyncio.Event()
        await self.queue.put(task.task_id)

    def _wake_waiters(self):
        self.manager_wakeup.set()

    def _signal_task_event(self, task_id):
        self.task_events[task_id].set()

    async def _dispatcher_loop(self) -> None:
        try:
            while True:
                task_id = await self.queue.get()
                processor = asyncio.create_task(
                    self._process_task(task_id),
                    name=f"mineru-fastapi-task-{task_id}",
                )
                self.active_tasks.add(processor)
                processor.add_done_callback(self._on_processor_done)
                self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_worker_error = str(exc)
            self._wake_waiters()
            logger.exception("Async task dispatcher crashed")
            raise

    def _on_processor_done(self, processor):
        self.active_tasks.discard(processor)

    async def _process_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return

        try:
            await self._run_task(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.status = TASK_FAILED
            task.error = str(exc)
            task.completed_at = utc_now_iso()
            self._signal_task_event(task_id)
            logger.exception(f"Async task failed: {task_id}")

    async def _run_task(self, task: AsyncParseTask) -> None:
        task.status = TASK_PROCESSING
        await task.release.wait()
        task.status = TASK_COMPLETED
        self._signal_task_event(task.task_id)


def health_payload(task_manager):
    return {
        "max_concurrent_requests": get_max_concurrent_requests(),
        "processing_window_size": get_processing_window_size(
            default=16
        ),
    }
'''
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
    def test_final_post_limiter_is_process_shared_drift_closed_and_cancel_safe(self) -> None:
        patched = patch_source(
            "mineru_vl_utils/vlm_client/http_client.py",
            _http_client_fixture(),
        )
        start = patched.index("class _ProcessAsyncRequestLimiter:")
        end = patched.index("class HTTPMethod", start)
        namespace: dict[str, object] = {"asyncio": asyncio}
        exec(compile(patched[start:end], "http-limiter.py", "exec"), namespace)

        async def exercise() -> None:
            limiter = namespace["_process_async_request_limiter"](2)
            self.assertIs(limiter, namespace["_process_async_request_limiter"](2))
            with self.assertRaisesRegex(RuntimeError, "drifted"):
                namespace["_process_async_request_limiter"](3)
            outer = asyncio.Semaphore(2)
            self.assertIsNot(outer, limiter.semaphore)
            release = asyncio.Event()

            async def hold() -> None:
                async with limiter:
                    await release.wait()

            holders = [asyncio.create_task(hold()) for _ in range(2)]
            while limiter.active != 2:
                await asyncio.sleep(0)
            waiter = asyncio.create_task(hold())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(limiter.active, 2)
            self.assertEqual(limiter.peak, 2)
            release.set()
            await asyncio.gather(*holders)
            self.assertEqual(limiter.active, 0)
            self.assertEqual(limiter.semaphore._value, 2)

        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "2"}):
            asyncio.run(exercise())
        self.assertIn("async with limiter:", patched)
        self.assertNotIn("async with semaphore:\n            response = await client.post", patched)

    def test_process_credit_pool_is_shared_across_documents_and_stage_gates_split(self) -> None:
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
            first = namespace["CapacityCreditBank"](profile)
            second = namespace["CapacityCreditBank"](profile)
            first_lease = await first.acquire(2)
            second_lease = await second.acquire(2)
            self.assertIs(first.pool, second.pool)
            self.assertEqual(second_lease.resident_windows_after_acquire, 2)
            self.assertEqual(second_lease.resident_pages_after_acquire, 4)
            blocked = asyncio.create_task(first.acquire(1))
            await asyncio.sleep(0)
            self.assertFalse(blocked.done())
            await first.release(first_lease)
            third_lease = await blocked
            await first.release(third_lease)
            await second.release(second_lease)
            first.assert_fully_released()
            second.assert_fully_released()
            a_gate, c_gate = namespace["process_capacity_stage_gates"]()
            same_a, same_c = namespace["process_capacity_stage_gates"]()
            self.assertIs(a_gate, same_a)
            self.assertIs(c_gate, same_c)
            self.assertIsNot(a_gate, c_gate)
            await a_gate.acquire()
            same_stage = asyncio.create_task(same_a.acquire())
            other_stage = asyncio.create_task(c_gate.acquire())
            await asyncio.sleep(0)
            self.assertFalse(same_stage.done())
            self.assertTrue(other_stage.done())
            c_gate.release()
            a_gate.release()
            await same_stage
            same_a.release()

        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "2"}):
            asyncio.run(exercise())

    def test_cross_page_transport_and_cardinality_fail_visible(self) -> None:
        patched = patch_source(
            "mineru_vl_utils/post_process/cross_page_table.py",
            _cross_page_fixture(),
        )
        self.assertNotIn("VLM batch predict failed", patched)
        self.assertIn("responses = batch_predict_fn(prompts)", patched)
        self.assertIn("responses = await aio_batch_predict_fn(prompts)", patched)
        start = patched.index("def _apply_merge_results")
        end = patched.index("def detect_cross_page_cell_merge", start)
        namespace: dict[str, object] = {}
        exec(compile(patched[start:end], "cross-page.py", "exec"), namespace)
        with self.assertRaisesRegex(RuntimeError, "count mismatch"):
            namespace["_apply_merge_results"]([], [object()], [])

    def test_hybrid_batch_ratio_is_strict_and_orientation_uses_model_gate(self) -> None:
        source = (
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n"
            + _HYBRID_COORDINATOR_FIXTURE
            + _hybrid_document_fixture(asynchronous=False)
            + "async def aio_doc_analyze(\n"
            + _hybrid_document_fixture(asynchronous=True)
        )
        patched = patch_source("mineru/backend/hybrid/hybrid_analyze.py", source)
        self.assertIn("MINERU_HYBRID_BATCH_RATIO must be explicitly configured", patched)
        self.assertIn('normalized not in {"1", "2", "4", "8"}', patched)
        self.assertIn("rotate_labels = run_ocr_inference(", patched)
        self.assertIn("hybrid_batch_ratio_requested=batch_ratio_requested", patched)
        self.assertIn("a_owner, c_owner = process_capacity_stage_gates()", patched)
        ratio_start = patched.index("def get_batch_ratio(_device):")
        ratio_end = patched.index("def _close_images", ratio_start)

        class RatioLogger:
            def info(self, _message: str) -> None:
                return None

        namespace = {"os": os, "logger": RatioLogger()}
        exec(compile(patched[ratio_start:ratio_end], "ratio.py", "exec"), namespace)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicitly configured"):
                namespace["get_batch_ratio"]("cpu")
        with patch.dict(os.environ, {"MINERU_HYBRID_BATCH_RATIO": "3"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "one of"):
                namespace["get_batch_ratio"]("cpu")
        with patch.dict(os.environ, {"MINERU_HYBRID_BATCH_RATIO": "8"}, clear=True):
            self.assertEqual(namespace["get_batch_ratio"]("cpu"), 8)

    def test_fast_api_bounds_nonterminal_admission_and_drains_to_terminal(self) -> None:
        patched = patch_source("mineru/cli/fast_api.py", _fast_api_fixture())

        class HTTPExceptionStub(Exception):
            def __init__(self, *, status_code: int, detail: str) -> None:
                super().__init__(detail)
                self.status_code = status_code

        class LoggerStub:
            def exception(self, _message: str) -> None:
                return None

        namespace: dict[str, object] = {
            "asyncio": asyncio,
            "suppress": __import__("contextlib").suppress,
            "HTTPException": HTTPExceptionStub,
            "FastAPI": object,
            "AsyncParseTask": object,
            "TASK_PENDING": "pending",
            "TASK_PROCESSING": "processing",
            "TASK_COMPLETED": "completed",
            "TASK_FAILED": "failed",
            "get_task_cleanup_interval_seconds": lambda: 0,
            "get_processing_window_size": lambda default: default,
            "is_task_terminal": lambda status: status in {"completed", "failed"},
            "utc_now_iso": lambda: "now",
            "logger": LoggerStub(),
            "os": os,
        }
        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "2"}):
            exec(compile(patched, "fast-api.py", "exec"), namespace)

        async def exercise() -> None:
            manager = namespace["AsyncTaskManager"](object())
            await manager.start()

            def task(task_id: str):
                value = type("Task", (), {})()
                value.task_id = task_id
                value.status = "pending"
                value.release = asyncio.Event()
                value.error = None
                value.completed_at = None
                return value

            first = task("first")
            await manager.submit(first)
            while first.status != "processing":
                await asyncio.sleep(0)
            second = task("second")
            await manager.submit(second)
            with self.assertRaises(HTTPExceptionStub) as full:
                await manager.submit(task("third"))
            self.assertEqual(full.exception.status_code, 429)
            first.release.set()
            while second.status != "processing":
                await asyncio.sleep(0)
            second.release.set()
            await manager.shutdown()
            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "completed")
            self.assertEqual(manager.queue._unfinished_tasks, 0)
            with self.assertRaises(HTTPExceptionStub) as closed:
                await manager.submit(task("fourth"))
            self.assertEqual(closed.exception.status_code, 503)

        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "2"}):
            asyncio.run(exercise())

        get_pending = namespace["get_max_pending_tasks"]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicitly configured"):
                get_pending()
        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "5"}):
            with self.assertRaisesRegex(RuntimeError, "one of"):
                get_pending()

    def test_preimages_match_the_reproduced_deployed_344_sources(self) -> None:
        self.assertEqual(
            TARGET_PREIMAGE_SHA256,
            {
                "mineru/cli/fast_api.py": (
                    "f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39"
                ),
                "mineru/backend/vlm/vlm_analyze.py": (
                    "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
                ),
                "mineru/backend/hybrid/hybrid_analyze.py": (
                    "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
                ),
                "mineru/utils/model_utils.py": (
                    "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
                ),
                "mineru_vl_utils/post_process/cross_page_table.py": (
                    "97581c69b92ae80df2a11f3dc986f329b26edca5af57e6052929aeadefab898f"
                ),
                "mineru_vl_utils/vlm_client/http_client.py": (
                    "afe42d8a5e310d27cb0173abf4d59ed6197bc0b60a0258f321a6cdedd07c6ba7"
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
            + _HYBRID_COORDINATOR_FIXTURE
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
                hybrid_batch_ratio_requested=4,
                hybrid_batch_ratio_effective=4,
                hybrid_batch_ratio_ocr_override=False,
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
                hybrid_batch_ratio_requested=4,
                hybrid_batch_ratio_effective=4,
                hybrid_batch_ratio_ocr_override=False,
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
            + _HYBRID_COORDINATOR_FIXTURE
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
                    a_owner=asyncio.Lock(),
                    c_owner=asyncio.Lock(),
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
            'io.agent-invest.mineru.capacity-policy="process-global-mineru-coordinator.v4"',
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
            "function Get-MineruPhaseTraceLines",
            collector,
        )
        self.assertIn('[ValidateSet("stdout", "stderr")]', collector)
        self.assertIn("$rawValue.GetType() -ne [string]", collector)
        self.assertIn("[StringComparison]::Ordinal", collector)
        self.assertIn(
            "$prefixIndex -ne $rawLine.LastIndexOf(",
            collector,
        )
        self.assertIn(
            'throw "phase-trace log line contains multiple event prefixes"',
            collector,
        )
        self.assertIn("$rawLine.Substring($prefixIndex)", collector)
        self.assertIn(
            'Get-MineruPhaseTraceLines -RawLines $stdoutLogLines -Stream "stdout"',
            collector,
        )
        self.assertIn(
            'Get-MineruPhaseTraceLines -RawLines $rawLogLines -Stream "stderr"',
            collector,
        )
        self.assertNotIn('.StartsWith("MINERU_PHASE_TRACE ")', collector)
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

    def test_windows_phase_trace_extraction_smoke_covers_stream_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[2]
        smoke = (
            root / "tests" / "windows" / "test_mineru_phase_trace_line_extraction.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("MINERU PHASE TRACE LINE EXTRACTION V1", smoke)
        self.assertIn("column-zero phase trace did not round trip", smoke)
        self.assertIn("embedded phase trace did not canonicalize", smoke)
        self.assertIn("ordinary stderr was treated as a phase trace", smoke)
        self.assertIn("multiple phase-trace prefixes were accepted", smoke)
        self.assertIn("stdout phase-trace placement was accepted", smoke)
        self.assertIn("non-string log line was accepted", smoke)


if __name__ == "__main__":
    unittest.main()
