"""Root-only managed-scratch v2 recovery, reusing the existing worker/fleet fixture.

These cases create no new database or runner. engine_or_skip and the base fixture's
managed-marker guard refuse production. All provider and semantic ports are local fakes.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres import staged_upgrade_scope_v4 as scope
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import ATOMIC_PUBLICATION_PREPARATION_FILENAME
from disclosure_anchor.application.contracts.worker_execution_upgrade import LegacyExecutionRefused, LegacyObligationsOpen
from tests._nul_recovery_v2_fixture import build_recovery_v2
from tests._f5_upgrade_duty_fixture import member_payload
from tests.integration import test_local_execution_upgrade_recovery_v4 as old
from tests.integration._f5_upgrade_legacy_fixture import PreF5Worker
from tests.unit._publication_text_fixture import as_v23


class RecoveryV2ManagedScratchTests(old.LocalExecutionUpgradeRecoveryTests):
    # Reuse setup and worker utilities without repeating the entire strict v1 scenarios.
    test_pre_f5_duties_recover_on_the_upgraded_worker_across_boots = None
    test_an_extra_old_profile_head_after_capture_is_a_public_stop_before_any_effect = None

    def setUp(self) -> None:
        super().setUp()
        self.bundle = build_recovery_v2(self.parent)
        origin = self.bundle.proposal["recovery_origin"]
        origin_parent = replace(self.parent, process_profile=self.bundle.origin_profile,
                                process_profile_path=Path(origin["process_profile_file"]),
                                worker_profile=self.bundle.origin_worker,
                                runtime_identity=origin["runtime_identity_sha256"])
        settings = old._settings(self.old_settings,
                                 disclosure_mineru_runtime_bundle_identity_sha256=origin["runtime_identity_sha256"])
        self.origin_worker = PreF5Worker(engine=self.engine, settings=settings, fleet=self.fleet,
                                         adapter=self.model, parent=origin_parent)
        self.addCleanup(self.origin_worker.close)
        self.worker = self.origin_worker

    def _capture_bundle(self):
        inventory = scope.capture_legacy_scope_inventory(self.engine)
        return self.bundle.with_inventory_payload(inventory.to_payload())

    def _built(self, bundle) -> SimpleNamespace:
        settings = old._settings(bundle.settings, database_url=self.database_url,
                                 disclosure_v4_secret_keyring_file=self.old_settings.disclosure_v4_secret_keyring_file)
        return SimpleNamespace(settings=settings, current_environment=bundle.environment,
                               current_profile=bundle.process_profile, proposal_path=bundle.proposal_path,
                               release_path=bundle.release_path,
                               inventory=scope.capture_legacy_scope_inventory(self.engine),
                               derived=SimpleNamespace(stream_activation_file=bundle.activation_path,
                                                       current_runtime_identity_sha256=bundle.current_runtime))

    def _publication_counts(self, document_id):
        with self.engine.connect() as conn:
            return tuple(conn.execute(sa.text(query), {"d": document_id}).scalar_one() for query in (
                "SELECT count(*) FROM disclosure_core.document_unit WHERE document_id=:d",
                "SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 WHERE document_id=:d",
                "SELECT count(*) FROM disclosure_ops.outbox_event WHERE document_id=:d AND event_kind='document_published'",
            ))

    def test_origin_duties_close_before_new_h0_with_bad_v23_seal_and_committed_ack_tails(self) -> None:
        with as_v23():
            seeded = self._seed(self._plan())
        bad = self._document("old-nul-seal", "local_materialized", 70)
        self.fleet.register(bad.source_sha256, bad.source, old.end_to_end._official_result_zip(
            bad.source_sha256, first_text=("第\x00节 重要事项", True)), complete_on_accept=True)
        work = self.worker.drive(self.worker.admit(bad, security_id=self.security_id), "local_materialized", fleet=self.fleet)
        with as_v23() as simulation, self.assertRaises(sa.exc.DataError):
            self.worker.backend.commit(work, credit_allowance=self.worker.limits.credits, stage_guard=self.worker.guard())
        self.assertGreater(simulation.builds, 0)
        data_root = self.paths.data_path(Path())
        sealed_authority = self._authority(work.attempt_id)
        retained = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in data_root.rglob("*")
                    if path.is_file() and sealed_authority.processing_run_id in path.parts}
        self.assertTrue(any(path.name == ATOMIC_PUBLICATION_PREPARATION_FILENAME for path in retained))
        tails = seeded["publish_committed"] + seeded["ack_pending"]
        tail_before = {doc.document_id: self._publication_counts(doc.document_id) for doc, _ in tails}
        members = tuple(attempt for pairs in seeded.values() for _, attempt in pairs) + (work.attempt_id,)
        for task in tuple(self.fleet.tasks.values()):
            self.fleet.complete(task.client_submit_key)
        self._expire_claims()
        bundle = self._capture_bundle()
        execution = bundle.verify().execution
        assert execution is not None
        self.assertEqual(set(execution.member_attempt_ids), set(members))
        self.assertNotEqual(execution.member_runtime_identity_sha256, self.parent.runtime_identity)
        self.assertEqual(execution.member_runtime_identity_sha256, bundle.current_runtime)
        hold = scope.PostgresLegacyObligationsGate(engine=self.engine, execution=execution)
        with self.assertRaises(LegacyObligationsOpen):
            hold.require_closed()
        waiting = self._document("waiting-new", "new", 95)
        built = self._built(bundle)
        for ttl in (None, 1):
            report = self._preflight(built, ttl=ttl,
                                     now=datetime.now(UTC) + timedelta(seconds=10))
            self.assertFalse(report["ready_to_install"], report)
            self.assertEqual(report["prepared_key_status"], "unverified" if ttl is None else "expired", report)
        admissions = []
        real_ingress = DurableStagedIngressV4.execute

        def assert_full_closure_before_h0(ingress, *args, **kwargs):
            self.assertTrue(all(state in old.FINAL_STATES and not current
                                for state, current in self._states(members).values()))
            admissions.append("new-H0")
            return real_ingress(ingress, *args, **kwargs)

        with patch.object(DurableStagedIngressV4, "execute", assert_full_closure_before_h0):
            boot = self._u01_boot(built, label="same-R-origin-closure",
                                  done=lambda: self._document_acked(waiting.document_id))
        self.assertEqual(admissions, ["new-H0"])
        hold.require_closed()
        self.assertTrue(hold.closed)
        states = self._states(members)
        self.assertEqual(states[work.attempt_id], ("local_failed", False))
        self.assertTrue(all(state == "acked" and not current for attempt, (state, current) in states.items()
                            if attempt != work.attempt_id))
        failed = self._authority(work.attempt_id)
        failures = [item.value for item in failed.evidence if item.kind == "failure_receipt"]
        self.assertEqual([value.error_code for value in failures], ["publication_text_unrepresentable"])
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}, retained)
        for doc, _ in tails:
            self.assertEqual(self._publication_counts(doc.document_id), tail_before[doc.document_id])
        receipt = json.loads(boot["receipt"].read_bytes())
        self.assertEqual(receipt["contract_version"], "worker-execution-boot-receipt.v2")
        self.assertEqual(receipt["origin_runtime_identity_sha256"], bundle.current_runtime)
        self.assertEqual(receipt["anchor_runtime_identity_sha256"], self.parent.runtime_identity)
        self.assertEqual(self.fleet.duplicate_posts, [])

    def test_same_r_inventory_omissions_closed_scope_restart_and_capture_lock(self) -> None:
        included = self._document("included", "prepared", 80)
        omitted = self._document("omitted", "submitted", 81)
        first = self.worker.admit(included, security_id=self.security_id)
        second = self.worker.drive(self.worker.admit(omitted, security_id=self.security_id), "submitted", fleet=self.fleet)
        bundle = self._capture_bundle()
        # Exactly the DB's same-snapshot timestamps; no host-clock fixture manufacture.
        with scope.read_only_repository(self.engine) as (repository, observed_at, times):
            self.assertLessEqual(times[second.attempt_id], datetime.fromisoformat(bundle.inventory["captured_at_utc"]))
        for members in ([], [member_payload(self._authority(first.attempt_id))]):
            execution = bundle.with_members(members).verify().execution
            assert execution is not None
            with self.assertRaisesRegex(LegacyExecutionRefused, "inventory is incomplete"):
                scope.require_legacy_scope(self.engine, execution)
        # Close only the listed member. The old omitted submitted duty must still be refused.
        first = self.worker.drive(first, "ack_pending", fleet=self.fleet)
        self.worker.backend.acknowledge(first, stage_guard=self.worker.guard())
        partial = bundle.with_members([next(item for item in bundle.inventory["members"]
                                            if item["attempt_id"] == first.attempt_id)])
        execution = partial.verify().execution
        assert execution is not None
        with self.assertRaisesRegex(LegacyExecutionRefused, "inventory is incomplete"):
            scope.require_legacy_scope(self.engine, execution)
        # Capture checks the actual worker singleton only in this scratch database.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as locked:
            locked.execute(sa.text("SELECT pg_advisory_lock(:ns, 0)"), {"ns": old.worker_cli.WORKER_NS})
            try:
                with self.assertRaises(LegacyExecutionRefused):
                    scope.capture_legacy_scope_inventory(self.engine)
            finally:
                locked.execute(sa.text("SELECT pg_advisory_unlock(:ns, 0)"), {"ns": old.worker_cli.WORKER_NS})
        self.assertIsNotNone(scope.capture_legacy_scope_inventory(self.engine))
        self.fleet.complete(self.fleet.task_for_attempt(second.attempt_id).client_submit_key)
        self._expire_claims()
        built = self._built(bundle)
        self._u01_boot(built, label="close-second", done=lambda: self._document_acked(omitted.document_id))
        # A genuinely empty capture is valid, and a later current head passes both scopes.
        empty_bundle = self._capture_bundle()
        self.assertEqual(empty_bundle.inventory["members"], [])
        # After full closure, a current head created after the original capture is legitimate.
        current = self._document("post-closure", "prepared", 82)
        new = self.worker.admit(current, security_id=self.security_id)
        execution = bundle.verify().execution
        assert execution is not None
        observed = scope.require_legacy_scope(self.engine, execution)
        self.assertEqual(observed.current_execution_heads, (new.attempt_id,))
        self.assertEqual(set(observed.closed_members), {first.attempt_id, second.attempt_id})
        empty_execution = empty_bundle.verify().execution
        assert empty_execution is not None
        empty_observed = scope.require_legacy_scope(self.engine, empty_execution)
        self.assertEqual(empty_observed.current_execution_heads, (new.attempt_id,))


if __name__ == "__main__":
    unittest.main()
