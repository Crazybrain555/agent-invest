"""The Mac side of a storage-managed runtime: wire envelope, poll and grants.

Synthetic HTTP payloads and in-memory evidence only; no network or database.
"""

from __future__ import annotations

from dataclasses import replace
import unittest

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4 as RealRemote,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MinerUProtocolV2WireError,
    parse_task_payload_with_failure_cause_v2,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    TERMINAL_RECEIPT_V4_CONTRACT,
    TERMINAL_RECEIPT_V5_CONTRACT,
    TerminalResultStorageV1,
    decode_remote_parse_evidence_v4,
    effective_resource_reservation_v4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    build_stage_resource_grant_v1,
    materialization_grant_limits_v4,
    terminal_result_growth_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderCompletedV4,
    RemoteProviderFailedV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderWaitingV4,
    StorageHoldDecisionV1,
)
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
)
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures

POLICY = "sha256:" + "f" * 64
INVENTORY = "sha256:" + "e" * 64
ORIGIN = "http://mineru.invalid"


def storage_status(phase: str, **changes: object) -> dict[str, object]:
    sealed = phase in {"source_sealed", "zip_writing", "zip_sealed"}
    value: dict[str, object] = {
        "schema": "mineru.task-storage-status.v1",
        "policy_sha256": POLICY,
        "phase": phase,
        "wait_reason": None,
        "wait_since_unix": None,
        "blocked": False,
        "selected_bytes": 900 if sealed else 0,
        "member_count": 3 if sealed else 0,
        "inventory_sha256": INVENTORY if sealed else None,
        "zip_bytes": 123 if phase == "zip_sealed" else 0,
    }
    value.update(changes)
    return value


class WireStorageEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)

    def parse(self, payload: dict[str, object], *, bound: bool = True):  # type: ignore[no-untyped-def]
        import json
        return parse_task_payload_with_failure_cause_v2(
            json.dumps(payload).encode(), api_origin=ORIGIN, idempotency_key=self.wire.key,
            attempt_identity="attempt-1", fence_identity="fence-1",
            storage_policy_sha256=POLICY if bound else None,
        )[0]

    def test_only_a_bound_reader_accepts_the_storage_envelope_and_then_requires_it(self) -> None:
        payload = {**self.wire._task_payload("completed", 123), "storage": storage_status("zip_sealed")}
        observation = self.parse(payload)
        assert observation.storage is not None
        self.assertEqual((observation.storage.selected_bytes, observation.storage.member_count), (900, 3))
        with self.assertRaises(MinerUProtocolV2WireError):
            self.parse(payload, bound=False)  # An old reader stays closed.
        with self.assertRaises(MinerUProtocolV2WireError):
            self.parse(self.wire._task_payload("completed", 123))  # A bound reader requires it.

    def test_envelope_must_agree_with_task_state_policy_and_result(self) -> None:
        completed = self.wire._task_payload("completed", 123)
        for label, storage in (
            ("policy", storage_status("zip_sealed", policy_sha256="sha256:" + "a" * 64)),
            ("zip extent", storage_status("zip_sealed", zip_bytes=124)),
            ("unsealed completion", storage_status("source_sealed")),
            ("block flag", storage_status("zip_sealed", wait_reason="free_floor", wait_since_unix=1.0,
                                           blocked=True)),
            ("closed fields", {**storage_status("zip_sealed"), "extra": 1}),
        ):
            with self.subTest(label), self.assertRaises(MinerUProtocolV2WireError):
                self.parse({**completed, "storage": storage})
        held = self.parse({
            **self.wire._task_payload("processing"), "protocol_state": "processing",
            "storage": storage_status("source_growing", wait_reason="hard_envelope_exceeded",
                                      wait_since_unix=2.0, blocked=True),
        })
        assert held.storage is not None
        self.assertTrue(held.storage.blocked)
        with self.assertRaises(MinerUProtocolV2WireError):
            self.parse({
                **self.wire._task_payload("pending"),
                "storage": storage_status("admitted", wait_reason="seal_integrity",
                                          wait_since_unix=2.0, blocked=True),
            })


class StorageBoundPollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)

    def remote(self, handler):  # type: ignore[no-untyped-def]
        return RealRemote(
            transport=httpx.MockTransport(handler), wall_clock=lambda: 10_000.0,
            request_timeout_seconds=30.0, result_storage_policy_sha256=POLICY,
        )

    def test_completed_storage_result_becomes_a_v5_terminal_receipt(self) -> None:
        accepted = self.wire._accepted()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={
                    **self.wire._task_payload("completed", 123), "storage": storage_status("zip_sealed"),
                })
            return httpx.Response(200, json={
                "schema": "mineru-task-protocol.v2", "task_id": "task-1", "lease_until_unix": 10_000.1,
            })

        with self.remote(handler) as provider:
            outcome = provider.poll_once(self.wire._poll_command(accepted, byte_limit=10_000))
        assert isinstance(outcome, RemoteProviderCompletedV4)
        terminal = outcome.receipt
        self.assertEqual(terminal.contract_version, TERMINAL_RECEIPT_V5_CONTRACT)
        self.assertEqual(
            terminal.result_storage,
            TerminalResultStorageV1(policy_sha256=POLICY, selected_bytes=900, member_count=3,
                                    inventory_sha256=INVENTORY),
        )
        encoded = encode_remote_parse_evidence_v4(terminal)
        self.assertEqual(decode_remote_parse_evidence_v4("terminal_receipt", encoded.exact_bytes).value, terminal)
        legacy = replace(terminal, contract_version=TERMINAL_RECEIPT_V4_CONTRACT, result_storage=None)
        self.assertNotIn(b"result_storage", legacy.canonical_bytes)

    def test_storage_wait_and_hold_are_visible_waiting_outcomes(self) -> None:
        accepted = self.wire._accepted()
        for storage, blocked in (
            (storage_status("admitted", wait_reason="source_growth_capacity", wait_since_unix=5.0), False),
            (storage_status("source_growing", wait_reason="hard_envelope_exceeded",
                            wait_since_unix=5.0, blocked=True), True),
        ):
            status = "pending" if storage["phase"] == "admitted" else "processing"

            def handler(request: httpx.Request, storage=storage, status=status) -> httpx.Response:
                return httpx.Response(200, json={
                    **self.wire._task_payload(status), "protocol_state": status, "storage": storage,
                })

            with self.subTest(blocked=blocked), self.remote(handler) as provider:
                outcome = provider.poll_once(self.wire._poll_command(accepted))
            assert isinstance(outcome, RemoteProviderWaitingV4)
            self.assertEqual(outcome.storage_wait_reason, storage["wait_reason"])
            self.assertIs(outcome.storage_blocked, blocked)
        # The port admits a hold only for the closed native hold reasons, and
        # never lets a hold reason pass as an ordinary wait.
        for reason, blocked in (("free_floor", True), ("hard_envelope_exceeded", False)):
            with self.subTest(reason=reason, blocked=blocked), self.assertRaises(ValueError):
                RemoteProviderWaitingV4(
                    remote_task_identity="task-1", status="processing",
                    response_sha256="sha256:" + "1" * 64, response_byte_count=2,
                    storage_wait_reason=reason, storage_blocked=blocked,
                )


    def test_operator_terminated_hold_is_a_closed_terminal_failure(self) -> None:
        accepted = self.wire._accepted()
        decision = StorageHoldDecisionV1(
            preview_sha256="sha256:" + "d" * 64, decided_by="root", reason="the declared envelope stays",
            fixed_by="none",
        )
        cause = {
            "schema": "mineru-task-failure-cause.v1", "task_id": "task-1", "retry_class": "permanent",
            "code": "storage_hold_terminated", "http_status": None, "transport_error": None,
            "hold_reason": "hard_envelope_exceeded", "decision_sha256": decision.sha256,
            "decision": decision.payload(),
        }

        def poll(failure_cause):  # type: ignore[no-untyped-def]
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    **self.wire._task_payload("failed"), "error": "storage hold terminated by operator",
                    "failure_cause": failure_cause, "storage": storage_status("source_growing"),
                })

            with self.remote(handler) as provider:
                return provider.poll_once(self.wire._poll_command(accepted))

        outcome = poll(cause)
        assert isinstance(outcome, RemoteProviderFailedV4) and outcome.failure_cause is not None
        self.assertEqual(
            (outcome.failure_cause.retry_class, outcome.failure_cause.hold_reason,
             outcome.failure_cause.decision_sha256, outcome.failure_cause.decision),
            ("permanent", "hard_envelope_exceeded", cause["decision_sha256"], decision),
        )
        failure = DurableStagedCoordinatorBackendV4._provider_terminal_failure(outcome, accepted.receipt)
        self.assertEqual(
            (failure.error_code, failure.retry_budget_class, failure.retryable),
            ("provider_storage_hold_terminated", "provider_terminal", False),
        )
        # The Mac's durable failure keeps the digest and the exact attribution it binds.
        self.assertIn(f"{cause['decision_sha256']}:{decision.canonical_json}", str(failure))
        for label, broken in (
            ("open field", {**cause, "extra": 1}),
            ("not a hold reason", {**cause, "hold_reason": "free_floor"}),
            ("no decision digest", {key: value for key, value in cause.items() if key != "decision_sha256"}),
            ("digest only", {key: value for key, value in cause.items() if key != "decision"}),
            ("attribution edited under its digest", {**cause, "decision": {**cause["decision"], "reason": "x"}}),
            ("open decision fields", {**cause, "decision": {**cause["decision"], "note": "x"}}),
            ("decision on another code", {**cause, "code": "unclassified", "retry_class": "unknown"}),
        ):
            with self.subTest(label), self.assertRaises(RemoteProviderProtocolErrorV4):
                poll(broken)


