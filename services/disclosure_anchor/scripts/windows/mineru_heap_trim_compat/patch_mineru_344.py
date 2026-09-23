#!/usr/bin/env python3
"""Build-time, exact-source runtime compatibility patch for MinerU 3.4.4.

The patch preserves the explicit, fail-visible glibc ``malloc_trim(0)`` hook,
single-owner serial execution and content-free phase evidence. Every source
file must match the deployed 3.4.4 bytes before any write occurs.
"""

from __future__ import annotations
import re

import hashlib
import json
import py_compile
from importlib import metadata
from pathlib import Path
from typing import Final

MINERU_VERSION: Final = "3.4.4"
MINERU_VL_UTILS_VERSION: Final = "1.0.5"
BASE_IMAGE_DIGEST: Final = (
    "sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458"
)
POLICY: Final = "glibc-malloc-trim-per-window.v1"
CAPACITY_POLICY: Final = "single-owner-serial-mineru.v1"
SITE_PACKAGES: Final = Path("/usr/local/lib/python3.12/dist-packages")
MARKER_PATH: Final = Path(
    "/opt/agent-invest/mineru-serial-v1/compatibility.json"
)
TARGET_PREIMAGE_SHA256: Final = {
    "mineru/cli/api_request.py": (
        "16e16ee7fe9d3b1872f6fb43e1f7b2e7d314d2f726311e821813abece0334e77"
    ),
    "mineru/cli/fast_api.py": (
        "f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39"
    ),
    "mineru/cli/common.py": (
        "d1e23e310bddc3da2d7f491be81ef112435824403d1c3a29e438505c1707dbc5"
    ),
    "mineru/backend/vlm/vlm_analyze.py": (
        "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
    ),
    "mineru/backend/hybrid/hybrid_analyze.py": (
        "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
    ),
    "mineru/utils/model_utils.py": (
        "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
    ),
    "mineru_vl_utils/post_process/cross_page_table.py": (
        "97581c69b92ae80df2a11f3dc986f329b26edca5af57e6052929aeadefab898f"
    ),
    "mineru_vl_utils/post_process/__init__.py": (
        "c1c426dfd5786d196a94854f8453b6deb800efd14c3749a8996ce201b29c9ad2"
    ),
    "mineru_vl_utils/vlm_client/http_client.py": (
        "afe42d8a5e310d27cb0173abf4d59ed6197bc0b60a0258f321a6cdedd07c6ba7"
    ),
}


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _replace_exact(
    source: str,
    old: str,
    new: str,
    *,
    count: int,
    label: str,
) -> str:
    observed = source.count(old)
    if observed != count:
        raise RuntimeError(
            f"{label} patch anchor count drifted: expected {count}, got {observed}"
        )
    return source.replace(old, new)


def _replace_exact_fixture_optional(
    source: str,
    old: str,
    new: str,
    *,
    count: int,
    label: str,
) -> str:
    """Skip retained-result anchors only in the manager-only unit fixture.

    Real installation remains protected by the full-file preimage digest.
    """
    if (
        label.startswith("FastAPI task protocol")
        and "async def create_async_parse_task(" not in source
    ):
        return source
    if "class AsyncParseTask:" not in source:
        return source
    return _replace_exact(source, old, new, count=count, label=label)


def _patch_owned_render_await(source: str) -> str:
    return _replace_exact(
        source,
        "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
        "                    pdf_bytes,\n"
        "                    start_page_id=window_start,\n"
        "                    end_page_id=window_end,\n"
        "                    image_type=ImageType.PIL,\n"
        "                )\n",
        "                images_list = await drain_owned_awaitable(\n"
        "                    aio_load_images_from_pdf_bytes_range(\n"
        "                        pdf_bytes,\n"
        "                        start_page_id=window_start,\n"
        "                        end_page_id=window_end,\n"
        "                        image_type=ImageType.PIL,\n"
        "                    ),\n"
        "                    on_cancel_result=_close_images,\n"
        "                )\n",
        count=1,
        label="async render result ownership on cancellation",
    )


def _replace_exact_occurrence(
    source: str,
    old: str,
    new: str,
    *,
    count: int,
    occurrence: int,
    label: str,
) -> str:
    observed = source.count(old)
    if observed != count or not 0 <= occurrence < count:
        raise RuntimeError(
            f"{label} patch anchor count drifted: expected {count}, got {observed}"
        )
    start = -1
    for _ in range(occurrence + 1):
        start = source.index(old, start + 1)
    return source[:start] + new + source[start + len(old) :]


def _replace_exact_span(
    source: str,
    start_marker: str,
    end_marker: str,
    replacement: str,
    *,
    label: str,
) -> str:
    if source.count(start_marker) != 1 or source.count(end_marker) != 1:
        raise RuntimeError(f"{label} patch span drifted")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement + source[end:]




def _patch_registry_persistence_behavior(source: str) -> str:
    """Finish P1 integration on the generated FastAPI source.

    The production import groups the registry symbols on one line, while small
    compatibility fixtures may omit individual endpoint/cleanup bodies.  Patch
    only anchors that are actually present, but require the full source's
    worker and ownership boundaries to be transformed.
    """
    counters = {"processor": 0, "http": 0, "cleanup": 0}

    import_block = re.search(
        r"from mineru\.cli\.agent_task_protocol_v2 import \(\n"
        r"(?P<body>.*?)^\)\n",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    if import_block is None:
        # Reduced unit fixtures intentionally omit the real module imports.
        # They are not executable generated sources, so leave their legacy
        # worker/endpoint snippets untouched.
        return source
    if "TaskRegistryPersistenceError" not in import_block.group("body"):
        raise RuntimeError("registry persistence exception import is absent")

    wait_failure_marker = (
        "self.task_wait_failures: dict[str, TaskRegistryPersistenceError]"
    )
    if wait_failure_marker not in source:
        source = _replace_exact(
            source,
            "        self.last_worker_error: Optional[str] = None\n",
            "        self.last_worker_error: Optional[str] = None\n"
            "        self.task_wait_failures: dict[str, TaskRegistryPersistenceError] = {}\n",
            count=1,
            label="FastAPI task-specific persistence wait state",
        )
        source = _replace_exact(
            source,
            "        self.last_worker_error = None\n"
            "        self.manager_wakeup = asyncio.Event()\n",
            "        self.last_worker_error = None\n"
            "        self.task_wait_failures.clear()\n"
            "        self.manager_wakeup = asyncio.Event()\n",
            count=1,
            label="FastAPI recovered waiter state reset",
        )
        source = _replace_exact(
            source,
            "    async def wait_for_terminal_state(self, task_id: str) -> AsyncParseTask:\n",
            "    def _raise_task_wait_failure(self, task_id: str) -> None:\n"
            "        failure = self.task_wait_failures.get(task_id)\n"
            "        if failure is None:\n"
            "            return\n"
            "        status = self.task_protocol_v2.persistence_status()\n"
            "        recovery_action = (\n"
            '            status.get("recovery_action") or "restart task manager"\n'
            "        )\n"
            "        raise TaskWaitAbortedError(\n"
            '            "Task registry persistence is unavailable while waiting; "\n'
            '            f"outcome={failure.outcome}; recovery={recovery_action}"\n'
            "        ) from failure\n\n"
            "    async def wait_for_terminal_state(self, task_id: str) -> AsyncParseTask:\n",
            count=1,
            label="FastAPI task-specific persistence wait outcome",
        )
        source = _replace_exact(
            source,
            "        if is_task_terminal(task.status):\n"
            "            return task\n\n"
            "        task_event = self.task_events.get(task_id)\n",
            "        if is_task_terminal(task.status):\n"
            "            return task\n"
            "        self._raise_task_wait_failure(task_id)\n\n"
            "        task_event = self.task_events.get(task_id)\n",
            count=1,
            label="FastAPI later waiter persistence check",
        )
        source = _replace_exact(
            source,
            "        if is_task_terminal(task.status):\n"
            "            return task\n"
            "        if self.is_shutting_down:\n",
            "        if is_task_terminal(task.status):\n"
            "            return task\n"
            "        self._raise_task_wait_failure(task_id)\n"
            "        if self.is_shutting_down:\n",
            count=1,
            label="FastAPI awakened waiter persistence check",
        )
        source = _replace_exact(
            source,
            "        event_wait_task = asyncio.create_task(task_event.wait())\n"
            "        manager_wait_task = asyncio.create_task(self.manager_wakeup.wait())\n"
            "        done: set[asyncio.Task[Any]] = set()\n"
            "        pending: set[asyncio.Task[Any]] = set()\n"
            "        try:\n"
            "            done, pending = await asyncio.wait(\n"
            "                {event_wait_task, manager_wait_task},\n"
            "                return_when=asyncio.FIRST_COMPLETED,\n"
            "            )\n"
            "        finally:\n"
            "            for waiter in pending:\n"
            "                waiter.cancel()\n"
            "            if pending:\n"
            "                await asyncio.gather(*pending, return_exceptions=True)\n"
            "            for waiter in done:\n"
            "                with suppress(asyncio.CancelledError):\n"
            "                    waiter.result()\n",
            "        event_wait_task = asyncio.create_task(task_event.wait())\n"
            "        manager_wait_task = asyncio.create_task(self.manager_wakeup.wait())\n"
            "        wait_helpers = (event_wait_task, manager_wait_task)\n"
            "        done: set[asyncio.Task[Any]] = set()\n"
            "        try:\n"
            "            done, _ = await asyncio.wait(\n"
            "                wait_helpers,\n"
            "                return_when=asyncio.FIRST_COMPLETED,\n"
            "            )\n"
            "        finally:\n"
            "            for waiter in wait_helpers:\n"
            "                waiter.cancel()\n"
            "            await asyncio.gather(*wait_helpers, return_exceptions=True)\n"
            "        for waiter in done:\n"
            "            with suppress(asyncio.CancelledError):\n"
            "                waiter.result()\n",
            count=1,
            label="FastAPI cancellation-safe waiter helper cleanup",
        )

    processor_marker = "task status remains nonterminal"
    if processor_marker not in source:
        processor_pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)except Exception as exc:\n"
            r"(?P=i)    task\.status = TASK_FAILED"
        )

        def processor_replacement(match: re.Match[str]) -> str:
            indent = match.group("i")
            return (
                f"{indent}except TaskRegistryPersistenceError as exc:\n"
                f"{indent}    self.task_wait_failures[task_id] = exc\n"
                f"{indent}    self._signal_task_event(task_id)\n"
                f"{indent}    logger.exception("
                '"Task registry persistence failed; task status remains nonterminal"'
                ")\n"
                f"{indent}    raise\n"
                f"{indent}except Exception as exc:\n"
                f"{indent}    task.status = TASK_FAILED"
            )

        source, counters["processor"] = processor_pattern.subn(
            processor_replacement,
            source,
        )

    cleanup_expected = (
        "with suppress(TaskProtocolConflict):" in source
        and "abandon_unbound" in source
        and "cleanup_file(task_output_dir)" in source
    )
    conflict_handlers_present = "except TaskProtocolConflict as exc:" in source

    lines = source.splitlines()
    rewritten: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if stripped == "except TaskProtocolConflict as exc:":
            recent = "\n".join(rewritten[-5:])
            if "except TaskRegistryPersistenceError as exc:" not in recent:
                rewritten.extend(
                    [
                        f"{indent}except TaskRegistryPersistenceError as exc:",
                        f"{indent}    raise HTTPException(",
                        f"{indent}        status_code=503, detail=str(exc)",
                        f"{indent}    ) from exc",
                    ]
                )
                counters["http"] += 1
        if stripped == "if protocol_record is not None:":
            cursor = index + 1
            while (
                cursor < min(len(lines), index + 24)
                and lines[cursor].strip() != "cleanup_file(task_output_dir)"
            ):
                cursor += 1
            block = lines[index : cursor + 1] if cursor < len(lines) else []
            joined = "\n".join(block)
            if (
                cursor < len(lines)
                and "with suppress(TaskProtocolConflict):" in joined
                and "abandon_unbound" in joined
            ):
                with_index = next(
                    pos
                    for pos in range(index + 1, cursor)
                    if "with suppress(TaskProtocolConflict):" in lines[pos]
                )
                call_lines = lines[with_index + 1 : cursor]
                rewritten.extend(
                    [
                        f"{indent}if protocol_record is None:",
                        f"{indent}    cleanup_file(task_output_dir)",
                        f"{indent}else:",
                        f"{indent}    try:",
                    ]
                )
                rewritten.extend(call_lines)
                rewritten.extend(
                    [
                        f"{indent}    except TaskRegistryPersistenceError:",
                        f"{indent}        raise",
                        f"{indent}    except TaskProtocolConflict:",
                        # A conflict means the registry may already own the
                        # input.  Preserve it for exact reconciliation.
                        f"{indent}        pass",
                        f"{indent}    else:",
                        f"{indent}        cleanup_file(task_output_dir)",
                    ]
                )
                counters["cleanup"] += 1
                index = cursor + 1
                continue
        rewritten.append(line)
        index += 1
    source = "\n".join(rewritten) + ("\n" if source.endswith("\n") else "")

    if "async def _process_task" in source and processor_marker not in source:
        raise RuntimeError(
            f"registry persistence worker guard was not patched: {counters}"
        )
    if "async def wait_for_terminal_state" in source and (
        wait_failure_marker not in source
        or "self.task_wait_failures[task_id] = exc" not in source
        or source.count("self._raise_task_wait_failure(task_id)") != 2
        or "wait_helpers = (event_wait_task, manager_wait_task)" not in source
        or "await asyncio.gather(*wait_helpers, return_exceptions=True)" not in source
        or "done, pending = await asyncio.wait" in source
    ):
        raise RuntimeError(
            f"registry persistence waiter guard was not patched: {counters}"
        )
    if cleanup_expected and counters["cleanup"] < 1:
        raise RuntimeError(
            f"bound task-tree cleanup guard was not patched: {counters}"
        )
    if conflict_handlers_present and "except TaskRegistryPersistenceError as exc:" not in source:
        raise RuntimeError(
            f"registry persistence HTTP guard was not patched: {counters}"
        )
    return source


def _patch_admission_responsibility(source: str) -> str:
    """Use durable ingress as the acceptance authority in the real API module."""
    if "from mineru.cli.agent_task_protocol_v2 import (" not in source:
        return source  # Reduced compatibility fixtures are not serving modules.
    if "async def create_async_parse_task(" not in source:
        # Same reduced-fixture boundary as _replace_exact_fixture_optional;
        # the installer checks the whole official preimage before patching.
        return source
    source = _replace_exact(
        source, "    DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict,\n",
        "    DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict, TaskAdmissionFull,\n",
        count=1, label="FastAPI admission exception import",
    )
    start = source.index("async def create_async_parse_task(\n")
    end = source.index("\n\nclass AsyncTaskManager:\n", start)
    original = source[start:end]
    upload_start = original.index("        uploads = await save_upload_files(")
    upload_end = original.index("        return task\n", upload_start) + len("        return task\n")
    upload_body = original[upload_start:upload_end]
    creation = '''async def create_async_parse_task(
    request_options: ParseRequestOptions,
) -> AsyncParseTask:
    task_manager = get_task_manager()
    identities = (request_options.agent_idempotency_key, request_options.agent_attempt_identity, request_options.agent_fence_identity)
    if not all(isinstance(item, str) and item for item in identities):
        raise HTTPException(status_code=400, detail="Task protocol v2 identities are required")
    try:
        record, created = task_manager.begin_submission(request_options)
    except TaskAdmissionFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except TaskRegistryPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TaskProtocolConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created:
        return task_manager.reconcile_submission(record)
    task_id = record.task_id
    try:
        task_output_dir = create_task_output_dir(task_id)
        uploads_dir = os.path.join(task_output_dir, "uploads")
        os.mkdir(uploads_dir)
        task_manager.task_protocol_v2.bind_ingress_root(record.idempotency_key)
''' + upload_body + '''    except BaseException as exc:
        try:
            current = task_manager.task_protocol_v2.get(record.idempotency_key)
            if current is not None and current.state in {"ingress", "ingress_cleanup"}:
                task_manager.task_protocol_v2.abort_ingress(record.idempotency_key)
        except BaseException as cleanup_exc:
            raise cleanup_exc from exc
        if isinstance(exc, TaskRegistryPersistenceError):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, TaskProtocolConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    finally:
        task_manager.finish_submission(task_id)
'''
    source = source[:start] + creation + source[end:]
    source = _replace_exact(
        source, "        self._next_submit_order = 1\n",
        "        self._next_submit_order = 1\n"
        "        self._scheduled_task_ids: set[str] = set()\n"
        "        self._ingress_in_flight: set[str] = set()\n"
        "        self._ingress_drained = asyncio.Event()\n"
        "        self._ingress_drained.set()\n"
        "        self._schedule_changed = asyncio.Event()\n",
        count=1, label="FastAPI live scheduling and upload ownership",
    )
    source = _replace_exact(
        source,
        "            if task.status not in TASK_TERMINAL_STATES:\n"
        "                task.status = TASK_PENDING\n"
        "                self.queue.put_nowait(task.task_id)\n",
        "        self._refill_pending_queue()\n",
        count=1, label="FastAPI bounded recovery refill",
    )
    start = source.index("    async def shutdown(self) -> None:\n", source.index("class AsyncTaskManager:"))
    end = source.index("    def get_queued_ahead(", start)
    source = source[:start] + '''    def begin_submission(self, options):
        key = options.agent_idempotency_key
        existing = self.task_protocol_v2.get(key)
        if existing is None and self.is_shutting_down:
            raise HTTPException(status_code=503, detail="Task manager is shutting down")
        if existing is None:
            reason = self.admission_snapshot()["blocked_reason"]
            if reason not in {None, "capacity_full"}:
                raise HTTPException(status_code=503, detail=reason)
        record, created = self.task_protocol_v2.reconcile_or_create(
            idempotency_key=key, task_id=str(uuid.uuid4()),
            attempt_identity=options.agent_attempt_identity,
            fence_identity=options.agent_fence_identity,
            max_nonterminal_tasks=self.max_nonterminal_tasks,
        )
        if created:
            self._ingress_in_flight.add(record.task_id)
            self._ingress_drained.clear()
        return record, created

    def finish_submission(self, task_id: str) -> None:
        self._ingress_in_flight.discard(task_id)
        if not self._ingress_in_flight:
            self._ingress_drained.set()

    def reconcile_submission(self, record):
        if record.state in {"ingress", "ingress_cleanup"}:
            raise HTTPException(status_code=503, detail={
                "code": ("task_ingress_in_progress" if record.task_id in self._ingress_in_flight
                         else "ingress_recovery_required"), "task_id": record.task_id,
                "accepted": False,
            })
        task = self.get(record.task_id)
        if task is None:
            raise HTTPException(status_code=410 if record.state == "consumed" else 503,
                                detail="Task responsibility requires reconciliation")
        return task

    def _refill_pending_queue(self) -> None:
        if self.last_worker_error is not None:
            return
        for task in sorted(self.tasks.values(), key=lambda item: (item.submit_order, item.created_at, item.task_id)):
            if len(self._scheduled_task_ids) >= self.max_nonterminal_tasks or self.queue.full():
                break
            if task.status == TASK_PENDING and task.task_id not in self._scheduled_task_ids:
                self.queue.put_nowait(task.task_id)
                self._scheduled_task_ids.add(task.task_id)

    def _finish_scheduled_task(self, task_id, processor) -> None:
        self._on_processor_done(processor)
        if processor.cancelled():
            self.last_worker_error = "Task processor was cancelled with retained responsibility"
        self._scheduled_task_ids.discard(task_id)
        try:
            self._refill_pending_queue()
        except Exception as exc:
            self.last_worker_error = str(exc)
            self._wake_waiters()
            raise
        finally:
            self._schedule_changed.set()

    async def shutdown(self) -> None:
        self.is_shutting_down = True
        self._wake_waiters()
        await self._ingress_drained.wait()
        self._refill_pending_queue()
        while self._scheduled_task_ids:
            if self.last_worker_error is not None:
                raise RuntimeError("Task shutdown is incomplete: " + self.last_worker_error)
            if self.dispatcher_task is None or self.dispatcher_task.done():
                raise RuntimeError("Task dispatcher stopped with retained responsibility")
            self._schedule_changed.clear()
            await self._schedule_changed.wait()
        status = self.task_protocol_v2.admission_status(set(self.tasks))
        if status["durable_nonterminal_tasks"]:
            raise RuntimeError("durable task responsibilities remain during shutdown")
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
        self.active_tasks.clear()
        self.task_protocol_v2.cleanup_consumed()

    async def submit(self, task: AsyncParseTask) -> None:
        record = self.task_protocol_v2.get(task.agent_idempotency_key)
        if record is None or record.task_id != task.task_id or record.task_payload is None:
            raise TaskProtocolConflict("Task was not durably accepted")
        if task.task_id not in self.tasks:
            task.submit_order = self._next_submit_order
            self._next_submit_order += 1
            self.tasks[task.task_id] = task
            self.task_events[task.task_id] = asyncio.Event()
        # Existing accepted responsibility may complete ingress after stop.
        # The registry already reserved capacity; queue fullness is not rejection.
        self._refill_pending_queue()

    def get(self, task_id: str) -> Optional[AsyncParseTask]:
        record = self.task_protocol_v2.get_by_task_id(task_id)
        if record is None or record.state in {"ingress", "ingress_cleanup", "consumed"}:
            return None
        failure = self.task_wait_failures.get(task_id)
        if failure is not None:
            raise HTTPException(status_code=503, detail={
                "code": "accepted_recovery_required", "task_id": task_id, "accepted": True,
                "phase": failure.phase, "outcome": failure.outcome, "committed": failure.committed,
            }) from failure
        if record.state in {"processing", "finalizing"} and task_id not in self._scheduled_task_ids:
            raise HTTPException(status_code=503, detail={
                "code": "accepted_recovery_required", "task_id": task_id, "accepted": True,
            }) from self.task_wait_failures.get(task_id)
        payload = self.task_protocol_v2.task_payload_for_route(record.idempotency_key)
        if payload is None:
            return None
        task = self.tasks.get(task_id)
        if task is None:
            task = AsyncParseTask(**payload)
            self.tasks[task_id] = task
            self.task_events.setdefault(task_id, asyncio.Event())
        else:
            for name in ("status", "error", "result_artifact_path", "result_artifact_sha256",
                         "result_artifact_bytes", "result_artifact_owner"):
                setattr(task, name, payload[name])
        self._refill_pending_queue()
        return task

    def admission_snapshot(self):
        snapshot = self.task_protocol_v2.admission_status(set(self.tasks), self._ingress_in_flight)
        count = snapshot["durable_nonterminal_tasks"]
        reason = None
        if self.is_shutting_down:
            reason = "shutting_down"
        elif self.last_worker_error is not None:
            reason = "worker_unavailable"
        elif snapshot["unowned_ingress_tasks"] or snapshot["ingress_cleanup_tasks"]:
            reason = "ingress_recovery_required"
        elif snapshot["routeless_accepted_tasks"]:
            reason = "accepted_recovery_required"
        elif count > self.max_nonterminal_tasks:
            reason = "recovery_overcommitted"
        elif count == self.max_nonterminal_tasks:
            reason = "capacity_full"
        snapshot.update(
            nonterminal_limit=self.max_nonterminal_tasks,
            scheduled_tasks=len(self._scheduled_task_ids), queue_depth=self.queue.qsize(),
            active_processors=sum(not task.done() for task in self.active_tasks),
            recovery_overcommitted=count > self.max_nonterminal_tasks,
            admission_open=reason is None, blocked_reason=reason,
        )
        return snapshot

''' + source[end:]
    source = _replace_exact(
        source, "    task_output_dir.mkdir(parents=True, exist_ok=True)\n",
        "    task_output_dir.mkdir(exist_ok=False)\n",
        count=1, label="FastAPI new-only task output directory",
    )
    source = _replace_exact(
        source, "    def _wake_waiters(self) -> None:\n",
        "    def _wake_waiters(self) -> None:\n        self._schedule_changed.set()\n",
        count=1, label="FastAPI shutdown failure wakeup",
    )
    source = _replace_exact(
        source, "    stats = task_manager.get_stats()\n",
        "    stats = task_manager.get_stats()\n    admission = task_manager.admission_snapshot()\n",
        count=1, label="FastAPI durable admission health snapshot",
    )
    source = _replace_exact(
        source, '        "queued_tasks": stats[TASK_PENDING],\n'
        '        "processing_tasks": stats[TASK_PROCESSING],\n',
        '        "task_admission": admission,\n'
        '        "queued_tasks": admission["ingress_tasks"] + admission["accepted_pending_tasks"],\n'
        '        "processing_tasks": admission["accepted_processing_tasks"] + admission["accepted_finalizing_tasks"],\n',
        count=1, label="FastAPI health includes upload and finalizer responsibility",
    )
    source = _replace_exact(
        source, '        "status": "healthy",\n',
        '        "status": "recovering" if admission["recovery_overcommitted"] else "healthy",\n',
        count=1, label="FastAPI overcommitted recovery is not qualified healthy",
    )
    source = _replace_exact(
        source, "                processor.add_done_callback(self._on_processor_done)\n",
        "                processor.add_done_callback(\n"
        "                    lambda done, key=task_id: self._finish_scheduled_task(key, done)\n"
        "                )\n", count=1, label="FastAPI refill on actual task completion",
    )
    source = _replace_exact(
        source,
        "    task = None if record is None else task_manager.get(record.task_id)\n",
        '    task = None if record is None or record.state == "consumed" else task_manager.reconcile_submission(record)\n',
        count=1, label="FastAPI ingress-aware keyed lookup",
    )
    return source


