"""Independent real-generated-code fixture; external model/IO effects are sentinels."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import os
import threading
import time
from contextlib import asynccontextmanager, nullcontext
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
    "cli": "mineru/cli/common.py",
}
# Independent frozen upstream identities, not a hash table copied from the generator at runtime.
PREIMAGE_HASHES = {
    "hybrid": "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0",
    "vlm": "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390",
    "model": "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109",
    "api": "f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39",
    "cli": "d1e23e310bddc3da2d7f491be81ef112435824403d1c3a29e438505c1707dbc5",
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


class GuardProbe:
    """A real re-entrant guard around the stubbed pdfium seams.

    Production serializes every pdfium call behind one process lock. The probe
    reproduces that contention and announces, from the owned worker thread, that
    it is about to block on the guard, so a test can fence the loop instead of
    sleeping.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.waiting = asyncio.Event()
        self.attempts = []
        self.loop = asyncio.get_running_loop()

    def hold(self, name):
        self.attempts.append(name)
        self.loop.call_soon_threadsafe(self.waiting.set)
        return self.lock


class Trace:
    def __init__(self, events, faults=None):
        self.events = events
        # The real document-end trace writes and flushes stderr, so it can fail
        # after the event is on its way out; that is the shape reproduced here.
        self.faults = dict(faults or {})

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
        fault = self.faults.get("document_failed")
        if fault is not None:
            raise fault


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
        faults=None,
        guard=False,
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
        # Seam name -> exception raised by that seam without a barrier round trip.
        self.faults = dict(faults or {})
        self.guard = GuardProbe() if guard else None
        self.model = object()
        self.calls = []
        # Document-lifetime seams: name, arguments and the thread that ran them.
        self.document_calls = []
        # Every real _OwnedPdfiumDocument the generated analyzer created.
        self.documents = []
        self.namespace = load_owned(sources["model"], sources[kind])
        self.namespace.update(self._environment())
        definitions = {"aio_doc_analyze", "_close_images"}
        if kind == "hybrid":
            definitions.add("_OwnedPdfiumDocument")
        load_definitions(sources[kind], definitions, self.namespace)
        if kind == "hybrid":
            self._record_owned_documents()
        if kind == "vlm":
            load_definitions(sources[kind], {"_get_model_async"}, self.namespace)

    def _record_owned_documents(self):
        """Keep every holder the real analyzer builds; its body stays untouched."""
        holder = self.namespace["_OwnedPdfiumDocument"]
        self.holder_class = holder

        def create(*args, **kwargs):
            document = holder(*args, **kwargs)
            self.documents.append(document)
            return document

        self.namespace["_OwnedPdfiumDocument"] = create

    def _gate(self, name):
        """Barrier and injected failure shared by every stubbed seam."""
        if self.block == name:
            self.barrier.enter()
        fault = self.faults.get(name)
        if fault is not None:
            raise fault

    def native(self, name, result=None):
        def operation(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            self.events.append(name)
            self._gate(name)
            return result(*args, **kwargs) if callable(result) else result

        return operation

    def document_seam(self, name, result=None, *, event=None, guarded=False):
        """A document-lifetime native seam: recorded, blockable, failable, guarded."""

        def operation(*args, **kwargs):
            self.document_calls.append(
                SimpleNamespace(
                    name=name,
                    args=args,
                    kwargs=kwargs,
                    thread_id=threading.get_ident(),
                )
            )
            if event is not None:
                self.events.append(event)
            self._gate(name)
            contention = (
                self.guard.hold(name)
                if guarded and self.guard is not None
                else nullcontext()
            )
            with contention:
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
        trace = Trace(self.events, self.faults)
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
            "ocr_classify": self.document_seam(
                "classify",
                lambda *a, **kw: self.ocr,
                event="ocr-classify",
                guarded=True,
            ),
            "open_pdfium_document": self.document_seam(
                "open_document", lambda *a: self.pdf, event="pdf:open", guarded=True
            ),
            "close_pdfium_document": self.document_seam(
                "close_document",
                lambda doc: doc.close(),
                event="pdf:close-call",
                guarded=True,
            ),
            "init_middle_json": lambda *a, **kw: {"pdf_info": []},
            "get_pdfium_document_page_count": self.document_seam(
                "page_count", lambda doc: 1, event="pdf:page-count", guarded=True
            ),
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
            "release_document_memory_owned": self.document_seam(
                "release_document", event="release-document-memory"
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


class CliFixture:
    """The real generated asynchronous CLI entry over sentinel output seams.

    ``aio_do_parse`` and the two asynchronous per-backend processors are loaded
    from the generated ``mineru/cli/common.py``; every filesystem, model and
    output-generation effect is a recorded stub, so the only real behaviour under
    test is which call the coroutine keeps on the serving loop.
    """

    IMAGE_DIRECTORY = "literal-image-dir"
    MARKDOWN_DIRECTORY = "literal-md-dir"

    def __init__(self, sources, *, block=None, backend="hybrid-transformers"):
        self.sources = sources
        self.backend = backend
        self.events = []
        self.calls = []
        self.block = block
        self.barrier = NativeBarrier(self.events, block) if block else None
        self.writers = []
        self.namespace = load_owned(sources["model"], sources["cli"])
        self.namespace.update(self._environment())
        load_definitions(
            sources["cli"],
            {"aio_do_parse", "_async_process_vlm", "_async_process_hybrid"},
            self.namespace,
        )

    def seam(self, name, result=None):
        def operation(*args, **kwargs):
            self.calls.append(
                SimpleNamespace(
                    name=name,
                    args=args,
                    kwargs=kwargs,
                    thread_id=threading.get_ident(),
                )
            )
            self.events.append(name)
            if self.block == name:
                self.barrier.enter()
            return result(*args, **kwargs) if callable(result) else result

        return operation

    def named(self, name):
        return [call for call in self.calls if call.name == name]

    def _environment(self):
        async def analyze(pdf_bytes, **kwargs):
            self.calls.append(
                SimpleNamespace(
                    name="analyze",
                    args=(pdf_bytes,),
                    kwargs=kwargs,
                    thread_id=threading.get_ident(),
                )
            )
            self.events.append("analyze")
            return (
                {"pdf_info": ["literal-page-info"]},
                ["literal-infer-result"],
            )

        def writer(directory):
            handle = SimpleNamespace(directory=directory)
            self.writers.append(handle)
            return handle

        return {
            "asyncio": asyncio,
            # A stub environment mapping: the generated entry writes MinerU
            # switches into it, and a test process must not inherit them.
            "os": SimpleNamespace(environ={}),
            "logger": SimpleNamespace(
                warning=lambda *a: self.events.append("warning"),
                info=lambda *a: None,
                debug=lambda *a: None,
            ),
            "MakeMode": SimpleNamespace(MM_MD="literal-mm-md"),
            "DEFAULT_HYBRID_EFFORT": "literal-default-effort",
            "normalize_backend": lambda backend: backend,
            "ensure_backend_dependencies": lambda backend: self.events.append(
                "dependencies"
            ),
            "get_vlm_engine": lambda **kwargs: "literal-engine",
            "validate_effort": lambda value: f"validated:{value}",
            "FileBasedDataWriter": writer,
            "aio_vlm_doc_analyze": analyze,
            "_load_hybrid_analyze_entrypoint": lambda name, backend: analyze,
            "_process_office_doc": self.seam("office", lambda *a, **kw: []),
            "_process_pipeline": self.seam("pipeline"),
            "_prepare_pdf_bytes": self.seam(
                "prepare_pdf_bytes",
                lambda values, start, end: [b"literal-rewritten-pdf" for _ in values],
            ),
            "prepare_env": self.seam(
                "prepare_env",
                lambda *args: (self.IMAGE_DIRECTORY, self.MARKDOWN_DIRECTORY),
            ),
            "_process_output": self.seam("process_output"),
        }

    def start(self, **overrides):
        arguments = {
            "output_dir": "literal-output-dir",
            "pdf_file_names": ["literal-file-name"],
            "pdf_bytes_list": [b"literal-source-pdf"],
            "p_lang_list": ["literal-lang"],
            "backend": self.backend,
            "parse_method": "literal-parse-method",
            "formula_enable": "literal-formula",
            "table_enable": "literal-table",
            "server_url": "literal-server-url",
            "f_draw_layout_bbox": "literal-draw-layout",
            "f_draw_span_bbox": "literal-draw-span",
            "f_dump_md": "literal-dump-md",
            "f_dump_middle_json": "literal-dump-middle",
            "f_dump_model_output": "literal-dump-model",
            "f_dump_orig_pdf": "literal-dump-orig",
            "f_dump_content_list": "literal-dump-content",
            "f_make_md_mode": "literal-md-mode",
            "start_page_id": "literal-start-page",
            "end_page_id": "literal-end-page",
            "effort": "literal-effort",
        }
        arguments.update(overrides)
        return asyncio.create_task(self.namespace["aio_do_parse"](**arguments))

    async def settle(self, task):
        if self.barrier:
            self.barrier.release.set()
        try:
            return await asyncio.wait_for(asyncio.shield(task), 5)
        except asyncio.CancelledError:
            return None
