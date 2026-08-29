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


if __name__ == "__main__":
    unittest.main()