def _patch_result_capacity_before_parse(source: str) -> str:
    if "async def create_async_parse_task(" not in source:
        return source
    replacements = (
        (
            "        protocol_root = get_output_root() / \".agent-task-protocol-v2\"\n",
            "        protocol_root = get_output_root() / \".agent-task-protocol-v2\"\n"
            "        result_limit = int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES\", \"2147483648\"))\n"
            "        result_budget = int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES\", \"268435456\"))\n"
            "        if not 0 < result_budget <= result_limit:\n"
            "            raise ValueError(\"result reservation must be positive and within its limit\")\n",
        ),
        (
            "            max_unacked_result_bytes=int(\n"
            "                os.getenv(\"MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES\", \"2147483648\")\n"
            "            ),\n",
            "            max_unacked_result_bytes=result_limit,\n",
        ),
        (
            "            result_reservation_bytes=int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES\", \"268435456\")),\n",
            "            result_reservation_bytes=result_budget,\n",
        ),
        (
            "DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict, TaskAdmissionFull,",
            "DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict, TaskAdmissionFull,\n"
            "    TaskResultCapacityRecoveryRequired, TaskExecutionStopped,",
        ),
        (
            "self.task_wait_failures: dict[str, TaskRegistryPersistenceError] = {}",
            "self.task_wait_failures: dict[str, TaskRegistryPersistenceError | TaskResultCapacityRecoveryRequired] = {}",
        ),
        (
            "        self.task_wait_failures.clear()\n",
            "        self.task_wait_failures.clear()\n"
            "        self._stopped_pending_task_ids.clear()\n"
            "        self.task_protocol_executor.start()\n",
        ),
        (
            "        self._scheduled_task_ids: set[str] = set()\n",
            "        self._scheduled_task_ids: set[str] = set()\n"
            "        self._stopped_pending_task_ids: set[str] = set()\n",
        ),
        (
            "            if task.status == TASK_PENDING and task.task_id not in self._scheduled_task_ids:\n",
            "            if (task.status == TASK_PENDING\n"
            "                    and task.task_id not in self._scheduled_task_ids\n"
            "                    and task.task_id not in self._stopped_pending_task_ids):\n",
        ),
        (
            "    def _wake_waiters(self) -> None:\n",
            "    def _wake_waiters(self) -> None:\n"
            "        if self.is_shutting_down or self.last_worker_error is not None:\n"
            "            self.task_protocol_executor.begin_shutdown(\n"
            "                abort_pending=self.last_worker_error is not None\n"
            "            )\n"
            "        else:\n"
            "            self.task_protocol_executor.notify_result_capacity_changed()\n",
        ),
        (
            "            logger.error(f\"Async task processor crashed: {exception}\")\n"
            "            self.last_worker_error = str(exception)\n",
            "            logger.error(f\"Async task processor crashed: {exception}\")\n"
            "            self.last_worker_error = str(exception)\n"
            "            self._wake_waiters()\n",
        ),
        (
            "            self.last_worker_error = \"Task processor was cancelled with retained responsibility\"\n",
            "            self.last_worker_error = \"Task processor was cancelled with retained responsibility\"\n"
            "            self._wake_waiters()\n",
        ),
        (
            "            logger.exception(\"Async task cleanup loop crashed\")\n",
            "            self._wake_waiters()\n"
            "            logger.exception(\"Async task cleanup loop crashed\")\n",
        ),
        (
            "                await build_retained_task_result(task)\n",
            "                await build_retained_task_result(\n"
            "                    task, byte_budget=self.task_protocol_executor.result_reservation_bytes\n"
            "                )\n",
        ),
        (
            "        except asyncio.CancelledError:\n"
            "            task.status = TASK_FAILED\n",
            "        except TaskExecutionStopped as exc:\n"
            "            if (exc.capacity_wait and self.is_shutting_down\n"
            "                    and self.last_worker_error is None):\n"
            "                self._stopped_pending_task_ids.add(task_id)\n"
            "                self._signal_task_event(task_id)\n"
            "                return\n"
            "            self._signal_task_event(task_id)\n"
            "            raise\n"
            "        except asyncio.CancelledError:\n"
            "            if task.status == TASK_PENDING:\n"
            "                self._signal_task_event(task_id)\n"
            "                raise\n"
            "            task.status = TASK_FAILED\n",
        ),
        (
            "        except TaskRegistryPersistenceError as exc:\n"
            "            self.task_wait_failures[task_id] = exc\n",
            "        except (TaskRegistryPersistenceError, TaskResultCapacityRecoveryRequired) as exc:\n"
            "            self.task_wait_failures[task_id] = exc\n",
        ),
        (
            "        failure = self.task_wait_failures.get(task_id)\n"
            "        if failure is not None:\n",
            "        failure = self.task_wait_failures.get(task_id)\n"
            "        if isinstance(failure, TaskResultCapacityRecoveryRequired):\n"
            "            raise HTTPException(status_code=503, detail={\n"
            "                \"code\": \"result_capacity_recovery_required\",\n"
            "                \"task_id\": task_id, \"accepted\": True,\n"
            "            }) from failure\n"
            "        if failure is not None:\n",
        ),
        (
            "        if failure is None:\n            return\n"
            "        status = self.task_protocol_v2.persistence_status()\n",
            "        if failure is None:\n            return\n"
            "        if isinstance(failure, TaskResultCapacityRecoveryRequired):\n"
            "            raise TaskWaitAbortedError(\n"
            "                \"Result capacity recovery requires owned cleanup and manager restart\"\n"
            "            ) from failure\n"
            "        status = self.task_protocol_v2.persistence_status()\n",
        ),
        (
            "        cleaned = self.task_protocol_v2.cleanup_consumed()\n",
            "        cleaned = self.task_protocol_v2.cleanup_consumed()\n"
            "        self.task_protocol_executor.notify_result_capacity_changed()\n",
        ),
        (
            "        task_manager.task_protocol_v2.cleanup_consumed()\n",
            "        task_manager.task_protocol_v2.cleanup_consumed()\n"
            "        task_manager.task_protocol_executor.notify_result_capacity_changed()\n",
        ),
    )
    for number, (old, new) in enumerate(replacements):
        source = _replace_exact(source, old, new, count=1, label=f"result capacity wiring {number}")
    return source


def _patch_explicit_capacity(source: str) -> str:
    """Wire the new explicit profile; preserve the unselected legacy branch."""
    if "async def create_async_parse_task(" not in source:
        return source
    replacements = (
        (
            "import os\n",
            "import os\n"
            "if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ\n"
            "        or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):\n"
            "    from mineru.cli.agent_capacity_bootstrap import get_process_capacity\n"
            "    get_process_capacity()  # Validate startup before model imports.\n",
        ),
        (
            "def get_max_concurrent_requests() -> int:\n",
            "def get_max_concurrent_requests() -> int:\n"
            "    if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ\n"
            "            or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):\n"
            "        from mineru.cli.agent_capacity_bootstrap import get_process_capacity\n"
            "        config = get_process_capacity()\n"
            "        if (type(_configured_max_concurrent_requests) is not int\n"
            "                or _configured_max_concurrent_requests != config.parse_active_limit):\n"
            "            raise RuntimeError('MinerU API parse capacity differs from its config')\n"
            "        return config.parse_active_limit\n",
        ),
        (
            "def get_max_pending_tasks() -> int:\n",
            "def get_max_pending_tasks() -> int:\n"
            "    if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ\n"
            "            or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):\n"
            "        from mineru.cli.agent_capacity_bootstrap import get_process_capacity\n"
            "        return get_process_capacity().total_nonterminal_limit\n",
        ),
        (
            "        self.max_nonterminal_tasks = get_max_pending_tasks()\n",
            "        self.capacity_config = None\n"
            "        self.capacity_observer = None\n"
            "        if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ\n"
            "                or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):\n"
            "            from mineru.cli.agent_capacity_bootstrap import get_process_capacity\n"
            "            self.capacity_config = get_process_capacity()\n"
            "        self.max_nonterminal_tasks = get_max_pending_tasks()\n",
        ),
        (
            "            parse_slots=get_max_concurrent_requests(), finalizer_slots=1,\n",
            "            parse_slots=get_max_concurrent_requests(),\n"
            "            finalizer_slots=(1 if self.capacity_config is None\n"
            "                             else self.capacity_config.finalizer_active_limit),\n",
        ),
        (
            "    async def start(self) -> None:\n",
            "    async def start(self) -> None:\n"
            "        if self.capacity_config is not None:\n"
            "            from mineru.cli.agent_capacity_bootstrap import verify_http_capacity\n"
            "            from mineru.cli.agent_capacity_observation import CapacityServingObservation\n"
            "            from mineru_vl_utils.vlm_client.http_client import _bind_capacity_owner\n"
            "            verify_http_capacity(\n"
            "                self.capacity_config, self.app.state.config.get('max_concurrency')\n"
            "            )\n"
            "            observer = CapacityServingObservation(self.capacity_config, self)\n"
            "            _bind_capacity_owner(self.capacity_config.sha256,\n"
            "                                 self.capacity_config.final_http_limit_per_loop,\n"
            "                                 self.begin_soft_drain)\n"
            "            self.capacity_observer = observer\n",
        ),
        (
            "    async def shutdown(self) -> None:\n"
            "        self.is_shutting_down = True\n"
            "        self._wake_waiters()\n",
            "    def begin_soft_drain(self) -> None:\n"
            "        self.is_shutting_down = True\n"
            "        self._wake_waiters()\n\n"
            "    async def shutdown(self) -> None:\n"
            "        self.begin_soft_drain()\n",
        ),
        (
            "    admission = task_manager.admission_snapshot()\n",
            "    admission = task_manager.admission_snapshot()\n"
            "    capacity_extra = {}\n"
            "    if getattr(task_manager, 'capacity_config', None) is None:\n"
            "        protocol_runtime = task_protocol_runtime_status(\n"
            "            task_manager.task_protocol_v2, task_manager.task_protocol_executor\n"
            "        )\n"
            "    else:\n"
            "        protocol_runtime = task_protocol_runtime_status(\n"
            "            task_manager.task_protocol_v2, task_manager.task_protocol_executor,\n"
            "            capacity_config_sha256=task_manager.capacity_config.sha256,\n"
            "        )\n"
            "        capacity_extra['capacity_observation'] = task_manager.capacity_observer.snapshot()\n",
        ),
        (
            '        "task_protocol_runtime": task_protocol_runtime_status(\n'
            "            task_manager.task_protocol_v2, task_manager.task_protocol_executor\n"
            "        ),\n",
            '        "task_protocol_runtime": protocol_runtime,\n'
            "        **capacity_extra,\n",
        ),
    )
    for number, (old, new) in enumerate(replacements):
        source = _replace_exact(source, old, new, count=1, label=f"explicit capacity wiring {number}")
    return source


_TABLE_IMAGE_CONSERVATION_SOURCE = (
    "def table_image_conservation(original, replaced, token_map):\n"
    "    \"\"\"Report every original in-table image crop that was not restored exactly once.\n"
    "\n"
    "    ``token_map`` is the crop map drawn before inference: each token stands for\n"
    "    exactly one crop. ``original`` is the model output before token replacement\n"
    "    and ``replaced`` the output after it. Occurrences are counted per token with\n"
    "    the same rule ``replace_table_image_tokens`` uses; identical image bytes never\n"
    "    merge two tokens. Kinds: ``missing`` (token absent), ``duplicate`` (token\n"
    "    echoed more than once), ``unrestored`` (token seen once but not placed exactly\n"
    "    once as an image). Nothing is edited or retried.\n"
    "    \"\"\"\n"
    "    if not token_map:\n"
    "        return []\n"
    "    original_text = original or \"\"\n"
    "    replaced_text = replaced or \"\"\n"
    "    occurrences = {}\n"
    "    restored_expected = {}\n"
    "    for token, data_uri in token_map.items():\n"
    "        pattern = r\"\\[\\s*\" + re.escape(token[1:-1]) + r\"\\s*\\]\"\n"
    "        occurrences[token] = len(re.findall(pattern, original_text))\n"
    "        restored_expected[data_uri] = restored_expected.get(data_uri, 0) + occurrences[token]\n"
    "    issues = []\n"
    "    for token in sorted(token_map):\n"
    "        data_uri = token_map[token]\n"
    "        actual = occurrences[token]\n"
    "        pattern = r\"\\[\\s*\" + re.escape(token[1:-1]) + r\"\\s*\\]\"\n"
    "        restored_actual = replaced_text.count(\"<img src=\\\"\" + data_uri + \"\\\"/>\")\n"
    "        if actual == 0:\n"
    "            kind = \"missing\"\n"
    "        elif actual > 1:\n"
    "            kind = \"duplicate\"\n"
    "        elif re.search(pattern, replaced_text) or restored_actual != restored_expected[data_uri]:\n"
    "            kind = \"unrestored\"\n"
    "        else:\n"
    "            continue\n"
    "        prefix = \"data:image/jpeg;base64,\"\n"
    "        if not data_uri.startswith(prefix):\n"
    "            raise RuntimeError(\"table image crop is not a JPEG data URI\")\n"
    "        image = base64.b64decode(data_uri[len(prefix):], validate=True)\n"
    "        issue = {\n"
    "            \"kind\": kind,\n"
    "            \"token\": token,\n"
    "            \"expected\": 1,\n"
    "            \"actual\": actual,\n"
    "            \"image_sha256\": \"sha256:\" + hashlib.sha256(image).hexdigest(),\n"
    "            \"image_byte_count\": len(image),\n"
    "            \"image_data_uri\": data_uri,\n"
    "        }\n"
    "        if kind == \"unrestored\":\n"
    "            issue[\"restored_expected\"] = restored_expected[data_uri]\n"
    "            issue[\"restored_actual\"] = restored_actual\n"
    "        issues.append(issue)\n"
    "    return issues\n"
)


def _patch_table_image_conservation(source: str) -> str:
    """Keep lost, duplicated or unrestored in-table crops visible in model output.

    The model output is never rewritten; the check only records what the
    unchanged replacement could not restore. Idempotent for an already
    patched source.
    """

    if "def table_image_conservation(" in source:
        return source
    source = _replace_exact(
        source,
        "from loguru import logger\n\n",
        "import base64\nimport hashlib\nimport re\n\nfrom loguru import logger\n\n",
        count=1,
        label="table image conservation imports",
    )
    source = _replace_exact(
        source,
        "def simple_process(\n",
        _TABLE_IMAGE_CONSERVATION_SOURCE + "\n\ndef simple_process(\n",
        count=1,
        label="table image conservation helper",
    )
    return _replace_exact(
        source,
        "    for block in blocks:\n"
        "        if block.type == \"table\" and block.content:\n"
        "            content = block.content\n"
        "            try:\n"
        "                content = convert_otsl_to_html(content)\n"
        "            except Exception as e:\n"
        "                logger.warning(\"Failed to convert OTSL to HTML: {}; content: {}\", e, block.content)\n"
        "            content = replace_table_image_tokens(content, block.get(TABLE_IMAGE_TOKEN_MAP_KEY))\n"
        "            block.content = replace_table_formula_delimiters(content, enabled=enable_table_formula_eq_wrap)\n",
        "    for block in blocks:\n"
        "        if block.type == \"table\" and block.content:\n"
        "            content = block.content\n"
        "            try:\n"
        "                content = convert_otsl_to_html(content)\n"
        "            except Exception as e:\n"
        "                logger.warning(\"Failed to convert OTSL to HTML: {}; content: {}\", e, block.content)\n"
        "            table_image_source = content\n"
        "            content = replace_table_image_tokens(content, block.get(TABLE_IMAGE_TOKEN_MAP_KEY))\n"
        "            table_image_issues = table_image_conservation(\n"
        "                table_image_source, content, block.get(TABLE_IMAGE_TOKEN_MAP_KEY)\n"
        "            )\n"
        "            if table_image_issues:\n"
        "                block[\"table_image_unmatched\"] = table_image_issues\n"
        "            block.content = replace_table_formula_delimiters(content, enabled=enable_table_formula_eq_wrap)\n"
        "        elif block.type == \"table\":\n"
        "            # An empty table output with an original crop map has lost every\n"
        "            # crop; keep that evidence instead of accepting the empty table.\n"
        "            table_image_issues = table_image_conservation(\n"
        "                block.content, block.content, block.get(TABLE_IMAGE_TOKEN_MAP_KEY)\n"
        "            )\n"
        "            if table_image_issues:\n"
        "                block[\"table_image_unmatched\"] = table_image_issues\n",
        count=1,
        label="table image conservation integration",
    )



def _patch_service_io_manager(source: str) -> str:
    """Bind the durable registry to an application-owned blocking IO plane."""
    if "async def create_async_parse_task(" not in source:
        return source
    source = _replace_exact(
        source, "import click\nimport uvicorn\n", "import anyio\nimport click\nimport uvicorn\n",
        count=1, label="service IO anyio import",
    )
    source = _replace_exact(
        source,
        "    TaskRegistryPersistenceError,\n    evict_consumed_routes, task_protocol_runtime_status,\n",
        "    TaskRegistryPersistenceError, TaskRegistryObservationBusy, RegistryServiceIO,\n"
        "    evict_consumed_routes, task_protocol_runtime_status,\n",
        count=1, label="service IO registry imports",
    )
    source = _replace_exact(
        source,
        "from mineru.utils.model_utils import strict_processing_window_size, to_thread_owned\n",
        "from mineru.utils.model_utils import drain_owned_awaitable, strict_processing_window_size, to_thread_owned\n",
        count=1, label="service IO drain import",
    )
    task_wait = (
        'class TaskWaitAbortedError(RuntimeError):\n'
        '    """Raised when a synchronous file_parse request cannot keep waiting safely."""\n\n\n'
    )
    helpers = task_wait + (
        'async def _settle_service_operation(awaitable):\n'
        '    with anyio.CancelScope(shield=True):\n'
        '        return await drain_owned_awaitable(awaitable)\n\n\n'
        'async def _registry_view(manager, reader):\n'
        '    try:\n'
        '        return await manager.task_protocol_v2.observe(reader)\n'
        '    except TaskRegistryObservationBusy as exc:\n'
        '        raise HTTPException(status_code=503, detail={"code": "registry_observation_busy"}) from exc\n'
        '    except TaskRegistryPersistenceError as exc:\n'
        '        raise HTTPException(status_code=503, detail={"code": "registry_persistence_unavailable"}) from exc\n\n\n'
    )
    source = _replace_exact(source, task_wait, helpers, count=1, label="service IO helpers")
    owner = (
        '        self.task_protocol_executor = SplitTaskExecutor(\n'
        '            parse_slots=get_max_concurrent_requests(),\n'
        '            finalizer_slots=(1 if self.capacity_config is None\n'
        '                             else self.capacity_config.finalizer_active_limit),\n'
        '            result_reservation_bytes=result_budget,\n'
        '        )\n'
    )
    source = _replace_exact(
        source, owner,
        owner + '        self.service_io = RegistryServiceIO(\n'
        '            drain=_settle_service_operation, max_pending=self.max_nonterminal_tasks + 8\n'
        '        )\n', count=1, label="service IO owner",
    )
    source = _replace_exact(
        source,
        '        self.task_protocol_v2.cleanup_consumed()\n'
        '        for payload in self.task_protocol_v2.recoverable_payloads():\n',
        '        cleaned = await self.service_io.call(\n'
        '            self.task_protocol_v2.cleanup_consumed, lane="bulk", required=True\n'
        '        )\n'
        '        if cleaned:\n'
        '            self.task_protocol_executor.notify_result_capacity_changed()\n'
        '        for payload in await self.service_io.call(\n'
        '            self.task_protocol_v2.recoverable_payloads, lane="bulk", required=True\n'
        '        ):\n', count=1, label="service IO startup",
    )
    begin_start=source.index('    def begin_submission(self, options):\n', source.index('class AsyncTaskManager:'))
    begin_end=source.index('    def finish_submission(', begin_start)
    begin="""    async def begin_submission(self, options):\n        key = options.agent_idempotency_key\n        existing = await self.task_protocol_v2.observe(lambda: self.task_protocol_v2.get(key))\n        allow_create = existing is None\n        if allow_create and self.is_shutting_down:\n            raise HTTPException(status_code=503, detail="Task manager is shutting down")\n        if allow_create:\n            reason = (await self.task_protocol_v2.observe(self.admission_snapshot))["blocked_reason"]\n            if reason not in {None, "capacity_full"}:\n                raise HTTPException(status_code=503, detail=reason)\n        task_id = str(uuid.uuid4())\n        if allow_create:\n            self._ingress_in_flight.add(task_id)\n            self._ingress_drained.clear()\n        try:\n            record, created = await self.service_io.call(\n                self.task_protocol_v2.reconcile_or_create,\n                idempotency_key=key, task_id=task_id,\n                attempt_identity=options.agent_attempt_identity,\n                fence_identity=options.agent_fence_identity,\n                max_nonterminal_tasks=self.max_nonterminal_tasks,\n                allow_create=allow_create, lane="metadata",\n            )\n        except BaseException:\n            if allow_create:\n                self._ingress_in_flight.discard(task_id)\n                if not self._ingress_in_flight:\n                    self._ingress_drained.set()\n            raise\n        if created and record.task_id != task_id:\n            raise RuntimeError("created task identity drifted")\n        if not created and allow_create:\n            self._ingress_in_flight.discard(task_id)\n            if not self._ingress_in_flight:\n                self._ingress_drained.set()\n        return record, created\n\n"""
    source=source[:begin_start]+begin+source[begin_end:]
    source = _replace_exact(
        source, 'record, created = task_manager.begin_submission(request_options)',
        'record, created = await task_manager.begin_submission(request_options)',
        count=1, label="service IO admission caller",
    )
    source = _replace_exact(
        source,
        '            if (task.status == TASK_PENDING\n'
        '                    and task.task_id not in self._scheduled_task_ids\n'
        '                    and task.task_id not in self._stopped_pending_task_ids):\n',
        '            if (task.status == TASK_PENDING\n'
        '                    and task.task_id not in self._scheduled_task_ids\n'
        '                    and task.task_id not in self._stopped_pending_task_ids\n'
        '                    and task.task_id not in self._ingress_in_flight):\n',
        count=1, label="service IO ingress queue fence",
    )
    source = _replace_exact(
        source,
        '                parse=parse_stage, finalize=finalizer_stage,\n            )\n',
        '                parse=parse_stage, finalize=finalizer_stage, registry_io=self.service_io,\n            )\n',
        count=1, label="service IO executor bridge",
    )
    source = _replace_exact(
        source, '                self.cleanup_expired_tasks()\n',
        '                await self.cleanup_expired_tasks()\n',
        count=1, label="service IO periodic cleanup await",
    )
    cleanup_start=source.index('    def cleanup_expired_tasks(self) -> int:\n', source.index('class AsyncTaskManager:'))
    cleanup_end=source.index('    def _is_task_expired(', cleanup_start)
    cleanup="""    async def cleanup_expired_tasks(self) -> int:\n        cleaned = await self.service_io.call(\n            self.task_protocol_v2.cleanup_consumed, lane="bulk", required=True\n        )\n        if cleaned:\n            self.task_protocol_executor.notify_result_capacity_changed()\n        self._evict_consumed_protocol_tasks()\n        return cleaned\n\n"""
    source=source[:cleanup_start]+cleanup+source[cleanup_end:]
    source = _replace_exact(
        source,
        '        self.active_tasks.clear()\n        self.task_protocol_v2.cleanup_consumed()\n',
        '        self.active_tasks.clear()\n'
        '        cleaned = await self.service_io.call(\n'
        '            self.task_protocol_v2.cleanup_consumed, lane="bulk", required=True\n'
        '        )\n'
        '        if cleaned:\n'
        '            self.task_protocol_executor.notify_result_capacity_changed()\n',
        count=1, label="service IO shutdown cleanup",
    )
    return source


