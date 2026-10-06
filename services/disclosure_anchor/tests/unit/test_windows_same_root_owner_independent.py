"""Independent owner tests at the real Mac evidence/transport boundary.

Transport responses are recorded fixtures; local operation records use disposable
directories. These tests cannot attest Windows Job/Docker daemon lifecycle,
restart-policy effects, NTFS identity, or a Windows entry point's pending gate.
"""

from __future__ import annotations

import copy
import json
import shutil
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_release_install as installation
from disclosure_anchor.adapters.runtime import mineru_same_root_maintenance as maintenance
from disclosure_anchor.adapters.runtime.mineru_release_package import ReleaseIdentityError
from disclosure_anchor.application.contracts import mineru_same_root as same_root_contract
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyScopeInventory, encode_legacy_scope_inventory,
)
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as native
from tests.unit.test_same_root_maintenance_independent import (
    _SameRootCase as _RegistryFixture, _canonical, _digest,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from tests._mineru_capacity_config_fixture import capacity_payload
from tests._mineru_capacity_v11_fixture import idle_health


_HASH = "sha256:" + "a" * 64


class _OwnerCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.capacity = MineruCapacityConfig(**capacity_payload())
        self.binding = SimpleNamespace(
            windows=SimpleNamespace(
                hostname="synthetic-windows-host", expected_active_compose_sha256=_HASH,
                expected_previous_capacity_sha256=self.capacity.sha256,
                job_lifetime_milliseconds=1_200_000, job_cleanup_milliseconds=5_000,
            ),
            mac_exclusive_lock_path=self.scratch / "deployment.lock", launchd_labels=(),
            previous_capacity=None, api_url="http://fixture.invalid",
        )
        self.report = SimpleNamespace(
            passed=True,
            manifest=SimpleNamespace(sha256=_HASH, projection={"compose_sha256": _HASH}),
            inputs=SimpleNamespace(
                capacity=self.capacity,
                deployment_profile=SimpleNamespace(
                    api_task_retention_seconds=600, api_task_cleanup_interval_seconds=30,
                ),
            ),
        )

    def ordinary_install(self, health: dict | None) -> tuple[dict, int]:
        raw = json.dumps(health).encode() if health is not None else b""
        with patch.object(installation, "assert_known_hosts_pins_address"), patch.object(
            installation, "assert_launchd_jobs_unloaded",
            return_value={"com.agentinvest.disclosure-worker": "not_loaded", "com.agentinvest.disclosure-gc": "not_loaded"},
        ), patch.object(
            installation, "fetch_json", **({"side_effect": OSError("API intentionally stopped")} if health is None else {"return_value": (health, raw)}),
        ), patch.object(installation, "_run_owned", side_effect=AssertionError("refused input reached remote mutation")) as remote:
            result = installation.install_release(
                report=self.report, package=self.scratch, binding=self.binding,
                output=self.scratch / "ordinary-install", operation_kind=installation.API_COMPATIBILITY,
            )
        return result, remote.call_count


class OrdinaryInstallationGatePreservationTests(_OwnerCase):
    def test_api_only_still_refuses_a_stopped_api_before_any_remote_owner_command(self) -> None:
        result, calls = self.ordinary_install(None)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["installation_verified"])
        self.assertEqual(result["write_permission"], "closed")
        self.assertEqual(calls, 0)
        self.assertIn("API intentionally stopped", result["first_error"])
        lock = installation.acquire_exclusivity(self.binding.mac_exclusive_lock_path, holder={"purpose": "check-released"})
        lock.release()

    def test_api_only_rejects_nonzero_responsibility_even_with_zero_queued_and_processing(self) -> None:
        health = copy.deepcopy(idle_health(capacity_payload()))
        health["task_admission"]["accepted_finalizing_tasks"] = 1
        result, calls = self.ordinary_install(health)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["installation_verified"])
        self.assertEqual(result["write_permission"], "closed")
        self.assertEqual(calls, 0)


_OPERATION = "sr-" + "7" * 32
_NOW = datetime(2026, 10, 7, 1, 0, 0, tzinfo=UTC)


