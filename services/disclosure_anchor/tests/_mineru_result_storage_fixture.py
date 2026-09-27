"""Capacity-config-v2 fixture: the actual generated API manager in storage mode.

The shipped capacity codec, file reader, bootstrap and observation modules load
under their installed names from a real v2 capacity file sized to the scratch
volume. The generated manager, ingress, routes, ZIP helpers and owned-thread
helpers execute; the parser boundary writes synthetic files through the actual
generated granted DataWriter. Nothing parses a PDF or reaches a network.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import starlette.formparsers
from starlette.responses import JSONResponse

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import patch_source
from tests._mineru_capacity_lifecycle_fixture import (
    MODEL_PREIMAGE_SHA256,
    PREIMAGE_SHA256,
    BoundaryHTTPException,
    Upload,
)

MIB = 1024 * 1024
COMMON_PREIMAGE_SHA256 = "d1e23e310bddc3da2d7f491be81ef112435824403d1c3a29e438505c1707dbc5"
STORAGE_NAMES = (
    "STORAGE_BLOCKED_REASONS", "ResultSelection", "StorageHoldOperatorRefused", "TaskStorageBlocked",
    "TaskStorageWait", "bind_source_growth_permit", "build_result_inventory", "live_free_bytes",
    "load_inventory_file", "require_storage_hold_operator", "require_storage_managed_output",
    "storage_status_payload", "verify_result_inventory", "write_inventory_file", "write_retained_zip",
)


class UpstreamFileBasedDataWriter:
    """MinerU 3.4.4 ``FileBasedDataWriter.write`` and ``DataWriter.write_string``."""

    def __init__(self, parent_dir: str = "") -> None:
        self._parent_dir = parent_dir

    def write(self, path: str, data: bytes) -> None:
        fn_path = path
        if not os.path.isabs(fn_path) and len(self._parent_dir) > 0:
            fn_path = os.path.join(self._parent_dir, path)
        if not os.path.exists(os.path.dirname(fn_path)) and os.path.dirname(fn_path) != "":
            os.makedirs(os.path.dirname(fn_path), exist_ok=True)
        with open(fn_path, "wb") as f:
            f.write(data)

    def write_string(self, path: str, data: str) -> None:
        self.write(path, data.encode("utf-8", errors="replace"))


def generated_granted_writer() -> type:
    """The actual generated ``common.FileBasedDataWriter`` over the upstream base."""
    service = Path(protocol.__file__).resolve().parents[3]
    raw = (service / "tests/fixtures/mineru_344_preimages/mineru/cli/common.py").read_bytes()
    if hashlib.sha256(raw).hexdigest() != COMMON_PREIMAGE_SHA256:
        raise AssertionError("official 3.4.4 common preimage changed")
    generated = patch_source("mineru/cli/common.py", raw.decode())
    nodes = [node for node in ast.parse(generated).body
             if isinstance(node, ast.ClassDef) and node.name == "FileBasedDataWriter"]
    if len(nodes) != 1:
        raise AssertionError("generated granted writer extraction drift")
    namespace: dict[str, object] = {
        "_UngrantedFileBasedDataWriter": UpstreamFileBasedDataWriter,
        "current_source_growth_permit": protocol.current_source_growth_permit,
        "storage_managed_output_required": protocol.storage_managed_output_required,
        "os": os,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<actual-generated-common-writer>", "exec"), namespace)
    return namespace["FileBasedDataWriter"]  # type: ignore[return-value]


class HoldRouteRequest:
    """The request surface the generated hold route uses: headers and a chunked body.

    ``read`` counts the body bytes the route actually pulled, so a test can
    prove a refusal came before the body.
    """

    def __init__(self, body: bytes = b"", *, headers: dict[str, str] | None = None,
                 chunk: int = 4096) -> None:
        self.headers = {name.lower(): value for name, value in (headers or {}).items()}
        self._body = body
        self._chunk = chunk
        self.read = 0

    @classmethod
    def json(cls, value: object, *, credential: str | None = None, **options: Any) -> HoldRouteRequest:
        headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
        return cls(json.dumps(value).encode(), headers=headers, **options)

    async def stream(self):  # type: ignore[no-untyped-def]
        for index in range(0, len(self._body), self._chunk):
            chunk = self._body[index:index + self._chunk]
            self.read += len(chunk)
            yield chunk
        yield b""


class ResultStorageApiFixture:
    """Manager, ingress and routes of the generated API under capacity config v2."""

    def __init__(self, root: Path, *, outputs: dict[str, bytes] | None = None,
                 single_limit: int = 8 * MIB) -> None:
        self._closed = False
        self._prior_modules: dict[str, types.ModuleType] | None = None
        self._latch_patch = None
        self._spool_patch = None
        self._operator_patch = None
        self.module: types.ModuleType | None = None
        self.proc_patch = None
        self.environment = None
        self.outputs = outputs if outputs is not None else {
            "{name}.md": b"# synthetic\n" * 32,
            "{name}_content_list.json": b"[]",
            "images/a.png": bytes(range(256)) * 32,
        }
        try:
            self._initialize(root, single_limit=single_limit)
        except BaseException:
            self.close()
            raise

    def _load_shipped_modules(self, service: Path) -> None:
        self._prior_modules = {name: value for name, value in sys.modules.items()
                               if name == "mineru" or name.startswith("mineru.")
                               or name == "mineru_vl_utils" or name.startswith("mineru_vl_utils.")}
        for name in self._prior_modules:
            del sys.modules[name]
        for name in ("mineru", "mineru.cli", "mineru_vl_utils", "mineru_vl_utils.vlm_client"):
            package = types.ModuleType(name)
            package.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = package
        originals = {
            "agent_capacity_config": service / "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
            "agent_capacity_file": service / "src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py",
            "agent_capacity_bootstrap": service / "scripts/windows/mineru_heap_trim_compat/agent_capacity_bootstrap.py",
            "agent_capacity_observation": service / "scripts/windows/mineru_heap_trim_compat/agent_capacity_observation.py",
        }
        for name, path in originals.items():
            fullname = "mineru.cli." + name
            spec = importlib.util.spec_from_file_location(fullname, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            spec.loader.exec_module(module)
        sys.modules["mineru.cli.agent_task_protocol_v2"] = protocol

    def _write_capacity(self, root: Path, *, single_limit: int) -> dict[str, str]:
        codec = sys.modules["mineru.cli.agent_capacity_config"]
        usage = os.statvfs(root)
        policy = codec.MineruResultStoragePolicy(
            contract_version="mineru.result-storage-policy.v1",
            native_volume_identity="scratch-native-volume",
            native_volume_total_bytes=usage.f_blocks * usage.f_frsize,
            native_work_disk_limit_bytes=64 * MIB, native_free_floor_bytes=MIB,
            native_source_pool_bytes=32 * MIB, native_completion_escrow_bytes=16 * MIB,
            native_metadata_reserve_bytes=MIB, native_source_single_limit_bytes=single_limit,
            native_growing_producer_limit=1, native_result_hard_limit_bytes=12 * MIB,
            native_normal_unacked_target_bytes=8 * MIB, initial_result_estimate_bytes=2 * MIB,
            native_allocation_unit_bytes=4096, native_file_overhead_bytes=4096,
            source_pdf_bytes_limit=MIB,
            mac_volume_identity="scratch-mac-volume", mac_volume_total_bytes=100 * 1024 * MIB,
            mac_work_disk_limit_bytes=64 * MIB, mac_free_floor_bytes=1024 * MIB,
            mac_normal_output_target_bytes=8 * MIB, mac_decode_input_limit_bytes=MIB,
            mac_decode_working_set_budget_bytes=8 * MIB, mac_decode_expansion_factor=4,
            mac_decode_stage_seconds=300, max_members=64, max_name_bytes=128,
            max_inventory_bytes=64 * 1024, transfer_logical_deadline_seconds=600,
            progress_window_seconds=10, minimum_progress_bytes=MIB,
        )
        config = codec.MineruCapacityConfigV2(
            contract_version="mineru.capacity-config.v2", parse_active_limit=2,
            total_nonterminal_limit=3, finalizer_active_limit=1, final_http_limit_per_loop=7,
            processing_window_size=8, pdf_render_processes_requested=3,
            hybrid_batch_ratio_requested=2, omp_num_threads=4, mkl_num_threads=2,
            openblas_num_threads=1, pipeline_inference_locks=True, api_process_limit=1,
            api_event_loop_limit=1, result_storage=policy,
        )
        self.policy = policy
        self.config_path = root / "capacity.json"
        self.config_path.write_bytes(config.exact_bytes)
        self.config_path.chmod(0o600)
        # Literal startup values; B/L variables are deliberately absent.
        return {
            "MINERU_CAPACITY_CONFIG_PATH": str(self.config_path),
            "MINERU_CAPACITY_CONFIG_SHA256": config.sha256,
            "MINERU_API_OUTPUT_ROOT": str(root / "output"), "MINERU_API_TASK_RETENTION_SECONDS": "0",
            "MINERU_API_MAX_CONCURRENT_REQUESTS": "2", "MINERU_API_MAX_PENDING_TASKS": "3",
            "MINERU_API_FINALIZER_SLOTS": "1", "MINERU_PROCESSING_WINDOW_SIZE": "8",
            "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1",
            "MINERU_PDF_RENDER_THREADS": "3", "MINERU_HYBRID_BATCH_RATIO": "2",
            "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS": "1",
        }

    def _load_generated_http(self, service: Path) -> None:
        http_raw = (service / "tests/fixtures/mineru_344_preimages/mineru_vl_utils/vlm_client/http_client.py").read_text()
        generated_http = patch_source("mineru_vl_utils/vlm_client/http_client.py", http_raw)
        http_names = {"_ProcessAsyncRequestLimiter", "_bind_capacity_owner", "_apply_capacity_soft_drain",
                      "_capacity_http_snapshot", "_process_async_request_snapshot", "_process_async_request_limiter"}
        nodes: list[ast.stmt] = []
        for node in ast.parse(generated_http).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in http_names:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and (t.id.startswith("_PROCESS_ASYNC_REQUEST_") or t.id == "_CAPACITY_OWNER")
                for t in node.targets
            ):
                nodes.append(node)
        if {getattr(n, "name", None) for n in nodes} - {None} != http_names:
            raise AssertionError("actual HTTP owner extraction drift")
        http = types.ModuleType("mineru_vl_utils.vlm_client.http_client")
        http.__dict__.update(asyncio=asyncio, _agent_request_os=os, _agent_request_threading=threading)
        sys.modules[http.__name__] = http
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<actual-generated-HTTP-owner>", "exec"), http.__dict__)

    def _initialize(self, root: Path, *, single_limit: int) -> None:
        # The shipped capacity reader never follows a symlinked path component.
        root = Path(root).resolve()
        self.root = root
        self.epoch = int(time.time())
        service = Path(protocol.__file__).resolve().parents[3]
        self._load_shipped_modules(service)
        (root / "output").mkdir()
        environment = self._write_capacity(root, single_limit=single_limit)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        # The permit-only latch is process global; restore it at close.
        self._latch_patch = patch.object(protocol, "_STORAGE_MANAGED_OUTPUT", False)
        self._latch_patch.start()
        # Storage binding places the framework's multipart spool on the output
        # volume; that process-global placement is undone at close.
        self._spool_patch = patch.object(
            starlette.formparsers, "SpooledTemporaryFile", tempfile.SpooledTemporaryFile,
        )
        self._spool_patch.start()
        self._load_generated_http(service)
        original_read_text = Path.read_text

        def proc_read(path, *args, **kwargs):
            if str(path) == "/proc/self/stat":
                return "271 (synthetic process name) S " + " ".join(["0"] * 18 + ["27182"])
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return "7b860196-6c69-44e5-a222-47a775ec8ceb\n"
            return original_read_text(path, *args, **kwargs)

        self.proc_patch = patch.object(Path, "read_text", proc_read)
        self.proc_patch.start()
        raw = (service / "tests/fixtures/mineru_344_preimages/mineru/cli/fast_api.py").read_bytes()
        if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
            raise AssertionError("official 3.4.4 FastAPI preimage changed")
        self.generated = patch_source("mineru/cli/fast_api.py", raw.decode())
        selected = {
            "StoredUpload", "AsyncParseTask", "AsyncTaskManager", "TaskWaitAbortedError",
            "_settle_service_operation", "_registry_view", "utc_now_iso", "get_int_env",
            "get_max_concurrent_requests", "get_max_pending_tasks", "get_task_retention_seconds",
            "get_task_cleanup_interval_seconds", "get_output_root", "cleanup_file",
            "build_upload_destination", "is_task_terminal", "_write_upload_chunk",
            "_prepare_ingress_tree", "save_upload_files", "create_task_output_dir",
            "create_async_parse_task", "ack_async_task_result", "health_check",
            "_request_resources", "_ServiceRequestMiddleware",
            "_hash_file", "_retained_result_sources", "_verify_and_close_result_sources",
            "_write_retained_zip_from_fds", "_commit_retained_result", "_build_retained_artifact_owned",
            "build_retained_task_result", "get_parse_dir", "get_images_dir_image_paths", "build_zip_arcname",
        }
        constants = {
            "TASK_PENDING", "TASK_PROCESSING", "TASK_COMPLETED", "TASK_FAILED", "TASK_TERMINAL_STATES",
            "DEFAULT_TASK_RETENTION_SECONDS", "DEFAULT_TASK_CLEANUP_INTERVAL_SECONDS",
            "DEFAULT_OUTPUT_ROOT", "_configured_max_concurrent_requests",
        }
        body: list[ast.stmt] = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        found = set()
        for node in ast.parse(self.generated).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected:
                if node.name in {"ack_async_task_result", "health_check"}:
                    node.decorator_list = []
                body.append(node)
                found.add(node.name)
            elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
                body.append(node)
            elif isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == "_request_resource_context" for target in node.targets
            ):
                body.append(node)
        if found != selected:
            raise AssertionError(f"generated API extraction drift: {selected - found}")
        module = types.ModuleType(f"_storage_generated_{uuid.uuid4().hex}")
        sys.modules[module.__name__] = module
        self.module = module
        namespace = module.__dict__
        namespace.update({
            "asyncio": asyncio, "os": os, "shutil": shutil, "stat": stat, "hashlib": hashlib,
            "tempfile": tempfile, "ContextVar": contextvars.ContextVar, "JSONResponse": JSONResponse,
            "resolve_parse_dir": lambda output, name, backend, method, **kw: Path(output) / name / method,
            "RESULT_IMAGE_SUFFIXES": {"png", "jpg", "jpeg"},
            "uuid": uuid, "Path": Path, "datetime": datetime, "timezone": timezone,
            "dataclass": dataclass, "asdict": asdict, "suppress": suppress,
            "HTTPException": BoundaryHTTPException,
            "logger": logging.getLogger("result-storage-fixture"),
            "SUPPORTED_UPLOAD_SUFFIXES": ["pdf"],
            "normalize_upload_filename": lambda name: name,
            "normalize_task_stem": lambda name: name,
            "uniquify_task_stems": lambda names: (names, False),
            "guess_suffix_by_path": lambda path: Path(path).suffix[1:],
            "DurableTaskRegistry": protocol.DurableTaskRegistry,
            "SplitTaskExecutor": protocol.SplitTaskExecutor,
            "TaskProtocolConflict": protocol.TaskProtocolConflict,
            "TaskRegistryPersistenceError": protocol.TaskRegistryPersistenceError,
            "TaskRegistryObservationBusy": protocol.TaskRegistryObservationBusy,
            "RegistryServiceIO": protocol.RegistryServiceIO,
            "ServingLoopProbe": protocol.ServingLoopProbe,
            "anyio": anyio,
            "TaskAdmissionFull": protocol.TaskAdmissionFull,
            "TaskResultCapacityRecoveryRequired": protocol.TaskResultCapacityRecoveryRequired,
            "TaskExecutionStopped": protocol.TaskExecutionStopped,
            "evict_consumed_routes": protocol.evict_consumed_routes,
            "task_protocol_runtime_status": protocol.task_protocol_runtime_status,
            "__version__": "3.4.4", "API_PROTOCOL_VERSION": 2,
            **{name: getattr(protocol, name) for name in STORAGE_NAMES},
        })
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                     "<actual-generated-fast_api>", "exec"), namespace)
        model_raw = (service / "tests/fixtures/mineru_344_preimages/mineru/utils/model_utils.py").read_bytes()
        if hashlib.sha256(model_raw).hexdigest() != MODEL_PREIMAGE_SHA256:
            raise AssertionError("official model_utils preimage changed")
        generated_model = patch_source("mineru/utils/model_utils.py", model_raw.decode())
        helper_names = {"OwnedOperation", "drain_owned_awaitable", "to_thread_owned", "strict_processing_window_size"}
        helper_nodes = [node for node in ast.parse(generated_model).body
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in helper_names]
        if {node.name for node in helper_nodes} != helper_names:
            raise AssertionError("actual generated owned-thread helpers changed")
        exec(compile(ast.fix_missing_locations(ast.Module(body=helper_nodes, type_ignores=[])),
                     "<actual-generated-owned-operation>", "exec"), namespace)
        namespace["_configured_max_concurrent_requests"] = 2
        self.writer_class = generated_granted_writer()
        namespace["run_parse_job"] = self._parse_boundary
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(config={"max_concurrency": 7}))
        namespace["app"] = self.app
        self.manager = module.AsyncTaskManager(self.app)
        self.app.state.task_manager = self.manager
        namespace["get_task_manager"] = lambda: self.manager

    async def _parse_boundary(self, *, output_dir, uploads, request_options, config):
        """Synthetic parser output through the actual generated granted writer."""
        writer_class = self.writer_class
        outputs = self.outputs

        def write_all() -> None:
            for name in request_options.file_names:
                parse_dir = Path(output_dir) / name / request_options.parse_method
                writer = writer_class(str(parse_dir))
                for relative, data in outputs.items():
                    writer.write(relative.format(name=name), data)

        assert self.module is not None
        await self.module.to_thread_owned(write_all)
        return list(request_options.file_names)

    def options(self, name: str, *, uploads=None):
        return types.SimpleNamespace(
            files=uploads if uploads is not None else [Upload()],
            backend="hybrid-http-client", effort="medium", parse_method="auto", lang_list=["ch"],
            formula_enable=True, table_enable=True, image_analysis=False,
            server_url="http://fixture.invalid", return_md=True, return_middle_json=True,
            return_model_output=True, return_content_list=True, return_images=True,
            response_format_zip=True, return_original_file=False,
            client_side_output_generation=False, start_page_id=0, end_page_id=99999,
            agent_idempotency_key=f"{self.epoch:x}.{hashlib.sha256(name.encode()).hexdigest()}",
            agent_attempt_identity="attempt-original", agent_fence_identity="fence-original",
        )

    @staticmethod
    def request() -> types.SimpleNamespace:
        return types.SimpleNamespace(
            url_for=lambda route, **params: f"http://fixture.invalid/{route}/{params['task_id']}",
        )

    def hold_route(self):  # type: ignore[no-untyped-def]
        """The actual generated ``POST /agent/storage-holds/{task_id}`` handler on this manager."""
        nodes = [node for node in ast.parse(self.generated).body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "agent_storage_hold_decision"]
        if len(nodes) != 1:
            raise AssertionError("generated hold route drifted")
        nodes[0].decorator_list = []
        namespace: dict[str, Any] = {
            "get_task_manager": lambda: self.manager, "HTTPException": BoundaryHTTPException,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), nodes[0],
        ], type_ignores=[])), "<actual-generated-hold-route>", "exec"), namespace)
        return namespace["agent_storage_hold_decision"]

    def enroll_operator(self, credential: str) -> Path:
        """Enroll a synthetic credential's verifier at a fixture-owned container path."""
        path = self.root / "operator" / "storage-hold-operator.json"
        if self._operator_patch is None:
            self._operator_patch = patch.object(protocol, "STORAGE_HOLD_OPERATOR_PATH", path)
            self._operator_patch.start()
        protocol.enroll_storage_hold_operator(protocol.storage_hold_operator_verifier(credential))
        return path

    async def create(self, options):
        assert self.module is not None
        return await self.module.create_async_parse_task(options)

    async def dispose_test_tasks(self) -> None:
        """Bounded teardown only, distinct from assertions about real shutdown."""
        pending = [self.manager.dispatcher_task, self.manager.cleanup_task, *self.manager.active_tasks]
        live = [task for task in pending if task is not None and not task.done()]
        for task in live:
            task.cancel()
        if live:
            await asyncio.wait_for(asyncio.gather(*live, return_exceptions=True), 2)
        await asyncio.wait_for(self.manager.service_io.close(), 2)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.module is not None:
            sys.modules.pop(self.module.__name__, None)
        if self.proc_patch is not None:
            self.proc_patch.stop()
        if self._latch_patch is not None:
            self._latch_patch.stop()
        if self._spool_patch is not None:
            self._spool_patch.stop()
        if self._operator_patch is not None:
            self._operator_patch.stop()
        if self._prior_modules is not None:
            for name in tuple(sys.modules):
                if name == "mineru" or name.startswith("mineru.") or name == "mineru_vl_utils" or name.startswith("mineru_vl_utils."):
                    del sys.modules[name]
            sys.modules.update(self._prior_modules)
        if self.environment is not None:
            self.environment.stop()
