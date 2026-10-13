"""Same-root maintenance surfaces not covered by the independent suites.

Plan recomputation, approval binding and fetched-record judgement are owned by
``test_windows_same_root_owner_independent``; this file pins the closed v3
binding against the Windows owner's own field list, the order of the prestop
and apply input gates (before any output or remote call), the release that
owns an operation, and the CLI surface.
"""

from __future__ import annotations

from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_release_install as installation
from disclosure_anchor.adapters.runtime import mineru_same_root_maintenance as maintenance
from disclosure_anchor.adapters.runtime.mineru_release_package import ReleaseIdentityError, ReleaseInputError
from disclosure_anchor.cli.mineru_release import build_parser


_OPERATION = "sr-" + "4" * 32
_HASH = "sha256:" + "a" * 64


def _inventory(*, age_seconds: int, members: list[dict[str, Any]] | None = None) -> bytes:
    captured = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    members = members or []
    return json.dumps({"contract_version": "worker-legacy-scope-inventory.v1", "captured_at_utc": captured,
                       "member_count": len(members), "members": members}).encode()


def _member() -> dict[str, Any]:
    sha = "sha256:" + "c" * 64
    return {
        "attempt_id": "attempt-1", "document_id": "document-1", "processing_run_id": "run-1",
        "attempt_generation": 0, "fence_identity": "fence-1", "h0_checkpoint_sha256": sha,
        "execution_spec_sha256": sha, "source_pdf_sha256": sha, "parser_target_sha256": sha, "request_sha256": sha,
        "runtime_epoch_sha256": sha, "client_submit_key": "key-1", "submission_epoch_unix": 1,
        "process_profile_sha256": sha, "worker_profile_sha256": sha, "observed_state": "submitted",
        "observed_lifecycle_version": 1, "observed_checkpoint_sha256": sha, "accepted_submission_sha256": None,
    }


class SameRootPhaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.binding = SimpleNamespace(
            windows=SimpleNamespace(
                hostname="synthetic-windows-host", workspace_root=PureWindowsPath("C:/agent-work"),
                expected_active_compose_sha256=_HASH, expected_previous_capacity_sha256=_HASH,
                job_lifetime_milliseconds=1_200_000, job_cleanup_milliseconds=5_000,
            ),
            mac_exclusive_lock_path=self.scratch / "deployment.lock", launchd_labels=(),
        )
        self.report = SimpleNamespace(passed=True, manifest=SimpleNamespace(sha256=_HASH))

    def run_phase(self, phase: str, output: Path, **options: Any) -> dict[str, Any]:
        return maintenance.run_same_root_phase(
            report=self.report, package=self.scratch, binding=self.binding, output=output,
            phase=phase, operation_id=_OPERATION, **options,
        )

    def test_binding_v3_is_exactly_the_windows_owner_closed_set(self) -> None:
        wrapper = (Path(__file__).resolve().parents[2] / "scripts/windows/run_mineru_installation.ps1").read_text(encoding="utf-8")
        block = re.search(r"m6\.installation-binding\.v3'\) \{\s*Assert-Closed \$binding @\(([^)]*)\)", wrapper)
        assert block is not None
        document = maintenance.same_root_binding_document(
            self.binding, api_device_profile="cuda0", phase="observe", operation_id=_OPERATION, exclusivity_sha256=_HASH,
        )
        self.assertEqual(set(document), set(re.findall(r"'([a-z0-9_]+)'", block.group(1))))
        self.assertEqual((document["approval_sha256"], document["legacy_scope_inventory_sha256"]), (None, None))
        for phase, extra in (("apply", {}), ("prestop", {}), ("observe", {"approval_sha256": _HASH}),
                             ("restore", {"legacy_scope_inventory_sha256": _HASH}), ("rollback", {})):
            with self.subTest(phase=phase), self.assertRaises(ReleaseInputError):
                maintenance.same_root_binding_document(
                    self.binding, api_device_profile="cuda0", phase=phase, operation_id=_OPERATION,
                    exclusivity_sha256=_HASH, **extra,
                )

    def test_prestop_refuses_open_stale_or_malformed_duty_before_any_output_or_remote_call(self) -> None:
        for name, raw, error in (
            ("member", _inventory(age_seconds=30, members=[_member()]), ReleaseIdentityError),
            ("stale", _inventory(age_seconds=maintenance.LEGACY_SCOPE_MAX_AGE_SECONDS + 60), ReleaseIdentityError),
            ("malformed", b'{"contract_version":"worker-legacy-scope-inventory.v1"}', ReleaseInputError),
        ):
            inventory = self.scratch / f"{name}.json"
            inventory.write_bytes(raw)
            output = self.scratch / f"out-{name}"
            with self.subTest(name=name), patch.object(installation, "_run_owned", side_effect=AssertionError("remote")):
                with self.assertRaises(error):
                    self.run_phase("prestop", output, legacy_scope_inventory_path=inventory)
                self.assertFalse(output.exists())

    def test_apply_needs_this_operations_approval_and_only_prestop_resumes(self) -> None:
        approval = self.scratch / "approval.json"
        approval.write_bytes(json.dumps({
            "contract_version": "m6.same-root-approval.v1", "operation_id": "sr-" + "5" * 32, "plan_sha256": _HASH,
            "approved_by": "root", "decision_reference": "other", "approved_at_utc": "2026-10-07T00:00:00Z"}).encode())
        with self.assertRaisesRegex(ReleaseIdentityError, "another operation"):
            self.run_phase("apply", self.scratch / "apply", approval_path=approval)
        with self.assertRaises(ReleaseInputError):
            self.run_phase("observe", self.scratch / "resume", resume=True)
        with self.assertRaises(ReleaseInputError):
            self.run_phase("restore", self.scratch / "approval-elsewhere", approval_path=approval)
        self.assertEqual([path.name for path in self.scratch.iterdir() if path.is_dir()], [])

    def test_plan_verification_refuses_an_operation_started_by_another_release(self) -> None:
        package = self.scratch / "package"
        (package / "api-context").mkdir(parents=True)
        shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts/windows/mineru_heap_trim_compat/agent_task_protocol_v2.py",
                        package / "api-context" / "agent_task_protocol_v2.py")
        for prestop_release, refusal in (("sha256:" + "b" * 64, (ReleaseIdentityError, "another release package")),
                                         (_HASH, (ReleaseInputError, "observe child result"))):
            outputs: dict[str, Path] = {}
            for phase, release in (("prestop", prestop_release), ("observe", _HASH)):
                outputs[phase] = self.scratch / prestop_release[7:15] / phase
                outputs[phase].mkdir(parents=True)
                (outputs[phase] / "same-root-result.json").write_bytes(json.dumps({
                    "contract_version": maintenance.SAME_ROOT_RESULT_CONTRACT, "status": "pass", "phase": phase,
                    "operation_id": _OPERATION, "release_manifest_sha256": release}).encode())
            # Same release: the gate passes and verification stops at the next missing record instead.
            with self.subTest(prestop_release=prestop_release), self.assertRaisesRegex(*refusal):
                maintenance.verify_same_root_plan(
                    report=self.report, package=package, prestop_output=outputs["prestop"], observe_output=outputs["observe"],
                )

    def test_cli_selects_one_explicit_phase_with_its_own_inputs(self) -> None:
        parser = build_parser()
        common = ["--package", "/tmp/p", "--private-binding", "/tmp/b", "--output", "/tmp/o"]
        prestop = parser.parse_args(["same-root", "prestop", *common, "--legacy-scope-inventory", "/tmp/i"])
        self.assertEqual((prestop.action, prestop.operation_id), ("prestop", None))
        apply = parser.parse_args(["same-root", "apply", *common, "--operation-id", _OPERATION, "--approval", "/tmp/a"])
        self.assertEqual(apply.approval, Path("/tmp/a"))
        for argv in (
            ["same-root", "prestop", *common],
            ["same-root", "apply", *common, "--operation-id", _OPERATION],
            ["same-root", "observe", *common],
            ["same-root", "approve", "--package", "/tmp/p", "--output", "/tmp/o"],
            ["same-root", "rollback", *common, "--operation-id", _OPERATION],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parser.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
