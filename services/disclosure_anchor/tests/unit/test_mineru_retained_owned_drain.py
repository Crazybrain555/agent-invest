"""Retained builder cancellation exercises actual generated IO and owned threads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unittest
import zipfile

from tests._mineru_owned_drain_fixture import generated_sources, loop_turns
from tests._mineru_retained_drain_fixture import RetainedFixture


class RetainedOwnedDrainTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    async def cancellation(self, phase, *, verify_error=None):
        fx = RetainedFixture(self.sources, block=phase, verify_error=verify_error)
        task = fx.start()
        try:
            await asyncio.wait_for(fx.barrier.started.wait(), 2)
            task.cancel("retained-original-cancel")
            await loop_turns()
            task.cancel("retained-repeat-cancel")
            await loop_turns()
            evidence = {
                "phase": phase,
                "task_done": task.done(),
                "native_finished": fx.barrier.finished.is_set(),
                "original_fds_alive": [fx.fd_alive(fd) for fd in fx.descriptors],
                "closes": list(fx.closes.values()),
                "events": list(fx.events),
            }
            print(
                "RETAINED_BEFORE_RELEASE " + json.dumps(evidence, sort_keys=True),
                flush=True,
            )
            self.assertFalse(task.done(), evidence)
            self.assertFalse(fx.barrier.finished.is_set())
            if phase != "hash":
                self.assertEqual(len(fx.descriptors), 2)
                self.assertTrue(all(fx.fd_alive(fd) for fd in fx.descriptors))
                self.assertEqual(list(fx.closes.values()), [0, 0])
            else:
                self.assertTrue((fx.root / ".retained-result.zip").is_file())
                self.assertEqual(list(fx.closes.values()), [1, 1])
            self.assertFalse(any(event.startswith("unlink:") for event in fx.events))
            fx.barrier.release.set()
            done, _ = await asyncio.wait({task}, timeout=2)
            self.assertIn(task, done)
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertEqual(caught.exception.args, ("retained-original-cancel",))
            if verify_error is not None:
                self.assertIs(caught.exception.__cause__, verify_error)
            self.assertTrue(fx.barrier.finished.is_set())
            self.assertEqual(list(fx.closes.values()), [1, 1])
            self.assertFalse(any(fx.fd_alive(fd) for fd in fx.descriptors))
            self.assertIsNone(fx.task.result_artifact_path)
            self.assertFalse((fx.root / ".retained-result.zip").exists())
            self.assertFalse((fx.root / ".retained-result.zip.part").exists())
        finally:
            await fx.close(task)

    async def test_acquire_cancel_closes_returned_original_fds_after_native_finishes(
        self,
    ):
        await self.cancellation("acquire")

    async def test_zip_write_cancel_keeps_source_fds_and_part_until_native_finishes(
        self,
    ):
        await self.cancellation("write")

    async def test_verify_cancel_consumes_each_descriptor_once(self):
        await self.cancellation("verify")

    async def test_hash_cancel_retains_zip_until_native_finishes(self):
        await self.cancellation("hash")

    async def test_verify_failure_after_cancel_is_original_cause_and_all_fds_close(
        self,
    ):
        await self.cancellation("verify", verify_error=OSError("literal-verify-error"))

    async def test_verify_failure_without_cancel_preserves_first_error_not_ebadf(self):
        failure = OSError("literal-original-stat-error")
        fx = RetainedFixture(self.sources, verify_error=failure)
        task = fx.start()
        try:
            with self.assertRaises(OSError) as caught:
                await asyncio.wait_for(task, 2)
            self.assertIs(caught.exception, failure)
            self.assertEqual(list(fx.closes.values()), [1, 1])
            self.assertFalse(any(fx.fd_alive(fd) for fd in fx.descriptors))
        finally:
            await fx.close(task)

    async def test_consumed_reused_fd_is_not_closed_again_and_other_fd_closes(self):
        failure = OSError("literal-consumed-close-error")
        fx = RetainedFixture(self.sources, close_error=failure)
        task = fx.start()
        try:
            with self.assertRaises(OSError) as caught:
                await asyncio.wait_for(task, 2)
            self.assertIs(caught.exception, failure)
            self.assertEqual(list(fx.closes.values()), [1, 1])
            self.assertIsNotNone(fx.replacement)
            self.assertEqual(
                os.fstat(fx.replacement).st_ino,
                (fx.root / "replacement.txt").stat().st_ino,
            )
            self.assertFalse(any(fx.fd_alive(fd) for fd in fx.descriptors))
        finally:
            await fx.close(task)

    async def test_normal_zip_members_bytes_hash_and_owner_are_literal_source_based(
        self,
    ):
        fx = RetainedFixture(self.sources)
        task = fx.start()
        try:
            await asyncio.wait_for(task, 2)
            path = fx.root / ".retained-result.zip"
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(
                    archive.namelist(), ["source.md", "source_middle.json"]
                )
                for name, value in fx.inputs.items():
                    self.assertEqual(archive.read(name), value)
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(fx.task.result_artifact_path, str(path))
            self.assertEqual(fx.task.result_artifact_sha256, digest)
            self.assertEqual(fx.task.result_artifact_bytes, len(raw))
            self.assertEqual(
                fx.task.result_artifact_owner,
                hashlib.sha256(
                    f"literal-task\0{digest}\0{len(raw)}".encode()
                ).hexdigest(),
            )
            self.assertEqual(list(fx.closes.values()), [1, 1])
            self.assertFalse(any(fx.fd_alive(fd) for fd in fx.descriptors))
            self.assertEqual(fx.events[:2], ["acquire:start", "acquire:end"])
            self.assertEqual(
                [event for event in fx.events if event.endswith(":start")],
                ["acquire:start", "write:start", "verify:start", "hash:start"],
            )
        finally:
            await fx.close(task)

    async def test_io_error_and_consumed_close_error_keep_primary_and_close_all(self):
        for phase in ("write", "acquire"):
            with self.subTest(phase=phase):
                primary = OSError("literal-original-" + phase + "-error")
                secondary = OSError("literal-secondary-close-error")
                fx = RetainedFixture(
                    self.sources, **{phase + "_error": primary}, close_error=secondary
                )
                task = fx.start()
                try:
                    with self.assertRaises(OSError) as caught:
                        await asyncio.wait_for(task, 2)
                    self.assertIs(caught.exception, primary)
                    self.assertEqual(
                        list(fx.closes.values()), [1, 1] if phase == "write" else [1]
                    )
                    self.assertFalse(any(fx.fd_alive(fd) for fd in fx.descriptors))
                    self.assertEqual(
                        os.fstat(fx.replacement).st_ino,
                        (fx.root / "replacement.txt").stat().st_ino,
                    )
                    errors = []
                    pending = [caught.exception]
                    seen = set()
                    while pending:
                        exc = pending.pop()
                        if id(exc) in seen:
                            continue
                        seen.add(id(exc))
                        errors.append(str(exc))
                        errors.extend(getattr(exc, "__notes__", ()))
                        pending.extend(
                            value
                            for value in (exc.__cause__, exc.__context__)
                            if value is not None
                        )
                    self.assertIn(str(secondary), "\n".join(errors))
                finally:
                    await fx.close(task)
