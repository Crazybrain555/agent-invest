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
from disclosure_anchor.application.contracts.windows_resident_telemetry import ResidentIdentity
from disclosure_anchor.application.ports.synchronized_telemetry import (
    TelemetrySnapshotDeadline,
    TelemetrySnapshotContinuityLost,
)


HASHES = ["sha256:" + character * 64 for character in "abcdef0"]


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


def _payload(sequence: int, *, lane: str = "gpu_fast") -> bytes:
    interval = 1000 if lane == "host_slow" else 250
    value: dict[str, object] = {
        "contract_version": "mineru.windows-resident-telemetry.v1",
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
            queue_vllm=unsupported,
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
                "expected_identity": _identity(),
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
        sampling_loop = script.split("while ($listener.IsListening)", 1)[1]
        for forbidden in ("Start-Process", "docker ", "wsl ", "ssh ", "nvidia-smi"):
            self.assertNotIn(forbidden, sampling_loop)

    def test_spawn_spec_binds_ready_identity_and_closed_config(self) -> None:
        spec = windows_resident_collector_spec(
            collector_identity_sha256=HASHES[0],
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
        self.assertEqual(set(config), {
            "base_url", "collector_identity_sha256", "expected_identity", "lane",
            "maximum_response_bytes", "maximum_sample_age_ms",
            "nominal_interval_ms", "path",
        })

    def test_default_off_supervisor_declares_job_object_and_has_no_activation_caller(self) -> None:
        root = Path(__file__).parents[2]
        supervisor = (root / "scripts/windows/start_mineru_resident_telemetry.ps1").read_text()
        self.assertIn("AssignProcessToJobObject", supervisor)
        self.assertIn("KILL_ON_CLOSE=0x2000", supervisor)
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
                "windows_resident_telemetry.py",
                "full_host_hour_kpi.py",
            }
        )
        self.assertNotIn("mineru_resident_telemetry", executable_surface)
        self.assertIn("ValidateRange(1024, 65535)", supervisor)
        self.assertIn("$PSHOME, 'powershell.exe'", supervisor)


if __name__ == "__main__":
    unittest.main()
