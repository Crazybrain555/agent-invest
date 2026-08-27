"""DB-free CLI replay behavior for capacity evidence."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.capacity_observer import (
    run_capacity_observation,
)
from disclosure_anchor.adapters.runtime.capacity_runtime_identity import (
    CapacityRuntimeTopology,
)
from disclosure_anchor.cli import capacity as capacity_cli
from tests.unit.test_capacity_observer import _samplers, _settings


class CapacityCliTests(unittest.TestCase):
    def test_observe_uses_exact_runtime_and_host_key_identity_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_settings = _settings(root)
            settings = base_settings.model_copy(
                update={
                    "disclosure_mineru_api_url": "http://127.0.0.1:30002",
                    "disclosure_mineru_observability_url": (
                        "http://127.0.0.1:30003"
                    ),
                    "disclosure_gpu_metrics_url": "http://127.0.0.1:30004/metrics",
                    "disclosure_gpu_expected_uuid": (
                        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                    ),
                    "disclosure_mineru_docker_memory_reserve_bytes": 4096,
                }
            )
            run = run_capacity_observation(
                settings=base_settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )
            topology = CapacityRuntimeTopology(
                windows_collector_sha256="sha256:" + "2" * 64,
                windows_node_identity_sha256="sha256:" + "3" * 64,
                ssh_host_key_sha256="sha256:" + "4" * 64,
            )
            args = capacity_cli._parser().parse_args(  # noqa: SLF001
                [
                    "observe",
                    "--duration-seconds",
                    "0.01",
                    "--interval-seconds",
                    "0.01",
                    "--runtime-manifest",
                    str(root / "runtime.json"),
                    "--host-observer-ssh-host",
                    "100.64.0.1",
                    "--host-observer-ssh-user",
                    "operator",
                    "--host-observer-identity-file",
                    str(root / "identity"),
                    "--host-observer-known-hosts-file",
                    str(root / "known_hosts"),
                ]
            )
            with patch.object(
                capacity_cli,
                "verify_capacity_runtime_topology",
                return_value=topology,
            ) as verify_runtime, patch.object(
                capacity_cli,
                "build_host_observer_ssh_command",
                return_value=["ssh"],
            ) as build_ssh, patch.object(
                capacity_cli,
                "MineruApiCapacitySampler",
                return_value=_samplers()[0],
            ), patch.object(
                capacity_cli,
                "VllmCapacitySampler",
                return_value=_samplers()[3],
            ), patch.object(
                capacity_cli,
                "GpuCapacitySampler",
                return_value=_samplers()[1],
            ), patch.object(
                capacity_cli,
                "MineruHostCapacitySampler",
                return_value=_samplers()[2],
            ), patch.object(
                capacity_cli,
                "run_capacity_observation",
                return_value=run,
            ), redirect_stdout(StringIO()):
                code = capacity_cli._observe(args, settings)  # noqa: SLF001

            self.assertEqual(code, 0)
            verify_runtime.assert_called_once_with(
                settings=settings,
                runtime_manifest_path=root / "runtime.json",
            )
            self.assertEqual(
                build_ssh.call_args.kwargs["expected_host_key_sha256"],
                topology.ssh_host_key_sha256,
            )

    def test_verify_replays_without_network_and_can_require_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )
            with patch.object(
                capacity_cli,
                "load_settings",
                return_value=settings,
            ), redirect_stdout(StringIO()) as output:
                code = capacity_cli.main(
                    ["verify", "--run-id", run.run_id, "--require-complete"]
                )

            self.assertEqual(code, 0)
            self.assertIn('"activation_authorized":false', output.getvalue())

    def test_require_complete_rejects_replayable_incomplete_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(fail_gpu=True),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )
            with patch.object(
                capacity_cli,
                "load_settings",
                return_value=settings,
            ), redirect_stdout(StringIO()):
                code = capacity_cli.main(
                    ["verify", "--run-id", run.run_id, "--require-complete"]
                )

            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
