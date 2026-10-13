"""The newly qualified result runtime upgrade: contract, relations and scope.

Synthetic identities and in-memory records only. The full deployment-gate
composition needs a qualified result-storage deployment and is not exercised
here; see the implementation report for that owed check.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import legacy_member_from_authority
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    VerifiedMinerUDeployment,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    TERMINAL_RECEIPT_V5_CONTRACT,
    TerminalResultStorageV1,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    build_stage_resource_grant_v1,
    decode_materialization_intent_v4,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    QUALIFIED_UPGRADE_CONTRACT,
    CompatibilityBasis,
    CurrentExecution,
    KeyLookupEvidenceReference,
    LegacyExecutionRefused,
    LegacyKeyLookup,
    LegacyKeyLookupEvidence,
    LegacyScopeInventory,
    LegacyScopeMember,
    LegacyScopeReference,
    LocalExecutionUpgradeReview,
    ParentQualification,
    QualifiedRuntimeUpgrade,
    RecoveryOrigin,
    VerifiedQualifiedExecution,
    decode_execution_upgrade,
    decode_legacy_key_lookup_evidence,
    encode_legacy_key_lookup_evidence,
    encode_qualified_runtime_upgrade,
    require_key_lookup_coverage,
    require_result_capacity_change,
    require_result_process_profile_change,
    require_result_runtime_change,
)
from tests._f5_upgrade_duty_fixture import DutyWorld, StageGuard
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import current_worker_profile
from tests._qualified_upgrade_flow_fixture_independent import build_synthetic_qnew_flow
from tests.unit.test_mineru_materialize_grant_v5 import STORAGE_POLICY


def sha(char: str) -> str:
    return "sha256:" + char * 64


ORIGIN_RUNTIME, TARGET_RUNTIME = sha("1"), sha("2")
ORIGIN_PROFILE, TARGET_PROFILE = sha("3"), sha("4")
ORIGIN_WORKER, TARGET_WORKER = sha("5"), sha("6")
CAPTURED = "2026-09-27T08:00:00+00:00"
EPOCH = int(datetime(2026, 9, 27, 7, 0, tzinfo=UTC).timestamp())


def key(index: int) -> str:
    return f"{EPOCH:x}." + str(index) * 64


def member(index: int, **changes: Any) -> LegacyScopeMember:
    """One prepared obligation frozen under the origin; H0/spec/fence/key unique per index."""
    values: dict[str, Any] = dict(
        attempt_id=f"attempt-{index}", document_id=f"doc-{index}", processing_run_id=f"run-{index}",
        attempt_generation=0, fence_identity=f"fence-{index}", h0_checkpoint_sha256=sha(str(index)),
        execution_spec_sha256=sha(str(index + 6)), source_pdf_sha256=sha("c"), parser_target_sha256=sha("d"),
        request_sha256=sha("e"), runtime_epoch_sha256=ORIGIN_RUNTIME, client_submit_key=key(index),
        submission_epoch_unix=EPOCH, process_profile_sha256=ORIGIN_PROFILE,
        worker_profile_sha256=ORIGIN_WORKER, observed_state="prepared", observed_lifecycle_version=0,
        observed_checkpoint_sha256=sha("f"), accepted_submission_sha256=None,
    )
    values.update(changes)
    return LegacyScopeMember(**values)


def inventory(*members: LegacyScopeMember) -> LegacyScopeInventory:
    return LegacyScopeInventory(captured_at_utc=CAPTURED, members=members or (member(0), member(1)))


def qualified_upgrade(**changes: Any) -> QualifiedRuntimeUpgrade:
    values: dict[str, Any] = dict(
        target_qualification=ParentQualification(
            runtime_identity_sha256=TARGET_RUNTIME, writer_code_sha256=sha("7"), smoke_receipt_sha256=sha("8"),
            canary_cache_sha256=sha("9"), validation_receipt_sha256=sha("a"),
            process_profile_file="/q/process-profile.json", process_profile_sha256=TARGET_PROFILE,
            worker_profile_sha256=TARGET_WORKER, stream_activation_file="/q/activation.json",
            stream_activation_sha256=sha("b"), qualified_at_utc="2026-09-28T00:00:00+00:00",
            service_epoch_sha256=sha("c"),
        ),
        recovery_origin=RecoveryOrigin(
            release_manifest_file="/o/release.json", release_manifest_sha256=sha("d"), source_revision="e7",
            writer_code_sha256=sha("e"), runtime_bundle_file="/o/runtime.json", runtime_bundle_sha256=sha("f"),
            runtime_identity_sha256=ORIGIN_RUNTIME, process_profile_file="/o/process-profile.json",
            process_profile_sha256=ORIGIN_PROFILE, worker_profile_sha256=ORIGIN_WORKER,
            capacity_config_sha256=sha("0"), stream_activation_file="/o/activation.json",
            stream_activation_sha256=sha("1"),
        ),
        current=CurrentExecution(
            release_manifest_file="/t/release.json", release_manifest_sha256=sha("2"), source_revision="e8",
            writer_code_sha256=sha("7"), runtime_bundle_file="/t/runtime.json", runtime_bundle_sha256=sha("3"),
            runtime_identity_sha256=TARGET_RUNTIME, process_profile_sha256=TARGET_PROFILE,
            worker_profile_sha256=TARGET_WORKER, capacity_config_sha256=sha("4"),
            stream_activation_sha256=sha("b"),
        ),
        runtime_changes=(("orchestrator", "capacity_config"), ("orchestrator", "container_image_digest")),
        basis=CompatibilityBasis(
            exact_change_manifest_sha256=sha("5"), independent_test_evidence_sha256=sha("6"),
            independent_code_review_sha256=sha("7"),
        ),
        legacy_scope=LegacyScopeReference(inventory_file="/o/inventory.json", inventory_sha256=sha("8"),
                                          member_count=2),
        key_lookups=KeyLookupEvidenceReference(evidence_file="/o/lookups.json", evidence_sha256=sha("9"),
                                               key_ttl_seconds=86_400),
    )
    values.update(changes)
    return QualifiedRuntimeUpgrade(**values)


def verified(upgrade: QualifiedRuntimeUpgrade | None = None,
             scope: LegacyScopeInventory | None = None) -> VerifiedQualifiedExecution:
    upgrade = upgrade or qualified_upgrade()
    encoded = encode_qualified_runtime_upgrade(upgrade)
    from disclosure_anchor.application.contracts.closed_document import sha256_of

    return VerifiedQualifiedExecution(
        upgrade=upgrade, upgrade_sha256=sha256_of(encoded),
        review=LocalExecutionUpgradeReview(
            proposal_sha256=sha256_of(encoded), reviewer_reference="reviewer", decision_reference="decision",
        ),
        review_sha256=sha("a"), inventory=scope or inventory(),
        parent_qualified_at=datetime(2026, 9, 28, tzinfo=UTC),
    )


class QualifiedUpgradeContractTests(unittest.TestCase):
    def test_round_trip_and_dispatch_keep_other_versions_unchanged(self) -> None:
        upgrade = qualified_upgrade()
        encoded = encode_qualified_runtime_upgrade(upgrade)
        self.assertEqual(decode_execution_upgrade(encoded), upgrade)
        payload = json.loads(encoded)
        self.assertEqual(
            (payload["contract_version"], payload["transition_kind"]),
            (QUALIFIED_UPGRADE_CONTRACT, "newly_qualified_result_runtime"),
        )
        for mutate in (
            lambda p: p.update(extra=1),
            lambda p: p["key_lookups"].update(extra=1),
            lambda p: p.update(transition_kind="local_operational_compatible"),
        ):
            changed = json.loads(encoded)
            mutate(changed)
            raw = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
            with self.subTest(changed=sorted(changed)), self.assertRaises(ValueError):
                decode_execution_upgrade(raw)

    def test_invariants_bind_qnew_to_the_target_and_changes_to_the_closed_axes(self) -> None:
        base = qualified_upgrade()
        for label, changes in (
            ("qnew qualifies another runtime", {"target_qualification": replace(
                base.target_qualification, runtime_identity_sha256=ORIGIN_RUNTIME)}),
            ("origin is the target", {"recovery_origin": replace(
                base.recovery_origin, runtime_identity_sha256=TARGET_RUNTIME)}),
            ("model axis", {"runtime_changes": (("inference_server", "model_snapshot_revision"),)}),
            ("unsorted", {"runtime_changes": tuple(reversed(base.runtime_changes))}),
            ("empty", {"runtime_changes": ()}),
            ("no members", {"legacy_scope": replace(base.legacy_scope, member_count=0)}),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                qualified_upgrade(**changes)


def manifest() -> dict[str, Any]:
    return {
        "contract_version": "mineru-runtime-bundle.v11",
        "client": {"writer_code_sha256": sha("e"), "package_set_sha256": sha("1"), "mineru_version": "3.4.4"},
        "orchestrator": {
            "container_image_digest": sha("2"), "processing_window_size": 16, "hybrid_batch_ratio": 1,
            "task_result_reservation_bytes": 268435456, "max_unacked_result_bytes": 2147483648,
            "capacity_config": {"contract_version": "mineru.capacity-config.v1", "parse_active_limit": 12},
            "capacity_config_sha256": sha("3"),
        },
        "inference_server": {"model_snapshot_revision": "a" * 40, "served_model_id": "model"},
        "topology": {"windows_compose_sha256": sha("4"), "api_endpoint_sha256": sha("5")},
    }


class ResultRelationTests(unittest.TestCase):
    def test_runtime_may_move_only_the_declared_result_storage_axes(self) -> None:
        origin, target = manifest(), manifest()
        target["orchestrator"].update(
            container_image_digest=sha("9"), task_result_reservation_bytes=None, max_unacked_result_bytes=None,
        )
        declared = (
            ("orchestrator", "container_image_digest"), ("orchestrator", "max_unacked_result_bytes"),
            ("orchestrator", "task_result_reservation_bytes"),
        )
        require_result_runtime_change(origin, target, changes=declared)
        with self.assertRaises(ValueError):  # Undeclared movement.
            require_result_runtime_change(origin, target, changes=declared[:1])
        for section, name, value in (
            ("inference_server", "model_snapshot_revision", "b" * 40),
            ("orchestrator", "processing_window_size", 8),
            ("orchestrator", "hybrid_batch_ratio", 2),
            ("topology", "api_endpoint_sha256", sha("6")),
            ("client", "mineru_version", "3.4.5"),
        ):
            moved = manifest()
            moved[section][name] = value
            with self.subTest(field=f"{section}.{name}"), self.assertRaises(ValueError):
                require_result_runtime_change(origin, moved, changes=((section, name),))

    def test_capacity_keeps_every_compute_limit(self) -> None:
        fields = {
            "parse_active_limit": 12, "total_nonterminal_limit": 14, "finalizer_active_limit": 1,
            "final_http_limit_per_loop": 14, "api_process_limit": 1, "api_event_loop_limit": 1,
            "processing_window_size": 16, "omp_num_threads": 4, "mkl_num_threads": 4, "openblas_num_threads": 4,
            "pdf_render_processes_requested": 4, "hybrid_batch_ratio_requested": 1, "pipeline_inference_locks": True,
        }
        origin = {"contract_version": "mineru.capacity-config.v1", **fields, "result_reservation_bytes": 1}
        target = {"contract_version": "mineru.capacity-config.v2", **fields, "result_storage": {}}
        require_result_capacity_change(origin, target)
        with self.assertRaises(ValueError):
            require_result_capacity_change(origin, {**target, "parse_active_limit": 14})
        with self.assertRaises(ValueError):
            require_result_capacity_change(origin, {**target, "contract_version": "mineru.capacity-config.v1"})

    def test_process_profile_moves_only_result_storage_fields(self) -> None:
        from tests.unit.test_mineru_process_profile import _profile

        origin = _profile()
        target = replace(
            origin, contract_version="mineru.process-profile.v3", runtime_bundle_identity_sha256=TARGET_RUNTIME,
            result_reservation_bytes=None, max_unacked_result_bytes=None,
            result_storage_policy_sha256=STORAGE_POLICY.sha256,
            temporary_disk_bytes_limit=origin.temporary_disk_bytes_limit * 2,
        )
        origin = replace(origin, runtime_bundle_identity_sha256=ORIGIN_RUNTIME)
        require_result_process_profile_change(
            origin, target, origin_runtime_sha256=ORIGIN_RUNTIME, target_runtime_sha256=TARGET_RUNTIME,
        )
        with self.assertRaises(ValueError):
            require_result_process_profile_change(
                origin, replace(target, api_task_slots=origin.api_task_slots + 1),
                origin_runtime_sha256=ORIGIN_RUNTIME, target_runtime_sha256=TARGET_RUNTIME,
            )


class KeyLookupTests(unittest.TestCase):
    def evidence(self, **changes: Any) -> LegacyKeyLookupEvidence:
        lookups = tuple(
            LegacyKeyLookup(
                attempt_id=f"attempt-{index}", client_submit_key=key(index), lookup_request_sha256=sha("1"),
                http_status=404, response_sha256=sha("2"), response_byte_count=40,
                observed_at_utc="2026-09-27T08:08:24+00:00",
            )
            for index in (0, 1)
        )
        values: dict[str, Any] = dict(api_runtime_identity_sha256=ORIGIN_RUNTIME, key_ttl_seconds=86_400,
                                      lookups=lookups)
        values.update(changes)
        return LegacyKeyLookupEvidence(**values)

    def test_every_original_key_absent_after_capture_and_within_its_lifetime(self) -> None:
        evidence = self.evidence()
        self.assertEqual(decode_legacy_key_lookup_evidence(encode_legacy_key_lookup_evidence(evidence)), evidence)
        scope = inventory()
        require_key_lookup_coverage(evidence, scope, origin_runtime_identity_sha256=ORIGIN_RUNTIME,
                                    key_ttl_seconds=86_400)
        first = evidence.lookups[0]
        for label, changed, ttl in (
            ("another runtime answered", self.evidence(api_runtime_identity_sha256=TARGET_RUNTIME), 86_400),
            ("lifetime differs", evidence, 3_600),
            ("a key is known", self.evidence(lookups=(replace(first, http_status=200), evidence.lookups[1])),
             86_400),
            ("before capture", self.evidence(lookups=(
                replace(first, observed_at_utc="2026-09-27T07:59:59+00:00"), evidence.lookups[1])), 86_400),
            ("expired when looked up", self.evidence(key_ttl_seconds=600), 600),
            ("missing member", self.evidence(lookups=evidence.lookups[:1]), 86_400),
            ("other key", self.evidence(lookups=(replace(first, client_submit_key=key(9)),
                                                 evidence.lookups[1])), 86_400),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                require_key_lookup_coverage(changed, scope, origin_runtime_identity_sha256=ORIGIN_RUNTIME,
                                            key_ttl_seconds=ttl)


class VerifiedQualifiedExecutionTests(unittest.TestCase):
    def test_exact_origin_members_bound_to_the_origin_and_summary_names_roles(self) -> None:
        execution = verified()
        self.assertEqual(execution.qualification_origin, "exact")
        self.assertEqual(execution.upgrade_contract_version, QUALIFIED_UPGRADE_CONTRACT)
        self.assertEqual(
            (execution.member_runtime_identity_sha256, execution.current_runtime_identity_sha256),
            (ORIGIN_RUNTIME, TARGET_RUNTIME),
        )
        summary = execution.summary()
        self.assertEqual(summary["qualification_origin"], "exact")
        self.assertEqual(summary["origin_runtime_identity_sha256"], ORIGIN_RUNTIME)
        self.assertEqual(summary["runtime_changes"], ["orchestrator.capacity_config",
                                                      "orchestrator.container_image_digest"])
        self.assertNotIn("parent_runtime_identity_sha256", summary)

    def test_only_never_submitted_prepared_obligations_move_to_qnew(self) -> None:
        for label, changed in (
            ("reconciling (unknown POST)", member(1, observed_state="reconciling")),
            ("accepted", member(1, observed_state="submitted", accepted_submission_sha256=sha("9"))),
            ("frozen elsewhere", member(1, runtime_epoch_sha256=TARGET_RUNTIME)),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                verified(scope=inventory(member(0), changed))

    def test_deployment_origin_follows_its_execution(self) -> None:
        base = dict(
            api_url="http://127.0.0.1:1", observability_url="http://127.0.0.1:2/v1",
            inference_upstream_url="http://i/v1", runtime_identity_sha256=TARGET_RUNTIME, served_model_id="m",
            canary_passed_at_utc=datetime(2026, 9, 28, tzinfo=UTC), canary_max_age_seconds=3600,
            task_retention_seconds=600, task_cleanup_interval_seconds=30, task_slots=1,
        )
        execution = verified()
        VerifiedMinerUDeployment(**base, qualification_origin="exact", execution=execution)
        with self.assertRaises(MinerUDeploymentGateError):
            VerifiedMinerUDeployment(**base, qualification_origin="compatible_parent", execution=execution)
        VerifiedMinerUDeployment(**base)
        with self.assertRaises(MinerUDeploymentGateError):
            VerifiedMinerUDeployment(**base, qualification_origin="compatible_parent")


class LegacyGrantBindingTests(unittest.TestCase):
    def grant_inputs(self) -> tuple[Any, Any, Any]:
        from tests.unit.test_remote_parse_evidence_v4 import _typed_happy_bundle

        _, reservation, values, *_ = _typed_happy_bundle()
        terminal = replace(
            values[4], contract_version=TERMINAL_RECEIPT_V5_CONTRACT,
            result_storage=TerminalResultStorageV1(policy_sha256=STORAGE_POLICY.sha256, selected_bytes=30,
                                                   member_count=3, inventory_sha256=sha("e")),
        )
        spec = SimpleNamespace(sha256=sha("7"))
        return SimpleNamespace(reservation=reservation, spec=spec), terminal, values

    def resolver(self, execution: Any) -> Any:
        resolver = object.__new__(ProductionV4StageInputResolver)
        resolver._storage_policy = STORAGE_POLICY
        resolver._legacy_execution = execution
        return resolver

    def test_pre_storage_reservation_grows_only_as_a_verified_legacy_member(self) -> None:
        # The bundle's synthetic credit policy is not v3: a pre-storage H0.
        bound, terminal, _ = self.grant_inputs()
        grant_for = ProductionV4StageInputResolver._stage_grant
        with self.assertRaises(ValueError):
            grant_for(self.resolver(None), bound, terminal)
        execution = mock.Mock(spec=VerifiedQualifiedExecution)
        execution.upgrade = qualified_upgrade()
        execution.upgrade_sha256 = sha("c")
        execution.is_member.return_value = True
        execution.member.return_value = SimpleNamespace(execution_spec_sha256=sha("7"))
        grant = grant_for(self.resolver(execution), bound, terminal)
        self.assertEqual((grant.execution_upgrade_sha256, grant.execution_spec_sha256), (sha("c"), sha("7")))
        self.assertIn(b"execution_upgrade_sha256", grant.canonical_bytes)
        execution.member.return_value = SimpleNamespace(execution_spec_sha256=sha("8"))
        with self.assertRaises(ValueError):  # Not the obligation's original spec.
            grant_for(self.resolver(execution), bound, terminal)

    def test_storage_era_grants_stay_unbound_with_unchanged_bytes(self) -> None:
        from disclosure_anchor.adapters.parsers.mineru_medium import v4_stage_input_resolver

        bound, terminal, _ = self.grant_inputs()
        # Treat the bundle's credit policy as the storage-era v3 policy.
        storage_era = SimpleNamespace(sha256=bound.reservation.credit_policy_sha256)
        with mock.patch.object(v4_stage_input_resolver, "STAGED_RESOURCE_CREDIT_POLICY_V3", storage_era):
            grant = ProductionV4StageInputResolver._stage_grant(self.resolver(None), bound, terminal)
        self.assertIsNone(grant.execution_upgrade_sha256)
        self.assertNotIn(b"execution_upgrade_sha256", grant.canonical_bytes)
        with self.assertRaises(ValueError):  # Half a binding.
            build_stage_resource_grant_v1(
                reservation=bound.reservation, terminal_receipt_sha256=terminal.sha256,
                storage_policy_sha256=STORAGE_POLICY.sha256, inventory_sha256=sha("e"),
                artifact_byte_count=terminal.artifact_byte_count, selected_bytes=30, member_count=3,
                decode_working_set_bytes=40, decode_input_limit_bytes=30, execution_upgrade_sha256=sha("c"),
            )

    def test_bound_grant_round_trips_inside_a_v5_intent(self) -> None:
        from tests.unit.test_stage_resource_grant_independent import _v5_bundle

        _, values, _ = _v5_bundle()
        intent = values[5]
        bound_grant = replace(intent.resource_grant, execution_upgrade_sha256=sha("c"),
                              execution_spec_sha256=sha("7"))
        bound_intent = replace(intent, resource_grant=bound_grant)
        self.assertEqual(decode_materialization_intent_v4(bound_intent.canonical_bytes), bound_intent)
        self.assertNotIn(b"execution_spec_sha256", intent.canonical_bytes)


class KeyLookupCaptureTests(unittest.TestCase):
    def test_capture_records_closed_absence_and_refuses_known_or_open_members(self) -> None:
        from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
        from disclosure_anchor.application.contracts.worker_execution_upgrade import (
            encode_legacy_scope_inventory,
        )

        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            path = Path(directory) / "inventory.json"
            path.write_bytes(encode_legacy_scope_inventory(inventory()))
            path.chmod(0o600)
            settings = SimpleNamespace(
                disclosure_mineru_runtime_bundle_identity_sha256=ORIGIN_RUNTIME,
                disclosure_mineru_api_url="http://127.0.0.1:30002",
            )
            seen: list[str] = []

            def absent(request: httpx.Request) -> httpx.Response:
                seen.append(request.url.path)
                return httpx.Response(404, json={"detail": "Task not found"})

            evidence = upgrade_runtime.capture_legacy_key_lookups(
                settings, inventory=path, key_ttl_seconds=86_400,  # type: ignore[arg-type]
                transport=httpx.MockTransport(absent),
                wall_clock=lambda: datetime(2026, 9, 27, 8, 10, tzinfo=UTC),
            )
            self.assertEqual(seen, [f"/tasks/by-idempotency/{key(0)}", f"/tasks/by-idempotency/{key(1)}"])
            self.assertEqual([item.http_status for item in evidence.lookups], [404, 404])
            require_key_lookup_coverage(evidence, inventory(), origin_runtime_identity_sha256=ORIGIN_RUNTIME,
                                        key_ttl_seconds=86_400)
            with self.assertRaises(MinerUDeploymentGateError):
                upgrade_runtime.capture_legacy_key_lookups(
                    settings, inventory=path, key_ttl_seconds=86_400,  # type: ignore[arg-type]
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
                )
            path.unlink()
            path.write_bytes(encode_legacy_scope_inventory(inventory(member(0), member(1, observed_state="reconciling"))))
            path.chmod(0o600)
            with self.assertRaises(MinerUDeploymentGateError):
                upgrade_runtime.capture_legacy_key_lookups(
                    settings, inventory=path, key_ttl_seconds=86_400,  # type: ignore[arg-type]
                    transport=httpx.MockTransport(absent),
                )


class PostBoundaryKeyLifetimeTests(unittest.TestCase):
    """The approved key lifetime is judged on the clock of the POST boundary."""

    def test_qualified_proof_needs_the_post_clock_and_refuses_an_expired_key(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        epoch = int((now - timedelta(seconds=30)).timestamp())
        origin = synthetic_parent_q0(self)
        with tempfile.TemporaryDirectory() as scratch:
            world = DutyWorld(Path(scratch))
            duty = world.duty(
                tag="qnew_clock", process_profile=origin.process_profile,
                worker_profile=origin.worker_profile, submission_epoch_unix=epoch,
            )
            inventory = LegacyScopeInventory(
                captured_at_utc=(now - timedelta(seconds=5)).isoformat(),
                members=(legacy_member_from_authority(duty.prepared()),),
            )
            flow = build_synthetic_qnew_flow(
                self, inventory=inventory, origin=origin, now=now,
                old_key_transport=httpx.MockTransport(
                    lambda request: httpx.Response(404, json={"detail": "Task not found"}),
                ),
            )
            execution = flow.checker(now=now).verified_execution
            assert execution is not None
            resolver = world.resolver(
                current_worker_profile(origin, flow.process_profile), legacy_execution=execution,
            )
            authority, intent, _ = duty.reconciling()
            snapshot, _, source = duty.command_parts()
            command = resolver.submission_command(
                authority, snapshot=snapshot, intent=intent, snapshot_source=source, stage_guard=StageGuard(),
            )
            proof = command.legacy_authorization
            assert proof is not None
            ttl = execution.upgrade.key_lookups.key_ttl_seconds
            self.assertEqual(
                execution.require_submission(command, proof, now_unix=float(epoch + ttl - 1)),
                execution.current_runtime_identity_sha256,
            )
            for label, clock in (("no clock", None), ("non-finite clock", float("nan")),
                                 ("boolean clock", True)):
                with self.subTest(label), self.assertRaisesRegex(LegacyExecutionRefused, "wall clock"):
                    execution.require_submission(command, proof, now_unix=clock)  # type: ignore[arg-type]
            with self.assertRaisesRegex(LegacyExecutionRefused, "expired before its POST"):
                execution.require_submission(command, proof, now_unix=float(epoch + ttl))

if __name__ == "__main__":
    unittest.main()
