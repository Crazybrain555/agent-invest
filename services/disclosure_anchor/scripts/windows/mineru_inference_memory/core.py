"""Owner-thread GPU memory observation for the MinerU vLLM inference worker.

Standard library only: importing this module never imports torch or vLLM,
creates a CUDA context or starts a thread. ``worker.py`` injects the CUDA
runtime, tensor type and vLLM output types, and calls the hooks only from the
engine thread that initialized the worker's device. Every value comes from
allocator bookkeeping or tensor metadata; nothing synchronizes the device,
collects garbage, releases cached blocks or changes allocator settings.
``OwnerMemoryMonitor.idle_evidence`` only reports the owner's own hook
timeline; the separately reviewed ``reclaim`` module decides and acts on it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeGuard

SCHEMA = "mineru.gpu-owner-memory.v1"
LOG_MARKER = "MINERU_GPU_OWNER_MEMORY"
MIN_SAMPLE_INTERVAL_NS = 30_000_000_000
MAX_CENSUS_ENTRIES = 65_536
MAX_RECORD_BYTES = 8_192
MAX_SOURCE_BYTES = 4 * 1024 * 1024
OWNER_IDENTITY_PATH = Path("/tmp/mineru-owner-memory/owner.json")
OWNER_IDENTITY_CONTRACT = "mineru.owner-memory-identity.v1"
OWNER_WORKER_CLASS = "mineru_inference_memory.worker.OwnerMemoryWorker"
# The reclaim mode's collective_rpc target on OwnerMemoryWorker.
RECLAIM_METHOD = "mineru_owner_memory_reclaim"
MAX_OWNER_IDENTITY_BYTES = 16 * 1024

EXPECTED_VLLM_VERSION = "0.21.0"
EXPECTED_TORCH_RELEASE = "2.11.0"
# Byte-exact vLLM v0.21.0 files that define the hook call order, the worker
# wrapper and the V1 runner's ``encoder_cache``. Any other bytes refuse startup.
EXPECTED_VLLM_SOURCES: dict[str, str] = {
    "v1/worker/worker_base.py": (
        "e6017acf80e442405f09f99413c0227515ae097d3e1dc79005b0b5c266932f12"
    ),
    "v1/worker/gpu_worker.py": (
        "53196cef1d32b5c5207c8986a5666352fd5ba0b79c418c074311147f266cadc4"
    ),
    "v1/worker/gpu_model_runner.py": (
        "95b7ee3ff5eecdb0fa791f375b2aeedc6ac507f3ea4a9683700d3b202b2caf08"
    ),
}

_POOLS = ("all", "large_pool", "small_pool")
ALLOCATOR_KEYS: tuple[str, ...] = (
    *(
        f"{stat}.{pool}.{metric}"
        for stat in (
            "allocated_bytes",
            "reserved_bytes",
            "active_bytes",
            "inactive_split_bytes",
        )
        for pool in _POOLS
        for metric in ("current", "peak")
    ),
    "requested_bytes.all.current",
    "requested_bytes.all.peak",
    *(f"segment.{pool}.current" for pool in _POOLS),
    "inactive_split.all.current",
    "num_alloc_retries",
    "num_ooms",
    "num_sync_all_streams",
    "num_device_alloc",
    "num_device_free",
)
REQUIRED_ALLOCATOR_KEYS = frozenset(
    {
        "allocated_bytes.all.current",
        "allocated_bytes.all.peak",
        "reserved_bytes.all.current",
        "reserved_bytes.all.peak",
        "active_bytes.all.current",
        "active_bytes.all.peak",
        "inactive_split_bytes.all.current",
        "inactive_split_bytes.all.peak",
        "num_alloc_retries",
        "num_ooms",
    }
)

_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+:-]{0,63}")
_ALLOC_CONF = re.compile(r"[0-9A-Za-z_:,.+-]{1,256}")
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_CLOCK = re.compile(r"[A-Za-z0-9_(). ]{1,64}")
# Per-entry metadata failures quarantine that entry; anything else fails the
# whole census section with its type recorded.
_ENTRY_ERRORS = (AttributeError, NotImplementedError, RuntimeError, TypeError, ValueError)

JsonDict = dict[str, Any]
# Pre-encoded so the last-resort line cannot fail on clocks or encoding.
_RECORD_FAILURE_LINE = json.dumps(
    {"schema": SCHEMA, "kind": "record_failure"}, sort_keys=True, separators=(",", ":")
)


class CudaRuntime(Protocol):
    """The only CUDA entry points the monitor may use."""

    def is_initialized(self) -> bool: ...

    def current_device(self) -> int: ...

    def memory_stats(self, device_index: int) -> Mapping[str, object]: ...

    def allocator_backend(self) -> str: ...

    def mem_get_info(self, device_index: int) -> tuple[int, int]: ...


class OwnerMemoryPreconditionError(RuntimeError):
    """The reviewed vLLM/torch surface is absent; the engine must not start."""


def token(value: object, pattern: re.Pattern[str] = _TOKEN) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value) is not None:
        return str(value)
    return None


def error_type(error: BaseException) -> str:
    return token(type(error).__qualname__, _TYPE_NAME) or "Exception"


def _count(value: object) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def file_sha256(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise OwnerMemoryPreconditionError(f"cannot read {path.name}") from error
    if len(payload) > MAX_SOURCE_BYTES:
        raise OwnerMemoryPreconditionError(f"{path.name} exceeds the source byte bound")
    return hashlib.sha256(payload).hexdigest()


def verify_versions(*, vllm_version: object, torch_version: object) -> None:
    mismatches = []
    if vllm_version != EXPECTED_VLLM_VERSION:
        mismatches.append(f"vllm={token(vllm_version)}")
    if (
        not isinstance(torch_version, str)
        or str(torch_version).split("+", 1)[0] != EXPECTED_TORCH_RELEASE
    ):
        mismatches.append(f"torch={token(torch_version)}")
    if mismatches:
        raise OwnerMemoryPreconditionError("unreviewed versions: " + ", ".join(mismatches))


def verify_vllm_sources(package_dir: Path) -> dict[str, str]:
    observed = {
        relative: file_sha256(package_dir / relative) for relative in EXPECTED_VLLM_SOURCES
    }
    mismatched = sorted(
        relative
        for relative, digest in observed.items()
        if digest != EXPECTED_VLLM_SOURCES[relative]
    )
    if mismatched:
        raise OwnerMemoryPreconditionError("unreviewed vLLM sources: " + ", ".join(mismatched))
    return observed


def require_loaded_file(module_file: object, expected: Path) -> None:
    """The hashed file must be the file the interpreter actually loaded."""
    if not isinstance(module_file, str) or Path(module_file).resolve() != expected.resolve():
        raise OwnerMemoryPreconditionError(f"loaded module is not the reviewed {expected.name}")


def verify_installed_distribution() -> dict[str, str]:
    """Image build check from package metadata and bytes; imports neither vLLM nor torch."""
    verify_versions(
        vllm_version=importlib.metadata.version("vllm"),
        torch_version=importlib.metadata.version("torch"),
    )
    spec = importlib.util.find_spec("vllm")
    locations = None if spec is None else spec.submodule_search_locations
    if locations is None or len(list(locations)) != 1:
        raise OwnerMemoryPreconditionError("vllm is not one installed regular package")
    return verify_vllm_sources(Path(list(locations)[0]))


def current_process_start_ticks() -> int:
    """Read this live Linux process birth; absent/invalid proc facts refuse startup."""
    try:
        with Path("/proc/self/stat").open("rb") as stream:
            raw = stream.read(8193)
        if not raw or len(raw) > 8192:
            raise ValueError("process stat byte bound exceeded")
        pid_text, remainder = raw.decode("latin-1").split(" (", 1)
        fields = remainder.rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        if int(pid_text) != os.getpid() or fields[0] in {"Z", "X"} or ticks <= 0:
            raise ValueError("process stat does not identify this live process")
        return ticks
    except (OSError, ValueError, IndexError) as error:
        raise OwnerMemoryPreconditionError("current owner process birth is unavailable") from error


def write_owner_identity(path: Path, identity: JsonDict) -> None:
    """Publish one bounded startup fact by atomic replacement, never follow a link."""
    fields = {"contract_version", "worker_class", "module_sha256", "pid", "proc_start_ticks",
              "device", "instance_id"}
    if (set(identity) != fields or identity["contract_version"] != OWNER_IDENTITY_CONTRACT
            or identity["worker_class"] != OWNER_WORKER_CLASS or identity["device"] != "cuda:0"
            or type(identity["pid"]) is not int or identity["pid"] <= 0
            or type(identity["proc_start_ticks"]) is not int or identity["proc_start_ticks"] <= 0
            or type(identity["module_sha256"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", identity["module_sha256"]) is None
            or type(identity["instance_id"]) is not str or not 1 <= len(identity["instance_id"]) <= 128):
        raise OwnerMemoryPreconditionError("owner identity values are invalid")
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not payload or len(payload) > MAX_OWNER_IDENTITY_BYTES:
        raise OwnerMemoryPreconditionError("owner identity exceeds its byte bound")
    if not path.is_absolute() or path.name != "owner.json":
        raise OwnerMemoryPreconditionError("owner identity path is invalid")
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".owner-" + uuid.uuid4().hex + ".tmp"
    try:
        directory = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or directory.st_mode & 0o022):
            raise OwnerMemoryPreconditionError("owner identity directory ownership/mode drifted")
        try:
            previous = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and (
            not stat.S_ISREG(previous.st_mode) or previous.st_uid != os.geteuid()
            or previous.st_mode & 0o022 or previous.st_nlink != 1
            or previous.st_size > MAX_OWNER_IDENTITY_BYTES
        ):
            raise OwnerMemoryPreconditionError("existing owner identity is not a bounded regular file")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def process_identity() -> JsonDict:
    """Linux process birth, so a reused PID in a fresh container stays distinct."""
    start_ticks: int | None = None
    boot_id: str | None = None
    try:
        stat = Path("/proc/self/stat").read_bytes().decode("latin-1")
        start_ticks = int(stat[stat.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        start_ticks = None
    try:
        raw_boot = Path("/proc/sys/kernel/random/boot_id").read_bytes().decode("latin-1")
        boot_id = str(uuid.UUID(raw_boot.strip()))
    except (OSError, ValueError):
        boot_id = None
    return {
        "start_ticks": start_ticks if start_ticks is not None and start_ticks > 0 else None,
        "boot_id": boot_id,
        "host_name": token(os.uname().nodename),
    }


def monotonic_clock_name() -> str | None:
    return token(time.get_clock_info("monotonic").implementation, _CLOCK)


def allocator_conf(environ: Mapping[str, str]) -> JsonDict:
    """Allocator settings as configured; null means unset, never a default."""
    conf: JsonDict = {}
    for name in ("PYTORCH_ALLOC_CONF", "PYTORCH_CUDA_ALLOC_CONF"):
        value = environ.get(name)
        if value is None or value == "":
            conf[name] = value
        else:
            conf[name] = token(value, _ALLOC_CONF) or "unrepresentable"
    return conf


def summarize_scheduler_output(output: object) -> JsonDict:
    """Counts only; request IDs and multimodal hashes never leave this function."""

    def size(name: str) -> int | None:
        value = getattr(output, name, None)
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            return len(value)
        return None

    tokens = getattr(output, "total_num_scheduled_tokens", None)
    return {
        "tokens": tokens if _count(tokens) else None,
        "requests": size("num_scheduled_tokens"),
        "encoder_requests": size("scheduled_encoder_inputs"),
        "finished_requests": size("finished_req_ids"),
        "free_encoder_entries": size("free_encoder_mm_hashes"),
    }


def _section(**fields: object) -> JsonDict:
    return {"state": None, "reason": None, "error_type": None, **fields}


_EMPTY_ALLOCATOR = _section(backend=None, stats=None, missing=None)
_EMPTY_DEVICE = _section(free_bytes=None, total_bytes=None)
_EMPTY_PROJECTION = _section(
    requested_entries=None,
    present_entries=None,
    present_logical_bytes=None,
    storages_losing_all_cache_refs=None,
    bytes_losing_all_cache_refs=None,
)
_EMPTY_CENSUS = _section(
    entries=None,
    owner_device_tensors=None,
    foreign_device_tensors=None,
    non_tensor_entries=None,
    uninspectable_entries=None,
    uninspectable_reasons=None,
    zero_byte_storage_entries=None,
    logical_bytes=None,
    storages=None,
    physical_bytes=None,
    largest_storage_bytes=None,
    shared_storages=None,
    max_entries_per_storage=None,
    overlapping_storages=None,
    scheduled_free=None,
)


def allocator_section(cuda: CudaRuntime, device_index: int) -> JsonDict:
    backend = token(cuda.allocator_backend())
    if backend != "native":
        # cudaMallocAsync reports several of these stats as constant zero.
        return {
            **_EMPTY_ALLOCATOR,
            "state": "unavailable",
            "reason": "unsupported_allocator_backend",
            "backend": backend,
        }
    raw = cuda.memory_stats(device_index)
    if not isinstance(raw, Mapping) or len(raw) == 0:
        return {
            **_EMPTY_ALLOCATOR,
            "state": "unavailable",
            "reason": "empty_allocator_stats",
            "backend": backend,
        }
    stats: dict[str, int | None] = {}
    missing: list[str] = []
    for key in ALLOCATOR_KEYS:
        value = raw.get(key)
        if _count(value):
            stats[key] = value
        else:
            stats[key] = None
            missing.append(key)
    complete = REQUIRED_ALLOCATOR_KEYS.isdisjoint(missing)
    return {
        **_EMPTY_ALLOCATOR,
        "state": "available" if complete else "incomplete",
        "reason": None if complete else "required_metric_missing",
        "backend": backend,
        "stats": stats,
        "missing": missing,
    }


def device_memory_section(cuda: CudaRuntime, device_index: int) -> JsonDict:
    """Device-wide cudaMemGetInfo: includes every process on the GPU."""
    info = cuda.mem_get_info(device_index)
    if (
        isinstance(info, tuple)
        and len(info) == 2
        and _count(info[0])
        and _count(info[1])
        and info[0] <= info[1]
    ):
        return {
            **_EMPTY_DEVICE,
            "state": "available",
            "free_bytes": info[0],
            "total_bytes": info[1],
        }
    return {**_EMPTY_DEVICE, "state": "unavailable", "reason": "invalid_mem_get_info"}


def _union(ranges: list[tuple[int, int]]) -> tuple[int, int]:
    """Covered bytes of sorted half-open ranges, and how many start inside another."""
    total = overlapping = 0
    current: tuple[int, int] | None = None
    for low, high in ranges:
        if current is None or low >= current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = (low, high)
        else:
            overlapping += 1
            current = (current[0], max(current[1], high))
    if current is not None:
        total += current[1] - current[0]
    return total, overlapping


def _note(reasons: list[str], reason: str) -> None:
    if reason not in reasons and len(reasons) < 4:
        reasons.append(reason)


def encoder_cache_census(
    cache: object,
    *,
    tensor_type: type[Any],
    device_index: int,
    project_free: bool = False,
    scheduled_free: object = None,
    max_entries: int = MAX_CENSUS_ENTRIES,
) -> JsonDict:
    """Logical view bytes versus deduplicated backing storage, from metadata only.

    A split view keeps its whole parent allocation alive, so each distinct
    storage is counted once by its device address range. Addresses and cache
    keys never leave this function. ``scheduled_free`` is this step's
    ``free_encoder_mm_hashes``, which the runner applies only after the hook.
    """
    if type(cache) is not dict:
        return {**_EMPTY_CENSUS, "state": "unavailable", "reason": "encoder_cache_not_dict"}
    if len(cache) > max_entries:
        return {
            **_EMPTY_CENSUS,
            "state": "unavailable",
            "reason": "entry_bound_exceeded",
            "entries": len(cache),
        }
    free_keys: frozenset[str] | None = None
    projection: JsonDict | None = None
    if project_free:
        if (
            isinstance(scheduled_free, (list, tuple))
            and len(scheduled_free) <= max_entries
            and all(type(key) is str for key in scheduled_free)
        ):
            free_keys = frozenset(scheduled_free)
            projection = {**_EMPTY_PROJECTION, "requested_entries": len(scheduled_free)}
        else:
            projection = {
                **_EMPTY_PROJECTION,
                "state": "unavailable",
                "reason": "scheduled_free_unreadable",
            }

    owner_tensors = foreign = non_tensors = uninspectable = zero_byte = 0
    logical_total = present_logical = 0
    reasons: list[str] = []
    # (address, bytes) -> [cache references, references kept after this step's free]
    storages: dict[tuple[int, int], list[int]] = {}
    for key, value in cache.items():
        if not isinstance(value, tensor_type):
            non_tensors += 1
            continue
        try:
            if value.is_cuda is not True or value.get_device() != device_index:
                foreign += 1
                continue
            logical = value.nbytes
            storage = value.untyped_storage()
            address = storage.data_ptr()
            size = storage.nbytes()
        except _ENTRY_ERRORS as error:
            uninspectable += 1
            _note(reasons, error_type(error))
            continue
        if not (_count(logical) and _count(address) and _count(size)):
            uninspectable += 1
            _note(reasons, "invalid_metadata")
            continue
        owner_tensors += 1
        logical_total += logical
        freed = free_keys is not None and key in free_keys
        if freed:
            present_logical += logical
        if size == 0:
            zero_byte += 1
            continue
        slot = storages.setdefault((address, size), [0, 0])
        slot[0] += 1
        if not freed:
            slot[1] += 1

    physical, overlapping = _union(sorted((low, low + size) for low, size in storages))
    references = [slot[0] for slot in storages.values()]
    if projection is not None and free_keys is not None:
        kept, _ = _union(
            sorted((low, low + size) for (low, size), slot in storages.items() if slot[1] > 0)
        )
        projection.update(
            state="available" if uninspectable == 0 else "incomplete",
            reason=None if uninspectable == 0 else "uninspectable_entries",
            present_entries=sum(1 for key in free_keys if key in cache),
            present_logical_bytes=present_logical,
            storages_losing_all_cache_refs=sum(1 for slot in storages.values() if slot[1] == 0),
            bytes_losing_all_cache_refs=physical - kept,
        )
    return {
        **_EMPTY_CENSUS,
        "state": "available" if uninspectable == 0 else "incomplete",
        "reason": None if uninspectable == 0 else "uninspectable_entries",
        "entries": len(cache),
        "owner_device_tensors": owner_tensors,
        "foreign_device_tensors": foreign,
        "non_tensor_entries": non_tensors,
        "uninspectable_entries": uninspectable,
        "uninspectable_reasons": reasons,
        "zero_byte_storage_entries": zero_byte,
        "logical_bytes": logical_total,
        "storages": len(storages),
        "physical_bytes": physical,
        "largest_storage_bytes": max((size for _, size in storages), default=None),
        "shared_storages": sum(1 for count in references if count > 1),
        "max_entries_per_storage": max(references, default=None),
        "overlapping_storages": overlapping,
        "scheduled_free": projection,
    }


class _Activity:
    """Owner steps since the last sample attempt; null once any step was unreadable."""

    def __init__(self) -> None:
        self.steps = 0
        self.token_steps: int | None = 0
        self.encoder_steps: int | None = 0
        self.scheduled_free_entries: int | None = 0

    def add(self, summary: JsonDict) -> None:
        self.steps += 1
        tokens, encoders, frees = (
            summary["tokens"],
            summary["encoder_requests"],
            summary["free_encoder_entries"],
        )
        self.token_steps = (
            None if self.token_steps is None or tokens is None else self.token_steps + (tokens > 0)
        )
        self.encoder_steps = (
            None
            if self.encoder_steps is None or encoders is None
            else self.encoder_steps + (encoders > 0)
        )
        self.scheduled_free_entries = (
            None
            if self.scheduled_free_entries is None or frees is None
            else self.scheduled_free_entries + frees
        )

    def as_json(self) -> JsonDict:
        return {
            "steps": self.steps,
            "token_steps": self.token_steps,
            "encoder_steps": self.encoder_steps,
            "scheduled_free_entries": self.scheduled_free_entries,
        }


@dataclass(frozen=True)
class _Owner:
    pid: int
    thread: int
    device_index: int


@dataclass(frozen=True)
class IdleEvidence:
    """The owner's hook timeline at one instant; never a proof of global idle.

    ``last_step`` is ``None`` when no step ran since warmup, ``cleanup`` when the
    last step scheduled no tokens and returned a final output, else ``work``.
    """

    owner_call: bool
    warm: bool
    instance: str
    pid: int | None
    device_index: int | None
    quiet_ns: int | None
    last_return_ns: int | None
    last_step: str | None


class OwnerMemoryMonitor:
    """Worker hooks. Startup binding may raise; serving-path hooks never do.

    Records go to the supplied logger as one ``LOG_MARKER`` JSON line each:
    one startup record, a one-shot ``post_warmup`` baseline, a one-shot
    ``first_use`` sample, and ``step_entry`` samples at least
    ``MIN_SAMPLE_INTERVAL_NS`` apart. Error records are one-shot or share that
    interval, so no code path logs per step.
    """

    def __init__(
        self,
        *,
        cuda: CudaRuntime,
        tensor_type: type[Any],
        output_types: tuple[type[Any], type[Any]],
        logger: logging.Logger,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wall_ns: Callable[[], int] = time.time_ns,
        thread_ident: Callable[[], int] = threading.get_ident,
        getpid: Callable[[], int] = os.getpid,
    ) -> None:
        self._cuda = cuda
        self._tensor_type = tensor_type
        self._async_type, self._final_type = output_types
        self._logger = logger
        self._clock_ns = clock_ns
        self._wall_ns = wall_ns
        self._thread_ident = thread_ident
        self._getpid = getpid
        self._instance = uuid.uuid4().hex
        self._seq = 0
        self._owner: _Owner | None = None
        self._runner: object = None
        self._warm = False
        self._first_use = "waiting"
        self._last_record_ns: int | None = None
        self._last_return_ns: int | None = None
        self._previous_output: str | None = None
        self._step_tokens: int | None = None
        self._last_step: str | None = None
        self._activity = _Activity()
        self._foreign_calls = 0
        self._foreign_reported = False
        self._clock_regressions = 0
        self._internal_failures = 0
        self._last_internal_report_ns: int | None = None
        self._clockless_reported = False
        self._fallback_reported = False

    def bind_owner(self, *, runner: object, device_index: int, startup: JsonDict) -> None:
        """Called once, on the thread that just initialized the worker's device."""
        if self._owner is not None:
            raise OwnerMemoryPreconditionError("owner-memory monitor is already bound")
        if not _count(device_index):
            raise OwnerMemoryPreconditionError("owner device index is invalid")
        self._owner = _Owner(self._getpid(), self._thread_ident(), device_index)
        self._runner = runner
        self._emit("startup", startup, self._clock_ns(), None, logging.INFO)

    def publish_owner_identity(
        self, *, module_path: Path, worker_class: str, path: Path | None = None,
        read_start_ticks: Callable[[], int] | None = None,
    ) -> JsonDict:
        """Once during startup, on the initialized owner; never on a serving hook."""
        owner = self._owner
        if (owner is None or owner.pid != self._getpid() or owner.thread != self._thread_ident()
                or owner.device_index != 0 or worker_class != OWNER_WORKER_CLASS):
            raise OwnerMemoryPreconditionError("identity publication requires the initialized CUDA:0 owner")
        ticks = (current_process_start_ticks if read_start_ticks is None else read_start_ticks)()
        if type(ticks) is not int or ticks <= 0:
            raise OwnerMemoryPreconditionError("current owner process birth is unavailable")
        identity = {
            "contract_version": OWNER_IDENTITY_CONTRACT, "worker_class": worker_class,
            "module_sha256": "sha256:" + file_sha256(module_path), "pid": owner.pid,
            "proc_start_ticks": ticks, "device": "cuda:0", "instance_id": self._instance,
        }
        write_owner_identity(OWNER_IDENTITY_PATH if path is None else path, identity)
        return identity

    def idle_evidence(self) -> IdleEvidence:
        """Read-only view for an owner-thread caller; no CUDA call and no record."""
        owner = self._owner
        owner_call = (owner is not None and self._getpid() == owner.pid
                      and self._thread_ident() == owner.thread)
        last = self._last_return_ns
        now = self._clock_ns()
        return IdleEvidence(
            owner_call=owner_call,
            warm=self._warm,
            instance=self._instance,
            pid=None if owner is None else owner.pid,
            device_index=None if owner is None else owner.device_index,
            quiet_ns=None if last is None or now < last else now - last,
            last_return_ns=last,
            last_step=self._last_step,
        )

    def after_warmup(self) -> None:
        self._guard("post_warmup", self._after_warmup)

    def before_execute(self, scheduler_output: object) -> None:
        self._guard("step_entry", lambda: self._before_execute(scheduler_output))

    def after_execute(self, result: object) -> None:
        self._guard("step_return", lambda: self._after_return(result, sampled=False))

    def after_sample_tokens(self, result: object) -> None:
        self._guard("step_return", lambda: self._after_return(result, sampled=True))

    def _after_warmup(self) -> None:
        owner = self._owner_call("post_warmup")
        if owner is None:
            return
        now = self._clock_ns()
        self._warm = True
        self._last_return_ns = now
        self._sample(owner, "post_warmup", now, {})

    def _before_execute(self, scheduler_output: object) -> None:
        owner = self._owner_call("step_entry") if self._warm else None
        if owner is None:
            return
        # A step is in progress until its return is classified.
        self._last_step = "work"
        self._step_tokens = None
        now = self._clock_ns()
        summary = summarize_scheduler_output(scheduler_output)
        self._step_tokens = summary["tokens"]
        if self._periodic_due(now):
            quiet = None if self._last_return_ns is None else now - self._last_return_ns
            context = {
                "quiet_ns": quiet,
                "previous_output": self._previous_output,
                "scheduled": summary,
            }
            free = getattr(scheduler_output, "free_encoder_mm_hashes", None)
            self._sample(owner, "step_entry", now, context, project_free=True, scheduled_free=free)
        self._activity.add(summary)
        tokens = summary["tokens"]
        if self._first_use == "waiting" and tokens is not None and tokens > 0:
            self._first_use = "pending"

    def _after_return(self, result: object, *, sampled: bool) -> None:
        owner = self._owner_call("step_return") if self._warm else None
        if owner is None:
            return
        self._last_step = "work"
        now = self._clock_ns()
        self._last_return_ns = now
        if result is None and not sampled:
            return  # execute_model deferred this step's output to sample_tokens.
        kind = self._output_kind(result)
        self._previous_output = kind
        # The scheduler reports finished requests in one zero-token schedule
        # whose execute_model returns a final output; any other return is work.
        if not sampled and kind == "final" and self._step_tokens == 0:
            self._last_step = "cleanup"
        if self._first_use == "pending":
            self._first_use = "done"
            self._sample(owner, "first_use", now, {"output": kind})

    def _output_kind(self, result: object) -> str:
        if result is None:
            return "none"
        if isinstance(result, self._async_type):
            return "async"
        if isinstance(result, self._final_type):
            return "final"
        return "other"

    def _owner_call(self, phase: str) -> _Owner | None:
        owner = self._owner
        if owner is None:
            return None
        pid, thread = self._getpid(), self._thread_ident()
        if pid == owner.pid and thread == owner.thread:
            return owner
        self._foreign_calls += 1
        if not self._foreign_reported:
            self._foreign_reported = True
            stage = "owner_pid" if pid != owner.pid else "owner_thread"
            self._error(phase, stage, None, self._clock_ns())
        return None

    def _periodic_due(self, now: int) -> bool:
        last = self._last_record_ns
        if last is None:
            return True
        if now < last:
            self._clock_regressions += 1
            self._last_record_ns = now
            if self._clock_regressions == 1:
                self._error("step_entry", "clock_regression", None, now)
            return False
        return now - last >= MIN_SAMPLE_INTERVAL_NS

    def _sample(
        self,
        owner: _Owner,
        phase: str,
        now: int,
        context: JsonDict,
        *,
        project_free: bool = False,
        scheduled_free: object = None,
    ) -> None:
        self._last_record_ns = now
        context = {**context, "since_previous_attempt": self._activity.as_json()}
        self._activity = _Activity()
        cuda, device_index = self._cuda, owner.device_index
        stage: str | None = None
        failure: str | None = None
        try:
            # current_device() would create a context if CUDA were uninitialized.
            if not cuda.is_initialized():
                stage = "cuda_not_initialized"
            elif cuda.current_device() != device_index:
                stage = "device_mismatch"
        except Exception as error:
            stage, failure = "device_check", error_type(error)
        if stage is not None:
            self._error(phase, stage, failure, now)
            return
        allocator = self._guarded_section(
            _EMPTY_ALLOCATOR, lambda: allocator_section(cuda, device_index)
        )
        device_memory = self._guarded_section(
            _EMPTY_DEVICE, lambda: device_memory_section(cuda, device_index)
        )
        census = self._guarded_section(
            _EMPTY_CENSUS,
            lambda: encoder_cache_census(
                getattr(self._runner, "encoder_cache", None),
                tensor_type=self._tensor_type,
                device_index=device_index,
                project_free=project_free,
                scheduled_free=scheduled_free,
            ),
        )
        body = {
            "phase": phase,
            "device_work": "not_synchronized",
            "context": context,
            "allocator": allocator,
            "device_memory": device_memory,
            "encoder_cache": census,
        }
        self._emit("sample", body, now, self._clock_ns(), logging.INFO)

    @staticmethod
    def _guarded_section(empty: JsonDict, build: Callable[[], JsonDict]) -> JsonDict:
        try:
            return build()
        except Exception as error:
            return {**empty, "state": "error", "error_type": error_type(error)}

    def _error(self, phase: str, stage: str, failure: str | None, now: int | None) -> None:
        body = {"phase": phase, "stage": stage, "error_type": failure}
        self._emit("error", body, now, None, logging.WARNING)

    def _guard(self, phase: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:
            try:
                self._internal_failure(phase, error)
            except Exception:
                # Even the fallback line failed: the log sink is unusable. Serving
                # continues and later records carry the internal_failures counter.
                return

    def _internal_failure(self, phase: str, error: Exception) -> None:
        self._internal_failures += 1
        try:
            now: int | None = self._clock_ns()
        except Exception:
            now = None
        if now is None:
            if self._clockless_reported:
                return
            self._clockless_reported = True
        else:
            last = self._last_internal_report_ns
            if last is not None and 0 <= now - last < MIN_SAMPLE_INTERVAL_NS:
                return
            self._last_internal_report_ns = now
        try:
            self._error(phase, "internal", error_type(error), now)
        except Exception:
            if not self._fallback_reported:
                self._fallback_reported = True
                self._logger.warning("%s %s", LOG_MARKER, _RECORD_FAILURE_LINE)

    def _emit(
        self,
        kind: str,
        body: Mapping[str, object],
        now: int | None,
        completed: int | None,
        level: int,
    ) -> None:
        owner = self._owner
        envelope = {
            "schema": SCHEMA,
            "kind": kind,
            "seq": self._seq,
            "instance": self._instance,
            "pid": None if owner is None else owner.pid,
            "device_index": None if owner is None else owner.device_index,
            "observed_at": {
                "monotonic_ns": now,
                "completed_monotonic_ns": now if completed is None else completed,
                "emitted_unix_ns": self._wall_ns(),
            },
            "counters": {
                "foreign_calls": self._foreign_calls,
                "internal_failures": self._internal_failures,
                "clock_regressions": self._clock_regressions,
            },
        }
        payload = json.dumps(
            {**body, **envelope}, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("owner-memory record exceeds its byte bound")
        self._seq += 1
        self._logger.log(level, "%s %s", LOG_MARKER, payload)