class _PlanEvidenceCase(_RegistryFixture):
    """Fetched Windows records, seeded independently from a valid consumed registry."""

    def setUp(self) -> None:
        super().setUp()
        self.package = self.scratch / "package"
        context = self.package / "api-context"
        context.mkdir(parents=True)
        shutil.copyfile(native.__file__, context / "agent_task_protocol_v2.py")
        self.report = SimpleNamespace(passed=True, manifest=SimpleNamespace(sha256=_HASH))
        self.prestop = self.scratch / "prestop"
        self.observed = self.scratch / "observed"
        self.old = dict(path="/var/lib/mineru-api-output", device=9007199254740993,
                        inode=9223372036854775806, uid=0, mode=16832)
        self.current = dict(self.old, device=9007199254740992, inode=9223372036854775805)
        seed = {k: v for k, v in self.payload.items() if k != "output_root"}
        self.original = _canonical(dict(seed, output_root=self.old))
        self.candidate = _canonical(dict(seed, output_root=self.current))
        self.body = _digest(_canonical(seed))
        self.observation = dict(
            schema="mineru-same-root-observation.v1", root=self.old["path"],
            registry_schema="mineru-task-registry.v4", approved_root=self.old, current_root=self.current,
            registry_state="r0", outcome="reattest", r0_sha256=_digest(self.original),
            r0_bytes=len(self.original), body_sha256=self.body, r1_sha256=_digest(self.candidate),
            r1_bytes=len(self.candidate), record_count=3, submission_watermark_bucket=1900000123,
            registry_file=dict(device=self.current["device"], inode=31, uid=0, mode=33152,
                               nlink=1, size=len(self.original)),
            control_directory=dict(device=self.current["device"], inode=32, uid=0, mode=16832),
            spool_directory=dict(device=self.current["device"], inode=33, uid=0, mode=16832),
        )
        self.plan = dict(
            contract_version="mineru-same-root-plan.v1", operation_id=_OPERATION, sequence=1,
            generation=1, baseline="prestop", captured_utc="2026-10-07T01:00:00Z",
            release_manifest_sha256=_HASH, api_image_id=_HASH, windows_identity_sha256=_HASH,
            registry_file_id="00000000000000310000000000000000", approved_root=self.old,
            current_root=self.current, outcome="reattest", registry_state="r0",
            expected_r0_sha256=_digest(self.original), expected_r0_bytes=len(self.original),
            expected_body_sha256=self.body, expected_r1_sha256=_digest(self.candidate),
            expected_r1_bytes=len(self.candidate), record_count=3,
            submission_watermark_bucket=1900000123, helper_observation="",
        )
        for output, phase in ((self.prestop, "prestop"), (self.observed, "observe")):
            state = output / "windows-operation" / "same-root" / "state"
            state.mkdir(parents=True)
            summary = dict(contract_version=maintenance.SAME_ROOT_RESULT_CONTRACT, status="pass",
                           phase=phase, operation_id=_OPERATION, write_permission="closed",
                           release_manifest_sha256=_HASH)
            (output / "same-root-result.json").write_bytes(_canonical(summary))
            proof = dict(contract_version="m6.same-root-api-proof.v1", operation_id=_OPERATION,
                         phase="prestop", proof_line=_canonical(dict(
                             file_count=1, total_bytes=len(self.original), quiescence=dict(
                                 root_identity=self.old, registry_sha256=_digest(self.original),
                                 record_count=3, submission_watermark_bucket=1900000123,
                             ),
                         )).decode())
            (state / "api-proof-prestop.json").write_bytes(_canonical(proof))
            (state / "prestop.json").write_bytes(_canonical(dict(
                contract_version="mineru-same-root-prestop.v1", operation_id=_OPERATION,
                release_manifest_sha256=_HASH, api_image_id=_HASH,
                registry_sha256=_digest(self.original), registry_bytes=len(self.original),
                body_sha256=self.body, completed_utc="2026-10-07T01:00:00Z",
            )))
            (state / "r0.registry.json").write_bytes(self.original)
            (state / "baseline.json").write_bytes(_canonical(dict(body_sha256=self.body)))
        self.state = self.observed / "windows-operation" / "same-root" / "state"
        (self.state / "plan-1.registry.json").write_bytes(self.original)
        self.persist_plan()

    def persist_plan(self) -> None:
        self.plan["helper_observation"] = _canonical(self.observation).decode()
        raw = _canonical(self.plan)
        name = f"plan-{self.plan['sequence']}.json"
        (self.state / name).write_bytes(raw)
        child = dict(contract_version="m6.same-root-child-result.v1", phase="observe",
                     operation_id=_OPERATION, status="pass", first_error=None, uncertain=False,
                     outcome=self.plan["outcome"], pending_present=True,
                     facts=dict(plan=dict(name=name, sha256=_digest(raw))),
                     exported_records=[], elapsed_milliseconds=1)
        (self.state.parent / "same-root-result.json").write_bytes(_canonical(child))

    def verify(self) -> dict:
        return maintenance.verify_same_root_plan(
            report=self.report, package=self.package, prestop_output=self.prestop,
            observe_output=self.observed,
        )

    def set_restated(self, plan_key: str, observation_key: str, value: object) -> None:
        self.plan[plan_key] = value
        self.observation[observation_key] = value
        self.persist_plan()


