"""Who owns the pdfium document and the CLI output work — the loop never does.

Every case below runs the real generated ``aio_doc_analyze``/``_OwnedPdfiumDocument``
and the real generated asynchronous CLI entry through ``to_thread_owned``; only the
native, model and filesystem effects are sentinels. Progress is proved by thread
barriers and FIFO loop fences, never by elapsed time.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests._mineru_owned_drain_fixture import (
    CliFixture,
    DocumentFixture,
    NativeFailure,
    generated_sources,
    loop_turns,
)
from tests.unit.test_mineru_heap_trim_compat import (
    _calls_to,
    _named_function,
    _owned_thread_calls,
    _single_owned_thread_await,
)


class GuardHolder:
    """Holds the fixture's pdfium guard on an executor thread until released."""

    def __init__(self, guard):
        self.guard = guard
        self.held = threading.Event()
        self.release = threading.Event()
        self.expired = False
        self.executor = ThreadPoolExecutor(max_workers=1)

    def _run(self):
        with self.guard.lock:
            self.held.set()
            if not self.release.wait(4):
                self.expired = True

    async def take(self):
        self.future = self.executor.submit(self._run)
        await asyncio.to_thread(self.held.wait, 4)
        if not self.held.is_set():
            raise AssertionError("independent guard holder never acquired the guard")

    def free(self):
        self.release.set()
        self.executor.shutdown(wait=True)


class OwnedDocumentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    async def advance_loop(self, beats=3):
        """The serving loop really ran: a FIFO fence plus an independent task."""
        counted = []

        async def heartbeat():
            for beat in range(beats):
                await loop_turns()
                counted.append(beat)

        await asyncio.wait_for(asyncio.create_task(heartbeat()), 2)
        self.assertEqual(counted, list(range(beats)))

    def seam(self, fx, name):
        return [call for call in fx.document_calls if call.name == name]

    async def settled(self, task):
        done, _ = await asyncio.wait({task}, timeout=3)
        self.assertIn(task, done, "the owned native call never settled after release")

    async def test_prologue_cancel_drains_the_worker_and_closes_off_the_loop(self):
        # The prologue owns classification, the pdfium open and the page count.
        # Cancelling it must not abandon the handle the worker just opened, and
        # must not leave a document the trace never started.
        for stage in ("classify", "page_count"):
            with self.subTest(stage=stage):
                loop_thread = threading.get_ident()
                fx = DocumentFixture(self.sources, block=stage)
                task = fx.start()
                try:
                    await asyncio.wait_for(fx.barrier.started.wait(), 2)
                    self.assertNotEqual(fx.barrier.thread_id, loop_thread)
                    await self.advance_loop()
                    self.assertFalse(task.done())
                    task.cancel("prologue-owner-cancel")
                    await loop_turns()
                    self.assertFalse(task.done())
                    self.assertFalse(fx.barrier.finished.is_set())
                    self.assertEqual(fx.pdf.closes, 0)
                    fx.barrier.release.set()
                    await self.settled(task)
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertEqual(caught.exception.args, ("prologue-owner-cancel",))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(fx.barrier.finished.is_set())
                    closes = self.seam(fx, "close_document")
                    self.assertEqual(len(closes), 1)
                    self.assertNotEqual(closes[0].thread_id, loop_thread)
                    self.assertEqual(fx.pdf.closes, 1)
                    self.assertEqual(
                        [event for event in fx.events if event.startswith("document:")],
                        [],
                    )
                    self.assertEqual(len(fx.documents), 1)
                    self.assertTrue(fx.documents[0].close_attempted)
                    self.assertTrue(fx.documents[0].closed)
                finally:
                    await fx.settle(task)

    async def test_page_count_failure_closes_in_the_worker_and_keeps_the_original(self):
        loop_thread = threading.get_ident()
        error = NativeFailure("literal page count failure")
        fx = DocumentFixture(self.sources, faults={"page_count": error})
        task = fx.start()
        with self.assertRaises(NativeFailure) as caught:
            await asyncio.wait_for(asyncio.shield(task), 3)
        self.assertIs(caught.exception, error)
        self.assertIsNone(caught.exception.__cause__)
        closes = self.seam(fx, "close_document")
        self.assertEqual(len(closes), 1)
        self.assertNotEqual(closes[0].thread_id, loop_thread)
        # The prologue closed inside its own worker call, not in a second dispatch.
        self.assertEqual(closes[0].thread_id, self.seam(fx, "page_count")[0].thread_id)
        self.assertEqual(fx.pdf.closes, 1)
        # No trace exists yet, so a failed prologue produces no document event.
        self.assertEqual(
            [event for event in fx.events if event.startswith("document:")], []
        )
        self.assertTrue(fx.documents[0].closed)

    async def test_cancel_during_release_keeps_one_close_and_no_document_end(self):
        # The close already happened inside close_and_release, so the failure
        # path must not close again and must not claim the document failed.
        loop_thread = threading.get_ident()
        fx = DocumentFixture(self.sources, block="release_document")
        task = fx.start()
        try:
            await asyncio.wait_for(fx.barrier.started.wait(), 2)
            self.assertNotEqual(fx.barrier.thread_id, loop_thread)
            self.assertEqual(fx.pdf.closes, 1)
            task.cancel("release-owner-cancel")
            await loop_turns()
            await self.advance_loop()
            self.assertFalse(task.done())
            self.assertFalse(fx.barrier.finished.is_set())
            fx.barrier.release.set()
            await self.settled(task)
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertEqual(caught.exception.args, ("release-owner-cancel",))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(fx.barrier.finished.is_set())
            self.assertEqual(fx.events.count("release-document-memory"), 1)
            self.assertEqual(fx.pdf.closes, 1)
            self.assertEqual(len(self.seam(fx, "close_document")), 1)
            self.assertNotIn("document:completed", fx.events)
            self.assertNotIn("document:failed", fx.events)
            after_release = fx.events[fx.events.index("release-document-memory") :]
            self.assertNotIn("pdf:close-call", after_release)
            self.assertNotIn("pdf:close", after_release)
        finally:
            await fx.settle(task)

    async def test_repeated_cancel_keeps_the_first_cancellation_and_one_close(self):
        for stage in ("close_document", "classify"):
            with self.subTest(stage=stage):
                fx = DocumentFixture(self.sources, block=stage)
                task = fx.start()
                try:
                    await asyncio.wait_for(fx.barrier.started.wait(), 2)
                    task.cancel("first-document-cancellation")
                    await loop_turns()
                    task.cancel("later-cancellation-must-not-replace-the-first")
                    await loop_turns()
                    self.assertFalse(task.done())
                    self.assertFalse(fx.barrier.finished.is_set())
                    fx.barrier.release.set()
                    await self.settled(task)
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertEqual(
                        caught.exception.args, ("first-document-cancellation",)
                    )
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(fx.barrier.finished.is_set())
                    self.assertEqual(len(self.seam(fx, "close_document")), 1)
                    self.assertEqual(fx.pdf.closes, 1)
                    self.assertNotIn("document:completed", fx.events)
                    self.assertNotIn("document:failed", fx.events)
                finally:
                    await fx.settle(task)

    async def test_close_failure_chains_onto_the_original_failure(self):
        loop_thread = threading.get_ident()
        for stage, note in (
            ("layout", "owned document close after failure failed: NativeFailure"),
            (
                "page_count",
                "owned document close after page count failure failed: NativeFailure",
            ),
        ):
            with self.subTest(stage=stage):
                original = NativeFailure(f"literal {stage} failure")
                close_error = NativeFailure("literal close failure")
                fx = DocumentFixture(
                    self.sources,
                    faults={stage: original, "close_document": close_error},
                )
                task = fx.start()
                with self.assertRaises(NativeFailure) as caught:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                # Direct identities only: ``raise failure from cleanup`` makes
                # cleanup.__context__ the failure, so the chain is cyclic.
                self.assertIs(caught.exception, original)
                self.assertIs(caught.exception.__cause__, close_error)
                self.assertIn(note, caught.exception.__notes__)
                closes = self.seam(fx, "close_document")
                self.assertEqual(len(closes), 1)
                self.assertNotEqual(closes[0].thread_id, loop_thread)
                self.assertEqual(fx.pdf.closes, 0)
                holder = fx.documents[0]
                self.assertTrue(holder.close_attempted)
                self.assertFalse(holder.closed)
                # A repeated close is a no-op: the attempt is never retried.
                holder.close()
                self.assertEqual(len(self.seam(fx, "close_document")), 1)
                self.assertTrue(holder.close_attempted)
                self.assertFalse(holder.closed)
                if stage == "layout":
                    # The trace end is emitted before the close is attempted.
                    self.assertLess(
                        fx.events.index("document:failed"),
                        fx.events.index("pdf:close-call"),
                    )
                else:
                    self.assertNotIn("document:failed", fx.events)

    async def test_trace_end_failure_never_skips_the_owned_close(self):
        loop_thread = threading.get_ident()
        for close_error in (None, NativeFailure("literal close failure")):
            with self.subTest(close_fails=close_error is not None):
                layout_error = NativeFailure("literal layout failure")
                trace_error = OSError("literal phase trace stderr failure")
                faults = {"layout": layout_error, "document_failed": trace_error}
                if close_error is not None:
                    faults["close_document"] = close_error
                fx = DocumentFixture(self.sources, faults=faults)
                task = fx.start()
                with self.assertRaises(NativeFailure) as caught:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                self.assertIs(caught.exception, layout_error)
                self.assertIn(
                    "phase trace document end after failure failed: OSError",
                    caught.exception.__notes__,
                )
                closes = self.seam(fx, "close_document")
                self.assertEqual(len(closes), 1)
                self.assertNotEqual(closes[0].thread_id, loop_thread)
                self.assertIn("document:failed", fx.events)
                self.assertLess(
                    fx.events.index("document:failed"),
                    fx.events.index("pdf:close-call"),
                )
                if close_error is None:
                    self.assertIs(caught.exception.__cause__, trace_error)
                    self.assertNotIn(
                        "owned document close after failure failed: NativeFailure",
                        caught.exception.__notes__,
                    )
                    self.assertEqual(fx.pdf.closes, 1)
                    self.assertTrue(fx.documents[0].closed)
                else:
                    self.assertIs(caught.exception.__cause__, close_error)
                    self.assertIs(close_error.__context__, trace_error)
                    self.assertIn(
                        "owned document close after failure failed: NativeFailure",
                        caught.exception.__notes__,
                    )
                    self.assertEqual(fx.pdf.closes, 0)
                    self.assertFalse(fx.documents[0].closed)

    async def test_guard_contention_never_stalls_the_serving_loop(self):
        # Production serializes every pdfium call behind one process guard. A
        # document that waits for it must wait on a worker thread, not on the
        # loop, both in the prologue and at the document-end close.
        with self.subTest(phase="prologue"):
            fx = DocumentFixture(self.sources, guard=True)
            holder = GuardHolder(fx.guard)
            await holder.take()
            task = fx.start()
            try:
                await asyncio.wait_for(fx.guard.waiting.wait(), 2)
                self.assertEqual(fx.guard.attempts, ["classify"])
                await self.advance_loop()
                self.assertFalse(task.done())
                self.assertNotIn("pdf:open", fx.events)
                self.assertIsNone(fx.documents[0].pdf_doc)
            finally:
                holder.free()
            result = await asyncio.wait_for(task, 3)
            self.assertEqual(
                result,
                (
                    {"pdf_info": [{"page": "literal-appended"}]},
                    [{"page": "literal-result"}],
                ),
            )
            self.assertEqual(fx.pdf.closes, 1)

        with self.subTest(phase="document-end close"):
            fx = DocumentFixture(self.sources, block="finalize", guard=True)
            task = fx.start()
            holder = GuardHolder(fx.guard)
            try:
                await asyncio.wait_for(fx.barrier.started.wait(), 2)
                await holder.take()
                fx.guard.waiting.clear()
                fx.barrier.release.set()
                await asyncio.wait_for(fx.guard.waiting.wait(), 2)
                self.assertEqual(fx.guard.attempts[-1], "close_document")
                await self.advance_loop()
                self.assertFalse(task.done())
                self.assertEqual(fx.pdf.closes, 0)
                self.assertNotIn("release-document-memory", fx.events)
            finally:
                holder.free()
            await asyncio.wait_for(task, 3)
            self.assertEqual(fx.pdf.closes, 1)
            self.assertEqual(
                fx.events[-3:],
                ["pdf:close", "release-document-memory", "document:completed"],
            )
            self.assertFalse(holder.expired)

    def test_synchronous_document_analyzer_keeps_its_upstream_shape(self):
        # Control for every asynchronous case above: the synchronous entry point
        # is not part of the serving profile and must stay byte-identical.
        tree = ast.parse(self.sources["hybrid"])
        sync_analyze = _named_function(tree, "doc_analyze")
        self.assertIsInstance(sync_analyze, ast.FunctionDef)
        self.assertEqual(
            {
                name: len(_calls_to(sync_analyze, name))
                for name in (
                    "ocr_classify",
                    "open_pdfium_document",
                    "get_pdfium_document_page_count",
                    "close_pdfium_document",
                    "to_thread_owned",
                )
            },
            {
                "ocr_classify": 1,
                "open_pdfium_document": 1,
                "get_pdfium_document_page_count": 1,
                "close_pdfium_document": 2,
                "to_thread_owned": 0,
            },
        )
        names = [
            node.id for node in ast.walk(sync_analyze) if isinstance(node, ast.Name)
        ]
        self.assertEqual(names.count("doc_closed"), 3)
        self.assertNotIn("_OwnedPdfiumDocument", names)
        # Exact source segment from baseline 733a9332, independently checked on
        # Python 3.9 and 3.13. ast.dump itself has version-dependent formatting.
        self.assertEqual(
            hashlib.sha256(
                ast.get_source_segment(self.sources["hybrid"], sync_analyze).encode()
            ).hexdigest(),
            "c2fe26dc4dd1524af2c167813dcc28b6b9741fd5d6e6d4050acce9190f77e63d",
        )


class CliOwnedBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    async def advance_loop(self, beats=3):
        counted = []

        async def heartbeat():
            for beat in range(beats):
                await loop_turns()
                counted.append(beat)

        await asyncio.wait_for(asyncio.create_task(heartbeat()), 2)
        self.assertEqual(counted, list(range(beats)))

    async def test_blocked_cli_work_drains_once_without_stalling_the_loop(self):
        for stage, backend, reached in (
            ("prepare_pdf_bytes", "hybrid-transformers", ()),
            ("prepare_env", "hybrid-transformers", ("prepare_pdf_bytes",)),
            (
                "process_output",
                "hybrid-transformers",
                ("prepare_pdf_bytes", "prepare_env", "analyze"),
            ),
            ("prepare_env", "vlm-transformers", ("prepare_pdf_bytes",)),
        ):
            with self.subTest(stage=stage, backend=backend):
                loop_thread = threading.get_ident()
                fx = CliFixture(self.sources, block=stage, backend=backend)
                task = fx.start()
                try:
                    await asyncio.wait_for(fx.barrier.started.wait(), 2)
                    self.assertNotEqual(fx.barrier.thread_id, loop_thread)
                    await self.advance_loop()
                    self.assertFalse(task.done())
                    task.cancel("cli-owner-cancel")
                    await loop_turns()
                    self.assertFalse(task.done())
                    self.assertFalse(fx.barrier.finished.is_set())
                    fx.barrier.release.set()
                    done, _ = await asyncio.wait({task}, timeout=3)
                    self.assertIn(task, done)
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertEqual(caught.exception.args, ("cli-owner-cancel",))
                    self.assertTrue(fx.barrier.finished.is_set())
                    # The started call drained exactly once and nothing after it ran.
                    self.assertEqual(len(fx.named(stage)), 1)
                    self.assertEqual(
                        [call.name for call in fx.calls],
                        ["office", *reached, stage],
                    )
                finally:
                    await fx.settle(task)

    async def test_cli_owned_calls_keep_the_upstream_arguments_and_leave_the_loop(self):
        loop_thread = threading.get_ident()
        for backend, directory, analyze_kwargs, environment in (
            (
                "hybrid-transformers",
                "hybrid_literal-parse-method",
                {
                    "backend": "transformers",
                    "parse_method": "literal-parse-method",
                    "inline_formula_enable": "literal-formula",
                    "server_url": None,
                    "effort": "validated:literal-effort",
                    "image_analysis": True,
                    "client_side_output_generation": False,
                },
                {
                    "MINERU_VLM_TABLE_ENABLE": "literal-table",
                    "MINERU_VLM_FORMULA_ENABLE": "true",
                },
            ),
            (
                "vlm-transformers",
                "vlm",
                {
                    "backend": "transformers",
                    "server_url": None,
                    "image_analysis": True,
                    "client_side_output_generation": False,
                },
                {
                    "MINERU_VLM_FORMULA_ENABLE": "literal-formula",
                    "MINERU_VLM_TABLE_ENABLE": "literal-table",
                },
            ),
        ):
            with self.subTest(backend=backend):
                fx = CliFixture(self.sources, backend=backend)
                await asyncio.wait_for(fx.start(), 3)
                self.assertEqual(
                    [call.name for call in fx.calls],
                    [
                        "office",
                        "prepare_pdf_bytes",
                        "prepare_env",
                        "analyze",
                        "process_output",
                    ],
                )
                # Every synchronous seam left the serving loop — office
                # conversion through its upstream asyncio.to_thread hand-off,
                # the rest through the owned pool. The document analyzer is a
                # coroutine and is still awaited directly on the loop.
                for call in fx.calls:
                    if call.name == "analyze":
                        self.assertEqual(call.thread_id, loop_thread)
                    else:
                        self.assertNotEqual(call.thread_id, loop_thread, call.name)
                rewrite = fx.named("prepare_pdf_bytes")[0]
                self.assertEqual(
                    rewrite.args,
                    ([b"literal-source-pdf"], "literal-start-page", "literal-end-page"),
                )
                self.assertEqual(rewrite.kwargs, {})
                self.assertEqual(
                    fx.named("prepare_env")[0].args,
                    ("literal-output-dir", "literal-file-name", directory),
                )
                analyze = fx.named("analyze")[0]
                self.assertEqual(analyze.args, (b"literal-rewritten-pdf",))
                self.assertEqual(
                    analyze.kwargs, {"image_writer": fx.writers[0], **analyze_kwargs}
                )
                self.assertEqual(
                    [writer.directory for writer in fx.writers],
                    [fx.IMAGE_DIRECTORY, fx.MARKDOWN_DIRECTORY],
                )
                output = fx.named("process_output")[0]
                self.assertEqual(
                    output.args,
                    (
                        ["literal-page-info"],
                        b"literal-rewritten-pdf",
                        "literal-file-name",
                        fx.MARKDOWN_DIRECTORY,
                        fx.IMAGE_DIRECTORY,
                        fx.writers[1],
                        "literal-draw-layout",
                        False,
                        "literal-dump-orig",
                        "literal-dump-md",
                        "literal-dump-content",
                        "literal-dump-middle",
                        "literal-dump-model",
                        "literal-md-mode",
                        {"pdf_info": ["literal-page-info"]},
                        ["literal-infer-result"],
                    ),
                )
                self.assertEqual(output.kwargs, {"process_mode": "vlm"})
                self.assertEqual(fx.namespace["os"].environ, environment)

    def test_cli_async_entrypoints_await_every_blocking_seam_off_the_loop(self):
        tree = ast.parse(self.sources["cli"])
        entry = _named_function(tree, "aio_do_parse")
        self.assertIsInstance(entry, ast.AsyncFunctionDef)
        self.assertEqual(
            ast.unparse(_single_owned_thread_await(entry, "_prepare_pdf_bytes")),
            "await to_thread_owned(_prepare_pdf_bytes, pdf_bytes_list, "
            "start_page_id, end_page_id)",
        )
        self.assertEqual(_calls_to(entry, "_prepare_pdf_bytes"), [])
        # Office conversion keeps the upstream asyncio.to_thread hand-off.
        self.assertEqual(
            [
                ast.unparse(call.args[0])
                for call in _calls_to(entry, "asyncio.to_thread")
            ],
            ["_process_office_doc"],
        )

        for name, directory in (
            ("_async_process_vlm", "parse_method"),
            ("_async_process_hybrid", "f'hybrid_{parse_method}'"),
        ):
            with self.subTest(processor=name):
                node = _named_function(tree, name)
                self.assertIsInstance(node, ast.AsyncFunctionDef)
                self.assertEqual(
                    ast.unparse(_single_owned_thread_await(node, "prepare_env")),
                    "await to_thread_owned(prepare_env, output_dir, pdf_file_name, "
                    f"{directory})",
                )
                self.assertEqual(
                    _single_owned_thread_await(node, "_process_output")
                    .value.keywords[0]
                    .arg,
                    "process_mode",
                )
                self.assertEqual(_calls_to(node, "prepare_env"), [])
                self.assertEqual(_calls_to(node, "_process_output"), [])

        # The synchronous entry points are outside the serving profile and keep
        # their direct, loop-free calls.
        for name, expected in (
            ("do_parse", {"_prepare_pdf_bytes": 1}),
            ("_process_vlm", {"prepare_env": 1, "_process_output": 1}),
            ("_process_hybrid", {"prepare_env": 1, "_process_output": 1}),
            ("_process_pipeline", {"prepare_env": 1, "_process_output": 1}),
            ("_process_office_doc", {"prepare_env": 1, "_process_output": 1}),
        ):
            with self.subTest(synchronous=name):
                node = _named_function(tree, name)
                self.assertIsInstance(node, ast.FunctionDef)
                self.assertEqual(
                    {
                        target: len(_calls_to(node, target))
                        for target in expected
                    },
                    expected,
                )
                self.assertEqual(
                    [
                        target
                        for target in (
                            "_prepare_pdf_bytes",
                            "prepare_env",
                            "_process_output",
                        )
                        if _owned_thread_calls(node, target)
                    ],
                    [],
                )


if __name__ == "__main__":
    unittest.main()
