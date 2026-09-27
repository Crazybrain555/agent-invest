"""Independent synthetic Qnew/E7 checker-to-POST acceptance.

The offline qualification test uses no database. The managed-scratch flow is
kept in this integration module and is run only by the scratch runner.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest

import httpx

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import (
    capture_legacy_scope_inventory,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentGateError
from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import capture_legacy_key_lookups
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    SnapshotReceiptV4, SubmissionIntentV4,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyExecutionRefused, LegacyScopeInventory, encode_legacy_scope_inventory,
)
from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamPressureSample, StreamSubmissionDeferred,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy, StreamAdmissionControl,
)
from tests._f5_upgrade_u01_fixture import current_worker_profile, write_private
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._qualified_upgrade_flow_fixture_independent import build_synthetic_qnew_flow
from tests.integration import test_local_execution_upgrade_recovery_v4 as f5
from tests.unit.test_qualified_runtime_upgrade import member


class QualifiedUpgradeOfflineCheckerIndependent(unittest.TestCase):
    def test_distinct_qnew_verifies_real_exact_checker_and_old_key_absence(self) -> None:
        # The origin builder owns the temp tree; build_synthetic_qnew_flow
        # creates its own origin of the same deterministic shape.
        origin = synthetic_parent_q0(self)
        one = replace(member(0), runtime_epoch_sha256=origin.runtime_identity,
                      process_profile_sha256=origin.process_profile.sha256,
                      worker_profile_sha256=origin.worker_profile.sha256)
        two = replace(member(1), runtime_epoch_sha256=origin.runtime_identity,
                      process_profile_sha256=origin.process_profile.sha256,
                      worker_profile_sha256=origin.worker_profile.sha256)
        now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        inventory = LegacyScopeInventory(captured_at_utc=datetime(2026, 9, 27, 8, 0, tzinfo=UTC).isoformat(),
                                         members=(one, two))
        seen: list[str] = []

        def absent(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(404, json={"detail": "Task not found"})

        flow = build_synthetic_qnew_flow(
            self, inventory=inventory, old_key_transport=httpx.MockTransport(absent),
            now=now,
        )
        checker = flow.checker(now=now)
        self.assertEqual(checker.qualification_origin, "exact")
        self.assertIsNotNone(checker.verified_execution)
        self.assertEqual(len(seen), 2)


class _Pressure:
    """One in-memory joined sample port; the product policy keeps its real 10s rule."""

    def __init__(self, config, clock: list[float]) -> None:
        self.config, self.clock, self.sequence = config, clock, 0

    def latest(self) -> StreamPressureSample:
        self.sequence += 1
        c = self.config
        return StreamPressureSample(
            sequence=self.sequence, observed_monotonic=self.clock[0],
            runtime_identity_sha256=c.runtime_identity_sha256,
            owner_identity_sha256=c.owner_identity_sha256,
            evidence_sha256="sha256:" + "1"*64,
            gpu_free_bytes=c.gpu_recover_bytes*2,
            host_available_bytes=c.host_recover_bytes*2,
            http_active=0, http_pending=0, provider_nonterminal_tasks=0,
        )


class QualifiedUpgradeManagedScratchFlowIndependent(unittest.TestCase):
    """Real H0 and backend/resolver/POST; only the API and pressure samples are scripted."""

    def test_e7_prepared_qnew_checker_resolver_and_pre_post_guard(self) -> None:
        # Existing F5 fixture owns managed DB isolation, row cleanup and two
        # tiny synthetic source PDFs. Running this class outside its scratch
        # runner skips at base.setUp(); it never falls back to production.
        base = f5.LocalExecutionUpgradeRecoveryTests(
            "test_pre_f5_duties_recover_on_the_upgraded_worker_across_boots",
        )
        self.addCleanup(base.doCleanups)
        base.setUp()
        works = []
        for index in range(2):
            doc = base._document(f"qnew-prepared-{index}", "prepared", index)
            works.append(base.worker.admit(doc, security_id=base.security_id))
        inventory = capture_legacy_scope_inventory(base.engine)
        self.assertEqual(len(inventory.members), 2)
        self.assertEqual({m.observed_state for m in inventory.members}, {"prepared"})
        now = datetime.now(UTC)
        flow = build_synthetic_qnew_flow(
            self, inventory=inventory, origin=base.parent, base_settings=base.old_settings,
            old_key_transport=httpx.MockTransport(base.fleet), now=now,
        )
        checker = flow.checker(now=now)
        self.assertEqual(checker.qualification_origin, "exact")
        execution = checker.verified_execution
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.upgrade.legacy_scope.member_count, 2)
        # The same product scope recheck used by resident boot.
        observed = f5.worker_cli._recheck_execution_upgrade(base.engine, execution)
        self.assertEqual(len(observed.unresolved_members), 2)

        worker_profile = current_worker_profile(flow.origin, flow.process_profile)
        resolver = ProductionV4StageInputResolver(
            uow_factory=unit_of_work_factory(base.engine), paths=base.paths,
            provider_source=ProviderDocumentFileSource(
                base.paths, text_reader=observe_pdf_text_rectangles,
            ), worker_profile=worker_profile, legacy_execution=execution,
            storage_policy=flow.capacity.result_storage,
        )
        activation = load_mineru_stream_activation(
            flow.target_activation,
            expected_sha256=flow.settings.disclosure_mineru_stream_pressure_config_sha256,
            expected_owner_uid=__import__("os").getuid(), expected_capacity=flow.capacity,
            expected_runtime_identity_sha256=flow.target_runtime,
        )
        assert activation is not None
        clock = [100.0]
        control = StreamAdmissionControl(
            MineruStreamPolicy(activation.policy), _Pressure(activation.policy, clock),
            monotonic=lambda: clock[0],
        )
        self.assertFalse(control.current().new_post_allowed)

        selected = inventory.members[0]
        posts: list[bytes] = []
        lookups: list[str] = []

        def provider(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.startswith("/tasks/by-idempotency/"):
                lookups.append(request.url.path)
                return httpx.Response(404, json={"detail": "Task not found"})
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                posts.append(body)
                return httpx.Response(200, json={
                    "task_id": "task-independent-qnew", "status": "pending",
                    "status_url": "/tasks/task-independent-qnew",
                    "result_url": "/tasks/task-independent-qnew/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": selected.client_submit_key,
                    "attempt_identity": selected.attempt_id,
                    "fence_identity": selected.fence_identity,
                    "protocol_state": "pending", "error": None,
                    "storage": {
                        "schema": "mineru.task-storage-status.v1",
                        "policy_sha256": flow.capacity.result_storage.sha256,
                        "phase": "admitted", "wait_reason": None,
                        "wait_since_unix": None, "blocked": False,
                        "selected_bytes": 0, "member_count": 0,
                        "inventory_sha256": None, "zip_bytes": 0,
                    },
                })
            raise AssertionError(f"unexpected scripted API request: {request.method} {request.url.path}")

        remote = MinerUHttpRemoteV4(
            transport=httpx.MockTransport(provider), token_factory=lambda n: b"q"*n,
            request_timeout_seconds=30, legacy_execution=execution,
            submission_guard=control,
            result_storage_policy_sha256=flow.capacity.result_storage.sha256,
        )
        self.addCleanup(remote.close)
        backend = base.worker.backend
        old_inputs, old_remote = backend._inputs, backend._remote
        self.addCleanup(setattr, backend, "_inputs", old_inputs)
        self.addCleanup(setattr, backend, "_remote", old_remote)
        backend._inputs, backend._remote = resolver, remote
        work = next(w for w in works if w.attempt_id == selected.attempt_id)
        work = backend.prepare_remote_io(
            work, credit_allowance=base.worker.limits.credits,
            stage_guard=base.worker.guard(),
        )
        self.assertEqual(work.state, "reconciling")

        # Real evidence reopening under the verified object, with adjacent
        # wrong-spec and wrong-original-key negatives before any POST.
        guard = base.worker.guard()
        authority, bound = backend._authority(work, "reconciling", guard)
        snapshot = backend._evidence(authority, "snapshot_receipt", SnapshotReceiptV4)
        intent = backend._evidence(authority, "submission_intent", SubmissionIntentV4)
        source = backend._materialization.submission_snapshot_source_v4(
            checkpoint=authority.checkpoint, reservation=backend._reservation(authority),
            snapshot_receipt=snapshot, submission_intent=intent,
            evidence=authority.evidence,
            resourceful_checkpoint_history=authority.checkpoint_history,
            claim=authority.claim_witness, claim_guard=backend._claim_guard,
        )
        with self.assertRaises(ValueError):
            bound.submission_command(
                authority, snapshot=snapshot,
                intent=replace(intent, request_sha256="sha256:" + "0"*64),
                snapshot_source=source, stage_guard=guard,
            )
        other = next(m for m in inventory.members if m.attempt_id != selected.attempt_id)
        with self.assertRaises(ValueError):
            bound.submission_command(
                authority, snapshot=snapshot,
                intent=replace(intent, client_submit_key=other.client_submit_key),
                snapshot_source=source, stage_guard=guard,
            )
        self.assertEqual(posts, [])
        command = bound.submission_command(
            authority, snapshot=snapshot, intent=intent,
            snapshot_source=source, stage_guard=guard,
        )
        with self.assertRaises(StreamSubmissionDeferred):
            remote.reconcile_or_submit(command)
        self.assertEqual(posts, [], "the actual pre-POST policy refused the cold start")
        clock[0] += 5
        self.assertFalse(control.current().new_post_allowed)
        clock[0] += 5
        self.assertTrue(control.current().new_post_allowed)

        # A reconciling/unknown old obligation cannot be captured for a fresh
        # Qnew approval, even if its original key still returns 404.
        unknown = capture_legacy_scope_inventory(base.engine)
        unknown_path = write_private(
            flow.origin.root / "inventory-unknown.json",
            encode_legacy_scope_inventory(unknown),
        )
        with self.assertRaises(MinerUDeploymentGateError):
            capture_legacy_key_lookups(
                flow.origin.settings, inventory=unknown_path, key_ttl_seconds=86_400,
                transport=httpx.MockTransport(base.fleet), wall_clock=lambda: now,
            )
        self.assertEqual(posts, [])

        submitted = backend.run_remote(
            work, credit_allowance=base.worker.limits.credits,
            stage_guard=base.worker.guard(),
        )
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(lookups), 2)
        self.assertIn(selected.client_submit_key.encode(), posts[0])

        accepted = capture_legacy_scope_inventory(base.engine)
        accepted_path = write_private(
            flow.origin.root / "inventory-accepted.json",
            encode_legacy_scope_inventory(accepted),
        )
        with self.assertRaises(MinerUDeploymentGateError):
            capture_legacy_key_lookups(
                flow.origin.settings, inventory=accepted_path, key_ttl_seconds=86_400,
                transport=httpx.MockTransport(base.fleet), wall_clock=lambda: now,
            )

        # The checker inventory is closed: a late third old H0 cannot borrow
        # this approval or reach another POST.
        extra = base._document("qnew-unapproved", "prepared", 3)
        base.worker.admit(extra, security_id=base.security_id)
        with self.assertRaises(LegacyExecutionRefused):
            f5.worker_cli._recheck_execution_upgrade(base.engine, execution)
        self.assertEqual(len(posts), 1)

        # A known original key also refuses the old-API absence capture.
        with self.assertRaises(MinerUDeploymentGateError):
            capture_legacy_key_lookups(
                flow.origin.settings, inventory=flow.inventory_file,
                key_ttl_seconds=86_400,
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json={"status": "pending"}),
                ),
                wall_clock=lambda: now,
            )


if __name__ == "__main__":
    unittest.main()
