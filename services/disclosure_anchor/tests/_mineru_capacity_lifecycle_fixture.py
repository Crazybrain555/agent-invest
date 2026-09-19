"""A1 fixture derived from independently authored R1/R6 admission fixture.

Actual generated manager, ACK, ZIP and owned-thread helpers execute; no parser/model.

The upload bytes are deliberately synthetic. The fixture proves local ownership
and scheduling behavior only; it neither parses PDFs nor qualifies content.
"""

from __future__ import annotations

import ast
import asyncio
import anyio
import hashlib
import importlib.util
import json
import threading
import logging
import os
import shutil
import stat
import sys
import time
import types
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import patch_source

PREIMAGE_SHA256 = "f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39"
MODEL_PREIMAGE_SHA256 = "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
UPLOAD_BYTES = b"synthetic-unparsed-admission-ownership-fixture\n"


class BoundaryHTTPException(Exception):
    """Only the HTTP exception transport surface; no admission behavior."""

    def __init__(self, *, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class Upload:
    filename = "paper.pdf"

    def __init__(self, *, entered=None, release=None, failure=None):
        self.entered = entered
        self.release = release
        self.failure = failure
        self.reads = 0
        self.closed = False

    async def read(self, size):
        if self.reads == 0:
            if self.entered is not None:
                self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.failure is not None:
                raise self.failure
        self.reads += 1
        return UPLOAD_BYTES if self.reads == 1 else b""

    async def close(self):
        self.closed = True


class CapacityLifecycleFixture:
    """AST allowlist excludes module startup, routing, imports, and model code."""

    def __init__(self, root: Path, *, budget=2097152, limit=6291456, cli_h=7):
        self._closed = False
        self._prior_modules = None
        self.module = None
        self.proc_patch = None
        self.environment = None
        try:
            self._initialize(root, budget=budget, limit=limit, cli_h=cli_h)
        except BaseException:
            self.close()
            raise

    def _initialize(self, root: Path, *, budget, limit, cli_h):
        self.root = root
        self.limit = limit
        self.epoch = int(time.time())
        service = Path(protocol.__file__).resolve().parents[3]
        self._prior_modules = {name: value for name, value in sys.modules.items()
                               if name == "mineru" or name.startswith("mineru.")
                               or name == "mineru_vl_utils" or name.startswith("mineru_vl_utils.")}
        for name in self._prior_modules:
            del sys.modules[name]
        for name in ("mineru", "mineru.cli", "mineru_vl_utils", "mineru_vl_utils.vlm_client"):
            package = types.ModuleType(name)
            package.__path__ = []
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
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            spec.loader.exec_module(module)
        sys.modules["mineru.cli.agent_task_protocol_v2"] = protocol
        self.bootstrap = sys.modules["mineru.cli.agent_capacity_bootstrap"]
        self.observation = sys.modules["mineru.cli.agent_capacity_observation"]
        self.config_payload = {
            "contract_version": "mineru.capacity-config.v1", "parse_active_limit": 2,
            "total_nonterminal_limit": 3, "finalizer_active_limit": 1,
            "final_http_limit_per_loop": 7, "processing_window_size": 8,
            "pdf_render_processes_requested": 3, "hybrid_batch_ratio_requested": 2,
            "omp_num_threads": 4, "mkl_num_threads": 2, "openblas_num_threads": 1,
            "pipeline_inference_locks": True, "result_reservation_bytes": budget,
            "max_unacked_result_bytes": limit, "api_process_limit": 1, "api_event_loop_limit": 1,
        }
        raw_config = json.dumps(self.config_payload, sort_keys=True, separators=(",", ":")).encode()
        self.config_path = root / "capacity.json"
        self.config_path.write_bytes(raw_config)
        self.config_path.chmod(0o600)
        self.config_sha = "sha256:" + hashlib.sha256(raw_config).hexdigest()
        # Literal ENV values, independently supplied; no production DTO projection oracle.
        self.environment = patch.dict(os.environ, {
            "MINERU_CAPACITY_CONFIG_PATH": str(self.config_path), "MINERU_CAPACITY_CONFIG_SHA256": self.config_sha,
            "MINERU_API_OUTPUT_ROOT": str(root / "output"), "MINERU_API_TASK_RETENTION_SECONDS": "0",
            "MINERU_API_MAX_CONCURRENT_REQUESTS": "2", "MINERU_API_MAX_PENDING_TASKS": "3",
            "MINERU_API_FINALIZER_SLOTS": "1", "MINERU_PROCESSING_WINDOW_SIZE": "8",
            "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1",
            "MINERU_PDF_RENDER_THREADS": "3", "MINERU_HYBRID_BATCH_RATIO": "2",
            "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS": "1",
            "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": str(limit),
            "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": str(budget),
        }, clear=True)
        self.environment.start()
        service = Path(protocol.__file__).resolve().parents[3]
        preimage = (
            service / "tests/fixtures/mineru_344_preimages/mineru/cli/fast_api.py"
        )
        raw = preimage.read_bytes()
        if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
            raise AssertionError("official 3.4.4 FastAPI preimage changed")
        self.generated = patch_source("mineru/cli/fast_api.py", raw.decode())
        http_raw = (service / "tests/fixtures/mineru_344_preimages/mineru_vl_utils/vlm_client/http_client.py").read_text()
        self.generated_http = patch_source("mineru_vl_utils/vlm_client/http_client.py", http_raw)
        http_names = {"_ProcessAsyncRequestLimiter", "_bind_capacity_owner", "_apply_capacity_soft_drain",
                      "_capacity_http_snapshot", "_process_async_request_snapshot", "_process_async_request_limiter"}
        http_nodes = []
        for node in ast.parse(self.generated_http).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in http_names:
                http_nodes.append(node)
            elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name)
                    and (t.id.startswith("_PROCESS_ASYNC_REQUEST_") or t.id == "_CAPACITY_OWNER") for t in node.targets):
                http_nodes.append(node)
        if {n.name for n in http_nodes if hasattr(n, "name")} != http_names:
            raise AssertionError("actual HTTP owner extraction drift")
        self.http = types.ModuleType("mineru_vl_utils.vlm_client.http_client")
        self.http.__dict__.update(asyncio=asyncio, _agent_request_os=os, _agent_request_threading=threading)
        sys.modules[self.http.__name__] = self.http
        exec(compile(ast.fix_missing_locations(ast.Module(body=http_nodes, type_ignores=[])),
                     "<actual-generated-HTTP-owner>", "exec"), self.http.__dict__)
        original_read_text = Path.read_text
        # Only the Linux proc-file IO seam is synthetic; PID, loop and all owner objects are real.
        def proc_read(path, *args, **kwargs):
            if str(path) == "/proc/self/stat":
                return "271 (synthetic process name) S " + " ".join(["0"] * 18 + ["27182"])
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return "7b860196-6c69-44e5-a222-47a775ec8ceb\n"
            return original_read_text(path, *args, **kwargs)
        self.proc_patch = patch.object(Path, "read_text", proc_read)
        self.proc_patch.start()
        selected = {
            "StoredUpload",
            "AsyncParseTask",
            "AsyncTaskManager",
            "TaskWaitAbortedError",
            "_settle_service_operation",
            "_registry_view",
            "utc_now_iso",
            "get_int_env",
            "get_max_concurrent_requests",
            "get_max_pending_tasks",
            "get_task_retention_seconds",
            "get_task_cleanup_interval_seconds",
            "get_output_root",
            "cleanup_file",
            "build_upload_destination",
            "is_task_terminal",
            "_write_upload_chunk",
            "_prepare_ingress_tree",
            "save_upload_files",
            "create_task_output_dir",
            "create_async_parse_task",
            "ack_async_task_result", "health_check",
            "_hash_file", "_retained_result_sources", "_verify_and_close_result_sources",
            "_write_retained_zip_from_fds", "_commit_retained_result", "_build_retained_artifact_owned", "build_retained_task_result",
            "get_parse_dir", "get_images_dir_image_paths", "build_zip_arcname",
        }
        constants = {
            "TASK_PENDING",
            "TASK_PROCESSING",
            "TASK_COMPLETED",
            "TASK_FAILED",
            "TASK_TERMINAL_STATES",
            "DEFAULT_TASK_RETENTION_SECONDS",
            "DEFAULT_TASK_CLEANUP_INTERVAL_SECONDS",
            "DEFAULT_OUTPUT_ROOT",
            "_configured_max_concurrent_requests",
        }
        body = [
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            )
        ]
        found = set()
        for node in ast.parse(self.generated).body:
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in selected
            ):
                if node.name in {"ack_async_task_result", "health_check"}:
                    node.decorator_list = []
                body.append(node)
                found.add(node.name)
            elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id in constants for t in node.targets
            ):
                body.append(node)
        if found != selected:
            raise AssertionError(f"generated API extraction drift: {selected - found}")
        module = types.ModuleType(f"_admission_generated_{uuid.uuid4().hex}")
        sys.modules[module.__name__] = module
        self.module = module
        namespace = module.__dict__
        namespace.update(
            {
                "asyncio": asyncio,
                "os": os,
                "shutil": shutil,
                "stat": stat,
                "hashlib": hashlib,
                # Only external directory resolution is a literal fixture boundary.
                # ZIP enumeration/writing/ownership checks below are actual generated code.
                "resolve_parse_dir": lambda output, name, backend, method, **kw: Path(output) / name / method,
                "RESULT_IMAGE_SUFFIXES": {"png", "jpg", "jpeg"},
                "uuid": uuid,
                "Path": Path,
                "datetime": datetime,
                "timezone": timezone,
                "dataclass": dataclass,
                "asdict": asdict,
                "suppress": suppress,
                "HTTPException": BoundaryHTTPException,
                "logger": logging.getLogger("independent-admission-fixture"),
                "SUPPORTED_UPLOAD_SUFFIXES": ["pdf"],
                # External filename/suffix utilities receive a single fixed safe name.
                # Their behavior is outside this admission contract.
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
            }
        )
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                "<actual-generated-fast_api>",
                "exec",
            ),
            namespace,
        )
        model_preimage = service / "tests/fixtures/mineru_344_preimages/mineru/utils/model_utils.py"
        model_raw = model_preimage.read_bytes()
        if hashlib.sha256(model_raw).hexdigest() != MODEL_PREIMAGE_SHA256:
            raise AssertionError("official model_utils preimage changed")
        self.generated_model = patch_source("mineru/utils/model_utils.py", model_raw.decode())
        helper_names = {"OwnedOperation", "drain_owned_awaitable", "to_thread_owned", "strict_processing_window_size"}
        helper_nodes = [node for node in ast.parse(self.generated_model).body
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in helper_names]
        if {node.name for node in helper_nodes} != helper_names:
            raise AssertionError("actual generated owned-thread helpers changed")
        exec(compile(ast.fix_missing_locations(ast.Module(body=helper_nodes, type_ignores=[])),
                     "<actual-generated-owned-operation>", "exec"), namespace)
        # This is create_app's already-read Linux CLI/environment result at the
        # app construction seam, not a replacement manager/executor or live Linux proof.
        namespace["_configured_max_concurrent_requests"] = 2
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(config={"max_concurrency": cli_h}))
        namespace["app"] = self.app
        try:
            self.manager = module.AsyncTaskManager(
                self.app
            )
        except BaseException:
            sys.modules.pop(module.__name__, None)
            self.environment.stop()
            raise
        self.app.state.task_manager = self.manager
        namespace["get_task_manager"] = lambda: self.manager

    def options(self, name: str, *, upload=None, fence="fence-original"):
        return types.SimpleNamespace(
            files=[upload if upload is not None else Upload()],
            backend="hybrid-http-client",
            effort="medium",
            parse_method="auto",
            lang_list=["ch"],
            formula_enable=True,
            table_enable=True,
            image_analysis=False,
            server_url="http://fixture.invalid",
            return_md=True,
            return_middle_json=True,
            return_model_output=True,
            return_content_list=True,
            return_images=False,
            response_format_zip=True,
            return_original_file=False,
            client_side_output_generation=False,
            start_page_id=0,
            end_page_id=99999,
            agent_idempotency_key=f"{self.epoch:x}.{hashlib.sha256(name.encode()).hexdigest()}",
            agent_attempt_identity="attempt-original",
            agent_fence_identity=fence,
        )

    async def create(self, options):
        return await self.module.create_async_parse_task(options)



    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.module is not None:
            sys.modules.pop(self.module.__name__, None)
        if self.proc_patch is not None:
            self.proc_patch.stop()
        if self._prior_modules is not None:
            for name in tuple(sys.modules):
                if name == "mineru" or name.startswith("mineru.") or name == "mineru_vl_utils" or name.startswith("mineru_vl_utils."):
                    del sys.modules[name]
            sys.modules.update(self._prior_modules)
        if self.environment is not None:
            self.environment.stop()

    async def dispose_test_tasks(self):
        """Bounded teardown only, distinct from assertions about real shutdown."""
        pending = [
            self.manager.dispatcher_task,
            self.manager.cleanup_task,
            *self.manager.active_tasks,
        ]
        live = [task for task in pending if task is not None and not task.done()]
        for task in live:
            task.cancel()
        if live:
            await asyncio.wait_for(asyncio.gather(*live, return_exceptions=True), 2)
        await asyncio.wait_for(self.manager.service_io.close(), 2)
