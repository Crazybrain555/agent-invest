"""Independent append seam; unchanged generated driver and upstream append/IO bodies."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

from tests._mineru_owned_drain_fixture import (
    DocumentFixture,
    Resource,
    Trace,
    load_definitions,
)

UPSTREAM = Path(__file__).parent / "fixtures/mineru_344_append"
PINS = {
    "mineru/backend/hybrid/hybrid_model_output_to_middle_json.py": "bfecd264bb92a1d8855e122da626d4b8a1fe97fe4c820823013deb9ef9412d87",
    "mineru/utils/pdfium_guard.py": "b669edf0761c2fbdd8d512601339b8f307b212dc777ab76cc026395ca20915e0",
    "mineru/data/data_reader_writer/base.py": "85eac3891bb6dc3be171dc6d5a18abd9a8cb1b592458fd218d68e4c255999803",
    "mineru/data/data_reader_writer/filebase.py": "c047bfd6a588095bf68c0c50204f10c9a6bce2d014a6065f26dc241acbe03e2c",
}


def upstream(relative):
    raw = (UPSTREAM / relative).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINS[relative]:
        raise AssertionError(f"immutable official append preimage drift: {relative}")
    return raw.decode()


class AppendTrace(Trace):
    def __init__(self, events):
        super().__init__(events)
        self.ordinal = 0
        self.records = []

    def start(self):
        self.ordinal += 1
        self.events.append(f"phase-start:{self.ordinal}")
        return self.ordinal

    def complete(self, phase, started, **kwargs):
        self.records.append((phase, started, kwargs))
        self.events.append(f"phase-end:{phase}")


class AppendBarrier:
    """A thread release with a watchdog, never elapsed-time success as an oracle."""

    def __init__(self, events):
        self.events = events
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.thread_id = None
        self.watchdog_released = False

    def hold(self):
        self.thread_id = threading.get_ident()
        self.events.append("append-held:enter")
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.release.wait(2):
                self.watchdog_released = True
        finally:
            self.events.append("append-held:leave")
            self.finished.set()


class AppendFixture(DocumentFixture):
    def __init__(
        self,
        sources,
        output,
        *,
        pages=17,
        effort="medium",
        ocr=False,
        client=False,
        hold=False,
        failure=None,
        failure_page=0,
    ):
        super().__init__(sources, effort=effort, ocr=ocr, client=client)
        self.pages = pages
        self.output = output
        self.images = []
        self.page_handles = []
        self.append_arguments = []
        self.page_info_calls = []
        self.phase = AppendTrace(self.events)
        self.hold = AppendBarrier(self.events) if hold else None
        self.failure = failure
        self.failure_page = failure_page
        self.middle = None
        self._wire_append()

    def _wire_append(self):
        guard = {
            "threading": threading,
            "contextmanager": contextmanager,
            "_pdfium_lock": threading.RLock(),
        }
        load_definitions(
            upstream("mineru/utils/pdfium_guard.py"),
            {
                "pdfium_guard",
                "open_pdfium_document",
                "get_pdfium_document_page_count",
                "close_pdfium_document",
                "close_pdfium_child",
            },
            guard,
        )
        writer_namespace = {"os": os, "ABC": ABC, "abstractmethod": abstractmethod}
        load_definitions(
            upstream("mineru/data/data_reader_writer/base.py"),
            {"DataWriter"},
            writer_namespace,
        )
        load_definitions(
            upstream("mineru/data/data_reader_writer/filebase.py"),
            {"FileBasedDataWriter"},
            writer_namespace,
        )
        self.writer = writer_namespace["FileBasedDataWriter"](str(self.output))
        fixture = self

        class Page(Resource):
            def close(self):
                if not guard["_pdfium_lock"]._is_owned():
                    raise AssertionError("upstream page close lost PDFium guard")
                super().close()

        class Pdf(Resource):
            def __len__(self):
                if not guard["_pdfium_lock"]._is_owned():
                    raise AssertionError("page count lost PDFium guard")
                return fixture.pages

            def __getitem__(self, index):
                if not guard["_pdfium_lock"]._is_owned() or self.closes:
                    raise AssertionError("page acquisition lost live guarded PDF")
                page = Page(fixture.events, f"page-{index}")
                fixture.page_handles.append(page)
                return page

            def close(self):
                if not guard["_pdfium_lock"]._is_owned():
                    raise AssertionError("PDF close lost guard")
                super().close()

        self.pdf = Pdf(self.events, "pdf")

        def page_info(model, image, page, writer, index, ocr):
            # This content seam is synthetic. It checks object/source conservation,
            # then uses the upstream writer against only this test's temporary root.
            if model != {"source_page": index, "literal": "unchanged-model"}:
                raise AssertionError("append changed/reordered model payload")
            if image["source_page"] != index or image["img_pil"].closes:
                raise AssertionError("append changed or prematurely closed image")
            if self.pdf.closes or page.closes or writer is not self.writer:
                raise AssertionError("append lost original PDF/page/writer ownership")
            self.page_info_calls.append((index, threading.get_ident(), ocr))
            self.events.append(f"page-info:{index}")
            if self.hold and index == 0:
                self.hold.hold()
            if self.failure is not None and index == self.failure_page:
                raise self.failure
            writer.write(f"page-{index}.bin", f"literal-page:{index}".encode())
            return {"page_idx": index, "literal": f"source-page:{index}"}

        append_namespace = {**guard, "blocks_to_page_info": page_info}
        load_definitions(
            upstream("mineru/backend/hybrid/hybrid_model_output_to_middle_json.py"),
            {
                "append_page_results_to_middle_json",
                "append_page_model_list_to_middle_json",
            },
            append_namespace,
        )
        actual_append = append_namespace["append_page_model_list_to_middle_json"]

        def record_append(*args, **kwargs):
            self.middle = args[0]
            self.append_arguments.append((args, kwargs))
            self.events.append(f"append-call:{kwargs['page_start_index']}")
            result = actual_append(*args, **kwargs)
            self.events.append(f"append-return:{kwargs['page_start_index']}")
            return result

        async def render(pdf_bytes, *, start_page_id, end_page_id, image_type):
            self.events.append(f"render:{start_page_id}")
            result = []
            for index in range(start_page_id, end_page_id + 1):
                image = Resource(self.events, f"image-{index}")
                self.images.append(image)
                result.append({"img_pil": image, "source_page": index})
            return result

        async def infer(*args, **kwargs):
            images = args[0] if args else kwargs["images"]
            return [
                {
                    "source_page": int(image.name.split("-")[1]),
                    "literal": "unchanged-model",
                }
                for image in images
            ]

        self.predictor.aio_batch_extract_with_layout = infer
        self.predictor.aio_batch_two_step_extract = infer
        self.progress.updates = []
        self.progress.update = self.progress.updates.append
        self.namespace.update(
            {
                "open_pdfium_document": lambda *args: guard["open_pdfium_document"](
                    lambda: self.pdf
                ),
                "get_pdfium_document_page_count": guard[
                    "get_pdfium_document_page_count"
                ],
                "close_pdfium_document": guard["close_pdfium_document"],
                "aio_load_images_from_pdf_bytes_range": render,
                "new_phase_trace": lambda **kwargs: self.phase,
                "_predict_layout_for_window": self.native(
                    "layout",
                    lambda images, *a: (
                        [{"layout": "literal"} for _ in images],
                        self.model,
                    ),
                ),
                "append_page_model_list_to_middle_json": record_append,
            }
        )

    def start(self, *, create_model=False):
        return asyncio.create_task(
            self.namespace["aio_doc_analyze"](
                b"synthetic-pdf-owner-only",
                self.writer,
                predictor=self.predictor,
                effort=self.effort,
                parse_method="ocr" if self.ocr else "auto",
                client_side_output_generation=self.client,
            )
        )

    async def settle(self, task):
        if self.hold:
            self.hold.release.set()
        return await super().settle(task)
