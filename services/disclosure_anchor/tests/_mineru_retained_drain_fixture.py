"""Real generated retained-result functions, real disposable FDs, bounded native gates."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tests._mineru_owned_drain_fixture import (
    NativeBarrier,
    load_definitions,
    load_owned,
)


class RetainedFixture:
    def __init__(
        self,
        sources,
        *,
        block=None,
        verify_error=None,
        close_error=None,
        acquire_error=None,
        write_error=None,
    ):
        self.temporary = tempfile.TemporaryDirectory(prefix="independent-owned-result-")
        self.root = Path(self.temporary.name)
        self.inputs = {
            "source.md": b"literal independent markdown\n",
            "source_middle.json": b'{"literal":"middle"}\n',
        }
        for name, payload in self.inputs.items():
            (self.root / name).write_bytes(payload)
        self.events = []
        self.descriptors = {}
        self.closes = {}
        self.fd_generation_kind = {}
        self.replacement = None
        self.forced_fixture_closes = []
        self.phase = None
        self.block = block
        self.barrier = NativeBarrier(self.events, block) if block else None
        self.blocked = False
        self.verify_error = verify_error
        self.close_error = close_error
        self.acquire_error = acquire_error
        self.write_error = write_error
        self.operation_done = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.source_list = None
        self.warnings = []
        self.task = SimpleNamespace(
            output_dir=str(self.root),
            file_names=["source"],
            backend="hybrid",
            parse_method="auto",
            return_md=True,
            return_middle_json=True,
            return_model_output=False,
            return_content_list=False,
            return_images=False,
            return_original_file=False,
            task_id="literal-task",
            result_artifact_path=None,
            result_artifact_sha256=None,
            result_artifact_bytes=None,
            result_artifact_owner=None,
        )
        self.namespace = load_owned(sources["model"], sources["api"])
        self.namespace.update(
            {
                "asyncio": asyncio,
                "os": self.os_proxy(),
                "Path": Path,
                "stat": stat,
                "hashlib": hashlib,
                "shutil": shutil,
                "logger": SimpleNamespace(warning=self.warnings.append),
                "get_parse_dir": lambda *args: str(self.root),
                "build_zip_arcname": lambda pdf_name, parse_dir, name: name,
            }
        )
        names = {
            "_retained_result_sources",
            "_verify_and_close_result_sources",
            "_write_retained_zip_from_fds",
            "_hash_file",
            "build_retained_task_result",
            "cleanup_file",
        }
        load_definitions(sources["api"], names, self.namespace)
        for name, phase in (
            ("_retained_result_sources", "acquire"),
            ("_write_retained_zip_from_fds", "write"),
            ("_verify_and_close_result_sources", "verify"),
            ("_hash_file", "hash"),
        ):
            actual = self.namespace[name]
            self.namespace[name] = self.wrap(actual, phase)

    def gate(self, phase):
        if self.block == phase and not self.blocked:
            self.blocked = True
            self.barrier.enter()

    def wrap(self, actual, phase):
        def operation(*args, **kwargs):
            self.phase = phase
            self.events.append(phase + ":start")
            try:
                if phase == "hash":
                    self.gate(phase)
                value = actual(*args, **kwargs)
                if phase == "acquire":
                    self.source_list = value[1]
                    self.gate(
                        phase
                    )  # actual FDs exist before result transfer to coroutine
                return value
            finally:
                self.events.append(phase + ":end")
                self.phase = None
                if phase == self.block:
                    self.loop.call_soon_threadsafe(self.operation_done.set)

        return operation

    def os_proxy(self):
        # Namespace-only adapter; never patches global os used by other threads/unittest.
        class Proxy:
            def __getattr__(self, name):
                return getattr(os, name)

        proxy = Proxy()

        def open_file(path, flags, *args, **kwargs):
            fd = os.open(path, flags, *args, **kwargs)
            self.fd_generation_kind[fd] = (
                "source" if Path(path).name in self.inputs else "other"
            )
            if Path(path).name in self.inputs:
                self.descriptors[fd] = os.fstat(fd)
                self.closes[fd] = 0
            return fd

        def close_file(fd):
            if self.fd_generation_kind.get(fd) == "source":
                self.closes[fd] += 1
                self.events.append("source-fd:close")
                os.close(fd)
                if self.close_error is not None and self.replacement is None:
                    replacement = os.open(
                        self.root / "replacement.txt", os.O_CREAT | os.O_RDWR, 0o600
                    )
                    if replacement != fd:
                        os.close(replacement)
                        raise AssertionError(
                            "expected immediate descriptor reuse at controlled seam"
                        )
                    self.replacement = replacement
                    raise self.close_error
                return
            os.close(fd)

        def fstat(fd):
            if self.phase == "verify":
                self.gate("verify")  # actual callee has entered before cancellation
                if self.verify_error is not None:
                    raise self.verify_error
            if self.phase == "acquire" and self.acquire_error is not None:
                raise self.acquire_error
            return os.fstat(fd)

        def read(fd, count):
            if self.phase == "write":
                self.gate("write")  # actual ZIP is open and source is about to be read
                if self.write_error is not None:
                    raise self.write_error
            return os.read(fd, count)

        def remove(path):
            self.events.append("unlink:" + Path(path).name)
            return os.remove(path)

        proxy.open = open_file
        proxy.close = close_file
        proxy.fstat = fstat
        proxy.read = read
        proxy.remove = remove
        # This budget is explicit fixture input, never ambient production environment.
        proxy.getenv = lambda key, default=None: (
            "2097152"
            if key == "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES"
            else default
        )
        return proxy

    def start(self):
        return asyncio.create_task(
            self.namespace["build_retained_task_result"](self.task, byte_budget=2097152)
        )

    def fd_alive(self, fd):
        try:
            observed = os.fstat(fd)
        except OSError:
            return False
        expected = self.descriptors[fd]
        return (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)

    async def close(self, task):
        if self.barrier:
            self.barrier.release.set()
            if self.blocked:
                await asyncio.wait_for(self.operation_done.wait(), 5)
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=5)
            if task not in done:
                raise AssertionError("generated retained builder did not settle")
            # Consume expected errors without replacing assertions from the test body.
            try:
                await task
            except BaseException as exc:
                self.events.append("fixture-observed:" + type(exc).__name__)
        # Cleanup failed-baseline leaks only when exact original inode is still held.
        for fd in self.descriptors:
            if self.fd_alive(fd):
                self.forced_fixture_closes.append(fd)
                os.close(fd)
        if self.replacement is not None:
            try:
                os.close(self.replacement)
            except OSError:
                pass  # a failed oracle may already prove original code consumed this FD
        self.temporary.cleanup()
