"""Real owned files and OS faults for the bounded capacity loader, no service."""

from contextlib import contextmanager
import dataclasses
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_capacity_config as loader
from disclosure_anchor.adapters.runtime import mineru_capacity_file as reader
from disclosure_anchor.application.contracts import mineru_capacity_config as codec
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES, capacity_payload


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def errors(error):
    seen, pending = set(), [error]
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend([item.__cause__, item.__context__])
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)


class MineruCapacityFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mineru-capacity-file-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "capacity.json"
        self.file.write_bytes(CAPACITY_BYTES)
        self.file.chmod(0o600)
        self.uid = self.file.stat().st_uid

    def read(self, path=None, raw=CAPACITY_BYTES, **kwargs):
        options = {"expected_sha256": digest(raw), "expected_owner_uid": self.uid}
        options.update(kwargs)
        return reader.read_mineru_capacity_file(path or self.file, **options)

    @contextmanager
    def descriptors(
        self, *, read_hook=None, open_hook=None, fstat_hook=None, close_faults=()
    ):
        """Observe real handles; hooks inject errors/mutations, never file authority."""
        original_open, original_close = os.open, os.close
        original_read, original_fstat = os.read, os.fstat
        live, allocated, reads = set(), [], []
        close_number = 0

        def opened(path, flags, *args, **kwargs):
            if open_hook is not None:
                open_hook(path, flags)
            fd = original_open(path, flags, *args, **kwargs)
            self.assertNotIn(fd, live)
            live.add(fd)
            allocated.append(fd)
            return fd

        def closed(fd):
            nonlocal close_number
            self.assertIn(fd, live, "closing an unowned or already closed descriptor")
            original_close(fd)
            live.remove(fd)
            close_number += 1
            if close_number <= len(close_faults):
                raise close_faults[close_number - 1]

        def read_bytes(fd, count):
            self.assertIn(fd, live)
            self.assertGreater(count, 0)
            self.assertLessEqual(count, 65537)
            result = (
                original_read(fd, count)
                if read_hook is None
                else read_hook(fd, count, original_read)
            )
            reads.append((fd, len(result)))
            return result

        def metadata(fd):
            return (
                original_fstat(fd)
                if fstat_hook is None
                else fstat_hook(fd, original_fstat)
            )

        with (
            patch.object(reader.os, "open", opened),
            patch.object(reader.os, "close", closed),
            patch.object(reader.os, "read", read_bytes),
            patch.object(reader.os, "fstat", metadata),
        ):
            try:
                yield {"live": live, "allocated": allocated, "reads": reads}
            finally:
                leftovers = tuple(live)
                for fd in leftovers:
                    original_close(fd)
                    live.remove(fd)
                self.assertEqual(leftovers, (), "helper leaked original descriptors")
                for fd in set(allocated):
                    with self.assertRaises(OSError) as gone:
                        original_fstat(fd)
                    self.assertEqual(gone.exception.errno, errno.EBADF)

    def test_raw_reader_accepts_nonjson_and_exact_size_endpoints_without_decoding(self):
        for raw in (b"x", b"\xffnot-json", b"R" * 65536):
            with self.subTest(size=len(raw)):
                self.file.write_bytes(raw)
                with self.descriptors() as owned:
                    self.assertEqual(self.read(raw=raw), raw)
                self.assertEqual(len({fd for fd, _ in owned["reads"]}), 1)
                self.assertEqual(sum(size for _, size in owned["reads"]), len(raw))
        for raw in (b"", b"X" * 65537):
            self.file.write_bytes(raw)
            with self.subTest(rejected_size=len(raw)), self.descriptors() as owned:
                with self.assertRaises(ValueError):
                    self.read(raw=raw)
                self.assertEqual(owned["reads"], [])

    def test_invalid_path_expected_digest_and_owner_refuse_before_open(self):
        invalid_paths = [
            Path("relative.json"),
            self.root / ".." / self.root.name / self.file.name,
            self.root / "..",
        ]
        with patch.object(
            reader.os,
            "open",
            side_effect=AssertionError("invalid request reached filesystem"),
        ):
            for path in invalid_paths:
                with self.subTest(path=str(path)), self.assertRaises(ValueError):
                    self.read(path)
            for sha in (
                "a" * 64,
                "sha256:" + "A" * 64,
                "sha256:" + "g" * 64,
                "sha256:" + "a" * 63,
                None,
            ):
                with self.subTest(sha=sha), self.assertRaises(ValueError):
                    self.read(expected_sha256=sha)
            for uid in (True, -1, 1.0, "1"):
                with self.subTest(uid=uid), self.assertRaises(ValueError):
                    self.read(expected_owner_uid=uid)

    def test_wrong_hash_owner_writable_mode_and_hardlink_reject_and_release(self):
        with self.descriptors(), self.assertRaises(ValueError):
            self.read(expected_sha256="sha256:" + "0" * 64)
        with self.descriptors(), self.assertRaises(ValueError):
            self.read(expected_owner_uid=self.uid + 1)
        for mode in (0o620, 0o602):
            self.file.chmod(mode)
            with (
                self.subTest(mode=oct(mode)),
                self.descriptors(),
                self.assertRaises(ValueError),
            ):
                self.read()
        # Readable by others is allowed; the contract forbids writable bits.
        self.file.chmod(0o644)
        self.assertEqual(self.read(), CAPACITY_BYTES)
        os.link(self.file, self.root / "alias")
        with self.descriptors(), self.assertRaises(ValueError):
            self.read()

    def test_parent_leaf_symlink_directory_and_real_fifo_never_read_payload(self):
        nested = self.root / "real"
        nested.mkdir()
        (nested / "capacity.json").write_bytes(CAPACITY_BYTES)
        (nested / "capacity.json").chmod(0o600)
        parent_link = self.root / "link"
        parent_link.symlink_to(nested, target_is_directory=True)
        leaf_link = self.root / "leaf.json"
        leaf_link.symlink_to(self.file)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)

        def require_nonblock(path, flags):
            if os.fspath(path) == fifo.name:
                self.assertTrue(
                    flags & os.O_NONBLOCK,
                    "refuse a blocking FIFO open before calling OS",
                )

        for path in (parent_link / "capacity.json", leaf_link, nested, fifo):
            with (
                self.subTest(path=path.name),
                self.descriptors(open_hook=require_nonblock) as owned,
            ):
                with self.assertRaises((OSError, ValueError)):
                    self.read(path)
                self.assertEqual(owned["reads"], [])

    def test_short_reads_use_one_held_payload_fd_and_conserve_exact_bytes(self):
        def short(fd, count, original):
            return original(fd, min(count, 7))

        with self.descriptors(read_hook=short) as owned:
            self.assertEqual(self.read(), CAPACITY_BYTES)
        self.assertGreater(len(owned["reads"]), 2)
        self.assertEqual(len({fd for fd, _ in owned["reads"]}), 1)
        self.assertEqual(sum(size for _, size in owned["reads"]), len(CAPACITY_BYTES))

    def test_during_read_changes_reject_even_when_returned_bytes_match_old_hash(self):
        for change in ("same_size", "grow", "truncate", "mode", "replace_same_bytes"):
            self.file.chmod(0o600)
            self.file.write_bytes(CAPACITY_BYTES)
            changed = False
            before = self.file.stat()

            def mutate(fd, count, original):
                nonlocal changed
                raw = original(fd, count)
                if not changed:
                    changed = True
                    if change == "same_size":
                        self.file.write_bytes(b"x" * len(CAPACITY_BYTES))
                        os.utime(
                            self.file,
                            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
                        )
                    elif change == "grow":
                        with self.file.open("ab") as stream:
                            stream.write(b"growth")
                    elif change == "truncate":
                        self.file.write_bytes(b"x")
                    elif change == "mode":
                        self.file.chmod(0o400)
                    else:
                        replacement = self.root / "replacement"
                        replacement.write_bytes(CAPACITY_BYTES)
                        replacement.chmod(0o600)
                        replacement.replace(self.file)
                return raw

            with self.subTest(change=change), self.descriptors(read_hook=mutate):
                with self.assertRaises(ValueError):
                    self.read()
            self.assertTrue(changed)
        self.file.chmod(0o600)

    def test_open_fstat_and_partial_read_errors_preserve_original_and_close_all_handles(
        self,
    ):
        for stage in ("open", "fstat", "read"):
            failure = OSError(errno.EIO, "owned fault at " + stage)
            calls = 0

            def fail_open(path, flags):
                if os.fspath(path) == self.file.name:
                    raise failure

            def fail_stat(fd, original):
                raise failure

            def fail_read(fd, count, original):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original(fd, 1)
                raise failure

            hooks = (
                {"open_hook": fail_open}
                if stage == "open"
                else {"fstat_hook": fail_stat}
                if stage == "fstat"
                else {"read_hook": fail_read}
            )
            with self.subTest(stage=stage), self.descriptors(**hooks):
                with self.assertRaises((OSError, ValueError)) as caught:
                    self.read()
                self.assertIn(failure, tuple(errors(caught.exception)))

    def test_primary_read_and_multiple_close_errors_remain_visible_and_other_fds_close(
        self,
    ):
        primary = OSError("original read failed")
        cleanup = [
            OSError("released leaf close failed"),
            OSError("released directory close failed"),
        ]

        def failed_read(fd, count, original):
            raise primary

        with self.descriptors(read_hook=failed_read, close_faults=cleanup):
            with self.assertRaises(BaseException) as caught:
                self.read()
        chain = tuple(errors(caught.exception))
        for failure in [primary, *cleanup]:
            self.assertIn(failure, chain)

    def test_loaded_config_keeps_exact_canonical_pair_and_invalid_json_is_not_raw_file_failure(
        self,
    ):
        with self.descriptors() as owned:
            original_decode = loader.decode_mineru_capacity_config

            def decode_after_close(raw):
                self.assertEqual(owned["live"], set())
                return original_decode(raw)

            with patch.object(
                loader, "decode_mineru_capacity_config", side_effect=decode_after_close
            ):
                loaded = loader.load_mineru_capacity_config(
                    self.file,
                    expected_sha256=digest(CAPACITY_BYTES),
                    expected_owner_uid=self.uid,
                )
        self.assertIs(type(loaded), loader.LoadedMineruCapacityConfig)
        self.assertIs(type(loaded.config), codec.MineruCapacityConfig)
        self.assertEqual(loaded.exact_bytes, CAPACITY_BYTES)
        self.assertEqual(loaded.sha256, digest(CAPACITY_BYTES))
        self.assertEqual(dataclasses.asdict(loaded.config), capacity_payload())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            loaded.exact_bytes = b"different"
        for config, raw in (
            (loaded.config, CAPACITY_BYTES + b"\n"),
            (capacity_payload(), CAPACITY_BYTES),
            (loaded.config, bytearray(CAPACITY_BYTES)),
        ):
            with (
                self.subTest(type=type(config).__name__, raw_type=type(raw).__name__),
                self.assertRaises(ValueError),
            ):
                loader.LoadedMineruCapacityConfig(config=config, exact_bytes=raw)
        invalid = b"not a JSON document"
        self.file.write_bytes(invalid)
        self.assertEqual(self.read(raw=invalid), invalid)
        with self.descriptors(), self.assertRaises(ValueError):
            loader.load_mineru_capacity_config(
                self.file, expected_sha256=digest(invalid), expected_owner_uid=self.uid
            )

    def test_raw_helper_identical_bytes_run_standalone_without_application_or_site_packages(
        self,
    ):
        source = Path(reader.__file__).read_bytes()
        copy = self.root / "standalone_reader.py"
        copy.write_bytes(source)
        program = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('standalone_reader', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from pathlib import Path
value = module.read_mineru_capacity_file(Path(sys.argv[2]), expected_sha256=sys.argv[3], expected_owner_uid=int(sys.argv[4]))
for name in sys.modules:
    assert name.split('.')[0] in sys.stdlib_module_names or name == '__main__', name
print(value.hex())
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                program,
                str(copy),
                str(self.file),
                digest(CAPACITY_BYTES),
                str(self.uid),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={},
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(bytes.fromhex(result.stdout.decode().strip()), CAPACITY_BYTES)
        self.assertEqual(copy.read_bytes(), source)
