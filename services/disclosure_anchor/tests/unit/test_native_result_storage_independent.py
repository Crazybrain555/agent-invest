"""Independent synthetic file witnesses for the native result-storage core."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from disclosure_anchor.application.contracts.mineru_capacity_config import retained_zip_upper_bound
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as native


def root_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "uid": stat.st_uid, "mode": stat.st_mode}


@contextmanager
def owned_descriptors():
    """Track every descriptor opened by a native helper, even on failure."""

    original_open, original_close = os.open, os.close
    live: set[int] = set()

    def opened(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        if descriptor in live:
            raise AssertionError("descriptor was reused while still owned")
        live.add(descriptor)
        return descriptor

    def closed(descriptor):
        original_close(descriptor)
        if descriptor not in live:
            raise AssertionError("native helper closed a descriptor it did not own")
        live.remove(descriptor)

    with patch.object(native.os, "open", side_effect=opened), patch.object(
        native.os, "close", side_effect=closed
    ):
        yield live
    if live:
        raise AssertionError(f"native helper leaked {len(live)} descriptors")


class NativeResultStorageIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="native-storage-independent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parse = self.root / "document" / "hybrid_auto"
        self.parse.mkdir(parents=True)
        (self.parse / "images").mkdir()
        self.middle = self.parse / "document_middle.json"
        self.middle.write_bytes(b'{"page":1,"text":"alpha"}')
        (self.parse / "images" / "crop.jpg").write_bytes(b"synthetic-jpeg-bytes")
        self.identity = root_identity(self.root)
        self.selection = (
            native.ResultSelection(
                pdf_name="document",
                parse_dir_parts=("document", "hybrid_auto"),
                arc_prefix="document",
                named_files=("document_middle.json",),
                image_suffixes=frozenset({"jpg"}), origin_prefix=None,
            ),
        )
        self.policy = SimpleNamespace(
            sha256="sha256:" + "a" * 64,
            max_members=10,
            max_name_bytes=256,
            max_inventory_bytes=64 * 1024,
        )

    def inventory(self) -> native.ResultInventory:
        return native.build_result_inventory(
            task_id="synthetic-task", task_root=self.root, root_identity=self.identity,
            selections=self.selection, policy=self.policy,
            zip_upper_bound=retained_zip_upper_bound,
        )

    def test_growth_refusal_happens_before_new_file_or_overwrite(self) -> None:
        allowance = 8 * (16 + 4)
        permit = native.SourceGrowthPermit(
            root=self.root, limit_bytes=allowance + 20,
            allocation_unit=16, file_overhead=4,
        )
        target = self.root / "generated.txt"
        permit.before_write(str(target), 5)
        target.write_bytes(b"first")
        self.assertEqual(permit.charged_bytes, allowance + 20)
        permit.before_write(str(target), 5)
        target.write_bytes(b"again")
        self.assertEqual(permit.charged_bytes, allowance + 20)
        before = target.read_bytes()
        with self.assertRaises(native.SourceGrowthLimitExceeded):
            permit.before_write(str(target), 17)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(permit.tripped)
        with self.assertRaises(native.SourceGrowthLimitExceeded):
            permit.before_write(str(self.root / "other.txt"), 1)
        self.assertFalse((self.root / "other.txt").exists())

    def test_growth_permit_rejects_symlink_parent_leaf_and_hardlink_leaf(self) -> None:
        original = self.root / "outside.txt"
        original.write_bytes(b"outside")
        parent = self.root / "linked-parent"
        parent.symlink_to(self.parse, target_is_directory=True)
        leaf = self.root / "linked-leaf.txt"
        leaf.symlink_to(original)
        hardlink = self.root / "hardlink.txt"
        os.link(original, hardlink)
        for label, target in (
            ("parent symlink", parent / "generated.txt"),
            ("leaf symlink", leaf),
            ("leaf hardlink", hardlink),
        ):
            permit = native.SourceGrowthPermit(
                root=self.root, limit_bytes=4096,
                allocation_unit=16, file_overhead=4,
            )
            with self.subTest(path=label), self.assertRaises(native.SourceGrowthLimitExceeded):
                permit.before_write(str(target), 4)

    def test_seekable_writer_charges_extent_once_and_refuses_before_write(self) -> None:
        path = self.root / "writer.part"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            writer = native.BudgetedSeekableWriter(descriptor, grant_bytes=6)
            self.assertEqual(writer.write(b"abcd"), 4)
            self.assertEqual(writer.extent, 4)
            writer.seek(0)
            writer.write(b"AB")
            self.assertEqual(writer.extent, 4)
            writer.seek(4)
            writer.write(b"ef")
            self.assertEqual(writer.extent, 6)
            writer.seek(6)
            with self.assertRaises(native.ResultGrantExceeded):
                writer.write(b"x")
            self.assertEqual(writer.extent, 6)
        finally:
            os.close(descriptor)
        self.assertEqual(path.read_bytes(), b"ABcdef")

    def test_seekable_writer_partial_error_cannot_later_exceed_grant(self) -> None:
        path = self.root / "transient-write.part"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        actual_write = os.write
        calls = 0

        def partial_then_transient_error(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return actual_write(fd, data[:3])
            if calls == 2:
                raise OSError(errno.EIO, "synthetic one-shot write error")
            return actual_write(fd, data)

        try:
            writer = native.BudgetedSeekableWriter(descriptor, grant_bytes=6)
            with patch.object(native.os, "write", side_effect=partial_then_transient_error):
                with self.assertRaises(OSError):
                    writer.write(b"abcd")
                try:
                    writer.write(b"123456")  # a close or retry may write again
                except (OSError, native.ResultGrantExceeded):
                    pass  # a permanently latched failure also preserves the grant
            self.assertLessEqual(os.fstat(descriptor).st_size, 6)
        finally:
            os.close(descriptor)

    def test_sealed_inventory_reopens_same_selected_inputs_and_zip(self) -> None:
        with owned_descriptors():
            original = self.inventory()
            seal = native.write_inventory_file(self.root, self.identity, original)
            reopened = native.load_inventory_file(
                self.root, self.identity, expected_sha256=seal,
                task_id="synthetic-task", policy=self.policy,
            )
            native.verify_result_inventory(
                task_root=self.root, root_identity=self.identity,
                inventory=reopened, selections=self.selection,
            )
            result_path, digest, size = native.write_retained_zip(
                task_root=self.root, root_identity=self.identity,
                inventory=reopened, selections=self.selection,
                grant_bytes=reopened.zip_upper_bound_bytes,
            )
        self.assertEqual(original.exact_bytes(), reopened.exact_bytes())
        self.assertEqual(original.sha256(), seal)
        self.assertEqual(size, result_path.stat().st_size)
        self.assertEqual(digest, hashlib.sha256(result_path.read_bytes()).hexdigest())
        self.assertLessEqual(size, reopened.zip_upper_bound_bytes)
        with zipfile.ZipFile(result_path) as archive:
            self.assertEqual(
                archive.namelist(),
                ["document/document_middle.json", "document/images/crop.jpg"],
            )
            self.assertEqual(archive.read("document/document_middle.json"), self.middle.read_bytes())

    def test_zip_close_denial_keeps_sealed_source_and_closes_all_fds(self) -> None:
        inventory = self.inventory()
        native.write_inventory_file(self.root, self.identity, inventory)
        with owned_descriptors():
            result_path, _digest, exact_size = native.write_retained_zip(
                task_root=self.root, root_identity=self.identity,
                inventory=inventory, selections=self.selection,
                grant_bytes=inventory.zip_upper_bound_bytes,
            )
        result_path.unlink()  # isolated scratch, only to replay the same source under a smaller grant
        with owned_descriptors(), self.assertRaises(native.ResultGrantExceeded):
            native.write_retained_zip(
                task_root=self.root, root_identity=self.identity,
                inventory=inventory, selections=self.selection,
                grant_bytes=exact_size - 1,
            )
        self.assertFalse((self.root / native.RETAINED_RESULT_NAME).exists())
        self.assertFalse((self.root / native.RETAINED_RESULT_PART_NAME).exists())
        self.assertTrue((self.root / native.RETAINED_INVENTORY_NAME).is_file())
        self.assertEqual(self.middle.read_bytes(), b'{"page":1,"text":"alpha"}')

    def test_partial_os_write_failure_discards_part_and_closes_all_fds(self) -> None:
        inventory = self.inventory()
        native.write_inventory_file(self.root, self.identity, inventory)
        actual_write = os.write
        calls = 0

        def partial_then_full_disk(descriptor: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return actual_write(descriptor, data[:3])
            raise OSError(errno.ENOSPC, "synthetic full disk")

        with owned_descriptors(), patch.object(native.os, "write", side_effect=partial_then_full_disk):
            with self.assertRaises(OSError) as failure:
                native.write_retained_zip(
                    task_root=self.root, root_identity=self.identity,
                    inventory=inventory, selections=self.selection,
                    grant_bytes=inventory.zip_upper_bound_bytes,
                )
        self.assertEqual(failure.exception.errno, errno.ENOSPC)
        self.assertGreaterEqual(calls, 2)
        self.assertFalse((self.root / native.RETAINED_RESULT_NAME).exists())
        self.assertFalse((self.root / native.RETAINED_RESULT_PART_NAME).exists())
        self.assertTrue((self.root / native.RETAINED_INVENTORY_NAME).is_file())
        self.assertEqual(self.middle.read_bytes(), b'{"page":1,"text":"alpha"}')

    def test_selected_leaf_symlink_hardlink_and_content_change_are_rejected(self) -> None:
        inventory = self.inventory()
        with owned_descriptors():
            self.middle.write_bytes(b'{"page":1,"text":"omega"}')
            with self.assertRaises(native.TaskProtocolConflict):
                native.verify_result_inventory(
                    task_root=self.root, root_identity=self.identity,
                    inventory=inventory, selections=self.selection,
                )
        self.middle.unlink()
        external = self.root / "unselected.txt"
        external.write_bytes(b"other")
        self.middle.symlink_to(external)
        with owned_descriptors(), self.assertRaises(native.TaskProtocolConflict):
            self.inventory()
        self.middle.unlink()
        os.link(external, self.middle)
        with owned_descriptors(), self.assertRaises(native.TaskProtocolConflict):
            self.inventory()

    def test_sealed_inventory_rejects_a_symlinked_parent_without_fd_leak(self) -> None:
        inventory = self.inventory()
        (self.root / "document").rename(self.root / "original-document")
        (self.root / "document").symlink_to(
            self.root / "original-document", target_is_directory=True
        )
        with owned_descriptors(), self.assertRaises((OSError, native.TaskProtocolConflict)):
            native.verify_result_inventory(
                task_root=self.root, root_identity=self.identity,
                inventory=inventory, selections=self.selection,
            )

    def test_reopened_seal_rejects_replaced_parent_even_with_same_content(self) -> None:
        inventory = self.inventory()
        seal = native.write_inventory_file(self.root, self.identity, inventory)
        reopened = native.load_inventory_file(
            self.root, self.identity, expected_sha256=seal,
            task_id="synthetic-task", policy=self.policy,
        )
        original_parent = self.root / "document"
        original_parent.rename(self.root / "old-document")
        self.parse.mkdir(parents=True)
        (self.parse / "images").mkdir()
        self.middle.write_bytes(b'{"page":1,"text":"alpha"}')
        (self.parse / "images" / "crop.jpg").write_bytes(b"synthetic-jpeg-bytes")
        with owned_descriptors(), self.assertRaises(native.TaskProtocolConflict):
            native.verify_result_inventory(
                task_root=self.root, root_identity=self.identity,
                inventory=reopened, selections=self.selection,
            )


if __name__ == "__main__":
    unittest.main()
