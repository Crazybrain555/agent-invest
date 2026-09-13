"""Independent extraction of complete pinned HTTP preimage and actual POST AST."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

SERVICE = Path(os.environ.get("M6_TEST_SERVICE_ROOT", Path(__file__).parents[1]))
PATCHER = Path(os.environ.get(
    "M6_TEST_PATCHER",
    SERVICE / "scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py",
))
RELATIVE = "mineru_vl_utils/vlm_client/http_client.py"
PREIMAGE_SHA256 = "afe42d8a5e310d27cb0173abf4d59ed6197bc0b60a0258f321a6cdedd07c6ba7"
CONFIG_SHA256 = "sha256:" + "7a" * 32


def generated_http():
    source = SERVICE / "tests/fixtures/mineru_344_preimages" / RELATIVE
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PREIMAGE_SHA256:
        raise AssertionError("complete upstream HTTP preimage identity changed")
    spec = importlib.util.spec_from_file_location("independent_http_owner_patcher", PATCHER)
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    result = patcher.patch_source(RELATIVE, raw.decode("utf-8"))
    compile(result, RELATIVE, "exec")
    return result


async def loop_fence():
    """Two FIFO callback fences; no elapsed sleep decides correctness."""
    for _ in range(2):
        done = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(done.set_result, None)
        await done


def foreign_call(operation):
    """Run a real second loop/thread while caller's loop is synchronously paused."""
    result = SimpleNamespace(value=None, error=None, thread_id=None)

    def run():
        result.thread_id = threading.get_ident()
        try:
            result.value = asyncio.run(operation())
        except BaseException as exc:
            result.error = exc

    thread = threading.Thread(target=run, name="independent-capacity-foreign-loop")
    thread.start()
    thread.join(3)
    if thread.is_alive():
        raise AssertionError("finite foreign-loop operation failed to finish")
    return result


class Transport:
    """The sole substituted IO: finite event-gated literal POST responses."""

    def __init__(self, *, held=False, failure=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not held:
            self.release.set()
        self.failure = failure
        self.calls = []
        self.getter_calls = 0

    async def post(self, url, *, json):
        self.calls.append((url, json))
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=3)
        if self.failure is not None:
            raise self.failure
        return {"literal_result": "owner-post-result"}


class HttpFixture:
    def __init__(self, source):
        tree = ast.parse(source)
        names = {
            "_ProcessAsyncRequestLimiter", "_bind_capacity_owner",
            "_apply_capacity_soft_drain", "_capacity_http_snapshot",
            "_process_async_request_limiter", "_process_async_request_snapshot",
        }
        assignments = {
            "_PROCESS_ASYNC_REQUEST_STATS_LOCK", "_PROCESS_ASYNC_REQUEST_STATS",
            "_PROCESS_ASYNC_REQUEST_LIMITERS", "_CAPACITY_OWNER",
        }
        nodes = []
        found = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                nodes.append(node)
                found.add(node.name)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in assignments
                for target in node.targets
            ):
                nodes.append(node)
                found.update(target.id for target in node.targets if isinstance(target, ast.Name))
            elif isinstance(node, ast.Import) and any(
                alias.asname in {"_agent_request_os", "_agent_request_threading"}
                for alias in node.names
            ):
                nodes.append(node)
        if found != names | assignments:
            raise AssertionError("actual generated owner definitions/globals missing")
        original_client = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                               and node.name == "HttpVlmClient")
        method = next(node for node in original_client.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "aio_predict")
        # Preserve the entire original generated method body, including its actual awaits.
        nodes.append(ast.ClassDef(name="ActualPostClient", bases=[], keywords=[],
                                  body=[method], decorator_list=[]))

        async def image_bytes(image):
            return [image], "png"

        self.namespace = {"asyncio": asyncio, "json": json,
                          "aio_image_to_bytes_list_and_format": image_bytes}
        unit = ast.Module(body=[ast.ImportFrom(module="__future__", level=0,
                    names=[ast.alias(name="annotations")]), *nodes], type_ignores=[])
        exec(compile(ast.fix_missing_locations(unit), "<actual-generated-http-owner>", "exec"),
             self.namespace)

    def bind(self, callback, capacity=2, sha=CONFIG_SHA256):
        return self.namespace["_bind_capacity_owner"](sha, capacity, callback)

    def snapshot(self, sha=CONFIG_SHA256):
        return self.namespace["_capacity_http_snapshot"](sha)

    def limiter(self, capacity=2):
        return self.namespace["_process_async_request_limiter"](capacity)

    @property
    def limiters(self):
        return self.namespace["_PROCESS_ASYNC_REQUEST_LIMITERS"]

    @property
    def owner(self):
        return self.namespace["_CAPACITY_OWNER"]

    def client(self, transport, capacity=2):
        client = self.namespace["ActualPostClient"]()
        client.debug = False
        client.max_concurrency = capacity
        client.chat_url = "http://literal.invalid/v1/chat/completions"
        client.system_prompt = "literal system"
        client.build_request_body = lambda **values: {"literal_request": values["prompt"]}
        client.get_response_data = lambda value: value
        client.get_response_content = lambda value: value["literal_result"]

        async def get_client():
            transport.getter_calls += 1
            return transport

        client._aio_client = get_client
        return client
