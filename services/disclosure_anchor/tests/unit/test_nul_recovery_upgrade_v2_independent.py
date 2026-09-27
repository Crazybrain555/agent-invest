"""Independent file-gate acceptance of role-separated Q0/E1/E2 recovery.

No database, production files or model calls. Complete pinned fixtures reach the
real deployment gate; counterexamples are re-reviewed except when testing a pin.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
import unittest

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyExecutionRefused, VerifiedQualifiedExecution, decode_local_execution_upgrade,
    decode_local_execution_upgrade_v2, encode_local_execution_upgrade_v2,
)
from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import verify_legacy_scope
from tests._f5_upgrade_duty_fixture import DutyWorld, StageGuard, member_payload
from tests._f5_upgrade_q0_fixture import sha256_bytes, synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import exact_json
from tests._nul_recovery_v2_fixture import build_recovery_v2


class RecoveryV2FileGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = synthetic_parent_q0(self)
        self.bundle = build_recovery_v2(self.parent)

    def test_release_only_and_writer_moved_relations_verify_and_keep_all_three_roles(self) -> None:
        for moved in (False, True):
            with self.subTest(writer_moved=moved):
                bundle = self.bundle if not moved else build_recovery_v2(self.parent, writer_moved=True)
                world = DutyWorld(bundle.root / ("duties-moved" if moved else "duties-equal"))
                duty = world.duty(tag="origin", process_profile=bundle.origin_profile, worker_profile=bundle.origin_worker)
                bundle = bundle.with_members([member_payload(duty.prepared())])
                execution = bundle.verify().execution
                self.assertIsInstance(execution, VerifiedQualifiedExecution)
                assert execution is not None
                summary = execution.summary()
                origin = bundle.proposal["recovery_origin"]
                self.assertEqual(summary["anchor_runtime_identity_sha256"], self.parent.runtime_identity)
                self.assertEqual(summary["origin_runtime_identity_sha256"], origin["runtime_identity_sha256"])
                self.assertEqual(summary["current_runtime_identity_sha256"], bundle.current_runtime)
                self.assertEqual(execution.member_runtime_identity_sha256, origin["runtime_identity_sha256"])
                self.assertNotEqual(origin["runtime_identity_sha256"], self.parent.runtime_identity)
                self.assertEqual(origin["runtime_identity_sha256"] != bundle.current_runtime, moved)
                decoded = decode_local_execution_upgrade_v2(bundle.proposal_path.read_bytes())
                self.assertEqual(encode_local_execution_upgrade_v2(decoded), bundle.proposal_path.read_bytes())
                authority, intent, _ = duty.reconciling()
                proof = execution.authorize_submission(authority, duty.spec, intent,
                                                       active_worker_profile=bundle.worker_profile)
                self.assertEqual(proof.parent_runtime_identity_sha256, origin["runtime_identity_sha256"])
                self.assertEqual(proof.parent_process_profile_sha256, bundle.origin_profile.sha256)
                self.assertNotEqual(proof.parent_runtime_identity_sha256, self.parent.runtime_identity)
                resolver = world.resolver(bundle.worker_profile, legacy_execution=execution)
                snapshot, intent, source = duty.command_parts()
                command = resolver.submission_command(authority, snapshot=snapshot, intent=intent,
                                                       snapshot_source=source, stage_guard=StageGuard())
                self.assertIsNotNone(command.legacy_authorization)
                self.assertEqual(execution.require_submission(command, command.legacy_authorization),
                                 bundle.current_runtime)
                self.assertEqual(command.legacy_authorization.parent_runtime_identity_sha256,
                                 origin["runtime_identity_sha256"])
                self.assertEqual(command.request_exact_bytes, duty.spec.request_exact_bytes)

    def test_same_worker_member_still_requires_exact_inventory_identity_before_each_stage(self) -> None:
        bundle = self.bundle
        world = DutyWorld(bundle.root / "same-worker-member")
        duty = world.duty(tag="listed", process_profile=bundle.origin_profile, worker_profile=bundle.origin_worker)
        bundle = bundle.with_members([member_payload(duty.prepared())])
        execution = bundle.verify().execution
        assert execution is not None
        resolver = world.resolver(bundle.worker_profile, legacy_execution=execution)
        resolver.assert_execution_profile(duty.prepared())
        twin = DutyWorld(bundle.root / "same-worker-twin").duty(
            tag="listed", timeout_seconds=21599, process_profile=bundle.origin_profile,
            worker_profile=bundle.origin_worker)
        self.assertEqual(twin.attempt_id, duty.attempt_id)
        with self.assertRaises(LegacyExecutionRefused):
            resolver.assert_execution_profile(twin.prepared())
        authority, intent, _ = twin.reconciling()
        snapshot, _, source = twin.command_parts()
        with self.assertRaises(LegacyExecutionRefused):
            resolver.submission_command(authority, snapshot=snapshot, intent=intent,
                                         snapshot_source=source, stage_guard=StageGuard())

    def test_inventory_cannot_bind_to_anchor_or_another_origin(self) -> None:
        world = DutyWorld(self.bundle.root / "foreign-duties")
        anchor = world.duty(tag="anchor", process_profile=self.parent.process_profile,
                            worker_profile=self.parent.worker_profile)
        with self.assertRaises(gate.MinerUDeploymentGateError):
            self.bundle.with_members([member_payload(anchor.prepared())]).verify()
        origin = world.duty(tag="origin", process_profile=self.bundle.origin_profile,
                            worker_profile=self.bundle.origin_worker)
        bundle = self.bundle.with_members([member_payload(origin.prepared())])
        execution = bundle.verify().execution
        assert execution is not None
        for field, value in (("client_submit_key", "different-key"),
                             ("request_sha256", sha256_bytes(b"different request"))):
            changed = copy.deepcopy(bundle.inventory["members"])
            changed[0][field] = value
            other = bundle.with_members(changed).verify().execution
            assert other is not None
            with self.subTest(field=field), self.assertRaises(LegacyExecutionRefused):
                other.require_member_progress(origin.prepared())
        stranger = world.duty(tag="omitted", process_profile=self.bundle.origin_profile,
                              worker_profile=self.bundle.origin_worker)
        with self.assertRaises(LegacyExecutionRefused):
            execution.require_member_progress(stranger.prepared())

    def test_repinned_origin_compute_model_capacity_and_target_release_drift_are_refused(self) -> None:
        bundle = self.bundle
        origin = bundle.proposal["recovery_origin"]
        for label, mutate in (
            ("writer", lambda value: value.update(writer_code_sha256=sha256_bytes(b"forged writer"))),
            ("capacity", lambda value: value.update(capacity_config_sha256=sha256_bytes(b"other capacity"))),
            ("worker", lambda value: value.update(worker_profile_sha256=sha256_bytes(b"other worker"))),
            ("same release", lambda value: value.update(release_manifest_sha256=bundle.proposal["current_execution"]["release_manifest_sha256"])),
        ):
            with self.subTest(label=label), self.assertRaises(gate.MinerUDeploymentGateError):
                bundle.with_proposal(lambda proposal: mutate(proposal["recovery_origin"])).verify()
        for section in ("client",):
            wrapper = json.loads(Path(origin["runtime_bundle_file"]).read_bytes())
            # A new unexpected computation fact is a drift, even consistently re-pinned and reviewed.
            wrapper["manifest"][section]["independent_compute_drift"] = "changed-model-or-computation"
            runtime = sha256_bytes(exact_json(wrapper["manifest"]))
            wrapper["identity_sha256"] = runtime
            path = bundle._file("drifted-origin-runtime", exact_json(wrapper))
            with self.subTest(section=section), self.assertRaises(gate.MinerUDeploymentGateError):
                bundle.with_proposal(lambda proposal: proposal["recovery_origin"].update(
                    runtime_bundle_file=str(path), runtime_bundle_sha256=sha256_bytes(path.read_bytes()),
                    runtime_identity_sha256=runtime)).verify()
        release = copy.deepcopy(bundle.release)
        release["files"][0]["sha256"] = sha256_bytes(b"target drift")
        with self.assertRaises(gate.MinerUDeploymentGateError):
            bundle.with_release(lambda value: value.update(release)).verify()

    def test_archived_pin_tamper_expired_q0_wrong_review_and_version_confusion_are_refused(self) -> None:
        bundle = self.bundle
        origin = bundle.proposal["recovery_origin"]
        for field in ("release_manifest_file", "runtime_bundle_file", "process_profile_file", "stream_activation_file"):
            path = Path(origin[field])
            exact = path.read_bytes()
            try:
                path.write_bytes(exact + b"\n")
                with self.subTest(pin=field), self.assertRaises(gate.MinerUDeploymentGateError):
                    bundle.verify()
            finally:
                path.write_bytes(exact)
        with self.assertRaises(gate.MinerUDeploymentGateError):
            bundle.verify(now=self.parent.clock + timedelta(days=365))
        with self.assertRaises(gate.MinerUDeploymentGateError):
            bundle.with_review(lambda review: review.update(proposal_sha256=sha256_bytes(b"different proposal"))).verify()
        with self.assertRaises(ValueError):
            decode_local_execution_upgrade(bundle.proposal_path.read_bytes())
        confused = dict(bundle.proposal, contract_version="worker-local-execution-upgrade.v1")
        with self.assertRaises(ValueError):
            decode_local_execution_upgrade_v2(exact_json(confused))


class SameRuntimeScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = build_recovery_v2(synthetic_parent_q0(self))
        self.world = DutyWorld(self.bundle.root / "same-runtime")
        self.member = self.world.duty(tag="listed", process_profile=self.bundle.origin_profile,
                                     worker_profile=self.bundle.origin_worker)
        self.outside = self.world.duty(tag="omitted", process_profile=self.bundle.origin_profile,
                                      worker_profile=self.bundle.origin_worker)
        self.captured = datetime.fromisoformat(self.bundle.inventory["captured_at_utc"])
        self.repository = mock.Mock()
        self.repository.count_staged_prepared_heads.return_value = 0

    def verify(self, bundle, unresolved, times):
        execution = bundle.verify().execution
        assert execution is not None
        return verify_legacy_scope(self.repository, execution,
                                   observed_at=self.captured + timedelta(seconds=10), unresolved=unresolved,
                                   created_at_by_attempt=times)

    def test_empty_inventory_cannot_hide_old_prepared_or_submitted_heads_in_same_runtime(self) -> None:
        for authority in (self.outside.prepared(), self.outside.submitted()):
            for offset in (-1, 0):
                with self.subTest(state=authority.state, seconds=offset), self.assertRaisesRegex(
                    LegacyExecutionRefused, "incomplete|before.*capture|not an inventory member",
                ):
                    self.verify(self.bundle, (authority,),
                                {authority.attempt_id: self.captured + timedelta(seconds=offset)})

    def test_complete_open_scope_still_holds_new_work_and_rejects_omitted_old_work_first(self) -> None:
        member, outside = self.member.prepared(), self.outside.prepared()
        bundle = self.bundle.with_members([member_payload(member)])
        observed = self.verify(bundle, (member,), {})
        self.assertEqual(observed.unresolved_members, (member.attempt_id,))
        with self.assertRaisesRegex(LegacyExecutionRefused, "premature"):
            self.verify(bundle, (member, outside), {outside.attempt_id: self.captured + timedelta(seconds=1)})
        with self.assertRaisesRegex(LegacyExecutionRefused, "incomplete|before.*capture|not an inventory member"):
            self.verify(bundle, (member, outside), {outside.attempt_id: self.captured})

    def test_genuine_empty_capture_accepts_only_post_capture_current_work(self) -> None:
        observed = self.verify(self.bundle, (), {})
        self.assertEqual(observed.current_execution_heads, ())
        authority = self.outside.prepared()
        observed = self.verify(self.bundle, (authority,),
                               {authority.attempt_id: self.captured + timedelta(seconds=1)})
        self.assertEqual(observed.current_execution_heads, (authority.attempt_id,))

    def test_unknown_naive_or_future_creation_time_fails_closed(self) -> None:
        authority = self.outside.prepared()
        for value in (None, self.captured.replace(tzinfo=None), self.captured + timedelta(seconds=11)):
            with self.subTest(value=value), self.assertRaises(LegacyExecutionRefused):
                self.verify(self.bundle, (authority,), {} if value is None else {authority.attempt_id: value})


if __name__ == "__main__":
    unittest.main()
