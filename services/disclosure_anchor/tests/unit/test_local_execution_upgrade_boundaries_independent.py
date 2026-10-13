"""Independent acceptance: the verified legacy context at its real boundaries (no database).

Pro §C2/§C4 and root's review items 1 and 5. One verified context, obtained from the real gate over
an independent U01 bundle, reaches the resolver, the POST boundary and the new-H0 hold:

* a listed member reopens on the current composition with every H0 fact exact; it may progress
  but never rewrite, regress or drift; a non-member under the same parent pair is refused;
* a head bound exactly to the current R1/P1/WP1 is ordinary exact-path work;
* a valid never-submitted member POSTs its original request bytes and key under a task-specific
  proof, while stream pressure judges the POST under R1; the proof is rechecked against its member
  (H0, spec, fence, source, request, key, parent P0) and its issuing context;
* new H0 admission is held while the legacy obligations are open; prepared claims continue.

Members are captured from real in-memory H0 packets (``tests._f5_upgrade_duty_fixture``) and the
independent member reading is compared with the product's capture.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import functools
from pathlib import Path
import shutil
import tempfile
from typing import Any
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import (
    legacy_member_from_authority,
    verify_legacy_scope,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import TASK_PROTOCOL_V2
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import advance_remote_parse_checkpoint_v4
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyExecutionRefused,
    LegacyObligationsOpen,
    VerifiedLegacyExecutionAuthorization,
    VerifiedQualifiedExecution,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import V4HeadNotFound
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderProtocolErrorV4,
    RemoteSubmissionCommandV4,
)
from disclosure_anchor.application.services.staged_new_work_admission_v4 import StagedV4NewWorkAdmitter
from tests._f5_upgrade_duty_fixture import Duty, DutyWorld, StageGuard, digest, member_payload
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import U01Bundle, build_u01
# Module import: importing the TestCase class by name would re-run its tests here.
from tests.unit import test_staged_new_work_admission_v4 as admission_tests


class _StreamGuard:
    """Records the runtime every POST is admitted under (the stream-pressure boundary)."""

    def __init__(self) -> None:
        self.runtimes: list[str] = []

    def assert_submission_allowed(self, *, runtime_identity_sha256: str) -> None:
        self.runtimes.append(runtime_identity_sha256)


class _LegacyContext:
    """One verified context over two captured members, plus a non-member and a new R1 head."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.parent = synthetic_parent_q0(test)
        root = Path(tempfile.mkdtemp(prefix="f5-duties-", dir=Path(tempfile.gettempdir()).resolve()))
        test.addCleanup(shutil.rmtree, root, True)
        self.world = DutyWorld(root)
        parent_pair = {"process_profile": self.parent.process_profile, "worker_profile": self.parent.worker_profile}
        self.prepared_member: Duty = self.world.duty(tag="member_prepared", **parent_pair)
        self.progressing_member: Duty = self.world.duty(tag="member_reconciling", **parent_pair)
        self.stranger: Duty = self.world.duty(tag="stranger_parent_pair", **parent_pair)
        self.captured_reconciling, _intent, _checkpoint = self.progressing_member.reconciling()
        self.captured = (self.prepared_member.prepared(), self.captured_reconciling)
        self.bundle: U01Bundle = build_u01(self.parent, members=[member_payload(item) for item in self.captured])
        execution = self.bundle.verify().execution
        assert isinstance(execution, VerifiedQualifiedExecution)
        self.execution = execution
        self.current: Duty = self.world.duty(
            tag="current_r1", process_profile=self.bundle.process_profile, worker_profile=self.bundle.worker_profile,
        )
        self.resolver = self.world.resolver(self.bundle.worker_profile, legacy_execution=execution)

    def submission(self, duty: Duty, resolver: Any = None) -> RemoteSubmissionCommandV4:
        authority, intent, _checkpoint = duty.reconciling()
        snapshot, _intent, source = duty.command_parts()
        return (resolver or self.resolver).submission_command(
            authority, snapshot=snapshot, intent=intent, snapshot_source=source,  # type: ignore[arg-type]
            stage_guard=StageGuard(),  # type: ignore[arg-type]
        )


class LegacyScopeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = _LegacyContext(self)
        self.execution = self.context.execution

    def test_the_independent_member_reading_equals_the_product_capture(self) -> None:
        for authority in self.context.captured:
            with self.subTest(attempt=authority.attempt_id):
                self.assertEqual(asdict(legacy_member_from_authority(authority)), member_payload(authority))

    def test_a_listed_member_reopens_on_the_current_composition_with_its_h0_exact(self) -> None:
        context = self.context
        prepared = context.prepared_member.prepared()
        context.resolver.assert_execution_profile(prepared)
        context.resolver.inspect_frozen_identity(prepared)
        self.assertEqual(self.execution.require_member_progress(prepared).attempt_id, prepared.attempt_id)

    def test_members_may_progress_but_never_regress_or_rewrite_their_history(self) -> None:
        context = self.context
        member = context.progressing_member
        submitted = member.submitted()
        self.assertGreater(submitted.lifecycle_version, context.captured_reconciling.lifecycle_version)
        self.execution.require_member_progress(submitted)
        context.resolver.assert_execution_profile(submitted)
        with self.assertRaisesRegex(LegacyExecutionRefused, "not a monotonic continuation"):
            self.execution.require_member_progress(member.prepared())
        _authority, _intent, _captured = member.reconciling()
        other = advance_remote_parse_checkpoint_v4(
            member.h0, state="reconciling", held_resource_credit=member.held_before_submit,
            snapshot_receipt_sha256=member.snapshot.sha256, submission_intent_sha256=digest(b"another intent"),
        )
        rewritten = replace(submitted, checkpoint_history=(member.h0, other, *submitted.checkpoint_history[2:]))
        with self.assertRaisesRegex(LegacyExecutionRefused, "not a monotonic continuation"):
            self.execution.require_member_progress(rewritten)

    def test_any_drift_of_a_member_identity_is_refused(self) -> None:
        # An authority always agrees with its own history (the contract refuses anything else),
        # so drift is a consistent twin: same attempt, fence, document and run, but another key,
        # source, request or spec than the captured member.
        context = self.context
        prepared = context.prepared_member.prepared()
        spec = prepared.execution_spec
        assert spec is not None
        root = Path(tempfile.mkdtemp(prefix="f5-twins-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(shutil.rmtree, root, True)
        pair = {"process_profile": context.parent.process_profile, "worker_profile": context.parent.worker_profile}
        twins = {
            "spec": replace(prepared, execution_spec=replace(spec, remote_runaway_seconds=86_401)),
            "key and epoch": DutyWorld(root / "epoch").duty(
                tag="member_prepared", submission_epoch_unix=spec.prepared_submission.submission_epoch_unix + 1,
                **pair).prepared(),
            "source bytes": DutyWorld(root / "source").duty(
                tag="member_prepared", source_variant=b" (other bytes)", **pair).prepared(),
            "request": DutyWorld(root / "request").duty(tag="member_prepared", timeout_seconds=21_599, **pair).prepared(),
        }
        for label, twin in twins.items():
            with self.subTest(drift=label):
                self.assertEqual((twin.attempt_id, twin.fence_identity, twin.document_id, twin.processing_run_id),
                                 (prepared.attempt_id, prepared.fence_identity, prepared.document_id,
                                  prepared.processing_run_id))
                with self.assertRaisesRegex(LegacyExecutionRefused, "identity drifted"):
                    self.execution.require_member_progress(twin)

    def test_a_non_member_under_the_parent_pair_is_refused_at_every_entry(self) -> None:
        stranger = self.context.stranger.prepared()
        with self.assertRaisesRegex(LegacyExecutionRefused, "not an obligation of the verified legacy scope"):
            self.execution.require_member_progress(stranger)
        with self.assertRaisesRegex(LegacyExecutionRefused, "not an obligation of the verified legacy scope"):
            self.context.resolver.assert_execution_profile(stranger)
        with self.assertRaisesRegex(LegacyExecutionRefused, "neither a verified legacy obligation nor bound"):
            self.execution.require_current_execution(stranger)

    def test_a_head_bound_to_the_current_execution_is_ordinary_exact_path_work(self) -> None:
        current = self.context.current.prepared()
        self.context.resolver.assert_execution_profile(current)
        self.execution.require_current_execution(current)
        with self.assertRaisesRegex(LegacyExecutionRefused, "not bound to the verified parent profile pair"):
            self.execution.require_member_progress(current)
        self.assertIsNone(self.context.submission(self.context.current).legacy_authorization)

    def test_without_the_context_the_parent_pair_cannot_reopen(self) -> None:
        plain = self.context.world.resolver(self.context.bundle.worker_profile)
        with self.assertRaisesRegex(ValueError, "worker composition changed"):
            plain.assert_execution_profile(self.context.prepared_member.prepared())

    def test_the_context_binds_the_resolver_to_the_verified_current_worker_profile(self) -> None:
        world, bundle = self.context.world, self.context.bundle
        for label, profile in (
            ("parent WP0", self.context.parent.worker_profile),
            ("other WP1", replace(bundle.worker_profile, mac_finalize_workers=bundle.worker_profile.mac_finalize_workers + 1)),
        ):
            with self.subTest(profile=label), self.assertRaisesRegex(LegacyExecutionRefused, "not the verified current"):
                world.resolver(profile, legacy_execution=self.execution)


class _ScopeRepository:
    """The two reads ``verify_legacy_scope`` makes besides the unresolved snapshot."""

    def __init__(self, *, staged: int = 0, observed: dict[str, Any] | None = None) -> None:
        self.staged, self.observed = staged, observed or {}

    def count_staged_prepared_heads(self) -> int:
        return self.staged

    def observe(self, attempt_id: str) -> Any:
        if attempt_id not in self.observed:
            raise V4HeadNotFound("v4 authority head is absent")
        return self.observed[attempt_id]


class LegacyScopeVerificationTests(unittest.TestCase):
    """The shared scope rule (recheck, preflight, doctor) while original obligations are open.

    Closed members need real final checkpoints; the post-closure R1 restart and the extra
    old-profile head after closure are exercised on the scratch database.
    """

    def setUp(self) -> None:
        self.context = _LegacyContext(self)
        self.execution = self.context.execution
        self.members = (self.context.prepared_member.prepared(), self.context.progressing_member.submitted())

    def _verify(self, unresolved: tuple[Any, ...], repository: _ScopeRepository | None = None) -> Any:
        captured = datetime.fromisoformat(self.execution.inventory.captured_at_utc)
        return verify_legacy_scope(repository or _ScopeRepository(), self.execution,
                                   observed_at=captured + timedelta(seconds=2), unresolved=unresolved,
                                   created_at_by_attempt={item.attempt_id: captured + timedelta(seconds=1)
                                                          for item in unresolved})

    def test_open_members_alone_verify_with_their_progress(self) -> None:
        observed = self._verify(self.members)
        self.assertEqual(sorted(observed.unresolved_members), sorted(item.attempt_id for item in self.members))
        self.assertEqual((observed.closed_members, observed.current_execution_heads), ((), ()))
        self.assertEqual(dict(observed.state_counts), {"prepared": 1, "submitted": 1})

    def test_any_outside_head_while_members_are_open_is_premature(self) -> None:
        for label, outside in (("current R1 head", self.context.current.prepared()),
                               ("old-profile head", self.context.stranger.prepared())):
            for unresolved in ((*self.members, outside), (outside, *self.members)):
                with self.subTest(outside=label, first=unresolved[0].attempt_id), self.assertRaisesRegex(
                    LegacyExecutionRefused, "new work before legacy closure is premature",
                ):
                    self._verify(unresolved)

    def test_a_member_that_vanished_or_is_open_outside_the_snapshot_is_refused(self) -> None:
        prepared, submitted = self.members
        with self.assertRaisesRegex(LegacyExecutionRefused, f"legacy obligation {submitted.attempt_id} disappeared"):
            self._verify((prepared,))
        still_open = _ScopeRepository(observed={submitted.attempt_id: submitted})
        with self.assertRaisesRegex(LegacyExecutionRefused, "is neither unresolved nor final"):
            self._verify((prepared,), still_open)

    def test_a_staged_non_current_head_refuses_the_scope(self) -> None:
        with self.assertRaisesRegex(LegacyExecutionRefused, "staged \\(non-current\\) prepared V4 head"):
            self._verify(self.members, _ScopeRepository(staged=1))


class LegacySubmissionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = _LegacyContext(self)
        self.execution = self.context.execution
        self.member = self.context.prepared_member
        self.command = self.context.submission(self.member)
        authorization = self.command.legacy_authorization
        assert isinstance(authorization, VerifiedLegacyExecutionAuthorization)
        self.authorization = authorization

    def test_a_valid_member_submits_its_original_request_and_key_under_a_proof(self) -> None:
        context, member, command, proof = self.context, self.member, self.command, self.authorization
        original = member.spec.prepared_submission
        self.assertEqual(command.request_exact_bytes, member.spec.request_exact_bytes)
        self.assertEqual(
            (command.submission_intent.client_submit_key, command.submission_intent.request_sha256,
             command.submission_intent.runtime_epoch_sha256, command.parser_options.runtime_bundle_identity_sha256),
            (original.client_submit_key, original.request_sha256, context.parent.runtime_identity,
             context.parent.runtime_identity),
        )
        self.assertEqual(
            (proof.attempt_id, proof.fence_identity, proof.h0_checkpoint_sha256, proof.execution_spec_sha256,
             proof.submission_intent_sha256, proof.source_pdf_sha256, proof.request_sha256, proof.client_submit_key,
             proof.parent_runtime_identity_sha256, proof.parent_process_profile_sha256,
             proof.active_runtime_identity_sha256, proof.upgrade_sha256),
            (member.attempt_id, member.fence_identity, member.h0.sha256, member.spec.sha256,
             command.submission_intent.sha256, original.source_pdf_sha256, original.request_sha256,
             original.client_submit_key, context.parent.runtime_identity, context.parent.process_profile.sha256,
             context.bundle.current_runtime, context.bundle.proposal_sha256),
        )
        self.assertEqual(self.execution.require_submission(command, proof), context.bundle.current_runtime)

    def test_the_post_boundary_rechecks_the_proof_against_its_member_and_issuer(self) -> None:
        # Root item 5: every identity the proof carries is compared with the verified member.
        context, proof = self.context, self.authorization
        other_member = context.progressing_member
        for field, value in (
            ("h0_checkpoint_sha256", other_member.h0.sha256),
            ("execution_spec_sha256", other_member.spec.sha256),
            ("parent_process_profile_sha256", context.bundle.process_profile.sha256),
            ("fence_identity", other_member.fence_identity),
            ("source_pdf_sha256", digest(other_member.source_bytes)),
            ("request_sha256", other_member.spec.request_sha256),
            ("client_submit_key", other_member.spec.prepared_submission.client_submit_key),
            ("upgrade_sha256", digest(b"another upgrade")),
            ("active_runtime_identity_sha256", context.parent.runtime_identity),
            ("parent_runtime_identity_sha256", context.bundle.current_runtime),
        ):
            with self.subTest(field=field), self.assertRaises(LegacyExecutionRefused):
                self.execution.require_submission(self.command, replace(proof, **{field: value}))
        # The same bytes verified again are another context: its proofs are not interchangeable.
        second = context.bundle.verify().execution
        assert second is not None
        with self.assertRaisesRegex(LegacyExecutionRefused, "not issued by this verified execution"):
            second.require_submission(self.command, proof)
        # A proof never travels to another command.
        other_command = context.submission(other_member)
        with self.assertRaises(ValueError):
            replace(other_command, legacy_authorization=proof)

    def test_the_stream_guard_admits_the_original_post_under_r1_after_a_closed_404(self) -> None:
        context, member, command = self.context, self.member, self.command
        key = command.submission_intent.client_submit_key
        calls: list[str] = []
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "Task not found"})
            bodies.append(request.read())
            return httpx.Response(202, json={**_task(key, member), "message": "Task submitted successfully"})

        guard = _StreamGuard()
        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler), token_factory=lambda count: b"t" * count,
            request_timeout_seconds=30.0, submission_guard=guard, legacy_execution=self.execution,
        ) as remote:
            result = remote.reconcile_or_submit(command)
        self.assertEqual(calls, [f"GET /tasks/by-idempotency/{key}", "POST /tasks"])
        self.assertEqual(guard.runtimes, [context.bundle.current_runtime], "pressure is judged under R1")
        self.assertEqual(len(bodies), 1)
        self.assertIn(key.encode(), bodies[0])
        self.assertIn(member.source_bytes, bodies[0])
        self.assertEqual(result.receipt.remote_task_identity, f"task-{member.attempt_id}")

    def test_a_proof_or_an_old_runtime_never_posts_through_the_wrong_transport(self) -> None:
        context = self.context
        key = self.command.submission_intent.client_submit_key
        unproven = replace(self.command, legacy_authorization=None)
        for label, legacy_execution, command, pattern in (
            ("proof without the context", None, self.command, "without the verified execution"),
            ("parent runtime without a proof", self.execution, unproven, "neither current nor a verified legacy"),
        ):
            with self.subTest(case=label):
                calls: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request.method)
                    return httpx.Response(404, json={"detail": "Task not found"})

                guard = _StreamGuard()
                with (
                    MinerUHttpRemoteV4(
                        transport=httpx.MockTransport(handler), token_factory=lambda count: b"t" * count,
                        request_timeout_seconds=30.0, submission_guard=guard, legacy_execution=legacy_execution,
                    ) as remote,
                    self.assertRaisesRegex(RemoteProviderProtocolErrorV4, pattern),
                ):
                    remote.reconcile_or_submit(command)
                self.assertNotIn("POST", calls)
                self.assertEqual(guard.runtimes, [])
        self.assertTrue(key)
        # recovery_only transport: even a verified proof never POSTs.
        posts: list[str] = []

        def recovery_handler(request: httpx.Request) -> httpx.Response:
            posts.append(request.method)
            return httpx.Response(404, json={"detail": "Task not found"})

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(recovery_handler), token_factory=lambda count: b"t" * count,
            request_timeout_seconds=30.0, allow_task_submission=False, legacy_execution=self.execution,
        ) as remote:
            try:
                remote.reconcile_or_submit(self.command)
            except Exception:  # noqa: BLE001 - any refusal shape; the witness is the wire
                pass
        self.assertNotIn("POST", posts)
        self.assertEqual(context.bundle.current_runtime, self.execution.current_runtime_identity_sha256)


