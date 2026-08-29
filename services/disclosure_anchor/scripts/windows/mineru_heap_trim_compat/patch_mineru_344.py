#!/usr/bin/env python3
"""Build-time, exact-source runtime compatibility patch for MinerU 3.4.4.

The patch preserves the explicit, fail-visible glibc ``malloc_trim(0)`` hook,
single-owner serial execution and content-free phase evidence. Every source
file must match the deployed 3.4.4 bytes before any write occurs.
"""

from __future__ import annotations

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
        self.peak = 0

    async def __aenter__(self):
        await self.semaphore.acquire()
        self.active += 1
        self.peak = max(self.peak, self.active)
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        if self.active < 1:
            raise RuntimeError("global VLM request limiter underflowed")
        self.active -= 1
        self.semaphore.release()


_PROCESS_ASYNC_REQUEST_LIMITERS = {}


def _process_async_request_limiter(capacity: int) -> _ProcessAsyncRequestLimiter:
    loop = asyncio.get_running_loop()
    limiter = _PROCESS_ASYNC_REQUEST_LIMITERS.get(loop)
    if limiter is None:
        limiter = _ProcessAsyncRequestLimiter(capacity)
        _PROCESS_ASYNC_REQUEST_LIMITERS[loop] = limiter
    elif limiter.capacity != capacity:
        raise RuntimeError("global VLM request concurrency drifted within one process")
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
            "        client = await self._aio_client()\n"
            "        limiter = _process_async_request_limiter(self.max_concurrency)\n"
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
            "    evict_consumed_routes,\n"
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
            "def _retained_result_sources(task: AsyncParseTask):\n"
            "    budget = int(os.getenv(\"MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES\", \"268435456\"))\n"
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
            "            try:\n"
            "                metadata = os.fstat(descriptor)\n"
            "                identity = (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)\n"
            "                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:\n"
            "                    raise RuntimeError(\"result source tree identity is unsafe\")\n"
            "                source_bytes += metadata.st_size\n"
            "                observations.append((path, arcname, descriptor, identity))\n"
            "                descriptor = -1\n"
            "            finally:\n"
            "                if descriptor >= 0:\n"
            "                    os.close(descriptor)\n"
            "        if source_bytes * 2 + len(observations) * 65536 + 1048576 > budget:\n"
            "            raise RuntimeError(\"result source tree exceeds reserved ZIP envelope\")\n"
            "        return budget, observations\n"
            "    except BaseException:\n"
            "        for _path, _arcname, descriptor, _identity in observations:\n"
            "            os.close(descriptor)\n"
            "        raise\n\n\n"
            "def _verify_and_close_result_sources(observations) -> None:\n"
            "    failure = None\n"
            "    for path, _arcname, descriptor, expected in observations:\n"
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
            "async def build_retained_task_result(task: AsyncParseTask) -> None:\n"
            "    source_observations = []\n"
            '    retained = os.path.join(task.output_dir, ".retained-result.zip")\n'
            '    retained_part = retained + ".part"\n'
            "    try:\n"
            "        budget, source_observations = await asyncio.to_thread(_retained_result_sources, task)\n"
            "        await asyncio.to_thread(_write_retained_zip_from_fds, source_observations, retained_part, budget)\n"
            "        await asyncio.to_thread(_verify_and_close_result_sources, source_observations)\n"
            "        source_observations = []\n"
            "        os.replace(retained_part, retained)\n"
            "        directory_fd = os.open(task.output_dir, os.O_RDONLY)\n"
            "        try:\n"
            "            os.fsync(directory_fd)\n"
            "        finally:\n"
            "            os.close(directory_fd)\n"
            "        artifact_sha256, artifact_bytes = await asyncio.to_thread(\n"
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
            "    except BaseException:\n"
            "        cleanup_file(retained)\n"
            "        raise\n"
            "    finally:\n"
            "        for _path, _arcname, descriptor, _identity in source_observations:\n"
            "            os.close(descriptor)\n"
            "        cleanup_file(retained_part)\n\n\n"
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
            "from mineru.utils.model_utils import strict_processing_window_size\n",
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
            "        \"processing_window_size\": strict_processing_window_size(),\n",
            count=1,
            label="FastAPI pending depth health identity",
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
        return source

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
            "import gc\n",
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
    """Require the versioned serial window without an implicit fallback."""
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
    return SerialExecutionProfile(
        profile_id=payload["profile_id"],
        profile_sha256=_serial_profile_hash(payload),
        pipeline_mode="serial",
        pipeline_depth=0,
        window_size=configured_window_size,
        max_resident_pages=configured_window_size,
        inner_inference_concurrency=7,
        vllm_max_num_seqs=128,
    )


def serial_runtime_status(configured_window_size: int) -> dict:
    profile = serial_execution_profile(configured_window_size)
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
        return _replace_exact(
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

    if relative_path == "mineru/backend/hybrid/hybrid_analyze.py":
        source = _replace_exact(
            source,
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n",
            "from mineru.utils.model_utils import (\n"
            "    clean_memory,\n"
            "    crop_img,\n"
            "    get_vram,\n"
            "    serial_execution_profile,\n"
            "    strict_processing_window_size,\n"
            "    new_phase_trace,\n"
            "    run_async_owned,\n"
            "    run_native_owned,\n"
            "    drain_owned_awaitable,\n"
            "    trim_process_heap,\n"
            ")\n",
            count=1,
            label="Hybrid import",
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
        return _replace_exact(
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
