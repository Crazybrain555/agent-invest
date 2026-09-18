"""Independent thin-pressure producer/route tests using actual patched bytes.

Kernel files are synthetic and local. No MinerU/framework startup or live HTTP.
"""

import ast
from contextlib import ExitStack, asynccontextmanager
import importlib.util
import os
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from tests._mineru_capacity_bootstrap_fixture import CapacityBootstrapFixture
from tests._mineru_capacity_config_fixture import capacity_payload
from tests.unit.test_linux_resident_host_sampler import _stat
from tests._mineru_owned_drain_fixture import generated_sources, load_definitions, load_owned


class ThinPressureProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="thin-pressure-independent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bootstrap = CapacityBootstrapFixture(self.root)
        self.addCleanup(self.bootstrap.close)
        self.config = self.bootstrap.codec.MineruCapacityConfig(**capacity_payload())
        source = (
            Path(__file__).resolve().parents[2]
            / "scripts/windows/mineru_heap_trim_compat/agent_capacity_observation.py"
        )
        spec = importlib.util.spec_from_file_location(
            "independent_pressure_producer", source
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.files = {
            "/proc/self/stat": _stat(os.getpid(), start=991),
            "/proc/sys/kernel/random/boot_id": "00000000-0000-4000-8000-000000000001\n",
            "/proc/self/cgroup": "0::/\n",
            "/proc/self/mountinfo": "31 22 0:28 / /sys/fs/cgroup ro,nosuid,nodev - cgroup2 cgroup rw\n",
            "/proc/meminfo": "MemTotal:       1024 kB\nMemAvailable:    512 kB\nMemFree:  1 kB\n",
            "/sys/fs/cgroup/memory.current": "262144\n",
            "/sys/fs/cgroup/memory.max": "1048576\n",
            "/sys/fs/cgroup/memory.events": "low 1\nhigh 2\nmax 3\noom 0\noom_kill 0\noom_group_kill 0\n",
        }
        for path, value in self.files.items():
            target = self.path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        namespace = self.path("/proc/self/ns/cgroup")
        namespace.parent.mkdir(parents=True)
        namespace.symlink_to("cgroup:[421]")
        original_readlink = os.readlink
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(self.module, "Path", side_effect=self.path)
        )
        self.stack.enter_context(
            patch.object(
                self.module.os,
                "readlink",
                side_effect=lambda path: original_readlink(self.path(path)),
            )
        )
        self.stack.enter_context(
            patch.object(
                self.bootstrap.bootstrap,
                "get_process_capacity",
                return_value=self.config,
            )
        )
        self.http = ModuleType("mineru_vl_utils.vlm_client.http_client")
        self.http._capacity_http_snapshot = lambda sha: {
            "loop_epoch": "sha256:" + "2" * 64,
            "final_http_limit_per_loop": self.config.final_http_limit_per_loop,
            "http_limiter_state": "ready",
            "http_counters": {},
            "owner_control": {"soft_drain_applied": False},
        }
        self.stack.enter_context(
            patch.dict(
                "sys.modules",
                {
                    self.http.__name__: self.http,
                    "torch": None,
                    "mineru.utils.pdf_image_tools": None,
                },
            )
        )
        self.manager = SimpleNamespace(
            max_nonterminal_tasks=self.config.total_nonterminal_limit,
            is_shutting_down=False,
            task_protocol_v2=SimpleNamespace(
                _limit=self.config.max_unacked_result_bytes
            ),
            task_protocol_executor=SimpleNamespace(
                parse_slots=self.config.parse_active_limit,
                finalizer_slots=self.config.finalizer_active_limit,
                result_reservation_bytes=self.config.result_reservation_bytes,
                stage_snapshot=lambda: {},
            ),
        )

    def path(self, original):
        return self.root / "kernel" / str(original).lstrip("/")

    def write(self, name, value):
        self.path(name).write_text(value)

    def test_actual_kernel_reader_is_bounded_read_only_and_keeps_scope_and_units(self):
        before = {name: self.path(name).read_bytes() for name in self.files}
        original_open = Path.open
        opened = []

        def read_only(path, mode="r", *args, **kwargs):
            self.assertIn(mode, ("r", "rb"), "pressure attempted a filesystem mutation")
            opened.append(path)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", autospec=True, side_effect=read_only):
            result = self.module._linux_pressure_memory()
        self.assertTrue(opened)
        self.assertEqual(
            before, {name: self.path(name).read_bytes() for name in self.files}
        )
        self.assertEqual(result["scope"], "self_cgroup_and_vm")
        self.assertEqual(result["ancestor_visibility"], "not_observed")
        self.assertEqual(result["vm_total_bytes"], 1048576)
        self.assertEqual(result["vm_available_bytes"], 524288)
        self.assertEqual(result["cgroup_current_bytes"], 262144)
        self.assertEqual(result["cgroup_max_bytes"], 1048576)
        self.assertEqual(result["memory_events"]["high"], 2)
        self.assertRegex(result["cgroup_identity_sha256"], r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(result, self.module._linux_pressure_memory())

    def test_local_unlimited_is_distinct_from_missing_or_guaranteed_headroom(self):
        self.write("/sys/fs/cgroup/memory.max", "max\n")
        result = self.module._linux_pressure_memory()
        self.assertIsNone(result["cgroup_max_bytes"])
        self.assertEqual(result["ancestor_visibility"], "not_observed")
        self.path("/sys/fs/cgroup/memory.max").unlink()
        with self.assertRaises(OSError):
            self.module._linux_pressure_memory()

    def test_malformed_duplicate_missing_or_wrong_unit_inputs_never_become_zero(self):
        cases = (
            ("/sys/fs/cgroup/memory.current", "-1\n"),
            ("/sys/fs/cgroup/memory.max", "1.0\n"),
            ("/sys/fs/cgroup/memory.events", "low 0\nhigh 0\nmax 0\noom 0\n"),
            (
                "/sys/fs/cgroup/memory.events",
                self.files["/sys/fs/cgroup/memory.events"] + "high 0\n",
            ),
            ("/proc/meminfo", "MemTotal: 1024 kB\n"),
            ("/proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 MB\n"),
            ("/proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 1025 kB\n"),
            ("/proc/meminfo", self.files["/proc/meminfo"] + "MemTotal: 1024 kB\n"),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.write(path, value)
                try:
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.module._linux_pressure_memory()
                finally:
                    self.write(path, self.files[path])

    def test_unqualified_membership_or_mount_mappings_are_rejected(self):
        for membership in (
            "0::/docker/api\n",
            "0::/../api\n",
            "0::/ (deleted)\n",
            "0::/\n0::/\n",
            "2:memory:/\n",
        ):
            self.write("/proc/self/cgroup", membership)
            with self.subTest(membership=membership), self.assertRaises(RuntimeError):
                self.module._linux_pressure_memory()
        self.write("/proc/self/cgroup", self.files["/proc/self/cgroup"])
        mount = self.files["/proc/self/mountinfo"]
        for value in (
            mount.replace(" / /sys/", " /.. /sys/"),
            mount.replace(" /sys/fs/cgroup ", " /other/cgroup "),
            mount.replace(" - cgroup2 ", " - cgroup "),
            mount + mount,
        ):
            self.write("/proc/self/mountinfo", value)
            with self.subTest(mount=value), self.assertRaises(RuntimeError):
                self.module._linux_pressure_memory()

    def test_identity_changes_during_the_read_invalidate_the_snapshot(self):
        original_read = self.module._kernel_text
        for changed in ("membership", "namespace", "mount"):

            def read_and_change(path, maximum=65536):
                result = original_read(path, maximum)
                if path == self.path("/sys/fs/cgroup/memory.events"):
                    if changed == "membership":
                        self.write("/proc/self/cgroup", "0::/moved\n")
                    elif changed == "namespace":
                        link = self.path("/proc/self/ns/cgroup")
                        link.unlink()
                        link.symlink_to("cgroup:[422]")
                    else:
                        self.write(
                            "/proc/self/mountinfo",
                            self.files["/proc/self/mountinfo"].replace(
                                "31 22", "32 22"
                            ),
                        )
                return result

            try:
                with (
                    self.subTest(changed=changed),
                    patch.object(
                        self.module, "_kernel_text", side_effect=read_and_change
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    self.module._linux_pressure_memory()
            finally:
                self.write("/proc/self/cgroup", self.files["/proc/self/cgroup"])
                self.write("/proc/self/mountinfo", self.files["/proc/self/mountinfo"])
                link = self.path("/proc/self/ns/cgroup")
                link.unlink()
                link.symlink_to("cgroup:[421]")

    def test_empty_oversized_or_non_ascii_kernel_payload_is_rejected(self):
        path = self.path("/proc/self/cgroup")
        for raw in (b"", b"x" * 4097, b"\xff"):
            path.write_bytes(raw)
            with (
                self.subTest(raw_size=len(raw)),
                self.assertRaises((RuntimeError, UnicodeError)),
            ):
                self.module._linux_pressure_memory()

    def test_actual_observer_binds_owner_capacity_clock_and_cgroup_lifetime(self):
        observer = self.module.CapacityServingObservation(self.config, self.manager)
        result = observer.pressure_snapshot()
        self.assertEqual(result["schema"], "mineru.process-pressure.v1")
        self.assertEqual(result["capacity_config_sha256"], self.config.sha256)
        self.assertEqual(result["owner"]["process_id"], os.getpid())
        self.assertEqual(result["owner"]["process_start_ticks"], 991)
        self.assertLessEqual(
            result["observed_at"]["started_ns"], result["observed_at"]["completed_ns"]
        )
        with (
            patch.object(self.module.os, "getpid", return_value=os.getpid() + 1),
            self.assertRaises(RuntimeError),
        ):
            observer.pressure_snapshot()
        with (
            patch.object(
                self.bootstrap.bootstrap, "get_process_capacity", return_value=None
            ),
            self.assertRaises(RuntimeError),
        ):
            observer.pressure_snapshot()
        self.write(
            "/proc/self/mountinfo",
            self.files["/proc/self/mountinfo"].replace("31 22", "32 22"),
        )
        with self.assertRaises(RuntimeError):
            observer.pressure_snapshot()

    def test_actual_patched_GET_route_reads_fresh_same_process_observer_and_fails_legacy(
        self,
    ):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from starlette.responses import JSONResponse

        import anyio
        from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol

        # Load the real full-preimage output and imported owned-operation helpers.
        # The fixture supplies lifecycle wiring, never a synchronous IO substitute.
        sources = generated_sources()
        patched = sources["api"]
        imported = {
            alias.name
            for node in ast.parse(patched).body
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("agent_task_protocol_v2")
            for alias in node.names
        }
        self.assertTrue({"RegistryServiceIO", "TaskRegistryObservationBusy"} <= imported)
        handler = next(
            node
            for node in ast.parse(patched).body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "agent_process_pressure_telemetry"
        )
        namespace = load_owned(sources["model"], patched)
        namespace["anyio"] = anyio
        load_definitions(patched, {"_settle_service_operation"}, namespace)

        @asynccontextmanager
        async def lifespan(_app):
            self.manager.service_io = protocol.RegistryServiceIO(
                drain=namespace["_settle_service_operation"],
                max_pending=self.config.total_nonterminal_limit + 8,
            )
            try:
                yield
            finally:
                await self.manager.service_io.close()

        app = FastAPI(lifespan=lifespan)
        observer = self.module.CapacityServingObservation(self.config, self.manager)
        self.manager.capacity_observer = observer
        app.state.task_manager = self.manager
        namespace.update({
            "app": app,
            "JSONResponse": JSONResponse,
            "HTTPException": HTTPException,
            "TaskRegistryObservationBusy": protocol.TaskRegistryObservationBusy,
            "get_task_manager": lambda: self.manager,
        })
        exec(
            compile(
                ast.Module(body=[handler], type_ignores=[]),
                "actual-pressure-route",
                "exec",
            ),
            namespace,
        )
        with TestClient(app) as client:
            route = "/agent/telemetry/pressure/v1"
            first = client.get(route)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers["cache-control"], "no-store")
            self.assertEqual(first.json()["memory"]["cgroup_current_bytes"], 262144)
            self.write("/sys/fs/cgroup/memory.current", "131072\n")
            self.assertEqual(
                client.get(route).json()["memory"]["cgroup_current_bytes"], 131072
            )
            self.assertEqual(client.post(route).status_code, 405)
            self.assertNotIn(route, client.get("/openapi.json").json()["paths"])
            self.manager.capacity_observer = None
            self.assertEqual(client.get(route).status_code, 503)
