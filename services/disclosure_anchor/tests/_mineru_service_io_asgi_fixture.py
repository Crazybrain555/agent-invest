"""Author integration fixture: actual generated routes/middleware and real ASGI.

Only parser output and Linux proc identity are synthetic. No model, remote API,
PDF parsing, DB or production root is used. This is not independent acceptance.
"""
from __future__ import annotations

import ast
import asyncio
import tempfile
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse

from tests._mineru_capacity_lifecycle_fixture import CapacityLifecycleFixture


async def until(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


class ServiceIOASGIFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="m6-service-io-asgi-")
        self.root = Path(self.temporary.name).resolve()
        self.fx = CapacityLifecycleFixture(self.root)
        self.manager = self.fx.manager
        self.module = self.fx.module
        self.releases = []
        self.children = []
        self.app = FastAPI()
        self.app.state.config = {"max_concurrency": 7}
        self.app.state.task_manager = self.manager
        self.manager.app = self.app
        namespace = self.module.__dict__
        namespace.update(app=self.app, Request=Request, BackgroundTasks=BackgroundTasks,
                         HTTPException=HTTPException, FileResponse=FileResponse,
                         JSONResponse=JSONResponse)
        wanted = {"_request_resources", "_ServiceRequestMiddleware", "_pin_response_result",
                  "get_async_task_result", "ack_async_task_result", "health_check", "get_task_manager"}
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        found = set()
        for node in ast.parse(self.fx.generated).body:
            if isinstance(node, ast.ImportFrom) and node.module == "contextvars":
                nodes.append(node)
            if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "_request_resource_context" for target in node.targets):
                nodes.append(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in wanted:
                nodes.append(node)
                found.add(node.name)
        if found != wanted:
            raise AssertionError("generated ASGI coverage drift: " + repr(wanted - found))
        if "app.add_middleware(_ServiceRequestMiddleware)" not in self.fx.generated:
            raise AssertionError("production create_app does not register request ownership")
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<actual-generated-service-asgi>", "exec"), namespace)
        self.app.add_middleware(namespace["_ServiceRequestMiddleware"])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://service.test")

        async def parser_boundary(output_dir, uploads, request_options, config):
            for upload in uploads:
                folder = Path(output_dir) / upload.stem / "auto"
                folder.mkdir(parents=True)
                (folder / (upload.stem + ".md")).write_bytes(b"literal controlled source\n")
                (folder / (upload.stem + "_middle.json")).write_bytes(b'{"source":"controlled"}\n')
                (folder / (upload.stem + "_model.json")).write_bytes(b'[]\n')
                (folder / (upload.stem + "_content_list.json")).write_bytes(b'[]\n')
            return [upload.stem for upload in uploads]
        namespace["run_parse_job"] = parser_boundary

    async def start(self):
        await self.manager.start()

    async def completed(self, name="asgi"):
        options = self.fx.options(name)
        task = await self.fx.create(options)
        await until(lambda: task.status in {"completed", "failed"})
        if task.status != "completed":
            raise AssertionError(task.error)
        await self.manager.service_io.call(self.manager.task_protocol_v2.lease,
                                          task.agent_idempotency_key, seconds=30)
        return task

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.children.append(task)
        return task

    async def close(self):
        for release in self.releases:
            release.set()
        for task in self.children:
            if not task.done():
                task.cancel()
        if self.children:
            await asyncio.wait_for(asyncio.gather(*self.children, return_exceptions=True), 5)
        await self.client.aclose()
        await self.fx.dispose_test_tasks()
        self.fx.close()
        self.temporary.cleanup()