class SameRootMacPlanConservationTests(_PlanEvidenceCase):
    def test_recomputes_exact_three_record_pair_and_high_integer_root_without_normal_constructor(self) -> None:
        packaged = maintenance.load_packaged_protocol(self.package)
        with patch.object(packaged.DurableTaskRegistry, "__init__", side_effect=AssertionError("normal recovery is forbidden")):
            result = self.verify()
        self.assertEqual(result["approved_root"], self.old)
        self.assertEqual(result["current_root"], self.current)
        self.assertIs(type(result["approved_root"]["device"]), int)
        self.assertEqual(result["r0_sha256"], _digest(self.original))
        self.assertEqual(result["r1_sha256"], _digest(self.candidate))
        self.assertEqual(result["body_sha256"], self.body)
        self.assertEqual((result["record_count"], result["submission_watermark_bucket"]), (3, 1900000123))
        self.assertEqual((self.state / "plan-1.registry.json").read_bytes(), self.original)

    def test_already_published_r1_reconstructs_and_binds_the_same_original_pair(self) -> None:
        self.set_restated("registry_state", "registry_state", "r1")
        (self.state / "plan-1.registry.json").write_bytes(self.candidate)
        result = self.verify()
        self.assertEqual(result["registry_state"], "r1")
        self.assertEqual(result["r0_sha256"], _digest(self.original))
        self.assertEqual(result["r1_sha256"], _digest(self.candidate))

    def test_r1_resume_rejects_wrong_original_digest_and_length(self) -> None:
        self.set_restated("registry_state", "registry_state", "r1")
        (self.state / "plan-1.registry.json").write_bytes(self.candidate)
        for field, observed, wrong in (("expected_r0_sha256", "r0_sha256", "sha256:" + "0" * 64),
                                        ("expected_r0_bytes", "r0_bytes", len(self.original) + 1)):
            with self.subTest(field=field):
                old = self.plan[field]
                self.set_restated(field, observed, wrong)
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                self.set_restated(field, observed, old)

    def test_rejects_wrong_candidate_digest_or_length_even_when_helper_restates_it(self) -> None:
        for field, observed, wrong in (("expected_r1_sha256", "r1_sha256", "sha256:" + "0" * 64),
                                        ("expected_r1_bytes", "r1_bytes", len(self.candidate) + 1)):
            with self.subTest(field=field):
                old = self.plan[field]
                self.set_restated(field, observed, wrong)
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                self.set_restated(field, observed, old)

    def test_rejects_uid_mode_path_change_and_boolean_or_rounded_identity(self) -> None:
        for field, wrong in (("uid", 1), ("mode", 16877), ("path", "/different-output"),
                              ("device", True), ("inode", float(self.current["inode"]))):
            with self.subTest(field=field):
                self.set_restated("current_root", "current_root", dict(self.current, **{field: wrong}))
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                self.set_restated("current_root", "current_root", self.current)

    def test_rejects_conservation_mismatch_in_count_watermark_or_body(self) -> None:
        for field, observed, wrong in (("record_count", "record_count", 2),
                                        ("submission_watermark_bucket", "submission_watermark_bucket", 1900000124),
                                        ("expected_body_sha256", "body_sha256", "sha256:" + "0" * 64)):
            with self.subTest(field=field):
                old = self.plan[field]
                self.set_restated(field, observed, wrong)
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                self.set_restated(field, observed, old)

    def test_rejects_helper_plan_disagreement_or_unknown_members(self) -> None:
        original = copy.deepcopy(self.plan)
        self.plan["record_count"] = 4
        self.persist_plan()
        with self.assertRaises(ReleaseIdentityError):
            self.verify()
        self.plan = copy.deepcopy(original)
        self.plan["unknown_member"] = None
        self.persist_plan()
        with self.assertRaises(ReleaseIdentityError):
            self.verify()

    def test_rejects_plan_and_summary_from_another_operation_or_release(self) -> None:
        original = copy.deepcopy(self.plan)
        for field, wrong in (("operation_id", "sr-" + "0" * 32), ("release_manifest_sha256", "sha256:" + "0" * 64)):
            with self.subTest(field=field):
                self.plan = copy.deepcopy(original)
                self.plan[field] = wrong
                self.persist_plan()
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()

    def test_rejects_fetched_plan_or_prestop_bytes_that_change_after_their_record(self) -> None:
        for path in (self.state / "plan-1.json", self.state / "r0.registry.json",
                     self.state / "baseline.json", self.state / "api-proof-prestop.json"):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                path.write_bytes(before)

    def test_live_or_resource_owning_record_cannot_be_approved_even_with_self_consistent_hashes(self) -> None:
        baseline_plan = copy.deepcopy(self.plan)
        baseline_observation = copy.deepcopy(self.observation)
        for change in (dict(state="running"), dict(storage={"unexpected": "resource"})):
            with self.subTest(change=change):
                value = json.loads(self.original)
                value["records"][0].update(change)
                raw = _canonical(value)
                body = _digest(_canonical({k: v for k, v in value.items() if k != "output_root"}))
                for output in (self.prestop, self.observed):
                    state = output / "windows-operation/same-root/state"
                    (state / "r0.registry.json").write_bytes(raw)
                    (state / "baseline.json").write_bytes(_canonical(dict(body_sha256=body)))
                    proof = json.loads((state / "api-proof-prestop.json").read_bytes())
                    inside = json.loads(proof["proof_line"])
                    inside["total_bytes"] = len(raw)
                    inside["quiescence"]["registry_sha256"] = _digest(raw)
                    proof["proof_line"] = _canonical(inside).decode()
                    (state / "api-proof-prestop.json").write_bytes(_canonical(proof))
                self.plan = copy.deepcopy(baseline_plan)
                self.observation = copy.deepcopy(baseline_observation)
                self.plan.update(expected_r0_sha256=_digest(raw), expected_r0_bytes=len(raw), expected_body_sha256=body)
                self.observation.update(r0_sha256=_digest(raw), r0_bytes=len(raw), body_sha256=body)
                (self.state / "plan-1.registry.json").write_bytes(raw)
                self.persist_plan()
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()

    def next_mount_plan(self) -> tuple[dict, bytes]:
        # A second storage observation must use this operation's latest actual
        # published registry. The initial prestop proof stays unchanged.
        next_root = dict(self.current, device=self.current["device"] + 5, inode=self.current["inode"] + 11)
        previous = dict(contract_version="mineru-same-root-apply-record.v1", operation_id=_OPERATION,
                        generation=1, current_root=self.current, registry_sha256=_digest(self.candidate),
                        registry_bytes=len(self.candidate), registry_file_id="synthetic-r1-id")
        (self.state / "apply-1.json").write_bytes(_canonical(previous))
        next_payload = json.loads(self.candidate)
        next_payload["output_root"] = next_root
        next_bytes = _canonical(next_payload)
        self.plan.update(sequence=2, generation=2, baseline="apply-1.json", approved_root=self.current,
                         current_root=next_root, expected_r0_sha256=_digest(self.candidate),
                         expected_r0_bytes=len(self.candidate), expected_r1_sha256=_digest(next_bytes),
                         expected_r1_bytes=len(next_bytes))
        self.observation.update(approved_root=self.current, current_root=next_root,
                                r0_sha256=_digest(self.candidate), r0_bytes=len(self.candidate),
                                r1_sha256=_digest(next_bytes), r1_bytes=len(next_bytes))
        (self.state / "plan-2.registry.json").write_bytes(self.candidate)
        self.persist_plan()
        return next_root, next_bytes

    def test_later_mount_starts_from_latest_published_registry_preserving_initial_body(self) -> None:
        next_root, next_bytes = self.next_mount_plan()
        result = self.verify()
        self.assertEqual(result["generation"], 2)
        self.assertEqual(result["approved_root"], self.current)
        self.assertEqual(result["current_root"], next_root)
        self.assertEqual(result["r0_sha256"], _digest(self.candidate))
        self.assertEqual(result["r1_sha256"], _digest(next_bytes))
        self.assertEqual(result["body_sha256"], self.body)
        self.assertEqual((self.state / "r0.registry.json").read_bytes(), self.original)

    def test_later_mount_cannot_reuse_initial_r0_after_a_verified_re_attestation(self) -> None:
        self.next_mount_plan()
        self.plan.update(approved_root=self.old, expected_r0_sha256=_digest(self.original),
                         expected_r0_bytes=len(self.original))
        self.observation.update(approved_root=self.old, r0_sha256=_digest(self.original),
                                r0_bytes=len(self.original))
        self.persist_plan()
        with self.assertRaises(ReleaseIdentityError):
            self.verify()

    def test_same_operation_cannot_verify_a_current_release_plan_from_another_prestop_release(self) -> None:
        old_manifest = "sha256:" + "b" * 64
        summary_path = self.prestop / "same-root-result.json"
        summary_bytes = summary_path.read_bytes()
        state_paths = [output / "windows-operation" / "same-root" / "state" / "prestop.json"
                       for output in (self.prestop, self.observed)]
        state_bytes = [path.read_bytes() for path in state_paths]
        for boundary in ("summary", "immutable_prestop_record", "both"):
            with self.subTest(boundary=boundary):
                summary = json.loads(summary_bytes)
                if boundary in ("summary", "both"):
                    summary["release_manifest_sha256"] = old_manifest
                summary_path.write_bytes(_canonical(summary))
                for path, raw in zip(state_paths, state_bytes, strict=True):
                    state = json.loads(raw)
                    if boundary in ("immutable_prestop_record", "both"):
                        state["release_manifest_sha256"] = old_manifest
                    path.write_bytes(_canonical(state))
                # The same operation, exact pair/body, current plan/report pin,
                # packaged real protocol, and copied old evidence all remain valid.
                # The origin package identity must independently block approval.
                with self.assertRaises(ReleaseIdentityError):
                    self.verify()
                self.assertEqual((self.state / "plan-1.registry.json").read_bytes(), self.original)
                self.assertEqual(self.plan["release_manifest_sha256"], self.report.manifest.sha256)

    def test_approval_binds_one_verified_digest_and_keeps_all_bytes_unchanged(self) -> None:
        output = self.scratch / "approval"
        result = maintenance.write_same_root_approval(
            report=self.report, package=self.package, prestop_output=self.prestop, observe_output=self.observed,
            approved_by="synthetic operator", decision_reference="synthetic decision", output=output, now=_NOW,
        )
        approval = same_root_contract.decode_same_root_approval((output / "same-root-approval.json").read_bytes())
        self.assertEqual(approval.operation_id, _OPERATION)
        self.assertEqual(approval.plan_sha256, _digest((self.state / "plan-1.json").read_bytes()))
        self.assertEqual(result["approval_sha256"], _digest((output / "same-root-approval.json").read_bytes()))
        self.assertEqual((self.state / "plan-1.registry.json").read_bytes(), self.original)
        self.assertEqual(approval.approved_at_utc, "2026-10-07T01:00:00Z")

    def test_noop_verifies_but_cannot_obtain_a_write_approval(self) -> None:
        self.set_restated("current_root", "current_root", self.old)
        self.set_restated("outcome", "outcome", "noop")
        self.set_restated("expected_r1_sha256", "r1_sha256", _digest(self.original))
        self.set_restated("expected_r1_bytes", "r1_bytes", len(self.original))
        self.assertEqual(self.verify()["outcome"], "noop")
        output = self.scratch / "noop-approval"
        with self.assertRaises(ReleaseIdentityError):
            maintenance.write_same_root_approval(
                report=self.report, package=self.package, prestop_output=self.prestop, observe_output=self.observed,
                approved_by="synthetic operator", decision_reference="synthetic decision", output=output, now=_NOW,
            )
        self.assertFalse(output.exists())


