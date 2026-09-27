"""Pure contract checks for a newly qualified result runtime over prepared E7 work.

Synthetic identities only: this does not certify the deployment checker, origin
API lookup, source reopening, POST guard, or installed Qnew qualification.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
import unittest

from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    CompatibilityBasis, CurrentExecution, KeyLookupEvidenceReference,
    LegacyExecutionRefused, LegacyKeyLookup, LegacyKeyLookupEvidence,
    LegacyScopeInventory, LegacyScopeMember, LegacyScopeReference,
    LocalExecutionUpgradeReview, ParentQualification, QualifiedRuntimeUpgrade,
    RecoveryOrigin, VerifiedQualifiedExecution, decode_execution_upgrade,
    decode_legacy_key_lookup_evidence, decode_legacy_scope_inventory,
    decode_qualified_runtime_upgrade,
    encode_legacy_key_lookup_evidence, encode_legacy_scope_inventory,
    encode_qualified_runtime_upgrade, require_key_lookup_coverage,
    require_result_capacity_change, require_result_runtime_change,
)


def _sha(label: str | bytes) -> str:
    raw = label.encode() if isinstance(label, str) else label
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _qualification(label: str, *, runtime: str, writer: str, process: str, worker: str, activation: str) -> ParentQualification:
    return ParentQualification(
        runtime_identity_sha256=runtime, writer_code_sha256=writer,
        smoke_receipt_sha256=_sha(label + "-smoke"),
        canary_cache_sha256=_sha(label + "-canary"),
        validation_receipt_sha256=_sha(label + "-validation"),
        process_profile_file="/synthetic/" + label + "/process.json",
        process_profile_sha256=process, worker_profile_sha256=worker,
        stream_activation_file="/synthetic/" + label + "/activation.json",
        stream_activation_sha256=activation,
        qualified_at_utc="2026-09-27T00:00:20+00:00",
        service_epoch_sha256=_sha(label + "-epoch"),
    )


def _fixture() -> tuple[QualifiedRuntimeUpgrade, LegacyScopeInventory, LegacyKeyLookupEvidence]:
    origin = RecoveryOrigin(
        release_manifest_file="/synthetic/E7/release.json",
        release_manifest_sha256=_sha("E7-release"), source_revision="E7",
        writer_code_sha256=_sha("E7-writer"),
        runtime_bundle_file="/synthetic/E7/runtime.json",
        runtime_bundle_sha256=_sha("E7-runtime-file"),
        runtime_identity_sha256=_sha("R7"),
        process_profile_file="/synthetic/E7/process.json",
        process_profile_sha256=_sha("P7"), worker_profile_sha256=_sha("WP7"),
        capacity_config_sha256=_sha("C7"),
        stream_activation_file="/synthetic/E7/activation.json",
        stream_activation_sha256=_sha("A7"),
    )
    current = CurrentExecution(
        release_manifest_file="/synthetic/E8/release.json",
        release_manifest_sha256=_sha("E8-release"), source_revision="E8",
        writer_code_sha256=_sha("E8-writer"),
        runtime_bundle_file="/synthetic/E8/runtime.json",
        runtime_bundle_sha256=_sha("E8-runtime-file"),
        runtime_identity_sha256=_sha("R8"),
        process_profile_sha256=_sha("P8"), worker_profile_sha256=_sha("WP8"),
        capacity_config_sha256=_sha("C8"),
        stream_activation_sha256=_sha("A8"),
    )
    qnew = _qualification(
        "Qnew", runtime=current.runtime_identity_sha256,
        writer=current.writer_code_sha256, process=current.process_profile_sha256,
        worker=current.worker_profile_sha256,
        activation=current.stream_activation_sha256,
    )
    member = LegacyScopeMember(
        attempt_id="attempt-1", document_id="doc-1", processing_run_id="run-1",
        attempt_generation=0, fence_identity="fence-1",
        h0_checkpoint_sha256=_sha("H0"), execution_spec_sha256=_sha("spec"),
        source_pdf_sha256=_sha("source"), parser_target_sha256=_sha("target"),
        request_sha256=_sha("request"), runtime_epoch_sha256=origin.runtime_identity_sha256,
        client_submit_key="old-key-1", submission_epoch_unix=1790467200,
        process_profile_sha256=origin.process_profile_sha256,
        worker_profile_sha256=origin.worker_profile_sha256,
        observed_state="prepared", observed_lifecycle_version=0,
        observed_checkpoint_sha256=_sha("H0"), accepted_submission_sha256=None,
    )
    inventory = LegacyScopeInventory(
        captured_at_utc="2026-09-27T00:00:10+00:00", members=(member,),
    )
    lookup = LegacyKeyLookup(
        attempt_id=member.attempt_id, client_submit_key=member.client_submit_key,
        lookup_request_sha256=_sha("old-key-request"), http_status=404,
        response_sha256=_sha("old-key-404"), response_byte_count=0,
        observed_at_utc="2026-09-27T00:00:11+00:00",
    )
    evidence = LegacyKeyLookupEvidence(
        api_runtime_identity_sha256=origin.runtime_identity_sha256,
        key_ttl_seconds=120, lookups=(lookup,),
    )
    upgrade = QualifiedRuntimeUpgrade(
        target_qualification=qnew, recovery_origin=origin, current=current,
        runtime_changes=(("client", "writer_code_sha256"),
                         ("orchestrator", "capacity_config_sha256")),
        basis=CompatibilityBasis(_sha("changes"), _sha("tests"), _sha("review")),
        legacy_scope=LegacyScopeReference(
            "/synthetic/E7/inventory.json", _sha(encode_legacy_scope_inventory(inventory)), 1,
        ),
        key_lookups=KeyLookupEvidenceReference(
            "/synthetic/E7/key-lookups.json", _sha(encode_legacy_key_lookup_evidence(evidence)), 120,
        ),
    )
    return upgrade, inventory, evidence


def _verified(upgrade: QualifiedRuntimeUpgrade, inventory: LegacyScopeInventory) -> VerifiedQualifiedExecution:
    proposal_sha = _sha(encode_qualified_runtime_upgrade(upgrade))
    return VerifiedQualifiedExecution(
        upgrade=upgrade, upgrade_sha256=proposal_sha,
        review=LocalExecutionUpgradeReview(proposal_sha, "synthetic-review", "synthetic-decision"),
        review_sha256=_sha("synthetic-review-bytes"), inventory=inventory,
        parent_qualified_at=datetime.fromisoformat(upgrade.target_qualification.qualified_at_utc),
    )


def _authority(member: LegacyScopeMember) -> SimpleNamespace:
    spec = SimpleNamespace(
        sha256=member.execution_spec_sha256,
        worker_profile=SimpleNamespace(sha256=member.worker_profile_sha256),
        process_profile_sha256=member.process_profile_sha256,
        prepared_submission=SimpleNamespace(
            runtime_bundle_identity_sha256=member.runtime_epoch_sha256,
            submission_epoch_unix=member.submission_epoch_unix,
        ),
        parser_options=SimpleNamespace(runtime_bundle_identity_sha256=member.runtime_epoch_sha256),
    )
    return SimpleNamespace(
        execution_spec=spec, attempt_id=member.attempt_id,
        document_id=member.document_id, processing_run_id=member.processing_run_id,
        attempt_generation=member.attempt_generation, fence_identity=member.fence_identity,
        source_pdf_sha256=member.source_pdf_sha256,
        parser_target_sha256=member.parser_target_sha256,
        request_sha256=member.request_sha256,
        runtime_epoch_sha256=member.runtime_epoch_sha256,
        client_submit_key=member.client_submit_key,
        lifecycle_version=0,
        checkpoint_history=(SimpleNamespace(
            sha256=member.observed_checkpoint_sha256, lifecycle_version=0,
            previous_checkpoint_sha256=None,
        ),),
    )


class NewlyQualifiedRuntimeUpgradeIndependentTest(unittest.TestCase):
    def test_new_qualification_is_exact_target_while_prepared_member_keeps_e7_origin(self) -> None:
        upgrade, inventory, evidence = _fixture()
        encoded = encode_qualified_runtime_upgrade(upgrade)
        self.assertEqual(decode_qualified_runtime_upgrade(encoded), upgrade)
        self.assertEqual(decode_execution_upgrade(encoded), upgrade)
        self.assertEqual(decode_legacy_key_lookup_evidence(encode_legacy_key_lookup_evidence(evidence)), evidence)
        self.assertEqual(decode_legacy_scope_inventory(encode_legacy_scope_inventory(inventory)), inventory)
        require_key_lookup_coverage(
            evidence, inventory, origin_runtime_identity_sha256=upgrade.recovery_origin.runtime_identity_sha256,
            key_ttl_seconds=upgrade.key_lookups.key_ttl_seconds,
        )
        execution = _verified(upgrade, inventory)
        q0 = _qualification(
            "Q0", runtime=_sha("R0"), writer=_sha("W0"), process=_sha("P0"),
            worker=_sha("WP0"), activation=_sha("A0"),
        )
        self.assertEqual(execution.qualification_origin, "exact")
        self.assertEqual(execution.qualification_anchor, upgrade.target_qualification)
        self.assertNotEqual(q0.runtime_identity_sha256, execution.current_runtime_identity_sha256)
        self.assertEqual(execution.member_runtime_identity_sha256, upgrade.recovery_origin.runtime_identity_sha256)
        self.assertNotEqual(execution.member_runtime_identity_sha256, execution.current_runtime_identity_sha256)
        self.assertEqual(execution.member_process_profile_sha256, inventory.members[0].process_profile_sha256)
        self.assertEqual(execution.member_worker_profile_sha256, inventory.members[0].worker_profile_sha256)
        self.assertEqual(execution.summary()["qualification_origin"], "exact")
        with self.assertRaises(ValueError):
            replace(upgrade, target_qualification=q0)
        with self.assertRaises(ValueError):
            replace(upgrade, recovery_origin=replace(
                upgrade.recovery_origin, runtime_identity_sha256=upgrade.current.runtime_identity_sha256,
            ))
        with self.assertRaises(ValueError):
            replace(upgrade, current=replace(
                upgrade.current, release_manifest_sha256=upgrade.recovery_origin.release_manifest_sha256,
            ))

    def test_only_prepared_original_keys_absent_before_real_ttl_are_covered(self) -> None:
        upgrade, inventory, evidence = _fixture()
        member = inventory.members[0]
        for bad in (
            replace(evidence, lookups=()),
            replace(evidence, api_runtime_identity_sha256=_sha("other-api")),
            replace(evidence, key_ttl_seconds=600),
            replace(evidence, lookups=(replace(evidence.lookups[0], client_submit_key="new-key"),)),
            replace(evidence, lookups=(replace(evidence.lookups[0], http_status=200),)),
            replace(evidence, lookups=(replace(evidence.lookups[0], http_status=500),)),
            replace(evidence, lookups=(replace(evidence.lookups[0], observed_at_utc="2026-09-27T00:00:09+00:00"),)),
            replace(evidence, lookups=(replace(evidence.lookups[0], observed_at_utc="2026-09-27T00:02:00+00:00"),)),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    require_key_lookup_coverage(
                        bad, inventory,
                        origin_runtime_identity_sha256=upgrade.recovery_origin.runtime_identity_sha256,
                        key_ttl_seconds=upgrade.key_lookups.key_ttl_seconds,
                    )
        for bad_member in (
            replace(member, observed_state="accepted"),
            replace(member, observed_state="submission_unknown"),
            replace(member, accepted_submission_sha256=_sha("accepted")),
            replace(member, runtime_epoch_sha256=upgrade.current.runtime_identity_sha256),
        ):
            with self.subTest(bad_member=bad_member):
                with self.assertRaises(ValueError):
                    _verified(upgrade, replace(inventory, members=(bad_member,)))
        encoded = json.loads(encode_legacy_key_lookup_evidence(evidence))
        encoded["unreviewed"] = True
        with self.assertRaises(ValueError):
            decode_legacy_key_lookup_evidence(json.dumps(encoded).encode())
        inventory_payload = json.loads(encode_legacy_scope_inventory(inventory))
        inventory_payload["members"][0]["unreviewed"] = True
        with self.assertRaises(ValueError):
            decode_legacy_scope_inventory(json.dumps(inventory_payload).encode())
        inventory_payload = json.loads(encode_legacy_scope_inventory(inventory))
        inventory_payload["member_count"] = 0
        with self.assertRaises(ValueError):
            decode_legacy_scope_inventory(json.dumps(inventory_payload).encode())
        with self.assertRaises(ValueError):
            replace(upgrade, key_lookups=replace(upgrade.key_lookups, key_ttl_seconds=0))

    def test_allowed_storage_axes_do_not_admit_model_source_request_or_compute_drift(self) -> None:
        changes = (("client", "writer_code_sha256"), ("orchestrator", "capacity_config_sha256"))
        origin = {
            "client": {"writer_code_sha256": _sha("old-writer"), "model_sha256": _sha("model"),
                       "source_pdf_sha256": _sha("source"), "request_sha256": _sha("request")},
            "orchestrator": {"capacity_config_sha256": _sha("old-capacity"),
                             "hybrid_batch_ratio_requested": 1},
            "topology": {"inference_server_sha256": _sha("inference")},
        }
        target = {
            "client": {**origin["client"], "writer_code_sha256": _sha("new-writer")},
            "orchestrator": {**origin["orchestrator"], "capacity_config_sha256": _sha("new-capacity")},
            "topology": dict(origin["topology"]),
        }
        require_result_runtime_change(origin, target, changes=changes)
        for section, field, value in (
            ("client", "model_sha256", _sha("different-model")),
            ("client", "source_pdf_sha256", _sha("different-source")),
            ("client", "request_sha256", _sha("different-request")),
            ("orchestrator", "hybrid_batch_ratio_requested", 2),
            ("topology", "inference_server_sha256", _sha("different-inference")),
        ):
            with self.subTest(field=field):
                drift = {key: dict(part) for key, part in target.items()}
                drift[section][field] = value
                with self.assertRaises(ValueError):
                    require_result_runtime_change(origin, drift, changes=changes)
        with self.assertRaises(ValueError):
            require_result_runtime_change(origin, target, changes=changes[:1])
        upgrade, _, _ = _fixture()
        with self.assertRaises(ValueError):
            replace(upgrade, runtime_changes=(("client", "model_sha256"),))
        compute = (
            "parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit",
            "final_http_limit_per_loop", "api_process_limit", "api_event_loop_limit",
            "processing_window_size", "omp_num_threads", "mkl_num_threads", "openblas_num_threads",
            "pdf_render_processes_requested", "hybrid_batch_ratio_requested", "pipeline_inference_locks",
        )
        origin_capacity = {"contract_version": "mineru.capacity-config.v1",
                           **{name: 1 for name in compute}, "result_reservation_bytes": 256}
        target_capacity = {**origin_capacity, "contract_version": "mineru.capacity-config.v2",
                           "result_reservation_bytes": 4096}
        require_result_capacity_change(origin_capacity, target_capacity)
        for field in ("parse_active_limit", "hybrid_batch_ratio_requested", "pipeline_inference_locks"):
            with self.subTest(compute=field):
                drift = {**target_capacity, field: 2}
                with self.assertRaises(ValueError):
                    require_result_capacity_change(origin_capacity, drift)

    def test_one_verified_object_binds_e7_h0_source_request_and_profile_roles(self) -> None:
        upgrade, inventory, _ = _fixture()
        execution = _verified(upgrade, inventory)
        member = inventory.members[0]
        authority = _authority(member)
        self.assertEqual(execution.require_member_progress(authority), member)
        for name, value in (
            ("source_pdf_sha256", _sha("other-source")),
            ("request_sha256", _sha("other-request")),
            ("fence_identity", "other-fence"),
            ("client_submit_key", "new-key"),
            ("runtime_epoch_sha256", upgrade.current.runtime_identity_sha256),
        ):
            with self.subTest(field=name):
                with self.assertRaises(LegacyExecutionRefused):
                    execution.require_member_progress(SimpleNamespace(**{**vars(authority), name: value}))
        with self.assertRaises(LegacyExecutionRefused):
            execution.require_member_progress(SimpleNamespace(**{**vars(authority), "attempt_id": "unlisted"}))
        wrong_spec = SimpleNamespace(**{**vars(authority.execution_spec),
                                        "process_profile_sha256": upgrade.current.process_profile_sha256})
        with self.assertRaises(LegacyExecutionRefused):
            execution.require_member_progress(SimpleNamespace(**{**vars(authority), "execution_spec": wrong_spec}))


if __name__ == "__main__":
    unittest.main()
