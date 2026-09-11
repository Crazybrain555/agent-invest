"""Synthetic ``fast_api.py`` source for exercising the generated P1 integration.

The text below is not upstream MinerU.  It carries only the exact anchors the
compatibility patcher rewrites (bounded admission, quiescent shutdown, the
terminal processor, the two-stage terminal waiter, an HTTP conflict boundary and
the failed-allocation cleanup block) together with the grouped protocol import
that the real preimage already contains.  ``patch_source`` therefore produces
real generated code for the manager, which the tests execute against a real
registry and asyncio loop with stubs for FastAPI, logging and the parse stage.
This is a fake integration of the HTTP layer and a live integration of the
generated waiter/processor code.
"""

from __future__ import annotations

import ast
import asyncio
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Optional
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (
    DurableTaskRegistry,
    SplitTaskExecutor,
    TaskProtocolConflict,
    TaskRegistryPersistenceError,
    evict_consumed_routes,
    task_protocol_runtime_status,
)
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import patch_source

FAST_API_PERSISTENCE_FIXTURE = '''import asyncio
from mineru.utils.config_reader import (
    get_max_concurrent_requests as read_max_concurrent_requests,
    get_processing_window_size,
)
from mineru.cli.agent_task_protocol_v2 import (
    DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict,
    TaskRegistryPersistenceError,
    evict_consumed_routes, task_protocol_runtime_status,
)

_configured_max_concurrent_requests = 1


def get_max_concurrent_requests() -> int:
    return _configured_max_concurrent_requests


def get_task_retention_seconds() -> int:
    return 0


class TaskWaitAbortedError(RuntimeError):
    pass


async def allocate_task_fixture(task_manager, protocol_record, task_output_dir):
    try:
        raise HTTPException(status_code=400, detail="synthetic allocation failure")
    except HTTPException:
        if protocol_record is not None:
            with suppress(TaskProtocolConflict):
                task_manager.task_protocol_v2.abandon_unbound(protocol_record.idempotency_key)
        cleanup_file(task_output_dir)
        raise


async def lease_task_fixture(task_manager, idempotency_key: str, seconds: int = 300):
    try:
        lease_until = task_manager.task_protocol_v2.lease(idempotency_key, seconds=seconds)
    except TaskProtocolConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"lease_until_unix": lease_until}


class AsyncTaskManager:
    def __init__(self, fastapi_app: FastAPI):
        self.app = fastapi_app
        self.tasks = {}
        self.task_events = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.dispatcher_task = None
        self.cleanup_task = None
        self.active_tasks = set()
        self.manager_wakeup = asyncio.Event()
        self.last_worker_error: Optional[str] = None
        self.is_shutting_down = False
        self.task_retention_seconds = get_task_retention_seconds()
        self.task_cleanup_interval_seconds = get_task_cleanup_interval_seconds()
        self._next_submit_order = 1
        self.task_protocol_v2 = get_task_registry()

    async def start(self):
        self.is_shutting_down = False
        self.last_worker_error = None
        self.manager_wakeup = asyncio.Event()
        self.dispatcher_task = asyncio.create_task(self._dispatcher_loop())

    async def shutdown(self) -> None:
        self.is_shutting_down = True
        self._wake_waiters()
        if self.dispatcher_task is not None:
            self.dispatcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.dispatcher_task
            self.dispatcher_task = None
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
            self.cleanup_task = None

        pending = list(self.active_tasks)
        for processor in pending:
            processor.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.active_tasks.clear()

    async def submit(self, task: AsyncParseTask) -> None:
        task.submit_order = self._next_submit_order
        self._next_submit_order += 1
        self.tasks[task.task_id] = task
        self.task_events[task.task_id] = asyncio.Event()
        await self.queue.put(task.task_id)

    def _wake_waiters(self):
        self.manager_wakeup.set()

    def _signal_task_event(self, task_id):
        self.task_events[task_id].set()

    async def _dispatcher_loop(self) -> None:
        try:
            while True:
                task_id = await self.queue.get()
                processor = asyncio.create_task(
                    self._process_task(task_id),
                    name=f"mineru-fastapi-task-{task_id}",
                )
                self.active_tasks.add(processor)
                processor.add_done_callback(self._on_processor_done)
                self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_worker_error = str(exc)
            self._wake_waiters()
            logger.exception("Async task dispatcher crashed")
            raise

    def _on_processor_done(self, processor):
        self.active_tasks.discard(processor)

    async def _process_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return

        try:
            await self._run_task(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.status = TASK_FAILED
            task.error = str(exc)
            task.completed_at = utc_now_iso()
            self._signal_task_event(task_id)
            logger.exception(f"Async task failed: {task_id}")

    async def _run_task(self, task: AsyncParseTask) -> None:
        task.status = TASK_PROCESSING
        await task.release.wait()
        task.outcome()
        task.status = TASK_COMPLETED
        self._signal_task_event(task.task_id)

    async def wait_for_terminal_state(self, task_id: str) -> AsyncParseTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskWaitAbortedError("Task not found")
        if is_task_terminal(task.status):
            return task

        task_event = self.task_events.get(task_id)
        if task_event is None:
            raise TaskWaitAbortedError("Task event not found")
        if self.is_shutting_down:
            raise TaskWaitAbortedError("Task manager is shutting down")
        if self.last_worker_error is not None:
            raise TaskWaitAbortedError(self.last_worker_error)
        event_wait_task = asyncio.create_task(task_event.wait())
        manager_wait_task = asyncio.create_task(self.manager_wakeup.wait())
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        try:
            done, pending = await asyncio.wait(
                {event_wait_task, manager_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in pending:
                waiter.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for waiter in done:
                with suppress(asyncio.CancelledError):
                    waiter.result()
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskWaitAbortedError("Task disappeared while waiting")
        if is_task_terminal(task.status):
            return task
        if self.is_shutting_down:
            raise TaskWaitAbortedError("Task manager is shutting down")
        if self.last_worker_error is not None:
            raise TaskWaitAbortedError(self.last_worker_error)
        raise TaskWaitAbortedError("Task woke without a terminal state")


def health_payload(task_manager):
    return {
        "max_concurrent_requests": get_max_concurrent_requests(),
        "processing_window_size": get_processing_window_size(
            default=DEFAULT_PROCESSING_WINDOW_SIZE
        ),
    }


class _FixtureTelemetryApp:
    def get(self, **kwargs):
        return lambda function: function


app = _FixtureTelemetryApp()


@app.get(path="/health")
async def health_check():
    return health_payload(None)
'''


