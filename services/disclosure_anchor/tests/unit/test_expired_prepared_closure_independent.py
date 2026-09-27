"""Independent pure acceptance for exact expired-original prepared closure plans.

Two synthetic V4 H0s have only preparation evidence, as the real nine did at
the read-only observation. Product capture and plan builders run; HTTP is a
scripted old-origin 404 and no database or parser is opened.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest

import httpx

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import legacy_member_from_authority
from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import capture_legacy_key_lookups
from disclosure_anchor.application.contracts.expired_prepared_closure import (
    ExpiredPreparedClosureRefused,
    build_expired_prepared_closure_plan,
    closure_decision_sha256,
    decode_expired_prepared_closure_plan,
    require_frozen_prepared_member,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyScopeInventory,
    encode_legacy_key_lookup_evidence,
    encode_legacy_scope_inventory,
)
from tests._f5_upgrade_duty_fixture import DutyWorld
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import write_private


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ExpiredPreparedClosureContractIndependent(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        self.ttl = 120
        self.origin = synthetic_parent_q0(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        world = DutyWorld(Path(temp.name))
        pair = dict(process_profile=self.origin.process_profile,
                    worker_profile=self.origin.worker_profile)
        self.expired = world.duty(
            tag="expired_h0", submission_epoch_unix=int((self.now - timedelta(seconds=150)).timestamp()),
            **pair,
        )
        self.valid = world.duty(
            tag="valid_h0", submission_epoch_unix=int((self.now - timedelta(seconds=110)).timestamp()),
            **pair,
        )
        self.heads = {d.attempt_id: d.prepared() for d in (self.expired, self.valid)}
        self.inventory = LegacyScopeInventory(
            captured_at_utc=(self.now - timedelta(seconds=100)).isoformat(),
            members=tuple(sorted((legacy_member_from_authority(h) for h in self.heads.values()),
                                 key=lambda item: item.attempt_id)),
        )
        inventory_bytes = encode_legacy_scope_inventory(self.inventory)
        self.inventory_sha = _sha(inventory_bytes)
        inventory_file = write_private(self.origin.root / "closure-inventory.json", inventory_bytes)
        looked_up: list[str] = []

        def absent(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "GET")
            looked_up.append(request.url.path)
            return httpx.Response(404, json={"detail": "Task not found"})

        self.lookups = capture_legacy_key_lookups(
            self.origin.settings, inventory=inventory_file, key_ttl_seconds=self.ttl,
            transport=httpx.MockTransport(absent),
            wall_clock=lambda: self.now - timedelta(seconds=90),
        )
        self.assertEqual(len(looked_up), 2)
        self.lookup_sha = _sha(encode_legacy_key_lookup_evidence(self.lookups))

    def plan(self, **changes):
        values = dict(
            inventory=self.inventory, inventory_sha256=self.inventory_sha,
            key_lookups=self.lookups, key_lookups_sha256=self.lookup_sha,
            origin_runtime_identity_sha256=self.origin.runtime_identity,
            key_ttl_seconds=self.ttl, heads=self.heads, now=self.now,
        )
        values.update(changes)
        return build_expired_prepared_closure_plan(**values)

    def test_mixed_inventory_closes_only_expired_exact_h0_without_snapshot(self) -> None:
        for duty in (self.expired, self.valid):
            head = self.heads[duty.attempt_id]
            self.assertEqual(head.lifecycle_version, 0)
            self.assertEqual([item.kind for item in head.evidence], ["preparation_intent"])
            self.assertIsNone(head.checkpoint.snapshot_receipt_sha256)
            member = next(m for m in self.inventory.members if m.attempt_id == duty.attempt_id)
            require_frozen_prepared_member(head, member)

        plan = self.plan()
        self.assertEqual([item.attempt_id for item in plan.members], [self.expired.attempt_id])
        self.assertEqual(plan.still_valid, ((self.valid.attempt_id,
                                             self.valid.spec.prepared_submission.submission_epoch_unix + self.ttl),))
        self.assertEqual(decode_expired_prepared_closure_plan(plan.exact_bytes), plan)
        self.assertEqual(plan.members[0].h0_checkpoint_sha256, self.expired.h0.sha256)
        self.assertEqual(plan.members[0].client_submit_key,
                         self.expired.spec.prepared_submission.client_submit_key)
        first = closure_decision_sha256(plan_sha256=plan.sha256, decided_by="operator-a", reason="expired")
        second = closure_decision_sha256(plan_sha256=plan.sha256, decided_by="operator-b", reason="expired")
        self.assertNotEqual(first, second, "decision attribution must change its identity")

    def test_no_premature_close_and_incomplete_old_origin_proof_refuse(self) -> None:
        before_expiry = self.now - timedelta(seconds=31)
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "no original key has expired"):
            self.plan(now=before_expiry)

        changed = replace(self.lookups,
                          lookups=tuple(replace(item, observed_at_utc=self.now.isoformat())
                                        for item in self.lookups.lookups))
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "had expired when it was looked up"):
            self.plan(key_lookups=changed)
        wrong_status = replace(self.lookups,
                               lookups=(replace(self.lookups.lookups[0], http_status=200),
                                        *self.lookups.lookups[1:]))
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "not proven absent"):
            self.plan(key_lookups=wrong_status)
        wrong_key = replace(self.lookups,
                            lookups=(replace(self.lookups.lookups[0], client_submit_key="wrong-key"),
                                     *self.lookups.lookups[1:]))
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "not its original key"):
            self.plan(key_lookups=wrong_key)
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "origin runtime"):
            self.plan(origin_runtime_identity_sha256="sha256:" + "0" * 64)
        with self.assertRaisesRegex(ExpiredPreparedClosureRefused, "no current head"):
            self.plan(heads={self.valid.attempt_id: self.heads[self.valid.attempt_id]})

    def test_progressed_or_drifted_h0_and_spec_refuse_before_plan(self) -> None:
        expired = self.expired
        member = next(m for m in self.inventory.members if m.attempt_id == expired.attempt_id)
        for label, head in (
            ("reconciling", expired.reconciling()[0]),
            ("accepted", expired.submitted()),
            ("spec", replace(self.heads[expired.attempt_id],
                             execution_spec=replace(expired.spec, remote_runaway_seconds=86_401))),
            ("key", replace(self.heads[expired.attempt_id], client_submit_key="different-key")),
            ("noncurrent", replace(self.heads[expired.attempt_id], is_current=False)),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ExpiredPreparedClosureRefused):
                    require_frozen_prepared_member(head, member)
                with self.assertRaises(ExpiredPreparedClosureRefused):
                    self.plan(heads={**self.heads, expired.attempt_id: head})


if __name__ == "__main__":
    unittest.main()
