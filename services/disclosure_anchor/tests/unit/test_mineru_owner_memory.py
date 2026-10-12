"""GPU owner-memory diagnostic without CUDA, torch or vLLM.

Fake tensors expose only the metadata the census reads; fake vLLM/torch
modules stand in for the serving image so the real ``worker.py`` binding runs.
These tests cannot show that real torch/vLLM objects behave like the fakes;
the serving image qualification owns that evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

PACKAGE = "mineru_inference_memory"
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "windows" / PACKAGE
MiB = 1 << 20
GiB = 1 << 30
SECOND = 1_000_000_000


def _load_core() -> types.ModuleType:
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PACKAGE_DIR / "__init__.py", submodule_search_locations=[str(PACKAGE_DIR)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{PACKAGE}.core")


core = _load_core()


class FakeStorage:
    def __init__(self, address: int, size: int) -> None:
        self._address, self._size = address, size

    def data_ptr(self) -> int:
        return self._address

    def nbytes(self) -> int:
        return self._size


class FakeTensor:
    def __init__(self, storage: Any, nbytes: Any, device: int = 0, *, is_cuda: bool = True):
        self._storage, self.nbytes, self._device, self.is_cuda = storage, nbytes, device, is_cuda

    def get_device(self) -> int:
        return self._device if self.is_cuda else -1

    def untyped_storage(self) -> Any:
        if isinstance(self._storage, BaseException):
            raise self._storage
        return self._storage


def split_views(address: int, parent_bytes: int, view_bytes: list[int]) -> list[FakeTensor]:
    """Views returned by ``Tensor.split``: each keeps the whole parent storage."""
    parent = FakeStorage(address, parent_bytes)
    return [FakeTensor(parent, size) for size in view_bytes]


class FakeAsyncOutput:
    pass


class FakeFinalOutput:
    pass


def native_stats(**overrides: int) -> dict[str, int]:
    stats = {key: 0 for key in core.ALLOCATOR_KEYS}
    stats.update(
        {
            "allocated_bytes.all.current": 7 * GiB,
            "reserved_bytes.all.current": 10 * GiB,
            "active_bytes.all.current": 7 * GiB,
            "inactive_split_bytes.all.current": 2 * GiB,
            "allocated_bytes.all.peak": 9 * GiB,
            "reserved_bytes.all.peak": 10 * GiB,
            "active_bytes.all.peak": 9 * GiB,
            "inactive_split_bytes.all.peak": 3 * GiB,
            "num_alloc_retries": 2,
        }
    )
    stats.update(overrides)
    return stats


class FakeCuda:
    def __init__(self, *, stats: Any = None, backend: str = "native", initialized: bool = True,
                 current: int = 0, mem: Any = (4 * GiB, 16 * GiB)) -> None:
        self.stats = native_stats() if stats is None else stats
        self.backend, self.initialized, self.current, self.mem = backend, initialized, current, mem
        self.calls: list[str] = []

    def is_initialized(self) -> bool:
        self.calls.append("is_initialized")
        return self.initialized

    def current_device(self) -> int:
        self.calls.append("current_device")
        return self.current

    def memory_stats(self, device_index: int) -> Any:
        self.calls.append("memory_stats")
        if isinstance(self.stats, BaseException):
            raise self.stats
        return dict(self.stats)

    def allocator_backend(self) -> str:
        self.calls.append("allocator_backend")
        return self.backend

    def mem_get_info(self, device_index: int) -> Any:
        self.calls.append("mem_get_info")
        return self.mem


class FakeClock:
    def __init__(self, seconds: float = 0) -> None:
        self.now = round(seconds * SECOND)
        self.error: BaseException | None = None

    def __call__(self) -> int:
        if self.error is not None:
            raise self.error
        return self.now

    def at(self, seconds: float) -> None:
        self.now = round(seconds * SECOND)


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def records(self) -> list[dict[str, Any]]:
        marker = core.LOG_MARKER + " "
        return [json.loads(line[len(marker):]) for line in self.lines if line.startswith(marker)]

    def samples(self, phase: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.records()
                if r["kind"] == "sample" and (phase is None or r["phase"] == phase)]

    def errors(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["kind"] == "error"]


def step(tokens: int = 64, *, encoders: int = 0, free: list[str] | None = None) -> Any:
    return types.SimpleNamespace(
        total_num_scheduled_tokens=tokens,
        num_scheduled_tokens={"request": tokens} if tokens else {},
        scheduled_encoder_inputs={"request": [0]} if encoders else {},
        finished_req_ids=set(),
        free_encoder_mm_hashes=list(free or []),
    )


class MonitorHarness:
    def __init__(self, test: unittest.TestCase, cuda: FakeCuda | None = None,
                 cache: dict[str, Any] | None = None) -> None:
        self.cuda = cuda or FakeCuda()
        self.clock = FakeClock()
        self.capture = Capture()
        self.logger = logging.getLogger(f"test.owner_memory.{id(self)}")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.capture)
        test.addCleanup(self.logger.removeHandler, self.capture)
        self.thread = threading.get_ident()
        self.pid = 4242
        self.runner = types.SimpleNamespace(encoder_cache={} if cache is None else cache)
        self.monitor = core.OwnerMemoryMonitor(
            cuda=self.cuda,
            tensor_type=FakeTensor,
            output_types=(FakeAsyncOutput, FakeFinalOutput),
            logger=self.logger,
            clock_ns=self.clock,
            wall_ns=lambda: 1_760_000_000 * SECOND,
            thread_ident=threading.get_ident,
            getpid=lambda: self.pid,
        )

    def bind(self, seconds: float = 0) -> None:
        self.clock.at(seconds)
        self.monitor.bind_owner(runner=self.runner, device_index=0, startup={"facts": "test"})

    def warm(self, seconds: float) -> None:
        self.clock.at(seconds)
        self.monitor.after_warmup()

    def run_step(self, seconds: float, scheduler_output: Any = None,
                 result: Any = None) -> None:
        self.clock.at(seconds)
        self.monitor.before_execute(step() if scheduler_output is None else scheduler_output)
        self.monitor.after_execute(None)
        self.monitor.after_sample_tokens(FakeAsyncOutput() if result is None else result)


class EncoderStorageCensusTests(unittest.TestCase):
    def census(self, cache: Any, **kwargs: Any) -> dict[str, Any]:
        return core.encoder_cache_census(cache, tensor_type=FakeTensor, device_index=0, **kwargs)

    def test_physical_storage_separates_one_shared_parent_from_many_pinned_parents(self):
        one_parent = dict(zip("abcdefgh", split_views(0x10_0000_0000, 64 * MiB, [MiB] * 8)))
        many_parents = {
            name: split_views(0x20_0000_0000 + index * GiB, 64 * MiB, [MiB])[0]
            for index, name in enumerate("abcdef")
        }
        shared, pinned = self.census(one_parent), self.census(many_parents)
        self.assertEqual((shared["logical_bytes"], shared["physical_bytes"]), (8 * MiB, 64 * MiB))
        self.assertEqual((shared["storages"], shared["shared_storages"]), (1, 1))
        self.assertEqual(shared["max_entries_per_storage"], 8)
        self.assertEqual((pinned["logical_bytes"], pinned["physical_bytes"]), (6 * MiB, 384 * MiB))
        self.assertEqual((pinned["storages"], pinned["shared_storages"]), (6, 0))
        self.assertEqual(pinned["largest_storage_bytes"], 64 * MiB)

    def test_scheduled_free_releases_only_parents_without_a_surviving_view(self):
        a_first, a_second = split_views(0x30_0000_0000, 64 * MiB, [MiB, 2 * MiB])
        b_only = split_views(0x40_0000_0000, 32 * MiB, [MiB])[0]
        census = self.census(
            {"a1": a_first, "a2": a_second, "b1": b_only},
            project_free=True,
            scheduled_free=["a1", "b1", "evicted-earlier"],
        )
        projection = census["scheduled_free"]
        self.assertEqual(projection["state"], "available")
        self.assertEqual((projection["requested_entries"], projection["present_entries"]), (3, 2))
        self.assertEqual(projection["present_logical_bytes"], 2 * MiB)
        # a2 still references A, so dropping a1 frees nothing of A's 64 MiB.
        self.assertEqual(projection["storages_losing_all_cache_refs"], 1)
        self.assertEqual(projection["bytes_losing_all_cache_refs"], 32 * MiB)
        self.assertIsNone(self.census({"a1": a_first})["scheduled_free"])

    def test_aliased_storage_objects_are_unioned_by_address_range(self):
        base = 0x50_0000_0000
        cache = {
            "first_handle": FakeTensor(FakeStorage(base, 16 * MiB), MiB),
            "second_handle": FakeTensor(FakeStorage(base, 16 * MiB), MiB),
            "interior_alias": FakeTensor(FakeStorage(base + 4 * MiB, 4 * MiB), MiB),
            "adjacent_block": FakeTensor(FakeStorage(base + 16 * MiB, 8 * MiB), MiB),
        }
        census = self.census(cache)
        self.assertEqual(census["physical_bytes"], 24 * MiB)
        self.assertEqual(census["logical_bytes"], 4 * MiB)
        self.assertEqual((census["storages"], census["shared_storages"]), (3, 1))
        self.assertEqual(census["overlapping_storages"], 1)

    def test_excluded_entries_are_counted_and_never_summed_as_device_bytes(self):
        good = split_views(0x60_0000_0000, 8 * MiB, [MiB])[0]
        cache = {
            "good": good,
            "other_gpu": FakeTensor(FakeStorage(0x70_0000_0000, 8 * MiB), MiB, device=1),
            "host": FakeTensor(FakeStorage(0x1000, 8 * MiB), MiB, is_cuda=False),
            "not_a_tensor": [good],
            "no_storage": FakeTensor(RuntimeError("no storage"), MiB),
            "bad_metadata": FakeTensor(FakeStorage(0x80_0000_0000, 8 * MiB), 1.5),
            "zero_bytes": FakeTensor(FakeStorage(0, 0), 0),
        }
        census = self.census(cache)
        self.assertEqual(census["state"], "incomplete")
        self.assertEqual(census["entries"], 7)
        self.assertEqual(census["owner_device_tensors"], 2)
        self.assertEqual(census["foreign_device_tensors"], 2)
        self.assertEqual(census["non_tensor_entries"], 1)
        self.assertEqual(census["uninspectable_entries"], 2)
        self.assertEqual(census["uninspectable_reasons"], ["RuntimeError", "invalid_metadata"])
        self.assertEqual(census["zero_byte_storage_entries"], 1)
        self.assertEqual((census["logical_bytes"], census["physical_bytes"]), (MiB, 8 * MiB))

    def test_unreadable_cache_or_bound_overflow_is_unavailable_not_zero(self):
        too_many = {str(i): split_views(i * GiB, MiB, [MiB])[0] for i in range(5)}
        for cache, kwargs, reason in (
            (too_many, {"max_entries": 4}, "entry_bound_exceeded"),
            (None, {}, "encoder_cache_not_dict"),
        ):
            with self.subTest(reason=reason):
                census = self.census(cache, **kwargs)
                self.assertEqual((census["state"], census["reason"]), ("unavailable", reason))
                self.assertIsNone(census["logical_bytes"])
                self.assertIsNone(census["physical_bytes"])
        projection = self.census({}, project_free=True, scheduled_free=None)["scheduled_free"]
        self.assertEqual(projection["state"], "unavailable")
        self.assertIsNone(projection["bytes_losing_all_cache_refs"])


class AllocatorSectionTests(unittest.TestCase):
    def test_missing_or_malformed_metrics_are_null_and_named_never_zero(self):
        stats = native_stats(num_ooms=True, num_device_free=-1)
        del stats["inactive_split_bytes.all.current"]
        stats["segment.small_pool.current"] = 3.0
        section = core.allocator_section(FakeCuda(stats=stats), 0)
        self.assertEqual((section["state"], section["reason"]), ("incomplete", "required_metric_missing"))
        broken = ["inactive_split_bytes.all.current", "num_ooms", "num_device_free",
                  "segment.small_pool.current"]
        self.assertEqual(sorted(section["missing"]), sorted(broken))
        for key in broken:
            self.assertIsNone(section["stats"][key], key)
        self.assertEqual(section["stats"]["reserved_bytes.all.current"], 10 * GiB)

        optional_only = native_stats()
        del optional_only["num_sync_all_streams"]
        section = core.allocator_section(FakeCuda(stats=optional_only), 0)
        self.assertEqual(section["state"], "available")
        self.assertEqual(section["missing"], ["num_sync_all_streams"])

    def test_non_native_backend_and_empty_stats_report_no_allocator_numbers(self):
        zeros = {key: 0 for key in core.ALLOCATOR_KEYS}
        cuda = FakeCuda(stats=zeros, backend="cudaMallocAsync")
        section = core.allocator_section(cuda, 0)
        self.assertEqual(section["reason"], "unsupported_allocator_backend")
        self.assertIsNone(section["stats"])
        self.assertNotIn("memory_stats", cuda.calls)
        section = core.allocator_section(FakeCuda(stats={}), 0)
        self.assertEqual((section["state"], section["reason"]), ("unavailable", "empty_allocator_stats"))
        self.assertIsNone(section["stats"])


class OwnerMonitorTests(unittest.TestCase):
    def test_nothing_touches_cuda_before_owner_binding(self):
        harness = MonitorHarness(self)
        harness.warm(1)
        harness.run_step(2)
        self.assertEqual(harness.cuda.calls, [])
        self.assertEqual(harness.capture.records(), [])

    def test_foreign_thread_or_forked_pid_never_samples_and_reports_once(self):
        for stage in ("owner_thread", "owner_pid"):
            with self.subTest(stage=stage):
                harness = MonitorHarness(self)
                harness.bind(0)
                harness.warm(1)
                calls_after_warmup = list(harness.cuda.calls)

                def foreign() -> None:
                    for seconds in (100, 200, 300):
                        harness.run_step(seconds)

                if stage == "owner_thread":
                    worker = threading.Thread(target=foreign)
                    worker.start()
                    worker.join()
                else:
                    harness.pid += 1
                    foreign()
                self.assertEqual(harness.cuda.calls, calls_after_warmup)
                self.assertEqual([e["stage"] for e in harness.capture.errors()], [stage])
                self.assertEqual(len(harness.capture.samples()), 1)
                harness.pid = 4242
                harness.run_step(400)
                owner_sample = harness.capture.samples("step_entry")[0]
                self.assertEqual(owner_sample["counters"]["foreign_calls"], 9)
                self.assertEqual(len(harness.capture.errors()), 1)

    def test_uninitialized_cuda_or_other_current_device_blocks_allocator_reads(self):
        for cuda, stage, calls in (
            (FakeCuda(initialized=False), "cuda_not_initialized", ["is_initialized"]),
            (FakeCuda(current=1), "device_mismatch", ["is_initialized", "current_device"]),
        ):
            with self.subTest(stage=stage):
                harness = MonitorHarness(self, cuda)
                harness.bind(0)
                harness.warm(1)
                self.assertEqual(cuda.calls, calls)
                self.assertEqual([e["stage"] for e in harness.capture.errors()], [stage])
                self.assertEqual(harness.capture.samples(), [])

    def test_busy_steps_are_throttled_and_every_quiet_gap_gets_a_pre_update_sample(self):
        views = split_views(0x90_0000_0000, 64 * MiB, [MiB, MiB])
        harness = MonitorHarness(self, cache={"page-hash-1": views[0], "page-hash-2": views[1]})
        harness.bind(0)
        harness.warm(1)
        busy = [2 + index * 0.05 for index in range(2401)]  # continuous decode, 2 s .. 122 s
        for seconds in busy:
            harness.run_step(seconds)
        harness.run_step(722, step(tokens=4096, encoders=1, free=["page-hash-1"]))

        samples = harness.capture.samples()
        phases = [s["phase"] for s in samples]
        self.assertEqual(phases.count("post_warmup"), 1)
        self.assertEqual(phases.count("first_use"), 1)
        # 7206 hook calls produced: startup, baseline, first use, 4 busy samples, 1 post-gap.
        self.assertEqual(len(harness.capture.records()), 8)
        periodic = harness.capture.samples("step_entry")
        starts = [s["observed_at"]["monotonic_ns"] for s in samples]
        self.assertTrue(all(b - a >= 30 * SECOND for a, b in zip(starts[1:], starts[2:])))
        busy_samples = [s for s in periodic if s["observed_at"]["monotonic_ns"] <= 122 * SECOND]
        self.assertEqual(len(busy_samples), (122 - 2) // 30)

        first_use = harness.capture.samples("first_use")[0]
        self.assertEqual(first_use["context"]["output"], "async")
        after_gap = periodic[-1]
        self.assertGreaterEqual(after_gap["context"]["quiet_ns"], 599 * SECOND)
        # Only the step that was sampled at 122 s ran since that attempt: the gap was idle.
        self.assertEqual(after_gap["context"]["since_previous_attempt"],
                         {"steps": 1, "token_steps": 1, "encoder_steps": 0,
                          "scheduled_free_entries": 0})
        self.assertEqual(after_gap["context"]["scheduled"]["free_encoder_entries"], 1)
        # Taken before the runner pops this step's free list: both views are still cached.
        self.assertEqual(after_gap["encoder_cache"]["entries"], 2)
        projection = after_gap["encoder_cache"]["scheduled_free"]
        self.assertEqual((projection["present_entries"], projection["bytes_losing_all_cache_refs"]), (1, 0))
        for sample in samples:
            self.assertEqual(sample["device_work"], "not_synchronized")
        self.assertEqual(harness.capture.errors(), [])
        self.assertTrue(all("page-hash" not in line for line in harness.capture.lines))

    def test_clock_regression_is_reported_once_and_sampling_resumes(self):
        harness = MonitorHarness(self)
        harness.bind(100)
        harness.warm(101)
        harness.run_step(140)
        harness.run_step(50)
        harness.run_step(51)
        harness.run_step(81)
        harness.run_step(20)
        self.assertEqual([e["stage"] for e in harness.capture.errors()], ["clock_regression"])
        self.assertEqual(len(harness.capture.samples("step_entry")), 2)
        self.assertEqual(harness.capture.records()[-1]["counters"]["clock_regressions"], 1)
        harness.run_step(51)
        self.assertEqual(harness.capture.records()[-1]["counters"]["clock_regressions"], 2)
        self.assertEqual(len(harness.capture.errors()), 1)

    def test_sampling_failures_are_bounded_records_and_never_reach_serving(self):
        cuda = FakeCuda(stats=RuntimeError("allocator query failed"))
        harness = MonitorHarness(self, cuda, cache={"page": split_views(GiB, MiB, [MiB])[0]})
        harness.bind(0)
        harness.warm(1)
        baseline = harness.capture.samples("post_warmup")[0]
        self.assertEqual(baseline["allocator"]["state"], "error")
        self.assertEqual(baseline["allocator"]["error_type"], "RuntimeError")
        self.assertIsNone(baseline["allocator"]["stats"])
        self.assertEqual(baseline["encoder_cache"]["physical_bytes"], MiB)

        harness.clock.error = OSError("clock unavailable")
        for _ in range(50):
            harness.monitor.before_execute(step())
            harness.monitor.after_sample_tokens(FakeAsyncOutput())
        internal = [e for e in harness.capture.errors() if e["stage"] == "internal"]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["error_type"], "OSError")
        self.assertIsNone(internal[0]["observed_at"]["monotonic_ns"])

        class BrokenSink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                raise OSError("log sink unavailable")

        harness.clock.error = None
        broken = BrokenSink()
        harness.logger.addHandler(broken)
        for seconds in (100, 200, 300):
            harness.run_step(seconds)  # a sample is due each time and every emit raises
        harness.logger.removeHandler(broken)
        failures = harness.monitor._internal_failures
        harness.run_step(400)
        self.assertGreater(failures, 100)
        self.assertEqual(harness.capture.records()[-1]["counters"]["internal_failures"], failures)

    def test_base_exceptions_from_sampling_are_not_swallowed(self):
        cuda = FakeCuda(stats=KeyboardInterrupt())
        harness = MonitorHarness(self, cuda)
        harness.bind(0)
        with self.assertRaises(KeyboardInterrupt):
            harness.warm(1)


class FakeServingStack:
    """Fake vLLM/torch modules sufficient to run the real worker binding."""

    def __init__(self, test: unittest.TestCase, *, vllm_version: str = "0.21.0",
                 torch_version: str = "2.11.0+cu130") -> None:
        directory = tempfile.TemporaryDirectory(prefix="owner-memory-vllm-")
        test.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve() / "vllm"
        self.files: dict[str, Path] = {}
        for relative in ("__init__.py", *core.EXPECTED_VLLM_SOURCES):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"# fake {relative} {uuid.uuid4().hex}\n".encode())
            self.files[relative] = path
        self.digests = {
            relative: hashlib.sha256(self.files[relative].read_bytes()).hexdigest()
            for relative in core.EXPECTED_VLLM_SOURCES
        }
        self.upstream: list[str] = []
        self.forbidden: list[str] = []
        self.execute_outcome: Any = None
        self.sample_outcome: Any = None
        self.use_v2 = False
        self.initializes_cuda = True
        self.cuda = types.SimpleNamespace(initialized=False, current=0, stats=native_stats(),
                                          backend="native", mem=(4 * GiB, 16 * GiB))
        self.compilation_times = object()
        self.modules = self._modules(vllm_version, torch_version)

    def _module(self, name: str, **attributes: Any) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        return module

    def _modules(self, vllm_version: str, torch_version: str) -> dict[str, types.ModuleType]:
        stack = self

        def forbidden(namespace: str) -> Any:
            def lookup(name: str) -> Any:
                if name.startswith("__"):
                    raise AttributeError(name)

                def call(*args: Any, **kwargs: Any) -> None:
                    stack.forbidden.append(f"{namespace}.{name}")
                return call
            return lookup

        cuda = self._module(
            "torch.cuda",
            is_initialized=lambda: stack.cuda.initialized,
            current_device=lambda: stack.cuda.current,
            memory_stats=lambda device=None: dict(stack.cuda.stats),
            get_allocator_backend=lambda: stack.cuda.backend,
            mem_get_info=lambda device=None: stack.cuda.mem,
            __getattr__=forbidden("torch.cuda"),
        )
        torch = self._module("torch", __version__=torch_version, cuda=cuda, Tensor=FakeTensor,
                             __getattr__=forbidden("torch"))

        class GPUModelRunner:
            def __init__(self) -> None:
                self.encoder_cache: dict[str, Any] = {}

        runner_module = self._module("vllm.v1.worker.gpu_model_runner",
                                     __file__=str(self.files["v1/worker/gpu_model_runner.py"]),
                                     GPUModelRunner=GPUModelRunner)

        class Worker:
            def __init__(self, vllm_config: Any, local_rank: int, rank: int,
                         distributed_init_method: str, is_driver_worker: bool = False) -> None:
                stack.upstream.append("__init__")
                self.rank, self.local_rank = rank, local_rank
                self.parallel_config = types.SimpleNamespace(world_size=1,
                                                             distributed_executor_backend="uni")
                self.scheduler_config = types.SimpleNamespace(async_scheduling=True)
                self.use_v2_model_runner = stack.use_v2
                self.device: Any = None
                self.model_runner: Any = None

            def init_device(self) -> None:
                stack.upstream.append("init_device")
                sys.modules["vllm.v1.worker.gpu_model_runner"] = runner_module
                self.device = types.SimpleNamespace(type="cuda", index=0)
                self.model_runner = GPUModelRunner()
                stack.cuda.initialized = stack.initializes_cuda

            def compile_or_warm_up_model(self) -> Any:
                stack.upstream.append("compile_or_warm_up_model")
                return stack.compilation_times

            def execute_model(self, scheduler_output: Any) -> Any:
                stack.upstream.append("execute_model")
                if isinstance(stack.execute_outcome, BaseException):
                    raise stack.execute_outcome
                return stack.execute_outcome

            def sample_tokens(self, grammar_output: Any) -> Any:
                stack.upstream.append("sample_tokens")
                if isinstance(stack.sample_outcome, BaseException):
                    raise stack.sample_outcome
                return stack.sample_outcome

        self.runner_class = GPUModelRunner
        vllm = self._module("vllm", __version__=vllm_version,
                            __file__=str(self.files["__init__.py"]), __path__=[])
        worker_base = self._module("vllm.v1.worker.worker_base",
                                   __file__=str(self.files["v1/worker/worker_base.py"]))
        gpu_worker = self._module("vllm.v1.worker.gpu_worker",
                                  __file__=str(self.files["v1/worker/gpu_worker.py"]), Worker=Worker)
        worker_package = self._module("vllm.v1.worker", __path__=[], gpu_worker=gpu_worker,
                                      worker_base=worker_base)
        outputs = self._module("vllm.v1.outputs", AsyncModelRunnerOutput=FakeAsyncOutput,
                               ModelRunnerOutput=FakeFinalOutput)
        v1 = self._module("vllm.v1", __path__=[], worker=worker_package, outputs=outputs)
        vllm.__dict__["v1"] = v1
        return {
            "torch": torch,
            "vllm": vllm,
            "vllm.v1": v1,
            "vllm.v1.worker": worker_package,
            "vllm.v1.worker.worker_base": worker_base,
            "vllm.v1.worker.gpu_worker": gpu_worker,
            "vllm.v1.outputs": outputs,
        }


class WorkerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = Capture()
        self.logger = logging.getLogger("vllm.mineru_inference_memory")
        saved = (self.logger.level, self.logger.propagate)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.capture)

        def restore() -> None:
            self.logger.removeHandler(self.capture)
            self.logger.setLevel(saved[0])
            self.logger.propagate = saved[1]

        self.addCleanup(restore)
        directory = tempfile.TemporaryDirectory(prefix="owner-memory-identity-")
        self.addCleanup(directory.cleanup)
        self.identity_path = Path(directory.name) / "owner" / "owner.json"
        for target, value in (("OWNER_IDENTITY_PATH", self.identity_path),
                              ("current_process_start_ticks", lambda: 12345)):
            patcher = mock.patch.object(core, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        collect = mock.patch("gc.collect")
        self.gc_collect = collect.start()
        self.addCleanup(collect.stop)

    def serve(self, stack: FakeServingStack, scope: contextlib.ExitStack | None = None, *,
              reviewed_sources: bool = True) -> Any:
        """Import the real binding against ``stack``; sys.modules and pins are restored."""
        if scope is None:
            scope = contextlib.ExitStack()
            self.addCleanup(scope.close)
        scope.enter_context(mock.patch.dict(sys.modules, stack.modules))
        if reviewed_sources:
            scope.enter_context(mock.patch.dict(core.EXPECTED_VLLM_SOURCES, stack.digests))
        sys.modules.pop(f"{PACKAGE}.worker", None)
        return importlib.import_module(f"{PACKAGE}.worker")

    @staticmethod
    def construct(module: Any) -> Any:
        return module.OwnerMemoryWorker(vllm_config=object(), local_rank=0, rank=0,
                                        distributed_init_method="tcp://127.0.0.1:1",
                                        is_driver_worker=True)

    def test_missing_process_birth_refuses_startup_before_model_load(self):
        stack = FakeServingStack(self)
        worker = self.construct(self.serve(stack))
        for invalid in (None, 0, -1, True, "123"):
            with self.subTest(invalid=invalid), mock.patch.object(core, "current_process_start_ticks", lambda: invalid):
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    worker.init_device()
                self.assertFalse(self.identity_path.exists())
                self.assertNotIn("compile_or_warm_up_model", stack.upstream)
                # A refused initialization is a new worker construction on retry.
                worker = self.construct(self.serve(stack))

    def test_overrides_return_the_exact_upstream_objects_and_label_async_output(self):
        stack = FakeServingStack(self)
        worker = self.construct(self.serve(stack))
        self.assertFalse(self.identity_path.exists())
        self.assertIsNone(worker.init_device())
        identity = json.loads(self.identity_path.read_bytes())
        self.assertEqual(set(identity), {"contract_version", "worker_class", "module_sha256", "pid",
                                         "proc_start_ticks", "device", "instance_id"})
        self.assertEqual(identity["pid"], os.getpid())
        self.assertEqual(identity["proc_start_ticks"], 12345)
        self.assertEqual(identity["module_sha256"], "sha256:" + hashlib.sha256((PACKAGE_DIR / "worker.py").read_bytes()).hexdigest())
        self.assertEqual(identity["instance_id"], self.capture.records()[0]["instance"])
        self.assertIs(worker.compile_or_warm_up_model(), stack.compilation_times)
        views = split_views(0xA0_0000_0000, 64 * MiB, [MiB, MiB])
        worker.model_runner.encoder_cache.update({"mm-hash-a": views[0], "mm-hash-b": views[1]})
        async_output = FakeAsyncOutput()
        stack.sample_outcome = async_output
        self.assertIsNone(worker.execute_model(step(tokens=2048, encoders=1)))
        self.assertIs(worker.sample_tokens(None), async_output)
        final_output = FakeFinalOutput()
        stack.execute_outcome = final_output
        self.assertIs(worker.execute_model(step(tokens=0)), final_output)

        startup, baseline, first_use = self.capture.records()
        self.assertEqual(startup["kind"], "startup")
        self.assertEqual(startup["worker"]["class"], f"{PACKAGE}.worker.OwnerMemoryWorker")
        self.assertEqual(startup["sources"]["vllm"],
                         {name: "sha256:" + digest for name, digest in stack.digests.items()})
        self.assertEqual(startup["sources"]["diagnostic"], {
            name: "sha256:" + hashlib.sha256((PACKAGE_DIR / name).read_bytes()).hexdigest()
            for name in ("__init__.py", "core.py", "worker.py")
        })
        self.assertEqual((baseline["phase"], baseline["encoder_cache"]["entries"]), ("post_warmup", 0))
        self.assertEqual(first_use["phase"], "first_use")
        self.assertEqual(first_use["context"]["output"], "async")
        self.assertEqual(first_use["device_work"], "not_synchronized")
        self.assertEqual(first_use["encoder_cache"]["physical_bytes"], 64 * MiB)
        self.assertEqual(stack.upstream, ["__init__", "init_device", "compile_or_warm_up_model",
                                          "execute_model", "sample_tokens", "execute_model"])
        self.assertEqual(stack.forbidden, [])
        self.gc_collect.assert_not_called()
        self.assertTrue(all("mm-hash" not in line for line in self.capture.lines))

    def test_upstream_exceptions_propagate_unchanged_and_nothing_is_sampled_after_them(self):
        class UpstreamFailure(Exception):
            pass

        class Interrupted(KeyboardInterrupt):
            pass

        stack = FakeServingStack(self)
        worker = self.construct(self.serve(stack))
        worker.init_device()
        worker.compile_or_warm_up_model()
        for method, argument, attribute, error in (
            ("execute_model", step(tokens=64), "execute_outcome", UpstreamFailure("forward")),
            ("sample_tokens", None, "sample_outcome", Interrupted()),
        ):
            with self.subTest(method=method):
                setattr(stack, attribute, error)
                calls = len(stack.upstream)
                try:
                    getattr(worker, method)(argument)
                except type(error) as caught:
                    propagated, tail = caught, caught.__traceback__
                else:
                    self.fail(f"{method} did not propagate the upstream exception")
                self.assertIs(propagated, error)
                self.assertIsNone(propagated.__context__)
                while tail is not None and tail.tb_next is not None:
                    tail = tail.tb_next
                self.assertIsNotNone(tail)
                self.assertEqual(tail.tb_frame.f_code.co_name, method)
                self.assertEqual(len(stack.upstream), calls + 1)
        self.assertEqual([r.get("phase") for r in self.capture.records()], [None, "post_warmup"])

    def test_unreviewed_versions_sources_or_bases_fail_before_upstream_initialization(self):
        cases = (
            ("vllm version", {"vllm_version": "0.21.1"}, True, None),
            ("torch version", {"torch_version": "2.12.0+cu130"}, True, None),
            ("source bytes", {}, False, None),
            ("extended bases", {}, True, "extension"),
        )
        for name, versions, reviewed, mutation in cases:
            with self.subTest(case=name), contextlib.ExitStack() as scope:
                stack = FakeServingStack(self, **versions)
                module = self.serve(stack, scope, reviewed_sources=reviewed)
                if mutation == "extension":
                    module.OwnerMemoryWorker.__bases__ += (type("Extension", (), {}),)
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    self.construct(module)
                self.assertEqual(stack.upstream, [])

    def test_unreviewed_runtime_state_fails_after_device_setup_before_any_model_work(self):
        def unroutable(stack: FakeServingStack, worker: Any) -> None:
            self.logger.removeHandler(self.capture)

        cases = (
            ("v2 runner", lambda stack, worker: setattr(worker, "use_v2_model_runner", True)),
            ("cuda never initialized",
             lambda stack, worker: setattr(stack, "initializes_cuda", False)),
            ("other current device", lambda stack, worker: setattr(stack.cuda, "current", 1)),
            ("records unroutable", unroutable),
        )
        for name, mutate in cases:
            with self.subTest(case=name), contextlib.ExitStack() as scope:
                self.logger.addHandler(self.capture)
                stack = FakeServingStack(self)
                worker = self.construct(self.serve(stack, scope))
                mutate(stack, worker)
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    worker.init_device()
                self.assertEqual(stack.upstream, ["__init__", "init_device"])

    def test_imports_create_no_cuda_context_thread_or_framework_import(self):
        probe = (
            "import sys, threading; sys.path.insert(0, sys.argv[1]); "
            f"import {PACKAGE}.core; "
            "assert not {'torch', 'vllm'} & set(sys.modules), sorted(sys.modules); "
            "assert threading.active_count() == 1"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(PACKAGE_DIR.parent)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        stack = FakeServingStack(self)
        threads = threading.active_count()
        self.serve(stack)
        self.assertEqual(threading.active_count(), threads)
        self.assertEqual(stack.forbidden, [])
        self.assertFalse(stack.cuda.initialized)


if __name__ == "__main__":
    unittest.main()
