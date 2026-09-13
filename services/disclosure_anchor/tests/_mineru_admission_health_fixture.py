"""Independent literal post-Form admission evidence and collector IO boundary.

No production builders generate these values. They describe small ownership
states from the frozen admission contract, not PDFs or qualified live capacity.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import io
import json
import re
import types
from copy import deepcopy
from pathlib import Path


def wire_health(*, legacy=False):
    value = {
        "status": "healthy", "version": "3.4.4", "protocol_version": 2,
        "queued_tasks": 0, "processing_tasks": 0, "completed_tasks": 0,
        "failed_tasks": 0, "max_concurrent_requests": 1,
        "max_pending_tasks_requested": 1, "max_pending_tasks_effective": 1,
        "processing_window_size": 16, "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30,
        "task_protocol_schema": "mineru-task-protocol.v2",
        "task_protocol_runtime": {
            "schema": "mineru-task-runtime.v1" if legacy else "mineru-task-runtime.v2",
            "enabled": True, "task_registry_max_records": 128,
            "task_result_reservation_bytes": 268435456,
            "max_unacked_result_bytes": 2147483648,
        },
    }
    if not legacy:
        value["task_protocol_runtime"].update(
            registry_schema="mineru-task-registry.v3",
            admission_scope="post_form_owned_upload",
        )
        value["task_admission"] = {
            "schema": "mineru-task-admission.v1",
            "registry_schema": "mineru-task-registry.v3",
            "nonterminal_limit": 1, "ingress_tasks": 0,
            "accepted_pending_tasks": 0, "accepted_processing_tasks": 0,
            "accepted_finalizing_tasks": 0, "durable_nonterminal_tasks": 0,
            "routeless_accepted_tasks": 0, "ingress_cleanup_tasks": 0,
            "unowned_ingress_tasks": 0, "scheduled_tasks": 0,
            "queue_depth": 0, "active_processors": 0,
            "recovery_overcommitted": False, "admission_open": True,
            "blocked_reason": None,
        }
    return value


def responsibility(phase):
    """Hand-specified single-responsibility states; no actual manager imported."""
    health = wire_health()
    admission = health["task_admission"]
    admission.update(durable_nonterminal_tasks=1, admission_open=False,
                     blocked_reason="capacity_full")
    if phase in {"ingress", "cleanup", "unowned"}:
        health["queued_tasks"] = 1
        admission["ingress_tasks"] = 1
        if phase != "ingress":
            admission["blocked_reason"] = "ingress_recovery_required"
            admission["ingress_cleanup_tasks" if phase == "cleanup"
                      else "unowned_ingress_tasks"] = 1
    elif phase in {"pending", "routeless"}:
        health["queued_tasks"] = 1
        admission["accepted_pending_tasks"] = 1
        if phase == "routeless":
            admission.update(routeless_accepted_tasks=1,
                             blocked_reason="accepted_recovery_required")
        else:
            admission.update(scheduled_tasks=1, queue_depth=1)
    elif phase in {"processing", "finalizing"}:
        health["processing_tasks"] = 1
        admission["accepted_" + phase + "_tasks"] = 1
        admission.update(scheduled_tasks=1, active_processors=1)
    else:
        raise AssertionError("unknown independent fixture phase")
    return health


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


class CollectorIO:
    """Execute the complete real collector probe with only external IO replaced.

    The importer admits an explicit finite set. No Docker, model, filesystem,
    HTTP request or new interpreter is executed by this fixture.
    """

    def __init__(self, health):
        self.raw = canonical(health) if isinstance(health, dict) else health
        self.read_sizes = []
        self.open_calls = []
        self.response_closed = False
        self.file_reads = []
        self.protocol = types.SimpleNamespace(
            __file__="/usr/local/lib/python3.12/dist-packages/mineru/cli/agent_task_protocol_v2.py"
        )
        # Execute the actual serving-package pure validator, not a test replica.
        protocol_path = (Path(__file__).resolve().parents[1]
                         / "scripts/windows/mineru_heap_trim_compat/agent_task_protocol_v2.py")
        nodes = [node for node in ast.parse(protocol_path.read_text()).body
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "validate_mineru_task_admission"]
        if len(nodes) != 1:
            raise AssertionError("expected one real serving admission validator")
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                     "<actual-serving-admission-validator>", "exec"), namespace)
        self.protocol.validate_mineru_task_admission = namespace["validate_mineru_task_admission"]

    def run(self, collector_path=None):
        if collector_path is None:
            collector_path = (Path(__file__).resolve().parents[1]
                              / "scripts/windows/collect_mineru_runtime.ps1")
        text = Path(collector_path).read_text(encoding="utf-8")
        matches = re.findall(r"\$compatProbeCode = @'\n(.*?)\n'@", text, re.DOTALL)
        if len(matches) != 1:
            raise AssertionError("expected exactly one real collector compat probe")
        owner = self

        class VirtualPath:
            def __init__(self, value):
                self.value = str(value)

            def __truediv__(self, other):
                return VirtualPath(self.value + "/" + str(other))

            def __eq__(self, other):
                return isinstance(other, VirtualPath) and self.value == other.value

            def read_bytes(self):
                prefix = "/usr/local/lib/python3.12/dist-packages/"
                if not self.value.startswith(prefix):
                    raise AssertionError("unexpected collector file read: " + self.value)
                owner.file_reads.append(self.value)
                return ("independent synthetic source bytes: " + self.value).encode()

            def read_text(self, *, encoding):
                if (self.value != "/opt/agent-invest/mineru-serial-v1/compatibility.json"
                        or encoding != "utf-8"):
                    raise AssertionError("unexpected collector marker read")
                return '{"independent_fixture_marker":true}'

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                owner.response_closed = True

            def read(self, size):
                owner.read_sizes.append(size)
                return owner.raw[:size]

        class Opener:
            def open(self, url, *, timeout):
                owner.open_calls.append((url, timeout))
                if url != "http://127.0.0.1:8000/health" or timeout != 10:
                    raise AssertionError("collector health origin/timeout changed")
                return Response()

        def proxy_handler(value):
            if value != {}:
                raise AssertionError("collector attempted ambient proxy")
            return "explicit-no-proxy"

        def build_opener(value):
            if value != "explicit-no-proxy":
                raise AssertionError("unexpected collector opener")
            return Opener()

        def version(name):
            return {"mineru": "3.4.4", "mineru-vl-utils": "1.0.5"}[name]

        modules = {
            "hashlib": hashlib, "json": json,
            "sys": types.SimpleNamespace(argv=[]),
            "os": types.SimpleNamespace(environ={
                "MINERU_PROCESSING_WINDOW_SIZE": "16", "MINERU_HYBRID_BATCH_RATIO": "1",
                "MINERU_API_MAX_PENDING_TASKS": "1",
            }),
            "pathlib": types.SimpleNamespace(Path=VirtualPath),
            "importlib.metadata": types.SimpleNamespace(
                metadata=types.SimpleNamespace(version=version)),
            "urllib.request": types.SimpleNamespace(request=types.SimpleNamespace(
                ProxyHandler=proxy_handler, build_opener=build_opener)),
            "mineru.utils.model_utils": types.SimpleNamespace(
                is_heap_trim_enabled=lambda: True, is_phase_trace_enabled=lambda: True,
                serial_runtime_status=lambda window: {"fixture_window": window}),
            "mineru.backend.pipeline.model_init": types.SimpleNamespace(
                PIPELINE_INFERENCE_LOCKS_ENABLED=True),
            "mineru.cli": types.SimpleNamespace(agent_task_protocol_v2=self.protocol),
        }

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name not in modules:
                raise AssertionError("collector unexpected import: " + name)
            return modules[name]

        safe_builtins = {**vars(builtins), "__import__": guarded_import}
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exec(compile(matches[0], "<actual-collector-compatProbeCode>", "exec"),
                 {"__builtins__": safe_builtins})
        lines = stdout.getvalue().splitlines()
        if len(lines) != 1:
            raise AssertionError("collector did not emit one JSON object")
        return json.loads(lines[0])


def changed(value, section, field, replacement):
    result = deepcopy(value)
    target = result if section is None else result[section]
    if replacement is DELETE:
        del target[field]
    else:
        target[field] = replacement
    return result


DELETE = object()