class HTTPExceptionStub(Exception):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class LoggerStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def exception(self, message: str) -> None:
        self.messages.append(message)


def _no_outcome() -> None:
    return None


class TaskStub:
    """Minimal in-memory task route for the generated manager."""

    def __init__(self, task_id: str, outcome: Callable[[], object] | None = None) -> None:
        self.task_id = task_id
        self.status = "pending"
        self.release = asyncio.Event()
        self.error: str | None = None
        self.completed_at: str | None = None
        self.submit_order = 0
        self.outcome = outcome if outcome is not None else _no_outcome


def generate_fast_api_source() -> str:
    """Run the real patcher over the synthetic fixture."""
    return patch_source("mineru/cli/fast_api.py", FAST_API_PERSISTENCE_FIXTURE)


def strip_mineru_imports(source: str) -> ast.Module:
    tree = ast.parse(source)
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("mineru")
        )
    ]
    return tree


def load_generated_fast_api(
    *,
    registry: DurableTaskRegistry,
    cleanup_file: Callable[[str], None] | None = None,
    logger: LoggerStub | None = None,
) -> dict[str, Any]:
    """Execute the generated module with stubs and return its namespace.

    The ``mineru.*`` imports are removed as AST nodes and replaced by the real
    protocol symbols plus explicit stubs, so no MinerU or FastAPI installation
    is required and nothing outside the temporary root is touched.
    """
    generated = generate_fast_api_source()
    tree = strip_mineru_imports(generated)
    namespace: dict[str, Any] = {
        "asyncio": asyncio,
        "os": os,
        "suppress": suppress,
        "Optional": Optional,
        "Any": Any,
        "HTTPException": HTTPExceptionStub,
        "FastAPI": object,
        "AsyncParseTask": TaskStub,
        "TASK_PENDING": "pending",
        "TASK_PROCESSING": "processing",
        "TASK_COMPLETED": "completed",
        "TASK_FAILED": "failed",
        "DEFAULT_PROCESSING_WINDOW_SIZE": 16,
        "get_task_cleanup_interval_seconds": lambda: 0,
        "get_processing_window_size": lambda default: default,
        "strict_processing_window_size": lambda: 16,
        "read_max_concurrent_requests": lambda: 1,
        "is_task_terminal": lambda status: status in {"completed", "failed"},
        "utc_now_iso": lambda: "synthetic-now",
        "logger": logger if logger is not None else LoggerStub(),
        "cleanup_file": cleanup_file if cleanup_file is not None else (lambda _path: None),
        "get_task_registry": lambda: registry,
        "DurableTaskRegistry": DurableTaskRegistry,
        "SplitTaskExecutor": SplitTaskExecutor,
        "TaskProtocolConflict": TaskProtocolConflict,
        "TaskRegistryPersistenceError": TaskRegistryPersistenceError,
        "evict_consumed_routes": evict_consumed_routes,
        "task_protocol_runtime_status": task_protocol_runtime_status,
    }
    with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "1"}):
        exec(compile(tree, "generated-fast-api-fixture", "exec"), namespace)
    namespace["__generated_source__"] = generated
    return namespace


async def build_manager(namespace: dict[str, Any]) -> Any:
    with patch.dict(os.environ, {"MINERU_API_MAX_PENDING_TASKS": "1"}):
        manager = namespace["AsyncTaskManager"](object())
    await manager.start()
    return manager


async def stop_manager(manager: Any) -> None:
    """Tear down without the generated quiescent shutdown's terminal demand."""
    dispatcher = manager.dispatcher_task
    if dispatcher is not None:
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher
        manager.dispatcher_task = None
    for processor in list(manager.active_tasks):
        processor.cancel()
        with suppress(BaseException):
            await processor