def _patch_service_io_ack_health(source: str) -> str:
    if "async def ack_async_task_result(" not in source:
        return source
    start = source.index("async def ack_async_task_result(task_id: str):\n")
    end = source.index("\n\n@app.get(path=\"/tasks/{task_id}/result\"", start)
    ack = (
        "async def ack_async_task_result(task_id: str):\n"
        "    task_manager = get_task_manager()\n"
        "    record = await _registry_view(task_manager, lambda: task_manager.task_protocol_v2.get_by_task_id(task_id))\n"
        "    if record is None:\n"
        "        raise HTTPException(status_code=404, detail=\"Task not found\")\n"
        "    key = record.idempotency_key\n"
        "    try:\n"
        "        await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.acknowledge_terminal_intent, key, lane=\"metadata\"\n"
        "        )\n"
        "        await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.cleanup_consumed,\n"
        "            idempotency_key=key, lane=\"bulk\", required=True,\n"
        "        )\n"
        "        def confirm():\n"
        "            current = task_manager.task_protocol_v2.get(key)\n"
        "            if current is None or current.task_id != task_id or current.state != \"consumed\":\n"
        "                raise TaskProtocolConflict(\"ACK cleanup did not reach consumed\")\n"
        "            task_manager._evict_consumed_protocol_tasks()\n"
        "            return {\"schema\": \"mineru-task-protocol.v2\", \"task_id\": task_id, \"status\": \"consumed\"}\n"
        "        result = await _registry_view(task_manager, confirm)\n"
        "        task_manager.task_protocol_executor.notify_result_capacity_changed()\n"
        "        return result\n"
        "    except TaskRegistryPersistenceError as exc:\n"
        "        raise HTTPException(status_code=503, detail=str(exc)) from exc\n"
        "    except TaskProtocolConflict as exc:\n"
        "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
    )
    source = source[:start] + ack + source[end:]

    status_start = source.index("async def get_async_task_status(task_id: str, request: Request):\n")
    status_end = source.index("\n\n@app.get(path=\"/tasks/by-idempotency/", status_start)
    status = (
        "async def get_async_task_status(task_id: str, request: Request):\n"
        "    task_manager = get_task_manager()\n"
        "    def view():\n"
        "        task = task_manager.get(task_id)\n"
        "        return None if task is None else task_manager.build_status_payload(task, request)\n"
        "    payload = await _registry_view(task_manager, view)\n"
        "    if payload is None:\n"
        "        raise HTTPException(status_code=404, detail=\"Task not found\")\n"
        "    return payload\n"
    )
    source = source[:status_start] + status + source[status_end:]

    reconcile_start = source.index("async def reconcile_async_task(idempotency_key: str, request: Request):\n")
    reconcile_end = source.index("\n\n@app.post(path=\"/tasks/{task_id}/lease\"", reconcile_start)
    reconcile = (
        "async def reconcile_async_task(idempotency_key: str, request: Request):\n"
        "    task_manager = get_task_manager()\n"
        "    def view():\n"
        "        record = task_manager.task_protocol_v2.get(idempotency_key)\n"
        "        task = None if record is None or record.state == \"consumed\" else task_manager.reconcile_submission(record)\n"
        "        return None if task is None else task_manager.build_status_payload(task, request)\n"
        "    payload = await _registry_view(task_manager, view)\n"
        "    if payload is None:\n"
        "        raise HTTPException(status_code=404, detail=\"Task not found\")\n"
        "    return payload\n"
    )
    source = source[:reconcile_start] + reconcile + source[reconcile_end:]

    lease_start = source.index("async def lease_async_task_result(task_id: str, seconds: int = 300):\n")
    lease_end = source.index("\n\n@app.post(path=\"/tasks/{task_id}/ack\"", lease_start)
    lease = (
        "async def lease_async_task_result(task_id: str, seconds: int = 300):\n"
        "    task_manager = get_task_manager()\n"
        "    if not 1 <= seconds <= 3600:\n"
        "        raise HTTPException(status_code=400, detail=\"Task protocol lease is invalid\")\n"
        "    record = await _registry_view(task_manager, lambda: task_manager.task_protocol_v2.get_by_task_id(task_id))\n"
        "    if record is None:\n"
        "        raise HTTPException(status_code=404, detail=\"Task not found\")\n"
        "    try:\n"
        "        lease_until = await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.lease, record.idempotency_key,\n"
        "            seconds=seconds, lane=\"metadata\"\n"
        "        )\n"
        "    except TaskRegistryPersistenceError as exc:\n"
        "        raise HTTPException(status_code=503, detail=str(exc)) from exc\n"
        "    except TaskProtocolConflict as exc:\n"
        "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
        "    return {\"schema\": \"mineru-task-protocol.v2\", \"task_id\": task_id, \"lease_until_unix\": lease_until}\n"
    )
    source = source[:lease_start] + lease + source[lease_end:]

    old = (
        "    stats = task_manager.get_stats()\n"
        "    admission = task_manager.admission_snapshot()\n"
        "    capacity_extra = {}\n"
        "    if getattr(task_manager, 'capacity_config', None) is None:\n"
        "        protocol_runtime = task_protocol_runtime_status(\n"
        "            task_manager.task_protocol_v2, task_manager.task_protocol_executor\n"
        "        )\n"
        "    else:\n"
        "        protocol_runtime = task_protocol_runtime_status(\n"
        "            task_manager.task_protocol_v2, task_manager.task_protocol_executor,\n"
        "            capacity_config_sha256=task_manager.capacity_config.sha256,\n"
        "        )\n"
        "        capacity_extra['capacity_observation'] = task_manager.capacity_observer.snapshot()\n"
    )
    new = (
        "    def view():\n"
        "        if not task_manager.is_healthy():\n"
        "            raise RuntimeError(\"task manager became unhealthy during health observation\")\n"
        "        stats = task_manager.get_stats()\n"
        "        admission = task_manager.admission_snapshot()\n"
        "        if getattr(task_manager, 'capacity_config', None) is None:\n"
        "            protocol_runtime = task_protocol_runtime_status(\n"
        "                task_manager.task_protocol_v2, task_manager.task_protocol_executor\n"
        "            )\n"
        "            capacity_extra = {}\n"
        "        else:\n"
        "            protocol_runtime = task_protocol_runtime_status(\n"
        "                task_manager.task_protocol_v2, task_manager.task_protocol_executor,\n"
        "                capacity_config_sha256=task_manager.capacity_config.sha256,\n"
        "            )\n"
        "            capacity_extra = {'capacity_observation': task_manager.capacity_observer.snapshot()}\n"
        "        return stats, admission, protocol_runtime, capacity_extra\n"
        "    try:\n"
        "        stats, admission, protocol_runtime, capacity_extra = await _registry_view(task_manager, view)\n"
        "    except HTTPException as exc:\n"
        "        code = exc.detail.get(\"code\", \"registry_observation_failed\") if isinstance(exc.detail, dict) else \"registry_observation_failed\"\n"
        "        return JSONResponse(status_code=503, content={\"status\": \"unhealthy\", \"version\": __version__, \"error\": code})\n"
        "    except RuntimeError as exc:\n"
        "        return JSONResponse(status_code=503, content={\"status\": \"unhealthy\", \"version\": __version__, \"error\": str(exc)[:256]})\n"
    )
    source = _replace_exact(source, old, new, count=1, label="service IO fresh health")

    temp_response_class = (
        "class TemporaryFileResponse(FileResponse):\n"
        "    async def __call__(self, scope, receive, send):\n"
        "        primary = None\n"
        "        if scope.get(\"type\") == \"http\" and \"http.response.pathsend\" in scope.get(\"extensions\", {}):\n"
        "            scope = dict(scope)\n"
        "            extensions = dict(scope.get(\"extensions\", {}))\n"
        "            extensions.pop(\"http.response.pathsend\", None)\n"
        "            scope[\"extensions\"] = extensions\n"
        "        try:\n"
        "            await super().__call__(scope, receive, send)\n"
        "        except BaseException as exc:\n"
        "            primary = exc\n"
        "        cleanup_error = None\n"
        "        try:\n"
        "            await _settle_service_operation(to_thread_owned(cleanup_file, self.path))\n"
        "        except BaseException as exc:\n"
        "            cleanup_error = exc\n"
        "        if primary is not None:\n"
        "            if cleanup_error is not None:\n"
        "                primary.add_note(\"temporary result cleanup failed: \" + type(cleanup_error).__name__)\n"
        "                raise primary from cleanup_error\n"
        "            raise primary\n"
        "        if cleanup_error is not None:\n"
        "            raise cleanup_error\n"
    )
    response_class = (
        "class OwnedFileResponse(FileResponse):\n"
        "    def __init__(self, *, manager, idempotency_key, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
        "        self._owner_manager = manager\n"
        "        self._owner_key = idempotency_key\n"
        "    async def __call__(self, scope, receive, send):\n"
        "        primary = None\n"
        "        if scope.get(\"type\") == \"http\" and \"http.response.pathsend\" in scope.get(\"extensions\", {}):\n"
        "            scope = dict(scope)\n"
        "            extensions = dict(scope.get(\"extensions\", {}))\n"
        "            extensions.pop(\"http.response.pathsend\", None)\n"
        "            scope[\"extensions\"] = extensions\n"
        "        try:\n"
        "            await super().__call__(scope, receive, send)\n"
        "        except BaseException as exc:\n"
        "            primary = exc\n"
        "        cleanup_error = None\n"
        "        try:\n"
        "            await _settle_service_operation(self._owner_manager.service_io.call(\n"
        "                self._owner_manager.task_protocol_v2.release_result, self._owner_key,\n"
        "                lane=\"metadata\", required=True,\n"
        "            ))\n"
        "        except BaseException as exc:\n"
        "            cleanup_error = exc\n"
        "        if primary is not None:\n"
        "            if cleanup_error is not None:\n"
        "                primary.add_note(\"result reader release failed: \" + type(cleanup_error).__name__)\n"
        "                raise primary from cleanup_error\n"
        "            raise primary\n"
        "        if cleanup_error is not None:\n"
        "            raise cleanup_error\n"
    )
    source = _replace_exact(
        source, "\n@asynccontextmanager\nasync def lifespan(app: FastAPI):\n",
        "\n" + temp_response_class + "\n\n" + response_class + "\n\n@asynccontextmanager\nasync def lifespan(app: FastAPI):\n",
        count=1, label="service IO owned file response",
    )

    result_start = source.index("async def get_async_task_result(\n")
    result_end = source.index("\n\n@app.get(path=\"/agent/telemetry/http-requests/v1\"", result_start)
    result_route = (
        "async def get_async_task_result(\n"
        "    task_id: str,\n"
        "    request: Request,\n"
        "    background_tasks: BackgroundTasks,\n"
        "):\n"
        "    del background_tasks\n"
        "    task_manager = get_task_manager()\n"
        "    task = await _registry_view(task_manager, lambda: task_manager.get(task_id))\n"
        "    if task is None:\n"
        "        raise HTTPException(status_code=404, detail=\"Task not found\")\n"
        "    if task.status in (TASK_PENDING, TASK_PROCESSING):\n"
        "        return JSONResponse(status_code=202, content={**task.to_status_payload(request), \"message\": \"Task result is not ready yet\"})\n"
        "    if task.status == TASK_FAILED:\n"
        "        return JSONResponse(status_code=409, content={**task.to_status_payload(request), \"message\": \"Task execution failed\"})\n"
        "    if not task.result_artifact_path or not task.result_artifact_sha256 or not task.result_artifact_owner:\n"
        "        raise HTTPException(status_code=410, detail=\"Retained task result is unavailable\")\n"
        "    if not task.agent_idempotency_key:\n"
        "        raise HTTPException(status_code=410, detail=\"Task protocol result owner is absent\")\n"
        "    key = task.agent_idempotency_key\n"
        "    try:\n"
        "        result_path = await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.acquire_result, key, lane=\"metadata\",\n"
        "            on_cancel_result=lambda _path: task_manager.task_protocol_v2.release_result(key),\n"
        "        )\n"
        "    except TaskRegistryPersistenceError as exc:\n"
        "        raise HTTPException(status_code=503, detail=str(exc)) from exc\n"
        "    except TaskProtocolConflict as exc:\n"
        "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
        "    return OwnedFileResponse(\n"
        "        manager=task_manager, idempotency_key=key, path=str(result_path),\n"
        "        media_type=\"application/zip\", filename=f\"{task.task_id}.zip\", status_code=200,\n"
        "        headers={\"X-MinerU-Result-SHA256\": task.result_artifact_sha256, \"X-MinerU-Result-Owner\": task.result_artifact_owner},\n"
        "    )\n"
    )
    source = source[:result_start] + result_route + source[result_end:]

    retained_marker = "async def build_retained_task_result(task: AsyncParseTask, *, byte_budget: int) -> None:\n"
    retained_helper = (
        "def _commit_retained_result(part_path: str, final_path: str, output_dir: str) -> None:\n"
        "    os.replace(part_path, final_path)\n"
        "    directory_fd = os.open(output_dir, os.O_RDONLY)\n"
        "    try:\n"
        "        os.fsync(directory_fd)\n"
        "    finally:\n"
        "        os.close(directory_fd)\n\n\n"
    )
    source = _replace_exact(
        source, retained_marker, retained_helper + retained_marker,
        count=1, label="service IO retained result commit helper",
    )
    source = _replace_exact(
        source,
        "        os.replace(retained_part, retained)\n"
        "        directory_fd = os.open(task.output_dir, os.O_RDONLY)\n"
        "        try:\n"
        "            os.fsync(directory_fd)\n"
        "        finally:\n"
        "            os.close(directory_fd)\n",
        "        await to_thread_owned(_commit_retained_result, retained_part, retained, task.output_dir)\n",
        count=1, label="service IO retained result commit",
    )
    source = _replace_exact(
        source, "        cleanup_file(retained)\n        raise\n",
        "        await to_thread_owned(cleanup_file, retained)\n        raise\n",
        count=1, label="service IO retained failure cleanup",
    )
    source = _replace_exact(
        source, "        cleanup_file(retained_part)\n        if close_failure is not None:\n",
        "        await to_thread_owned(cleanup_file, retained_part)\n        if close_failure is not None:\n",
        count=1, label="service IO retained part cleanup",
    )

    # Temporary ZIP creation is owned and its response cleans the copy in finally.
    zip_old = (
        "        zip_task = asyncio.create_task(\n"
        "            asyncio.to_thread(\n"
        "                create_result_zip,\n"
    )
    zip_new = (
        "        zip_task = asyncio.create_task(\n"
        "            to_thread_owned(\n"
        "                create_result_zip,\n"
    )
    source = _replace_exact(source, zip_old, zip_new, count=1, label="service IO temporary ZIP creation")
    source = _replace_exact(
        source,
        "        background_tasks.add_task(cleanup_file, zip_path)\n"
        "        return FileResponse(\n",
        "        return TemporaryFileResponse(\n",
        count=1, label="service IO temporary ZIP response cleanup",
    )

    parse_start = source.index("async def parse_pdf(\n")
    parse_end = source.index("\n\n@app.post(\n    path=\"/tasks\"", parse_start)
    parse_block = source[parse_start:parse_end]
    old_return = (
        "    return await build_sync_file_parse_response(\n"
        "        background_tasks=background_tasks,\n"
        "        task=task,\n"
        "        request=http_request,\n"
        "    )\n"
    )
    new_return = (
        "    if not task.agent_idempotency_key:\n"
        "        raise HTTPException(status_code=410, detail=\"Task protocol result owner is absent\")\n"
        "    key = task.agent_idempotency_key\n"
        "    try:\n"
        "        await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.acquire_inline_result, key, lane=\"metadata\",\n"
        "            on_cancel_result=lambda _path: task_manager.task_protocol_v2.release_result(key),\n"
        "        )\n"
        "        try:\n"
        "            return await build_sync_file_parse_response(\n"
        "                background_tasks=background_tasks, task=task, request=http_request,\n"
        "            )\n"
        "        finally:\n"
        "            await _settle_service_operation(task_manager.service_io.call(\n"
        "                task_manager.task_protocol_v2.release_result, key, lane=\"metadata\", required=True,\n"
        "            ))\n"
        "    except TaskRegistryPersistenceError as exc:\n"
        "        raise HTTPException(status_code=503, detail=str(exc)) from exc\n"
        "    except TaskProtocolConflict as exc:\n"
        "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
    )
    if parse_block.count(old_return) != 1:
        raise AssertionError("sync file_parse return drift")
    parse_block = parse_block.replace(old_return, new_return, 1)
    source = source[:parse_start] + parse_block + source[parse_end:]
    return source




def _patch_service_io_ingress(source: str) -> str:
    if "async def create_async_parse_task(" not in source:
        return source

    # One local helper owns directory creation and initial namespace binding.
    source = _replace_exact(
        source,
        "def create_task_output_dir(task_id: str) -> str:\n",
        "def _prepare_ingress_tree(task_manager, task_id: str, key: str) -> tuple[str, str]:\n"
        "    task_output_dir = create_task_output_dir(task_id)\n"
        "    uploads_dir = os.path.join(task_output_dir, \"uploads\")\n"
        "    try:\n"
        "        os.mkdir(uploads_dir)\n"
        "        task_manager.task_protocol_v2.bind_ingress_root(key)\n"
        "        return task_output_dir, uploads_dir\n"
        "    except BaseException:\n"
        "        cleanup_file(task_output_dir)\n"
        "        raise\n\n\n"
        "def create_task_output_dir(task_id: str) -> str:\n",
        count=1, label="service IO ingress tree helper",
    )

    source = _replace_exact(
        source,
        "async def save_upload_files(upload_dir: str, files: list[UploadFile]) -> list[StoredUpload]:\n"
        "    os.makedirs(upload_dir, exist_ok=True)\n",
        "def _write_upload_chunk(destination: Path, chunk: bytes, first: bool) -> None:\n"
        "    mode = \"xb\" if first else \"ab\"\n"
        "    with open(destination, mode) as handle:\n"
        "        handle.write(chunk)\n\n\n"
        "async def save_upload_files(upload_dir: str, files: list[UploadFile], service_io=None) -> list[StoredUpload]:\n"
        "    if service_io is None:\n"
        "        os.makedirs(upload_dir, exist_ok=True)\n"
        "    else:\n"
        "        await service_io.call(os.makedirs, upload_dir, exist_ok=True, lane=\"bulk\", required=True)\n",
        count=1, label="service IO upload helper",
    )
    source = _replace_exact(
        source,
        "        destination = build_upload_destination(upload_dir, filename)\n",
        "        destination = (build_upload_destination(upload_dir, filename) if service_io is None\n"
        "                       else await service_io.call(build_upload_destination, upload_dir, filename, lane=\"bulk\", required=True))\n",
        count=1, label="service IO upload destination",
    )
    source = _replace_exact(
        source,
        "        try:\n"
        "            with open(destination, \"wb\") as handle:\n"
        "                while True:\n"
        "                    chunk = await upload.read(1 << 20)\n"
        "                    if not chunk:\n"
        "                        break\n"
        "                    handle.write(chunk)\n\n"
        "            file_suffix = guess_suffix_by_path(destination)\n",
        "        first_chunk = True\n"
        "        try:\n"
        "            while True:\n"
        "                chunk = await upload.read(1 << 20)\n"
        "                if not chunk:\n"
        "                    break\n"
        "                if service_io is None:\n"
        "                    _write_upload_chunk(destination, chunk, first_chunk)\n"
        "                else:\n"
        "                    await service_io.call(_write_upload_chunk, destination, chunk, first_chunk, lane=\"bulk\", required=True)\n"
        "                first_chunk = False\n\n"
        "            file_suffix = guess_suffix_by_path(destination)\n",
        count=1, label="service IO upload writes",
    )
    source = _replace_exact(
        source,
        "            if file_suffix not in SUPPORTED_UPLOAD_SUFFIXES:\n"
        "                cleanup_file(str(destination))\n",
        "            if file_suffix not in SUPPORTED_UPLOAD_SUFFIXES:\n"
        "                if service_io is None:\n"
        "                    cleanup_file(str(destination))\n"
        "                else:\n"
        "                    await service_io.call(cleanup_file, str(destination), lane=\"bulk\", required=True)\n",
        count=1, label="service IO upload suffix cleanup",
    )
    source = _replace_exact(
        source,
        "        except Exception:\n"
        "            cleanup_file(str(destination))\n"
        "            raise\n",
        "        except BaseException:\n"
        "            if service_io is None:\n"
        "                cleanup_file(str(destination))\n"
        "            else:\n"
        "                await _settle_service_operation(service_io.call(cleanup_file, str(destination), lane=\"bulk\", required=True))\n"
        "            raise\n",
        count=1, label="service IO upload failure cleanup",
    )

    # Loop owns route adoption; durable pending is never routeless.
    finish_start = source.index("    def finish_submission(self, task_id: str) -> None:\n", source.index("class AsyncTaskManager:"))
    finish_end = source.index("    def reconcile_submission(", finish_start)
    ingress_helpers = """    def _adopt_ingress_task(self, task: AsyncParseTask) -> None:\n        if task.task_id not in self.tasks:\n            task.submit_order = self._next_submit_order\n            self._next_submit_order += 1\n            self.tasks[task.task_id] = task\n            self.task_events[task.task_id] = asyncio.Event()\n\n    def _discard_ingress_task(self, task_id: str) -> None:\n        if task_id in self._scheduled_task_ids:\n            raise RuntimeError(\"scheduled task cannot be discarded as ingress\")\n        self.tasks.pop(task_id, None)\n        self.task_events.pop(task_id, None)\n        self.task_wait_failures.pop(task_id, None)\n\n    def finish_submission(self, task_id: str) -> None:\n        self._ingress_in_flight.discard(task_id)\n        if not self._ingress_in_flight:\n            self._ingress_drained.set()\n        self._refill_pending_queue()\n\n"""
    source = source[:finish_start] + ingress_helpers + source[finish_end:]

    submit_start = source.index("    async def submit(self, task: AsyncParseTask) -> None:\n", source.index("class AsyncTaskManager:"))
    submit_end = source.index("    def get(self, task_id:", submit_start)
    submit = """    async def submit(self, task: AsyncParseTask) -> None:\n        record = await self.task_protocol_v2.observe(lambda: self.task_protocol_v2.get(task.agent_idempotency_key))\n        if record is None or record.task_id != task.task_id or record.task_payload is None:\n            raise TaskProtocolConflict(\"Task was not durably accepted\")\n        self._adopt_ingress_task(task)\n        self._refill_pending_queue()\n\n"""
    source = source[:submit_start] + submit + source[submit_end:]

    source = _replace_exact(
        source,
        "        task_output_dir = create_task_output_dir(task_id)\n"
        "        uploads_dir = os.path.join(task_output_dir, \"uploads\")\n"
        "        os.mkdir(uploads_dir)\n"
        "        task_manager.task_protocol_v2.bind_ingress_root(record.idempotency_key)\n"
        "        uploads = await save_upload_files(uploads_dir, request_options.files)\n",
        "        task_output_dir, uploads_dir = await task_manager.service_io.call(\n"
        "            _prepare_ingress_tree, task_manager, task_id, record.idempotency_key, lane=\"bulk\", required=True\n"
        "        )\n"
        "        uploads = await save_upload_files(uploads_dir, request_options.files, task_manager.service_io)\n",
        count=1, label="service IO ingress prepare",
    )
    source = _replace_exact(
        source,
        "        task_manager.task_protocol_v2.bind_task_payload(task.agent_idempotency_key, asdict(task))\n"
        "        await task_manager.submit(task)\n",
        "        task_manager._adopt_ingress_task(task)\n"
        "        await task_manager.service_io.call(\n"
        "            task_manager.task_protocol_v2.bind_task_payload, task.agent_idempotency_key, asdict(task), lane=\"bulk\", required=True\n"
        "        )\n"
        "        await task_manager.submit(task)\n",
        count=1, label="service IO payload bind",
    )
    source = _replace_exact(
        source,
        "            current = task_manager.task_protocol_v2.get(record.idempotency_key)\n"
        "            if current is not None and current.state in {\"ingress\", \"ingress_cleanup\"}:\n"
        "                task_manager.task_protocol_v2.abort_ingress(record.idempotency_key)\n",
        "            current = await task_manager.task_protocol_v2.observe(lambda: task_manager.task_protocol_v2.get(record.idempotency_key))\n"
        "            if current is not None and current.state in {\"ingress\", \"ingress_cleanup\"}:\n"
        "                await _settle_service_operation(task_manager.service_io.call(\n"
        "                    task_manager.task_protocol_v2.abort_ingress, record.idempotency_key, lane=\"bulk\", required=True\n"
        "                ))\n"
        "                task_manager._discard_ingress_task(task_id)\n"
        "            elif task is not None and task_id not in task_manager._scheduled_task_ids:\n"
        "                task_manager._discard_ingress_task(task_id)\n",
        count=1, label="service IO ingress abort",
    )
    source = _replace_exact(
        source, "task = await create_async_parse_task(request_options)",
        "task = await _settle_service_operation(create_async_parse_task(request_options))",
        count=2, label="service IO request-owned ingress",
    )
    return source




def _patch_service_io_shutdown(source: str) -> str:
    if "async def shutdown_app_state(app: FastAPI)" not in source:
        return source
    source = _replace_exact(
        source,
        '        status = self.task_protocol_v2.admission_status(set(self.tasks))\n',
        '        status = await self.task_protocol_v2.observe(\n'
        '            lambda: self.task_protocol_v2.admission_status(set(self.tasks))\n'
        '        )\n',
        count=1, label="service IO shutdown fresh status",
    )
    old = (
        'async def shutdown_app_state(app: FastAPI) -> None:\n'
        '    current_task_manager = getattr(app.state, "task_manager", None)\n'
        '    if current_task_manager is not None:\n'
        '        await current_task_manager.shutdown()\n'
        '    app.state.task_manager = None\n'
        '    shutdown_runtime_resources()\n'
    )
    new = (
        'async def shutdown_app_state(app: FastAPI) -> None:\n'
        '    current_task_manager = getattr(app.state, "task_manager", None)\n'
        '    primary_error = None\n'
        '    close_error = None\n'
        '    if current_task_manager is not None:\n'
        '        try:\n'
        '            await current_task_manager.shutdown()\n'
        '        except BaseException as exc:\n'
        '            primary_error = exc\n'
        '        try:\n'
        '            await _settle_service_operation(current_task_manager.service_io.close())\n'
        '        except BaseException as exc:\n'
        '            close_error = exc\n'
        '    app.state.task_manager = None\n'
        '    shutdown_runtime_resources()\n'
        '    if primary_error is not None:\n'
        '        if close_error is not None:\n'
        '            primary_error.add_note("service IO close failed: " + type(close_error).__name__)\n'
        '            raise primary_error from close_error\n'
        '        raise primary_error\n'
        '    if close_error is not None:\n'
        '        raise close_error\n'
    )
    return _replace_exact(source, old, new, count=1, label="service IO lifespan close")




def _patch_service_io_pressure(source: str) -> str:
    if "async def agent_process_pressure_telemetry():" not in source:
        return source
    old = (
        "async def agent_process_pressure_telemetry():\n"
        "    manager = get_task_manager()\n"
        "    observer = getattr(manager, 'capacity_observer', None)\n"
        "    if observer is None:\n"
        "        raise HTTPException(status_code=503, detail='explicit capacity pressure unavailable')\n"
        "    return JSONResponse(content=observer.pressure_snapshot(),\n"
        "                        headers={\"Cache-Control\": \"no-store\"})\n"
    )
    new = (
        "async def agent_process_pressure_telemetry():\n"
        "    manager = get_task_manager()\n"
        "    observer = getattr(manager, 'capacity_observer', None)\n"
        "    if observer is None:\n"
        "        raise HTTPException(status_code=503, detail='explicit capacity pressure unavailable')\n"
        "    try:\n"
        "        started, serving = observer.pressure_begin()\n"
        "        memory = await manager.service_io.call(observer.pressure_kernel_memory, lane=\"observe\")\n"
        "        payload = observer.pressure_finish(started, serving, memory)\n"
        "    except TaskRegistryObservationBusy as exc:\n"
        "        raise HTTPException(status_code=503, detail={\"code\": \"pressure_io_busy\"}) from exc\n"
        "    except RuntimeError as exc:\n"
        "        raise HTTPException(status_code=503, detail={\"code\": \"pressure_observation_invalid\", \"reason\": str(exc)[:256]}) from exc\n"
        "    return JSONResponse(content=payload, headers={\"Cache-Control\": \"no-store\"})\n"
    )
    return _replace_exact(source, old, new, count=1, label="service IO pressure kernel split")




