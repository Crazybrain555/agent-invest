"""Host-capacity and service-epoch projection regressions."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    build_host_observer_ssh_command,
    project_host_capacity_sample,
    project_host_service_epoch,
    project_synchronized_host_capacity_sample,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
)


COLLECTOR = "sha256:" + "f" * 64
NODE = "sha256:" + "0" * 64


def _payload(*, available: int = 8192, restart: int = 0) -> dict[str, object]:
    containers = []
    for index, name in enumerate(
        ("mineru-api", "mineru-api-proxy", "mineru-openai-server"),
        start=1,
    ):
        containers.append(
            {
                "name": name,
                "id": str(index) * 64,
                "started_at_utc": "2026-08-25T00:00:00+00:00",
                "restart_count": restart if name == "mineru-api" else 0,
                "oom_killed": False,
                "exit_code": 0,
                "running": True,
                "status": "running",
                "health": "healthy",
                "pid": 100 + index,
                "memory_current_bytes": 2048,
                "memory_max_bytes": None,
                "memory_events": {"oom": 0, "oom_kill": 0, "high": 0},
                "pid1_rss_bytes": 1024,
                "pid1_rss_hwm_bytes": 2048,
                "docker_vm_memory_total_bytes": 16384,
                "docker_vm_memory_available_bytes": available,
            }
        )
    return {
        "schema": "mineru-host-capacity-sample.v1",
        "observed_at_utc": "2026-08-25T00:00:01+00:00",
        "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
        "collector_sha256": COLLECTOR,
        "windows_node_identity_sha256": NODE,
        "containers": containers,
    }


def _psi(*, full_supported: bool = True) -> dict[str, object]:
    line = {
        "avg10_pct": 0.0,
        "avg60_pct": 0.0,
        "avg300_pct": 0.0,
        "total_stall_us": 0,
    }
    return {
        "some": line,
        "full_status": "supported" if full_supported else "unsupported",
        "full_reason": None if full_supported else "collector_unsupported",
        "full": line if full_supported else None,
    }


def _synchronized_payload() -> dict[str, object]:
    return {
        "schema": "mineru-synchronized-host-capacity-sample.v1",
        "observed_at_utc": "2026-08-29T00:00:00+00:00",
        "collector_sha256": COLLECTOR,
        "windows_node_identity_sha256": NODE,
        "api_process": {
            "process_epoch_sha256": "sha256:" + "1" * 64,
            "cpu_user_ns_total": 100,
            "cpu_system_ns_total": 50,
            "rss_bytes": 1000,
            "rss_hwm_bytes": 1200,
            "thread_count": 8,
        },
        "docker_vm": {
            "memory_total_bytes": 32_000,
            "memory_available_bytes": 10_000,
        },
        "parent_cgroup": {
            "epoch_sha256": "sha256:" + "2" * 64,
            "memory_current_bytes": 20_000,
            "memory_max_status": "bounded",
            "memory_max_bytes": 32_000,
            "memory_stat": {
                "anon_bytes": 10_000,
                "file_bytes": 5_000,
                "shmem_bytes": 100,
                "slab_bytes": 500,
            },
            "memory_events": {
                "low_total": 0,
                "high_total": 0,
                "max_total": 0,
                "oom_total": 0,
                "oom_kill_total": 0,
                "oom_group_kill_total": 0,
            },
            "memory_psi": _psi(),
            "cpu_stat": {
                "usage_ns_total": 1000,
                "user_ns_total": 600,
                "system_ns_total": 300,
                "throttled_ns_total": 0,
                "throttled_periods_total": 0,
            },
            "cpu_psi": _psi(full_supported=False),
            "io_psi": _psi(),
        },
    }


class CapacityHostObserverTests(unittest.TestCase):
    def test_ssh_command_requires_exact_manifest_host_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = root / "identity"
            known_hosts = root / "known_hosts"
            key_blob = b"exact-ed25519-public-key-blob"
            identity.write_text("private-test-key", encoding="utf-8")
            known_hosts.write_text(
                "100.64.0.1 ssh-ed25519 "
                + base64.b64encode(key_blob).decode("ascii")
                + "\n",
                encoding="utf-8",
            )
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            expected = "sha256:" + hashlib.sha256(key_blob).hexdigest()

            command = build_host_observer_ssh_command(
                host="100.64.0.1",
                user="operator",
                port=22,
                identity_file=identity,
                known_hosts_file=known_hosts,
                expected_host_key_sha256=expected,
            )
            self.assertIn("StrictHostKeyChecking=yes", command)
            with self.assertRaisesRegex(ValueError, "SSH key drifted"):
                build_host_observer_ssh_command(
                    host="100.64.0.1",
                    user="operator",
                    port=22,
                    identity_file=identity,
                    known_hosts_file=known_hosts,
                    expected_host_key_sha256="sha256:" + "0" * 64,
                )

    def test_safe_capacity_projection_removes_raw_ids(self) -> None:
        payload = _payload()
        projected = project_host_capacity_sample(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
            docker_memory_reserve_bytes=4096,
        )

        encoded = json.dumps(projected.model_dump(mode="json"), sort_keys=True)
        self.assertEqual(projected.safety_violation_codes, ())
        self.assertNotIn(str(1) * 64, encoded)
        self.assertEqual(projected.container_count, 3)

    def test_reserve_and_restart_violations_remain_visible(self) -> None:
        for payload, expected in (
            (_payload(available=1024), "memory_reserve_crossed"),
            (_payload(restart=1), "container_state_unsafe"),
        ):
            projected = project_host_capacity_sample(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
                docker_memory_reserve_bytes=4096,
            )
            self.assertIn(expected, projected.safety_violation_codes)

    def test_malformed_fixture_fails_capacity_and_epoch_projection(self) -> None:
        payload = _payload()
        containers = payload["containers"]
        assert isinstance(containers, list)
        assert isinstance(containers[0], dict)
        containers[0]["unexpected"] = 1
        for validator in (
            lambda: project_host_capacity_sample(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
                docker_memory_reserve_bytes=4096,
            ),
            lambda: project_host_service_epoch(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
            ),
        ):
            with self.assertRaises(ValueError):
                validator()

    def test_available_memory_uses_conservative_minimum_across_probes(self) -> None:
        payload = _payload()
        containers = payload["containers"]
        assert isinstance(containers, list)
        assert isinstance(containers[1], dict)
        containers[1]["docker_vm_memory_available_bytes"] = 7000
        projected = project_host_capacity_sample(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
            docker_memory_reserve_bytes=4096,
        )
        self.assertEqual(projected.docker_vm_memory_available_bytes, 7000)

    def test_service_epoch_has_no_operator_memory_reserve(self) -> None:
        payload = _payload(available=1)
        projected = project_host_service_epoch(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
        )

        self.assertEqual(projected.api_container_id, str(1) * 64)
        self.assertEqual(projected.restart_count_total, 0)
        self.assertNotIn(
            "reserve",
            json.dumps(projected.__dict__, sort_keys=True),
        )

    def test_synchronized_projection_is_closed_and_has_no_fixed_reserve(self) -> None:
        observed_at, process, host = project_synchronized_host_capacity_sample(
            _synchronized_payload(),
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
        )

        self.assertEqual(observed_at.isoformat(), "2026-08-29T00:00:00+00:00")
        self.assertEqual(process.cpu_user_ns_total, 100)
        self.assertEqual(host.memory_current_bytes, 20_000)
        self.assertEqual(host.cpu_psi.full_status, "unsupported")
        self.assertNotIn(
            "reserve",
            json.dumps(host.model_dump(mode="json"), sort_keys=True),
        )

    def test_synchronized_projection_rejects_missing_or_extra_metrics(self) -> None:
        for tamper in ("missing", "extra", "identity"):
            payload = _synchronized_payload()
            parent = payload["parent_cgroup"]
            assert isinstance(parent, dict)
            if tamper == "missing":
                parent.pop("memory_psi")
            elif tamper == "extra":
                parent["raw_process_command"] = "forbidden"
            else:
                payload["windows_node_identity_sha256"] = "sha256:" + "9" * 64
            with self.subTest(tamper=tamper), self.assertRaises(ValueError):
                project_synchronized_host_capacity_sample(
                    payload,
                    expected_collector_sha256=COLLECTOR,
                    expected_windows_node_identity_sha256=NODE,
                )


if __name__ == "__main__":
    unittest.main()
