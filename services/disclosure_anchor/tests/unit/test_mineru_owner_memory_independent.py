"""Independent counterexamples for read-only GPU owner observation (CPU fakes only)."""

from __future__ import annotations

import ast
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


_PACKAGE = Path(__file__).resolve().parents[2] / "scripts/windows/mineru_inference_memory"
_CORE_NAME = "_independent_mineru_owner_memory_core"
_spec = importlib.util.spec_from_file_location(_CORE_NAME, _PACKAGE / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
sys.modules[_CORE_NAME] = core
_spec.loader.exec_module(core)


class Storage:
    def __init__(self, address, size):
        self.address, self.size = address, size

    def data_ptr(self):
        return self.address

    def nbytes(self):
        return self.size


class Tensor:
    is_cuda = True

    def __init__(self, storage, logical, device=0):
        self.storage, self.nbytes, self.device = storage, logical, device

    def get_device(self):
        return self.device

    def untyped_storage(self):
        return self.storage


class Cuda:
    def __init__(self):
        self.calls = []
        self.initialized = True
        self.device = 0
        self.backend = "native"
        self.stats = {key: 7 for key in core.ALLOCATOR_KEYS}
        self.failure = None

    def is_initialized(self):
        self.calls.append("is_initialized")
        return self.initialized

    def current_device(self):
        self.calls.append("current_device")
        return self.device

    def memory_stats(self, device):
        self.calls.append(("memory_stats", device))
        if self.failure:
            raise self.failure
        return self.stats

    def allocator_backend(self):
        self.calls.append("allocator_backend")
        return self.backend

    def mem_get_info(self, device):
        self.calls.append(("mem_get_info", device))
        return (100, 1000)

    def __getattr__(self, name):
        raise AssertionError(f"unapproved CUDA operation: {name}")


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.values = []

    def emit(self, record):
        self.values.append(json.loads(record.getMessage().split(" ", 1)[1]))


def scheduler(*, tokens=0, free=()):
    return SimpleNamespace(
        total_num_scheduled_tokens=tokens,
        num_scheduled_tokens={"private-request": tokens} if tokens else {},
        scheduled_encoder_inputs={}, finished_req_ids=set(),
        free_encoder_mm_hashes=list(free),
    )


def monitor():
    cuda = Cuda()
    clock = SimpleNamespace(now=0, pid=13, thread=17)
    records = Records()
    logger = logging.Logger("independent-owner-test", logging.INFO)
    logger.addHandler(records)
    runner = SimpleNamespace(encoder_cache={})
    observer = core.OwnerMemoryMonitor(
        cuda=cuda, tensor_type=Tensor, output_types=(bytes, tuple), logger=logger,
        clock_ns=lambda: clock.now, wall_ns=lambda: 1,
        thread_ident=lambda: clock.thread, getpid=lambda: clock.pid,
    )
    observer.bind_owner(runner=runner, device_index=0, startup={})
    return observer, cuda, clock, records.values, runner


@contextmanager
def fake_worker():
    """Import real override source against controlled upstream, without torch/vLLM."""
    package_name = "mineru_inference_memory"
    package = ModuleType(package_name)
    package.__path__ = [str(_PACKAGE)]
    torch = ModuleType("torch")
    torch.Tensor, torch.__version__, torch.cuda = Tensor, "2.11.0+cu128", Cuda()
    vllm = ModuleType("vllm")
    vllm.__version__, vllm.__file__ = "0.21.0", "/unused/vllm/__init__.py"
    outputs = ModuleType("vllm.v1.outputs")
    outputs.AsyncModelRunnerOutput, outputs.ModelRunnerOutput = bytes, tuple
    gpu = ModuleType("vllm.v1.worker.gpu_worker")
    base = ModuleType("vllm.v1.worker.worker_base")
    worker_package = ModuleType("vllm.v1.worker")
    worker_package.gpu_worker, worker_package.worker_base = gpu, base

    class Upstream:
        def __init__(self, *args, **kwargs):
            self.calls = []
            self.result = object()
            self.failure = None

        def invoke(self, name, *args):
            self.calls.append((name, args))
            if self.failure is not None:
                raise self.failure
            return self.result

        def init_device(self):
            return self.invoke("init_device")

        def compile_or_warm_up_model(self):
            return self.invoke("compile_or_warm_up_model")

        def execute_model(self, output):
            return self.invoke("execute_model", output)

        def sample_tokens(self, output):
            return self.invoke("sample_tokens", output)

    gpu.Worker = Upstream
    modules = {package_name: package, f"{package_name}.core": core,
               "torch": torch, "vllm": vllm, "vllm.v1": ModuleType("vllm.v1"),
               "vllm.v1.outputs": outputs, "vllm.v1.worker": worker_package,
               "vllm.v1.worker.gpu_worker": gpu, "vllm.v1.worker.worker_base": base}
    with patch.dict(sys.modules, modules):
        name = f"{package_name}.worker"
        spec = importlib.util.spec_from_file_location(name, _PACKAGE / "worker.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        try:
            yield module, torch
        finally:
            sys.modules.pop(name, None)


@contextmanager
def collector_probe():
    """Execute the actual byte-only collector Python with disposable files/proc facts."""
    source = (_PACKAGE.parent / "collect_mineru_runtime.ps1").read_text()
    code = source.split("$diagnosticProbe = @'\n", 1)[1].split("\n'@", 1)[0]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name in ("mineru_inference_memory/__init__.py", "mineru_inference_memory/core.py",
                     "mineru_inference_memory/worker.py", "vllm/v1/worker/worker_base.py",
                     "vllm/v1/worker/gpu_worker.py", "vllm/v1/worker/gpu_model_runner.py",
                     "vllm/v1/worker/gpu/mm/encoder_runner.py"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("# deterministic source " + name).encode())
        observer, _, _, _, _ = monitor()
        marker = root / "owner.json"
        identity = observer.publish_owner_identity(module_path=root / "mineru_inference_memory/worker.py",
            worker_class=core.OWNER_WORKER_CLASS, path=marker, read_start_ticks=lambda: 222)
        def proc(pid, ticks, state="R"):
            return (f"{pid} (owner process) " + " ".join([state] + ["0"] * 18 + [str(ticks)])).encode()
        processes = {"/proc/13/stat": proc(13, 222), "/proc/1/stat": proc(1, 100)}
        original_open, original_path_open = os.open, Path.open
        def open_fd(path, flags, *args, **kwargs):
            if str(path) == "/tmp/mineru-owner-memory/owner.json":
                path = marker
            return original_open(path, flags, *args, **kwargs)
        def open_path(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                if str(path) not in processes:
                    raise FileNotFoundError(str(path))
                return io.BytesIO(processes[str(path)])
            return original_path_open(path, *args, **kwargs)
        def run():
            output = io.StringIO()
            with patch("importlib.metadata.distribution", return_value=SimpleNamespace(locate_file=lambda _: root)), \
                    patch.object(os, "open", side_effect=open_fd), \
                    patch.object(Path, "open", open_path), redirect_stdout(output):
                exec(compile(code, "collect_mineru_runtime.ps1:diagnosticProbe", "exec"), {})
            return json.loads(output.getvalue())
        yield run, marker, identity, processes, proc


class IndependentOwnerMemoryTests(unittest.TestCase):
    def census(self, cache, **kwargs):
        return core.encoder_cache_census(cache, tensor_type=Tensor, device_index=0, **kwargs)

    def test_split_views_retain_whole_distinct_parents_and_free_is_only_projection(self):
        # Two historical batches; the visible pieces are much smaller than their parents.
        first, second = Storage(10_000, 4096), Storage(30_000, 8192)
        cache = {"private-a": Tensor(first, 64), "private-b": Tensor(first, 128),
                 "private-c": Tensor(second, 32)}
        snapshot = dict(cache)
        partial = self.census(cache, project_free=True, scheduled_free=["private-a"])
        self.assertEqual(partial["logical_bytes"], 224)
        self.assertEqual(partial["physical_bytes"], 12_288)
        self.assertEqual(partial["storages"], 2)
        self.assertEqual(partial["shared_storages"], 1)
        self.assertEqual(partial["scheduled_free"]["bytes_losing_all_cache_refs"], 0)
        complete = self.census(cache, project_free=True,
                               scheduled_free=["private-a", "private-b", "missing"])
        self.assertEqual(complete["scheduled_free"]["bytes_losing_all_cache_refs"], 4096)
        self.assertEqual(complete["scheduled_free"]["present_entries"], 2)
        self.assertEqual(cache, snapshot)
        for key, value in snapshot.items():
            self.assertIs(cache[key], value)
        cache.pop("private-a")
        self.assertEqual(self.census(cache)["physical_bytes"], 12_288)
        cache.pop("private-b")
        self.assertEqual(self.census(cache)["physical_bytes"], 8192)
        self.assertNotIn("private-", json.dumps(complete))
        self.assertNotIn("30000", json.dumps(complete))

    def test_alias_storage_wrappers_and_adjacent_allocations_do_not_inflate_or_collapse(self):
        cache = {"a": Tensor(Storage(1000, 256), 8),
                 "b": Tensor(Storage(1000, 256), 8),
                 "c": Tensor(Storage(1256, 256), 8)}
        actual = self.census(cache)
        self.assertEqual((actual["physical_bytes"], actual["storages"],
                          actual["max_entries_per_storage"]), (512, 2, 2))
        self.assertEqual(actual["overlapping_storages"], 0)

    def test_inaccessible_tensor_metadata_is_visible_and_never_reported_as_empty(self):
        class Broken(Tensor):
            def untyped_storage(self):
                raise RuntimeError("secret tensor detail")

        actual = self.census({"broken": Broken(None, 64)}, project_free=True,
                             scheduled_free=["broken"])
        self.assertEqual(actual["state"], "incomplete")
        self.assertEqual(actual["uninspectable_entries"], 1)
        self.assertEqual(actual["uninspectable_reasons"], ["RuntimeError"])
        self.assertEqual(actual["scheduled_free"]["state"], "incomplete")
        self.assertNotIn("secret", json.dumps(actual))
        unavailable = self.census(None)
        self.assertIsNone(unavailable["physical_bytes"])
        self.assertEqual(unavailable["state"], "unavailable")

    def test_foreign_owner_and_uninitialized_device_never_reach_cuda_queries(self):
        for attribute in ("pid", "thread"):
            observer, cuda, state, records, _ = monitor()
            setattr(state, attribute, getattr(state, attribute) + 1)
            for _ in range(50):
                observer.after_warmup()
                observer.before_execute(scheduler(tokens=1))
                observer.after_execute((1,))
            self.assertEqual(cuda.calls, [])
            self.assertEqual([r["kind"] for r in records], ["startup", "error"])
            self.assertEqual(records[-1]["stage"], "owner_" + attribute)
        observer, cuda, _, records, _ = monitor()
        cuda.initialized = False
        observer.after_warmup()
        self.assertEqual(cuda.calls, ["is_initialized"])
        self.assertEqual(records[-1]["stage"], "cuda_not_initialized")

    def test_rate_limit_and_deferred_sampling_preserve_asynchrony(self):
        observer, cuda, state, records, _ = monitor()
        observer.after_warmup()
        observer.before_execute(scheduler(tokens=1))
        observer.after_execute(None)
        self.assertEqual(len(records), 2)
        observer.after_sample_tokens(b"async-output")
        self.assertEqual(records[-1]["phase"], "first_use")
        self.assertEqual(records[-1]["context"]["output"], "async")
        count = len(cuda.calls)
        for _ in range(100):
            observer.before_execute(scheduler(tokens=1))
            observer.after_execute((1,))
        self.assertEqual(len(cuda.calls), count)
        state.now = 30_000_000_000 - 1
        observer.before_execute(scheduler())
        self.assertEqual(len(cuda.calls), count)
        state.now += 1
        observer.before_execute(scheduler())
        self.assertEqual(records[-1]["phase"], "step_entry")
        self.assertEqual(records[-1]["device_work"], "not_synchronized")
        self.assertEqual(records[-1]["context"]["since_previous_attempt"]["steps"], 101)
        self.assertTrue(all(r["device_work"] == "not_synchronized"
                            for r in records if r["kind"] == "sample"))

    def test_activity_window_is_since_attempt_even_when_sample_cannot_be_emitted(self):
        observer, cuda, state, records, _ = monitor()
        observer.after_warmup()
        observer.before_execute(scheduler())
        state.now = 30_000_000_000
        cuda.initialized = False
        observer.before_execute(scheduler())
        self.assertEqual(records[-1]["stage"], "cuda_not_initialized")
        state.now += 30_000_000_000
        cuda.initialized = True
        observer.before_execute(scheduler())
        self.assertEqual(records[-1]["kind"], "sample")
        self.assertEqual(records[-1]["context"]["since_previous_attempt"]["steps"], 1)
        self.assertNotIn("since_previous_record", records[-1]["context"])

    def test_allocator_unknowns_and_failures_remain_observable(self):
        cuda = Cuda()
        cuda.stats = {"allocated_bytes.all.current": 0, "num_ooms": False}
        result = core.allocator_section(cuda, 0)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["stats"]["allocated_bytes.all.current"], 0)
        self.assertIsNone(result["stats"]["reserved_bytes.all.current"])
        self.assertIsNone(result["stats"]["num_ooms"])
        cuda.backend = "cudaMallocAsync"
        cuda.calls.clear()
        result = core.allocator_section(cuda, 0)
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["stats"])
        self.assertEqual(cuda.calls, ["allocator_backend"])
        observer, cuda, _, records, _ = monitor()
        cuda.failure = RuntimeError("private failure detail")
        observer.after_warmup()
        sample = records[-1]
        self.assertEqual(sample["allocator"]["state"], "error")
        self.assertEqual(sample["allocator"]["error_type"], "RuntimeError")
        self.assertIsNone(sample["allocator"]["stats"])
        self.assertEqual(sample["device_memory"]["state"], "available")
        self.assertNotIn("private failure", json.dumps(sample))

    def test_internal_diagnostic_failure_is_rate_limited_without_raising(self):
        observer, cuda, state, records, _ = monitor()
        observer.after_warmup()
        class BrokenScheduler:
            @property
            def total_num_scheduled_tokens(self):
                raise ValueError("private scheduler detail")
        for _ in range(20):
            observer.before_execute(BrokenScheduler())
        errors = [r for r in records if r["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["stage"], errors[0]["error_type"]),
                         ("internal", "ValueError"))
        state.now = 30_000_000_000
        observer.before_execute(BrokenScheduler())
        self.assertEqual(records[-1]["counters"]["internal_failures"], 21)
        self.assertNotIn("private scheduler detail", json.dumps(records))

    def test_clock_failure_is_reported_with_unknown_time_and_serving_continues(self):
        observer, cuda, _, records, _ = monitor()
        def broken_clock():
            raise OSError("clock sentinel")
        observer._clock_ns = broken_clock
        for _ in range(10):
            observer.after_warmup()
        errors = [r for r in records if r["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["stage"], "internal")
        self.assertEqual(errors[0]["error_type"], "OSError")
        self.assertIsNone(errors[0]["observed_at"]["monotonic_ns"])
        self.assertEqual(cuda.calls, [])

    def test_broken_log_handler_cannot_prevent_upstream_inference(self):
        class BrokenHandler(logging.Handler):
            def emit(self, record):
                raise OSError("log sink unavailable")
        with fake_worker() as (module, _):
            with patch.object(module, "_static_preconditions", return_value={}):
                worker = module.OwnerMemoryWorker()
            observer, _, state, records, _ = monitor()
            observer.after_warmup()
            original_logger = observer._logger
            logger = logging.Logger("independent-broken-sink", logging.INFO)
            logger.addHandler(BrokenHandler())
            observer._logger = logger
            worker._mineru_owner_memory = observer
            state.now = 30_000_000_000
            result = object()
            worker.result = result
            output = scheduler(tokens=1)
            self.assertIs(worker.execute_model(output), result)
            self.assertEqual(worker.calls, [("execute_model", (output,))])
            failures = observer._internal_failures
            self.assertGreater(failures, 0)
            observer._logger = original_logger
            state.now += 30_000_000_000
            self.assertIs(worker.execute_model(scheduler()), result)
            self.assertGreaterEqual(records[-1]["counters"]["internal_failures"], failures)
            self.assertEqual(records[-1]["kind"], "sample")

    def test_worker_import_is_cuda_inert_and_returns_or_raises_exact_upstream_object(self):
        with fake_worker() as (module, torch):
            self.assertEqual(torch.cuda.calls, [])
            with patch.object(module, "_static_preconditions", return_value={}):
                worker = module.OwnerMemoryWorker()
            observer, cuda, _, records, _ = monitor()
            observer.after_warmup()
            worker._mineru_owner_memory = observer
            cuda.failure = RuntimeError("allocator unavailable")
            for method, args in (("compile_or_warm_up_model", ()),
                                 ("execute_model", (scheduler(tokens=1),)),
                                 ("sample_tokens", (object(),))):
                worker.calls.clear()
                result = object()
                worker.result = result
                self.assertIs(getattr(worker, method)(*args), result)
                self.assertEqual(worker.calls, [(method, args)])
                failure = LookupError("upstream sentinel")
                worker.failure = failure
                worker.calls.clear()
                with self.assertRaises(LookupError) as caught:
                    getattr(worker, method)(*args)
                self.assertIs(caught.exception, failure)
                self.assertEqual(worker.calls, [(method, args)])
                worker.failure = None
            self.assertTrue(any(r.get("allocator", {}).get("state") == "error"
                                for r in records))

    def test_init_device_preserves_upstream_return_and_checks_only_after_success(self):
        with fake_worker() as (module, torch), tempfile.TemporaryDirectory() as temporary:
            with patch.object(module, "_static_preconditions", return_value={}):
                worker = module.OwnerMemoryWorker()
            worker.model_runner = SimpleNamespace(encoder_cache={})
            with patch.object(module, "_runtime_preconditions", return_value=0) as checks, \
                    patch.object(module, "_startup_facts", return_value={}), \
                    patch.object(core, "OWNER_IDENTITY_PATH", Path(temporary) / "owner.json"), \
                    patch.object(core, "current_process_start_ticks", return_value=12345):
                result = worker.result
                self.assertIs(worker.init_device(), result)
                checks.assert_called_once_with(worker)
                marker = json.loads((Path(temporary) / "owner.json").read_text())
                self.assertEqual(marker["pid"], os.getpid())
                self.assertEqual(marker["proc_start_ticks"], 12345)
                self.assertEqual(marker["instance_id"], worker._mineru_owner_memory._instance)
                self.assertEqual(marker["module_sha256"],
                                 "sha256:" + hashlib.sha256((_PACKAGE / "worker.py").read_bytes()).hexdigest())
                self.assertEqual(worker.calls, [("init_device", ())])
                checks.reset_mock()
                failure = RuntimeError("init sentinel")
                worker.failure = failure
                with self.assertRaises(RuntimeError) as caught:
                    worker.init_device()
                self.assertIs(caught.exception, failure)
                checks.assert_not_called()
            self.assertEqual(torch.cuda.calls, [])

    def test_owner_marker_is_closed_correlated_and_atomically_replaced(self):
        observer, cuda, _, records, runner = monitor()
        sentinel = Tensor(Storage(10_000, 4096), 32)
        runner.encoder_cache["private-entry"] = sentinel
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner.json"
            identity = observer.publish_owner_identity(
                module_path=_PACKAGE / "worker.py", worker_class=core.OWNER_WORKER_CLASS,
                path=path, read_start_ticks=lambda: 222)
            literal = {"contract_version": "mineru.owner-memory-identity.v1",
                       "worker_class": "mineru_inference_memory.worker.OwnerMemoryWorker",
                       "module_sha256": "sha256:" + hashlib.sha256((_PACKAGE / "worker.py").read_bytes()).hexdigest(),
                       "pid": 13, "proc_start_ticks": 222, "device": "cuda:0",
                       "instance_id": records[0]["instance"]}
            self.assertEqual(identity, literal)
            self.assertEqual(json.loads(path.read_bytes()), literal)
            self.assertLessEqual(path.stat().st_size, 16_384)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            old = path.read_bytes()
            replacement = {**identity, "instance_id": "new-instance"}
            original_replace = os.replace
            observed = []
            def inspect_replace(source, destination, **kwargs):
                self.assertEqual(path.read_bytes(), old)
                staged = path.parent / source
                self.assertEqual(json.loads(staged.read_bytes()), replacement)
                self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
                observed.append((source, destination))
                return original_replace(source, destination, **kwargs)
            with patch.object(core.os, "replace", side_effect=inspect_replace):
                core.write_owner_identity(path, replacement)
            self.assertEqual(len(observed), 1)
            self.assertEqual(json.loads(path.read_bytes()), replacement)
            self.assertEqual(list(path.parent.iterdir()), [path])
            with patch.object(core.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    core.write_owner_identity(path, identity)
            self.assertEqual(json.loads(path.read_bytes()), replacement)
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(cuda.calls, [])
        self.assertEqual(runner.encoder_cache, {"private-entry": sentinel})

    def test_owner_marker_refuses_foreign_or_missing_owner_and_invalid_process_birth(self):
        for mutate in (lambda o, s: setattr(o, "_owner", None),
                       lambda o, s: setattr(s, "pid", 99),
                       lambda o, s: setattr(s, "thread", 99)):
            observer, cuda, state, _, _ = monitor()
            mutate(observer, state)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "owner.json"
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    observer.publish_owner_identity(module_path=_PACKAGE / "worker.py",
                        worker_class=core.OWNER_WORKER_CLASS, path=path, read_start_ticks=lambda: 222)
                self.assertFalse(path.exists())
            self.assertEqual(cuda.calls, [])
        for ticks in (None, 0, -1, True, "222"):
            observer, cuda, _, _, _ = monitor()
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "owner.json"
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    observer.publish_owner_identity(module_path=_PACKAGE / "worker.py",
                        worker_class=core.OWNER_WORKER_CLASS, path=path, read_start_ticks=lambda: ticks)
                self.assertFalse(path.exists())
            self.assertEqual(cuda.calls, [])

    def test_owner_marker_refuses_extra_fields_unsafe_targets_and_oversized_preimage(self):
        observer, _, _, _, _ = monitor()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "owner.json"
            identity = observer.publish_owner_identity(module_path=_PACKAGE / "worker.py",
                worker_class=core.OWNER_WORKER_CLASS, path=path, read_start_ticks=lambda: 222)
            old = path.read_bytes()
            for changes in ({"extra": "no"}, {"pid": True}, {"device": "cuda:1"},
                            {"proc_start_ticks": 0}, {"module_sha256": "sha256:bad"},
                            {"worker_class": "unreviewed.Worker"}, {"instance_id": "x" * 16_385}):
                with self.subTest(changes=changes), self.assertRaises(core.OwnerMemoryPreconditionError):
                    core.write_owner_identity(path, {**identity, **changes})
                self.assertEqual(path.read_bytes(), old)
            target = root / "target"
            target.write_bytes(b"protected")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(core.OwnerMemoryPreconditionError):
                core.write_owner_identity(path, identity)
            self.assertEqual(target.read_bytes(), b"protected")
            path.unlink()
            path.write_bytes(b"x" * 16_385)
            with self.assertRaises(core.OwnerMemoryPreconditionError):
                core.write_owner_identity(path, identity)
            self.assertEqual(path.stat().st_size, 16_385)

    def test_process_birth_reader_rejects_absent_malformed_dead_or_foreign_process(self):
        def raw(pid=13, state="R", ticks="222"):
            return f"{pid} (owner ) odd name) ".encode() + " ".join(
                [state] + ["0"] * 18 + [ticks] + ["0"] * 5).encode()
        with patch.object(core.os, "getpid", return_value=13):
            with patch.object(Path, "open", return_value=io.BytesIO(raw())):
                self.assertEqual(core.current_process_start_ticks(), 222)
            for value in (raw(pid=99), raw(state="Z"), raw(state="X"), raw(ticks="0"),
                          raw(ticks="-1"), raw(ticks="unknown"), b"broken", b"x" * 8193):
                with self.subTest(value=value[:80]), patch.object(Path, "open", return_value=io.BytesIO(value)):
                    with self.assertRaises(core.OwnerMemoryPreconditionError):
                        core.current_process_start_ticks()
            with patch.object(Path, "open", side_effect=FileNotFoundError):
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    core.current_process_start_ticks()

    def test_uninitialized_upstream_worker_cannot_publish_marker(self):
        with fake_worker() as (module, torch), tempfile.TemporaryDirectory() as temporary:
            module.LOGGER = monitor()[0]._logger
            with patch.object(module, "_static_preconditions", return_value={}):
                worker = module.OwnerMemoryWorker()
            class Runner:
                encoder_cache = {}
            worker.model_runner = Runner()
            worker.use_v2_model_runner = False
            worker.device = SimpleNamespace(type="cuda", index=0)
            runner_module = SimpleNamespace(__file__="/unused/gpu_model_runner.py", GPUModelRunner=Runner)
            path = Path(temporary) / "owner.json"
            with patch.dict(sys.modules, {"vllm.v1.worker.gpu_model_runner": runner_module}), \
                    patch.object(module, "require_loaded_file"), \
                    patch.object(module, "file_sha256", return_value=core.EXPECTED_VLLM_SOURCES["v1/worker/gpu_model_runner.py"]), \
                    patch.object(module, "_startup_facts", return_value={}), \
                    patch.object(core, "OWNER_IDENTITY_PATH", path), \
                    patch.object(core, "current_process_start_ticks", return_value=222):
                torch.cuda.initialized = False
                with self.assertRaises(core.OwnerMemoryPreconditionError):
                    worker.init_device()
                self.assertFalse(path.exists())
                self.assertEqual(torch.cuda.calls, ["is_initialized"])
                torch.cuda.initialized = True
                self.assertIs(worker.init_device(), worker.result)
                self.assertEqual(json.loads(path.read_bytes())["pid"], os.getpid())
                self.assertEqual(torch.cuda.calls, ["is_initialized", "is_initialized", "current_device"])

    def test_collector_agrees_with_live_process_and_actual_module_without_cuda_import(self):
        with collector_probe() as (run, marker, identity, _, _):
            old = marker.read_bytes()
            torch_before, vllm_before = sys.modules.get("torch"), sys.modules.get("vllm")
            result = run()
            self.assertEqual(result["loaded_workers"], [{**identity,
                "observed_proc_start_ticks": 222, "namespace_init_start_ticks": 100}])
            self.assertEqual(result["module_sha256"], identity["module_sha256"])
            self.assertEqual(marker.read_bytes(), old)
            self.assertIs(sys.modules.get("torch"), torch_before)
            self.assertIs(sys.modules.get("vllm"), vllm_before)

    def test_collector_rejects_stale_dead_absent_or_mismatched_owner_instead_of_defaulting(self):
        with collector_probe() as (run, marker, identity, processes, proc):
            valid = marker.read_bytes()
            for value in (proc(13, 223), proc(13, 222, "Z"), proc(13, 222, "X"), b"malformed"):
                with self.subTest(proc=value):
                    processes["/proc/13/stat"] = value
                    with self.assertRaises((RuntimeError, ValueError, IndexError)):
                        run()
            processes.pop("/proc/13/stat")
            with self.assertRaises(FileNotFoundError):
                run()
            processes["/proc/13/stat"] = proc(13, 222)
            processes["/proc/1/stat"] = proc(1, 300)
            with self.assertRaises(RuntimeError):
                run()
            processes["/proc/1/stat"] = proc(1, 100)
            for value in (json.dumps({**identity, "extra": 1}).encode(),
                          json.dumps({**identity, "module_sha256": "sha256:" + "0" * 64}).encode(),
                          valid[:-1] + b',"pid":13}', b"x" * 16_385):
                with self.subTest(marker=value[:100]):
                    marker.write_bytes(value)
                    with self.assertRaises((RuntimeError, ValueError)):
                        run()
            marker.write_bytes(valid)
            self.assertEqual(run()["loaded_workers"][0]["pid"], 13)

    def test_exact_versions_and_loaded_file_identity_fail_closed(self):
        core.verify_versions(vllm_version="0.21.0", torch_version="2.11.0+cu128")
        for vllm, torch in (("0.21.1", "2.11.0"), ("0.21.0", "2.11.1"),
                            ("0.21.0+local", "2.11.0"), ("0.21.0", None)):
            with self.subTest(vllm=vllm, torch=torch), self.assertRaises(
                    core.OwnerMemoryPreconditionError):
                core.verify_versions(vllm_version=vllm, torch_version=torch)
        with self.assertRaises(core.OwnerMemoryPreconditionError):
            core.require_loaded_file(str(_PACKAGE / "worker.py"), _PACKAGE / "core.py")

    def test_observation_source_has_no_mutator_synchronizer_or_thread_creation(self):
        forbidden = {"empty_cache", "synchronize", "gc", "collect", "Thread", "Timer",
                     "set_device", "set_per_process_memory_fraction", "memory_snapshot"}
        for filename in ("core.py", "worker.py"):
            tree = ast.parse((_PACKAGE / filename).read_text())
            calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                     for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, (ast.Attribute, ast.Name))}
            self.assertFalse(calls & forbidden, (filename, calls & forbidden))
        tree = ast.parse((_PACKAGE / "core.py").read_text())
        imports = {node.module or "" for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree)
                       if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse(any(name.split(".")[0] in {"torch", "vllm"} for name in imports))


if __name__ == "__main__":
    unittest.main()
