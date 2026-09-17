from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest

from disclosure_anchor.adapters.runtime.windows_resident_telemetry import (
    build_windows_resident_telemetry_sampler,
    windows_resident_collector_spec,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.windows_resident_telemetry import (
    HostQueueBinding, ResidentIdentity,
)
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES
from tests._mineru_capacity_health_fixture import capacity_health_payload
from disclosure_anchor.application.ports.synchronized_telemetry import (
    TelemetrySnapshotDeadline,
    TelemetrySnapshotContinuityLost,
)


HASHES = ["sha256:" + character * 64 for character in "abcdef0"]
OBSERVER_CLOCK = "sha256:" + "1" * 64
# The host lane is judged against the release's frozen capacity and the API process the
# Linux sampler measured; both come from the shared capacity fixtures, never from a host.
CAPACITY = decode_mineru_capacity_config(CAPACITY_BYTES)


def _host_binding() -> HostQueueBinding:
    owner = capacity_health_payload()["capacity_observation"]["owner"]
    return HostQueueBinding(
        expected_capacity=CAPACITY, serving_namespace_pid=owner["process_id"],
        api_boot_id=owner["boot_id"], api_start_ticks=owner["process_start_ticks"],
        task_retention_seconds=600, task_cleanup_interval_seconds=30,
    )


def _queue_wire() -> dict[str, object]:
    """One supported host observation: the producer's own bytes, forwarded verbatim."""
    health = capacity_health_payload()
    owner = health["capacity_observation"]["owner"]
    return {
        "reason": None,
        "status": "supported",
        "values": {
            "api_health": json.dumps(health, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "api_http": json.dumps({
                "contract_version": "mineru.api-http-request-snapshot.v1",
                "process_id": owner["process_id"], "active_requests": 4, "pending_requests": 5,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "vllm": {"vllm_requests_running": 1, "vllm_requests_waiting": 0,
                     "vllm_kv_cache_usage_ratio": 0.5, "vllm_preemptions_total": 7},
        },
    }


def _identity() -> dict[str, str]:
    names = (
        "exporter_source_sha256",
        "host_assignment_identity_sha256",
        "boot_identity_sha256",
        "runtime_bundle_identity_sha256",
        "process_profile_sha256",
        "clock_domain_identity_sha256",
        "exporter_process_epoch_sha256",
    )
    return dict(zip(names, HASHES, strict=True))


def _payload(sequence: int, *, lane: str = "gpu_fast", queue: dict[str, object] | None = None) -> bytes:
    interval = 1000 if lane == "host_slow" else 250
    value: dict[str, object] = {
        "contract_version": "mineru.windows-resident-telemetry.v2",
        "identity": _identity(),
        "lane": lane,
        "observed_at_utc": (
            datetime.now(timezone.utc) - timedelta(milliseconds=interval) + timedelta(milliseconds=interval * sequence)
        ).isoformat(),
        "sampled_monotonic_ns": sequence * interval * 1_000_000,
        "sequence": sequence,
    }
    unsupported = {
        "reason": "collector_unsupported",
        "status": "unsupported",
        "values": None,
    }
    if lane == "gpu_fast":
        value["gpu"] = unsupported
    else:
        value.update(
            api_process=unsupported,
            host_cgroup=unsupported,
            queue_vllm=unsupported if queue is None else queue,
        )
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class _Handler(BaseHTTPRequestHandler):
    payloads: list[bytes] = []
    requests = 0
    declared_length: int | None = None
    status = 200
    dynamic = False
    lane_sequences = {"gpu_fast": 0, "host_slow": 0}

    def do_GET(self) -> None:
        type(self).requests += 1
        if type(self).dynamic:
            lane = "host_slow" if "/host_slow/" in self.path else "gpu_fast"
            type(self).lane_sequences[lane] += 1
            payload = _payload(type(self).lane_sequences[lane], lane=lane)
        else:
            payload = type(self).payloads.pop(0)
        self.send_response(type(self).status)
        self.send_header(
            "Content-Length",
            str(type(self).declared_length if type(self).declared_length is not None else len(payload)),
        )
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


class WindowsResidentTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.requests = 0
        _Handler.declared_length = None
        _Handler.status = 200
        _Handler.dynamic = False
        _Handler.lane_sequences = {"gpu_fast": 0, "host_slow": 0}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _sampler(self, *, lane: str = "gpu_fast"):
        return build_windows_resident_telemetry_sampler(
            {
                "lane": lane,
                "base_url": f"http://127.0.0.1:{self.server.server_port}",
                "path": f"/{lane}",
                "maximum_response_bytes": 65536,
                "maximum_sample_age_ms": 1000,
                "nominal_interval_ms": 1000 if lane == "host_slow" else 250,
                "collector_identity_sha256": HASHES[0],
                "observer_clock_domain_identity_sha256": OBSERVER_CLOCK,
                "expected_identity": _identity(),
                **({"host_binding": _host_binding().as_config()} if lane == "host_slow" else {}),
            }
        )

    def test_persistent_sampler_maps_two_strict_samples(self) -> None:
        _Handler.payloads = [_payload(1), _payload(2)]
        sampler = self._sampler()
        def deadline() -> TelemetrySnapshotDeadline:
            return TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
        first = sampler.snapshot(deadline=deadline())
        second = sampler.snapshot(deadline=deadline())
        self.assertEqual(first.identity.runtime_bundle_identity_sha256, HASHES[3])
        self.assertEqual(first.identity.clock_domain_identity_sha256, OBSERVER_CLOCK)
        self.assertNotEqual(first.identity.clock_domain_identity_sha256, _identity()["clock_domain_identity_sha256"])
        assert first.resident_exporter_provenance is not None
        self.assertEqual(first.resident_exporter_provenance.wire_sampled_monotonic_ns, 250_000_000)
        self.assertEqual(first.resident_exporter_provenance.exporter_process_epoch_sha256, HASHES[6])
        self.assertEqual(second.gpu.status, "unsupported")
        self.assertEqual(_Handler.requests, 2)
        sampler.close()

    def test_sequence_rollback_and_identity_drift_fail_closed(self) -> None:
        drifted = json.loads(_payload(2))
        drifted["identity"]["boot_identity_sha256"] = "sha256:" + "9" * 64
        _Handler.payloads = [
            _payload(1),
            _payload(1),
            json.dumps(drifted, sort_keys=True, separators=(",", ":")).encode(),
        ]
        sampler = self._sampler()
        def deadline() -> TelemetrySnapshotDeadline:
            return TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
        sampler.snapshot(deadline=deadline())
        with self.assertRaisesRegex(ValueError, "sequence"):
            sampler.snapshot(deadline=deadline())
        with self.assertRaisesRegex(ValueError, "identity"):
            sampler.snapshot(deadline=deadline())

    def test_duplicate_noncanonical_and_stale_payloads_fail_closed(self) -> None:
        duplicate = _payload(1).replace(b'"sequence":1', b'"sequence":1,"sequence":2')
        noncanonical = _payload(1) + b" "
        stale = json.loads(_payload(1))
        stale["observed_at_utc"] = "2020-01-01T00:00:00+00:00"
        for payload in (
            duplicate,
            noncanonical,
            json.dumps(stale, sort_keys=True, separators=(",", ":")).encode(),
        ):
            _Handler.payloads = [payload]
            with self.assertRaises(ValueError):
                self._sampler().snapshot(
                    deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
                )

    def test_host_lane_maps_and_transport_partial_or_non_200_never_reuses_stale(self) -> None:
        _Handler.payloads = [_payload(1, lane="host_slow")]
        host = self._sampler(lane="host_slow").snapshot(
            deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
        )
        self.assertEqual(host.queue_vllm.status, "unsupported")

        _Handler.payloads = [_payload(1)]
        _Handler.declared_length = len(_Handler.payloads[0]) + 1
        with self.assertRaises(ConnectionError):
            self._sampler().snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 500_000_000)
            )

    def test_forwarded_host_bytes_become_frame_values_only_under_the_frozen_capacity(self) -> None:
        # The decisive host-lane boundary: the sampler forwards the producer's raw reply and
        # the frame values come from validating it against the release's frozen capacity and
        # the sampled API process - never from anything the Windows side decided.
        _Handler.payloads = [_payload(1, lane="host_slow", queue=_queue_wire())]
        host = self._sampler(lane="host_slow").snapshot(
            deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
        )
        self.assertEqual(host.queue_vllm.status, "supported")
        values = host.queue_vllm.values
        assert values is not None
        self.assertEqual(values.api_max_pending_tasks, CAPACITY.total_nonterminal_limit)
        self.assertEqual(values.api_nonterminal_tasks,
                         values.api_queued_tasks + values.api_processing_tasks)
        # The HTTP counts are the snapshot's own, and the vLLM digest is the exporter's.
        self.assertEqual((values.api_http_active_requests, values.api_http_pending_requests), (4, 5))
        self.assertEqual(values.vllm_preemptions_total, 7)

        # A reply that is internally consistent but names another capacity is not a sample.
        foreign = _queue_wire()
        health = json.loads(foreign["values"]["api_health"])
        forged = "sha256:" + "9" * 64
        health["task_protocol_runtime"]["capacity_config_sha256"] = forged
        health["capacity_observation"]["capacity_config_sha256"] = forged
        foreign["values"]["api_health"] = json.dumps(
            health, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        _Handler.payloads = [_payload(1, lane="host_slow", queue=foreign)]
        with self.assertRaises(ValueError):
            self._sampler(lane="host_slow").snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
            )

    def test_sequence_checkpoint_loss_is_terminal_without_third_get(self) -> None:
        _Handler.payloads = [_payload(1), _payload(3)]
        sampler = self._sampler()
        sampler.snapshot(
            deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
        )
        _Handler.status = 409
        with self.assertRaises(TelemetrySnapshotContinuityLost):
            sampler.snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
            )
        with self.assertRaises(TelemetrySnapshotContinuityLost):
            sampler.snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 2_000_000_000)
            )
        self.assertEqual(_Handler.requests, 2)
        _Handler.declared_length = None
        _Handler.status = 503
        _Handler.payloads = [_payload(1)]
        with self.assertRaisesRegex(ConnectionError, "HTTP 503"):
            self._sampler().snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 500_000_000)
            )

    def test_exporter_sampling_loop_contains_no_per_tick_helper(self) -> None:
        script = (
            Path(__file__).parents[2]
            / "scripts/windows/mineru_resident_telemetry_exporter.ps1"
        ).read_text()
        sampling_loop = script.split("$sampleAction =", 1)[1].split("$closeAction =", 1)[0]
        for forbidden in ("Start-Process", "docker ", "wsl ", "ssh ", "nvidia-smi", "Add-Type", "::new("):
            self.assertNotIn(forbidden, sampling_loop)
        self.assertIn("$gpu.ReadJson()", sampling_loop)
        self.assertIn("Read-MineruLinuxResponse 'sample'", sampling_loop)
        self.assertIn("$queue.Observe($health,$http,$metrics)", sampling_loop)

    def test_spawn_spec_binds_ready_identity_and_closed_config(self) -> None:
        spec = windows_resident_collector_spec(
            collector_identity_sha256=HASHES[0],
            observer_clock_domain_identity_sha256=OBSERVER_CLOCK,
            lane="gpu_fast",
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            path="/gpu_fast",
            maximum_response_bytes=65536,
            maximum_sample_age_ms=1000,
            nominal_interval_ms=250,
            expected_identity=ResidentIdentity.model_validate(_identity()),
        )
        self.assertEqual(spec.expected_collector_identity_sha256, HASHES[0])
        config = json.loads(spec.canonical_config_json)
        self.assertEqual(config["collector_identity_sha256"], HASHES[0])
        self.assertEqual(config["observer_clock_domain_identity_sha256"], OBSERVER_CLOCK)
        self.assertEqual(set(config), {
            "base_url", "collector_identity_sha256", "expected_identity", "lane",
            "maximum_response_bytes", "maximum_sample_age_ms",
            "nominal_interval_ms", "path", "observer_clock_domain_identity_sha256",
        })
        for invalid in (None, "", "sha256:" + "G" * 64, 5):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "observer_clock"):
                build_windows_resident_telemetry_sampler({**config, "observer_clock_domain_identity_sha256": invalid})
        del config["observer_clock_domain_identity_sha256"]
        with self.assertRaisesRegex(ValueError, "config shape"):
            build_windows_resident_telemetry_sampler(config)

    def test_default_off_supervisor_declares_job_object_and_has_no_activation_caller(self) -> None:
        root = Path(__file__).parents[2]
        supervisor = (root / "scripts/windows/start_mineru_resident_telemetry.ps1").read_text()
        job = (root / "scripts/windows/mineru_telemetry_job_supervisor.cs").read_text()
        bootstrap = (root / "scripts/windows/load_mineru_resident_session.ps1").read_text()
        self.assertIn("[MineruTelemetryJobSupervisor]::Run", supervisor)
        self.assertNotIn("Add-Type", supervisor)
        self.assertNotIn("INFINITE", supervisor)
        self.assertIn("UpdateProcThreadAttribute", job)
        self.assertIn("KILL_ON_CLOSE = 0x2000", job)
        active_surfaces = "\n".join(
            path.read_text(errors="replace")
            for path in (
                root / "Makefile",
                root / "scripts/windows/install_mineru_fixed_api.ps1",
                root / "src/disclosure_anchor/settings.py",
            )
        )
        self.assertNotIn("start_mineru_resident_telemetry", active_surfaces)
        executable_surface = "\n".join(
            path.read_text(errors="replace")
            for parent in (root / "src", root / "scripts")
            for path in parent.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".ps1"}
            and path.name not in {
                "mineru_resident_telemetry_exporter.ps1",
                "start_mineru_resident_telemetry.ps1",
                "load_mineru_resident_session.ps1",
                "test_mineru_resident_session.ps1",
                "test_mineru_resident_bootstrap.ps1",
                "windows_resident_telemetry.py",
                "resident_session_evidence.py",
                "resident_telemetry_owner.py",
                "full_host_hour_kpi.py",
            }
        )
        self.assertFalse("mineru_resident_telemetry" in executable_surface, "resident telemetry has an unexpected activation caller")
        self.assertFalse("run_resident_telemetry_session(" in executable_surface, "explicit diagnostic owner has an automatic activation caller")
        self.assertIn("Get-MineruInteger $config 'port' 1024 65535", bootstrap)
        self.assertIn("$PSHOME,'powershell.exe'", bootstrap)


if __name__ == "__main__":
    unittest.main()
