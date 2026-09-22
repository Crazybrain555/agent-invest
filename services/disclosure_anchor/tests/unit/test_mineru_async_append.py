"""Threaded append contract through real generated aio and exact upstream append bodies."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tests._mineru_async_append_fixture import AppendFixture
from tests._mineru_owned_drain_fixture import (
    NativeFailure,
    generated_sources,
    loop_turns,
)


class HybridAsyncAppendTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    async def test_slow_append_allows_independent_loop_progress_before_release(self):
        with tempfile.TemporaryDirectory() as root:
            fx = AppendFixture(self.sources, Path(root), hold=True)
            task = fx.start()
            try:
                await asyncio.wait_for(fx.hold.entered.wait(), 3)
                advanced = []

                async def independent_coroutine():
                    advanced.append("loop-progress")

                await asyncio.create_task(independent_coroutine())
                snapshot = {
                    "loop_advanced": advanced,
                    "append_finished": fx.hold.finished.is_set(),
                    "watchdog_released": fx.hold.watchdog_released,
                    "document_done": task.done(),
                    "append_thread": fx.hold.thread_id,
                    "loop_thread": threading.get_ident(),
                }
                print("APPEND_BEFORE_RELEASE " + json.dumps(snapshot), flush=True)
                self.assertFalse(fx.hold.finished.is_set(), snapshot)
                self.assertNotEqual(fx.hold.thread_id, threading.get_ident())
                self.assertFalse(task.done())
                self.assertEqual(fx.pdf.closes, 0)
                self.assertTrue(all(image.closes == 0 for image in fx.images))
                self.assertNotIn("phase-end:window_append", fx.events)
                fx.hold.release.set()
                await asyncio.wait_for(asyncio.shield(task), 3)
                self.assertFalse(fx.hold.watchdog_released)
            finally:
                await fx.settle(task)

    async def test_repeated_cancel_drains_append_before_image_pdf_and_writer_release(
        self,
    ):
        for failure in (
            None,
            NativeFailure("append failed while cancellation pending"),
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                fx = AppendFixture(self.sources, Path(root), hold=True, failure=failure)
                task = fx.start()
                try:
                    await asyncio.wait_for(fx.hold.entered.wait(), 3)
                    task.cancel("first-document-cancellation")
                    await loop_turns()
                    task.cancel("later-cancellation")
                    await loop_turns()
                    self.assertFalse(task.done())
                    self.assertFalse(fx.hold.finished.is_set())
                    self.assertEqual(fx.pdf.closes, 0)
                    self.assertTrue(all(i.closes == 0 for i in fx.images))
                    self.assertEqual(fx.page_handles[0].closes, 0)
                    self.assertEqual(list(Path(root).iterdir()), [])
                    fx.hold.release.set()
                    done, _ = await asyncio.wait({task}, timeout=3)
                    self.assertIn(task, done)
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertEqual(
                        caught.exception.args, ("first-document-cancellation",)
                    )
                    self.assertIs(caught.exception.__cause__, failure)
                    self.assertEqual(fx.pdf.closes, 1)
                    self.assertTrue(all(i.closes == 1 for i in fx.images))
                    self.assertTrue(all(p.closes == 1 for p in fx.page_handles))
                    self.assertLess(
                        fx.events.index("append-held:leave"),
                        fx.events.index("image-0:close"),
                    )
                    self.assertLess(
                        fx.events.index("image-0:close"), fx.events.index("pdf:close")
                    )
                    self.assertNotIn("document:completed", fx.events)
                    self.assertNotIn("phase-end:window_append", fx.events)
                    self.assertNotIn("render:16", fx.events)
                    self.assertNotIn("finalize", fx.events)
                    if failure is None:
                        self.assertEqual(len(list(Path(root).iterdir())), 16)
                    else:
                        self.assertEqual(list(Path(root).iterdir()), [])
                    self.assertFalse(fx.hold.watchdog_released)
                finally:
                    await fx.settle(task)

    async def test_two_windows_preserve_exact_pages_arguments_phase_and_cleanup_order(
        self,
    ):
        for effort, ocr, client in (
            ("medium", False, False),
            ("medium", True, True),
            ("high", False, True),
            ("high", True, False),
        ):
            with (
                self.subTest(effort=effort, ocr=ocr, client=client),
                tempfile.TemporaryDirectory() as root,
            ):
                fx = AppendFixture(
                    self.sources, Path(root), effort=effort, ocr=ocr, client=client
                )
                result = await asyncio.wait_for(fx.start(), 4)
                self.assertEqual(
                    result,
                    (
                        {
                            "pdf_info": [
                                {"page_idx": n, "literal": f"source-page:{n}"}
                                for n in range(17)
                            ]
                        },
                        [
                            {"source_page": n, "literal": "unchanged-model"}
                            for n in range(17)
                        ],
                    ),
                )
                self.assertEqual([v[0] for v in fx.page_info_calls], list(range(17)))
                self.assertTrue(
                    all(v[1] != threading.get_ident() for v in fx.page_info_calls)
                )
                self.assertEqual(fx.progress.updates, [1] * 17)
                self.assertEqual(len(fx.append_arguments), 2)
                for index, (args, kwargs) in enumerate(fx.append_arguments):
                    self.assertEqual(len(args), 5)
                    self.assertIs(args[0], result[0])
                    self.assertIs(args[3], fx.pdf)
                    self.assertIs(args[4], fx.writer)
                    self.assertEqual(len(args[1]), 16 if index == 0 else 1)
                    self.assertEqual(
                        kwargs,
                        {
                            "page_start_index": index * 16,
                            "_ocr_enable": ocr,
                            "progress_bar": fx.progress,
                        },
                    )
                for n in range(17):
                    self.assertEqual(
                        (Path(root) / f"page-{n}.bin").read_bytes(),
                        f"literal-page:{n}".encode(),
                    )
                appends = [r for r in fx.phase.records if r[0] == "window_append"]
                self.assertEqual(
                    [r[2] for r in appends],
                    [
                        {
                            "window": {
                                "window_index": 0,
                                "page_start": 0,
                                "page_end_exclusive": 16,
                            },
                            "append_index": 0,
                        },
                        {
                            "window": {
                                "window_index": 1,
                                "page_start": 16,
                                "page_end_exclusive": 17,
                            },
                            "append_index": 1,
                        },
                    ],
                )
                for index, record in enumerate(appends):
                    start = index * 16
                    began = fx.events.index(f"phase-start:{record[1]}")
                    called = fx.events.index(f"append-call:{start}")
                    returned = fx.events.index(f"append-return:{start}")
                    completed = fx.events.index("phase-end:window_append", returned)
                    closed = fx.events.index(f"image-{start}:close")
                    trimmed = fx.events.index("trim", closed)
                    total = fx.events.index("phase-end:window_total", trimmed)
                    self.assertEqual(
                        sorted(
                            [began, called, returned, completed, closed, trimmed, total]
                        ),
                        [began, called, returned, completed, closed, trimmed, total],
                    )
                    if index == 0:
                        self.assertLess(total, fx.events.index("render:16"))
                self.assertEqual((fx.pdf.closes, fx.progress.closes), (1, 1))
                self.assertTrue(all(i.closes == 1 for i in fx.images + fx.page_handles))
                finalizer = "server_finalize" if client else "finalize"
                self.assertLess(
                    fx.events.index("image-16:close"), fx.events.index(finalizer)
                )
                self.assertLess(
                    fx.events.index(finalizer), fx.events.index("pdf:close")
                )
                # Document end closes the pdfium document and releases the
                # allocator caches and heap in one owned-pool call, then closes
                # the phase trace on the loop. The close is no longer a serving
                # loop statement, but it still precedes the release exactly as
                # it did before either moved off the loop.
                self.assertEqual(
                    fx.events[-3:],
                    ["pdf:close", "release-document-memory", "document:completed"],
                )
                self.assertEqual(fx.events.count("pdf:close"), 1)

    async def test_append_error_is_original_and_partial_output_is_not_success(self):
        error = NativeFailure("literal second-page append failure")
        with tempfile.TemporaryDirectory() as root:
            fx = AppendFixture(self.sources, Path(root), failure=error, failure_page=1)
            task = fx.start()
            with self.assertRaises(NativeFailure) as caught:
                await asyncio.wait_for(asyncio.shield(task), 4)
            self.assertIs(caught.exception, error)
            self.assertEqual(
                fx.middle, {"pdf_info": [{"page_idx": 0, "literal": "source-page:0"}]}
            )
            self.assertEqual([p.name for p in Path(root).iterdir()], ["page-0.bin"])
            self.assertEqual([v[0] for v in fx.page_info_calls], [0, 1])
            self.assertTrue(all(p.closes == 1 for p in fx.page_handles + fx.images))
            self.assertEqual(fx.pdf.closes, 1)
            self.assertNotIn("render:16", fx.events)
            self.assertNotIn("finalize", fx.events)
            self.assertNotIn("phase-end:window_append", fx.events)
            self.assertNotIn("document:completed", fx.events)
            self.assertIn("document:failed", fx.events)
