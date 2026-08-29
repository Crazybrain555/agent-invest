"""Exact-source regressions for the MinerU 3.4.4 heap-return image patch."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_phase_trace import (
    PHASE_TRACE_PREFIX,
    parse_phase_trace_line,
    validate_complete_phase_trace,
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
    return '''from mineru.utils.config_reader import get_processing_window_size

_configured_max_concurrent_requests = 1


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
        "        configured_window_size = get_processing_window_size(default=64)\n"
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

        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "1"}):
            asyncio.run(exercise())
        self.assertIn("async with limiter:", patched)
        self.assertNotIn("async with semaphore:\n            response = await client.post", patched)


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
        patched = patched.split("\n", 1)[1]

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
            "strict_processing_window_size": lambda: 16,
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
            with self.assertRaises(HTTPExceptionStub) as full:
                await manager.submit(task("second"))
            self.assertEqual(full.exception.status_code, 429)
            first.release.set()
            while first.status != "completed":
                await asyncio.sleep(0)
            second = task("second")
            await manager.submit(second)
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

        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "1"}):
            asyncio.run(exercise())

        get_pending = namespace["get_max_pending_tasks"]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicitly configured"):
                get_pending()
        with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "2"}):
            with self.assertRaisesRegex(RuntimeError, "serial execution"):
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

    def test_processing_window_is_required_canonical_and_versioned(self) -> None:
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
        strict = namespace["strict_processing_window_size"]
        for environment in ({}, {"MINERU_PROCESSING_WINDOW_SIZE": "016"}):
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ), self.assertRaisesRegex(RuntimeError, "canonical positive integer"):
                strict()
        with patch.dict(
            os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "32"}, clear=True
        ), self.assertRaisesRegex(RuntimeError, "must equal 16"):
            strict()
        with patch.dict(
            os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "16"}, clear=True
        ):
            self.assertEqual(strict(), 16)

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
        self.assertEqual(patched_hybrid.count("trim_process_heap()"), 4)
        self.assertEqual(patched_vlm.count('"window_vlm",'), 2)
        self.assertEqual(patched_hybrid.count('"window_layout",'), 2)
        self.assertEqual(patched_hybrid.count('"window_postprocess",'), 2)
        self.assertEqual(patched_hybrid.count('"window_total",'), 2)
        self.assertIn("serial_execution_profile", patched_hybrid)
        self.assertNotIn("get_processing_window_size(default=64)", patched_vlm)
        self.assertNotIn("get_processing_window_size(default=64)", patched_hybrid)

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
            execution_profile = namespace["serial_execution_profile"](16)
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
        self.assertIn(
            'io.agent-invest.mineru.capacity-policy="single-owner-serial-mineru.v1"',
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
        self.assertIn('schema = "mineru-phase-trace-capture.v2"', collector)
        self.assertIn("$traceLines.Count -gt $MaxTraceLines", collector)
        self.assertIn("$traceByteCount -gt $MaxTraceBytes", collector)
        self.assertIn("active_profile_sha256", collector)
        self.assertIn("mineru-serial-v1/compatibility.json", collector)
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
