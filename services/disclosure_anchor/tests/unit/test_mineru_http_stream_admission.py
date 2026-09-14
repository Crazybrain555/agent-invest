"""Stream pressure gates new task POSTs without blocking owned HTTP work."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
import unittest

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4,
)
from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderCompletedV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
    RemoteProviderWaitingV4,
)
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures


class _AdmissionGuard:
    def __init__(self, *, blocked: bool, events: list[str]) -> None:
        self.blocked = blocked
        self.events = events
        self.identities: list[str] = []
        self.deferred = StreamSubmissionDeferred("stream pressure is unknown")

    def assert_submission_allowed(self, *, runtime_identity_sha256: str) -> None:
        self.events.append("admission-check")
        self.identities.append(runtime_identity_sha256)
        if self.blocked:
            raise self.deferred


class MinerUHttpStreamAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Compose existing sealed wire fixtures without inheriting their tests.
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)
        self.events: list[str] = []

    def _provider(
        self, handler: httpx.SyncHandler, admission: _AdmissionGuard,
    ) -> MinerUHttpRemoteV4:
        return MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"s" * count,
            wall_clock=lambda: 10_000.0,
            request_timeout_seconds=30.0,
            submission_guard=admission,
        )

    def test_proved_absence_deferred_before_post_is_not_unknown_submission(self) -> None:
        command = self.wire._submission_command()
        source = cast(wire_fixtures._SnapshotSource, command.snapshot_source)
        admission = _AdmissionGuard(blocked=True, events=self.events)

        def handler(request: httpx.Request) -> httpx.Response:
            self.events.append(f"{request.method} {request.url.path}")
            self.assertEqual(request.method, "GET")
            return httpx.Response(404, json={"detail": "Task not found"})

        with self._provider(handler, admission) as provider:
            with self.assertRaises(StreamSubmissionDeferred) as raised:
                provider.reconcile_or_submit(command)

        # The exact deferral survives; no ambiguous POST or reconciliation GET.
        self.assertIs(raised.exception, admission.deferred)
        self.assertEqual(self.events, [
            "GET /tasks/by-idempotency/" + self.wire.key, "admission-check",
        ])
        self.assertEqual(source.opens, 1)
        self.assertEqual(admission.identities, [
            command.parser_options.runtime_bundle_identity_sha256,
        ])
        self.assertEqual(self.wire.snapshot.read_bytes(), self.wire.source)

    def test_unproved_absence_never_reaches_admission_guard(self) -> None:
        for status, body, expected in (
            (404, {"detail": "other absence"}, RemoteProviderProtocolErrorV4),
            (429, {}, RemoteProviderUnavailableV4),
        ):
            with self.subTest(status=status):
                self.events.clear()
                command = self.wire._submission_command()
                source = cast(wire_fixtures._SnapshotSource, command.snapshot_source)
                admission = _AdmissionGuard(blocked=True, events=self.events)

                def handler(request: httpx.Request) -> httpx.Response:
                    self.events.append(request.method)
                    return httpx.Response(status, json=body)

                with self._provider(handler, admission) as provider:
                    with self.assertRaises(expected):
                        provider.reconcile_or_submit(command)
                self.assertEqual(self.events, ["GET"])
                self.assertEqual(admission.identities, [])
                self.assertEqual(source.opens, 0)

    def test_pressure_change_during_snapshot_setup_is_checked_before_send(self) -> None:
        admission = _AdmissionGuard(blocked=False, events=self.events)
        command = self.wire._submission_command()
        source = cast(wire_fixtures._SnapshotSource, command.snapshot_source)

        class PauseAfterSnapshotOpen(wire_fixtures._Guard):
            def checkpoint(self) -> None:
                super().checkpoint()
                if source.opens:
                    admission.blocked = True

        command = replace(command, step_guard=PauseAfterSnapshotOpen())

        def handler(request: httpx.Request) -> httpx.Response:
            self.events.append(request.method)
            self.assertEqual(request.method, "GET")
            self.assertFalse(admission.blocked)
            return httpx.Response(404, json={"detail": "Task not found"})

        with self._provider(handler, admission) as provider:
            with self.assertRaises(StreamSubmissionDeferred) as raised:
                provider.reconcile_or_submit(command)
        self.assertIs(raised.exception, admission.deferred)
        self.assertEqual(self.events, ["GET", "admission-check"])
        self.assertEqual(source.opens, 1)

    def test_allowed_absence_path_checks_once_then_posts_original_snapshot(self) -> None:
        admission = _AdmissionGuard(blocked=False, events=self.events)

        def handler(request: httpx.Request) -> httpx.Response:
            self.events.append(request.method)
            if request.method == "GET":
                self.assertEqual(admission.identities, [])
                return httpx.Response(404, json={"detail": "Task not found"})
            self.assertEqual(self.events, ["GET", "admission-check", "POST"])
            self.assertEqual(request.url.path, "/tasks")
            self.assertIn(self.wire.source, request.read())
            return httpx.Response(202, json={
                **self.wire._task_payload("pending"),
                "message": "Task submitted successfully",
            })

        with self._provider(handler, admission) as provider:
            accepted = provider.reconcile_or_submit(self.wire._submission_command())
        self.assertIsNotNone(accepted.absence_proof)
        self.assertEqual(accepted.receipt.remote_task_identity, "task-1")
        self.assertEqual(len(admission.identities), 1)

    def test_paused_existing_lookup_poll_and_result_lease_continue(self) -> None:
        admission = _AdmissionGuard(blocked=True, events=self.events)
        command = self.wire._submission_command()
        source = cast(wire_fixtures._SnapshotSource, command.snapshot_source)
        self.wire.snapshot.unlink()
        poll_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            self.events.append(f"{request.method} {request.url.path}")
            if "/by-idempotency/" in request.url.path:
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json=self.wire._task_payload("pending"))
            if request.url.path == "/tasks/task-1":
                self.assertEqual(request.method, "GET")
                poll_count += 1
                return httpx.Response(200, json=self.wire._task_payload(
                    "processing" if poll_count == 1 else "completed", artifact_bytes=8,
                ))
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/tasks/task-1/lease")
            self.assertEqual(request.url.params["seconds"], "300")
            return httpx.Response(200, json=self.wire._lease_payload(10_120.0))

        with self._provider(handler, admission) as provider:
            accepted = provider.reconcile_or_submit(command)
            waiting = provider.poll_once(self.wire._poll_command(accepted))
            completed = provider.poll_once(self.wire._poll_command(accepted))
        self.assertIsNone(accepted.absence_proof)
        self.assertIsInstance(waiting, RemoteProviderWaitingV4)
        self.assertIsInstance(completed, RemoteProviderCompletedV4)
        self.assertEqual(source.opens, 0)
        self.assertEqual(admission.identities, [])
        self.assertEqual(self.events, [
            "GET /tasks/by-idempotency/" + self.wire.key,
            "GET /tasks/task-1", "GET /tasks/task-1", "POST /tasks/task-1/lease",
        ])

    def test_paused_ack_post_preserves_claim_check_and_disposal_path(self) -> None:
        admission = _AdmissionGuard(blocked=True, events=self.events)
        command, capability = self.wire._ack_evidence()
        body = (
            b'{"schema":"mineru-task-protocol.v2","task_id":"task-1",'
            b'"status":"consumed"}'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            self.events.append(f"{request.method} {request.url.path}")
            self.assertEqual(request.content, b"")
            return httpx.Response(200, content=body)

        with self._provider(handler, admission) as provider:
            response = provider.acknowledge(
                command=command,
                provider_capability=capability,
                step_guard=self.wire.guard,
                before_ack_post=lambda: self.events.append("claim-current"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.exact_bytes, body)
        self.assertEqual(admission.identities, [])
        self.assertEqual(self.events, ["claim-current", "POST /tasks/task-1/ack"])

    def test_pressure_after_post_does_not_block_ambiguous_response_lookup(self) -> None:
        admission = _AdmissionGuard(blocked=False, events=self.events)

        def handler(request: httpx.Request) -> httpx.Response:
            self.events.append(request.method)
            if request.method == "POST":
                self.assertIn(self.wire.source, request.read())
                admission.blocked = True
                raise httpx.ReadTimeout("accepted response lost", request=request)
            if admission.blocked:
                return httpx.Response(200, json=self.wire._task_payload("processing"))
            return httpx.Response(404, json={"detail": "Task not found"})

        with self._provider(handler, admission) as provider:
            accepted = provider.reconcile_or_submit(self.wire._submission_command())
        self.assertEqual(accepted.receipt.remote_task_identity, "task-1")
        self.assertIsNotNone(accepted.absence_proof)
        self.assertEqual(self.events, ["GET", "admission-check", "POST", "GET"])
        self.assertEqual(len(admission.identities), 1)


if __name__ == "__main__":
    unittest.main()
