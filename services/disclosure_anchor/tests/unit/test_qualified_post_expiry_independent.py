"""Original-key expiry across a qualified-upgrade preflight and a later POST.

The synthetic Qnew bundle passes the real deployment checker. The in-memory
V4 duty supplies an exact H0/spec/source and the real resolver issues the
legacy POST proof. Only the provider wire and wall clock are scripted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import legacy_member_from_authority
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.application.contracts.worker_execution_upgrade import LegacyScopeInventory
from tests._f5_upgrade_duty_fixture import DutyWorld, StageGuard
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import current_worker_profile
from tests._qualified_upgrade_flow_fixture_independent import build_synthetic_qnew_flow


class QualifiedOriginalKeyPostExpiryIndependent(unittest.TestCase):
    def test_preflight_valid_key_cannot_post_after_approved_ttl_expires(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        epoch = int((now - timedelta(seconds=30)).timestamp())
        origin = synthetic_parent_q0(self)
        with tempfile.TemporaryDirectory() as scratch:
            world = DutyWorld(Path(scratch))
            duty = world.duty(
                tag="qnew_ttl", process_profile=origin.process_profile,
                worker_profile=origin.worker_profile, submission_epoch_unix=epoch,
            )
            waiting = world.duty(
                tag="qnew_ttl_waiting", process_profile=origin.process_profile,
                worker_profile=origin.worker_profile, submission_epoch_unix=epoch,
            )
            accepted_late = world.duty(
                tag="qnew_ttl_accepted", process_profile=origin.process_profile,
                worker_profile=origin.worker_profile, submission_epoch_unix=epoch,
            )
            inventory = LegacyScopeInventory(
                captured_at_utc=(now - timedelta(seconds=5)).isoformat(),
                members=tuple(sorted((
                    legacy_member_from_authority(duty.prepared()),
                    legacy_member_from_authority(waiting.prepared()),
                    legacy_member_from_authority(accepted_late.prepared()),
                ), key=lambda item: item.attempt_id)),
            )

            def absent(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                return httpx.Response(404, json={"detail": "Task not found"})

            flow = build_synthetic_qnew_flow(
                self, inventory=inventory, origin=origin,
                old_key_transport=httpx.MockTransport(absent), now=now,
            )
            checker = flow.checker(now=now)
            self.assertEqual(checker.qualification_origin, "exact")
            execution = checker.verified_execution
            self.assertIsNotNone(execution)
            assert execution is not None
            ttl = execution.upgrade.key_lookups.key_ttl_seconds
            self.assertEqual(ttl, 86_400)
            self.assertLess(int(now.timestamp()) - epoch, ttl)

            resolver = world.resolver(
                current_worker_profile(origin, flow.process_profile),
                legacy_execution=execution,
            )
            authority, intent, _ = duty.reconciling()
            snapshot, _, source = duty.command_parts()
            command = resolver.submission_command(
                authority, snapshot=snapshot, intent=intent,
                snapshot_source=source, stage_guard=StageGuard(),
            )
            waiting_authority, waiting_intent, _ = waiting.reconciling()
            waiting_snapshot, _, waiting_source = waiting.command_parts()
            waiting_command = resolver.submission_command(
                waiting_authority, snapshot=waiting_snapshot, intent=waiting_intent,
                snapshot_source=waiting_source, stage_guard=StageGuard(),
            )
            accepted_authority, accepted_intent, _ = accepted_late.reconciling()
            accepted_snapshot, _, accepted_source = accepted_late.command_parts()
            accepted_command = resolver.submission_command(
                accepted_authority, snapshot=accepted_snapshot, intent=accepted_intent,
                snapshot_source=accepted_source, stage_guard=StageGuard(),
            )
            self.assertEqual(command.submission_intent.client_submit_key,
                             next(item.client_submit_key for item in inventory.members
                                  if item.attempt_id == duty.attempt_id))
            self.assertIsNotNone(command.legacy_authorization)

            # The actual preflight scope logic uses the approved original-key
            # TTL, never a caller's longer replacement, against the same H0.
            repository = SimpleNamespace(count_staged_prepared_heads=lambda: 0)
            engine = SimpleNamespace(dispose=lambda: None)

            @contextmanager
            def read_only(_engine):
                yield repository, now, {}

            def preflight(supplied_ttl: int, at: datetime):
                report, blockers = {}, []
                with (
                    mock.patch.object(upgrade_runtime, "read_only_repository", read_only),
                    mock.patch.object(upgrade_runtime, "observe_unresolved_heads",
                                      return_value=(authority, waiting_authority, accepted_authority)),
                    mock.patch.object(upgrade_runtime, "_preflight_resolver", return_value=resolver),
                ):
                    upgrade_runtime._preflight_scope(
                        report, blockers, flow.settings, lambda: engine,
                        current_worker_profile(origin, flow.process_profile),
                        execution, supplied_ttl, at,
                    )
                return report, blockers

            first_report, first_blockers = preflight(ttl, now)
            self.assertEqual(first_report["prepared_key_status"], "verified")
            self.assertEqual(first_blockers, [])

            clock = [float(epoch + ttl - 1)]
            calls: list[str] = []
            selected = [duty]
            existing_lookup = [False]

            def task_payload() -> dict:
                return {
                    "task_id": "task-qnew-ttl", "status": "pending",
                    "status_url": "/tasks/task-qnew-ttl",
                    "result_url": "/tasks/task-qnew-ttl/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": selected[0].spec.prepared_submission.client_submit_key,
                    "attempt_identity": selected[0].attempt_id,
                    "fence_identity": selected[0].fence_identity,
                    "protocol_state": "pending", "error": None,
                    "storage": {
                        "schema": "mineru.task-storage-status.v1",
                        "policy_sha256": flow.capacity.result_storage.sha256,
                        "phase": "admitted", "wait_reason": None,
                        "wait_since_unix": None, "blocked": False,
                        "selected_bytes": 0, "member_count": 0,
                        "inventory_sha256": None, "zip_bytes": 0,
                    },
                }

            def provider(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                if request.method == "GET":
                    if existing_lookup[0]:
                        return httpx.Response(200, json=task_payload())
                    return httpx.Response(404, json={"detail": "Task not found"})
                self.assertEqual(request.method, "POST")
                self.assertIn(selected[0].spec.prepared_submission.client_submit_key.encode(), request.read())
                return httpx.Response(202, json=task_payload())

            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(provider), wall_clock=lambda: clock[0],
                request_timeout_seconds=30, legacy_execution=execution,
                result_storage_policy_sha256=flow.capacity.result_storage.sha256,
            ) as remote:
                accepted = remote.reconcile_or_submit(command)
                self.assertEqual(accepted.receipt.remote_task_identity, "task-qnew-ttl")
                self.assertEqual(calls, ["GET", "POST"])

                clock[0] = float(epoch + ttl)
                late = datetime.fromtimestamp(clock[0], UTC)
                late_report, late_blockers = preflight(ttl * 2, late)
                self.assertNotEqual(late_report.get("prepared_key_status"), "verified")
                self.assertTrue(late_blockers)
                calls.clear()
                selected[0] = waiting
                try:
                    remote.reconcile_or_submit(waiting_command)
                except Exception:  # noqa: BLE001 - refusal type is not the contract under test
                    pass
                self.assertNotIn("POST", calls, "an expired original key must never leave as a new POST")

                # An already accepted original task is still recoverable via
                # GET after TTL. This branch must never open its snapshot or
                # send a replacement POST, even if the no-POST gate refuses 404.
                calls.clear()
                selected[0] = accepted_late
                existing_lookup[0] = True
                seen = remote.reconcile_or_submit(accepted_command)
                self.assertEqual(seen.receipt.attempt_id, accepted_late.attempt_id)
                self.assertEqual(seen.receipt.remote_task_identity, "task-qnew-ttl")
                self.assertEqual(calls, ["GET"])
                self.assertEqual(accepted_source.opens, 0)


if __name__ == "__main__":
    unittest.main()