class GrantArithmeticTests(unittest.TestCase):
    def test_effective_reservation_grows_only_through_verified_evidence(self) -> None:
        reserved = ResourceCreditVector(
            documents=1, snapshot_items=1, snapshot_bytes=10, remote_waits=1, provider_tasks=1,
            provider_result_bytes=100, materialization_items=1, compressed_bytes=100, decoded_bytes=400,
            temp_disk_bytes=500, output_items=1, output_bytes=400, output_pages=2, ack_items=1,
        )
        grown = terminal_result_growth_v4(reserved, artifact_byte_count=5_000)
        self.assertEqual((grown.provider_result_bytes, grown.compressed_bytes), (5_000, 5_000))
        self.assertEqual(terminal_result_growth_v4(reserved, artifact_byte_count=50), reserved)
        limits = materialization_grant_limits_v4(
            reserved, artifact_byte_count=5_000, selected_bytes=9_000, decode_working_set_bytes=700,
        )
        # Disk: spool Z + unpacked S + decode outputs W; RAM: W, never S.
        self.assertEqual(limits.temp_disk_bytes, 5_000 + 9_000 + 700)
        self.assertEqual(limits.output_bytes, 9_000 + 700)
        self.assertEqual(limits.decoded_bytes, 700)
        self.assertEqual(limits.output_pages, reserved.output_pages)

    def test_grant_allowance_binds_one_grant_and_only_raises_the_reservation(self) -> None:
        from tests.unit.test_remote_parse_evidence_v4 import _typed_happy_bundle

        _, reservation, values, *_ = _typed_happy_bundle()
        terminal = replace(
            values[4], contract_version=TERMINAL_RECEIPT_V5_CONTRACT,
            result_storage=TerminalResultStorageV1(
                policy_sha256=POLICY, selected_bytes=30, member_count=3, inventory_sha256=INVENTORY,
            ),
        )
        grant = build_stage_resource_grant_v1(
            reservation=reservation, terminal_receipt_sha256=terminal.sha256,
            storage_policy_sha256=POLICY, inventory_sha256=INVENTORY,
            artifact_byte_count=terminal.artifact_byte_count, selected_bytes=30, member_count=3,
            decode_working_set_bytes=40, decode_input_limit_bytes=30,
        )
        effective = effective_resource_reservation_v4(reservation, terminal=terminal, intent=None)
        self.assertTrue(reservation.reserved_credit.fits(effective))
        from disclosure_anchor.application.contracts.staged_resource_credit import (
            ResourceReservationInput,
            encode_resource_reservation_input,
        )
        encoded = encode_resource_reservation_input(ResourceReservationInput(
            source_pdf_sha256=reservation.source_pdf_sha256,
            source_byte_count=reservation.source_byte_count,
            source_page_count=reservation.source_page_count,
            process_profile_sha256=reservation.process_profile_sha256,
            credit_policy_sha256=reservation.credit_policy_sha256,
            bucket=reservation.reservation_bucket, reservation=reservation.reserved_credit,
        ))
        allowance = PerAttemptResourceAllowance(
            reservation_input_sha256=encoded.sha256, reservation_input=encoded,
            limits=grant.limits, stage_grant_sha256=grant.sha256,
        )
        self.assertIn(b"stage_grant_sha256", allowance.canonical_bytes)
        with self.assertRaises(ValueError):
            PerAttemptResourceAllowance(
                reservation_input_sha256=encoded.sha256, reservation_input=encoded,
                limits=grant.limits,  # A raised ceiling must name its grant.
            )
        shrunk = replace(grant.limits, output_pages=0)
        with self.assertRaises(ValueError):
            PerAttemptResourceAllowance(
                reservation_input_sha256=encoded.sha256, reservation_input=encoded,
                limits=shrunk, stage_grant_sha256=grant.sha256,
            )


if __name__ == "__main__":
    unittest.main()
