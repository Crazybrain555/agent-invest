"""Independent admission fixture: real generated code and real registry, no parser.

The upload bytes are deliberately synthetic. The fixture proves local ownership
and scheduling behavior only; it neither parses PDFs nor qualifies content.
"""

from __future__ import annotations

import ast
import asyncio
import anyio
import hashlib
import logging
import os
import shutil
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


class AdmissionFixture:
    """AST allowlist excludes module startup, routing, imports, and model code."""

    def __init__(self, root: Path):
        self.root = root
        self.epoch = int(time.time())
        self.environment = patch.dict(
            os.environ,
            {
                "MINERU_API_OUTPUT_ROOT": str(root),
                "MINERU_API_MAX_PENDING_TASKS": "1",
                "MINERU_API_TASK_RETENTION_SECONDS": "0",
                "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "1024",
                "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "512",
            },
        )
        self.environment.start()
        service = Path(protocol.__file__).resolve().parents[3]
        preimage = (
            service / "tests/fixtures/mineru_344_preimages/mineru/cli/fast_api.py"
        )
        raw = preimage.read_bytes()
        if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
            raise AssertionError("official 3.4.4 FastAPI preimage changed")
        self.generated = patch_source("mineru/cli/fast_api.py", raw.decode())
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
        generated_model = patch_source("mineru/utils/model_utils.py", model_raw.decode())
        helper_names = {"OwnedOperation", "drain_owned_awaitable"}
        helper_nodes = [node for node in ast.parse(generated_model).body
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in helper_names]
        if {node.name for node in helper_nodes} != helper_names:
            raise AssertionError("actual generated owned-thread helpers changed")
        exec(compile(ast.fix_missing_locations(ast.Module(body=helper_nodes, type_ignores=[])),
                     "<actual-generated-owned-operation>", "exec"), namespace)
        self.manager = module.AsyncTaskManager(
            types.SimpleNamespace(state=types.SimpleNamespace(config={}))
        )
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

    def cold_registry(self):
        return protocol.DurableTaskRegistry(
            self.root / ".agent-task-protocol-v2/registry.json",
            max_unacked_result_bytes=1024,
            output_root=self.root,
            enforce_key_lifecycle=True,
        )

    def files(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".agent-task-protocol-v2" not in path.parts
        }

    def close(self):
        sys.modules.pop(self.module.__name__, None)
        self.environment.stop()

    def fail_parser(self, *, entered=None, release=None, seen=None):
        async def parse_boundary(**kwargs):
            if seen is not None:
                seen.append(kwargs["request_options"].task_id)
            if entered is not None:
                entered.set()
            if release is not None:
                await release.wait()
            raise RuntimeError("synthetic parser-boundary failure; PDF never parsed")

        self.module.run_parse_job = parse_boundary

    def seed_legacy_pending(self, name):
        """An old durable accepted obligation, not a bypass of new-key admission."""
        options = self.options(name)
        task_id = f"legacy-{name}"
        self.manager.task_protocol_v2.reconcile_or_create(
            idempotency_key=options.agent_idempotency_key,
            task_id=task_id,
            attempt_identity=options.agent_attempt_identity,
            fence_identity=options.agent_fence_identity,
        )
        output = self.root / task_id
        (output / "uploads").mkdir(parents=True)
        upload = output / "uploads/paper.pdf"
        upload.write_bytes(UPLOAD_BYTES)
        values = vars(options).copy()
        del values["files"]
        task = self.module.AsyncParseTask(
            **values,
            task_id=task_id,
            status="pending",
            file_names=["paper"],
            created_at=self.module.utc_now_iso(),
            output_dir=str(output),
            upload_names=["paper.pdf"],
            uploads=[str(upload)],
        )
        self.manager.task_protocol_v2.bind_task_payload(
            options.agent_idempotency_key, asdict(task)
        )
        return task

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
