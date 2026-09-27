"""Managed closure of expired, never-submitted prepared obligations (Pro VI.2), without a database.

Real prepared H0s from the F5 duty fixture sit in the in-memory V4 repository;
the real persistence, backend and materializer cleanup close them. The final
failure commit is the in-memory counterpart of the PostgreSQL committer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import legacy_member_from_authority
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.contracts.expired_prepared_closure import (
    ExpiredPreparedClosureRefused,
    decode_expired_prepared_closure_plan,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyKeyLookup,
    LegacyKeyLookupEvidence,
    LegacyScopeInventory,
    encode_legacy_key_lookup_evidence,
    encode_legacy_scope_inventory,
)
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    failure_receipt_from_authority_v4,
    processing_run_error_from_failure_v4,
    processing_run_failed_event_v4,
)
from disclosure_anchor.cli.expired_prepared_closure import compose
from tests._f5_upgrade_duty_fixture import SUBMISSION_EPOCH, DutyWorld
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests.unit.test_mineru_http_staged_v4 import _published_test_root
from tests.unit.test_staged_coordinator_persistence_v4 import _Repository

TTL = 86_400
EPOCH = SUBMISSION_EPOCH
LATER_EPOCH = EPOCH + TTL // 2
CAPTURED = datetime.fromtimestamp(LATER_EPOCH + 10, UTC)
NOW = datetime.fromtimestamp(EPOCH + TTL + 60, UTC)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class _Committer:
    """The final-failure transaction: the successor, the failed run and its event together."""

    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.runs: list[tuple[str, dict[str, Any], str]] = []

    def commit(self, command: Any) -> Any:
        authority = self.repository.append_successor(command.append)
        receipt = failure_receipt_from_authority_v4(authority)
        event = processing_run_failed_event_v4(command, receipt)
        self.runs.append((authority.processing_run_id, processing_run_error_from_failure_v4(receipt), event.event_kind))
        return authority

    def reconcile(self, command: Any) -> Any:
        raise AssertionError("no final commit response is lost in these tests")


class _Uow:
    def __init__(self, factory: _Factory) -> None:
        self.remote_parse_v4 = factory.repository
        self.remote_parse_v4_failures = factory.committer
        self._factory = factory

    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        if self._factory.commit_losses:
            self._factory.commit_losses -= 1
            raise RuntimeError("commit response lost after the write applied")


class _Factory:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.committer = _Committer(repository)
        self.commit_losses = 0

    def __call__(self) -> _Uow:
        return _Uow(self)


class ExpiredPreparedClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        origin = synthetic_parent_q0(self)
        world = DutyWorld(root / "world")
        self.duties = {
            tag: world.duty(tag=tag, process_profile=origin.process_profile, worker_profile=origin.worker_profile,
                            submission_epoch_unix=epoch)
            for tag, epoch in (("expired_a", EPOCH), ("expired_b", EPOCH), ("valid", LATER_EPOCH))
        }
        heads = tuple(duty.prepared() for duty in self.duties.values())
        self.repository = _Repository(heads)
        # The E7 owner stopped long ago: its claims are expired in the database.
        self.repository.database_now = datetime(2030, 6, 1, tzinfo=UTC)
        self.factory = _Factory(self.repository)
        self.inventory = LegacyScopeInventory(
            captured_at_utc=CAPTURED.isoformat(),
            members=tuple(sorted((legacy_member_from_authority(head) for head in heads),
                                 key=lambda item: item.attempt_id)),
        )
        self.origin = self.inventory.members[0].runtime_epoch_sha256
        self.assertEqual(self.origin, origin.process_profile.runtime_bundle_identity_sha256)
        scratch = root / "scratch"
        scratch.mkdir(mode=0o700)
        self.scratch = scratch
        self.published = _published_test_root(scratch)

    def lookups(self, **changes: Any) -> LegacyKeyLookupEvidence:
        observed = (CAPTURED + timedelta(seconds=5)).isoformat()
        values: dict[str, Any] = dict(api_runtime_identity_sha256=self.origin, key_ttl_seconds=TTL)
        values.update(changes)
        status = values.pop("http_status", 404)
        observed = values.pop("observed_at_utc", observed)
        return LegacyKeyLookupEvidence(lookups=tuple(
            LegacyKeyLookup(
                attempt_id=member.attempt_id, client_submit_key=member.client_submit_key,
                lookup_request_sha256=_sha(member.client_submit_key.encode()), http_status=status,
                response_sha256=_sha(b"absent"), response_byte_count=6, observed_at_utc=observed,
            )
            for member in self.inventory.members
        ), **values)

    def closure(self, now: datetime = NOW):  # type: ignore[no-untyped-def]
        return compose(
            uow_factory=self.factory, scratch_root=self.scratch, published_root=self.published,
            process_guard=lambda: None, utc_now=lambda: now,
        )

    def plan(self, closure=None, lookups=None, **changes: Any):  # type: ignore[no-untyped-def]
        evidence = lookups or self.lookups()
        values: dict[str, Any] = dict(
            inventory=self.inventory, inventory_sha256=_sha(encode_legacy_scope_inventory(self.inventory)),
            key_lookups=evidence, key_lookups_sha256=_sha(encode_legacy_key_lookup_evidence(evidence)),
            origin_runtime_identity_sha256=self.origin, key_ttl_seconds=TTL,
        )
        values.update(changes)
        return (closure or self.closure()).plan(**values)

    def execute(self, plan, closure=None, reason: str = "keys expired before requalification"):  # type: ignore[no-untyped-def]
        evidence = self.lookups()
        return (closure or self.closure()).execute(
            plan=plan, inventory=self.inventory,
            inventory_sha256=_sha(encode_legacy_scope_inventory(self.inventory)),
            key_lookups=evidence, key_lookups_sha256=_sha(encode_legacy_key_lookup_evidence(evidence)),
            decided_by="root", reason=reason,
        )

    def test_plan_needs_before_expiry_absence_and_an_unmoved_never_submitted_h0(self) -> None:
        plan = self.plan()
        self.assertEqual([item.attempt_id for item in plan.members], ["rpa_f5_expired_a", "rpa_f5_expired_b"])
        self.assertEqual(plan.still_valid, (("rpa_f5_valid", LATER_EPOCH + TTL),))
        self.assertEqual(decode_expired_prepared_closure_plan(plan.exact_bytes), plan)
        after_expiry = (datetime.fromtimestamp(EPOCH + TTL + 1, UTC)).isoformat()
        moved = self.duties["expired_a"].reconciling()[0]
        for label, attempt in (
            ("a 404 after the key expired proves nothing", lambda: self.plan(
                lookups=self.lookups(observed_at_utc=after_expiry))),
            ("a found key", lambda: self.plan(lookups=self.lookups(http_status=200))),
            ("another lifetime than the evidence", lambda: self.plan(key_ttl_seconds=TTL * 2)),
            ("another origin runtime", lambda: self.plan(origin_runtime_identity_sha256="sha256:" + "9" * 64)),
            ("nothing expired yet", lambda: self.plan(closure=self.closure(CAPTURED))),
            ("a head past its H0", lambda: (
                self.repository.heads.__setitem__(moved.attempt_id, moved), self.plan())),
        ):
            with self.subTest(label), self.assertRaises(ExpiredPreparedClosureRefused):
                attempt()
        self.assertEqual(self.factory.committer.runs, [])

    def test_execute_closes_only_the_reviewed_members_through_the_ordinary_chain(self) -> None:
        plan = self.plan()
        closed = self.execute(plan)
        self.assertEqual([(item.attempt_id, item.final_state, item.continued_from) for item in closed], [
            ("rpa_f5_expired_a", "pre_submission_failed", "prepared"),
            ("rpa_f5_expired_b", "pre_submission_failed", "prepared"),
        ])
        for item in closed:
            head = self.repository.load(item.attempt_id)
            failure = failure_receipt_from_authority_v4(head)
            self.assertEqual(
                (failure.error_code, failure.retry_budget_class, failure.retryable,
                 failure.submission_was_attempted, failure.error_stage, head.claim_owner_identity),
                ("original_key_expired", "original_key_lifetime", False, False, "managed_closure", None),
            )
            self.assertIn(plan.sha256, failure.message)
            self.assertEqual(head.checkpoint_history[0].sha256, self.duties[item.attempt_id[7:]].h0.sha256)
            kinds = {evidence.kind for evidence in head.evidence}
            self.assertFalse(kinds & {"submission_intent", "accepted_submission", "ack_receipt"})
        self.assertEqual(
            [(run, error["retry_budget_class"], error["retryable"]) for run, error, _ in self.factory.committer.runs],
            [("run_f5_expired_a", "original_key_lifetime", False), ("run_f5_expired_b", "original_key_lifetime", False)],
        )
        # The member whose key is still valid is untouched: it keeps its H0.
        self.assertEqual(self.repository.load("rpa_f5_valid").state, "prepared")
        # The same decision replays without a second effect; another decision is refused.
        again = self.execute(plan)
        self.assertEqual([item.continued_from for item in again], ["pre_submission_failed"] * 2)
        self.assertEqual([replace(item, continued_from="") for item in again],
                         [replace(item, continued_from="") for item in closed])
        self.assertEqual(len(self.factory.committer.runs), 2)
        with self.assertRaises(ExpiredPreparedClosureRefused):
            self.execute(plan, reason="a different decision")

    def test_lost_response_interrupted_cleanup_and_live_claims_never_duplicate(self) -> None:
        plan = self.plan()
        # The claim's and the cleanup_pending append's commit responses are lost:
        # both are reconciled from the durable head, never written twice.
        self.factory.commit_losses = 2
        with mock.patch.object(
            MinerUHttpStagedV4, "cleanup_v4", side_effect=RuntimeError("interrupted before cleanup"),
        ), self.assertRaises(RuntimeError):
            self.execute(plan)
        interrupted = self.repository.load("rpa_f5_expired_a")
        self.assertEqual((interrupted.state, self.factory.commit_losses), ("cleanup_pending", 0))
        self.assertEqual(self.factory.committer.runs, [])
        # The interrupted run's claim is still live: another run never overrides it.
        with self.assertRaises(ExpiredPreparedClosureRefused):
            self.execute(plan)
        self.repository.heads["rpa_f5_expired_a"] = replace(
            self.repository.heads["rpa_f5_expired_a"],
            claim_lease_until=self.repository.database_now - timedelta(seconds=1), database_lease=None,
        )
        closed = self.execute(plan)
        self.assertEqual([item.continued_from for item in closed], ["cleanup_pending", "prepared"])
        self.assertEqual(len(self.factory.committer.runs), 2)
        self.assertEqual(
            sum(evidence.kind == "failure_receipt" for evidence in self.repository.load("rpa_f5_expired_a").evidence),
            1,
        )


if __name__ == "__main__":
    unittest.main()
