"""Golden parity tests for staged and passive host-capacity validation."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import scripts.mineru_staged_load as staged
from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    build_host_observer_ssh_command,
    project_host_capacity_sample,
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
        "collector_path": staged.MINERU_WINDOWS_COLLECTOR_PATH,
        "collector_sha256": COLLECTOR,
        "windows_node_identity_sha256": NODE,
        "containers": containers,
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

    def test_safe_fixture_matches_staged_validator_and_removes_raw_ids(self) -> None:
        payload = _payload()
        staged._validate_host_capacity_sample(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
            docker_memory_reserve_bytes=4096,
        )
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

    def test_reserve_and_restart_violations_have_staged_parity(self) -> None:
        for payload, expected in (
            (_payload(available=1024), "memory_reserve_crossed"),
            (_payload(restart=1), "container_state_unsafe"),
        ):
            with self.subTest(expected=expected), self.assertRaises(
                staged._TrustedHostCapacityViolation
            ):
                staged._validate_host_capacity_sample(
                    payload,
                    expected_collector_sha256=COLLECTOR,
                    expected_windows_node_identity_sha256=NODE,
                    docker_memory_reserve_bytes=4096,
                )
            projected = project_host_capacity_sample(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
                docker_memory_reserve_bytes=4096,
            )
            self.assertIn(expected, projected.safety_violation_codes)

    def test_malformed_fixture_fails_both_validators(self) -> None:
        payload = _payload()
        containers = payload["containers"]
        assert isinstance(containers, list)
        assert isinstance(containers[0], dict)
        containers[0]["unexpected"] = 1
        for validator in (
            lambda: staged._validate_host_capacity_sample(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
                docker_memory_reserve_bytes=4096,
            ),
            lambda: project_host_capacity_sample(
                payload,
                expected_collector_sha256=COLLECTOR,
                expected_windows_node_identity_sha256=NODE,
                docker_memory_reserve_bytes=4096,
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
        staged._validate_host_capacity_sample(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
            docker_memory_reserve_bytes=4096,
        )

        projected = project_host_capacity_sample(
            payload,
            expected_collector_sha256=COLLECTOR,
            expected_windows_node_identity_sha256=NODE,
            docker_memory_reserve_bytes=4096,
        )
        self.assertEqual(projected.docker_vm_memory_available_bytes, 7000)


if __name__ == "__main__":
    unittest.main()
