"""Actual generated hybrid/VLM wiring, without native model or provider imports."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest

from tests._mineru_owned_drain_fixture import (
    DocumentFixture,
    NativeFailure,
    generated_sources,
    loop_turns,
)


class NativeOwnedDrainTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    async def exercise_cancel(
        self,
        *,
        stage,
        kind="hybrid",
        effort="medium",
        ocr=False,
        client=False,
        repeated=False,
        native_error=None,
    ):
        fx = DocumentFixture(
            self.sources,
            block=stage,
            error=native_error,
            kind=kind,
            effort=effort,
            ocr=ocr,
            client=client,
        )
        task = fx.start(create_model=stage == "get_model")
        cancelled = None
        try:
            await asyncio.wait_for(fx.barrier.started.wait(), 2)
            self.assertNotEqual(fx.barrier.thread_id, threading.get_ident())
            task.cancel("original-owner-cancel")
            await loop_turns()
            if repeated:
                task.cancel("later-cancel-must-not-replace-original")
                await loop_turns()
            before = {
                "kind": kind,
                "stage": stage,
                "effort": effort,
                "ocr": ocr,
                "task_done": task.done(),
                "native_finished": fx.barrier.finished.is_set(),
                "image_closes": fx.image.closes,
                "pdf_closes": fx.pdf.closes,
            }
            print(
                "OWNERSHIP_BEFORE_RELEASE " + json.dumps(before, sort_keys=True),
                flush=True,
            )
            self.assertFalse(fx.barrier.finished.is_set())
            self.assertFalse(task.done(), before)
            self.assertEqual(fx.pdf.closes, 0, before)
            expected_images = 1 if stage in {"finalize", "server_finalize"} else 0
            self.assertEqual(fx.image.closes, expected_images, before)
            fx.barrier.release.set()
            try:
                done, _ = await asyncio.wait({task}, timeout=2)
                self.assertIn(
                    task, done, "native operation did not settle after release"
                )
                await task
            except asyncio.CancelledError as exc:
                cancelled = exc
            self.assertIsNotNone(cancelled)
            self.assertEqual(cancelled.args, ("original-owner-cancel",))
            self.assertTrue(fx.barrier.finished.is_set())
            if native_error is not None:
                self.assertIs(cancelled.__cause__, native_error)
                self.assertIn(
                    "owned operation drain failed: NativeFailure", cancelled.__notes__
                )
            self.assertEqual(fx.pdf.closes, 0 if stage == "get_model" else 1)
            self.assertEqual(fx.image.closes, 0 if stage == "get_model" else 1)
            if stage != "get_model":
                self.assertLess(
                    fx.events.index(stage + ":finished"), fx.events.index("pdf:close")
                )
                self.assertNotIn("document:completed", fx.events)
            print(
                "OWNERSHIP_AFTER_RELEASE "
                + json.dumps({"stage": stage, "events": fx.events}),
                flush=True,
            )
        finally:
            await fx.settle(task)

    async def test_layout_cancel_keeps_original_image_pdf_and_await_owned(self):
        await self.exercise_cancel(stage="layout")

    async def test_all_remaining_hybrid_thread_calls_hold_their_resources(self):
        for stage, effort, ocr, client in (
            ("orientation", "medium", False, False),
            ("sidecar", "medium", True, False),
            ("sidecar", "high", True, False),
            ("ocr", "medium", False, False),
            ("ocr", "high", False, False),
            ("title", "medium", False, False),
            ("finalize", "medium", False, False),
            ("server_finalize", "medium", False, True),
        ):
            with self.subTest(stage=stage, effort=effort, ocr=ocr, client=client):
                await self.exercise_cancel(
                    stage=stage, effort=effort, ocr=ocr, client=client
                )

    async def test_repeated_cancel_preserves_first_cancel_and_drains_once(self):
        await self.exercise_cancel(stage="layout", repeated=True)

    async def test_cancel_then_native_failure_keeps_both_original_errors(self):
        await self.exercise_cancel(
            stage="ocr",
            native_error=NativeFailure("native-literal-error"),
            repeated=True,
        )

    async def test_vlm_finalizer_cancel_keeps_pdf_until_actual_completion(self):
        await self.exercise_cancel(stage="finalize", kind="vlm")

    async def test_vlm_model_creation_cancel_drains_without_opening_document(self):
        await self.exercise_cancel(stage="get_model", kind="vlm", repeated=True)

    async def test_normal_hybrid_result_arguments_and_order_are_unchanged(self):
        for effort, ocr, client in (
            ("medium", False, False),
            ("medium", True, False),
            ("high", False, False),
            ("high", True, True),
        ):
            with self.subTest(effort=effort, ocr=ocr, client=client):
                fx = DocumentFixture(
                    self.sources, effort=effort, ocr=ocr, client=client
                )
                result = await asyncio.wait_for(fx.start(), 2)
                self.assertEqual(
                    result,
                    (
                        {"pdf_info": [{"page": "literal-appended"}]},
                        [{"page": "literal-result"}],
                    ),
                )
                calls = [call[0] for call in fx.calls]
                self.assertEqual(
                    calls,
                    ["layout"]
                    + (["orientation"] if effort == "medium" else [])
                    + [
                        "sidecar" if ocr else "ocr",
                        "title",
                        "server_finalize" if client else "finalize",
                    ],
                )
                layout_args = fx.calls[0][1]
                self.assertIs(layout_args[0][0], fx.image)
                self.assertEqual(layout_args[1:], (True, 1, ocr))
                self.assertEqual(fx.pdf.closes, 1)
                self.assertEqual(fx.image.closes, 1)
                self.assertEqual(fx.progress.closes, 1)
                self.assertLess(
                    fx.events.index("append"), fx.events.index("image:close")
                )
                self.assertLess(
                    fx.events.index(calls[-1]), fx.events.index("pdf:close")
                )
                self.assertNotIn("document:failed", fx.events)

    async def test_native_error_without_cancel_propagates_original_once(self):
        error = NativeFailure("ordinary-native-error")
        fx = DocumentFixture(self.sources, block="layout", error=error)
        task = fx.start()
        try:
            await asyncio.wait_for(fx.barrier.started.wait(), 2)
            fx.barrier.release.set()
            with self.assertRaises(NativeFailure) as caught:
                await asyncio.wait_for(asyncio.shield(task), 2)
            self.assertIs(caught.exception, error)
            self.assertEqual(fx.image.closes, 1)
            self.assertEqual(fx.pdf.closes, 1)
        finally:
            try:
                await fx.settle(task)
            except NativeFailure:
                pass

    async def test_unrelated_document_threads_can_enter_without_new_global_lock(self):
        first = DocumentFixture(self.sources, block="layout")
        second = DocumentFixture(self.sources, block="layout")
        tasks = [first.start(), second.start()]
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    first.barrier.started.wait(), second.barrier.started.wait()
                ),
                2,
            )
            self.assertFalse(first.barrier.finished.is_set())
            self.assertFalse(second.barrier.finished.is_set())
            self.assertNotEqual(first.barrier.thread_id, second.barrier.thread_id)
        finally:
            first.barrier.release.set()
            second.barrier.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)

    async def test_normal_vlm_both_finalization_modes_keep_return_value(self):
        for client in (False, True):
            with self.subTest(client=client):
                fx = DocumentFixture(self.sources, kind="vlm", client=client)
                result = await asyncio.wait_for(fx.start(), 2)
                self.assertEqual(
                    result,
                    (
                        {"pdf_info": [{"page": "literal-appended"}]},
                        [{"page": "literal-result"}],
                    ),
                )
                self.assertEqual(
                    [v[0] for v in fx.calls], [] if client else ["finalize"]
                )
                self.assertEqual((fx.image.closes, fx.pdf.closes), (1, 1))

    async def test_hybrid_render_result_cancel_returns_images_before_pdf_close(self):
        await self.exercise_cancel(stage="render", repeated=True)

    async def test_vlm_render_result_cancel_returns_images_before_pdf_close(self):
        await self.exercise_cancel(stage="render", kind="vlm", repeated=True)
