"""Independent external IO for the complete real explicit collector Python probe."""

import builtins
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import types

from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from tests._mineru_admission_health_fixture import CollectorIO, canonical
from tests._mineru_capacity_v11_fixture import SOURCE_PATHS


class CapacityCollectorIO(CollectorIO):
    """Borrow the earlier real admission validator, extend only external IO."""

    def __init__(self, health, config_payload):
        super().__init__(health)
        self.config_raw = canonical(config_payload)
        self.capacity = decode_mineru_capacity_config(self.config_raw)
        self.expected_sha = self.capacity.sha256
        service = Path(__file__).resolve().parents[1]
        # External runners load this module from outside the service; the old
        # fixture's actual module filename retains the maintained repository root.
        from tests import _mineru_admission_health_fixture as old_fixture
        service = Path(old_fixture.__file__).resolve().parents[1]
        self.source_bytes = {image: (service / local).read_bytes() for image, local in SOURCE_PATHS.items()}
        self.source_open_calls = []
        self.source_read_sizes = []
        self.source_closed = []
        self.source_error = None
        self.config_error = None
        self.health_error = None
        self.getter_calls = 0
        self.config_reads = []
        self.stdout = ""
        self.payload = config_payload

    def run(self, collector_path=None):
        if collector_path is None:
            from tests import _mineru_admission_health_fixture as old_fixture
            collector_path = Path(old_fixture.__file__).resolve().parents[1] / "scripts/windows/collect_mineru_runtime.ps1"
        source = Path(collector_path).read_text(encoding="utf-8")
        matches = re.findall(r"\$compatProbeCode = @'\n(.*?)\n'@", source, re.DOTALL)
        if len(matches) != 1:
            raise AssertionError("expected one actual collector probe")
        owner = self
        prefix = "/usr/local/lib/python3.12/dist-packages/"

        class TrackedSource(io.BytesIO):
            def __init__(self, name, raw):
                super().__init__(raw)
                self.source_name = name

            def read(self, size=-1):
                owner.source_read_sizes.append((self.source_name, size))
                if owner.source_error is not None:
                    raise owner.source_error
                return super().read(size)

            def close(self):
                if not self.closed:
                    owner.source_closed.append(self.source_name)
                super().close()

        class VirtualPath:
            def __init__(self, value):
                self.value = str(value)

            def __str__(self):
                return self.value

            def __truediv__(self, value):
                return VirtualPath(self.value + "/" + str(value))

            def __eq__(self, other):
                return isinstance(other, VirtualPath) and self.value == other.value

            def read_bytes(self):
                if not self.value.startswith(prefix):
                    raise AssertionError("unexpected source path: " + self.value)
                owner.file_reads.append(self.value)
                return ("independent legacy source fixture: " + self.value).encode()

            def read_text(self, *, encoding):
                if self.value != "/opt/agent-invest/mineru-serial-v1/compatibility.json" or encoding != "utf-8":
                    raise AssertionError("unexpected marker path")
                return '{"independent_fixture_marker":true}'

            def open(self, mode):
                name = self.value.removeprefix(prefix)
                if mode != "rb" or not self.value.startswith(prefix) or name not in owner.source_bytes:
                    raise AssertionError("unexpected owned source stream")
                owner.source_open_calls.append(name)
                return TrackedSource(name, owner.source_bytes[name])

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                owner.response_closed = True

            def read(self, size):
                owner.read_sizes.append(size)
                if owner.health_error is not None:
                    raise owner.health_error
                return owner.raw[:size]

        class Opener:
            def open(self, url, *, timeout):
                owner.open_calls.append((url, timeout))
                if url != "http://127.0.0.1:8000/health" or timeout != 10:
                    raise AssertionError("unexpected URL or timeout")
                return Response()

        def proxy_handler(value):
            if value != {}:
                raise AssertionError("ambient proxy forbidden")
            return "explicit-no-proxy"

        def build_opener(value):
            if value != "explicit-no-proxy":
                raise AssertionError("unexpected opener")
            return Opener()

        def get_capacity():
            owner.getter_calls += 1
            return owner.capacity

        def read_config(path, *, expected_sha256, expected_owner_uid):
            owner.config_reads.append((str(path), expected_sha256, expected_owner_uid))
            if str(path) != "/usr/local/etc/mineru/capacity.json" or expected_owner_uid != 0:
                raise AssertionError("config owner/path boundary changed")
            if owner.config_error is not None:
                raise owner.config_error
            return owner.config_raw

        def forbidden_serial(*_):
            raise AssertionError("fresh serial runtime cannot replace serving observation")

        modules = {
            "hashlib": hashlib, "json": json,
            "sys": types.SimpleNamespace(argv=["-", self.expected_sha]),
            "os": types.SimpleNamespace(environ={
                "MINERU_PROCESSING_WINDOW_SIZE": str(self.payload["processing_window_size"]),
                "MINERU_HYBRID_BATCH_RATIO": str(self.payload["hybrid_batch_ratio_requested"]),
                "MINERU_API_MAX_PENDING_TASKS": str(self.payload["total_nonterminal_limit"]),
            }),
            "pathlib": types.SimpleNamespace(Path=VirtualPath),
            "importlib.metadata": types.SimpleNamespace(metadata=types.SimpleNamespace(
                version=lambda name: {"mineru": "3.4.4", "mineru-vl-utils": "1.0.5"}[name])),
            "urllib.request": types.SimpleNamespace(request=types.SimpleNamespace(
                ProxyHandler=proxy_handler, build_opener=build_opener)),
            "mineru.utils.model_utils": types.SimpleNamespace(
                is_heap_trim_enabled=lambda: True, is_phase_trace_enabled=lambda: False,
                serial_runtime_status=forbidden_serial),
            "mineru.backend.pipeline.model_init": types.SimpleNamespace(PIPELINE_INFERENCE_LOCKS_ENABLED=True),
            "mineru.cli": types.SimpleNamespace(agent_task_protocol_v2=self.protocol),
            "mineru.cli.agent_capacity_bootstrap": types.SimpleNamespace(get_process_capacity=get_capacity),
            "mineru.cli.agent_capacity_file": types.SimpleNamespace(read_mineru_capacity_file=read_config),
        }

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name not in modules:
                raise AssertionError("unexpected probe import: " + name)
            return modules[name]

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                exec(compile(matches[0], "<actual-explicit-collector-probe>", "exec"),
                     {"__builtins__": {**vars(builtins), "__import__": guarded_import}})
        finally:
            self.stdout = output.getvalue()
        lines = self.stdout.splitlines()
        if len(lines) != 1:
            raise AssertionError("probe must emit exactly one successful JSON object")
        return json.loads(lines[0])