class SameRootMacRecordedTransportTests(_OwnerCase):
    def run_recorded(self, operation: dict | bytes | None, *, wrapper_exit: int | None = 0) -> tuple[dict, list[str]]:
        self.binding.windows.workspace_root = PureWindowsPath("C:/synthetic-workspace")
        self.binding.ssh_executable = Path("/synthetic-unused-ssh")
        self.binding.ssh_executable_sha256 = _HASH
        self.binding.ssh = SimpleNamespace()
        self.report.manifest.installation = {"wrapper_sha256": _HASH}
        self.report.inputs.deployment_profile.api_device_profile = "cuda"
        labels = []
        output = self.scratch / "same-root-observe"

        def transport(*_arguments, **kwargs):
            label = kwargs["label"]
            labels.append(label)
            if label == "wrapper" and wrapper_exit is None:
                raise installation.InstallationOutcomeUnknown("synthetic SSH acknowledgment lost")
            if label == "fetch" and operation is not None:
                directory = output / "windows-operation"
                directory.mkdir()
                (directory / "operation-result.json").write_bytes(
                    operation if type(operation) is bytes else _canonical(operation)
                )
            return SimpleNamespace(exit_code=wrapper_exit if label == "wrapper" else 0)

        with ExitStack() as stack:
            stack.enter_context(patch.object(installation, "assert_known_hosts_pins_address"))
            stack.enter_context(patch.object(installation, "assert_launchd_jobs_unloaded", return_value={"synthetic-worker": "not_loaded"}))
            stack.enter_context(patch.object(installation, "read_idle_health", side_effect=AssertionError("observe must work with stopped API")))
            stack.enter_context(patch.object(installation, "stage_batch", return_value="synthetic package staging only\n"))
            stack.enter_context(patch.object(installation, "_sftp_argv", return_value=["recorded-sftp"]))
            stack.enter_context(patch.object(installation, "_run_owned", side_effect=transport))
            stack.enter_context(patch.object(maintenance, "pinned_windows_script", return_value="recorded pinned script"))
            stack.enter_context(patch.object(maintenance, "owner_ssh_command", return_value=["recorded-ssh"]))
            result = maintenance.run_same_root_phase(
                report=self.report, package=self.scratch, binding=self.binding, output=output,
                phase="observe", operation_id=_OPERATION,
            )
        self.assertEqual(result["write_permission"], "closed")
        self.assertEqual(labels, ["stage", "wrapper", "fetch"])
        lock = installation.acquire_exclusivity(self.binding.mac_exclusive_lock_path, holder={"purpose": "check-released"})
        lock.release()
        return result, labels

    @staticmethod
    def operation(**overrides) -> dict:
        value = dict(contract_version="m6.installation-operation.v3", operation_kind="same-root-reattestation",
                     maintenance_phase="observe", operation_id=_OPERATION, status="pass", first_error=None,
                     daemon_side_outcome="verified", same_root_outcome="reattest", pending_present=True)
        value.update(overrides)
        return value

    def test_observe_uses_fetched_phase_evidence_while_api_stopped_and_preserves_closed_write_permission(self) -> None:
        result, _ = self.run_recorded(self.operation())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["next_required"], ["approve", "apply"])
        self.assertTrue(result["pending_present"])

    def test_lost_wrapper_ack_with_a_pass_record_still_requires_read_only_recovery(self) -> None:
        result, _ = self.run_recorded(self.operation(), wrapper_exit=None)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["next_required"], ["same_root_status", "read_only_recovery_judgement"])
        self.assertIn("acknowledgment lost", result["first_error"])

    def test_missing_or_unreadable_fetched_result_is_unknown_never_retried(self) -> None:
        for wire in (None, b"{", b'{"status":"pass","status":"failed"}'):
            with self.subTest(wire=wire):
                output = self.scratch / "same-root-observe"
                if output.exists():
                    shutil.rmtree(output)
                result, _ = self.run_recorded(wire)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["daemon_side_outcome"], "unknown")

    def test_wrong_operation_or_phase_record_cannot_turn_cli_success_into_pass(self) -> None:
        for changed in (dict(operation_id="sr-" + "0" * 32), dict(maintenance_phase="apply"),
                        dict(contract_version="m6.installation-operation.v2")):
            with self.subTest(changed=changed):
                output = self.scratch / "same-root-observe"
                if output.exists():
                    shutil.rmtree(output)
                result, _ = self.run_recorded(self.operation(**changed))
                self.assertEqual(result["status"], "unknown")

    def test_unknown_daemon_side_cannot_be_overridden_by_pass_status(self) -> None:
        result, _ = self.run_recorded(self.operation(daemon_side_outcome="unknown"))
        self.assertEqual(result["status"], "unknown")

    def test_verified_daemon_with_nonzero_wrapper_exit_does_not_pass(self) -> None:
        result, _ = self.run_recorded(self.operation(), wrapper_exit=70)
        self.assertEqual(result["status"], "failed")

    def test_fresh_zero_inventory_has_bounded_age_and_stale_future_capture_refuses(self) -> None:
        fresh = encode_legacy_scope_inventory(LegacyScopeInventory(captured_at_utc="2026-10-07T01:00:00Z", members=()))
        self.assertEqual(maintenance.require_zero_duty_inventory(fresh, now=_NOW).members, ())
        for now in (_NOW + timedelta(seconds=901), _NOW - timedelta(seconds=61)):
            with self.subTest(now=now), self.assertRaises(ReleaseIdentityError):
                maintenance.require_zero_duty_inventory(fresh, now=now)
