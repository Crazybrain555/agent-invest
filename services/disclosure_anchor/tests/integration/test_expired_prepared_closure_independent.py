"""Managed-scratch acceptance of one expired, never-submitted V4 H0 closure.

Run only through tests/integration/_runner.py. The inherited F5 fixture pins a
disposable database and cleans its rows. The old API is scripted; no native
service, parser or model is used. This test never runs against production.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import unittest
from unittest import mock

import httpx
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import capture_legacy_scope_inventory
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import capture_legacy_key_lookups
from disclosure_anchor.application.contracts.expired_prepared_closure import (
    ORIGINAL_KEY_EXPIRED_ERROR_CODE,
    decode_expired_prepared_closure_plan,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    encode_legacy_key_lookup_evidence,
    encode_legacy_scope_inventory,
)
from disclosure_anchor.application.use_cases.parse_requeue import ParseRequeue, ParseRequeueCommand
from disclosure_anchor.application.worker import queries
from disclosure_anchor.cli import expired_prepared_closure as closure_cli
from disclosure_anchor.domain.errors import ParseRequeueError
from tests._f5_upgrade_u01_fixture import write_private
# Import the module, not its TestCase class: discovery must not run its tests again.
from tests.integration import test_local_execution_upgrade_recovery_v4 as f5


class ExpiredPreparedClosureManagedScratchIndependent(unittest.TestCase):
    def test_exact_h0_closes_without_snapshot_post_ack_or_automatic_requeue(self) -> None:
        base = f5.LocalExecutionUpgradeRecoveryTests(
            "test_pre_f5_duties_recover_on_the_upgraded_worker_across_boots",
        )
        self.addCleanup(base.doCleanups)
        base.setUp()  # engine_or_skip verifies the managed scratch marker and all DB URLs.
        document = base._document("expired-closure-h0", "prepared", 0)
        work = base.worker.admit(document, security_id=base.security_id)
        original = base._authority(work.attempt_id)
        self.assertEqual(original.state, "prepared")
        self.assertEqual(original.lifecycle_version, 0)
        self.assertEqual([item.kind for item in original.evidence], ["preparation_intent"])
        self.assertIsNone(original.checkpoint.snapshot_receipt_sha256)
        original_key = original.client_submit_key
        h0_sha = original.checkpoint_sha256

        inventory = capture_legacy_scope_inventory(base.engine)
        self.assertEqual(len(inventory.members), 1)
        inventory_file = write_private(
            base.root / "expired-inventory.json", encode_legacy_scope_inventory(inventory),
        )
        ttl = 86_400
        capture_time = datetime.now(UTC)
        lookups = capture_legacy_key_lookups(
            base.old_settings, inventory=inventory_file, key_ttl_seconds=ttl,
            transport=httpx.MockTransport(base.fleet), wall_clock=lambda: capture_time,
        )
        self.assertEqual([item.http_status for item in lookups.lookups], [404])
        self.assertLess(capture_time.timestamp() - inventory.members[0].submission_epoch_unix, ttl)
        lookup_file = write_private(
            base.root / "expired-lookups.json", encode_legacy_key_lookup_evidence(lookups),
        )
        future = datetime.fromtimestamp(inventory.members[0].submission_epoch_unix + ttl + 1, UTC)
        calls_before = base.fleet.http_call_count()
        plan_file = base.root / "expired-plan.json"
        receipt_file = base.root / "expired-receipt.json"
        with (
            mock.patch.object(closure_cli, "require_runtime_app_connection",
                              base._require_scratch_connection),
            closure_cli._worker_singleton(base.old_settings) as singleton,
        ):
            closure = closure_cli.compose(
                uow_factory=unit_of_work_factory(base.engine),
                scratch_root=base.runtime_root / "staged_v4" / "scratch",
                published_root=base.paths.data_path(Path()),
                process_guard=lambda: f5.worker_cli._assert_staged_singleton(singleton),
                utc_now=lambda: future,  # Inject the use-case clock; no 24h sleep.
            )
            preview = closure_cli.run_preview(Namespace(
                inventory=inventory_file, key_lookups=lookup_file,
                origin_runtime_identity_sha256=base.parent.runtime_identity,
                key_ttl_seconds=ttl, out=plan_file,
            ), closure)
            self.assertEqual(preview["closable"], [work.attempt_id])
            self.assertEqual(preview["still_valid"], [])
            plan_bytes = plan_file.read_bytes()
            self.assertEqual(decode_expired_prepared_closure_plan(plan_bytes).sha256,
                             preview["plan_sha256"])
            self.assertEqual("sha256:" + hashlib.sha256(plan_bytes).hexdigest(),
                             preview["plan_sha256"])

            execute_args = Namespace(
                plan=plan_file, expect_sha256=preview["plan_sha256"],
                inventory=inventory_file, key_lookups=lookup_file,
                decided_by="independent-scratch", reason="actual old key lifetime elapsed",
                out=receipt_file,
            )
            # The prior worker's claim is still live. The managed entry must
            # not steal it or append any cleanup/failure evidence.
            with self.assertRaises(Exception):  # noqa: BLE001 - exact claim exception is an adapter detail
                closure_cli.run_execute(execute_args, closure)
            still_prepared = base._authority(work.attempt_id)
            self.assertEqual((still_prepared.state, still_prepared.checkpoint_sha256),
                             ("prepared", h0_sha))
            self.assertFalse(receipt_file.exists())

            base._expire_claims()  # Existing F5 fixture changes only the disposable old-owner lease.
            first = closure_cli.run_execute(execute_args, closure)
            first_receipt = receipt_file.read_bytes()
            self.assertEqual(first["members"][0]["final_state"], "pre_submission_failed")
            self.assertEqual(first["continued_from"][work.attempt_id], "prepared")
            final = base._authority(work.attempt_id)
            self.assertEqual(final.state, "pre_submission_failed")
            self.assertEqual(final.checkpoint_history[0].sha256, h0_sha)
            self.assertEqual(final.client_submit_key, original_key)
            self.assertIsNone(final.checkpoint.accepted_submission_sha256)
            self.assertIsNone(final.checkpoint.ack_receipt_sha256)
            failure = next(item.value for item in final.evidence if item.kind == "failure_receipt")
            self.assertEqual(failure.error_code, ORIGINAL_KEY_EXPIRED_ERROR_CODE)
            self.assertEqual(failure.source_checkpoint_sha256, h0_sha)
            self.assertFalse(failure.submission_was_attempted)
            self.assertEqual(failure.outcome, "pre_submission_failure")
            self.assertFalse(any(item.kind == "submission_intent" for item in final.evidence))
            self.assertEqual(base.fleet.http_call_count(), calls_before,
                             "closure itself must never GET, POST or ACK")
            with unit_of_work_factory(base.engine)() as uow:
                run = uow.processing_runs.get(final.processing_run_id)
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run.status, "failed")
                self.assertEqual(run.error["error_code"], ORIGINAL_KEY_EXPIRED_ERROR_CODE)
                self.assertIsNone(uow.processing_runs.parse_requeue_decision_for_run(run.processing_run_id))

            # A lost CLI response can repeat the exact plan/decision. No new
            # checkpoint, outbox or terminal cleanup is allowed on replay.
            rows_after = base._durable_rows()
            second = closure_cli.run_execute(execute_args, closure)
            self.assertEqual(second["members"], first["members"])
            self.assertEqual(receipt_file.read_bytes(), first_receipt)
            self.assertEqual(base._durable_rows(), rows_after)
            self.assertEqual(base.fleet.http_call_count(), calls_before)

        # Closure made the failure durable; it did not authorize another parse.
        queue = PostgresV4OrdinaryParseCandidateSource(
            engine=base.engine, max_retries=3, scope_classes=None,
            admission_document_ids=(document.document_id,),
        )
        self.assertEqual(queue.list_candidates(after_document_id=None, limit=1).candidates, ())
        with base.engine.connect() as conn:
            before = queries.parse_admission_diagnosis(
                conn, document_id=document.document_id, max_retries=3,
            )
        self.assertFalse(before["currently_eligible"])
        self.assertEqual(before["remaining_blockers"]["latest_failed_run_id"],
                         final.processing_run_id)
        self.assertFalse(before["remaining_blockers"]["latest_failed_run_released"])

        # The existing append-only requeue API is a separate named decision.
        # Its row is deleted only by fixture cleanup in this disposable DB,
        # before the inherited F5 cleanup removes the failed run/document.
        def remove_test_decision() -> None:
            with base.engine.begin() as conn:
                conn.execute(sa.text(
                    "DELETE FROM disclosure_ops.parse_requeue_decision "
                    "WHERE processing_run_id=:run_id"
                ), {"run_id": final.processing_run_id})

        self.addCleanup(remove_test_decision)
        requeue = ParseRequeue(uow_factory=unit_of_work_factory(base.engine))
        command = ParseRequeueCommand(
            document_id=document.document_id,
            processing_run_id=final.processing_run_id,
            fixed_by="synthetic new-runtime parse authority reviewed",
            reason="the original key is expired; re-admission requires a distinct decision",
            decided_by="independent-scratch-operator",
        )
        rows_before_dry_run = base._durable_rows()
        dry = requeue.execute(command, dry_run=True)
        self.assertTrue(dry.dry_run)
        self.assertIsNone(dry.decision_id)
        self.assertEqual(dry.failure_error_code, ORIGINAL_KEY_EXPIRED_ERROR_CODE)
        self.assertEqual(dry.failure_retry_budget_class, "original_key_lifetime")
        with unit_of_work_factory(base.engine)() as uow:
            self.assertIsNone(uow.processing_runs.parse_requeue_decision_for_run(
                final.processing_run_id,
            ))
        self.assertEqual(base._durable_rows(), rows_before_dry_run)
        self.assertEqual(queue.list_candidates(after_document_id=None, limit=1).candidates, ())
        self.assertEqual(base.fleet.http_call_count(), calls_before)

        released = requeue.execute(command)
        self.assertFalse(released.dry_run)
        self.assertIsNotNone(released.decision_id)
        self.assertEqual(released.failure_retry_budget_class, "original_key_lifetime")
        with unit_of_work_factory(base.engine)() as uow:
            decision = uow.processing_runs.parse_requeue_decision_for_run(
                final.processing_run_id,
            )
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(decision.decision_id, released.decision_id)
            run_after = uow.processing_runs.get(final.processing_run_id)
            self.assertIsNotNone(run_after)
            assert run_after is not None
            self.assertEqual((run_after.status, run_after.error), (run.status, run.error))
        eligible = queue.list_candidates(after_document_id=None, limit=1).candidates
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].document_id, document.document_id)
        with base.engine.connect() as conn:
            after = queries.parse_admission_diagnosis(
                conn, document_id=document.document_id, max_retries=3,
            )
        self.assertTrue(after["currently_eligible"])
        self.assertTrue(after["remaining_blockers"]["latest_failed_run_released"])
        self.assertEqual(after["remaining_blockers"]["latest_failed_run_id"],
                         final.processing_run_id)
        with self.assertRaises(ParseRequeueError) as duplicate:
            requeue.execute(command)
        self.assertEqual(duplicate.exception.error["error_code"], "DECISION_ALREADY_EXISTS")
        self.assertEqual((base._authority(work.attempt_id).checkpoint_history[0].sha256,
                          base._authority(work.attempt_id).client_submit_key),
                         (h0_sha, original_key))
        self.assertEqual(base.fleet.http_call_count(), calls_before)

    def test_cleanup_pending_interruption_reopens_same_decision_without_remote_effect(self) -> None:
        base = f5.LocalExecutionUpgradeRecoveryTests(
            "test_pre_f5_duties_recover_on_the_upgraded_worker_across_boots",
        )
        self.addCleanup(base.doCleanups)
        base.setUp()  # Managed scratch marker and URL checks run before any database write.
        document = base._document("expired-closure-interrupted", "prepared", 0)
        work = base.worker.admit(document, security_id=base.security_id)
        h0 = base._authority(work.attempt_id)
        self.assertEqual((h0.state, h0.lifecycle_version), ("prepared", 0))
        self.assertEqual([item.kind for item in h0.evidence], ["preparation_intent"])
        self.assertIsNone(h0.checkpoint.snapshot_receipt_sha256)
        original_key, h0_sha = h0.client_submit_key, h0.checkpoint_sha256

        inventory = capture_legacy_scope_inventory(base.engine)
        inventory_file = write_private(
            base.root / "interrupted-inventory.json", encode_legacy_scope_inventory(inventory),
        )
        ttl = 86_400
        capture_time = datetime.now(UTC)
        lookups = capture_legacy_key_lookups(
            base.old_settings, inventory=inventory_file, key_ttl_seconds=ttl,
            transport=httpx.MockTransport(base.fleet), wall_clock=lambda: capture_time,
        )
        self.assertEqual([item.http_status for item in lookups.lookups], [404])
        self.assertLess(capture_time.timestamp() - inventory.members[0].submission_epoch_unix, ttl)
        lookup_file = write_private(
            base.root / "interrupted-lookups.json", encode_legacy_key_lookup_evidence(lookups),
        )
        future = datetime.fromtimestamp(inventory.members[0].submission_epoch_unix + ttl + 1, UTC)
        plan_file = base.root / "interrupted-plan.json"
        receipt_file = base.root / "interrupted-receipt.json"
        calls_before = base.fleet.http_call_count()
        base._expire_claims()  # The old owner is gone; only its disposable lease changes.

        def new_closure(singleton):
            return closure_cli.compose(
                uow_factory=unit_of_work_factory(base.engine),
                scratch_root=base.runtime_root / "staged_v4" / "scratch",
                published_root=base.paths.data_path(Path()),
                process_guard=lambda: f5.worker_cli._assert_staged_singleton(singleton),
                utc_now=lambda: future,
            )

        with mock.patch.object(closure_cli, "require_runtime_app_connection",
                               base._require_scratch_connection):
            with closure_cli._worker_singleton(base.old_settings) as singleton:
                first_process = new_closure(singleton)
                preview = closure_cli.run_preview(Namespace(
                    inventory=inventory_file, key_lookups=lookup_file,
                    origin_runtime_identity_sha256=base.parent.runtime_identity,
                    key_ttl_seconds=ttl, out=plan_file,
                ), first_process)
                self.assertEqual(preview["closable"], [work.attempt_id])
                execute_args = Namespace(
                    plan=plan_file, expect_sha256=preview["plan_sha256"],
                    inventory=inventory_file, key_lookups=lookup_file,
                    decided_by="independent-scratch", reason="actual old key lifetime elapsed",
                    out=receipt_file,
                )
                # Fail exactly after the failure successor is durable and before
                # owned cleanup starts: the decision has an uncertain reply.
                with mock.patch.object(first_process._backend, "finish_closed_cleanup",
                                       side_effect=RuntimeError("lost before cleanup")):
                    with self.assertRaisesRegex(RuntimeError, "lost before cleanup"):
                        closure_cli.run_execute(execute_args, first_process)
                pending = base._authority(work.attempt_id)
                self.assertEqual(pending.state, "cleanup_pending")
                self.assertEqual(pending.checkpoint_history[0].sha256, h0_sha)
                self.assertEqual(pending.client_submit_key, original_key)
                self.assertFalse(receipt_file.exists())
                self.assertEqual(base.fleet.http_call_count(), calls_before)

            # Model the process being gone: release singleton, expire only its
            # scratch claim, and compose a new owner against the same DB/head.
            base._expire_claims()
            with closure_cli._worker_singleton(base.old_settings) as singleton:
                recovered = new_closure(singleton)
                first = closure_cli.run_execute(execute_args, recovered)
                final = base._authority(work.attempt_id)
                self.assertEqual((final.state, final.client_submit_key),
                                 ("pre_submission_failed", original_key))
                self.assertEqual(final.checkpoint_history[0].sha256, h0_sha)
                self.assertEqual(first["continued_from"][work.attempt_id], "cleanup_pending")
                self.assertEqual(len([item for item in final.evidence
                                      if item.kind == "failure_receipt"]), 1)
                self.assertEqual(len([item for item in final.evidence
                                      if item.kind == "cleanup_receipt"]), 1)
                self.assertFalse(any(item.kind in ("submission_intent", "accepted_submission", "ack_receipt")
                                     for item in final.evidence))
                with unit_of_work_factory(base.engine)() as uow:
                    run = uow.processing_runs.get(final.processing_run_id)
                    self.assertIsNotNone(run)
                    assert run is not None
                    self.assertEqual(run.status, "failed")
                    self.assertIsNone(uow.processing_runs.parse_requeue_decision_for_run(
                        run.processing_run_id,
                    ))
                self.assertEqual(base.fleet.http_call_count(), calls_before)

                receipt = receipt_file.read_bytes()
                rows = base._durable_rows()
                replay = closure_cli.run_execute(execute_args, recovered)
                self.assertEqual(replay["members"], first["members"])
                self.assertEqual(receipt_file.read_bytes(), receipt)
                self.assertEqual(base._durable_rows(), rows)
                self.assertEqual(base.fleet.http_call_count(), calls_before)


if __name__ == "__main__":
    unittest.main()