def _patch_service_scope_completion(source: str) -> str:
    """Complete the request/response ownership boundary of the exact API overlay."""
    if "async def create_async_parse_task(" not in source:
        return source
    import ast
    replacements = {'build_retained_task_result': "async def build_retained_task_result(task: AsyncParseTask, *, byte_budget: int, service_io=None) -> None:\n    try:\n        if service_io is None:\n            result = await to_thread_owned(_build_retained_artifact_owned, task, byte_budget)\n        else:\n            result = await service_io.call(_build_retained_artifact_owned, task, byte_budget, lane='bulk', required=True)\n    except BaseException as primary:\n        # A cancelled caller can own a successful but unreturned ZIP. The worker\n        # has settled at this point; compensate it before publishing any task result.\n        try:\n            retained = os.path.join(task.output_dir, '.retained-result.zip')\n            if service_io is None:\n                await to_thread_owned(cleanup_file, retained)\n            else:\n                await service_io.call(cleanup_file, retained, lane='bulk', required=True)\n        except BaseException as cleanup_error:\n            primary.add_note('unreturned retained result cleanup failed: ' + repr(cleanup_error))\n            raise primary from cleanup_error\n        raise\n    retained, digest, size = result\n    task.result_artifact_path = retained\n    task.result_artifact_sha256 = digest\n    task.result_artifact_bytes = size\n    task.result_artifact_owner = hashlib.sha256(f'{task.task_id}\\0{digest}\\0{size}'.encode()).hexdigest()\n", 'build_result_response': "async def build_result_response(\n    background_tasks: BackgroundTasks, status_code: int, output_dir: str, pdf_file_names: list[str],\n    backend: str, parse_method: str, return_md: bool, return_middle_json: bool,\n    return_model_output: bool, return_content_list: bool, return_images: bool,\n    response_format_zip: bool, return_original_file: bool, zip_filename: str = 'results.zip',\n) -> Response:\n    resources = _request_resources()\n    io = resources.owner\n    parameters = dict(output_dir=output_dir, pdf_file_names=pdf_file_names, backend=backend,\n        parse_method=parse_method, return_md=return_md, return_middle_json=return_middle_json,\n        return_model_output=return_model_output, return_content_list=return_content_list,\n        return_images=return_images)\n    if response_format_zip:\n        path = await io.call(create_result_zip, **parameters, return_original_file=return_original_file,\n                             lane='bulk', required=True, on_cancel_result=cleanup_file)\n        resources.defer(cleanup_file, path, lane='bulk')\n        return FileResponse(path=path, media_type='application/zip', filename=zip_filename, status_code=status_code)\n    result = await io.call(build_result_dict, **parameters, lane='bulk', required=True)\n    return JSONResponse(status_code=status_code, content={'backend':backend, 'version':__version__, 'results':result})\n", 'get_async_task_result': "async def get_async_task_result(task_id: str, request: Request, background_tasks: BackgroundTasks):\n    manager = get_task_manager()\n    task = await _registry_view(manager, lambda: manager.get(task_id))\n    if task is None:\n        raise HTTPException(status_code=404, detail='Task not found')\n    if task.status in (TASK_PENDING, TASK_PROCESSING):\n        return JSONResponse(status_code=202, content={**task.to_status_payload(request), 'message':'Task result is not ready yet'})\n    if task.status == TASK_FAILED:\n        return JSONResponse(status_code=409, content={**task.to_status_payload(request), 'message':'Task execution failed'})\n    if not task.agent_idempotency_key or not task.result_artifact_sha256 or not task.result_artifact_owner:\n        raise HTTPException(status_code=410, detail='Task protocol result owner is absent')\n    try:\n        if not task.result_artifact_path or not await manager.service_io.call(os.path.isfile, task.result_artifact_path, lane='bulk'):\n            raise HTTPException(status_code=410, detail='Retained task result is unavailable')\n        path = await _pin_response_result(manager, task.agent_idempotency_key)\n        return FileResponse(path=str(path), media_type='application/zip', filename=f'{task.task_id}.zip',\n            headers={'X-MinerU-Result-SHA256':task.result_artifact_sha256, 'X-MinerU-Result-Owner':task.result_artifact_owner})\n    except TaskRegistryObservationBusy as exc:\n        raise HTTPException(status_code=503, detail={'code':'result_reader_busy'}) from exc\n    except TaskRegistryPersistenceError as exc:\n        raise HTTPException(status_code=503, detail=str(exc)) from exc\n    except TaskProtocolConflict as exc:\n        raise HTTPException(status_code=409, detail=str(exc)) from exc\n", 'ack_async_task_result': "async def ack_async_task_result(task_id: str):\n    manager = get_task_manager()\n    async def owned():\n        record = await _registry_view(manager, lambda: manager.task_protocol_v2.get_by_task_id(task_id))\n        if record is None:\n            raise HTTPException(status_code=404, detail='Task not found')\n        key = record.idempotency_key\n        try:\n            await manager.service_io.call(manager.task_protocol_v2.acknowledge_terminal_intent, key)\n            await manager.service_io.call(manager.task_protocol_v2.cleanup_consumed,\n                idempotency_key=key, lane='bulk', required=True)\n            def confirm():\n                actual = manager.task_protocol_v2.get(key)\n                if actual is None or actual.task_id != task_id or actual.state != 'consumed':\n                    raise TaskProtocolConflict('ACK cleanup did not reach consumed')\n                manager._evict_consumed_protocol_tasks()\n                return {'schema':'mineru-task-protocol.v2','task_id':task_id,'status':'consumed'}\n            result = await _registry_view(manager, confirm)\n            manager.task_protocol_executor.notify_result_capacity_changed()\n            return result\n        except TaskRegistryObservationBusy as exc:\n            raise HTTPException(status_code=503, detail={'code':'registry_io_busy'}) from exc\n        except TaskRegistryPersistenceError as exc:\n            raise HTTPException(status_code=503, detail=str(exc)) from exc\n        except TaskProtocolConflict as exc:\n            raise HTTPException(status_code=409, detail=str(exc)) from exc\n    return await _settle_service_operation(owned())\n", 'shutdown_app_state': "async def shutdown_app_state(app: FastAPI) -> None:\n    manager = getattr(app.state, 'task_manager', None)\n    primary = None\n    cleanup_error = None\n    if manager is not None:\n        try:\n            await _settle_service_operation(manager.shutdown())\n        except BaseException as exc:\n            primary = exc\n        try:\n            # Do not tear down the executor under a still-owned parser/cleanup task.\n            for task in (manager.dispatcher_task, manager.cleanup_task):\n                if task is not None and not task.done():\n                    task.cancel()\n            waiting = [task for task in (manager.dispatcher_task, manager.cleanup_task) if task is not None]\n            if waiting:\n                await _settle_service_operation(asyncio.gather(*waiting, return_exceptions=True))\n            live = tuple(manager.active_tasks)\n            if live:\n                results = await _settle_service_operation(asyncio.gather(*live, return_exceptions=True))\n                for result in results:\n                    if isinstance(result, BaseException) and primary is None:\n                        primary = result\n            await _settle_service_operation(manager.service_io.close())\n        except BaseException as exc:\n            cleanup_error = exc\n    app.state.task_manager = None\n    try:\n        await _settle_service_operation(to_thread_owned(shutdown_runtime_resources))\n    except BaseException as exc:\n        if cleanup_error is None:\n            cleanup_error = exc\n        else:\n            cleanup_error.add_note('runtime close also failed: ' + repr(exc))\n    if primary is not None:\n        if cleanup_error is not None:\n            primary.add_note('service shutdown cleanup failed: ' + repr(cleanup_error))\n            raise primary from cleanup_error\n        raise primary\n    if cleanup_error is not None:\n        raise cleanup_error\n", 'OwnedFileResponse': '', 'TemporaryFileResponse': '', '_cleanup_generated_zip_task': '', 'parse_pdf': 'async def parse_pdf(\n    http_request: Request,\n    background_tasks: BackgroundTasks,\n    request_options: Annotated[\n        ParseRequestOptions, Depends(parse_request_form)\n    ],\n):\n    task = await _settle_service_operation(create_async_parse_task(request_options))\n    request_options = None\n    task_manager = get_task_manager()\n\n    try:\n        task = await task_manager.wait_for_terminal_state(task.task_id)\n    except TaskWaitAbortedError as exc:\n        return JSONResponse(\n            status_code=503,\n            content={\n                **task.to_status_payload(http_request),\n                "message": "Task manager became unavailable while waiting for result",\n                "error": str(exc),\n            },\n        )\n    except TaskRegistryObservationBusy as exc:\n        raise HTTPException(status_code=503, detail={"code": "task_wait_busy"}) from exc\n\n    if task.status == TASK_FAILED:\n        return JSONResponse(\n            status_code=409,\n            content={\n                **task.to_status_payload(http_request),\n                "message": "Task execution failed",\n            },\n        )\n\n    if not task.agent_idempotency_key:\n        raise HTTPException(status_code=410, detail="Task protocol result owner is absent")\n    try:\n        await _pin_response_result(task_manager, task.agent_idempotency_key, inline=True)\n        return await build_sync_file_parse_response(background_tasks=background_tasks, task=task, request=http_request)\n    except TaskRegistryObservationBusy as exc:\n        raise HTTPException(status_code=503, detail={"code":"result_reader_busy"}) from exc\n    except TaskRegistryPersistenceError as exc:\n        raise HTTPException(status_code=503, detail=str(exc)) from exc\n    except TaskProtocolConflict as exc:\n        raise HTTPException(status_code=409, detail=str(exc)) from exc\n', 'build_sync_file_parse_response': 'async def build_sync_file_parse_response(\n    background_tasks: BackgroundTasks,\n    task: AsyncParseTask,\n    request: Request,\n) -> Response:\n    task_payload = task.to_status_payload(request)\n    if task.response_format_zip:\n        response = await build_result_response(\n            background_tasks=background_tasks,\n            status_code=200,\n            output_dir=task.output_dir,\n            pdf_file_names=task.file_names,\n            backend=task.backend,\n            parse_method=task.parse_method,\n            return_md=task.return_md,\n            return_middle_json=task.return_middle_json,\n            return_model_output=task.return_model_output,\n            return_content_list=task.return_content_list,\n            return_images=task.return_images,\n            response_format_zip=task.response_format_zip,\n            return_original_file=task.return_original_file,\n            zip_filename=f"{task.task_id}.zip",\n        )\n        response.headers[FILE_PARSE_TASK_ID_HEADER] = task.task_id\n        response.headers[FILE_PARSE_TASK_STATUS_HEADER] = task.status\n        response.headers[FILE_PARSE_TASK_STATUS_URL_HEADER] = task_payload["status_url"]\n        response.headers[FILE_PARSE_TASK_RESULT_URL_HEADER] = task_payload["result_url"]\n        return response\n\n    result_dict = await _request_resources().owner.call(\n        build_result_dict, lane="bulk", required=True,\n        output_dir=task.output_dir,\n        pdf_file_names=task.file_names,\n        backend=task.backend,\n        parse_method=task.parse_method,\n        return_md=task.return_md,\n        return_middle_json=task.return_middle_json,\n        return_model_output=task.return_model_output,\n        return_content_list=task.return_content_list,\n        return_images=task.return_images,\n    )\n    return JSONResponse(\n        status_code=200,\n        content={\n            **task_payload,\n            "backend": task.backend,\n            "version": __version__,\n            "results": result_dict,\n        },\n    )\n', 'build_task_submission_response': 'async def build_task_submission_response(\n    task: AsyncParseTask,\n    request: Request,\n    task_manager: "AsyncTaskManager",\n) -> JSONResponse:\n    payload = await _registry_view(task_manager, lambda: task_manager.build_status_payload(task, request))\n    payload["message"] = "Task submitted successfully"\n    return JSONResponse(status_code=202, content=payload)\n', 'submit_parse_task': 'async def submit_parse_task(\n    http_request: Request,\n    request_options: Annotated[\n        ParseRequestOptions, Depends(parse_request_form)\n    ],\n):\n    task_manager = get_task_manager()\n    task = await _settle_service_operation(create_async_parse_task(request_options))\n    return await build_task_submission_response(task, http_request, task_manager)\n', 'AsyncTaskManager.begin_submission': '    async def begin_submission(self, options):\n        key = options.agent_idempotency_key\n        existing = await self.task_protocol_v2.observe(lambda: self.task_protocol_v2.get(key))\n        allow_create = existing is None\n        if allow_create and self.is_shutting_down:\n            raise HTTPException(status_code=503, detail="Task manager is shutting down")\n        if allow_create:\n            reason = (await self.task_protocol_v2.observe(self.admission_snapshot))["blocked_reason"]\n            if reason not in {None, "capacity_full"}:\n                raise HTTPException(status_code=503, detail=reason)\n        # No await between the final stop check and the ingress ticket.\n        if allow_create and self.is_shutting_down:\n            raise HTTPException(status_code=503, detail="Task manager is shutting down")\n        task_id = str(uuid.uuid4())\n        if allow_create:\n            self._ingress_in_flight.add(task_id)\n            self._ingress_drained.clear()\n        try:\n            record, created = await self.service_io.call(\n                self.task_protocol_v2.reconcile_or_create,\n                idempotency_key=key, task_id=task_id,\n                attempt_identity=options.agent_attempt_identity,\n                fence_identity=options.agent_fence_identity,\n                max_nonterminal_tasks=self.max_nonterminal_tasks,\n                allow_create=allow_create, lane="metadata",\n            )\n        except BaseException:\n            if allow_create:\n                self._ingress_in_flight.discard(task_id)\n                if not self._ingress_in_flight:\n                    self._ingress_drained.set()\n            raise\n        if created and record.task_id != task_id:\n            raise RuntimeError("created task identity drifted")\n        if not created and allow_create:\n            self._ingress_in_flight.discard(task_id)\n            if not self._ingress_in_flight:\n                self._ingress_drained.set()\n        return record, created\n', 'AsyncTaskManager.wait_for_terminal_state': '    async def wait_for_terminal_state(self, task_id: str) -> AsyncParseTask:\n        task = self.tasks.get(task_id)\n        if task is None:\n            raise TaskWaitAbortedError("Task not found")\n        if is_task_terminal(task.status):\n            return task\n        await self.task_protocol_v2.observe(lambda: self._raise_task_wait_failure(task_id))\n\n        task_event = self.task_events.get(task_id)\n        if task_event is None:\n            raise TaskWaitAbortedError("Task wait handle is unavailable")\n\n        event_wait_task = asyncio.create_task(task_event.wait())\n        manager_wait_task = asyncio.create_task(self.manager_wakeup.wait())\n        wait_helpers = (event_wait_task, manager_wait_task)\n        done: set[asyncio.Task[Any]] = set()\n        try:\n            done, _ = await asyncio.wait(\n                wait_helpers,\n                return_when=asyncio.FIRST_COMPLETED,\n            )\n        finally:\n            for waiter in wait_helpers:\n                waiter.cancel()\n            await asyncio.gather(*wait_helpers, return_exceptions=True)\n        for waiter in done:\n            with suppress(asyncio.CancelledError):\n                waiter.result()\n\n        task = self.tasks.get(task_id)\n        if task is None:\n            if self.is_shutting_down:\n                raise TaskWaitAbortedError("Task manager is shutting down")\n            raise TaskWaitAbortedError("Task was removed before completion")\n        if is_task_terminal(task.status):\n            return task\n        await self.task_protocol_v2.observe(lambda: self._raise_task_wait_failure(task_id))\n        if self.is_shutting_down:\n            raise TaskWaitAbortedError("Task manager is shutting down")\n        raise TaskWaitAbortedError(\n            self.last_worker_error or "Task manager became unavailable while waiting"\n        )\n', 'AsyncTaskManager.cleanup_expired_tasks': '    async def cleanup_expired_tasks(self) -> int:\n        cleaned = await self.service_io.call(\n            self.task_protocol_v2.cleanup_consumed, lane="bulk", required=True\n        )\n        if cleaned:\n            self.task_protocol_executor.notify_result_capacity_changed()\n        try:\n            await self.task_protocol_v2.observe(self._evict_consumed_protocol_tasks)\n        except TaskRegistryObservationBusy:\n            # Route eviction is idempotent and retried next interval; a busy\n            # data lock is not a cleanup-loop failure.\n            logger.info("route eviction deferred: registry observation busy")\n        return cleaned\n', 'AsyncTaskManager.shutdown': '    async def shutdown(self) -> None:\n        self.begin_soft_drain()\n        await self.service_io.quiesce_requests()\n        await self._ingress_drained.wait()\n        self._refill_pending_queue()\n        while self._scheduled_task_ids:\n            if self.last_worker_error is not None:\n                raise RuntimeError("Task shutdown is incomplete: " + self.last_worker_error)\n            if self.dispatcher_task is None or self.dispatcher_task.done():\n                raise RuntimeError("Task dispatcher stopped with retained responsibility")\n            self._schedule_changed.clear()\n            await self._schedule_changed.wait()\n        status = await self.task_protocol_v2.observe(\n            lambda: self.task_protocol_v2.admission_status(set(self.tasks))\n        )\n        if status["durable_nonterminal_tasks"]:\n            raise RuntimeError("durable task responsibilities remain during shutdown")\n        if self.dispatcher_task is not None:\n            self.dispatcher_task.cancel()\n            with suppress(asyncio.CancelledError):\n                await self.dispatcher_task\n            self.dispatcher_task = None\n        if self.cleanup_task is not None:\n            self.cleanup_task.cancel()\n            with suppress(asyncio.CancelledError):\n                await self.cleanup_task\n            self.cleanup_task = None\n        self.active_tasks.clear()\n        cleaned = await self.service_io.call(\n            self.task_protocol_v2.cleanup_consumed, lane="bulk", required=True\n        )\n        if cleaned:\n            self.task_protocol_executor.notify_result_capacity_changed()\n', 'AsyncTaskManager._process_task': '    async def _process_task(self, task_id: str) -> None:\n        task = self.tasks.get(task_id)\n        if task is None:\n            return\n\n        try:\n            if not task.agent_idempotency_key:\n                raise RuntimeError("Task protocol route identity is absent")\n            async def parse_stage():\n                await self._run_parse_stage(task)\n            async def finalizer_stage():\n                await build_retained_task_result(\n                    task, byte_budget=self.task_protocol_executor.result_reservation_bytes, service_io=self.service_io\n                )\n                return (Path(task.result_artifact_path), task.result_artifact_sha256, task.result_artifact_bytes, task.result_artifact_owner)\n            await self.task_protocol_executor.run(\n                registry=self.task_protocol_v2, key=task.agent_idempotency_key,\n                parse=parse_stage, finalize=finalizer_stage, registry_io=self.service_io,\n            )\n            task.status = TASK_COMPLETED\n            task.completed_at = utc_now_iso()\n            self._signal_task_event(task.task_id)\n        except TaskExecutionStopped as exc:\n            if (exc.capacity_wait and self.is_shutting_down\n                    and self.last_worker_error is None):\n                self._stopped_pending_task_ids.add(task_id)\n                self._signal_task_event(task_id)\n                return\n            self._signal_task_event(task_id)\n            raise\n        except asyncio.CancelledError:\n            if task.status == TASK_PENDING:\n                self._signal_task_event(task_id)\n                raise\n            task.status = TASK_FAILED\n            task.error = "Task processor was cancelled"\n            task.completed_at = utc_now_iso()\n            self._signal_task_event(task_id)\n            raise\n        except (TaskRegistryPersistenceError, TaskResultCapacityRecoveryRequired) as exc:\n            self.task_wait_failures[task_id] = exc\n            self._signal_task_event(task_id)\n            logger.exception("Task registry persistence failed; task status remains nonterminal")\n            raise\n        except Exception as exc:\n            task.status = TASK_FAILED\n            task.error = str(exc)\n            task.completed_at = utc_now_iso()\n            self._signal_task_event(task_id)\n            logger.exception(f"Async task failed: {task_id}")\n        finally:\n            self.queue.task_done()\n', 'create_async_parse_task': 'async def create_async_parse_task(\n    request_options: ParseRequestOptions,\n) -> AsyncParseTask:\n    task_manager = get_task_manager()\n    identities = (request_options.agent_idempotency_key, request_options.agent_attempt_identity, request_options.agent_fence_identity)\n    if not all(isinstance(item, str) and item for item in identities):\n        raise HTTPException(status_code=400, detail="Task protocol v2 identities are required")\n    try:\n        record, created = await task_manager.begin_submission(request_options)\n    except TaskAdmissionFull as exc:\n        raise HTTPException(status_code=429, detail=str(exc)) from exc\n    except TaskRegistryPersistenceError as exc:\n        raise HTTPException(status_code=503, detail=str(exc)) from exc\n    except TaskRegistryObservationBusy as exc:\n        raise HTTPException(status_code=503, detail={"code": "registry_observation_busy"}) from exc\n    except TaskProtocolConflict as exc:\n        raise HTTPException(status_code=409, detail=str(exc)) from exc\n    if not created:\n        return await _registry_view(task_manager, lambda: task_manager.reconcile_submission(\n            task_manager.task_protocol_v2.get(record.idempotency_key)))\n    task_id = record.task_id\n    task = None\n    try:\n        task_output_dir, uploads_dir = await task_manager.service_io.call(\n            _prepare_ingress_tree, task_manager, task_id, record.idempotency_key, lane="bulk", required=True\n        )\n        uploads = await save_upload_files(uploads_dir, request_options.files, task_manager.service_io)\n        request_options.files.clear()\n        file_names = [upload.stem for upload in uploads]\n        task = AsyncParseTask(\n            task_id=task_id,\n            status=TASK_PENDING,\n            backend=request_options.backend,\n            file_names=file_names,\n            created_at=utc_now_iso(),\n            output_dir=task_output_dir,\n            effort=request_options.effort,\n            parse_method=request_options.parse_method,\n            lang_list=request_options.lang_list,\n            formula_enable=request_options.formula_enable,\n            table_enable=request_options.table_enable,\n            image_analysis=request_options.image_analysis,\n            server_url=request_options.server_url,\n            return_md=request_options.return_md,\n            return_middle_json=request_options.return_middle_json,\n            return_model_output=request_options.return_model_output,\n            return_content_list=request_options.return_content_list,\n            return_images=request_options.return_images,\n            response_format_zip=request_options.response_format_zip,\n            return_original_file=request_options.return_original_file,\n            client_side_output_generation=request_options.client_side_output_generation,\n            start_page_id=request_options.start_page_id,\n            end_page_id=request_options.end_page_id,\n            upload_names=[upload.original_name for upload in uploads],\n            uploads=[upload.path for upload in uploads],\n            agent_idempotency_key=request_options.agent_idempotency_key,\n            agent_attempt_identity=request_options.agent_attempt_identity,\n            agent_fence_identity=request_options.agent_fence_identity,\n        )\n        task_manager._adopt_ingress_task(task)\n        await task_manager.service_io.call(\n            task_manager.task_protocol_v2.bind_task_payload, task.agent_idempotency_key, asdict(task), lane="bulk", required=True\n        )\n        await task_manager.submit(task)\n        return task\n    except BaseException as exc:\n        try:\n            current = await task_manager.service_io.call(task_manager.task_protocol_v2.get, record.idempotency_key, lane="metadata", required=True)\n            if current is not None and current.state in {"ingress", "ingress_cleanup"}:\n                await _settle_service_operation(task_manager.service_io.call(\n                    task_manager.task_protocol_v2.abort_ingress, record.idempotency_key, lane="bulk", required=True\n                ))\n                task_manager._discard_ingress_task(task_id)\n            elif current is not None and current.state in {"pending", "processing", "finalizing", "completed", "failed"}:\n                # Accepted responsibility survives response/observation failure.\n                # Its already-adopted route is not an ingress cleanup target.\n                if task is None:\n                    raise RuntimeError("accepted ingress route was never constructed") from exc\n                if current.state == "pending" and task_id not in task_manager._scheduled_task_ids:\n                    # Failed route adoption is rehydratable from the accepted payload.\n                    # Never remove an executing route or delete its durable input.\n                    task_manager._discard_ingress_task(task_id)\n            elif current is None:\n                task_manager._discard_ingress_task(task_id)\n        except BaseException as cleanup_exc:\n            task_manager.last_worker_error = "ingress_reconciliation_failed:" + type(cleanup_exc).__name__\n            task_manager._stopped_pending_task_ids.add(task_id)\n            task_manager.begin_soft_drain()\n            exc.add_note("ingress cleanup/reconciliation failed: " + repr(cleanup_exc))\n            raise exc from cleanup_exc\n        if isinstance(exc, TaskRegistryPersistenceError):\n            raise HTTPException(status_code=503, detail=str(exc)) from exc\n        if isinstance(exc, TaskRegistryObservationBusy):\n            raise HTTPException(status_code=503, detail={"code": "registry_observation_busy"}) from exc\n        if isinstance(exc, TaskProtocolConflict):\n            raise HTTPException(status_code=409, detail=str(exc)) from exc\n        raise\n    finally:\n        task_manager.finish_submission(task_id)\n', '_prepare_ingress_tree': 'def _prepare_ingress_tree(task_manager, task_id: str, key: str) -> tuple[str, str]:\n    task_output_dir = create_task_output_dir(task_id)\n    uploads_dir = os.path.join(task_output_dir, "uploads")\n    os.mkdir(uploads_dir)\n    task_manager.task_protocol_v2.bind_ingress_root(key)\n    return task_output_dir, uploads_dir\n', 'save_upload_files': 'async def save_upload_files(upload_dir: str, files: list[UploadFile], service_io=None) -> list[StoredUpload]:\n    if service_io is None:\n        os.makedirs(upload_dir, exist_ok=True)\n    else:\n        await service_io.call(os.makedirs, upload_dir, exist_ok=True, lane="bulk", required=True)\n    uploads: list[StoredUpload] = []\n\n    for upload in files:\n        original_name = upload.filename or f"upload-{uuid.uuid4()}"\n        filename = normalize_upload_filename(original_name)\n        normalized_stem = normalize_task_stem(Path(filename).stem)\n        destination = (build_upload_destination(upload_dir, filename) if service_io is None\n                       else await service_io.call(build_upload_destination, upload_dir, filename, lane="bulk", required=True))\n        handle = None\n        primary = None\n        try:\n            if service_io is None:\n                handle = open(destination, "xb")\n            else:\n                handle = await service_io.call(open, destination, "xb", lane="bulk", required=True,\n                                               on_cancel_result=lambda stream: stream.close())\n            while True:\n                chunk = await _settle_service_operation(upload.read(1 << 20))\n                if not chunk:\n                    break\n                if service_io is None:\n                    handle.write(chunk)\n                else:\n                    await service_io.call(handle.write, chunk, lane="bulk", required=True)\n            closing = handle\n            handle = None\n            if service_io is None:\n                closing.close()\n            else:\n                await service_io.call(closing.close, lane="bulk", required=True)\n            file_suffix = (guess_suffix_by_path(destination) if service_io is None else\n                           await service_io.call(guess_suffix_by_path, destination, lane="bulk", required=True))\n            if file_suffix not in SUPPORTED_UPLOAD_SUFFIXES:\n                if service_io is None:\n                    cleanup_file(str(destination))\n                else:\n                    await service_io.call(cleanup_file, str(destination), lane="bulk", required=True)\n                raise HTTPException(\n                    status_code=400,\n                    detail=f"Unsupported file type: {file_suffix}",\n                )\n\n            uploads.append(\n                StoredUpload(\n                    original_name=original_name,\n                    stem=normalized_stem,\n                    path=str(destination),\n                )\n            )\n        except BaseException as exc:\n            primary = exc\n            try:\n                if handle is not None:\n                    closing = handle\n                    handle = None\n                    if service_io is None:\n                        closing.close()\n                    else:\n                        await _settle_service_operation(service_io.call(closing.close, lane="bulk", required=True))\n                if service_io is None:\n                    cleanup_file(str(destination))\n                else:\n                    await _settle_service_operation(service_io.call(cleanup_file, str(destination), lane="bulk", required=True))\n            except BaseException as cleanup_exc:\n                exc.add_note("upload cleanup failed: " + repr(cleanup_exc))\n                raise exc from cleanup_exc\n            raise\n        finally:\n            try:\n                await _settle_service_operation(upload.close())\n            except BaseException as close_exc:\n                if primary is not None:\n                    primary.add_note("upload source close failed: " + repr(close_exc))\n                    raise primary from close_exc\n                raise\n\n    normalized_stems, renamed_stems = uniquify_task_stems(\n        [upload.stem for upload in uploads]\n    )\n    if renamed_stems:\n        rename_details = ", ".join(\n            f"{Path(upload.original_name).name} -> {effective_stem}"\n            for upload, effective_stem in zip(uploads, normalized_stems)\n            if upload.stem != effective_stem\n        )\n        logger.warning(\n            f"Normalized duplicate upload stems within request: {rename_details}"\n        )\n        uploads = [\n            StoredUpload(\n                original_name=upload.original_name,\n                stem=effective_stem,\n                path=upload.path,\n            )\n            for upload, effective_stem in zip(uploads, normalized_stems)\n        ]\n    return uploads\n', 'run_parse_job': 'async def run_parse_job(\n    output_dir: str,\n    uploads: list[StoredUpload],\n    request_options: ParseRequestOptions | AsyncParseTask,\n    config: dict[str, Any],\n) -> list[str]:\n    pdf_file_names, pdf_bytes_list = await to_thread_owned(load_parse_inputs, uploads)\n    actual_lang_list = normalize_lang_list(request_options.lang_list, len(pdf_file_names))\n    response_file_names = list(pdf_file_names)\n\n    parse_kwargs = dict(\n        output_dir=output_dir,\n        pdf_file_names=list(pdf_file_names),\n        pdf_bytes_list=list(pdf_bytes_list),\n        p_lang_list=list(actual_lang_list),\n        backend=request_options.backend,\n        parse_method=request_options.parse_method,\n        effort=getattr(request_options, "effort", DEFAULT_HYBRID_EFFORT),\n        formula_enable=request_options.formula_enable,\n        table_enable=request_options.table_enable,\n        image_analysis=request_options.image_analysis,\n        server_url=request_options.server_url,\n        f_draw_layout_bbox=False,\n        f_draw_span_bbox=False,\n        f_dump_md=request_options.return_md,\n        f_dump_middle_json=request_options.return_middle_json,\n        f_dump_model_output=request_options.return_model_output,\n        f_dump_orig_pdf=(\n            request_options.return_original_file and request_options.response_format_zip\n        ),\n        f_dump_content_list=request_options.return_content_list,\n        start_page_id=request_options.start_page_id,\n        end_page_id=request_options.end_page_id,\n        client_side_output_generation=getattr(\n            request_options,\n            "client_side_output_generation",\n            False,\n        ),\n        **config,\n    )\n\n    if request_options.backend == "pipeline":\n        await to_thread_owned(do_parse, **parse_kwargs)\n    else:\n        await aio_do_parse(**parse_kwargs)\n    return response_file_names\n'}
    lines = source.splitlines(keepends=True)
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in replacements:
            nodes.append((node, node.name))
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                key = node.name + "." + getattr(child, "name", "")
                if key in replacements and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.append((child, key))
    if {key for _, key in nodes} != set(replacements) or len(nodes) != len(replacements):
        raise RuntimeError("service resource scope generation anchors drifted")
    for node, key in sorted(nodes, key=lambda item: item[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [replacements[key]]
    source = "".join(lines)
    source = _replace_exact(source, "\n@asynccontextmanager\nasync def lifespan(app: FastAPI):\n",
                            "\n" + 'from contextvars import ContextVar\n\n_request_resource_context = ContextVar(\'mineru_request_resources\', default=None)\n\n\ndef _request_resources():\n    resources = _request_resource_context.get()\n    if resources is None:\n        raise RuntimeError(\'result access requires the real ASGI request resource scope\')\n    return resources\n\n\nclass _ServiceRequestMiddleware:\n    """Keep the entire ASGI response and its resource release in one owned scope."""\n\n    def __init__(self, app):\n        self.app = app\n\n    async def __call__(self, scope, receive, send):\n        path = scope.get(\'path\', \'\')\n        method = scope.get(\'method\', \'\')\n        mutation = method == \'POST\' and (path == \'/file_parse\' or path == \'/tasks\' or path.startswith(\'/tasks/\'))\n        reader = method in {\'GET\', \'HEAD\'} and path.startswith(\'/tasks/\') and path.endswith(\'/result\')\n        if scope.get(\'type\') != \'http\' or not (mutation or reader):\n            return await self.app(scope, receive, send)\n        manager = getattr(app.state, \'task_manager\', None)\n        if manager is None:\n            response = JSONResponse(status_code=503, content={\'detail\': \'Task manager is not initialized\'})\n            return await response(scope, receive, send)\n        try:\n            resources = manager.service_io.open_request(\'mutation\' if mutation else \'reader\')\n        except TaskRegistryObservationBusy:\n            response = JSONResponse(status_code=503, content={\'detail\': {\'code\': \'request_resources_busy\'}})\n            return await response(scope, receive, send)\n        adjusted = dict(scope)\n        extensions = dict(scope.get(\'extensions\', {}))\n        extensions.pop(\'http.response.pathsend\', None)\n        adjusted[\'extensions\'] = extensions\n        token = _request_resource_context.set(resources)\n        primary = None\n        cleanup_error = None\n        try:\n            await _settle_service_operation(self.app(adjusted, receive, send))\n        except BaseException as exc:\n            primary = exc\n        finally:\n            try:\n                await _settle_service_operation(resources.close())\n            except BaseException as exc:\n                cleanup_error = exc\n                manager.last_worker_error = \'response_resource_release_failed:\' + type(exc).__name__\n                manager.begin_soft_drain()\n            finally:\n                _request_resource_context.reset(token)\n        if primary is not None:\n            if cleanup_error is not None:\n                primary.add_note(\'response resource release failed: \' + repr(cleanup_error))\n                raise primary from cleanup_error\n            raise primary\n        if cleanup_error is not None:\n            raise cleanup_error\n\n\nasync def _pin_response_result(manager, key, *, inline=False):\n    resources = _request_resources()\n    resources.reserve_reader()\n    acquire = manager.task_protocol_v2.acquire_inline_result if inline else manager.task_protocol_v2.acquire_result\n    path = await manager.service_io.call(\n        acquire, key, required=True,\n        on_cancel_result=lambda _path: manager.task_protocol_v2.release_result(key),\n    )\n    # Same task, no await between acquiring a returned pin and registering its finally.\n    resources.defer(manager.task_protocol_v2.release_result, key)\n    return path\n\n\ndef _build_retained_artifact_owned(task, byte_budget):\n    """Same retained ZIP algorithm; all descriptors and namespace IO stay on one worker."""\n    observations = []\n    retained = os.path.join(task.output_dir, \'.retained-result.zip\')\n    part = retained + \'.part\'\n    primary = None\n    result = None\n    try:\n        budget, observations = _retained_result_sources(task, byte_budget=byte_budget)\n        _write_retained_zip_from_fds(observations, part, budget)\n        closing = observations\n        observations = []\n        _verify_and_close_result_sources(closing)\n        _commit_retained_result(part, retained, task.output_dir)\n        digest, size = _hash_file(retained)\n        if size <= 0:\n            raise RuntimeError(\'retained result ZIP is empty\')\n        result = retained, digest, size\n    except BaseException as exc:\n        primary = exc\n    cleanup_error = None\n    for _path, _name, descriptor, _identity in observations:\n        try:\n            os.close(descriptor)\n        except BaseException as exc:\n            if cleanup_error is None:\n                cleanup_error = exc\n            else:\n                cleanup_error.add_note(\'additional source close failed: \' + repr(exc))\n    for path in (part, retained) if primary is not None else (part,):\n        try:\n            cleanup_file(path)\n        except BaseException as exc:\n            if cleanup_error is None:\n                cleanup_error = exc\n            else:\n                cleanup_error.add_note(\'additional retained cleanup failed: \' + repr(exc))\n    if primary is not None:\n        if cleanup_error is not None:\n            primary.add_note(\'retained resource cleanup failed: \' + repr(cleanup_error))\n            raise primary from cleanup_error\n        raise primary\n    if cleanup_error is not None:\n        raise cleanup_error\n    return result\n' + "\n@asynccontextmanager\nasync def lifespan(app: FastAPI):\n",
                            count=1, label="ASGI resource scope helpers")
    source = _replace_exact(source, "    app.add_middleware(GZipMiddleware, minimum_size=1000)\n",
                            "    app.add_middleware(GZipMiddleware, minimum_size=1000)\n    app.add_middleware(_ServiceRequestMiddleware)\n",
                            count=1, label="ASGI resource scope registration")
    compile(source, "<owned-service-api>", "exec")
    return source


def _patch_health_durable_view_observation(source: str) -> str:
    """Answer the closed health projection from the durable view, off the data lock."""
    if "async def create_async_parse_task(" not in source:
        return source
    source = _replace_exact(
        source,
        "    TaskRegistryPersistenceError, TaskRegistryObservationBusy, RegistryServiceIO,\n",
        "    TaskRegistryPersistenceError, TaskRegistryObservationBusy, RegistryServiceIO,\n"
        "    ServingLoopProbe,\n",
        count=1, label="serving loop probe import",
    )
    source = _replace_exact(
        source,
        '        self.service_io = RegistryServiceIO(\n'
        '            drain=_settle_service_operation, max_pending=self.max_nonterminal_tasks + 8\n'
        '        )\n',
        '        self.service_io = RegistryServiceIO(\n'
        '            drain=_settle_service_operation, max_pending=self.max_nonterminal_tasks + 8\n'
        '        )\n'
        '        self.serving_loop_probe = ServingLoopProbe()\n',
        count=1, label="serving loop probe owner",
    )
    source = _replace_exact(
        source,
        "            self.capacity_observer = observer\n"
        "        self.is_shutting_down = False\n",
        "            self.capacity_observer = observer\n"
        "        self.serving_loop_probe.start(asyncio.get_running_loop())\n"
        "        self.is_shutting_down = False\n",
        count=1, label="serving loop probe start",
    )
    # A manager that fails to start is never attached to the app, so its
    # shutdown never runs and the probe must be released here.
    source = _replace_exact(
        source,
        "    async def start(self) -> None:\n",
        "    async def start(self) -> None:\n"
        "        try:\n"
        "            await self._start_owned()\n"
        "        except BaseException:\n"
        "            self.serving_loop_probe.close()\n"
        "            raise\n\n"
        "    async def _start_owned(self) -> None:\n",
        count=1, label="serving loop probe start ownership",
    )
    source = _replace_exact(
        source,
        "            await _settle_service_operation(manager.service_io.close())\n",
        "            manager.serving_loop_probe.close()\n"
        "            await _settle_service_operation(manager.service_io.close())\n",
        count=1, label="serving loop probe close",
    )
    source = _replace_exact(
        source,
        "    def admission_snapshot(self):\n"
        "        snapshot = self.task_protocol_v2.admission_status(set(self.tasks), self._ingress_in_flight)\n",
        "    def admission_snapshot_from_view(self, view):\n"
        "        return self._decorate_admission(\n"
        "            self.task_protocol_v2.admission_status_from_view(\n"
        "                view, set(self.tasks), self._ingress_in_flight\n"
        "            )\n"
        "        )\n\n"
        "    def admission_snapshot(self):\n"
        "        return self._decorate_admission(\n"
        "            self.task_protocol_v2.admission_status(set(self.tasks), self._ingress_in_flight)\n"
        "        )\n\n"
        "    def _decorate_admission(self, snapshot):\n",
        count=1, label="durable view admission projection",
    )
    # The closed projection is built from the published durable state, so the
    # route never waits for the data lock that a commit holds across fsync.
    source = _replace_exact_span(
        source,
        "    def view():\n        if not task_manager.is_healthy():\n",
        '    return {\n        "status": "recovering" if admission["recovery_overcommitted"] else "healthy",\n',
        '    try:\n'
        '        view = task_manager.task_protocol_v2.durable_view()\n'
        '        admission = task_manager.admission_snapshot_from_view(view)\n'
        '        stats = task_manager.get_stats()\n'
        "        if getattr(task_manager, 'capacity_config', None) is None:\n"
        '            protocol_runtime = task_protocol_runtime_status(\n'
        '                task_manager.task_protocol_v2, task_manager.task_protocol_executor,\n'
        '                persistence_event=view.persistence_event,\n'
        '                durability_uncertain=view.durability_uncertain,\n'
        '            )\n'
        '            capacity_extra = {}\n'
        '        else:\n'
        '            protocol_runtime = task_protocol_runtime_status(\n'
        '                task_manager.task_protocol_v2, task_manager.task_protocol_executor,\n'
        '                capacity_config_sha256=task_manager.capacity_config.sha256,\n'
        '                persistence_event=view.persistence_event,\n'
        '                durability_uncertain=view.durability_uncertain,\n'
        '            )\n'
        "            capacity_extra = {'capacity_observation': task_manager.capacity_observer.snapshot()}\n"
        '    except TaskRegistryPersistenceError:\n'
        '        return JSONResponse(status_code=503, content={"status": "unhealthy", "version": __version__, "error": "registry_persistence_unavailable"})\n'
        '    except RuntimeError as exc:\n'
        '        return JSONResponse(status_code=503, content={"status": "unhealthy", "version": __version__, "error": str(exc)[:256]})\n',
        label="durable view health projection",
    )
    compile(source, "<owned-service-api>", "exec")
    return source


def _patch_api_gc_lifecycle(source: str) -> str:
    """Wire a CLI-only pre-serving freeze; exact full-file preimages still apply."""
    if "def main(" not in source:
        # Existing reduced manager fixtures have no executable CLI/lifespan.
        return source
    source = _replace_exact(
        source,
        "    ServingLoopProbe,\n",
        "    ServingLoopProbe, bootstrap_api_gc, enter_api_gc_runtime,\n"
        "    mark_api_gc_quiesced, close_api_gc, shutdown_pdf_render_executor_verified,\n",
        count=1, label="GC lifecycle imports",
    )
    source = _replace_exact(
        source,
        'async def startup_app_state(app: FastAPI) -> "AsyncTaskManager":\n',
        'async def startup_app_state(app: FastAPI) -> "AsyncTaskManager":\n'
        '    enter_api_gc_runtime()\n',
        count=1, label="GC sealed before recovery dispatcher",
    )
    source = _replace_exact(
        source,
        '    if cleanup_error is not None:\n        raise cleanup_error\n\n\n'
        'def shutdown_runtime_resources() -> None:\n',
        '    if cleanup_error is not None:\n        raise cleanup_error\n'
        '    mark_api_gc_quiesced()\n\n\n'
        'def shutdown_runtime_resources() -> None:\n',
        count=1, label="GC quiescence after all owned runtime cleanup",
    )
    source = _replace_exact(
        source,
        'def shutdown_runtime_resources() -> None:\n    try:\n        shutdown_cached_models()\n    except Exception as exc:\n        logger.warning(f"Failed to shutdown cached VLM models: {exc}")\n\n    try:\n        shutdown_pdf_render_executor()\n    except Exception as exc:\n        logger.warning(f"Failed to shutdown PDF render executor: {exc}")\n',
        'def shutdown_runtime_resources() -> None:\n    failure = None\n    for close in (shutdown_cached_models, shutdown_pdf_render_executor_verified):\n        try:\n            close()\n        except BaseException as exc:\n            if failure is None:\n                failure = exc\n            else:\n                failure.add_note("another runtime close failed: " + repr(exc))\n    if failure is not None:\n        raise failure\n',
        count=1, label="GC requires positive native cleanup; do not swallow errors",
    )
    old = '    if reload:\n        uvicorn.run(\n'
    if source.count(old) != 1:
        raise RuntimeError("GC main branch anchor drifted")
    start = source.index(old)
    end = source.index('\n\nif __name__ == "__main__":', start)
    branch = source[start:end]
    replacement = (
        '    bootstrap_api_gc(app, reload=reload)\n'
        '    _gc_primary = None\n'
        '    try:\n' + ''.join('    '+line if line.strip() else line for line in branch.splitlines(keepends=True))
        + '\n    except BaseException as exc:\n'
        '        _gc_primary = exc\n'
        '        raise\n'
        '    finally:\n'
        '        try:\n'
        '            close_api_gc()\n'
        '        except BaseException as cleanup:\n'
        '            if _gc_primary is not None:\n'
        '                _gc_primary.add_note("API GC close failed: " + repr(cleanup))\n'
        '            else:\n'
        '                raise\n'
    )
    return source[:start] + replacement + source[end:]


def patch_source(relative_path: str, source: str) -> str:
    """Return the deterministic patched source for one exact MinerU module."""

    if relative_path == "mineru/cli/api_request.py":
        source = _replace_exact(
            source,
            "    end_page_id: int\n",
            "    end_page_id: int\n"
            "    agent_idempotency_key: Optional[str]\n"
            "    agent_attempt_identity: Optional[str]\n"
            "    agent_fence_identity: Optional[str]\n",
            count=1,
            label="task protocol v2 request identities",
        )
        source = _replace_exact(
            source,
            "    end_page_id: Annotated[\n"
            "        int,\n"
            '        Form(description="The ending page for PDF parsing, beginning from 0"),\n'
            "    ] = 99999,\n"
            ") -> ParseRequestOptions:\n",
            "    end_page_id: Annotated[\n"
            "        int,\n"
            '        Form(description="The ending page for PDF parsing, beginning from 0"),\n'
            "    ] = 99999,\n"
            "    agent_idempotency_key: Annotated[Optional[str], Form()] = None,\n"
            "    agent_attempt_identity: Annotated[Optional[str], Form()] = None,\n"
            "    agent_fence_identity: Annotated[Optional[str], Form()] = None,\n"
            ") -> ParseRequestOptions:\n",
            count=1,
            label="task protocol v2 form identities",
        )
        source = _replace_exact(
            source,
            "        end_page_id=end_page_id,\n    )\n",
            "        end_page_id=end_page_id,\n"
            "        agent_idempotency_key=agent_idempotency_key,\n"
            "        agent_attempt_identity=agent_attempt_identity,\n"
            "        agent_fence_identity=agent_fence_identity,\n"
            "    )\n",
            count=1,
            label="task protocol v2 form result",
        )
        return source

    if relative_path == "mineru_vl_utils/vlm_client/http_client.py":
        limiter = '''

class _ProcessAsyncRequestLimiter:
    """One final-POST concurrency owner shared by every client on one loop."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise RuntimeError("global VLM request concurrency is invalid")
        self.capacity = capacity
        self.semaphore = asyncio.Semaphore(capacity)
        self.active = 0
        self.pending = 0
        self.peak = 0

    async def __aenter__(self):
        with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
            self.pending += 1
            _PROCESS_ASYNC_REQUEST_STATS["pending"] += 1
        try:
            await self.semaphore.acquire()
        except BaseException:
            with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
                self.pending -= 1
                _PROCESS_ASYNC_REQUEST_STATS["pending"] -= 1
            raise
        with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
            self.pending -= 1
            _PROCESS_ASYNC_REQUEST_STATS["pending"] -= 1
            self.active += 1
            _PROCESS_ASYNC_REQUEST_STATS["active"] += 1
            self.peak = max(self.peak, self.active)
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
            if self.active < 1 or _PROCESS_ASYNC_REQUEST_STATS["active"] < 1:
                raise RuntimeError("global VLM request limiter underflowed")
            self.active -= 1
            _PROCESS_ASYNC_REQUEST_STATS["active"] -= 1
        self.semaphore.release()


import os as _agent_request_os
import threading as _agent_request_threading

_PROCESS_ASYNC_REQUEST_STATS_LOCK = _agent_request_threading.Lock()
_PROCESS_ASYNC_REQUEST_STATS = {"active": 0, "pending": 0}
_PROCESS_ASYNC_REQUEST_LIMITERS = {}
_CAPACITY_OWNER = None


def _bind_capacity_owner(config_sha256, capacity, request_soft_drain):
    """Bind the real manager before any final POST, without creating a limiter."""
    global _CAPACITY_OWNER
    import uuid
    loop = asyncio.get_running_loop()
    if type(capacity) is not int or not 1 <= capacity <= 128 or not callable(request_soft_drain):
        raise RuntimeError("capacity owner binding is invalid")
    with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
        if _CAPACITY_OWNER is None:
            if _PROCESS_ASYNC_REQUEST_LIMITERS:
                raise RuntimeError("HTTP limiter preceded capacity owner startup")
            _CAPACITY_OWNER = {
                "loop": loop, "process_id": _agent_request_os.getpid(),
                "loop_epoch": str(uuid.uuid4()), "config_sha256": config_sha256,
                "capacity": capacity, "request_soft_drain": request_soft_drain,
                "foreign_loop_observed": False, "soft_drain_requested": False,
                "soft_drain_applied": False,
            }
        elif (
            _CAPACITY_OWNER["loop"] is not loop
            or _CAPACITY_OWNER["process_id"] != _agent_request_os.getpid()
            or _CAPACITY_OWNER["config_sha256"] != config_sha256
            or _CAPACITY_OWNER["capacity"] != capacity
            or _CAPACITY_OWNER["request_soft_drain"] != request_soft_drain
            or _CAPACITY_OWNER["soft_drain_requested"]
        ):
            raise RuntimeError("capacity owner cannot be rebound within one process")


def _apply_capacity_soft_drain(owner):
    if asyncio.get_running_loop() is not owner["loop"]:
        raise RuntimeError("capacity drain callback is outside the serving loop")
    owner["request_soft_drain"]()
    with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
        owner["soft_drain_applied"] = True


def _capacity_http_snapshot(config_sha256):
    """Read initialized owner facts on its loop; health never creates credits."""
    loop = asyncio.get_running_loop()
    with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
        owner = _CAPACITY_OWNER
        if (owner is None or owner["loop"] is not loop
                or owner["process_id"] != _agent_request_os.getpid()
                or owner["config_sha256"] != config_sha256):
            raise RuntimeError("capacity HTTP observation has no matching serving owner")
        limiter = _PROCESS_ASYNC_REQUEST_LIMITERS.get(loop)
        return {
            "process_id": owner["process_id"], "loop_epoch": owner["loop_epoch"],
            "final_http_limit_per_loop": None if limiter is None else limiter.capacity,
            "http_limiter_state": "not_initialized" if limiter is None else "initialized",
            "http_counters": {
                "active_requests": _PROCESS_ASYNC_REQUEST_STATS["active"],
                "pending_requests": _PROCESS_ASYNC_REQUEST_STATS["pending"],
            },
            "owner_control": {
                "foreign_loop_observed": owner["foreign_loop_observed"],
                "soft_drain_requested": owner["soft_drain_requested"],
                "soft_drain_applied": owner["soft_drain_applied"],
                "trigger": "foreign_event_loop" if owner["foreign_loop_observed"] else None,
            },
        }


def _process_async_request_snapshot() -> dict:
    """Logical final POST calls, including transport retries; not sockets/tasks."""
    with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
        return {
            "contract_version": "mineru.api-http-request-snapshot.v1",
            "active_requests": _PROCESS_ASYNC_REQUEST_STATS["active"],
            "pending_requests": _PROCESS_ASYNC_REQUEST_STATS["pending"],
            "process_id": _agent_request_os.getpid(),
        }


def _process_async_request_limiter(capacity: int) -> _ProcessAsyncRequestLimiter:
    loop = asyncio.get_running_loop()
    notify_owner = None
    foreign_loop = False
    with _PROCESS_ASYNC_REQUEST_STATS_LOCK:
        owner = _CAPACITY_OWNER
        if owner is None and (
            "MINERU_CAPACITY_CONFIG_PATH" in _agent_request_os.environ
            or "MINERU_CAPACITY_CONFIG_SHA256" in _agent_request_os.environ
        ):
            raise RuntimeError("capacity serving owner is not initialized")
        if owner is not None:
            if owner["process_id"] != _agent_request_os.getpid():
                raise RuntimeError("capacity owner process changed")
            foreign_loop = owner["loop"] is not loop
            if foreign_loop:
                owner["foreign_loop_observed"] = True
                if not owner["soft_drain_requested"]:
                    owner["soft_drain_requested"] = True
                    notify_owner = owner
            elif type(capacity) is not int or capacity != owner["capacity"]:
                raise RuntimeError("HTTP request capacity differs from the serving config")
        if not foreign_loop:
            limiter = _PROCESS_ASYNC_REQUEST_LIMITERS.get(loop)
            if limiter is None:
                limiter = _ProcessAsyncRequestLimiter(capacity)
                _PROCESS_ASYNC_REQUEST_LIMITERS[loop] = limiter
            elif limiter.capacity != capacity:
                raise RuntimeError("global VLM request concurrency drifted within one process")
    if foreign_loop:
        if notify_owner is not None:
            notify_owner["loop"].call_soon_threadsafe(_apply_capacity_soft_drain, notify_owner)
        raise RuntimeError("final POST rejected outside the capacity serving loop")
    return limiter
'''
        source = _replace_exact(
            source,
            "\n\nclass HTTPMethod(str, Enum):\n",
            limiter + "\n\nclass HTTPMethod(str, Enum):\n",
            count=1,
            label="HTTP global request limiter",
        )
        return _replace_exact(
            source,
            "        client = await self._aio_client()\n"
            "        response = await client.post(self.chat_url, json=request_body)\n",
            "        limiter = _process_async_request_limiter(self.max_concurrency)\n"
            "        client = await self._aio_client()\n"
            "        async with limiter:\n"
            "            response = await client.post(self.chat_url, json=request_body)\n",
            count=1,
            label="HTTP final async POST ownership",
        )

    if relative_path == "mineru_vl_utils/post_process/cross_page_table.py":
        source = _replace_exact(
            source,
            "    if len(tasks) != len(responses):\n"
            "        logger.warning(\n"
            '            "Task/response count mismatch: {} tasks but {} responses, skipping merge results",\n'
            "            len(tasks), len(responses),\n"
            "        )\n"
            "        return\n",
            "    if len(tasks) != len(responses):\n"
            "        raise RuntimeError(\n"
            '            "cross-page table task/response count mismatch: "\n'
            '            f"{len(tasks)} tasks but {len(responses)} responses"\n'
            "        )\n",
            count=1,
            label="cross-page response cardinality",
        )
        source = _replace_exact(
            source,
            "    prompts = [t.prompt for t in tasks]\n"
            "    try:\n"
            "        responses = batch_predict_fn(prompts)\n"
            "    except Exception as e:\n"
            '        logger.warning("VLM batch predict failed for cross-page table merge: {}", e)\n'
            "        return\n\n"
            "    _apply_merge_results(results, tasks, responses)\n",
            "    prompts = [t.prompt for t in tasks]\n"
            "    responses = batch_predict_fn(prompts)\n"
            "    _apply_merge_results(results, tasks, responses)\n",
            count=1,
            label="cross-page synchronous failure visibility",
        )
        return _replace_exact(
            source,
            "    prompts = [t.prompt for t in tasks]\n"
            "    try:\n"
            "        responses = await aio_batch_predict_fn(prompts)\n"
            "    except Exception as e:\n"
            '        logger.warning("VLM batch predict failed for cross-page table merge: {}", e)\n'
            "        return\n\n"
            "    _apply_merge_results(results, tasks, responses)\n",
            "    prompts = [t.prompt for t in tasks]\n"
            "    responses = await aio_batch_predict_fn(prompts)\n"
            "    _apply_merge_results(results, tasks, responses)\n",
            count=1,
            label="cross-page asynchronous failure visibility",
        )

    if relative_path == "mineru/cli/fast_api.py":
        source = _replace_exact_fixture_optional(
            source,
            "    task_id = str(uuid.uuid4())\n"
            "    task_output_dir = create_task_output_dir(task_id)\n"
            '    uploads_dir = os.path.join(task_output_dir, "uploads")\n'
            "    task_manager = get_task_manager()\n",
            "    task_manager = get_task_manager()\n"
            "    task_id = str(uuid.uuid4())\n"
            "    protocol_record = None\n"
            "    identities = (request_options.agent_idempotency_key, request_options.agent_attempt_identity, request_options.agent_fence_identity)\n"
            "    if not all(isinstance(item, str) and item for item in identities):\n"
            '        raise HTTPException(status_code=400, detail="Task protocol v2 identities are required")\n'
            "    try:\n"
            "        protocol_record, created = task_manager.task_protocol_v2.reconcile_or_create(\n"
            "            idempotency_key=request_options.agent_idempotency_key, task_id=task_id,\n"
            "            attempt_identity=request_options.agent_attempt_identity, fence_identity=request_options.agent_fence_identity,\n"
            "        )\n"
            "    except TaskProtocolConflict as exc:\n"
            "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
            "    if not created:\n"
            "        existing = task_manager.get(protocol_record.task_id)\n"
            "        if existing is None:\n"
            '            raise HTTPException(status_code=409, detail="Reconciled task route is unavailable")\n'
            "        return existing\n"
            "    task_id = protocol_record.task_id\n"
            "    task_output_dir = create_task_output_dir(task_id)\n"
            '    uploads_dir = os.path.join(task_output_dir, "uploads")\n',
            count=1,
            label="FastAPI task protocol pre-allocation reconcile",
        )
        source = _replace_exact_fixture_optional(
            source,
            "from dataclasses import dataclass\n",
            "from dataclasses import asdict, dataclass\n",
            count=1,
            label="FastAPI task protocol serialization import",
        )
        source = _replace_exact_fixture_optional(
            source,
            "from mineru.cli.api_request import ParseRequestOptions, parse_request_form\n",
            "from mineru.cli.api_request import ParseRequestOptions, parse_request_form\n"
            "from mineru.cli.agent_task_protocol_v2 import (\n"
            "    DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict,\n"
            "    TaskRegistryPersistenceError,\n"
            "    evict_consumed_routes, task_protocol_runtime_status,\n"
            ")\n",
            count=1,
            label="FastAPI task protocol v2 import",
        )
        source = _replace_exact_fixture_optional(
            source,
            "import asyncio\nimport mimetypes\n",
            "import asyncio\nimport hashlib\nimport mimetypes\nimport stat\n",
            count=1,
            label="FastAPI retained result hashing import",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    completed_at: Optional[str] = None\n    error: Optional[str] = None\n",
            "    completed_at: Optional[str] = None\n"
            "    error: Optional[str] = None\n"
            "    result_artifact_path: Optional[str] = None\n"
            "    result_artifact_sha256: Optional[str] = None\n"
            "    result_artifact_bytes: Optional[int] = None\n"
            "    result_artifact_owner: Optional[str] = None\n",
            count=1,
            label="FastAPI retained result task identity",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        if queued_ahead is not None:\n"
            '            payload["queued_ahead"] = queued_ahead\n',
            "        if self.status == TASK_COMPLETED:\n"
            "            if (\n"
            "                not self.result_artifact_sha256\n"
            "                or not isinstance(self.result_artifact_bytes, int)\n"
            "                or self.result_artifact_bytes <= 0\n"
            "                or not self.result_artifact_owner\n"
            "            ):\n"
            '                raise RuntimeError("completed task has no retained result identity")\n'
            '            payload["result_artifact_sha256"] = self.result_artifact_sha256\n'
            '            payload["result_artifact_bytes"] = self.result_artifact_bytes\n'
            '            payload["result_artifact_owner"] = self.result_artifact_owner\n'
            '            payload["result_artifact_schema"] = "mineru-retained-result.v1"\n'
            "        if self.agent_idempotency_key:\n"
            '            payload["task_protocol_schema"] = "mineru-task-protocol.v2"\n'
            '            payload["idempotency_key"] = self.agent_idempotency_key\n'
            '            payload["attempt_identity"] = self.agent_attempt_identity\n'
            '            payload["fence_identity"] = self.agent_fence_identity\n'
            "        if queued_ahead is not None:\n"
            '            payload["queued_ahead"] = queued_ahead\n',
            count=1,
            label="FastAPI retained result status contract",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    return zip_path\n\n\ndef _cleanup_generated_zip_task",
            "    return zip_path\n\n\n"
            "def _hash_file(path: str) -> tuple[str, int]:\n"
            "    digest = hashlib.sha256()\n"
            "    total = 0\n"
            '    with open(path, "rb") as source_file:\n'
            "        while chunk := source_file.read(1024 * 1024):\n"
            "            digest.update(chunk)\n"
            "            total += len(chunk)\n"
            "    return digest.hexdigest(), total\n\n\n"
            "def _retained_result_sources(task: AsyncParseTask, *, byte_budget: int):\n"
            "    if type(byte_budget) is not int or byte_budget < 1:\n"
            "        raise ValueError(\"result byte budget must be a positive integer\")\n"
            "    budget = byte_budget\n"
            "    candidates = []\n"
            "    for pdf_name in task.file_names:\n"
            "        parse_dir = get_parse_dir(task.output_dir, pdf_name, task.backend, task.parse_method)\n"
            "        selected = []\n"
            "        if task.return_md:\n"
            "            selected.append(f\"{pdf_name}.md\")\n"
            "        if task.return_middle_json:\n"
            "            selected.append(f\"{pdf_name}_middle.json\")\n"
            "        if task.return_model_output:\n"
            "            selected.append(f\"{pdf_name}_model.json\")\n"
            "        if task.return_content_list:\n"
            "            selected.extend([f\"{pdf_name}_content_list.json\", f\"{pdf_name}_content_list_v2.json\"])\n"
            "        for name in selected:\n"
            "            path = os.path.join(parse_dir, name)\n"
            "            if os.path.exists(path):\n"
            "                candidates.append((path, build_zip_arcname(pdf_name, parse_dir, name)))\n"
            "        if task.return_images:\n"
            "            for path in get_images_dir_image_paths(os.path.join(parse_dir, \"images\")):\n"
            "                candidates.append((path, build_zip_arcname(pdf_name, parse_dir, os.path.join(\"images\", os.path.basename(path)))))\n"
            "        if task.return_original_file:\n"
            "            prefix = f\"{pdf_name}_origin.\"\n"
            "            for path in sorted(Path(parse_dir).iterdir()):\n"
            "                if path.is_file() and path.name.startswith(prefix):\n"
            "                    candidates.append((str(path), build_zip_arcname(pdf_name, parse_dir, path.name)))\n"
            "    if len(candidates) > 4096:\n"
            "        raise RuntimeError(\"result source member/FD envelope exceeded\")\n"
            "    if len({name for _path, name in candidates}) != len(candidates):\n"
            "        raise RuntimeError(\"result ZIP member names are not unique\")\n"
            "    source_bytes = 0\n"
            "    observations = []\n"
            "    try:\n"
            "        for path, arcname in sorted(candidates, key=lambda item: item[1]):\n"
            "            descriptor = os.open(path, os.O_RDONLY | getattr(os, \"O_NOFOLLOW\", 0))\n"
            "            descriptor_error = None\n"
            "            try:\n"
            "                metadata = os.fstat(descriptor)\n"
            "                identity = (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)\n"
            "                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:\n"
            "                    raise RuntimeError(\"result source tree identity is unsafe\")\n"
            "                source_bytes += metadata.st_size\n"
            "                observations.append((path, arcname, descriptor, identity))\n"
            "                descriptor = -1\n"
            "            except BaseException as exc:\n"
            "                descriptor_error = exc\n"
            "                raise\n"
            "            finally:\n"
            "                if descriptor >= 0:\n"
            "                    try:\n"
            "                        os.close(descriptor)\n"
            "                    except BaseException as close_error:\n"
            "                        if descriptor_error is None:\n"
            "                            raise\n"
            "                        descriptor_error.add_note(f\"result source cleanup failed: {close_error!r}\")\n"
            "        if source_bytes * 2 + len(observations) * 65536 + 1048576 > budget:\n"
            "            raise RuntimeError(\"result source tree exceeds reserved ZIP envelope\")\n"
            "        return budget, observations\n"
            "    except BaseException as primary_error:\n"
            "        for _path, _arcname, descriptor, _identity in observations:\n"
            "            try:\n"
            "                os.close(descriptor)\n"
            "            except BaseException as close_error:\n"
            "                primary_error.add_note(f\"result source cleanup failed: {close_error!r}\")\n"
            "        raise\n\n\n"
            "def _verify_and_close_result_sources(observations) -> None:\n"
            "    closing_observations = tuple(observations)\n"
            "    observations.clear()\n"
            "    failure = None\n"
            "    for path, _arcname, descriptor, expected in closing_observations:\n"
            "        try:\n"
            "            current = os.fstat(descriptor)\n"
            "            by_path = os.stat(path, follow_symlinks=False)\n"
            "            observed = (current.st_dev, current.st_ino, current.st_mode, current.st_nlink, current.st_size, current.st_mtime_ns, current.st_ctime_ns)\n"
            "            path_identity = (by_path.st_dev, by_path.st_ino, by_path.st_mode, by_path.st_nlink, by_path.st_size, by_path.st_mtime_ns, by_path.st_ctime_ns)\n"
            "            if observed != expected or path_identity != expected:\n"
            "                raise RuntimeError(\"result source changed during ZIP generation\")\n"
            "        except BaseException as exc:\n"
            "            if failure is None:\n"
            "                failure = exc\n"
            "        finally:\n"
            "            try:\n"
            "                os.close(descriptor)\n"
            "            except BaseException as exc:\n"
            "                if failure is None:\n"
            "                    failure = exc\n"
            "    if failure is not None:\n"
            "        raise failure\n\n\n"
            "def _write_retained_zip_from_fds(observations, target: str, budget: int) -> None:\n"
            "    import zipfile\n"
            "    with zipfile.ZipFile(target, \"x\", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:\n"
            "        for _path, arcname, descriptor, expected in observations:\n"
            "            os.lseek(descriptor, 0, os.SEEK_SET)\n"
            "            info = zipfile.ZipInfo(arcname.replace(os.sep, \"/\"), date_time=(1980, 1, 1, 0, 0, 0))\n"
            "            info.compress_type = zipfile.ZIP_DEFLATED\n"
            "            info.external_attr = (stat.S_IFREG | 0o600) << 16\n"
            "            total = 0\n"
            "            with archive.open(info, \"w\", force_zip64=True) as member:\n"
            "                while chunk := os.read(descriptor, 1024 * 1024):\n"
            "                    total += len(chunk)\n"
            "                    if total > expected[4]:\n"
            "                        raise RuntimeError(\"result source exceeded closed receipt\")\n"
            "                    member.write(chunk)\n"
            "            if total != expected[4]:\n"
            "                raise RuntimeError(\"result source truncated after receipt\")\n"
            "    if os.path.getsize(target) > budget:\n"
            "        raise RuntimeError(\"retained result exceeded reserved bytes\")\n"
            "    descriptor = os.open(target, os.O_RDONLY | getattr(os, \"O_NOFOLLOW\", 0))\n"
            "    try:\n"
            "        os.fsync(descriptor)\n"
            "    finally:\n"
            "        os.close(descriptor)\n\n\n"
            "async def build_retained_task_result(task: AsyncParseTask, *, byte_budget: int) -> None:\n"
            "    source_observations = []\n"
            "    primary_error = None\n"
            '    retained = os.path.join(task.output_dir, ".retained-result.zip")\n'
            '    retained_part = retained + ".part"\n'
            "    try:\n"
            "        budget, source_observations = await to_thread_owned(\n"
            "            _retained_result_sources, task, byte_budget=byte_budget,\n"
            "            on_cancel_result=lambda result: _verify_and_close_result_sources(result[1]),\n"
            "        )\n"
            "        await to_thread_owned(_write_retained_zip_from_fds, source_observations, retained_part, budget)\n"
            "        await to_thread_owned(_verify_and_close_result_sources, source_observations)\n"
            "        source_observations = []\n"
            "        os.replace(retained_part, retained)\n"
            "        directory_fd = os.open(task.output_dir, os.O_RDONLY)\n"
            "        try:\n"
            "            os.fsync(directory_fd)\n"
            "        finally:\n"
            "            os.close(directory_fd)\n"
            "        artifact_sha256, artifact_bytes = await to_thread_owned(\n"
            "            _hash_file, retained\n"
            "        )\n"
            "        if artifact_bytes <= 0:\n"
            '            raise RuntimeError("retained result ZIP is empty")\n'
            "        task.result_artifact_path = retained\n"
            "        task.result_artifact_sha256 = artifact_sha256\n"
            "        task.result_artifact_bytes = artifact_bytes\n"
            "        task.result_artifact_owner = hashlib.sha256(\n"
            '            f"{task.task_id}\\0{artifact_sha256}\\0{artifact_bytes}".encode()\n'
            "        ).hexdigest()\n"
            "    except BaseException as exc:\n"
            "        primary_error = exc\n"
            "        cleanup_file(retained)\n"
            "        raise\n"
            "    finally:\n"
            "        closing_observations = tuple(source_observations)\n"
            "        source_observations.clear()\n"
            "        close_failure = None\n"
            "        for _path, _arcname, descriptor, _identity in closing_observations:\n"
            "            try:\n"
            "                os.close(descriptor)\n"
            "            except BaseException as close_error:\n"
            "                if primary_error is not None:\n"
            "                    primary_error.add_note(f\"result source cleanup failed: {close_error!r}\")\n"
            "                elif close_failure is None:\n"
            "                    close_failure = close_error\n"
            "                else:\n"
            "                    close_failure.add_note(f\"additional source cleanup failed: {close_error!r}\")\n"
            "        cleanup_file(retained_part)\n"
            "        if close_failure is not None:\n"
            "            raise close_failure\n\n\n"
            "def _cleanup_generated_zip_task",
            count=1,
            label="FastAPI retained result builder",
        )
        source = _replace_exact(
            source,
            "from mineru.utils.config_reader import (\n"
            "    get_max_concurrent_requests as read_max_concurrent_requests,\n"
            "    get_processing_window_size,\n"
            ")\n",
            "from mineru.utils.config_reader import (\n"
            "    get_max_concurrent_requests as read_max_concurrent_requests,\n"
            ")\n"
            "from mineru.utils.model_utils import strict_processing_window_size, to_thread_owned\n",
            count=1,
            label="FastAPI strict processing window import",
        )
        source = _replace_exact(
            source,
            "def get_max_concurrent_requests() -> int:\n"
            "    return _configured_max_concurrent_requests\n\n\n"
            "def get_task_retention_seconds() -> int:\n",
            "def get_max_concurrent_requests() -> int:\n"
            "    if _configured_max_concurrent_requests != 1:\n"
            "        raise RuntimeError(\n"
            "            \"serial MinerU requires exactly one active task slot\"\n"
            "        )\n"
            "    return 1\n\n\n"
            "def get_max_pending_tasks() -> int:\n"
            "    raw = os.getenv(\"MINERU_API_MAX_PENDING_TASKS\")\n"
            "    if raw != \"1\":\n"
            "        raise RuntimeError(\n"
            "            \"MINERU_API_MAX_PENDING_TASKS must be explicitly configured \"\n"
            "            \"to 1 for serial execution\"\n"
            "        )\n"
            "    requested = int(raw)\n"
            "    if requested < get_max_concurrent_requests():\n"
            "        raise RuntimeError(\n"
            '            "MINERU_API_MAX_PENDING_TASKS must be >= active task slots"\n'
            "        )\n"
            "    return requested\n\n\n"
            "def get_task_retention_seconds() -> int:\n",
            count=1,
            label="FastAPI strict pending admission depth",
        )
        source = _replace_exact(
            source,
            "        self.queue: asyncio.Queue[str] = asyncio.Queue()\n",
            "        self.max_nonterminal_tasks = get_max_pending_tasks()\n"
            "        self.queue: asyncio.Queue[str] = asyncio.Queue(\n"
            "            maxsize=self.max_nonterminal_tasks\n"
            "        )\n",
            count=1,
            label="FastAPI bounded task queue",
        )
        source = _replace_exact(
            source,
            "    async def shutdown(self) -> None:\n"
            "        self.is_shutting_down = True\n"
            "        self._wake_waiters()\n"
            "        if self.dispatcher_task is not None:\n"
            "            self.dispatcher_task.cancel()\n"
            "            with suppress(asyncio.CancelledError):\n"
            "                await self.dispatcher_task\n"
            "            self.dispatcher_task = None\n"
            "        if self.cleanup_task is not None:\n"
            "            self.cleanup_task.cancel()\n"
            "            with suppress(asyncio.CancelledError):\n"
            "                await self.cleanup_task\n"
            "            self.cleanup_task = None\n\n"
            "        pending = list(self.active_tasks)\n"
            "        for processor in pending:\n"
            "            processor.cancel()\n"
            "        if pending:\n"
            "            await asyncio.gather(*pending, return_exceptions=True)\n"
            "        self.active_tasks.clear()\n\n"
            "    async def submit(self, task: AsyncParseTask) -> None:\n"
            "        task.submit_order = self._next_submit_order\n"
            "        self._next_submit_order += 1\n"
            "        self.tasks[task.task_id] = task\n"
            "        self.task_events[task.task_id] = asyncio.Event()\n"
            "        await self.queue.put(task.task_id)\n",
            "    async def shutdown(self) -> None:\n"
            "        self.is_shutting_down = True\n"
            "        self._wake_waiters()\n"
            "        await self.queue.join()\n"
            "        pending = tuple(self.active_tasks)\n"
            "        if pending:\n"
            "            await asyncio.gather(*pending)\n"
            "        nonterminal = [\n"
            "            task.task_id for task in self.tasks.values()\n"
            "            if not is_task_terminal(task.status)\n"
            "        ]\n"
            "        if nonterminal:\n"
            "            raise RuntimeError(\n"
            '                "accepted tasks did not reach terminal state during shutdown"\n'
            "            )\n"
            "        if self.dispatcher_task is not None:\n"
            "            self.dispatcher_task.cancel()\n"
            "            with suppress(asyncio.CancelledError):\n"
            "                await self.dispatcher_task\n"
            "            self.dispatcher_task = None\n"
            "        if self.cleanup_task is not None:\n"
            "            self.cleanup_task.cancel()\n"
            "            with suppress(asyncio.CancelledError):\n"
            "                await self.cleanup_task\n"
            "            self.cleanup_task = None\n"
            "        self.active_tasks.clear()\n\n"
            "    async def submit(self, task: AsyncParseTask) -> None:\n"
            "        if self.is_shutting_down:\n"
            "            raise HTTPException(\n"
            '                status_code=503, detail="Task manager is shutting down"\n'
            "            )\n"
            "        nonterminal = sum(\n"
            "            not is_task_terminal(item.status) for item in self.tasks.values()\n"
            "        )\n"
            "        if nonterminal >= self.max_nonterminal_tasks:\n"
            "            raise HTTPException(\n"
            '                status_code=429, detail="Task admission capacity exhausted"\n'
            "            )\n"
            "        task.submit_order = self._next_submit_order\n"
            "        self._next_submit_order += 1\n"
            "        self.tasks[task.task_id] = task\n"
            "        self.task_events[task.task_id] = asyncio.Event()\n"
            "        try:\n"
            "            self.queue.put_nowait(task.task_id)\n"
            "        except asyncio.QueueFull as exc:\n"
            "            self.tasks.pop(task.task_id, None)\n"
            "            self.task_events.pop(task.task_id, None)\n"
            "            raise HTTPException(\n"
            '                status_code=429, detail="Task admission queue is full"\n'
            "            ) from exc\n",
            count=1,
            label="FastAPI admission and quiescent shutdown",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        self.active_tasks.clear()\n\n"
            "    async def submit(self, task: AsyncParseTask) -> None:\n",
            "        self.active_tasks.clear()\n"
            "        self.task_protocol_v2.cleanup_consumed()\n\n"
            "    async def submit(self, task: AsyncParseTask) -> None:\n",
            count=1,
            label="FastAPI retained result shutdown cleanup",
        )
        source = _replace_exact(
            source,
            "                processor.add_done_callback(self._on_processor_done)\n"
            "                self.queue.task_done()\n",
            "                processor.add_done_callback(self._on_processor_done)\n",
            count=1,
            label="FastAPI queue completion ownership",
        )
        source = _replace_exact(
            source,
            "        except asyncio.CancelledError:\n"
            "            raise\n"
            "        except Exception as exc:\n"
            "            task.status = TASK_FAILED\n"
            "            task.error = str(exc)\n"
            "            task.completed_at = utc_now_iso()\n"
            "            self._signal_task_event(task_id)\n"
            '            logger.exception(f"Async task failed: {task_id}")\n\n'
            "    async def _run_task(self, task: AsyncParseTask) -> None:\n",
            "        except asyncio.CancelledError:\n"
            "            task.status = TASK_FAILED\n"
            '            task.error = "Task processor was cancelled"\n'
            "            task.completed_at = utc_now_iso()\n"
            "            self._signal_task_event(task_id)\n"
            "            raise\n"
            "        except Exception as exc:\n"
            "            task.status = TASK_FAILED\n"
            "            task.error = str(exc)\n"
            "            task.completed_at = utc_now_iso()\n"
            "            self._signal_task_event(task_id)\n"
            '            logger.exception(f"Async task failed: {task_id}")\n'
            "        finally:\n"
            "            self.queue.task_done()\n\n"
            "    async def _run_task(self, task: AsyncParseTask) -> None:\n",
            count=1,
            label="FastAPI terminal processor completion",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        task.status = TASK_COMPLETED\n"
            "        task.completed_at = utc_now_iso()\n",
            "        await build_retained_task_result(task)\n"
            "        task.status = TASK_COMPLETED\n"
            "        task.completed_at = utc_now_iso()\n",
            count=1,
            label="FastAPI retain result before completed",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    return await build_result_response(\n"
            "        background_tasks=background_tasks,\n"
            "        status_code=200,\n"
            "        output_dir=task.output_dir,\n"
            "        pdf_file_names=task.file_names,\n"
            "        backend=task.backend,\n"
            "        parse_method=task.parse_method,\n"
            "        return_md=task.return_md,\n"
            "        return_middle_json=task.return_middle_json,\n"
            "        return_model_output=task.return_model_output,\n"
            "        return_content_list=task.return_content_list,\n"
            "        return_images=task.return_images,\n"
            "        response_format_zip=task.response_format_zip,\n"
            "        return_original_file=task.return_original_file,\n"
            '        zip_filename=f"{task.task_id}.zip",\n'
            "    )\n",
            "    if (\n"
            "        not task.result_artifact_path\n"
            "        or not task.result_artifact_sha256\n"
            "        or not task.result_artifact_owner\n"
            "        or not os.path.isfile(task.result_artifact_path)\n"
            "    ):\n"
            '        raise HTTPException(status_code=410, detail="Retained task result is unavailable")\n'
            "    result_path = task.result_artifact_path\n"
            "    if not task.agent_idempotency_key:\n"
            '        raise HTTPException(status_code=410, detail="Task protocol result owner is absent")\n'
            "    try:\n"
            "        result_path = str(task_manager.task_protocol_v2.acquire_result(task.agent_idempotency_key))\n"
            "    except TaskProtocolConflict as exc:\n"
            "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
            "    background_tasks.add_task(task_manager.task_protocol_v2.release_result, task.agent_idempotency_key)\n"
            "    return FileResponse(\n"
            "        path=result_path,\n"
            '        media_type="application/zip",\n'
            '        filename=f"{task.task_id}.zip",\n'
            "        status_code=200,\n"
            "        headers={\n"
            '            "X-MinerU-Result-SHA256": task.result_artifact_sha256,\n'
            '            "X-MinerU-Result-Owner": task.result_artifact_owner,\n'
            "        },\n"
            "    )\n",
            count=1,
            label="FastAPI immutable retained result endpoint",
        )
        source = _replace_exact(
            source,
            "        \"max_concurrent_requests\": get_max_concurrent_requests(),\n"
            "        \"processing_window_size\": get_processing_window_size(\n"
            "            default=DEFAULT_PROCESSING_WINDOW_SIZE\n"
            "        ),\n",
            "        \"max_concurrent_requests\": get_max_concurrent_requests(),\n"
            "        \"max_pending_tasks_requested\": get_max_pending_tasks(),\n"
            "        \"max_pending_tasks_effective\": task_manager.max_nonterminal_tasks,\n"
            '        "task_protocol_schema": "mineru-task-protocol.v2",\n'
            '        "task_protocol_runtime": task_protocol_runtime_status(\n'
            "            task_manager.task_protocol_v2, task_manager.task_protocol_executor\n"
            "        ),\n"
            "        \"processing_window_size\": strict_processing_window_size(),\n",
            count=1,
            label="FastAPI pending depth health identity",
        )
        source = _replace_exact(
            source,
            '@app.get(path="/health")\n',
            '@app.get(path="/agent/telemetry/http-requests/v1", include_in_schema=False)\n'
            "async def agent_http_request_telemetry():\n"
            "    from mineru_vl_utils.vlm_client.http_client import _process_async_request_snapshot\n"
            "    return JSONResponse(\n"
            "        content=_process_async_request_snapshot(),\n"
            '        headers={"Cache-Control": "no-store"},\n'
            "    )\n\n\n"
            '@app.get(path="/agent/telemetry/pressure/v1", include_in_schema=False)\n'
            "async def agent_process_pressure_telemetry():\n"
            "    manager = get_task_manager()\n"
            "    observer = getattr(manager, 'capacity_observer', None)\n"
            "    if observer is None:\n"
            "        raise HTTPException(status_code=503, detail='explicit capacity pressure unavailable')\n"
            "    return JSONResponse(content=observer.pressure_snapshot(),\n"
            '                        headers={"Cache-Control": "no-store"})\n\n\n'
            '@app.get(path="/health")\n',
            count=1,
            label="FastAPI same-process outgoing HTTP telemetry",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        return task.to_status_payload(\n"
            "            request,\n"
            "            queued_ahead=self.get_queued_ahead(task.task_id),\n"
            "        )\n",
            "        payload = task.to_status_payload(\n"
            "            request,\n"
            "            queued_ahead=self.get_queued_ahead(task.task_id),\n"
            "        )\n"
            "        if not task.agent_idempotency_key:\n"
            '            raise RuntimeError("Task protocol route identity is absent")\n'
            "        record = self.task_protocol_v2.get(task.agent_idempotency_key)\n"
            "        if record is None:\n"
            '            raise RuntimeError("Task protocol route disappeared")\n'
            '        payload["task_protocol_schema"] = "mineru-task-protocol.v2"\n'
            '        payload["protocol_state"] = record.state\n'
            '        payload["idempotency_key"] = record.idempotency_key\n'
            "        return payload\n",
            count=1,
            label="FastAPI task protocol status identity",
        )
        source = _replace_exact_fixture_optional(
            source,
            '@app.get(path="/tasks/{task_id}/result", name="get_async_task_result")\n',
            '@app.get(path="/tasks/by-idempotency/{idempotency_key}", name="reconcile_async_task")\n'
            "async def reconcile_async_task(idempotency_key: str, request: Request):\n"
            "    task_manager = get_task_manager()\n"
            "    record = task_manager.task_protocol_v2.get(idempotency_key)\n"
            "    task = None if record is None else task_manager.get(record.task_id)\n"
            "    if task is None:\n"
            '        raise HTTPException(status_code=404, detail="Task not found")\n'
            "    return task_manager.build_status_payload(task, request)\n\n"
            '@app.post(path="/tasks/{task_id}/lease", name="lease_async_task_result")\n'
            "async def lease_async_task_result(task_id: str, seconds: int = 300):\n"
            "    task_manager = get_task_manager()\n"
            "    if not 1 <= seconds <= 3600:\n"
            '        raise HTTPException(status_code=400, detail="Task protocol lease is invalid")\n'
            "    record = task_manager.task_protocol_v2.get_by_task_id(task_id)\n"
            "    if record is None:\n"
            '        raise HTTPException(status_code=404, detail="Task not found")\n'
            "    try:\n"
            "        lease_until = task_manager.task_protocol_v2.lease(record.idempotency_key, seconds=seconds)\n"
            "    except TaskProtocolConflict as exc:\n"
            "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
            '    return {"schema": "mineru-task-protocol.v2", "task_id": task_id, "lease_until_unix": lease_until}\n\n'
            '@app.post(path="/tasks/{task_id}/ack", name="ack_async_task_result")\n'
            "async def ack_async_task_result(task_id: str):\n"
            "    task_manager = get_task_manager()\n"
            "    record = task_manager.task_protocol_v2.get_by_task_id(task_id)\n"
            "    if record is None:\n"
            '        raise HTTPException(status_code=404, detail="Task not found")\n'
            "    try:\n"
            "        if record.state == \"failed\":\n"
            "            task_manager.task_protocol_v2.acknowledge_failed(record.idempotency_key)\n"
            "        else:\n"
            "            task_manager.task_protocol_v2.acknowledge(record.idempotency_key)\n"
            "        task_manager.task_protocol_v2.cleanup_consumed()\n"
            "        task_manager._evict_consumed_protocol_tasks()\n"
            "    except TaskProtocolConflict as exc:\n"
            "        raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
            '    return {"schema": "mineru-task-protocol.v2", "task_id": task_id, "status": "consumed"}\n\n'
            '@app.get(path="/tasks/{task_id}/result", name="get_async_task_result")\n',
            count=1,
            label="FastAPI task protocol reconcile lease ACK routes",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    error: Optional[str] = None\n"
            "    result_artifact_path: Optional[str] = None\n",
            "    error: Optional[str] = None\n"
            "    agent_idempotency_key: Optional[str] = None\n"
            "    agent_attempt_identity: Optional[str] = None\n"
            "    agent_fence_identity: Optional[str] = None\n"
            "    result_artifact_path: Optional[str] = None\n",
            count=1,
            label="FastAPI task protocol durable identities",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    except HTTPException:\n"
            "        cleanup_file(task_output_dir)\n"
            "        raise\n"
            "    except Exception:\n"
            "        cleanup_file(task_output_dir)\n"
            "        raise\n\n\n"
            "class AsyncTaskManager:\n",
            "    except HTTPException:\n"
            "        if protocol_record is not None:\n"
            "            with suppress(TaskProtocolConflict):\n"
            "                task_manager.task_protocol_v2.abandon_unbound(protocol_record.idempotency_key)\n"
            "        cleanup_file(task_output_dir)\n"
            "        raise\n"
            "    except Exception:\n"
            "        if protocol_record is not None:\n"
            "            with suppress(TaskProtocolConflict):\n"
            "                task_manager.task_protocol_v2.abandon_unbound(protocol_record.idempotency_key)\n"
            "        cleanup_file(task_output_dir)\n"
            "        raise\n\n\n"
            "class AsyncTaskManager:\n",
            count=1,
            label="FastAPI task protocol failed allocation rollback",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        self._next_submit_order = 1\n",
            "        self._next_submit_order = 1\n"
            '        protocol_root = get_output_root() / ".agent-task-protocol-v2"\n'
            "        self.task_protocol_v2 = DurableTaskRegistry(\n"
            '            protocol_root / "registry.json",\n'
            "            max_unacked_result_bytes=int(\n"
            '                os.getenv("MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES", "2147483648")\n'
            "            ),\n"
            "            output_root=get_output_root(),\n"
            "            tombstone_retention_seconds=int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_TOMBSTONE_RETENTION_SECONDS\", \"86400\")),\n"
            "            enforce_key_lifecycle=True,\n"
            "        )\n"
            "        self.task_protocol_executor = SplitTaskExecutor(\n"
            "            parse_slots=get_max_concurrent_requests(), finalizer_slots=1,\n"
            "            result_reservation_bytes=int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES\", \"268435456\")),\n"
            "        )\n",
            count=1,
            label="FastAPI task protocol manager ownership",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        if self.dispatcher_task is None or self.dispatcher_task.done():\n",
            "        self.task_protocol_v2.cleanup_consumed()\n"
            "        for payload in self.task_protocol_v2.recoverable_payloads():\n"
            "            task = AsyncParseTask(**payload)\n"
            "            self.tasks[task.task_id] = task\n"
            "            self.task_events[task.task_id] = asyncio.Event()\n"
            "            if task.status not in TASK_TERMINAL_STATES:\n"
            "                task.status = TASK_PENDING\n"
            "                self.queue.put_nowait(task.task_id)\n"
            "        if self.dispatcher_task is None or self.dispatcher_task.done():\n",
            count=1,
            label="FastAPI task protocol restart recovery",
        )
        source = _replace_exact_fixture_optional(
            source,
            "            upload_names=[upload.original_name for upload in uploads],\n"
            "            uploads=[upload.path for upload in uploads],\n"
            "        )\n"
            "        await task_manager.submit(task)\n",
            "            upload_names=[upload.original_name for upload in uploads],\n"
            "            uploads=[upload.path for upload in uploads],\n"
            "            agent_idempotency_key=request_options.agent_idempotency_key,\n"
            "            agent_attempt_identity=request_options.agent_attempt_identity,\n"
            "            agent_fence_identity=request_options.agent_fence_identity,\n"
            "        )\n"
            "        task_manager.task_protocol_v2.bind_task_payload(task.agent_idempotency_key, asdict(task))\n"
            "        await task_manager.submit(task)\n",
            count=1,
            label="FastAPI task protocol pre-submit reconcile",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        try:\n"
            "            if _request_semaphore is not None:\n"
            "                async with _request_semaphore:\n"
            "                    await self._run_task(task)\n"
            "            else:\n"
            "                await self._run_task(task)\n",
            "        try:\n"
            "            if not task.agent_idempotency_key:\n"
            '                raise RuntimeError("Task protocol route identity is absent")\n'
            "            async def parse_stage():\n"
            "                await self._run_parse_stage(task)\n"
            "            async def finalizer_stage():\n"
            "                await build_retained_task_result(task)\n"
            "                return (Path(task.result_artifact_path), task.result_artifact_sha256, task.result_artifact_bytes, task.result_artifact_owner)\n"
            "            await self.task_protocol_executor.run(\n"
            "                registry=self.task_protocol_v2, key=task.agent_idempotency_key,\n"
            "                parse=parse_stage, finalize=finalizer_stage,\n"
            "            )\n"
            "            task.status = TASK_COMPLETED\n"
            "            task.completed_at = utc_now_iso()\n"
            "            self._signal_task_event(task.task_id)\n",
            count=1,
            label="FastAPI task protocol split parse and finalizer",
        )
        source = _replace_exact_fixture_optional(
            source,
            "    async def _run_task(self, task: AsyncParseTask) -> None:\n"
            "        task.status = TASK_PROCESSING\n",
            "    async def _run_parse_stage(self, task: AsyncParseTask) -> None:\n"
            "        task.status = TASK_PROCESSING\n",
            count=1,
            label="FastAPI task protocol parse stage",
        )
        source = _replace_exact_fixture_optional(
            source,
            "        await build_retained_task_result(task)\n"
            "        task.status = TASK_COMPLETED\n"
            "        task.completed_at = utc_now_iso()\n"
            "        self._signal_task_event(task.task_id)\n\n"
            "    def cleanup_expired_tasks(self) -> int:\n",
            "\n    def _evict_consumed_protocol_tasks(self) -> int:\n"
            "        return evict_consumed_routes(\n"
            "            self.task_protocol_v2, self.tasks, self.task_events\n"
            "        )\n\n"
            "    def cleanup_expired_tasks(self) -> int:\n"
            "        cleaned = self.task_protocol_v2.cleanup_consumed()\n"
            "        self._evict_consumed_protocol_tasks()\n"
            "        return cleaned\n",
            count=1,
            label="FastAPI task protocol cleanup ownership",
        )
        source = _patch_registry_persistence_behavior(source)
        source = _patch_admission_responsibility(source)
        source = _patch_explicit_capacity(_patch_result_capacity_before_parse(source))
        return _patch_api_gc_lifecycle(_patch_health_durable_view_observation(_patch_service_scope_completion(_patch_service_io_pressure(_patch_service_io_shutdown(_patch_service_io_ingress(_patch_service_io_ack_health(_patch_service_io_manager(source))))))))

    if relative_path == "mineru/utils/model_utils.py":
        source = _replace_exact(
            source,
            "import math\nimport os\nimport time\nimport gc\n",
            "import asyncio\n"
            "import ctypes\n"
            "from dataclasses import dataclass\n"
            "from functools import lru_cache\n"
            "import hashlib\n"
            "import json\n"
            "import math\n"
            "import os\n"
            "import stat\n"
            "import sys\n"
            "import threading\n"
            "import time\n"
            "import uuid\n"
            "import weakref\n"
            "import gc\n"
            "if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ\n"
            "        or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):\n"
            "    from mineru.cli.agent_capacity_bootstrap import get_process_capacity\n"
            "    get_process_capacity()  # Validate startup before torch/model imports.\n",
            count=1,
            label="model-utils imports",
        )
        helper = '''_PHASE_TRACE_PREFIX = "MINERU_PHASE_TRACE "
_PHASE_TRACE_SCHEMA = "mineru-phase-trace.v4"
_PHASE_TRACE_BACKENDS = frozenset({"hybrid", "vlm"})
_PHASE_TRACE_PIPELINE_MODES = frozenset({"serial"})
_PHASE_TRACE_PHASES = frozenset({
    "document",
    "document_finalize",
    "window_append",
    "window_layout",
    "window_postprocess",
    "window_render",
    "window_total",
    "window_vlm",
})
_PHASE_TRACE_OUTPUT_LOCK = threading.Lock()
_PHASE_TRACE_PROCESS_EPOCH = uuid.uuid4().hex
_SERIAL_PROFILE_SCHEMA = "mineru-serial-execution-profile.v1"
_SERIAL_PROCESSING_WINDOW_SIZE = 16


def is_phase_trace_enabled() -> bool:
    """Return the default-off, closed-vocabulary phase-trace switch."""
    value = os.getenv("MINERU_PHASE_TRACE")
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("MINERU_PHASE_TRACE has an invalid value")


def strict_processing_window_size() -> int:
    """Require the selected explicit window without an implicit fallback."""
    if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ
            or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        return get_process_capacity().processing_window_size
    raw = os.getenv("MINERU_PROCESSING_WINDOW_SIZE")
    if raw is None or not raw.isdigit() or str(int(raw)) != raw:
        raise RuntimeError(
            "MINERU_PROCESSING_WINDOW_SIZE must be a canonical positive integer"
        )
    value = int(raw)
    if value != _SERIAL_PROCESSING_WINDOW_SIZE:
        raise RuntimeError("MINERU_PROCESSING_WINDOW_SIZE must equal 16")
    return value


class _DisabledPhaseTrace:
    def document_started(self) -> None:
        return None

    def document_completed(self) -> None:
        return None

    def document_failed(self) -> None:
        return None

    def window(self, **_kwargs):
        return None

    def start(self) -> int:
        return 0

    def complete(self, _phase: str, _started_ns: int, **_kwargs) -> None:
        return None


_DISABLED_PHASE_TRACE = _DisabledPhaseTrace()


def _serial_integer(value, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"serial execution profile {label} is invalid")
    return value


def _serial_profile_hash(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _capacity_sha256(value, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise RuntimeError(f"capacity {label} SHA-256 is invalid")
    return value


@dataclass(frozen=True)
class SerialExecutionProfile:
    profile_id: str
    profile_sha256: str
    pipeline_mode: str
    pipeline_depth: int
    window_size: int
    max_resident_pages: int
    inner_inference_concurrency: int
    vllm_max_num_seqs: int

    @property
    def max_resident_windows(self) -> int:
        return 1

    @property
    def max_resident_decoded_bytes(self) -> int:
        return self.max_resident_pages * 3500 * 3500 * 4


def serial_execution_profile(configured_window_size: int) -> SerialExecutionProfile:
    configured_window_size = _serial_integer(
        configured_window_size,
        label="configured_window_size",
        minimum=1,
    )
    payload = {
        "inner_inference_concurrency": 7,
        "owner_task_slots": 1,
        "pipeline_depth": 0,
        "pipeline_mode": "serial",
        "profile_id": f"serial-w{configured_window_size}",
        "schema": _SERIAL_PROFILE_SCHEMA,
        "vllm_max_num_seqs": 128,
        "window_size": configured_window_size,
    }
    if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ
            or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        config = get_process_capacity()
        if configured_window_size != config.processing_window_size:
            raise RuntimeError("document window differs from the capacity config")
        payload.update(
            schema="mineru-native-document-profile.v1",
            capacity_config_sha256=config.sha256,
            owner_task_slots=config.parse_active_limit,
            inner_inference_concurrency=config.final_http_limit_per_loop,
            profile_id=f"native-w{configured_window_size}",
        )
    return SerialExecutionProfile(
        profile_id=payload["profile_id"],
        profile_sha256=_serial_profile_hash(payload),
        pipeline_mode="serial",
        pipeline_depth=0,
        window_size=configured_window_size,
        max_resident_pages=configured_window_size,
        inner_inference_concurrency=payload["inner_inference_concurrency"],
        vllm_max_num_seqs=128,
    )


def serial_runtime_status(configured_window_size: int) -> dict:
    profile = serial_execution_profile(configured_window_size)
    if ('MINERU_CAPACITY_CONFIG_PATH' in os.environ
            or 'MINERU_CAPACITY_CONFIG_SHA256' in os.environ):
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        config = get_process_capacity()
        return {
            "configured_window_size": profile.window_size,
            "mode": "serial", "owner_task_slots": config.parse_active_limit,
            "profile_sha256": profile.profile_sha256,
            "schema": "mineru-native-runtime.v1",
            "capacity_config_sha256": config.sha256,
        }
    return {
        "configured_window_size": profile.window_size,
        "mode": "serial",
        "owner_task_slots": 1,
        "profile_sha256": profile.profile_sha256,
        "schema": "mineru-serial-runtime.v1",
    }


class MinerUPhaseTrace:
    """Emit content-free interval events that remain valid under overlap."""

    def __init__(
        self,
        *,
        backend: str,
        page_count: int,
        window_size: int,
        total_windows: int,
        execution_profile: SerialExecutionProfile,
        source_pdf_bytes: int,
        hybrid_batch_ratio_requested=None,
        hybrid_batch_ratio_effective=None,
        hybrid_batch_ratio_ocr_override=None,
    ) -> None:
        if backend not in _PHASE_TRACE_BACKENDS:
            raise RuntimeError("phase trace backend is unsupported")
        pipeline_mode = execution_profile.pipeline_mode
        profile_id = execution_profile.profile_id
        if pipeline_mode not in _PHASE_TRACE_PIPELINE_MODES:
            raise RuntimeError("phase trace pipeline mode is unsupported")
        if (
            not isinstance(profile_id, str)
            or not 1 <= len(profile_id) <= 64
            or profile_id[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
            or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for char in profile_id
            )
        ):
            raise RuntimeError("phase trace profile identity is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (page_count, window_size, total_windows, source_pdf_bytes)
        ):
            raise RuntimeError("phase trace document dimensions are invalid")
        if window_size != execution_profile.window_size:
            raise RuntimeError("phase trace window/profile identity drifted")
        if (
            not isinstance(execution_profile.profile_sha256, str)
            or len(execution_profile.profile_sha256) != 71
            or not execution_profile.profile_sha256.startswith("sha256:")
            or any(
                char not in "0123456789abcdef"
                for char in execution_profile.profile_sha256[7:]
            )
        ):
            raise RuntimeError("phase trace profile hash is invalid")
        self.backend = backend
        self.page_count = page_count
        self.window_size = window_size
        self.total_windows = total_windows
        self.pipeline_mode = pipeline_mode
        self.profile_id = profile_id
        self.profile_sha256 = execution_profile.profile_sha256
        self.pipeline_depth = execution_profile.pipeline_depth
        self.source_pdf_bytes = source_pdf_bytes
        self.max_resident_pages = execution_profile.max_resident_pages
        self.max_resident_windows = execution_profile.max_resident_windows
        self.max_resident_decoded_bytes = (
            execution_profile.max_resident_decoded_bytes
        )
        self.inner_inference_concurrency = (
            execution_profile.inner_inference_concurrency
        )
        self.vllm_max_num_seqs = execution_profile.vllm_max_num_seqs
        ratio_values = (
            hybrid_batch_ratio_requested,
            hybrid_batch_ratio_effective,
        )
        if backend == "hybrid":
            if (
                any(value not in {1, 2, 4, 8} for value in ratio_values)
                or not isinstance(hybrid_batch_ratio_ocr_override, bool)
                or (
                    hybrid_batch_ratio_ocr_override
                    and hybrid_batch_ratio_effective != 1
                )
                or (
                    not hybrid_batch_ratio_ocr_override
                    and hybrid_batch_ratio_effective
                    != hybrid_batch_ratio_requested
                )
            ):
                raise RuntimeError("phase trace hybrid batch ratio is invalid")
        elif any(value is not None for value in (*ratio_values, hybrid_batch_ratio_ocr_override)):
            raise RuntimeError("VLM phase trace unexpectedly has a hybrid batch ratio")
        self.hybrid_batch_ratio_requested = hybrid_batch_ratio_requested
        self.hybrid_batch_ratio_effective = hybrid_batch_ratio_effective
        self.hybrid_batch_ratio_ocr_override = hybrid_batch_ratio_ocr_override
        self.hybrid_layout_batch_cap = (
            min(8, hybrid_batch_ratio_effective)
            if hybrid_batch_ratio_effective is not None
            else None
        )
        self.hybrid_mfr_batch_cap = (
            hybrid_batch_ratio_effective * 16
            if hybrid_batch_ratio_effective is not None
            else None
        )
        self.hybrid_ocr_det_batch_cap = (
            hybrid_batch_ratio_effective * 8
            if hybrid_batch_ratio_effective is not None
            else None
        )
        self.hybrid_table_orientation_batch_cap = self.hybrid_ocr_det_batch_cap
        self.trace_id = uuid.uuid4().hex
        self.sequence = 0
        self.document_started_ns = 0
        self.ended = False

    def _emit(
        self,
        *,
        event: str,
        phase: str,
        outcome: str,
        started_ns: int,
        ended_ns: int,
        window,
        append_index,
        credit_lease,
    ) -> None:
        if phase not in _PHASE_TRACE_PHASES:
            raise RuntimeError("phase trace phase is unsupported")
        if ended_ns < started_ns:
            raise RuntimeError("phase trace interval is invalid")
        if window is None:
            window_index = page_start = page_end_exclusive = window_page_count = None
        else:
            window_index, page_start, page_end_exclusive = window
            window_page_count = page_end_exclusive - page_start
        if credit_lease is None:
            reserved_windows = reserved_decoded_bytes = None
            actual_decoded_bytes = resident_pages_after_acquire = None
            resident_windows_after_acquire = None
            resident_decoded_bytes_after_acquire = None
        else:
            reserved_windows = credit_lease.reserved_windows
            reserved_decoded_bytes = credit_lease.reserved_decoded_bytes
            actual_decoded_bytes = credit_lease.actual_decoded_bytes
            resident_pages_after_acquire = credit_lease.resident_pages_after_acquire
            resident_windows_after_acquire = (
                credit_lease.resident_windows_after_acquire
            )
            resident_decoded_bytes_after_acquire = (
                credit_lease.resident_decoded_bytes_after_acquire
            )
        with _PHASE_TRACE_OUTPUT_LOCK:
            self.sequence += 1
            payload = {
                "append_index": append_index,
                "actual_decoded_bytes": actual_decoded_bytes,
                "backend": self.backend,
                "duration_ns": ended_ns - started_ns,
                "ended_monotonic_ns": ended_ns,
                "event": event,
                "hybrid_batch_ratio_effective": self.hybrid_batch_ratio_effective,
                "hybrid_batch_ratio_ocr_override": self.hybrid_batch_ratio_ocr_override,
                "hybrid_batch_ratio_requested": self.hybrid_batch_ratio_requested,
                "hybrid_layout_batch_cap": self.hybrid_layout_batch_cap,
                "hybrid_mfr_batch_cap": self.hybrid_mfr_batch_cap,
                "hybrid_ocr_det_batch_cap": self.hybrid_ocr_det_batch_cap,
                "hybrid_table_orientation_batch_cap": (
                    self.hybrid_table_orientation_batch_cap
                ),
                "inner_inference_concurrency": self.inner_inference_concurrency,
                "max_resident_decoded_bytes": self.max_resident_decoded_bytes,
                "max_resident_pages": self.max_resident_pages,
                "max_resident_windows": self.max_resident_windows,
                "outcome": outcome,
                "page_count": self.page_count,
                "page_end_exclusive": page_end_exclusive,
                "page_start": page_start,
                "phase": phase,
                "pipeline_depth": self.pipeline_depth,
                "pipeline_mode": self.pipeline_mode,
                "process_epoch": _PHASE_TRACE_PROCESS_EPOCH,
                "profile_id": self.profile_id,
                "profile_sha256": self.profile_sha256,
                "reserved_decoded_bytes": reserved_decoded_bytes,
                "reserved_windows": reserved_windows,
                "resident_decoded_bytes_after_acquire": (
                    resident_decoded_bytes_after_acquire
                ),
                "resident_pages_after_acquire": resident_pages_after_acquire,
                "resident_windows_after_acquire": resident_windows_after_acquire,
                "schema": _PHASE_TRACE_SCHEMA,
                "sequence": self.sequence,
                "started_monotonic_ns": started_ns,
                "source_pdf_bytes": self.source_pdf_bytes,
                "total_windows": self.total_windows,
                "trace_id": self.trace_id,
                "window_index": window_index,
                "window_page_count": window_page_count,
                "window_size": self.window_size,
                "vllm_max_num_seqs": self.vllm_max_num_seqs,
            }
            sys.stderr.write(
                _PHASE_TRACE_PREFIX
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
                + "\\n"
            )
            sys.stderr.flush()

    def document_started(self) -> None:
        if self.document_started_ns or self.ended:
            raise RuntimeError("phase trace document start drifted")
        self.document_started_ns = time.monotonic_ns()
        self._emit(
            event="document_start",
            phase="document",
            outcome="started",
            started_ns=self.document_started_ns,
            ended_ns=self.document_started_ns,
            window=None,
            append_index=None,
            credit_lease=None,
        )

    def _end_document(self, outcome: str) -> None:
        if self.ended:
            return
        if not self.document_started_ns:
            raise RuntimeError("phase trace document ended before start")
        self.ended = True
        self._emit(
            event="document_end",
            phase="document",
            outcome=outcome,
            started_ns=self.document_started_ns,
            ended_ns=time.monotonic_ns(),
            window=None,
            append_index=None,
            credit_lease=None,
        )

    def document_completed(self) -> None:
        self._end_document("success")

    def document_failed(self) -> None:
        self._end_document("error")

    def window(
        self,
        *,
        window_index: int,
        page_start: int,
        page_end_exclusive: int,
    ):
        if (
            isinstance(window_index, bool)
            or isinstance(page_start, bool)
            or isinstance(page_end_exclusive, bool)
            or not 0 <= window_index < self.total_windows
            or not 0 <= page_start < page_end_exclusive <= self.page_count
        ):
            raise RuntimeError("phase trace window dimensions are invalid")
        return (window_index, page_start, page_end_exclusive)

    def start(self) -> int:
        return time.monotonic_ns()

    def complete(
        self,
        phase: str,
        started_ns: int,
        *,
        window=None,
        outcome: str = "success",
        append_index=None,
        credit_lease=None,
    ) -> None:
        if isinstance(started_ns, bool) or not isinstance(started_ns, int):
            raise RuntimeError("phase trace start timestamp is invalid")
        if outcome not in {"success", "error"}:
            raise RuntimeError("phase trace interval outcome is invalid")
        finished_ns = time.monotonic_ns()
        if started_ns <= 0 or finished_ns < started_ns:
            raise RuntimeError("phase trace duration is invalid")
        if (append_index is not None) != (phase == "window_append"):
            raise RuntimeError("phase trace append identity is invalid")
        self._emit(
            event="interval_complete",
            phase=phase,
            outcome=outcome,
            started_ns=started_ns,
            ended_ns=finished_ns,
            window=window,
            append_index=append_index,
            credit_lease=credit_lease,
        )


def new_phase_trace(**kwargs):
    if not is_phase_trace_enabled():
        return _DISABLED_PHASE_TRACE
    return MinerUPhaseTrace(**kwargs)


class OwnedOperation:
    """Linearize cancellation with one started native or remote owner."""

    def __init__(self, awaitable) -> None:
        self.awaitable = awaitable
        self.task = None
        self.state = "new"
        self.cancel_requested = False

    async def run(self, *, on_cancel_result=None):
        if self.state != "new":
            raise RuntimeError("owned operation cannot be reused")
        self.state = "running"
        self.task = asyncio.ensure_future(self.awaitable)
        cancellation = None
        while True:
            try:
                result = await asyncio.shield(self.task)
                self.state = "settled_success"
                break
            except asyncio.CancelledError as exc:
                if self.task.cancelled():
                    self.state = "settled_error"
                    raise
                self.cancel_requested = True
                if cancellation is None:
                    cancellation = exc
                continue
            except BaseException as exc:
                self.state = "settled_error"
                if cancellation is not None:
                    cancellation.add_note(
                        f"owned operation drain failed: {type(exc).__name__}"
                    )
                    self.state = "drained"
                    raise cancellation from exc
                self.state = "drained"
                raise
        self.state = "drained"
        if cancellation is not None:
            if on_cancel_result is not None:
                try:
                    on_cancel_result(result)
                except BaseException as exc:
                    cancellation.add_note(
                        "owned operation cancellation cleanup failed: "
                        f"{type(exc).__name__}"
                    )
                    raise cancellation from exc
            raise cancellation
        return result


async def drain_owned_awaitable(awaitable, *, on_cancel_result=None):
    return await OwnedOperation(awaitable).run(
        on_cancel_result=on_cancel_result,
    )


async def to_thread_owned(function, /, *args, on_cancel_result=None, **kwargs):
    """Drain a started native call before its caller releases owned resources."""
    return await drain_owned_awaitable(
        asyncio.to_thread(function, *args, **kwargs),
        on_cancel_result=on_cancel_result,
    )


async def run_native_owned(
    native_owner,
    function,
    /,
    *args,
    on_cancel_result=None,
    **kwargs,
):
    """Serialize native A/C work and never abandon a running executor thread."""
    async with native_owner:
        return await drain_owned_awaitable(
            asyncio.to_thread(function, *args, **kwargs),
            on_cancel_result=on_cancel_result,
        )


async def run_async_owned(
    native_owner,
    awaitable_factory,
    *,
    on_cancel_result=None,
):
    """Create an async wrapper only after acquiring its native owner."""
    if not callable(awaitable_factory):
        raise RuntimeError("async owned operation requires an awaitable factory")
    async with native_owner:
        return await drain_owned_awaitable(
            awaitable_factory(),
            on_cancel_result=on_cancel_result,
        )


async def _await_inference_started(started, inference_task) -> None:
    if started.is_set():
        return
    started_task = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait(
            (started_task, inference_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if inference_task in done and not started.is_set():
            await inference_task
            raise RuntimeError("inference completed without acquiring its request owner")
        await started_task
    finally:
        if not started_task.done():
            started_task.cancel()
            await asyncio.gather(started_task, return_exceptions=True)


def is_heap_trim_enabled() -> bool:
    """Require an explicit, closed-vocabulary heap-return policy."""
    value = os.getenv("MINERU_MALLOC_TRIM")
    if value is None:
        raise RuntimeError("MINERU_MALLOC_TRIM must be explicitly configured")
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("MINERU_MALLOC_TRIM has an invalid value")


@lru_cache(maxsize=1)
def _malloc_trim():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("heap return requires Linux/glibc")
    libc = ctypes.CDLL(None)
    function = getattr(libc, "malloc_trim", None)
    if function is None:
        raise RuntimeError("glibc malloc_trim is unavailable")
    function.argtypes = [ctypes.c_size_t]
    function.restype = ctypes.c_int
    return function


def trim_process_heap() -> bool:
    """Invoke glibc heap return when enabled; never hide an enabled failure."""
    if not is_heap_trim_enabled():
        return False
    _malloc_trim()(0)
    return True


def release_document_memory_owned(device) -> None:
    """Document-end memory release for the serving loop's owned thread pool.

    Returns the CUDA caching allocator's unused blocks and the glibc heap, both of
    which release the GIL. It never runs a cyclic collection: ``gc.collect()``
    holds the GIL for the whole traversal, so from any thread it stalls the
    serving loop and with it ``/health``. Cyclic garbage is left to the automatic
    collector; the serving profile is CUDA/CPU only.
    """
    if str(device).startswith("cuda") and torch.cuda.is_available():
        torch.cuda.empty_cache()
    trim_process_heap()


'''
        helper += '''_MODEL_DEVICE_OUTPUT_PREFIX = "MINERU_MODEL_DEVICE "
_MODEL_DEVICE_LOCK = threading.Lock()
_MODEL_DEVICE_SEEN = weakref.WeakKeyDictionary()


def _model_device_metadata(module, weight_path):
    """Read metadata from the serving tensors, without inference or transfers."""
    groups = {}
    if module is not None:
        for kind in ("parameters", "buffers"):
            getter = getattr(module, kind, None)
            if getter is None:
                continue
            for tensor in getter():
                key = (kind, str(tensor.device), str(tensor.dtype))
                group = groups.setdefault(key, {"kind": kind, "device": key[1],
                                               "dtype": key[2], "tensors": 0, "elements": 0})
                group["tensors"] += 1
                group["elements"] += tensor.numel()
    path = str(weight_path) if isinstance(weight_path, (str, os.PathLike)) and str(weight_path) else None
    return {"state": "available" if groups and path is not None else "unavailable",
            "weight_path": path, "tensor_groups": [groups[key] for key in sorted(groups)]}


def record_hybrid_model_devices(model, *, role="hybrid"):
    """Once per actual serving instance/role; initialization evidence, not a probe."""
    if not is_phase_trace_enabled():
        return None
    if role not in {"hybrid", "orientation"}:
        raise ValueError("unknown Hybrid model identity role")
    # Capacity validation is authoritative, including after a successful record.
    # Keep it outside the recoverable observation-IO boundary.
    capacity = None
    if "MINERU_CAPACITY_CONFIG_PATH" in os.environ or "MINERU_CAPACITY_CONFIG_SHA256" in os.environ:
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        capacity = get_process_capacity()
    with _MODEL_DEVICE_LOCK:
        seen = _MODEL_DEVICE_SEEN.setdefault(model, set())
        if role in seen:
            return None
        try:
            event = _hybrid_model_device_event(model, role, capacity)
            with _PHASE_TRACE_OUTPUT_LOCK:
                print(_MODEL_DEVICE_OUTPUT_PREFIX + json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
        except OSError as exc:
            # A failed diagnostic read/write must not replace model inference.
            # Do not mark this instance as successfully recorded; a later call
            # may collect the evidence once the IO failure has recovered.
            logger.warning(
                f"Hybrid model device evidence unavailable for {role}: {type(exc).__name__}: {exc}"
            )
            return {"state": "unavailable", "role": role, "error_type": type(exc).__name__}
        seen.add(role)
        return event


def _hybrid_model_device_event(model, role, capacity):
    models = {}
    if role == "hybrid":
        layout = getattr(model, "layout_model", None)
        models["layout"] = _model_device_metadata(
            getattr(layout, "model", None), getattr(layout, "model_dir", None))
        mfr = getattr(getattr(model, "mfr_model", None), "model", None)
        models["mfr"] = _model_device_metadata(
            mfr, getattr(getattr(mfr, "config", None), "_name_or_path", None))
        ocr = getattr(model, "ocr_model", None)
    else:
        ocr = getattr(model, "ocr_engine", None)
    for name, attribute in (("ocr_detector", "text_detector"), ("ocr_recognizer", "text_recognizer")):
        wrapper = getattr(ocr, attribute, None)
        models[name] = _model_device_metadata(
            getattr(wrapper, "net", None), getattr(wrapper, "weights_path", None))
    with open("/proc/self/stat", encoding="utf-8") as stream:
        stat_fields = stream.read().rsplit(")", 1)[1].split()
    return {"schema": "mineru-hybrid-model-device.v1", "role": role,
            "process_id": os.getpid(), "process_start_ticks": int(stat_fields[19]),
            "process_epoch": _PHASE_TRACE_PROCESS_EPOCH,
            "capacity_config_sha256": None if capacity is None else capacity.sha256,
            "instance_id": id(model), "models": models,
            "observation": "serving_instance_initialization"}


'''
        return _replace_exact(
            source,
            "def clean_memory(device='cuda'):\n",
            helper + "def clean_memory(device='cuda'):\n",
            count=1,
            label="model-utils helper",
        )

    if relative_path == "mineru/backend/vlm/vlm_analyze.py":
        source = _replace_exact(
            source,
            "from ...utils.config_reader import get_device, get_processing_window_size\n\n"
            "from ...utils.enum_class import ImageType\n",
            "from ...utils.config_reader import get_device, get_processing_window_size\n"
            "from ...utils.model_utils import (\n"
            "    drain_owned_awaitable,\n"
            "    to_thread_owned,\n"
            "    serial_execution_profile,\n"
            "    strict_processing_window_size,\n"
            "    new_phase_trace,\n"
            "    trim_process_heap,\n"
            ")\n\n"
            "from ...utils.enum_class import ImageType\n",
            count=1,
            label="VLM import",
        )
        source = _replace_exact(
            source,
            "        configured_window_size = get_processing_window_size(default=64)\n",
            "        configured_window_size = strict_processing_window_size()\n",
            count=2,
            label="VLM strict processing window",
        )
        source = _replace_exact(
            source,
            "@contextmanager\n"
            "def predictor_execution_guard(predictor: MinerUClient):\n"
            '    lock = getattr(predictor, "_mineru_execution_lock", None)\n'
            "    if lock is None:\n"
            "        yield\n"
            "        return\n"
            "    with lock:\n"
            "        yield\n\n\n"
            "@asynccontextmanager\n"
            "async def aio_predictor_execution_guard(predictor: MinerUClient):\n"
            '    lock = getattr(predictor, "_mineru_execution_lock", None)\n'
            "    if lock is None:\n"
            "        yield\n"
            "        return\n"
            "    await asyncio.to_thread(lock.acquire)\n"
            "    try:\n"
            "        yield\n"
            "    finally:\n"
            "        lock.release()\n",
            "@contextmanager\n"
            "def predictor_execution_guard(\n"
            "    predictor: MinerUClient,\n"
            "    *,\n"
            "    phase_trace=None,\n"
            "    trace_window=None,\n"
            "):\n"
            "    phase_started_ns = phase_trace.start() if phase_trace is not None else 0\n"
            '    outcome = "success"\n'
            '    lock = getattr(predictor, "_mineru_execution_lock", None)\n'
            "    try:\n"
            "        if lock is None:\n"
            "            yield\n"
            "        else:\n"
            "            with lock:\n"
            "                yield\n"
            "    except BaseException:\n"
            '        outcome = "error"\n'
            "        raise\n"
            "    finally:\n"
            "        if phase_trace is not None:\n"
            "            phase_trace.complete(\n"
            '                "window_vlm",\n'
            "                phase_started_ns,\n"
            "                window=trace_window,\n"
            "                outcome=outcome,\n"
            "            )\n\n\n"
            "@asynccontextmanager\n"
            "async def aio_predictor_execution_guard(\n"
            "    predictor: MinerUClient,\n"
            "    *,\n"
            "    phase_trace=None,\n"
            "    trace_window=None,\n"
            "):\n"
            "    phase_started_ns = phase_trace.start() if phase_trace is not None else 0\n"
            "    outcome = \"success\"\n"
            "    lock = getattr(predictor, \"_mineru_execution_lock\", None)\n"
            "    lock_acquired = False\n"
            "    try:\n"
            "        if lock is not None:\n"
            "            await drain_owned_awaitable(\n"
            "                asyncio.to_thread(lock.acquire),\n"
            "                on_cancel_result=lambda acquired: (\n"
            "                    lock.release() if acquired else None\n"
            "                ),\n"
            "            )\n"
            "            lock_acquired = True\n"
            "        yield\n"
            "    except BaseException:\n"
            "        outcome = \"error\"\n"
            "        raise\n"
            "    finally:\n"
            "        if lock_acquired:\n"
            "            lock.release()\n"
            "        if phase_trace is not None and phase_started_ns:\n"
            "            phase_trace.complete(\n"
            '                "window_vlm",\n'
            "                phase_started_ns,\n"
            "                window=trace_window,\n"
            "                outcome=outcome,\n"
            "            )\n",
            count=1,
            label="VLM predictor phase",
        )
        source = _replace_exact(
            source,
            "    results = []\n    doc_closed = False\n    try:\n",
            "    results = []\n    phase_trace = None\n    doc_closed = False\n    try:\n",
            count=2,
            label="VLM phase trace declaration",
        )
        source = _replace_exact(
            source,
            "with predictor_execution_guard(predictor):",
            "with predictor_execution_guard(\n"
            "                        predictor,\n"
            "                        phase_trace=phase_trace,\n"
            "                        trace_window=window_trace_context,\n"
            "                    ):",
            count=1,
            label="VLM synchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "async with aio_predictor_execution_guard(predictor):",
            "async with aio_predictor_execution_guard(\n"
            "                        predictor,\n"
            "                        phase_trace=phase_trace,\n"
            "                        trace_window=window_trace_context,\n"
            "                    ):",
            count=1,
            label="VLM asynchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "        logger.info(\n"
            "            f'VLM processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        infer_start = time.time()\n",
            "        logger.info(\n"
            "            f'VLM processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        execution_profile = serial_execution_profile(\n"
            "            configured_window_size\n"
            "        )\n"
            "        phase_trace = new_phase_trace(\n"
            '            backend="vlm",\n'
            "            page_count=page_count,\n"
            "            window_size=configured_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "        )\n"
            "        phase_trace.document_started()\n\n"
            "        infer_start = time.time()\n",
            count=2,
            label="VLM document phase start",
        )
        source = _replace_exact(
            source,
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n",
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
            "                window_trace_context = phase_trace.window(\n"
            "                    window_index=window_index,\n"
            "                    page_start=window_start,\n"
            "                    page_end_exclusive=window_end + 1,\n"
            "                )\n"
            "                window_started_ns = phase_trace.start()\n"
            "                render_started_ns = phase_trace.start()\n",
            count=2,
            label="VLM window phase start",
        )
        source = _replace_exact(
            source,
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                try:\n",
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            '                    "window_render",\n'
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="VLM synchronous render phase",
        )
        source = _replace_exact(
            source,
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                try:\n",
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            '                    "window_render",\n'
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="VLM asynchronous render phase",
        )
        source = _replace_exact(
            source,
            "                    append_page_blocks_to_middle_json(\n"
            "                        middle_json,\n",
            "                    append_started_ns = phase_trace.start()\n"
            "                    append_page_blocks_to_middle_json(\n"
            "                        middle_json,\n",
            count=2,
            label="VLM append phase start",
        )
        source = _replace_exact(
            source,
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n",
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            '                        "window_append",\n'
            "                        append_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                        append_index=window_index,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n"
            "                    phase_trace.complete(\n"
            '                        "window_total",\n'
            "                        window_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n",
            count=2,
            label="VLM append and window completion",
        )
        source = _replace_exact(
            source,
            "        if not client_side_output_generation:\n"
            '            finalize_middle_json(middle_json["pdf_info"])\n'
            "        close_pdfium_document(pdf_doc)\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if not client_side_output_generation:\n"
            '            finalize_middle_json(middle_json["pdf_info"])\n'
            '        phase_trace.complete("document_finalize", finalize_started_ns)\n'
            "        close_pdfium_document(pdf_doc)\n",
            count=1,
            label="VLM synchronous finalize phase",
        )
        source = _replace_exact(
            source,
            "        if not client_side_output_generation:\n"
            '            await asyncio.to_thread(finalize_middle_json, middle_json["pdf_info"])\n'
            "        close_pdfium_document(pdf_doc)\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if not client_side_output_generation:\n"
            '            await asyncio.to_thread(finalize_middle_json, middle_json["pdf_info"])\n'
            '        phase_trace.complete("document_finalize", finalize_started_ns)\n'
            "        close_pdfium_document(pdf_doc)\n",
            count=1,
            label="VLM asynchronous finalize phase",
        )
        source = _replace_exact(
            source,
            "        doc_closed = True\n        return middle_json, results\n",
            "        doc_closed = True\n"
            "        phase_trace.document_completed()\n"
            "        trim_process_heap()\n"
            "        return middle_json, results\n",
            count=2,
            label="VLM document completion",
        )
        source = _replace_exact(
            source,
            "    finally:\n"
            "        if not doc_closed:\n"
            "            close_pdfium_document(pdf_doc)\n",
            "    finally:\n"
            "        if not doc_closed:\n"
            "            if phase_trace is not None:\n"
            "                phase_trace.document_failed()\n"
            "            close_pdfium_document(pdf_doc)\n",
            count=2,
            label="VLM document failure",
        )
        source = _patch_owned_render_await(source)
        return _replace_exact(
            source,
            "await asyncio.to_thread(",
            "await to_thread_owned(",
            count=2,
            label="VLM native model initialization and finalization drain",
        )

    if relative_path == "mineru/cli/common.py":
        # The asynchronous CLI entry runs the pdfium rewrite of every PDF, the
        # output directory creation and the whole output generation (markdown,
        # content lists, JSON dumps, file writes) on the serving loop. Each is
        # awaited on the owned thread pool instead; the synchronous do_parse
        # path and the office conversion path keep their upstream shape.
        source = _replace_exact(
            source,
            "from mineru.utils.pdf_image_tools import images_bytes_to_pdf_bytes\n",
            "from mineru.utils.model_utils import to_thread_owned\n"
            "from mineru.utils.pdf_image_tools import images_bytes_to_pdf_bytes\n",
            count=1,
            label="CLI owned thread import",
        )
        source = _replace_exact_occurrence(
            source,
            "    pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id)\n",
            "    pdf_bytes_list = await to_thread_owned(\n"
            "        _prepare_pdf_bytes, pdf_bytes_list, start_page_id, end_page_id\n"
            "    )\n",
            count=2,
            occurrence=1,
            label="CLI asynchronous PDF rewrite off the serving loop",
        )
        source = _replace_exact_occurrence(
            source,
            "        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)\n",
            "        local_image_dir, local_md_dir = await to_thread_owned(\n"
            "            prepare_env, output_dir, pdf_file_name, parse_method\n"
            "        )\n",
            count=3,
            occurrence=1,
            label="CLI asynchronous VLM output directories off the serving loop",
        )
        source = _replace_exact_occurrence(
            source,
            "        local_image_dir, local_md_dir = prepare_env("
            'output_dir, pdf_file_name, f"hybrid_{parse_method}")\n',
            "        local_image_dir, local_md_dir = await to_thread_owned(\n"
            '            prepare_env, output_dir, pdf_file_name, f"hybrid_{parse_method}"\n'
            "        )\n",
            count=2,
            occurrence=1,
            label="CLI asynchronous hybrid output directories off the serving loop",
        )
        output_call = (
            "        _process_output(\n"
            "            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,\n"
            "            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,\n"
            "            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,\n"
            '            f_make_md_mode, middle_json, infer_result, process_mode="vlm"\n'
            "        )\n"
        )
        owned_output_call = (
            "        await to_thread_owned(\n"
            "            _process_output,\n"
            "            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,\n"
            "            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,\n"
            "            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,\n"
            '            f_make_md_mode, middle_json, infer_result, process_mode="vlm"\n'
            "        )\n"
        )
        # Occurrences in file order: _async_process_vlm, _process_vlm,
        # _process_hybrid, _async_process_hybrid. The last one is replaced first
        # so that the first one's index is unchanged.
        source = _replace_exact_occurrence(
            source,
            output_call,
            owned_output_call,
            count=4,
            occurrence=3,
            label="CLI asynchronous hybrid output generation off the serving loop",
        )
        return _replace_exact_occurrence(
            source,
            output_call,
            owned_output_call,
            count=3,
            occurrence=0,
            label="CLI asynchronous VLM output generation off the serving loop",
        )

    if relative_path == "mineru/backend/hybrid/hybrid_analyze.py":
        source = _replace_exact(
            source,
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n",
            "from mineru.utils.model_utils import (\n"
            "    clean_memory,\n"
            "    crop_img,\n"
            "    get_vram,\n"
            "    record_hybrid_model_devices,\n"
            "    serial_execution_profile,\n"
            "    strict_processing_window_size,\n"
            "    new_phase_trace,\n"
            "    run_async_owned,\n"
            "    run_native_owned,\n"
            "    drain_owned_awaitable,\n"
            "    to_thread_owned,\n"
            "    trim_process_heap,\n"
            "    release_document_memory_owned,\n"
            ")\n",
            count=1,
            label="Hybrid import",
        )
        source = _replace_exact(
            source,
            "    images_layout_res = _predict_layout_for_title_split(\n",
            "    record_hybrid_model_devices(hybrid_pipeline_model)\n"
            "    images_layout_res = _predict_layout_for_title_split(\n",
            count=1,
            label="Hybrid serving model device identity",
        )
        source = _replace_exact_span(
            source,
            "def get_batch_ratio(device):\n",
            "\n\ndef _close_images(images_list):\n",
            '''def get_batch_ratio(_device):
    """Return one explicit, closed-set process batch ratio."""
    raw_value = os.getenv("MINERU_HYBRID_BATCH_RATIO")
    if raw_value is None:
        raise RuntimeError("MINERU_HYBRID_BATCH_RATIO must be explicitly configured")
    normalized = raw_value.strip()
    if normalized not in {"1", "2", "4", "8"}:
        raise RuntimeError("MINERU_HYBRID_BATCH_RATIO must be one of 1,2,4,8")
    batch_ratio = int(normalized)
    logger.info(f"hybrid batch ratio (explicit): {batch_ratio}")
    return batch_ratio
''',
            label="Hybrid strict batch ratio",
        )
        source = _replace_exact(
            source,
            "        rotate_labels = table_orientation_cls_model.batch_predict(\n"
            "            table_inputs,\n"
            "            det_batch_size=max(1, batch_ratio * OCR_DET_BASE_BATCH_SIZE),\n"
            "            tqdm_enable=True,\n"
            "        )\n",
            "    except Exception as exc:\n"
            "        logger.warning(\n"
            "            f\"Hybrid medium effort table orientation classification failed: {exc}, using original table images\"\n"
            "        )\n"
            "        return\n\n"
            "    record_hybrid_model_devices(table_orientation_cls_model, role='orientation')\n"
            "    try:\n"
            "        rotate_labels = run_ocr_inference(\n"
            "            table_orientation_cls_model.batch_predict,\n"
            "            table_inputs,\n"
            "            det_batch_size=max(1, batch_ratio * OCR_DET_BASE_BATCH_SIZE),\n"
            "            tqdm_enable=True,\n"
            "        )\n",
            count=1,
            label="Hybrid table-orientation model gate",
        )
        source = _replace_exact(
            source,
            "    model_list = []\n"
            "    doc_closed = False\n"
            "    hybrid_pipeline_model = None\n",
            "    model_list = []\n"
            "    phase_trace = None\n"
            "    doc_closed = False\n"
            "    hybrid_pipeline_model = None\n",
            count=2,
            label="Hybrid phase trace declaration",
        )
        source = _replace_exact(
            source,
            "with predictor_execution_guard(predictor):",
            "with predictor_execution_guard("
            "predictor, phase_trace=phase_trace, "
            "trace_window=window_trace_context):",
            count=3,
            label="Hybrid synchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "async with aio_predictor_execution_guard(predictor):",
            "async with aio_predictor_execution_guard("
            "predictor, phase_trace=phase_trace, "
            "trace_window=window_trace_context):",
            count=3,
            label="Hybrid asynchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "                        optimize_hybrid_formula_number_blocks(window_model_list)\n",
            "                        postprocess_started_ns = phase_trace.start()\n"
            "                        optimize_hybrid_formula_number_blocks(window_model_list)\n",
            count=2,
            label="Hybrid medium postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            _apply_vlm_ocr_det_sidecars_for_window(\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            _apply_vlm_ocr_det_sidecars_for_window(\n",
            count=2,
            occurrence=1,
            label="Hybrid synchronous high OCR postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            window_model_list = _process_ocr_and_formulas(\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            window_model_list = _process_ocr_and_formulas(\n",
            count=2,
            occurrence=1,
            label="Hybrid synchronous high native postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            await asyncio.to_thread(\n"
            "                                _apply_vlm_ocr_det_sidecars_for_window,\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            await asyncio.to_thread(\n"
            "                                _apply_vlm_ocr_det_sidecars_for_window,\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous high OCR postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            window_model_list = await asyncio.to_thread(\n"
            "                                _process_ocr_and_formulas,\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            window_model_list = await asyncio.to_thread(\n"
            "                                _process_ocr_and_formulas,\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous high native postprocess phase start",
        )
        source = _replace_exact(
            source,
            "                    _apply_layout_title_split(\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            "                    _apply_layout_title_split(\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            '                        "window_postprocess",\n'
            "                        postprocess_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            count=1,
            label="Hybrid synchronous postprocess phase end",
        )
        source = _replace_exact(
            source,
            "                    await asyncio.to_thread(\n"
            "                        _apply_layout_title_split,\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            "                    await asyncio.to_thread(\n"
            "                        _apply_layout_title_split,\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            '                        "window_postprocess",\n'
            "                        postprocess_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            count=1,
            label="Hybrid asynchronous postprocess phase end",
        )
        source = _replace_exact_occurrence(
            source,
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        effective_window_size = min(page_count, configured_window_size) if page_count else 0\n",
            "        configured_window_size = strict_processing_window_size()\n"
            "        execution_profile = serial_execution_profile(\n"
            "            configured_window_size\n"
            "        )\n"
            "        active_window_size = execution_profile.window_size\n"
            "        effective_window_size = min(page_count, active_window_size) if page_count else 0\n",
            count=2,
            occurrence=0,
            label="Hybrid synchronous serial window selection",
        )
        source = _replace_exact(
            source,
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        effective_window_size = min(page_count, configured_window_size) if page_count else 0\n",
            "        configured_window_size = strict_processing_window_size()\n"
            "        execution_profile = serial_execution_profile(\n"
            "            configured_window_size\n"
            "        )\n"
            "        active_window_size = execution_profile.window_size\n"
            "        effective_window_size = min(page_count, active_window_size) if page_count else 0\n",
            count=1,
            label="Hybrid asynchronous serial window selection",
        )
        source = _replace_exact_occurrence(
            source,
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        batch_ratio_requested = get_batch_ratio(device)\n"
            "        batch_ratio_ocr_override = bool(_ocr_enable)\n"
            "        batch_ratio = 1 if batch_ratio_ocr_override else batch_ratio_requested\n"
            "        phase_trace = new_phase_trace(\n"
            '            backend="hybrid",\n'
            "            page_count=page_count,\n"
            "            window_size=active_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "            hybrid_batch_ratio_requested=batch_ratio_requested,\n"
            "            hybrid_batch_ratio_effective=batch_ratio,\n"
            "            hybrid_batch_ratio_ocr_override=batch_ratio_ocr_override,\n"
            "        )\n"
            "        phase_trace.document_started()\n",
            count=2,
            occurrence=0,
            label="Hybrid synchronous document phase start",
        )
        source = _replace_exact(
            source,
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        batch_ratio_requested = get_batch_ratio(device)\n"
            "        batch_ratio_ocr_override = bool(_ocr_enable)\n"
            "        batch_ratio = 1 if batch_ratio_ocr_override else batch_ratio_requested\n"
            "        phase_trace = new_phase_trace(\n"
            '            backend="hybrid",\n'
            "            page_count=page_count,\n"
            "            window_size=active_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "            hybrid_batch_ratio_requested=batch_ratio_requested,\n"
            "            hybrid_batch_ratio_effective=batch_ratio,\n"
            "            hybrid_batch_ratio_ocr_override=batch_ratio_ocr_override,\n"
            "        )\n"
            "        phase_trace.document_started()\n",
            count=1,
            label="Hybrid asynchronous document phase start",
        )
        source = _replace_exact(
            source,
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n",
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
            "                window_trace_context = phase_trace.window(\n"
            "                    window_index=window_index,\n"
            "                    page_start=window_start,\n"
            "                    page_end_exclusive=window_end + 1,\n"
            "                )\n"
            "                window_started_ns = phase_trace.start()\n"
            "                render_started_ns = phase_trace.start()\n",
            count=2,
            label="Hybrid window phase start",
        )
        source = _replace_exact(
            source,
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                try:\n",
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            '                    "window_render",\n'
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="Hybrid synchronous render phase",
        )
        source = _replace_exact(
            source,
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                try:\n",
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            '                    "window_render",\n'
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="Hybrid asynchronous render phase",
        )
        source = _replace_exact(
            source,
            "                    images_layout_res, hybrid_pipeline_model = _predict_layout_for_window(\n",
            "                    layout_started_ns = phase_trace.start()\n"
            "                    images_layout_res, hybrid_pipeline_model = _predict_layout_for_window(\n",
            count=1,
            label="Hybrid synchronous layout phase start",
        )
        source = _replace_exact(
            source,
            "                        _ocr_enable,\n"
            "                    )\n"
            '                    if effort == "medium":\n',
            "                        _ocr_enable,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            '                        "window_layout",\n'
            "                        layout_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            '                    if effort == "medium":\n',
            count=2,
            label="Hybrid layout phase end",
        )
        source = _replace_exact(
            source,
            "                    images_layout_res, hybrid_pipeline_model = await asyncio.to_thread(\n",
            "                    layout_started_ns = phase_trace.start()\n"
            "                    images_layout_res, hybrid_pipeline_model = await asyncio.to_thread(\n",
            count=1,
            label="Hybrid asynchronous layout phase start",
        )
        source = _replace_exact(
            source,
            "                    append_page_model_list_to_middle_json(\n"
            "                        middle_json,\n",
            "                    append_started_ns = phase_trace.start()\n"
            "                    append_page_model_list_to_middle_json(\n"
            "                        middle_json,\n",
            count=2,
            label="Hybrid append phase start",
        )
        source = _replace_exact(
            source,
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n",
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            '                        "window_append",\n'
            "                        append_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                        append_index=window_index,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n"
            "                    phase_trace.complete(\n"
            '                        "window_total",\n'
            "                        window_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n",
            count=2,
            label="Hybrid append and window completion",
        )
        source = _replace_exact_occurrence(
            source,
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n"
            "                    phase_trace.complete(\n"
            '                        "window_total",\n',
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    await to_thread_owned(trim_process_heap)\n"
            "                    phase_trace.complete(\n"
            '                        "window_total",\n',
            count=2,
            occurrence=1,
            label="Hybrid asynchronous window heap return off the serving loop",
        )
        source = _replace_exact_occurrence(
            source,
            "                    append_page_model_list_to_middle_json(\n",
            "                    await to_thread_owned(\n"
            "                        append_page_model_list_to_middle_json,\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous append resource drain",
        )
        source = _replace_exact(
            source,
            "        if client_side_output_generation:\n"
            "            apply_server_side_postprocess(\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if client_side_output_generation:\n"
            "            apply_server_side_postprocess(\n",
            count=1,
            label="Hybrid synchronous finalize phase start",
        )
        source = _replace_exact(
            source,
            "        if client_side_output_generation:\n"
            "            await asyncio.to_thread(\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if client_side_output_generation:\n"
            "            await asyncio.to_thread(\n",
            count=1,
            label="Hybrid asynchronous finalize phase start",
        )
        source = _replace_exact(
            source,
            "        close_pdfium_document(pdf_doc)\n"
            "        doc_closed = True\n"
            "        clean_memory(device)\n"
            "        return middle_json, model_list\n",
            '        phase_trace.complete("document_finalize", finalize_started_ns)\n'
            "        close_pdfium_document(pdf_doc)\n"
            "        doc_closed = True\n"
            "        clean_memory(device)\n"
            "        phase_trace.document_completed()\n"
            "        trim_process_heap()\n"
            "        return middle_json, model_list\n",
            count=2,
            label="Hybrid document completion",
        )
        # The serving loop must never run a cyclic collection: the asynchronous
        # variant releases allocator caches and the heap on its owned thread and
        # leaves cyclic garbage to the automatic collector. The synchronous
        # doc_analyze (occurrence 0) keeps the original clean_memory sequence.
        source = _replace_exact_occurrence(
            source,
            "        doc_closed = True\n"
            "        clean_memory(device)\n"
            "        phase_trace.document_completed()\n"
            "        trim_process_heap()\n"
            "        return middle_json, model_list\n",
            "        doc_closed = True\n"
            "        await to_thread_owned(release_document_memory_owned, device)\n"
            "        phase_trace.document_completed()\n"
            "        return middle_json, model_list\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous document memory release off the serving loop",
        )
        source = _replace_exact(
            source,
            "    finally:\n"
            "        if not doc_closed:\n"
            "            close_pdfium_document(pdf_doc)\n",
            "    finally:\n"
            "        if not doc_closed:\n"
            "            if phase_trace is not None:\n"
            "                phase_trace.document_failed()\n"
            "            close_pdfium_document(pdf_doc)\n",
            count=2,
            label="Hybrid document failure",
        )
        # Asynchronous document ownership. The prologue (OCR classification,
        # pdfium open, page count) and every close of the pdfium document run
        # on the owned thread pool; the holder created on the loop before the
        # first dispatch keeps the handle across cancellation, so no result
        # callback has to close a document on the loop. The synchronous
        # doc_analyze keeps its upstream prologue and finally block.
        source = _replace_exact(
            source,
            "\n\ndef doc_analyze(\n",
            '''

class _OwnedPdfiumDocument:
    """One pdfium document whose native calls all run on owned worker threads.

    The thread that closes flips ``close_attempted`` before the native call, so
    a close that was cancelled mid-flight or that failed is never repeated by
    the caller's failure path; ``closed`` records an actually completed close.
    """

    def __init__(self):
        self.pdf_doc = None
        self.close_attempted = False
        self.closed = False

    def prepare(self, pdf_bytes, parse_method):
        ocr_enable = ocr_classify(pdf_bytes, parse_method=parse_method)
        self.pdf_doc = open_pdfium_document(pdfium.PdfDocument, pdf_bytes)
        try:
            page_count = get_pdfium_document_page_count(self.pdf_doc)
        except BaseException as failure:
            try:
                self.close()
            except BaseException as cleanup_failure:
                failure.add_note(
                    "owned document close after page count failure failed: "
                    f"{type(cleanup_failure).__name__}"
                )
                raise failure from cleanup_failure
            raise
        return ocr_enable, page_count

    def close(self):
        if self.pdf_doc is None or self.close_attempted:
            return
        self.close_attempted = True
        close_pdfium_document(self.pdf_doc)
        self.closed = True

    def close_and_release(self, device):
        self.close()
        release_document_memory_owned(device)


def doc_analyze(
''',
            count=1,
            label="Hybrid owned document holder",
        )
        source = _replace_exact_occurrence(
            source,
            "    device = get_device()\n"
            "    _ocr_enable = ocr_classify(pdf_bytes, parse_method=parse_method)\n"
            "\n"
            "    pdf_doc = open_pdfium_document(pdfium.PdfDocument, pdf_bytes)\n"
            "    middle_json = init_middle_json(\n"
            "        _ocr_enable,\n"
            "        effort=effort,\n"
            "    )\n"
            "    model_list = []\n"
            "    phase_trace = None\n"
            "    doc_closed = False\n"
            "    hybrid_pipeline_model = None\n"
            "    try:\n"
            "        page_count = get_pdfium_document_page_count(pdf_doc)\n",
            "    device = get_device()\n"
            "    owned_document = _OwnedPdfiumDocument()\n"
            "    model_list = []\n"
            "    phase_trace = None\n"
            "    hybrid_pipeline_model = None\n"
            "    try:\n"
            "        _ocr_enable, page_count = await to_thread_owned(\n"
            "            owned_document.prepare, pdf_bytes, parse_method\n"
            "        )\n"
            "        pdf_doc = owned_document.pdf_doc\n"
            "        middle_json = init_middle_json(\n"
            "            _ocr_enable,\n"
            "            effort=effort,\n"
            "        )\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous document prologue off the serving loop",
        )
        source = _replace_exact(
            source,
            '        phase_trace.complete("document_finalize", finalize_started_ns)\n'
            "        close_pdfium_document(pdf_doc)\n"
            "        doc_closed = True\n"
            "        await to_thread_owned(release_document_memory_owned, device)\n"
            "        phase_trace.document_completed()\n"
            "        return middle_json, model_list\n"
            "    finally:\n"
            "        if not doc_closed:\n"
            "            if phase_trace is not None:\n"
            "                phase_trace.document_failed()\n"
            "            close_pdfium_document(pdf_doc)\n",
            '        phase_trace.complete("document_finalize", finalize_started_ns)\n'
            "        await to_thread_owned(owned_document.close_and_release, device)\n"
            "        phase_trace.document_completed()\n"
            "        return middle_json, model_list\n"
            "    except BaseException as failure:\n"
            "        # The trace end writes to stderr and the close needs the pdfium\n"
            "        # guard; neither may skip the other. Every cleanup failure is\n"
            "        # chained onto the original failure, which stays the one raised.\n"
            "        cleanup_failure = None\n"
            "        if not owned_document.closed and phase_trace is not None:\n"
            "            try:\n"
            "                phase_trace.document_failed()\n"
            "            except BaseException as trace_failure:\n"
            "                failure.add_note(\n"
            '                    "phase trace document end after failure failed: "\n'
            '                    f"{type(trace_failure).__name__}"\n'
            "                )\n"
            "                cleanup_failure = trace_failure\n"
            "        if not owned_document.close_attempted:\n"
            "            try:\n"
            "                await to_thread_owned(owned_document.close)\n"
            "            except BaseException as close_failure:\n"
            "                failure.add_note(\n"
            '                    "owned document close after failure failed: "\n'
            '                    f"{type(close_failure).__name__}"\n'
            "                )\n"
            "                if cleanup_failure is not None:\n"
            "                    close_failure.__context__ = cleanup_failure\n"
            "                cleanup_failure = close_failure\n"
            "        if cleanup_failure is not None:\n"
            "            raise failure from cleanup_failure\n"
            "        raise\n",
            count=1,
            label="Hybrid asynchronous document close and release off the serving loop",
        )
        source = _patch_owned_render_await(source)
        return _replace_exact(
            source,
            "await asyncio.to_thread(",
            "await to_thread_owned(",
            count=9,
            label="Hybrid native layout and postprocess resource drain",
        )

    if relative_path == "mineru_vl_utils/post_process/__init__.py":
        return _patch_table_image_conservation(source)

    raise ValueError(f"unapproved MinerU compatibility target: {relative_path}")


def apply_patch(
    *,
    site_packages: Path = SITE_PACKAGES,
    marker_path: Path = MARKER_PATH,
) -> dict[str, object]:
    """Verify all preimages, patch atomically per file, and emit one marker."""

    if metadata.version("mineru") != MINERU_VERSION:
        raise RuntimeError(f"MinerU must be exactly {MINERU_VERSION}")
    if metadata.version("mineru-vl-utils") != MINERU_VL_UTILS_VERSION:
        raise RuntimeError(f"mineru-vl-utils must be exactly {MINERU_VL_UTILS_VERSION}")
    original: dict[str, bytes] = {}
    for relative_path, expected in TARGET_PREIMAGE_SHA256.items():
        payload = (site_packages / relative_path).read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != expected:
            raise RuntimeError(
                f"{relative_path} preimage drifted: expected {expected}, got {observed}"
            )
        original[relative_path] = payload

    patched: dict[str, bytes] = {}
    for relative_path, payload in original.items():
        text = payload.decode("utf-8")
        updated_text = patch_source(relative_path, text)
        compile(updated_text, relative_path, "exec")
        updated = updated_text.encode("utf-8")
        if updated == payload:
            raise RuntimeError(f"{relative_path} patch made no change")
        patched[relative_path] = updated

    for relative_path, payload in patched.items():
        path = site_packages / relative_path
        path.write_bytes(payload)
        py_compile.compile(str(path), doraise=True)

    patcher_sha256 = _sha256(Path(__file__).read_bytes())
    marker: dict[str, object] = {
        "schema": "mineru-runtime-compatibility.v5",
        "policy": POLICY,
        "capacity_policy": CAPACITY_POLICY,
        "mineru_version": MINERU_VERSION,
        "mineru_vl_utils_version": MINERU_VL_UTILS_VERSION,
        "base_image_digest": BASE_IMAGE_DIGEST,
        "patcher_sha256": patcher_sha256,
        "preimage_sha256": {
            path: "sha256:" + digest
            for path, digest in sorted(TARGET_PREIMAGE_SHA256.items())
        },
        "patched_source_sha256": {
            path: _sha256(payload) for path, payload in sorted(patched.items())
        },
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return marker


if __name__ == "__main__":
    print(json.dumps(apply_patch(), sort_keys=True, separators=(",", ":")))
