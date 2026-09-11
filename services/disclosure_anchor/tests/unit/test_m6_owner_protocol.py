"""Independently authored tests for the M6 owner control client and line transport (WP-D Python).

Expectations follow docs/implementation/design/m6-owner-control.md. The owner
here is a scripted double: a reply it composes proves nothing about a Windows
host, a journal or a physical clock. Lease arithmetic is asserted at
contract-visible boundaries (send-anchored, shortened by drift/uncertainty,
never receipt-anchored), not by reproducing the client's formula.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import m6_owner_ssh
from disclosure_anchor.adapters.runtime.m6_owner_protocol import (
    M6LeasePolicy, M6LineOwnerTransport, M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected,
)
from disclosure_anchor.application.contracts.m6_owner import (
    M6AdmissionClosedAck, M6AppendObservation, M6BindOwner, M6CloseOwner, M6OwnerControl, M6OwnerRequest,
)
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6OwnerResumed, M6RunEvent

from tests import m6_owner_support as owner_support
from tests import m6_support as m6
from tests.m6_owner_support import NS, FakeChannel, FakeOpener, ManualClock, ScriptedOwner, SYNTHETIC_TOKEN

MS = 1_000_000
GRANT_TICKS = 5_000_000  # 500 ms at the synthetic 10 MHz


class ClientCase(unittest.TestCase):
    """Common e2e-mode fixture: budget 5 s, reserve 200 ms, lease cap 1 s, margin 10 ms."""

    def setUp(self) -> None:
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh")})
        self.spec = self.fixture.spec
        self.anchor = owner_support.anchor_for(self.spec)
        self.clock = ManualClock(NS)
        self.owner = ScriptedOwner(self.spec, self.anchor, self.clock)
        self.obs = self.fixture.at(10)

    def client(self, **kwargs: object) -> M6OwnerClient:
        return owner_support.client_for(self.owner, **kwargs)  # type: ignore[arg-type]

    def lease(self, *, observed: int | None = None, grant_ticks: int = GRANT_TICKS, rtt_ns: int = 20 * MS,
              last_sequence: int = 0) -> None:
        observed = self.obs if observed is None else observed
        self.owner.answer(self.owner.status(observed=observed, state="open", last_sequence=last_sequence,
                                            lease=observed + grant_ticks), rtt_ns=rtt_ns)

    def status_ok(self, *, observed: int | None = None, state: str = "open", **kwargs: object) -> None:
        self.owner.answer(self.owner.status(observed=self.obs if observed is None else observed, state=state, **kwargs))  # type: ignore[arg-type]

    def admitted(self, *, sequence: int = 1, fence: str = "fence-1", kind: str = "e2e_runner",
                 epoch: str = m6.RUNNER_EPOCH) -> object:
        return owner_support.producer_event(self.spec, M6AttemptAdmitted(
            attempt_id="att-1", fence_identity=fence, document_id=self.fixture.entries["a"].document_id,
            processing_run_id="prun-1", source_pdf_sha256=self.fixture.entries["a"].source_pdf_sha256,
            source_byte_count=self.fixture.entries["a"].source_byte_count, source_page_count=7,
            process_profile_sha256=self.spec.runtime.process_profile_sha256,
        ), kind=kind, epoch=epoch, sequence=sequence)  # type: ignore[arg-type]

    def resume_record(self, *, new_epoch: str = m6.OWNER_EPOCH_2, previous: str | None = None,
                      sequence: int = 5, tick: int | None = None, kind: str = "owner", **payload: object) -> M6RunEvent:
        values: dict[str, object] = {
            "clock": self.spec.clock, "t0_ticks": self.spec.t0_ticks, "deadline_ticks": self.spec.deadline_ticks,
            "previous_owner_epoch_sha256": previous if previous is not None else self.anchor.owner_process_epoch_sha256,
        }
        values.update(payload)
        event = owner_support.producer_event(self.spec, M6OwnerResumed(**values), kind=kind, epoch=new_epoch, sequence=1)  # type: ignore[arg-type]
        return self.owner.stamp(event, sequence=sequence, tick=self.fixture.at(20) if tick is None else tick,
                                owner_epoch=new_epoch)


class BootstrapAndIdentityTests(ClientCase):
    def test_bootstrap_interval_and_runtime_bind_before_control(self) -> None:
        self.anchor.assert_spec(self.spec)
        dumped = self.anchor.model_dump()
        for absent in ("manifest_sha256", "spec_sha256", "scope_sha256", "quality_plan_sha256", "campaign_id"):
            self.assertNotIn(absent, dumped, "the anchor deliberately carries no corpus/spec hash")

        drifted_t0 = owner_support.anchor_for(self.spec, t0_ticks=self.spec.t0_ticks + 1,
                                              deadline_ticks=self.spec.deadline_ticks + 1)
        with self.assertRaises(ValueError):
            drifted_t0.assert_spec(self.spec)
        for label, overrides in {
            "owner_source": {"owner_source_sha256": m6.digest("owner-source:other")},
            "gpu": {"gpu_device_identity_sha256": m6.digest("gpu:other")},
            "run_id": {"run_id": "run-other"},
            "resources": {"resources": m6.envelope(max_events=199)},
            "clock": {"clock": m6.clock_domain("boot-b")},
            "planned_interval": {"planned_seconds": self.spec.planned_seconds + 1,
                                  "deadline_ticks": self.spec.deadline_ticks + m6.QPC_HZ},
            "close_bound": {"max_close_ticks": self.spec.max_close_ticks + 1},
        }.items():
            with self.assertRaises(ValueError, msg=label):
                owner_support.anchor_for(self.spec, **overrides).assert_spec(self.spec)
        with self.assertRaises(ValueError):
            owner_support.anchor_for(self.spec, deadline_ticks=self.spec.deadline_ticks + 1)
        with self.assertRaises(ValueError):
            owner_support.anchor_for(self.spec, max_close_ticks=self.spec.deadline_ticks - 1)

        with self.assertRaises(ValueError):
            M6OwnerClient(anchor=drifted_t0, spec=self.spec, transport=self.owner, caller_role="controller",
                          producer_epoch_sha256=m6.digest("controller"), continuous_ns=self.clock,
                          lease_policy=owner_support.policy())
        self.assertEqual(self.owner.requests, [], "identity is checked before any transport use")

        controller = self.client(role="controller", epoch=m6.digest("controller-epoch"))
        self.status_ok(state="bound")
        reply = controller.bind()
        self.assertEqual(reply.status.state, "bound")
        bind = self.owner.requests[-1]
        self.assertIsInstance(bind.command, M6BindOwner)
        self.assertEqual(bind.command.anchor_sha256, self.anchor.canonical_sha256())  # type: ignore[union-attr]
        self.assertEqual((bind.run_id, bind.spec_sha256), (self.spec.run_id, self.spec.canonical_sha256()))
        self.assertEqual(self.owner.requests[-1].contract_version, "m6.owner-request.v1")

        runner = self.client()
        with self.assertRaises(M6OwnerProtocolError):
            runner.bind()
        self.assertEqual(len(self.owner.requests), 1, "a runner bind never reaches the wire")

        # Bound but not opened: the owner answers lease requests with ok and a null lease.
        self.status_ok(state="bound")
        self.assertFalse(runner.refresh_admission())
        self.assertFalse(runner.admission_allowed())
        self.assertIsInstance(self.owner.requests[-1].command, M6OwnerControl)
        self.assertEqual(self.owner.requests[-1].command.kind, "lease")

    def test_identity_nonce_canonical_shape_and_bounds_are_checked(self) -> None:
        client = self.client()
        self.status_ok()
        self.status_ok()
        client.request(M6OwnerControl(kind="status"))
        client.request(M6OwnerControl(kind="status"))
        first, second = self.owner.requests[-2:]
        self.assertNotEqual(first.request_id, second.request_id, "every control exchange has a fresh request ID")
        self.assertTrue(first.request_id and second.request_id)
        self.assertNotIn(b"\n", self.owner.raw_requests[-1])
        self.assertEqual(M6OwnerRequest.from_canonical_bytes(self.owner.raw_requests[-1], maximum_bytes=65536), second)

        for label, status_kwargs in {
            "run_id": {"run_id": "run-other"},
            "spec": {"spec_sha256": m6.digest("spec:other")},
            "anchor": {"anchor_sha256": m6.digest("anchor:other")},
            "owner_incarnation": {"epoch": m6.OWNER_EPOCH_2},
        }.items():
            self.status_ok(**status_kwargs)
            with self.assertRaises(M6OwnerProtocolError, msg=label):
                client.request(M6OwnerControl(kind="status"))

        status = self.owner.status(observed=self.obs)
        self.owner.answer_raw(lambda request: self.owner.reply(
            request.model_copy(update={"request_id": "someone-else"}), status))
        with self.assertRaises(M6OwnerProtocolError):
            client.request(M6OwnerControl(kind="status"))

        shapes = {
            "indented": lambda request: json.dumps(json.loads(self.owner.reply(request, status)), indent=1).encode(),
            "trailing_lf": lambda request: self.owner.reply(request, status) + b"\n",
            "padded_over_bound": lambda request: self.owner.reply(request, status) + b" " * 70_000,
            "unknown_field": lambda request: json.dumps(
                {**json.loads(self.owner.reply(request, status)), "extra": 1}, sort_keys=True,
                separators=(",", ":")).encode(),
            "missing_status": lambda request: json.dumps(
                {k: v for k, v in json.loads(self.owner.reply(request, status)).items() if k != "status"},
                sort_keys=True, separators=(",", ":")).encode(),
            "empty": lambda request: b"",
            "not_json": lambda request: b"ok",
        }
        for label, producer in shapes.items():
            self.owner.answer_raw(producer)
            with self.assertRaises(ValueError, msg=label):
                client.request(M6OwnerControl(kind="status"))

        for label, kwargs in {
            "epoch_not_hash": {"epoch": "abc"},
            "epoch_uppercase": {"epoch": m6.RUNNER_EPOCH.upper()},
            "role_owner": {"role": "owner"},
            "wire_bound_low": {"maximum_wire_bytes": 4095},
            "wire_bound_high": {"maximum_wire_bytes": 65537},
            "chain_not_tuple": {"recovery_chain": []},
        }.items():
            with self.assertRaises(ValueError, msg=label):
                self.client(**kwargs)


class LeaseGuardTests(ClientCase):
    def test_lease_starts_at_request_send_and_accounts_for_rtt_margin_and_drift(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease(rtt_ns=20 * MS)  # 500 ms grant, reply arrives 20 ms after send
        self.assertTrue(client.refresh_admission())
        self.assertEqual(self.clock.value, NS + 20 * MS)

        self.clock.value = NS + 400 * MS
        self.assertTrue(client.admission_allowed())
        self.clock.value = NS + 490 * MS
        self.assertFalse(client.admission_allowed(), "drift and uncertainty shorten the nominal 500 ms")
        self.clock.value = NS + 500 * MS
        self.assertFalse(client.admission_allowed(), "nominal end measured from send")
        self.clock.value = NS + 515 * MS
        self.assertFalse(client.admission_allowed(), "a receipt-anchored guard would still allow this")

        # A larger drift allowance shortens the grant further.
        drifty = self.client(lease_policy=owner_support.policy(maximum_clock_drift_ppm=100_000))
        self.clock.value = 5 * NS
        self.lease(rtt_ns=20 * MS)
        self.assertTrue(drifty.refresh_admission())
        self.clock.value = 5 * NS + 400 * MS
        self.assertTrue(drifty.admission_allowed())
        self.clock.value = 5 * NS + 460 * MS
        self.assertFalse(drifty.admission_allowed(), "10% drift allowance: less than 455 ms of 500 remain")

        # A larger uncertainty margin is subtracted as well.
        cautious = self.client(lease_policy=owner_support.policy(uncertainty_margin_ns=100 * MS))
        self.clock.value = 9 * NS
        self.lease(rtt_ns=20 * MS)
        self.assertTrue(cautious.refresh_admission())
        self.clock.value = 9 * NS + 390 * MS
        self.assertTrue(cautious.admission_allowed())
        self.clock.value = 9 * NS + 400 * MS
        self.assertFalse(cautious.admission_allowed())

    def test_reply_slower_than_grant_cannot_create_a_fresh_lease(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease(rtt_ns=495 * MS)  # the 500 ms grant is consumed by drift/margin before the reply lands
        self.assertFalse(client.refresh_admission())
        self.assertFalse(client.admission_allowed())

        self.clock.value = 3 * NS
        self.lease(rtt_ns=600 * MS)
        self.assertFalse(client.refresh_admission())

        self.clock.value = 5 * NS
        self.lease(rtt_ns=20 * MS)
        self.assertTrue(client.refresh_admission(), "a prompt reply after slow ones still leases normally")

    def test_sleep_or_expiry_cannot_preserve_admission(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())
        self.clock.value = NS + 100 * MS
        self.assertTrue(client.admission_allowed())
        self.clock.advance(10 * NS)  # the sleep-inclusive clock jumps across a suspend
        self.assertFalse(client.admission_allowed())
        self.assertFalse(client.admission_allowed(), "expiry is not undone by asking again")

        self.status_ok(lease=self.obs + GRANT_TICKS)
        client.request(M6OwnerControl(kind="status"))
        self.assertFalse(client.admission_allowed(), "an expired grant is not revived by a status exchange")

        self.lease(observed=self.fixture.at(11))
        self.assertTrue(client.refresh_admission(), "only a fresh lease exchange re-arms the guard")

    def test_clock_regression_and_transport_loss_latch_admission_closed_but_allow_drain(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())
        self.clock.value = NS - 1
        with self.assertRaises(M6OwnerProtocolError):
            client.admission_allowed()
        self.clock.value = NS + 100 * MS
        self.assertFalse(client.admission_allowed(), "admission is latched closed after a regression")
        self.lease(observed=self.fixture.at(11))
        self.assertFalse(client.refresh_admission(), "a later valid lease does not reopen a latched guard")
        event = self.admitted()
        record = self.owner.stamp(event, sequence=3, tick=self.fixture.at(12))  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(12), last_sequence=3), record=record)
        self.assertEqual(client.append(event), record, "drain/observation submission remains possible")  # type: ignore[arg-type]

        lost = ScriptedOwner(self.spec, self.anchor, ManualClock(NS))
        client = owner_support.client_for(lost)
        lost.answer(lost.status(observed=self.obs, lease=self.obs + GRANT_TICKS), rtt_ns=20 * MS)
        self.assertTrue(client.refresh_admission())
        lost.raise_on_exchange(EOFError("synthetic channel loss"))
        with self.assertRaises(EOFError):
            client.request(M6OwnerControl(kind="status"))
        self.assertEqual(len(lost.requests), 2, "no automatic retry after a lost response")
        self.assertEqual(lost.handlers, [], "the failed exchange consumed exactly one scripted reply")
        self.assertFalse(client.admission_allowed())
        lost.answer(lost.status(observed=self.fixture.at(11), lease=self.fixture.at(11) + GRANT_TICKS), rtt_ns=20 * MS)
        self.assertFalse(client.refresh_admission())
        lost.answer(lost.status(observed=self.fixture.at(12), state="stopping"))
        client.request(M6OwnerControl(kind="stop"))
        self.assertEqual(lost.requests[-1].command.kind, "stop", "control stays possible for drain")

    def test_lease_configuration_is_finite_and_conservative(self) -> None:
        owner_support.policy()
        M6LeasePolicy(stop_propagation_reserve_ns=1, maximum_lease_ns=30 * NS, uncertainty_margin_ns=1,
                      maximum_clock_drift_ppm=1)
        M6LeasePolicy(stop_propagation_reserve_ns=1, maximum_clock_drift_ppm=100_000)
        for label, kwargs in {
            "reserve_zero": {"stop_propagation_reserve_ns": 0},
            "reserve_negative": {"stop_propagation_reserve_ns": -1},
            "reserve_bool": {"stop_propagation_reserve_ns": True},
            "reserve_str": {"stop_propagation_reserve_ns": "1"},
            "lease_zero": {"maximum_lease_ns": 0},
            "lease_over_30s": {"maximum_lease_ns": 30 * NS + 1},
            "lease_float": {"maximum_lease_ns": 1e9},
            "margin_zero": {"uncertainty_margin_ns": 0},
            "margin_not_below_lease": {"uncertainty_margin_ns": NS},
            "drift_zero": {"maximum_clock_drift_ppm": 0},
            "drift_over": {"maximum_clock_drift_ppm": 100_001},
        }.items():
            with self.assertRaises(ValueError, msg=label):
                owner_support.policy(**kwargs)  # type: ignore[arg-type]

        one_second_budget = m6.make_fixture("e2e_publication", resources=m6.envelope(stop_admission_budget_ticks=m6.QPC_HZ))
        tight = ScriptedOwner(one_second_budget.spec, owner_support.anchor_for(one_second_budget.spec), self.clock)
        with self.assertRaises(ValueError):
            owner_support.client_for(tight, lease_policy=owner_support.policy(stop_propagation_reserve_ns=NS))
        with self.assertRaises(ValueError):
            owner_support.client_for(tight, lease_policy=owner_support.policy(stop_propagation_reserve_ns=995 * MS))
        owner_support.client_for(tight, lease_policy=owner_support.policy(stop_propagation_reserve_ns=900 * MS))
        self.assertEqual(tight.requests, [])

    def test_lease_must_leave_room_for_actual_claim_return_and_stop_ack(self) -> None:
        one_second_budget = m6.make_fixture("e2e_publication", resources=m6.envelope(stop_admission_budget_ticks=m6.QPC_HZ))
        owner = ScriptedOwner(one_second_budget.spec, owner_support.anchor_for(one_second_budget.spec), self.clock)
        client = owner_support.client_for(owner, lease_policy=owner_support.policy(stop_propagation_reserve_ns=100 * MS))
        obs = one_second_budget.at(10)
        owner.answer(owner.status(observed=obs, lease=obs + m6.QPC_HZ), rtt_ns=MS)  # a nominal 1 s grant
        with self.assertRaises(M6OwnerProtocolError):
            client.refresh_admission()
        self.assertFalse(client.admission_allowed())
        owner.answer(owner.status(observed=obs, lease=obs + 9 * m6.QPC_HZ // 10), rtt_ns=MS)  # 900 ms fits
        self.assertFalse(client.refresh_admission(), "a corrected reply cannot undo the protocol-error latch")

        # A separate, previously healthy client accepts the budget-minus-reserve
        # boundary. This is not automatic transport recovery after a failure.
        healthy_owner = ScriptedOwner(one_second_budget.spec, owner_support.anchor_for(one_second_budget.spec),
                                      ManualClock(NS))
        healthy = owner_support.client_for(healthy_owner, lease_policy=owner_support.policy(
            stop_propagation_reserve_ns=100 * MS))
        healthy_owner.answer(healthy_owner.status(observed=obs, lease=obs + 9 * m6.QPC_HZ // 10), rtt_ns=MS)
        self.assertTrue(healthy.refresh_admission())

        client = self.client()
        deadline = self.spec.deadline_ticks
        self.owner.answer(self.owner.status(observed=deadline - 1000, lease=deadline + 1), rtt_ns=MS)
        with self.assertRaises(M6OwnerProtocolError):
            client.refresh_admission()
        self.owner.answer(self.owner.status(observed=deadline - 1000, lease=deadline), rtt_ns=MS)
        self.assertFalse(client.refresh_admission())

        fresh = self.client()
        self.owner.answer(self.owner.status(observed=deadline - GRANT_TICKS, lease=deadline), rtt_ns=MS)
        self.assertTrue(fresh.refresh_admission(), "a healthy lease may end exactly at the original deadline")

    def test_nonlease_observation_roundtrip_preserves_only_original_unexpired_grant(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())

        self.clock.value = NS + 300 * MS
        self.status_ok(observed=self.fixture.at(11), lease=self.fixture.at(11) + 50_000_000)  # owner shows 5 s
        client.request(M6OwnerControl(kind="status"))
        self.clock.value = NS + 480 * MS
        self.assertTrue(client.admission_allowed(), "the original grant is preserved")
        self.clock.value = NS + 490 * MS
        self.assertFalse(client.admission_allowed(), "a non-lease exchange never extends it")

        self.clock.value = NS + 600 * MS
        self.status_ok(observed=self.fixture.at(12), lease=self.fixture.at(12) + 50_000_000)
        client.request(M6OwnerControl(kind="status"))
        self.assertFalse(client.admission_allowed())

        event = self.admitted()
        record = self.owner.stamp(event, sequence=2, tick=self.fixture.at(13))  # type: ignore[arg-type]
        self.clock.value = 2 * NS
        self.lease(observed=self.fixture.at(13), last_sequence=2)
        self.assertTrue(client.refresh_admission())
        self.owner.answer(self.owner.status(observed=self.fixture.at(14), last_sequence=2,
                                            lease=self.fixture.at(14) + 50_000_000), record=record)
        client.append(event)  # type: ignore[arg-type]
        self.clock.value = 2 * NS + 480 * MS
        self.assertTrue(client.admission_allowed())
        self.clock.value = 2 * NS + 490 * MS
        self.assertFalse(client.admission_allowed(), "an append reply cannot extend the grant either")


class ControlStateTests(ClientCase):
    def test_stop_is_irreversible_and_cannot_reopen_from_a_later_response(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())

        self.owner.answer(self.owner.status(observed=self.fixture.at(11), lease=self.fixture.at(11) + GRANT_TICKS))
        client.request(M6OwnerControl(kind="stop"))  # owner still says open: the local guard is already shut
        self.assertFalse(client.admission_allowed())
        self.lease(observed=self.fixture.at(12))
        self.assertFalse(client.refresh_admission(), "no later lease reopens a stopped runner")

        client = self.client()
        self.lease()
        self.assertTrue(client.refresh_admission())
        self.status_ok(observed=self.fixture.at(11), state="stopping")
        client.request(M6OwnerControl(kind="stop"))
        self.assertFalse(client.admission_allowed())
        self.lease(observed=self.fixture.at(12))
        with self.assertRaises(M6OwnerProtocolError):
            client.refresh_admission()  # an "open" status after "stopping" is a state regression
        self.status_ok(observed=self.fixture.at(13), state="stopping")
        self.assertFalse(client.refresh_admission())

    def test_counter_and_owner_sequence_regressions_fail_closed(self) -> None:
        client = self.client()
        self.status_ok(observed=self.fixture.at(10), last_sequence=5)
        client.request(M6OwnerControl(kind="status"))
        self.status_ok(observed=self.fixture.at(10), last_sequence=5)
        client.request(M6OwnerControl(kind="status"))  # equal values are not a regression
        self.status_ok(observed=self.fixture.at(9), last_sequence=5)
        with self.assertRaises(M6OwnerProtocolError):
            client.request(M6OwnerControl(kind="status"))
        self.status_ok(observed=self.fixture.at(11), last_sequence=4)
        with self.assertRaises(M6OwnerProtocolError):
            client.request(M6OwnerControl(kind="status"))
        self.status_ok(observed=self.spec.t0_ticks - 1)
        with self.assertRaises(M6OwnerProtocolError):
            self.client().request(M6OwnerControl(kind="status"))

        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())
        self.status_ok(observed=self.spec.deadline_ticks)
        client.request(M6OwnerControl(kind="status"))
        self.assertFalse(client.admission_allowed(), "the owner reaching its deadline closes local admission")

    def test_failed_run_can_acknowledge_native_cleanup_and_close_without_state_regression(self) -> None:
        controller = self.client(role="controller", epoch=m6.digest("controller-epoch"))
        self.status_ok(state="failed")
        controller.request(M6OwnerControl(kind="status"))
        self.status_ok(observed=self.fixture.at(11), state="failed")
        controller.request(M6CloseOwner(ownership_receipt_sha256=m6.digest("ownership"), residual_count=0,
                                        children_exited=True, reason="failed"))
        self.status_ok(observed=self.fixture.at(12), state="closed")
        reply = controller.request(M6OwnerControl(kind="status"))
        self.assertEqual(reply.status.state, "closed")
        self.status_ok(observed=self.fixture.at(13), state="failed")
        with self.assertRaises(M6OwnerProtocolError):
            controller.request(M6OwnerControl(kind="status"))
        self.status_ok(observed=self.fixture.at(14), state="draining")
        with self.assertRaises(M6OwnerProtocolError):
            controller.request(M6OwnerControl(kind="status"))

        runner = self.client()
        self.status_ok(state="failed")
        self.assertFalse(runner.refresh_admission(), "closure confirms nothing about credit")
        self.assertFalse(runner.admission_allowed())

    def test_close_revokes_lease_and_only_claims_local_transport_exit(self) -> None:
        client = self.client()
        self.clock.value = NS
        self.lease()
        self.assertTrue(client.refresh_admission())
        client.close()
        self.assertEqual(self.owner.close_calls, 1)
        with self.assertRaises(RuntimeError):
            client.admission_allowed()
        with self.assertRaises(RuntimeError):
            client.request(M6OwnerControl(kind="status"))
        client.close()
        self.assertEqual(self.owner.close_calls, 1, "closing twice is idempotent")
        self.assertEqual(len(self.owner.requests), 1, "close sends no owner command; it is a local exit only")

        failing = ScriptedOwner(self.spec, self.anchor, self.clock)
        failing.close_error = OSError("synthetic channel close failure")
        client = owner_support.client_for(failing)
        with self.assertRaises(OSError):
            client.close()
        self.assertFalse(client.admission_allowed(), "admission is revoked even when local closure failed")

    def test_control_roles_bind_and_ack_incarnation_are_explicit(self) -> None:
        ack = M6AdmissionClosedAck(runner_epoch_sha256=m6.RUNNER_EPOCH, last_producer_sequence=3,
                                   admitted_attempt_count=2, unresolved_claim_count=0,
                                   reconciliation_receipt_sha256=m6.digest("reconciliation"))
        close = M6CloseOwner(ownership_receipt_sha256=m6.digest("ownership"), residual_count=0,
                             children_exited=True, reason="deadline_drained")
        forbidden = {
            ("controller", m6.digest("controller-epoch")): [M6OwnerControl(kind="lease"), ack,
                                                            M6AppendObservation(event=self.admitted())],  # type: ignore[arg-type]
            ("e2e_runner", m6.RUNNER_EPOCH): [M6BindOwner(anchor_sha256=self.anchor.canonical_sha256()),
                                              M6OwnerControl(kind="open"), close],
            ("service_runner", m6.RUNNER_EPOCH): [M6OwnerControl(kind="lease"), M6OwnerControl(kind="stop"), ack,
                                                  M6AppendObservation(event=self.admitted(kind="service_runner"))],  # type: ignore[arg-type]
            ("public_verifier", m6.PUBLIC_EPOCH): [M6OwnerControl(kind="lease"), M6OwnerControl(kind="stop"), ack,
                                                   M6OwnerControl(kind="open"), close],
            ("quality_verifier", m6.QUALITY_EPOCH): [M6OwnerControl(kind="lease"), M6OwnerControl(kind="stop"), ack,
                                                     M6BindOwner(anchor_sha256=self.anchor.canonical_sha256())],
        }
        for (role, epoch), commands in forbidden.items():
            client = self.client(role=role, epoch=epoch)
            for command in commands:
                with self.assertRaises(M6OwnerProtocolError, msg=role + "/" + command.kind):
                    client.request(command)
        self.assertEqual(self.owner.requests, [], "role violations never reach the wire")

        runner = self.client()
        with self.assertRaises(M6OwnerProtocolError):
            runner.request(ack.model_copy(update={"runner_epoch_sha256": m6.RUNNER_EPOCH_2}))
        with self.assertRaises(M6OwnerProtocolError):
            runner.append(self.admitted(kind="public_verifier", epoch=m6.PUBLIC_EPOCH))  # type: ignore[arg-type]
        with self.assertRaises(M6OwnerProtocolError):
            runner.append(self.admitted(epoch=m6.RUNNER_EPOCH_2))  # type: ignore[arg-type]
        self.assertEqual(self.owner.requests, [])

        self.status_ok(state="draining")
        runner.request(ack)
        self.assertIsInstance(self.owner.requests[-1].command, M6AdmissionClosedAck)
        self.assertFalse(runner.admission_allowed())
        for role, epoch in (("public_verifier", m6.PUBLIC_EPOCH), ("quality_verifier", m6.QUALITY_EPOCH),
                            ("controller", m6.digest("controller-epoch")), ("e2e_runner", m6.RUNNER_EPOCH)):
            self.status_ok(observed=self.fixture.at(11), state="draining")
            self.client(role=role, epoch=epoch).request(M6OwnerControl(kind="status"))
        self.assertEqual(self.owner.requests[-1].command.kind, "status")

        service_mode = m6.make_fixture("service_diagnostic", {"s": (2, "fresh")})
        service_owner = ScriptedOwner(service_mode.spec, owner_support.anchor_for(service_mode.spec), self.clock)
        with self.assertRaises(M6OwnerProtocolError):
            owner_support.client_for(service_owner, role="e2e_runner").request(M6OwnerControl(kind="lease"))
        service_owner.answer(service_owner.status(observed=service_mode.at(10), state="bound"))
        owner_support.client_for(service_owner, role="service_runner").request(M6OwnerControl(kind="lease"))
        self.assertEqual(len(service_owner.requests), 1, "the selected runner role follows the run mode")

    def test_pinned_ssh_is_lazy_and_its_actual_session_is_closed(self) -> None:
        sessions: list[object] = []
        reply = owner_support.synthetic_reply_line()

        class FakeSession:
            def __init__(self, config: object, *, remote_port: int, timeout: float) -> None:
                self.config, self.remote_port, self.timeout = config, remote_port, timeout
                self.channels: list[FakeChannel] = []
                self.close_calls = 0
                sessions.append(self)

            def open_channel(self, remaining: float) -> FakeChannel:
                channel = FakeChannel([reply + b"\n", reply + b"\n"])
                self.channels.append(channel)
                return channel

            def close(self) -> None:
                self.close_calls += 1

        with tempfile.TemporaryDirectory() as root:
            token_path = Path(root) / "owner-token"
            token_path.write_text(SYNTHETIC_TOKEN + "\n")
            bad_token = Path(root) / "bad-token"
            bad_token.write_text("not-a-token\n")
            config = object()
            with mock.patch.object(m6_owner_ssh, "_Session", FakeSession), mock.patch.object(
                m6_owner_ssh, "_read_private_config", lambda path, secret=False: Path(path).read_text()
            ):
                with self.assertRaises(ValueError):
                    m6_owner_ssh.m6_ssh_owner_transport(config=config, token_path="relative/token", remote_port=4444,  # type: ignore[arg-type]
                                                        continuous_ns=self.clock)
                with self.assertRaises(ValueError):
                    m6_owner_ssh.m6_ssh_owner_transport(config=config, token_path=str(bad_token), remote_port=4444,  # type: ignore[arg-type]
                                                        continuous_ns=self.clock)
                for port in (0, 1023, 65536):
                    with self.assertRaises(ValueError, msg=str(port)):
                        m6_owner_ssh.m6_ssh_owner_transport(config=config, token_path=str(token_path), remote_port=port,  # type: ignore[arg-type]
                                                            continuous_ns=self.clock)
                transport = m6_owner_ssh.m6_ssh_owner_transport(config=config, token_path=str(token_path),  # type: ignore[arg-type]
                                                                remote_port=4444, continuous_ns=self.clock)
                self.assertEqual(sessions, [], "no SSH session before the first exchange")
                request = b'{"synthetic":"request"}'
                self.assertEqual(transport.exchange(request), reply)
                self.assertEqual(len(sessions), 1)
                session = sessions[0]
                self.assertEqual((session.config, session.remote_port), (config, 4444))  # type: ignore[attr-defined]
                self.assertEqual(len(session.channels), 1)  # type: ignore[attr-defined]
                self.assertEqual(session.channels[0].sent, [b"M6-AUTH/1 " + SYNTHETIC_TOKEN.encode() + b"\n" + request + b"\n"])  # type: ignore[attr-defined]
                self.assertEqual(transport.exchange(request), reply)
                self.assertEqual((len(sessions), len(session.channels)), (1, 1), "one session, one persistent channel")  # type: ignore[attr-defined]
                transport.close()
                self.assertEqual(session.channels[0].close_calls, 1)  # type: ignore[attr-defined]
                self.assertEqual(session.close_calls, 1, "the actual pinned session is closed")  # type: ignore[attr-defined]
                with self.assertRaises(RuntimeError):
                    transport.exchange(request)


class ObservationStampTests(ClientCase):
    def test_exact_observation_retry_returns_old_stamp_and_conflict_retains_evidence(self) -> None:
        client = self.client()
        event = self.admitted()
        record = self.owner.stamp(event, sequence=7, tick=self.fixture.at(10))  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(10), last_sequence=7), record=record)
        first = client.append(event)  # type: ignore[arg-type]
        self.assertEqual(first, record)
        self.assertEqual(first.stamp.producer_event_sha256, event.canonical_sha256())  # type: ignore[attr-defined]

        self.owner.answer(self.owner.status(observed=self.fixture.at(11), last_sequence=7), record=record)
        second = client.append(event)  # type: ignore[arg-type]
        self.assertEqual(second, first, "an exact retry returns the original durable stamp")
        self.assertNotEqual(self.owner.requests[-1].request_id, self.owner.requests[-2].request_id)
        self.assertEqual(self.owner.requests[-1].command.event, self.owner.requests[-2].command.event)  # type: ignore[union-attr]

        changed = self.admitted(fence="fence-2")  # same producer key, different bytes
        conflict_record = self.owner.stamp(changed, sequence=8, tick=self.fixture.at(12))  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(12), last_sequence=8), outcome="conflict",
                          record=conflict_record, error_code="producer_conflict")
        with self.assertRaises(M6OwnerRejected) as rejected:
            client.append(changed)  # type: ignore[arg-type]
        self.assertEqual(rejected.exception.reply.outcome, "conflict")
        self.assertEqual(rejected.exception.reply.record, conflict_record, "the conflict stamp is the submitted bytes")
        self.assertEqual(rejected.exception.reply.record.stamp.producer_event_sha256, changed.canonical_sha256())  # type: ignore[union-attr,attr-defined]
        self.assertNotIn(SYNTHETIC_TOKEN, str(rejected.exception))

        self.owner.answer(self.owner.status(observed=self.fixture.at(13), last_sequence=8), outcome="conflict",
                          record=record, error_code="producer_conflict")
        with self.assertRaises(M6OwnerProtocolError):
            client.append(changed)  # type: ignore[arg-type]  # a conflict carrying the predecessor stamp is untrusted

        self.owner.answer(self.owner.status(observed=self.fixture.at(14), last_sequence=8), outcome="rejected",
                          error_code="admission_closed")
        with self.assertRaises(M6OwnerRejected) as refused:
            client.append(self.admitted(sequence=2))  # type: ignore[arg-type]
        self.assertIsNone(refused.exception.reply.record)
        self.assertEqual(refused.exception.reply.error_code, "admission_closed")
        self.assertFalse(client.admission_allowed(), "any rejection closes the local admission guard")

    def test_wrong_stamp_boot_bytes_or_producer_never_acknowledged(self) -> None:
        client = self.client()
        event = self.admitted()
        good = self.owner.stamp(event, sequence=3, tick=self.fixture.at(10))  # type: ignore[arg-type]
        other_event = self.admitted(fence="fence-9")
        cases = {
            "other_boot": self.owner.stamp(event, sequence=3, tick=self.fixture.at(10), boot=m6.digest("boot:boot-b")),  # type: ignore[arg-type]
            "unknown_owner_incarnation": self.owner.stamp(event, sequence=3, tick=self.fixture.at(10), owner_epoch=m6.OWNER_EPOCH_2),  # type: ignore[arg-type]
            "different_bytes": self.owner.stamp(other_event, sequence=3, tick=self.fixture.at(10)),  # type: ignore[arg-type]
            "no_record": None,
        }
        for label, record in cases.items():
            self.owner.answer(self.owner.status(observed=self.fixture.at(10), last_sequence=3), record=record)
            with self.assertRaises(M6OwnerProtocolError, msg=label):
                client.append(event)  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(10), last_sequence=3), record=good)
        self.assertEqual(client.append(event), good)  # type: ignore[arg-type]

        # A status exchange that returns someone else's stamped record is equally untrusted.
        self.owner.answer(self.owner.status(observed=self.fixture.at(11), last_sequence=4),
                          record=self.owner.stamp(other_event, sequence=4, tick=self.fixture.at(11), boot=m6.digest("boot:boot-b")))  # type: ignore[arg-type]
        with self.assertRaises(M6OwnerProtocolError):
            client.request(M6OwnerControl(kind="status"))

    def test_rejected_cannot_conceal_a_stamped_record_and_pre_t0_stamp_is_rejected(self) -> None:
        client = self.client()
        event = self.admitted()
        record = self.owner.stamp(event, sequence=3, tick=self.fixture.at(10))  # type: ignore[arg-type]
        status = self.owner.status(observed=self.fixture.at(10), last_sequence=3)

        def concealed(request: M6OwnerRequest) -> bytes:
            document = json.loads(self.owner.reply(request, status, record=record))
            document.update({"outcome": "rejected", "error_code": "refused"})
            return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

        self.owner.answer_raw(concealed)
        with self.assertRaises(ValueError) as caught:
            client.append(event)  # type: ignore[arg-type]
        self.assertNotIsInstance(caught.exception, M6OwnerRejected)

        def contradictory(request: M6OwnerRequest) -> bytes:
            document = json.loads(self.owner.reply(request, status, record=record))
            document.update({"outcome": "ok", "error_code": "refused"})
            return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

        self.owner.answer_raw(contradictory)
        with self.assertRaises(ValueError):
            client.append(event)  # type: ignore[arg-type]

        early = self.owner.stamp(event, sequence=3, tick=self.spec.t0_ticks - 1)  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(10), last_sequence=3), record=early)
        with self.assertRaises(M6OwnerProtocolError):
            client.append(event)  # type: ignore[arg-type]
        at_t0 = self.owner.stamp(event, sequence=3, tick=self.spec.t0_ticks)  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(10), last_sequence=3), record=at_t0)
        self.assertEqual(client.append(event), at_t0)  # type: ignore[arg-type]

        # The model itself refuses a stamp ahead of the owner's own status.
        with self.assertRaises(ValueError):
            self.owner.reply(self.owner.requests[-1], self.owner.status(observed=self.fixture.at(10), last_sequence=2),
                             record=at_t0)
        with self.assertRaises(ValueError):
            self.owner.reply(self.owner.requests[-1], self.owner.status(observed=self.spec.t0_ticks - 1, last_sequence=3),
                             record=at_t0)


class RecoveryChainTests(ClientCase):
    def test_explicit_resume_chain_allows_old_stamp_retry_and_never_reopens_admission(self) -> None:
        chain = (self.resume_record(),)
        self.owner.owner_epoch = m6.OWNER_EPOCH_2
        client = self.client(recovery_chain=chain)
        self.assertFalse(client.admission_allowed())

        self.owner.answer(self.owner.status(observed=self.fixture.at(21), state="draining", last_sequence=5))
        client.request(M6OwnerControl(kind="status"))

        event = self.admitted()
        old_stamp = self.owner.stamp(event, sequence=3, tick=self.fixture.at(10), owner_epoch=m6.OWNER_EPOCH)  # type: ignore[arg-type]
        self.owner.answer(self.owner.status(observed=self.fixture.at(22), state="draining", last_sequence=6), record=old_stamp)
        self.assertEqual(client.append(event), old_stamp, "an exact retry may return the predecessor's stamp")  # type: ignore[arg-type]

        self.owner.answer(self.owner.status(observed=self.fixture.at(23), state="draining", last_sequence=6))
        self.assertFalse(client.refresh_admission())
        self.owner.answer(self.owner.status(observed=self.fixture.at(24), state="open", last_sequence=6,
                                            lease=self.fixture.at(24) + GRANT_TICKS))
        with self.assertRaises(M6OwnerProtocolError):
            client.refresh_admission()  # a recovered owner claiming "open" is a regression
        self.assertFalse(client.admission_allowed())

        self.owner.answer(self.owner.status(observed=self.fixture.at(25), state="draining", last_sequence=6,
                                            epoch=m6.OWNER_EPOCH))
        with self.assertRaises(M6OwnerProtocolError):
            client.request(M6OwnerControl(kind="status"))  # the predecessor incarnation is no longer accepted

        plain = ScriptedOwner(self.spec, self.anchor, self.clock, owner_epoch=m6.OWNER_EPOCH_2)
        unrecovered = owner_support.client_for(plain)
        plain.answer(plain.status(observed=self.fixture.at(21), state="draining"))
        with self.assertRaises(M6OwnerProtocolError):
            unrecovered.request(M6OwnerControl(kind="status"))  # status alone never changes the expected owner

    def test_resume_proof_rejects_wrong_boot_missing_predecessor_repeated_epoch_and_interval_drift(self) -> None:
        good = self.resume_record()
        self.owner.owner_epoch = m6.OWNER_EPOCH_2
        self.client(recovery_chain=(good,))

        rejected = {
            "wrong_boot": self.owner.stamp(good.event, sequence=5, tick=self.fixture.at(20), owner_epoch=m6.OWNER_EPOCH_2,
                                           boot=m6.digest("boot:boot-b")),
            "missing_predecessor": self.resume_record(previous=m6.digest("owner-epoch:unknown")),
            "repeated_epoch": self.resume_record(new_epoch=m6.OWNER_EPOCH, previous=m6.OWNER_EPOCH),
            "t0_drift": self.resume_record(t0_ticks=self.spec.t0_ticks + 1),
            "deadline_drift": self.resume_record(deadline_ticks=self.spec.deadline_ticks + m6.QPC_HZ),
            "other_clock": self.resume_record(clock=m6.clock_domain("boot-b")),
            "not_owner_producer": self.resume_record(kind="e2e_runner"),
            "stamp_before_t0": self.resume_record(tick=self.spec.t0_ticks - 1),
            "other_run": self.owner.stamp(good.event.model_copy(update={"run_id": "run-other"}), sequence=5,
                                          tick=self.fixture.at(20), owner_epoch=m6.OWNER_EPOCH_2),
            "stamp_by_predecessor": self.owner.stamp(good.event, sequence=5, tick=self.fixture.at(20),
                                                     owner_epoch=m6.OWNER_EPOCH),
        }
        for label, record in rejected.items():
            with self.assertRaises(M6OwnerProtocolError, msg=label):
                self.client(recovery_chain=(record,))

        third = m6.digest("owner-epoch-3")
        second_hop = self.resume_record(new_epoch=third, previous=m6.OWNER_EPOCH_2, sequence=9, tick=self.fixture.at(30))
        self.owner.owner_epoch = third
        self.client(recovery_chain=(good, second_hop))
        for label, bad_second in {
            "sequence_not_increasing": self.resume_record(new_epoch=third, previous=m6.OWNER_EPOCH_2, sequence=5,
                                                          tick=self.fixture.at(30)),
            "tick_regressed": self.resume_record(new_epoch=third, previous=m6.OWNER_EPOCH_2, sequence=9,
                                                 tick=self.fixture.at(19)),
            "skips_predecessor": self.resume_record(new_epoch=third, previous=m6.OWNER_EPOCH, sequence=9,
                                                    tick=self.fixture.at(30)),
            "returns_to_original": self.resume_record(new_epoch=m6.OWNER_EPOCH, previous=m6.OWNER_EPOCH_2, sequence=9,
                                                      tick=self.fixture.at(30)),
        }.items():
            with self.assertRaises(M6OwnerProtocolError, msg=label):
                self.client(recovery_chain=(good, bad_second))

        with self.assertRaises(TypeError):
            self.client(recovery_chain=(good, good.event))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.client(recovery_chain=(good,) * 9)
        with self.assertRaises(ValueError):
            self.client(recovery_chain=[good])  # type: ignore[arg-type]
        self.assertEqual(self.owner.requests, [])


class LineTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(NS)
        self.reply = owner_support.synthetic_reply_line()
        self.request = b'{"synthetic":"request"}'

    def transport(self, opener: FakeOpener, **kwargs: object) -> M6LineOwnerTransport:
        return M6LineOwnerTransport(token=SYNTHETIC_TOKEN, open_channel=opener, close_session=opener.close_session,
                                    continuous_ns=self.clock, **kwargs)  # type: ignore[arg-type]

    def test_actual_stream_preserves_lf_requests_and_reuses_channel_without_secret_in_reply(self) -> None:
        channel = FakeChannel([self.reply[:5], self.reply[5:] + b"\n", self.reply + b"\n"])
        opener = FakeOpener([channel])
        transport = self.transport(opener)
        self.assertEqual(opener.timeouts, [], "the channel opens lazily")
        self.assertEqual(transport.exchange(self.request), self.reply)
        self.assertEqual(channel.sent, [b"M6-AUTH/1 " + SYNTHETIC_TOKEN.encode() + b"\n" + self.request + b"\n"])
        self.assertEqual(transport.exchange(self.request), self.reply)
        self.assertEqual(len(opener.timeouts), 1, "the persistent channel is reused")
        self.assertEqual(len(channel.sent), 2)
        self.assertTrue(all(0 < timeout <= 5 for timeout in channel.timeouts))
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), self.reply)
        self.assertEqual(channel.close_calls, 0)

        for label, bad in {"lf": b'{"a":"\n"}', "cr": b"{\r}", "empty": b"", "oversize": b"x" * 65537,
                           "text": "not bytes"}.items():
            with self.assertRaises(ValueError, msg=label):
                transport.exchange(bad)  # type: ignore[arg-type]
        self.assertEqual(len(channel.sent), 2, "framing violations never reach the channel")

        exact = FakeChannel([b"y" * 4096 + b"\n"])
        bounded = self.transport(FakeOpener([exact]), maximum_wire_bytes=4096)
        self.assertEqual(bounded.exchange(self.request), b"y" * 4096, "a reply of exactly the bound plus LF is accepted")

        for label, kwargs in {"token_short": {"token": "abc"}, "token_upper": {"token": SYNTHETIC_TOKEN.upper()},
                              "timeout_zero": {"timeout_ns": 0}, "timeout_long": {"timeout_ns": 30 * NS + 1},
                              "bound_low": {"maximum_wire_bytes": 4095}, "bound_high": {"maximum_wire_bytes": 65537}}.items():
            values: dict[str, object] = {"token": SYNTHETIC_TOKEN, "open_channel": opener,
                                         "close_session": opener.close_session, "continuous_ns": self.clock}
            values.update(kwargs)
            with self.assertRaises(ValueError, msg=label):
                M6LineOwnerTransport(**values)  # type: ignore[arg-type]

        transport.close()
        self.assertEqual((channel.close_calls, opener.session_closes), (1, 1))
        transport.close()
        self.assertEqual((channel.close_calls, opener.session_closes), (1, 1))
        with self.assertRaises(RuntimeError):
            transport.exchange(self.request)

    def test_eof_oversize_crlf_trailing_line_and_deadline_close_the_channel(self) -> None:
        def slow_recv() -> None:
            self.clock.advance(6 * NS)

        def regress() -> None:
            self.clock.value -= 1

        cases: dict[str, tuple[FakeChannel, type[BaseException]]] = {
            "eof": (FakeChannel([]), EOFError),
            "eof_after_partial": (FakeChannel([self.reply[:3]]), EOFError),
            "oversize_without_lf": (FakeChannel([b"z" * 70_000]), M6OwnerProtocolError),
            "oversize_before_lf": (FakeChannel([b"z" * 65_537 + b"\n"]), M6OwnerProtocolError),
            "crlf": (FakeChannel([self.reply + b"\r\n"]), M6OwnerProtocolError),
            "trailing_after_lf": (FakeChannel([self.reply + b"\nmore"]), M6OwnerProtocolError),
            "two_lines": (FakeChannel([b"a\nb\n"]), M6OwnerProtocolError),
            "deadline": (FakeChannel([self.reply + b"\n"], on_recv=slow_recv), TimeoutError),
            "clock_regression": (FakeChannel([self.reply + b"\n"], on_recv=regress), ValueError),
        }
        for label, (channel, error) in cases.items():
            self.clock.value = NS
            follow_up = FakeChannel([self.reply + b"\n"])
            opener = FakeOpener([channel, follow_up])
            transport = self.transport(opener)
            with self.assertRaises(error, msg=label):
                transport.exchange(self.request)
            self.assertEqual(channel.close_calls, 1, label + ": the channel is closed after the failure")
            self.assertEqual(len(channel.sent), 1, label)
            self.clock.value = 2 * NS
            self.assertEqual(transport.exchange(self.request), self.reply, label + ": a new channel is opened")
            self.assertEqual(len(opener.timeouts), 2, label)
            self.assertEqual(channel.sent, channel.sent[:1], label + ": the failed channel is never reused")

    def test_failed_close_is_retained_for_cleanup_and_channel_is_never_reused(self) -> None:
        first = FakeChannel([])
        first.close_errors = [OSError("synthetic close failure")]
        second = FakeChannel([self.reply + b"\n"])
        opener = FakeOpener([first, second])
        transport = self.transport(opener)
        with self.assertRaises(BaseExceptionGroup) as group:
            transport.exchange(self.request)
        self.assertEqual({type(exc) for exc in group.exception.exceptions}, {EOFError, OSError})
        self.assertNotIn(SYNTHETIC_TOKEN, str(group.exception))
        self.assertEqual(first.close_calls, 1)

        self.assertEqual(transport.exchange(self.request), self.reply)
        self.assertEqual(first.close_calls, 2, "cleanup of the retained handle is retried before opening anew")
        self.assertEqual(len(first.sent), 1, "the failed channel never carries another request")
        self.assertEqual(len(second.sent), 1)

        third = FakeChannel([self.reply + b"\n"])
        third.close_errors = [OSError("synthetic close failure")]
        fourth = FakeChannel([self.reply + b"\n"])
        opener = FakeOpener([third, fourth])
        transport = self.transport(opener)
        self.assertEqual(transport.exchange(self.request), self.reply)
        with self.assertRaises(BaseExceptionGroup):
            transport.close()
        self.assertEqual(opener.session_closes, 1, "session closure is still attempted")
        self.assertEqual(transport.exchange(self.request), self.reply, "not closed: the handle stays owned")
        self.assertEqual((third.close_calls, len(third.sent), len(fourth.sent)), (2, 1, 1))
        transport.close()
        self.assertEqual((fourth.close_calls, opener.session_closes), (1, 2))

        fifth = FakeChannel([self.reply + b"\n"])
        opener = FakeOpener([fifth])
        opener.session_close_error = OSError("synthetic session close failure")
        transport = self.transport(opener)
        transport.exchange(self.request)
        with self.assertRaises(BaseExceptionGroup):
            transport.close()
        self.assertEqual(fifth.close_calls, 1)
        with self.assertRaises(BaseExceptionGroup):
            transport.close()  # still not verified closed; no silent success


if __name__ == "__main__":
    unittest.main()
