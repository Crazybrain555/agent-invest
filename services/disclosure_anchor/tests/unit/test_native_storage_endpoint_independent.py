"""Independent synthetic checks of generated MinerU storage endpoint boundaries."""

from __future__ import annotations

from abc import ABC, abstractmethod
import ast
import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import File, UploadFile

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as native
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import patch_source
from tests._mineru_service_io_asgi_fixture import ServiceIOASGIFixture


WRITER_PREIMAGE_SHA256 = {
    "mineru/data/data_reader_writer/base.py": (
        "85eac3891bb6dc3be171dc6d5a18abd9a8cb1b592458fd218d68e4c255999803"
    ),
    "mineru/data/data_reader_writer/filebase.py": (
        "c047bfd6a588095bf68c0c50204f10c9a6bce2d014a6065f26dc241acbe03e2c"
    ),
    "mineru/cli/common.py": (
        "d1e23e310bddc3da2d7f491be81ef112435824403d1c3a29e438505c1707dbc5"
    ),
}


def pinned_preimage(fixture_root: Path, name: str) -> str:
    raw = (fixture_root / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != WRITER_PREIMAGE_SHA256[name]:
        raise AssertionError(f"pinned MinerU 3.4.4 preimage changed: {name}")
    return raw.decode("utf-8")


def granted_writer_class():
    """Execute only the exact wheel writer and generated common.py wrapper classes."""
    service = Path(__file__).resolve().parents[2]
    fixture_root = service / "tests/fixtures/mineru_344_preimages"
    base = pinned_preimage(fixture_root, "mineru/data/data_reader_writer/base.py")
    filebase = pinned_preimage(fixture_root, "mineru/data/data_reader_writer/filebase.py")
    common = pinned_preimage(fixture_root, "mineru/cli/common.py")
    generated = patch_source("mineru/cli/common.py", common)
    namespace = {
        "ABC": ABC, "abstractmethod": abstractmethod, "os": os,
        "current_source_growth_permit": native.current_source_growth_permit,
        "storage_managed_output_required": native.storage_managed_output_required,
    }
    for source, name in ((base, "DataWriter"), (filebase, "FileBasedDataWriter")):
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == name]
        if len(nodes) != 1:
            raise AssertionError(f"pinned wheel class {name} changed")
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<exact-mineru-3.4.4-writer>", "exec"), namespace)
    namespace["_UngrantedFileBasedDataWriter"] = namespace["FileBasedDataWriter"]
    nodes = [node for node in ast.parse(generated).body
             if isinstance(node, ast.ClassDef) and node.name == "FileBasedDataWriter"]
    if len(nodes) != 1:
        raise AssertionError("generated common.py granted writer changed")
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<generated-mineru-granted-writer>", "exec"), namespace)
    return namespace["FileBasedDataWriter"]


class NativeGrantedWriterIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-endpoint-writer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.writer = granted_writer_class()(str(self.root))

    def permit(self):
        return native.SourceGrowthPermit(
            root=self.root, limit_bytes=4096, allocation_unit=16, file_overhead=4,
        )

    def test_generated_writer_refuses_overwrite_before_open(self):
        target = self.root / "existing.txt"
        target.write_bytes(b"prior")
        allowance = 8 * (16 + 4)
        permit = native.SourceGrowthPermit(
            root=self.root, limit_bytes=allowance + 15,
            allocation_unit=16, file_overhead=4,
        )
        with native.bind_source_growth_permit(permit):
            with self.assertRaises(native.SourceGrowthLimitExceeded):
                self.writer.write("existing.txt", b"x" * 17)
        self.assertEqual(target.read_bytes(), b"prior")

    def test_generated_writer_rejects_parent_symlink_and_hardlink_before_bytes(self):
        real_parent = self.root / "document" / "auto"
        real_parent.mkdir(parents=True)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        hardlink = self.root / "hardlink.txt"
        os.link(outside, hardlink)
        for label, name, changed in (
            ("parent symlink", "linked-parent/new.txt", real_parent / "new.txt"),
            ("hardlink leaf", "hardlink.txt", outside),
        ):
            with self.subTest(path=label), native.bind_source_growth_permit(self.permit()):
                try:
                    self.writer.write(name, b"changed")
                except (native.SourceGrowthLimitExceeded, native.TaskProtocolConflict, OSError):
                    rejected = True
                else:
                    rejected = False
                self.assertEqual((rejected, changed.exists(), outside.read_bytes()),
                                 (True, label == "hardlink leaf", b"outside"))

    def test_granted_path_replaced_with_link_cannot_change_aliased_bytes(self):
        owned = self.root / "owned"
        owned.mkdir()
        writer = self.writer.__class__(str(owned))
        for kind in ("symlink", "hardlink"):
            with self.subTest(replacement=kind):
                target = owned / f"cached-{kind}.txt"
                outside = self.root / f"outside-{kind}.txt"
                outside.write_bytes(b"unchanged")
                permit = native.SourceGrowthPermit(
                    root=owned, limit_bytes=4096, allocation_unit=16, file_overhead=4,
                )
                with native.bind_source_growth_permit(permit):
                    writer.write(target.name, b"first")
                    self.assertEqual(target.read_bytes(), b"first")
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(outside)
                    else:
                        os.link(outside, target)
                    try:
                        writer.write(target.name, b"changed")
                    except Exception:
                        pass  # any refusal is valid if it precedes aliased bytes
                self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_leaf_swap_after_permit_approval_cannot_change_aliased_bytes(self):
        owned = self.root / "owned"
        owned.mkdir()
        outside = self.root / "outside-seam.txt"
        outside.write_bytes(b"unchanged")
        target = owned / "seam.txt"
        writer = self.writer.__class__(str(owned))
        permit = native.SourceGrowthPermit(
            root=owned, limit_bytes=4096, allocation_unit=16, file_overhead=4,
        )
        original_before_write = permit.before_write

        def approved_then_swapped(path: str, size: int) -> None:
            original_before_write(path, size)
            target.symlink_to(outside)

        root_identity = (owned.stat().st_dev, owned.stat().st_ino)
        with native.bind_source_growth_permit(permit), patch.object(
            permit, "before_write", side_effect=approved_then_swapped
        ):
            try:
                writer.write(target.name, b"changed")
            except Exception:
                pass  # a post-approval identity refusal is valid
        self.assertEqual((owned.stat().st_dev, owned.stat().st_ino), root_identity)
        self.assertEqual(outside.read_bytes(), b"unchanged")


class NativeResultASGIIndependentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = ServiceIOASGIFixture()
        self.addAsyncCleanup(self.fx.close)
        await self.fx.start()

    @unittest.skipUnless(importlib.util.find_spec("python_multipart"),
                         "python-multipart is absent from this test interpreter")
    async def test_prebody_multipart_limit_precedes_starlette_spool_write(self):
        import starlette.formparsers as formparsers

        self.fx.manager.result_storage_policy = SimpleNamespace(source_pdf_bytes_limit=1024)
        accepted = []

        @self.fx.app.post("/tasks")
        async def synthetic_upload(files: list[UploadFile] = File(...)):
            accepted.append(len(await files[0].read()))
            return {"accepted": True}

        spool_dir = self.fx.root / "synthetic-spool"
        spool_dir.mkdir()
        spool_writes = []
        rolled_to_disk = []

        class CountingSpool(tempfile.SpooledTemporaryFile):
            def write(self, data):
                spool_writes.append(len(data))
                count = super().write(data)
                rolled_to_disk.append(self._rolled)
                return count

        with patch.object(tempfile, "tempdir", str(spool_dir)), patch.object(
            formparsers, "SpooledTemporaryFile", CountingSpool
        ):
            response = await self.fx.client.post(
                "/tasks", files={"files": ("synthetic.pdf", b"x" * ((1 << 20) + 17), "application/pdf")},
            )
        self.assertEqual(
            (response.status_code in {413, 429}, accepted == [], spool_writes == []),
            (True, True, True),
            f"status={response.status_code}, route={accepted}, spool_writes={spool_writes}, "
            f"rolled={rolled_to_disk}",
        )

    async def test_strong_validator_ranges_and_failed_send_release_pin(self):
        task = await self.fx.completed("endpoint-range")
        self.fx.manager.result_storage_policy = SimpleNamespace(source_pdf_bytes_limit=1024)
        raw = Path(task.result_artifact_path).read_bytes()
        path = f"/tasks/{task.task_id}/result"
        expected_etag = f'"{task.result_artifact_sha256}"'

        full = await self.fx.client.get(path)
        self.assertEqual((full.status_code, full.content, full.headers.get("etag")),
                         (200, raw, expected_etag))
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

        ranged = await self.fx.client.get(path, headers={"Range": "bytes=0-7", "If-Range": expected_etag})
        self.assertEqual((ranged.status_code, ranged.content), (206, raw[:8]))
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

        changed = await self.fx.client.get(path, headers={"Range": "bytes=0-7", "If-Range": '"old"'})
        self.assertEqual((changed.status_code, changed.content), (200, raw))
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

        for header, status in (("bytes=not-a-range", 400), (f"bytes={len(raw)}-", 416)):
            with self.subTest(range=header):
                response = await self.fx.client.get(path, headers={"Range": header})
                self.assertEqual(response.status_code, status)
                self.assertEqual(
                    self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0
                )

        failure = OSError("synthetic ASGI send failure")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                raise failure

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": [],
            "server": ("service.test", 80), "client": ("127.0.0.1", 1234),
            "root_path": "", "extensions": {},
        }
        with self.assertRaises(OSError) as caught:
            await asyncio.wait_for(self.fx.app(scope, receive, send), 2)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)


if __name__ == "__main__":
    unittest.main()
