"""vLLM ``--worker-cls`` entry: the reviewed GPU Worker plus owner-memory records.

Selected with ``--worker-cls mineru_inference_memory.worker.OwnerMemoryWorker``.
vLLM imports this module in the engine process while resolving the worker
class. The import adds nothing beyond the modules that the default
``vllm.v1.worker.gpu_worker`` import already loads: no CUDA call, no thread.
The V1 runner module is checked only after upstream ``init_device`` loaded it.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

import torch
import vllm
from vllm.v1.outputs import AsyncModelRunnerOutput, ModelRunnerOutput
from vllm.v1.worker import gpu_worker, worker_base
from vllm.v1.worker.gpu_worker import Worker

from .core import (
    EXPECTED_VLLM_SOURCES,
    MAX_CENSUS_ENTRIES,
    MIN_SAMPLE_INTERVAL_NS,
    OwnerMemoryMonitor,
    OwnerMemoryPreconditionError,
    allocator_conf,
    file_sha256,
    monotonic_clock_name,
    process_identity,
    require_loaded_file,
    token,
    verify_versions,
    verify_vllm_sources,
)

# A child of vLLM's configured "vllm" logger, so records reach the engine log.
LOGGER = logging.getLogger("vllm.mineru_inference_memory")
_RUNNER_MODULE = "vllm.v1.worker.gpu_model_runner"
_RUNNER_SOURCE = "v1/worker/gpu_model_runner.py"
_PACKAGE_FILES = ("__init__.py", "core.py", "worker.py")


class _TorchCuda:
    """Read-only allocator and device queries; see ``core.CudaRuntime``."""

    def is_initialized(self) -> bool:
        return torch.cuda.is_initialized() is True

    def current_device(self) -> int:
        return torch.cuda.current_device()

    def memory_stats(self, device_index: int) -> Any:
        return torch.cuda.memory_stats(device_index)

    def allocator_backend(self) -> Any:
        return torch.cuda.get_allocator_backend()

    def mem_get_info(self, device_index: int) -> Any:
        return torch.cuda.mem_get_info(device_index)


def _vllm_root() -> Path:
    location = getattr(vllm, "__file__", None)
    if not isinstance(location, str):
        raise OwnerMemoryPreconditionError("vllm is not a regular installed package")
    return Path(location).resolve().parent


def _static_preconditions() -> dict[str, str]:
    """Versions, exact sources and class shape, checked before upstream __init__."""
    verify_versions(vllm_version=vllm.__version__, torch_version=torch.__version__)
    root = _vllm_root()
    sources = verify_vllm_sources(root)
    require_loaded_file(gpu_worker.__file__, root / "v1/worker/gpu_worker.py")
    require_loaded_file(worker_base.__file__, root / "v1/worker/worker_base.py")
    # --worker-extension-cls appends to __bases__ before the worker is built.
    if OwnerMemoryWorker.__bases__ != (Worker,) or Worker is not gpu_worker.Worker:
        raise OwnerMemoryPreconditionError("owner-memory worker bases were extended")
    return sources


def _runtime_preconditions(worker: Any) -> int:
    """The objects the hooks read exist exactly as reviewed, before model load."""
    root = _vllm_root()
    runner_module = sys.modules.get(_RUNNER_MODULE)
    if runner_module is None:
        raise OwnerMemoryPreconditionError("the V1 GPU model runner module is not loaded")
    require_loaded_file(getattr(runner_module, "__file__", None), root / _RUNNER_SOURCE)
    if file_sha256(root / _RUNNER_SOURCE) != EXPECTED_VLLM_SOURCES[_RUNNER_SOURCE]:
        raise OwnerMemoryPreconditionError("unreviewed vLLM sources: " + _RUNNER_SOURCE)
    runner = worker.model_runner
    if worker.use_v2_model_runner is not False or type(runner) is not getattr(
        runner_module, "GPUModelRunner", None
    ):
        raise OwnerMemoryPreconditionError("worker is not using the reviewed V1 GPU runner")
    if type(getattr(runner, "encoder_cache", None)) is not dict:
        raise OwnerMemoryPreconditionError("the V1 runner encoder_cache is not a dict")
    device = worker.device
    index = getattr(device, "index", None)
    if getattr(device, "type", None) != "cuda" or type(index) is not int or index < 0:
        raise OwnerMemoryPreconditionError("the worker device is not one CUDA device")
    if torch.cuda.is_initialized() is not True:
        raise OwnerMemoryPreconditionError("CUDA is not initialized after init_device")
    if torch.cuda.current_device() != index:
        raise OwnerMemoryPreconditionError("the current CUDA device is not the worker device")
    if not LOGGER.isEnabledFor(logging.INFO) or not LOGGER.hasHandlers():
        raise OwnerMemoryPreconditionError("owner-memory records would not reach the vLLM log")
    return index


def _startup_facts(worker: Any, vllm_sources: dict[str, str]) -> dict[str, Any]:
    package = Path(__file__).resolve().parent
    parallel, scheduler = worker.parallel_config, worker.scheduler_config
    backend = parallel.distributed_executor_backend
    runner_class = type(worker.model_runner)
    return {
        "versions": {
            "vllm": token(vllm.__version__),
            "torch": token(str(torch.__version__)),
            "python": token(platform.python_version()),
        },
        "sources": {
            "vllm": {name: "sha256:" + digest for name, digest in vllm_sources.items()},
            "diagnostic": {
                name: "sha256:" + file_sha256(package / name) for name in _PACKAGE_FILES
            },
        },
        "worker": {
            "class": f"{type(worker).__module__}.{type(worker).__qualname__}",
            "runner": f"{runner_class.__module__}.{runner_class.__qualname__}",
            "rank": worker.rank,
            "local_rank": worker.local_rank,
            "world_size": parallel.world_size,
            "executor_backend": token(backend) if isinstance(backend, str) else "custom",
            "async_scheduling": scheduler.async_scheduling,
        },
        "allocator": {
            "backend": token(torch.cuda.get_allocator_backend()),
            "conf": allocator_conf(os.environ),
        },
        "process": process_identity(),
        "clock": monotonic_clock_name(),
        "policy": {
            "min_sample_interval_ns": MIN_SAMPLE_INTERVAL_NS,
            "periodic_phase": "step_entry",
            "one_shot_phases": ["post_warmup", "first_use"],
            "census_entry_bound": MAX_CENSUS_ENTRIES,
        },
    }


class OwnerMemoryWorker(Worker):
    """vLLM 0.21.0 GPU Worker with read-only owner-thread memory records.

    Each override calls the upstream method once and returns its exact result.
    No upstream call is wrapped in ``try``, so upstream exceptions propagate
    unchanged; the monitor's serving-path hooks never raise.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if type(self) is not OwnerMemoryWorker:
            raise OwnerMemoryPreconditionError("unreviewed subclass of the owner-memory worker")
        sources = _static_preconditions()
        super().__init__(*args, **kwargs)
        self._mineru_owner_sources = sources
        self._mineru_owner_memory = OwnerMemoryMonitor(
            cuda=_TorchCuda(),
            tensor_type=torch.Tensor,
            output_types=(AsyncModelRunnerOutput, ModelRunnerOutput),
            logger=LOGGER,
        )

    def init_device(self) -> Any:
        result = super().init_device()
        device_index = _runtime_preconditions(self)
        self._mineru_owner_memory.bind_owner(
            runner=self.model_runner,
            device_index=device_index,
            startup=_startup_facts(self, self._mineru_owner_sources),
        )
        self._mineru_owner_memory.publish_owner_identity(
            module_path=Path(__file__).resolve(),
            worker_class=f"{type(self).__module__}.{type(self).__qualname__}",
        )
        return result

    def compile_or_warm_up_model(self) -> Any:
        result = super().compile_or_warm_up_model()
        self._mineru_owner_memory.after_warmup()
        return result

    def execute_model(self, scheduler_output: Any) -> Any:
        self._mineru_owner_memory.before_execute(scheduler_output)
        result = super().execute_model(scheduler_output)
        self._mineru_owner_memory.after_execute(result)
        return result

    def sample_tokens(self, grammar_output: Any) -> Any:
        result = super().sample_tokens(grammar_output)
        self._mineru_owner_memory.after_sample_tokens(result)
        return result
