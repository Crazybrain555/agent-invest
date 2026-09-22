"""Independent real-generated-code fixture; external model/IO effects are sentinels."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(os.environ.get("M6_TEST_SERVICE_ROOT", Path(__file__).parents[1]))
PATCHER = Path(
    os.environ.get(
        "M6_TEST_PATCHER",
        SERVICE / "scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py",
    )
)
PREIMAGES = SERVICE / "tests/fixtures/mineru_344_preimages"
RELATIVE = {
    "hybrid": "mineru/backend/hybrid/hybrid_analyze.py",
    "vlm": "mineru/backend/vlm/vlm_analyze.py",
    "model": "mineru/utils/model_utils.py",
    "api": "mineru/cli/fast_api.py",
}
# Independent frozen upstream identities, not a hash table copied from the generator at runtime.
PREIMAGE_HASHES = {
    "hybrid": "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0",
    "vlm": "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390",
    "model": "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109",
    "api": "f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39",
}


def generated_sources():
    spec = importlib.util.spec_from_file_location(
        "independent_owned_drain_patcher", PATCHER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {}
    for kind, relative in RELATIVE.items():
        raw = (PREIMAGES / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != PREIMAGE_HASHES[kind]:
            raise AssertionError(f"upstream preimage drift: {relative}")
        result[kind] = module.patch_source(relative, raw.decode("utf-8"))
        compile(result[kind], relative, "exec")
    return result


def load_definitions(source, names, namespace):
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    if {node.name for node in nodes} != set(names):
        raise AssertionError(
            f"missing real generated definitions: {set(names) - {node.name for node in nodes}}"
        )
    unit = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    # The actual function/class nodes are compiled without modifying any bodies or awaits.
    exec(
        compile(ast.fix_missing_locations(unit), "<actual-generated-mineru>", "exec"),
        namespace,
    )


def load_owned(source, consumer_source):
    names = {
        "OwnedOperation",
        "drain_owned_awaitable",
        "run_native_owned",
        "run_async_owned",
    }
    if any(
        isinstance(n, ast.AsyncFunctionDef) and n.name == "to_thread_owned"
        for n in ast.parse(source).body
    ):
        names.add("to_thread_owned")
    namespace = {"asyncio": asyncio}
    load_definitions(source, names, namespace)
    consumer = {"asyncio": asyncio}
    # Bind only helpers actually imported by the generated consumer. This prevents
    # AST isolation from concealing a missing or wrong production import.
    for node in ast.parse(consumer_source).body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "utils.model_utils"
        ):
            for alias in node.names:
                if alias.name in names:
                    consumer[alias.asname or alias.name] = namespace[alias.name]
    return consumer


async def loop_turns():
    """A FIFO callback fence, never an elapsed-sleep correctness assumption."""
    for _ in range(3):
        done = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(done.set_result, None)
        await done


class NativeFailure(RuntimeError):
    pass


class NativeBarrier:
    def __init__(self, events, name, *, error=None):
        self.events = events
        self.name = name
        self.error = error
        self.release = threading.Event()
        self.finished = threading.Event()
        self.started = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.thread_id = None

    def enter(self):
        self.thread_id = threading.get_ident()
        self.events.append(f"{self.name}:entered")
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            if not self.release.wait(4):
                raise AssertionError("independent native barrier expired")
            if self.error is not None:
                raise self.error
        finally:
            self.events.append(f"{self.name}:finished")
            self.finished.set()


class Resource:
    width = 64
    height = 32

    def __init__(self, events, name):
        self.events = events
        self.name = name
        self.closes = 0

    def close(self):
        self.closes += 1
        self.events.append(f"{self.name}:close")


class Trace:
    def __init__(self, events):
        self.events = events

    def start(self):
        return 1

    def window(self, **kwargs):
        return kwargs

    def complete(self, *args, **kwargs):
        pass

    def document_started(self):
        self.events.append("document:started")

    def document_completed(self):
        self.events.append("document:completed")

    def document_failed(self):
        self.events.append("document:failed")


class DocumentFixture:
    def __init__(
        self,
        sources,
        *,
        block=None,
        error=None,
        kind="hybrid",
        effort="medium",
        ocr=False,
        client=False,
    ):
        self.sources = sources
        self.kind = kind
        self.effort = effort
        self.ocr = ocr
        self.client = client
        self.events = []
        self.pdf = Resource(self.events, "pdf")
        self.image = Resource(self.events, "image")
        self.progress = Resource(self.events, "progress")
        self.barrier = NativeBarrier(self.events, block, error=error) if block else None
        self.block = block
        self.failure = error
        self.model = object()
        self.calls = []
        self.namespace = load_owned(sources["model"], sources[kind])
        self.namespace.update(self._environment())
        load_definitions(
            sources[kind], {"aio_doc_analyze", "_close_images"}, self.namespace
        )
        if kind == "vlm":
            load_definitions(sources[kind], {"_get_model_async"}, self.namespace)

    def native(self, name, result=None):
        def operation(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            self.events.append(name)
            if self.block == name:
                self.barrier.enter()
            return result(*args, **kwargs) if callable(result) else result

        return operation

    def _environment(self):
        async def render(*args, **kwargs):
            # This external render seam has the upstream coroutine -> to_thread shape.
            # The actual hybrid/VLM caller and its cancellation-result cleanup are unchanged AST.
            if self.block == "render":
                return await asyncio.to_thread(
                    self.native("render", [{"img_pil": self.image}])
                )
            self.events.append("render")
            return [{"img_pil": self.image}]

        async def infer(*args, **kwargs):
            self.events.append("infer")
            return [{"page": "literal-result"}]

        @asynccontextmanager
        async def guard(*args, **kwargs):
            self.events.append("model-lock:enter")
            try:
                yield
            finally:
                self.events.append("model-lock:exit")

        def append(middle, results, images, pdf, writer, **kwargs):
            if images[0]["img_pil"] is not self.image or pdf is not self.pdf:
                raise AssertionError("actual source/image identity changed")
            if self.image.closes or self.pdf.closes:
                raise AssertionError("append saw closed resources")
            middle["pdf_info"].append({"page": "literal-appended"})
            self.events.append("append")

        predictor = SimpleNamespace(
            aio_batch_extract_with_layout=infer, aio_batch_two_step_extract=infer
        )
        self.predictor = predictor
        trace = Trace(self.events)
        return {
            "time": time,
            "asyncio": asyncio,
            "pdfium": SimpleNamespace(PdfDocument=object),
            "ImageType": SimpleNamespace(PIL="literal-pil"),
            "logger": SimpleNamespace(info=lambda *a: None, debug=lambda *a: None),
            "_validate_parse_effort": lambda value: value,
            "_resolve_effective_image_analysis": lambda effort, image: image,
            "_maybe_enable_serial_execution": lambda value, backend: value,
            "get_device": lambda: "cpu",
            "ocr_classify": lambda *a, **kw: self.ocr,
            "open_pdfium_document": lambda *a: self.pdf,
            "close_pdfium_document": lambda doc: doc.close(),
            "init_middle_json": lambda *a, **kw: {"pdf_info": []},
            "get_pdfium_document_page_count": lambda doc: 1,
            "strict_processing_window_size": lambda: 16,
            "serial_execution_profile": lambda size: SimpleNamespace(window_size=size),
            "get_batch_ratio": lambda device: 1,
            "new_phase_trace": lambda **kw: trace,
            "aio_load_images_from_pdf_bytes_range": render,
            "_normalize_page_size": lambda image: (image.width, image.height),
            "_predict_layout_for_window": self.native(
                "layout", ([{"layout": "literal"}], self.model)
            ),
            "_apply_medium_table_orientation_labels": self.native("orientation"),
            "_build_medium_vlm_layout_blocks": lambda *args: ["literal-layout-block"],
            "aio_predictor_execution_guard": guard,
            "not_extract_list": ["literal-exclusion"],
            "optimize_hybrid_formula_number_blocks": lambda values: None,
            "_apply_vlm_ocr_det_sidecars_for_window": self.native("sidecar"),
            "_process_ocr_and_formulas": self.native(
                "ocr", lambda images, values, *a, **kw: values
            ),
            "_apply_layout_title_split": self.native("title"),
            "tqdm": lambda **kw: self.progress,
            "exclude_progress_bar_idle_time": lambda *a, **kw: None,
            "append_page_model_list_to_middle_json": append,
            "append_page_blocks_to_middle_json": append,
            "trim_process_heap": lambda: self.events.append("trim"),
            "apply_server_side_postprocess": self.native("server_finalize"),
            "finalize_middle_json": self.native("finalize"),
            "clean_memory": lambda device: self.events.append("clean-memory"),
            "release_document_memory_owned": lambda device: self.events.append(
                "release-document-memory"
            ),
            "ModelSingleton": lambda: SimpleNamespace(
                get_model=self.native("get_model", predictor)
            ),
        }

    def start(self, *, create_model=False):
        kwargs = {
            "predictor": None if create_model else self.predictor,
            "client_side_output_generation": self.client,
        }
        if self.kind == "hybrid":
            kwargs.update(
                effort=self.effort, parse_method="ocr" if self.ocr else "auto"
            )
        return asyncio.create_task(
            self.namespace["aio_doc_analyze"](b"sentinel-owned-pdf", None, **kwargs)
        )

    async def settle(self, task):
        if self.barrier:
            self.barrier.release.set()
        try:
            return await asyncio.wait_for(asyncio.shield(task), 5)
        except asyncio.CancelledError:
            return None
