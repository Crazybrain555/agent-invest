"""Active MinerU execution must not regain the retired candidate/depth-one path."""

from __future__ import annotations

from pathlib import Path
import unittest


class RetiredMineruCandidateClosureTests(unittest.TestCase):
    def test_active_execution_surface_has_no_retired_candidate_contract(self) -> None:
        service_root = Path(__file__).resolve().parents[2]
        active_paths = (
            service_root / "Makefile",
            service_root / "config/mineru-windows.compose.yaml",
            service_root / "config/README.md",
            service_root / "docs/implementation/runbooks/production-operations.md",
            service_root / "scripts/attest_mineru_remote_runtime.py",
            service_root / "scripts/collect_mineru_phase_trace.py",
            service_root / "scripts/windows/collect_mineru_runtime.ps1",
            service_root / "scripts/windows/install_mineru_fixed_api.ps1",
            service_root
            / "scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py",
            service_root
            / "src/disclosure_anchor/adapters/runtime/mineru_phase_trace.py",
            service_root
            / "src/disclosure_anchor/adapters/runtime/mineru_phase_trace_capture.py",
            service_root / "src/disclosure_anchor/settings.py",
            service_root / "src/disclosure_anchor/adapters/runtime/mineru_identity.py",
            service_root
            / "src/disclosure_anchor/adapters/runtime/mineru_deployment_gate.py",
            service_root
            / "src/disclosure_anchor/application/contracts/mineru_api_health.py",
            service_root
            / "src/disclosure_anchor/adapters/runtime/mineru_orchestrator.py",
        )
        retired_tokens = (
            "MINERU_" + "CAPACITY_PROFILE_JSON",
            "mineru-execution-profile." + "v2",
            "depth" + "1",
            "Capacity" + "CreditBank",
            "run_bounded_ordered_" + "pipeline",
            "_aio_run_hybrid_capacity_" + "pipeline",
            "window_b_" + "queue_wait",
            "window_credit_" + "wait",
            "window_" + "release",
            "trace_ready_" + "ns",
            "process_stage_" + "gates",
            "MINERU_API_MAX_SUPPORTED_" + "TASK_SLOTS",
            "MINERU_API_MAX_SUPPORTED_" + "PENDING_TASKS",
            '"legacy", "' + "candidate" + '"',
            '"legacy"' + "|" + '"candidate"',
        )
        findings = []
        for path in active_paths:
            contents = path.read_text(encoding="utf-8")
            findings.extend(
                f"{path.relative_to(service_root)}: {token}"
                for token in retired_tokens
                if token in contents
            )
        self.assertEqual(findings, [])

    def test_active_execution_surface_has_no_retired_task_protocol_compatibility(
        self,
    ) -> None:
        service_root = Path(__file__).resolve().parents[2]
        staged_client = (
            service_root
            / "src/disclosure_anchor/adapters/parsers/mineru_medium/http_staged.py"
        ).read_text(encoding="utf-8")
        patcher = (
            service_root
            / "scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py"
        ).read_text(encoding="utf-8")
        installer = (
            service_root / "scripts/windows/install_mineru_fixed_api.ps1"
        ).read_text(encoding="utf-8")
        compose = (service_root / "config/mineru-windows.compose.yaml").read_text(
            encoding="utf-8"
        )
        dockerfile = (
            service_root / "scripts/windows/mineru_heap_trim_compat/Dockerfile"
        ).read_text(encoding="utf-8")

        retired_client_tokens = (
            "task_protocol_v2: bool = False",
            "task_protocol_v2=True",
            'payload.get("task_protocol_v2", False)',
            'payload.get("v") not in {1, 2, 3}',
            "_spool_result",
        )
        retired_server_tokens = (
            "task_protocol_v2 is None",
            "task_protocol_v2 is not None",
            "if self.task_protocol_v2:",
            "if task_manager.task_protocol_v2:",
        )
        self.assertEqual(
            [token for token in retired_client_tokens if token in staged_client], []
        )
        self.assertEqual(
            [token for token in retired_server_tokens if token in patcher], []
        )
        self.assertNotIn("One-time migration compatibility", installer)
        self.assertIn(
            "existing MinerU topology is not the exact proxy-isolated topology",
            installer,
        )
        for artifact in (compose, dockerfile):
            self.assertNotIn("MINERU_TASK_PROTOCOL_V2:", artifact)
            self.assertNotIn("MINERU_TASK_PROTOCOL_V2=", artifact)


if __name__ == "__main__":
    unittest.main()
