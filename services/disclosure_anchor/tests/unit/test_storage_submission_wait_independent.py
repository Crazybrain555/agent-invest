"""Independent, synthetic witness for storage-refused V4 submissions.

The real HTTP adapter and durable backend policy are composed here; only the
provider's HTTP boundary, source PDF bytes, and persistence fixture are local.
"""

from __future__ import annotations

import hashlib
from unittest import TestCase, mock

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium import http_remote_v4
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteSubmissionCommandV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    RetryStage,
    StageWaiting,
)
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures
from tests.unit.test_staged_coordinator_backend_v4 import (
    _authority,
    _backend,
    _guard,
    _work,
    V4StageInputResolver,
)

_INGRESS_CAPACITY_REASON = "source_growth_capacity"


class StorageSubmissionWaitIndependentTests(TestCase):
    def setUp(self) -> None:
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)

    def _run_backend(
        self, provider: MinerUHttpRemoteV4, command: RemoteSubmissionCommandV4,
    ):
        authority = _authority("reconciling")
        inputs = mock.Mock(spec=V4StageInputResolver)
        inputs.submission_command.return_value = command
        materialization = mock.Mock()
        materialization.submission_snapshot_source_v4.return_value = command.snapshot_source
        backend, persistence, _, _ = _backend(
            authority,
            inputs=inputs,
            remote=provider,  # type: ignore[arg-type]
            materialization=materialization,
        )
        try:
            return backend.run_remote(
                _work(authority),
                credit_allowance=ResourceCreditVector(provider_tasks=1, ack_items=1),
                stage_guard=_guard(),
            )
        finally:
            # A capacity refusal must leave the original durable work untouched.
            self.assertEqual(persistence.appends, [])

    def test_closed_capacity_refusal_is_bounded_healthy_wait_on_original_key(self) -> None:
        command = self.wire._submission_command()
        original_intent = command.submission_intent
        original_intent_bytes = original_intent.canonical_bytes
        original_request_bytes = command.request_exact_bytes
        original_snapshot = self.wire.snapshot.read_bytes()
        self.assertEqual(
            original_intent.source_pdf_sha256,
            "sha256:" + hashlib.sha256(original_snapshot).hexdigest(),
        )
        calls: list[str] = []
        post_bodies: list[bytes] = []
        refusals = 4  # Repeated capacity waits must keep the non-budget outcome.

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "Task not found"})
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/tasks")
            body = request.read()
            post_bodies.append(body)
            for field, value in (
                ("agent_idempotency_key", original_intent.client_submit_key),
                ("agent_attempt_identity", original_intent.attempt_id),
                ("agent_fence_identity", original_intent.fence_identity),
            ):
                self.assertIn(
                    f'name="{field}"\r\n\r\n{value}\r\n'.encode(), body,
                )
            self.assertIn(original_snapshot, body)
            if len(post_bodies) <= refusals:
                return httpx.Response(
                    429,
                    json={"detail": {
                        "code": "storage_capacity_wait",
                        "reason": _INGRESS_CAPACITY_REASON,
                    }},
                )
            return httpx.Response(202, json=self.wire._task_payload("pending"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"w" * count,
            request_timeout_seconds=30.0,
        ) as provider:
            for _ in range(refusals):
                with self.assertRaises(StageWaiting) as raised:
                    self._run_backend(provider, command)
                self.assertNotIsInstance(raised.exception, RetryStage)
                self.assertGreater(raised.exception.retry_after_seconds, 0)
                self.assertLessEqual(raised.exception.retry_after_seconds, 30)
            accepted = provider.reconcile_or_submit(command)

        self.assertEqual(accepted.receipt.submission_intent_sha256, original_intent.sha256)
        self.assertEqual(accepted.receipt.attempt_id, original_intent.attempt_id)
        self.assertEqual(accepted.receipt.fence_identity, original_intent.fence_identity)
        self.assertEqual(command.submission_intent, original_intent)
        self.assertEqual(command.submission_intent.canonical_bytes, original_intent_bytes)
        self.assertEqual(command.request_exact_bytes, original_request_bytes)
        self.assertEqual(self.wire.snapshot.read_bytes(), original_snapshot)
        self.assertEqual(len(post_bodies), refusals + 1)
        self.assertEqual(
            calls,
            [
                f"GET /tasks/by-idempotency/{original_intent.client_submit_key}",
                "POST /tasks",
                f"GET /tasks/by-idempotency/{original_intent.client_submit_key}",
            ] * refusals
            + [
                f"GET /tasks/by-idempotency/{original_intent.client_submit_key}",
                "POST /tasks",
            ],
        )

    def test_pre_body_capacity_refusal_does_not_require_full_upload(self) -> None:
        command = self.wire._submission_command()
        calls: list[str] = []
        observed_uploads: list[http_remote_v4._GuardedUpload] = []

        class ObservedUpload(http_remote_v4._GuardedUpload):
            def __init__(self, source, submission_command):
                super().__init__(source, submission_command)
                observed_uploads.append(self)

        class EarlyRefusalTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                calls.append(f"{request.method} {request.url.path}")
                if request.method == "GET":
                    return httpx.Response(404, json={"detail": "Task not found"})
                self_test.assertEqual(request.method, "POST")
                # Unlike MockTransport, never call request.read()/iter_bytes().
                return httpx.Response(429, json={"detail": {
                    "code": "storage_capacity_wait",
                    "reason": _INGRESS_CAPACITY_REASON,
                }})

        self_test = self

        try:
            with mock.patch.object(http_remote_v4, "_GuardedUpload", ObservedUpload):
                with MinerUHttpRemoteV4(
                    transport=EarlyRefusalTransport(),
                    request_timeout_seconds=30.0,
                ) as provider:
                    with self.assertRaises(StageWaiting) as raised:
                        self._run_backend(provider, command)
        finally:
            # This observation must execute even while P5's wait mapping is red.
            self.assertEqual(len(observed_uploads), 1)
            self.assertEqual(observed_uploads[0].byte_count, 0)
        self.assertGreater(raised.exception.retry_after_seconds, 0)
        self.assertEqual(self.wire.snapshot.read_bytes(), self.wire.source)
        self.assertEqual(calls, [
            f"GET /tasks/by-idempotency/{command.submission_intent.client_submit_key}",
            "POST /tasks",
            f"GET /tasks/by-idempotency/{command.submission_intent.client_submit_key}",
        ])

    def test_general_429_and_unknown_post_retain_retry_failure_semantics(self) -> None:
        for outcome in ("ordinary_429", "wrong_code_429", "lost_post"):
            with self.subTest(outcome=outcome):
                command = self.wire._submission_command()
                calls: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(f"{request.method} {request.url.path}")
                    if request.method == "GET":
                        return httpx.Response(404, json={"detail": "Task not found"})
                    self.assertEqual(request.method, "POST")
                    request.read()
                    if outcome == "ordinary_429":
                        return httpx.Response(429, json={"detail": "Too Many Requests"})
                    if outcome == "wrong_code_429":
                        return httpx.Response(429, json={"detail": {"code": "other_wait"}})
                    raise httpx.ReadTimeout("POST response lost", request=request)

                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler),
                    request_timeout_seconds=30.0,
                ) as provider:
                    with self.assertRaises(RetryStage):
                        self._run_backend(provider, command)
                self.assertEqual(calls, [
                    f"GET /tasks/by-idempotency/{command.submission_intent.client_submit_key}",
                    "POST /tasks",
                    f"GET /tasks/by-idempotency/{command.submission_intent.client_submit_key}",
                ])
