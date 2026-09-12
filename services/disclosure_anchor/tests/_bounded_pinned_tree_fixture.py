"""Independent tiny filesystem fixtures and fault tracers, never IO authority DTOs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


LITERAL_FILES = {
    "a.json": b'{"answer":7}\n',
    "sub/b.txt": b"bravo\n",
    "z.bin": b"\x00raw\xff",
}
LITERAL_DIRECTORIES = (".", "empty", "sub", "sub/empty")
LITERAL_ENTRY_COUNT = 7
LITERAL_BYTE_COUNT = 24


def make_tree(root: Path) -> None:
    root.mkdir(mode=0o700)
    for relative in LITERAL_DIRECTORIES[1:]:
        (root / relative).mkdir(mode=0o700)
    for relative, raw in LITERAL_FILES.items():
        path = root / relative
        path.write_bytes(raw)
        path.chmod(0o600)


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def identity(fd: int) -> tuple[int, int]:
    value = os.fstat(fd)
    return value.st_dev, value.st_ino


def errors_in(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    found = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None and not current.__suppress_context__:
            pending.append(current.__context__)
    return tuple(found)


class TracedScandir:
    """Wrap real directory-FD iterators; count returned entries and close calls."""
    def __init__(self, original) -> None:
        self.original = original
        self.iterators = []
        self.entries_seen = 0

    def __call__(self, fd):
        if type(fd) is not int:
            raise AssertionError("bounded tree must enumerate its held directory FD")
        iterator = _ScanIterator(self, self.original(fd))
        self.iterators.append(iterator)
        return iterator

    def assert_all_closed(self, case) -> None:
        case.assertGreater(len(self.iterators), 0)
        for iterator in self.iterators:
            case.assertEqual(iterator.close_calls, 1)
            case.assertTrue(iterator.closed)


class _ScanIterator:
    def __init__(self, owner, actual) -> None:
        self.owner = owner
        self.actual = actual
        self.close_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.actual)
        self.owner.entries_seen += 1
        return entry

    def close(self):
        self.close_calls += 1
        self.actual.close()
        self.closed = True