def _task(key: str, duty: Duty) -> dict[str, Any]:
    task_id = f"task-{duty.attempt_id}"
    return {
        "task_id": task_id, "status": "pending", "status_url": f"/tasks/{task_id}",
        "result_url": f"/tasks/{task_id}/result", "task_protocol_schema": TASK_PROTOCOL_V2,
        "idempotency_key": key, "attempt_identity": duty.attempt_id, "fence_identity": duty.fence_identity,
        "protocol_state": "pending", "error": None,
    }


class _ObligationsGate:
    """Open for the given number of checks, then closed for good (closure never reopens)."""

    def __init__(self, open_checks: int) -> None:
        self.open_checks = open_checks
        self.calls = 0

    def require_closed(self) -> None:
        self.calls += 1
        if self.calls <= self.open_checks:
            raise LegacyObligationsOpen("legacy obligations open (2/2)")


class NewWorkHoldTests(unittest.TestCase):
    def _admitter(self, gate: _ObligationsGate) -> tuple[Any, ...]:
        constructor = functools.partial(StagedV4NewWorkAdmitter, legacy_obligations=gate)
        with mock.patch.object(admission_tests, "StagedV4NewWorkAdmitter", constructor):
            return admission_tests.StagedV4NewWorkAdmitterTests()._fixture()

    def test_new_h0_waits_for_the_legacy_obligations_while_prepared_claims_continue(self) -> None:
        gate = _ObligationsGate(2)
        admitter, candidates, claims, claimed, credit = self._admitter(gate)
        claims.admit_new = lambda **_kwargs: admission_tests.AdmissionOutcome(work=(claimed,), backlog_exists=False)
        two = credit.reservation + credit.reservation
        held = admitter.admit_new(limit=2, available_credits=two)
        self.assertEqual(held.work, (claimed,), "existing prepared claims keep draining")
        self.assertEqual(held.deferred_reason, "legacy obligations open (2/2)")
        self.assertIsNone(held.observation_request)
        self.assertEqual(candidates.calls, [], "no ordinary candidate is even listed while obligations are open")
        again = admitter.admit_new(limit=2, available_credits=two)
        self.assertEqual(again.deferred_reason, "legacy obligations open (2/2)")
        self.assertEqual(candidates.calls, [])
        claims.admit_new = lambda **_kwargs: admission_tests.AdmissionOutcome(work=(), backlog_exists=False)
        opened = admission_tests.StagedV4NewWorkAdmitterTests._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertIsNone(opened.deferred_reason)
        self.assertEqual(opened.work, (claimed,))
        self.assertEqual(claims.claims, ["attempt-1"], "the new H0 is created and claimed only after closure")
        self.assertGreaterEqual(gate.calls, 3)


if __name__ == "__main__":
    unittest.main()
