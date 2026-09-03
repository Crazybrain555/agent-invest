from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import errno
import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.storage import immutable_artifact_store as store_module
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    ImmutableArtifactStore,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict,
    AtomicPublicationArtifactReadinessError,
)


class _Paths:
    def __init__(self, root: Path) -> None:
        self._root = root

    def data_path(self, relpath: Path) -> Path:
        return self._root / relpath


class ImmutableArtifactStoreTests(unittest.TestCase):
    def test_exact_replay_is_idempotent_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            relpath = Path("published/run/request.json")

            first = store.create_or_verify(relpath=relpath, payload=b"exact\n")
            second = store.create_or_verify(relpath=relpath, payload=b"exact\n")

            self.assertEqual(first, second)
            observed = (root / relpath).lstat()
            self.assertEqual(stat.S_IMODE(observed.st_mode), 0o600)
            self.assertEqual(observed.st_nlink, 1)
            self.assertEqual(tuple(root.rglob("*.tmp")), ())

    def test_different_replay_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(Path(raw_root))
            )
            relpath = Path("published/run/request.json")
            store.create_or_verify(relpath=relpath, payload=b"winner\n")

            with self.assertRaises(AtomicPublicationArtifactConflict):
                store.create_or_verify(relpath=relpath, payload=b"different\n")

    def test_concurrent_exact_writers_converge_without_hardlink_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            barrier = threading.Barrier(2)

            def hook(phase: str) -> None:
                if phase == "after_temp_fsync":
                    barrier.wait(timeout=5)

            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root), fault_hook=hook
            )
            relpath = Path("published/run/request.json")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(
                    pool.map(
                        lambda _: store.create_or_verify(
                            relpath=relpath,
                            payload=b"same bytes\n",
                        ),
                        range(2),
                    )
                )

            self.assertEqual(results[0], results[1])
            self.assertEqual((root / relpath).read_bytes(), b"same bytes\n")
            self.assertEqual((root / relpath).lstat().st_nlink, 1)

    def test_fault_boundaries_are_exactly_replayable(self) -> None:
        phases = (
            "after_temp_fsync",
            "after_rename",
            "after_final_fsync",
            "after_parent_fsync",
        )
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)

                def hook(observed: str) -> None:
                    if observed == phase:
                        raise RuntimeError(phase)

                relpath = Path("published/run/request.json")
                failing = ImmutableArtifactStore(  # type: ignore[arg-type]
                    _Paths(root), fault_hook=hook
                )
                with self.assertRaisesRegex(RuntimeError, phase):
                    failing.create_or_verify(relpath=relpath, payload=b"exact\n")

                replay = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
                replay.create_or_verify(relpath=relpath, payload=b"exact\n")
                self.assertEqual((root / relpath).read_bytes(), b"exact\n")
                self.assertEqual((root / relpath).lstat().st_nlink, 1)

    def test_symlink_and_hardlink_aliases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            relpath = Path("published/run/request.json")
            final = root / relpath
            final.parent.mkdir(parents=True)
            target = root / "target"
            target.write_bytes(b"exact\n")
            final.symlink_to(target)
            with self.assertRaises(AtomicPublicationArtifactConflict):
                store.create_or_verify(relpath=relpath, payload=b"exact\n")

            final.unlink()
            receipt = store.create_or_verify(relpath=relpath, payload=b"exact\n")
            os.link(final, root / "alias")
            with self.assertRaises(AtomicPublicationArtifactConflict):
                store.read_exact(
                    relpath=relpath,
                    expected_sha256=receipt.artifact_hash,
                    expected_byte_count=6,
                    max_byte_count=6,
                )

    def test_nested_create_and_exact_replay_fsync_every_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relpath = Path("a/b/c/request.json")
            original_fsync = store_module.os.fsync

            def capture(
                target: list[tuple[str, int | str]],
            ) -> Callable[[int], None]:
                def tracked(descriptor: int) -> None:
                    observed = store_module.os.fstat(descriptor)
                    if stat.S_ISDIR(observed.st_mode):
                        target.append(("directory", observed.st_ino))
                    elif stat.S_ISREG(observed.st_mode):
                        target.append(("file", observed.st_ino))
                    original_fsync(descriptor)

                return tracked

            first_events: list[tuple[str, int | str]] = []

            def hook(phase: str) -> None:
                if phase in {
                    "after_rename",
                    "after_final_fsync",
                    "after_parent_fsync",
                }:
                    first_events.append(("phase", phase))

            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root),
                fault_hook=hook,
            )
            with mock.patch.object(
                store_module.os,
                "fsync",
                side_effect=capture(first_events),
            ):
                store.create_or_verify(relpath=relpath, payload=b"exact\n")

            expected_final_durability: list[tuple[str, int | str]] = [
                ("file", (root / relpath).stat().st_ino),
                ("directory", (root / "a/b/c").stat().st_ino),
                ("directory", (root / "a/b").stat().st_ino),
                ("directory", (root / "a").stat().st_ino),
                ("directory", root.stat().st_ino),
            ]
            after_rename = first_events.index(("phase", "after_rename"))
            self.assertEqual(
                first_events[after_rename:],
                [
                    ("phase", "after_rename"),
                    expected_final_durability[0],
                    ("phase", "after_final_fsync"),
                    *expected_final_durability[1:],
                    ("phase", "after_parent_fsync"),
                ],
            )

            replay_events: list[tuple[str, int | str]] = []
            replay_store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root)
            )
            with mock.patch.object(
                store_module.os,
                "fsync",
                side_effect=capture(replay_events),
            ):
                replay_store.create_or_verify(relpath=relpath, payload=b"exact\n")

            self.assertEqual(replay_events, expected_final_durability)

    def test_fault_during_new_directory_durability_is_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            tripped = False

            def hook(phase: str) -> None:
                nonlocal tripped
                if phase == "after_directory_fsync" and not tripped:
                    tripped = True
                    raise RuntimeError(phase)

            relpath = Path("new/nested/request.json")
            failing = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root),
                fault_hook=hook,
            )
            with self.assertRaisesRegex(RuntimeError, "after_directory_fsync"):
                failing.create_or_verify(relpath=relpath, payload=b"exact\n")

            replay = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            replay.create_or_verify(relpath=relpath, payload=b"exact\n")
            self.assertEqual((root / relpath).read_bytes(), b"exact\n")

    def test_parent_rename_symlink_swap_fails_closed_and_stays_beneath_pin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_parent:
            parent = Path(raw_parent)
            root = parent / "data"
            outside = parent / "outside"
            detached = parent / "detached"
            root.mkdir()
            outside.mkdir()
            swapped = False

            def hook(phase: str) -> None:
                nonlocal swapped
                if phase == "after_directory_fsync" and not swapped:
                    swapped = True
                    (root / "published").rename(detached)
                    (root / "published").symlink_to(
                        outside,
                        target_is_directory=True,
                    )

            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root),
                fault_hook=hook,
            )
            with self.assertRaises(AtomicPublicationArtifactConflict):
                store.create_or_verify(
                    relpath=Path("published/run/request.json"),
                    payload=b"exact\n",
                )

            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertEqual(
                (detached / "run/request.json").read_bytes(),
                b"exact\n",
            )

    def test_ulid_failure_closes_every_pinned_directory_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            descriptor_root = (
                Path("/proc/self/fd")
                if Path("/proc/self/fd").is_dir()
                else Path("/dev/fd")
            )
            before = len(os.listdir(descriptor_root))

            with mock.patch.object(
                store_module,
                "new_ulid",
                side_effect=RuntimeError("ulid failed"),
            ):
                for _ in range(20):
                    with self.assertRaisesRegex(RuntimeError, "ulid failed"):
                        store.create_or_verify(
                            relpath=Path("published/run/request.json"),
                            payload=b"exact\n",
                        )

            self.assertEqual(len(os.listdir(descriptor_root)), before)

    def test_removed_parent_during_open_is_conflict_without_fd_leak(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "published/run").mkdir(parents=True)
            store = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            descriptor_root = (
                Path("/proc/self/fd")
                if Path("/proc/self/fd").is_dir()
                else Path("/dev/fd")
            )
            before = len(os.listdir(descriptor_root))
            original_open = store_module.os.open
            removed = False

            def racing_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal removed
                if (
                    path == "run"
                    and flags & os.O_DIRECTORY
                    and dir_fd is not None
                    and not removed
                ):
                    removed = True
                    (root / "published/run").rmdir()
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(
                store_module.os,
                "open",
                side_effect=racing_open,
            ):
                with self.assertRaisesRegex(
                    AtomicPublicationArtifactConflict,
                    "directory changed while opening",
                ):
                    store.create_or_verify(
                        relpath=Path("published/run/request.json"),
                        payload=b"exact\n",
                    )

            self.assertEqual(len(os.listdir(descriptor_root)), before)

    def test_directory_capacity_error_remains_readiness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(Path(raw_root))
            )
            failure = OSError(errno.ENOSPC, "no space left on device")

            with mock.patch.object(
                store_module.os,
                "mkdir",
                side_effect=failure,
            ):
                with self.assertRaisesRegex(
                    AtomicPublicationArtifactReadinessError,
                    "cannot be opened or created",
                ):
                    store.create_or_verify(
                        relpath=Path("published/run/request.json"),
                        payload=b"exact\n",
                    )

    def test_root_open_infrastructure_error_remains_readiness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(Path(raw_root))
            )
            descriptor_root = (
                Path("/proc/self/fd")
                if Path("/proc/self/fd").is_dir()
                else Path("/dev/fd")
            )
            before = len(os.listdir(descriptor_root))

            with mock.patch.object(
                store_module.os,
                "open",
                side_effect=OSError(errno.EMFILE, "too many open files"),
            ):
                with self.assertRaisesRegex(
                    AtomicPublicationArtifactReadinessError,
                    "data root cannot be pinned",
                ):
                    store.create_or_verify(
                        relpath=Path("published/run/request.json"),
                        payload=b"exact\n",
                    )

            self.assertEqual(len(os.listdir(descriptor_root)), before)

    def test_final_verify_infrastructure_error_remains_readiness_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relpath = Path("published/run/request.json")
            fail_verify = False

            def hook(phase: str) -> None:
                nonlocal fail_verify
                if phase == "after_parent_fsync":
                    fail_verify = True

            original_stat = store_module.os.stat

            def failing_stat(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                if fail_verify and path == root:
                    raise OSError(errno.EIO, "input/output error")
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            store = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root),
                fault_hook=hook,
            )
            with mock.patch.object(
                store_module.os,
                "stat",
                side_effect=failing_stat,
            ):
                with self.assertRaisesRegex(
                    AtomicPublicationArtifactReadinessError,
                    "directory path cannot be inspected",
                ):
                    store.create_or_verify(relpath=relpath, payload=b"exact\n")

            self.assertEqual((root / relpath).read_bytes(), b"exact\n")

    def test_final_file_stat_and_open_infrastructure_errors_are_readiness(
        self,
    ) -> None:
        for operation, failure_errno in (("stat", errno.EIO), ("open", errno.EMFILE)):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                relpath = Path("published/run/request.json")
                store = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
                receipt = store.create_or_verify(relpath=relpath, payload=b"exact\n")
                descriptor_root = (
                    Path("/proc/self/fd")
                    if Path("/proc/self/fd").is_dir()
                    else Path("/dev/fd")
                )
                before = len(os.listdir(descriptor_root))
                original = getattr(store_module.os, operation)

                def fail_final(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    if path == relpath.name:
                        raise OSError(failure_errno, "infrastructure failure")
                    return original(path, *args, **kwargs)

                with mock.patch.object(
                    store_module.os,
                    operation,
                    side_effect=fail_final,
                ):
                    with self.assertRaises(AtomicPublicationArtifactReadinessError):
                        store.read_exact(
                            relpath=relpath,
                            expected_sha256=receipt.artifact_hash,
                            expected_byte_count=receipt.byte_count,
                            max_byte_count=receipt.byte_count,
                        )

                self.assertEqual(len(os.listdir(descriptor_root)), before)

    def test_equal_length_temp_mutation_is_rejected_before_rename(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relpath = Path("published/run/request.json")

            def hook(phase: str) -> None:
                if phase == "after_temp_fsync":
                    (temp_path,) = tuple((root / relpath.parent).glob(".*.tmp"))
                    temp_path.write_bytes(b"evil!\n")

            failing = ImmutableArtifactStore(  # type: ignore[arg-type]
                _Paths(root),
                fault_hook=hook,
            )
            with self.assertRaisesRegex(
                AtomicPublicationArtifactConflict,
                "temp identity drifted",
            ):
                failing.create_or_verify(relpath=relpath, payload=b"exact\n")

            self.assertFalse((root / relpath).exists())
            self.assertEqual(tuple((root / relpath.parent).glob(".*.tmp")), ())
            replay = ImmutableArtifactStore(_Paths(root))  # type: ignore[arg-type]
            replay.create_or_verify(relpath=relpath, payload=b"exact\n")
            self.assertEqual((root / relpath).read_bytes(), b"exact\n")


if __name__ == "__main__":
    unittest.main()
