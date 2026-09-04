from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, BinaryIO, Callable, cast
import unittest
from unittest.mock import patch

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4 as _MinerUHttpRemoteV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    MinerUV4Transport,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MAX_WIRE_JSON_BYTES,
    canonical_client_submit_key_v2,
    canonical_result_owner_v2,
    submission_form_v2,
    submission_request_exact_bytes_v2,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_provider_v4 import (
    AcceptedProviderSubmissionV4,
    RemotePollCommandV4,
    RemoteProviderCompletedV4,
    RemoteProviderFailedV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
    RemoteProviderV4Port,
    RemoteProviderWaitingV4,
    RemoteSubmissionAmbiguousV4,
    RemoteSubmissionCommandV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PrivateProviderCapabilityV4,
    ProviderAckCommandV4,
    V4StageGuard,
    seal_provider_ack_command_v4,
)
from tests.unit.test_staged_provider_parser_v4 import (
    _ack_replay,
    _happy_path_for_port,
)

_SHA_B = "sha256:" + "b" * 64
MinerUHttpRemoteV4 = partial(
    _MinerUHttpRemoteV4,
    request_timeout_seconds=30.0,
)


class _Guard:
    def __init__(self) -> None:
        self.checkpoints = 0
        self.remaining = 60.0
        self.fail = False

    def checkpoint(self) -> None:
        self.checkpoints += 1
        if self.fail:
            raise RuntimeError("guard revoked")

    def remaining_seconds(self) -> float:
        self.checkpoint()
        return self.remaining


class _SnapshotSource:
    def __init__(
        self,
        *,
        path: Path,
        intent: SubmissionIntentV4,
        receipt: SnapshotReceiptV4,
    ) -> None:
        self.path = path
        self.intent = intent
        self.receipt = receipt
        self.opens = 0
        self.fail_before_open = False
        self.fail_on_exit = False

    def validates(
        self,
        *,
        submission_intent: SubmissionIntentV4,
        snapshot_receipt: SnapshotReceiptV4,
    ) -> bool:
        return submission_intent == self.intent and snapshot_receipt == self.receipt

    @contextmanager
    def open(self, *, step_guard: V4StageGuard) -> Iterator[BinaryIO]:
        step_guard.checkpoint()
        self.opens += 1
        if self.fail_before_open:
            raise OSError("snapshot pin failed")
        try:
            with self.path.open("rb") as stream:
                yield stream
        finally:
            if self.fail_on_exit:
                raise OSError("snapshot close verification failed")


class _RecordingStream(httpx.SyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def _require_remote_ports(
    remote: RemoteProviderV4Port,
    staged: MinerUV4Transport,
) -> None:
    del remote, staged


class MinerUHttpRemoteV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temp.name) / "snapshot.pdf"
        self.source = b"%PDF-1.7\nclosed snapshot\n%%EOF\n"
        self.snapshot.write_bytes(self.source)
        self.snapshot.chmod(0o600)
        self.source_sha = "sha256:" + hashlib.sha256(self.source).hexdigest()
        self.key = canonical_client_submit_key_v2(
            source_pdf_sha256=self.source_sha,
            attempt_identity="attempt-1",
            fence_identity="fence-1",
            submission_epoch_unix=1,
        )
        self.guard = _Guard()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_404_then_one_post_returns_task_bound_capability(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "Task not found"})
            body = request.read()
            self.assertIn(b"agent_idempotency_key", body)
            self.assertIn(self.key.encode(), body)
            self.assertIn(self.source, body)
            return httpx.Response(202, json=self._task_payload("pending"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"s" * count,
        ) as provider:
            result = provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(
            calls,
            ["GET /tasks/by-idempotency/" + self.key, "POST /tasks"],
        )
        self.assertIsNotNone(result.absence_proof)
        self.assertEqual(result.receipt.remote_task_identity, "task-1")
        self.assertEqual(result.provider_capability.token_bytes, b"s" * 32)
        self.assertEqual(
            result.provider_capability.capability_purpose,
            "submitted_task_resume",
        )
        self.assertNotIn("ssssssss", repr(result))
        self.assertNotIn("ssssssss", repr(result.provider_capability))

    def test_existing_task_does_not_open_or_post_snapshot(self) -> None:
        command = self._submission_command()
        source = cast(_SnapshotSource, command.snapshot_source)
        self.snapshot.unlink()
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(200, json=self._task_payload("processing"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"x" * count,
        ) as provider:
            result = provider.reconcile_or_submit(command)
        self.assertEqual(calls, ["GET"])
        self.assertEqual(source.opens, 0)
        self.assertIsNone(result.absence_proof)

    def test_lookup_failure_never_authorizes_post_or_snapshot_open(self) -> None:
        cases: tuple[
            tuple[str, Callable[[httpx.Request], httpx.Response]], ...
        ] = (
            (
                "transport",
                lambda request: (_ for _ in ()).throw(
                    httpx.ReadTimeout("lookup timeout", request=request)
                ),
            ),
            ("bad-404", lambda _request: httpx.Response(404, json={"detail": "x"})),
            ("rate-limit", lambda _request: httpx.Response(429, json={})),
            (
                "oversized",
                lambda _request: httpx.Response(
                    200, content=b"x" * (MAX_WIRE_JSON_BYTES + 1)
                ),
            ),
        )
        for name, lookup in cases:
            command = self._submission_command()
            source = cast(_SnapshotSource, command.snapshot_source)
            calls: list[str] = []

            def handler(
                request: httpx.Request,
                lookup: Callable[[httpx.Request], httpx.Response] = lookup,
            ) -> httpx.Response:
                calls.append(request.method)
                return lookup(request)

            with self.subTest(name=name), self.assertRaises(
                (RemoteProviderUnavailableV4, RemoteProviderProtocolErrorV4)
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler)
                ) as provider:
                    provider.reconcile_or_submit(command)
            self.assertEqual(calls, ["GET"])
            self.assertEqual(source.opens, 0)

    def test_token_failure_has_zero_network_or_file_side_effects(self) -> None:
        command = self._submission_command()
        source = cast(_SnapshotSource, command.snapshot_source)
        calls: list[str] = []

        def token_failure(_count: int) -> bytes:
            raise OSError("entropy unavailable")

        def forbidden_network(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            raise AssertionError("network must not be reached")

        with self.assertRaises(RemoteProviderProtocolErrorV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(forbidden_network),
                token_factory=token_failure,
            ) as provider:
                provider.reconcile_or_submit(command)
        self.assertEqual(calls, [])
        self.assertEqual(source.opens, 0)

    def test_noncanonical_submit_key_has_zero_network_side_effects(self) -> None:
        wrong_key = "1." + "a" * 64
        self.assertNotEqual(wrong_key, self.key)
        command = self._submission_command(client_key=wrong_key)
        calls: list[str] = []

        def forbidden_network(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            raise AssertionError("network must not be reached")

        with self.assertRaises(RemoteProviderProtocolErrorV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(forbidden_network)
            ) as provider:
                provider.reconcile_or_submit(command)
        self.assertEqual(calls, [])

    def test_post_timeout_reconciles_once_by_same_key_without_second_post(self) -> None:
        calls: list[str] = []
        lookups = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal lookups
            calls.append(request.method)
            if request.method == "POST":
                request.read()
                raise httpx.ReadTimeout("lost response", request=request)
            lookups += 1
            if lookups == 1:
                return httpx.Response(404, json={"detail": "Task not found"})
            return httpx.Response(200, json=self._task_payload("pending"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"r" * count,
        ) as provider:
            result = provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"])
        self.assertEqual(result.receipt.remote_task_identity, "task-1")

    def test_invalid_post_response_reconciles_once_without_resubmit(self) -> None:
        calls: list[str] = []
        lookups = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal lookups
            calls.append(request.method)
            if request.method == "POST":
                request.read()
                return httpx.Response(202, content=b'{"task_id":"truncated"}')
            lookups += 1
            if lookups == 1:
                return httpx.Response(404, json={"detail": "Task not found"})
            return httpx.Response(200, json=self._task_payload("pending"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"z" * count,
        ) as provider:
            result = provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"])
        self.assertEqual(result.receipt.remote_task_identity, "task-1")

    def test_one_episode_ambiguous_404_never_loops_or_resubmits(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                request.read()
                return httpx.Response(500, json={"detail": "lost"})
            return httpx.Response(404, json={"detail": "Task not found"})

        with self.assertRaises(RemoteSubmissionAmbiguousV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_later_episode_replays_only_same_canonical_submission(self) -> None:
        calls: list[str] = []
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                bodies.append(request.read())
                return httpx.Response(500, json={"detail": "lost"})
            return httpx.Response(404, json={"detail": "Task not found"})

        with MinerUHttpRemoteV4(transport=httpx.MockTransport(handler)) as provider:
            for _ in range(2):
                with self.assertRaises(RemoteSubmissionAmbiguousV4):
                    provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"] * 2)
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertIn(self.key.encode(), body)
            self.assertIn(self.source, body)
            self.assertIn(b'name="backend"', body)
            self.assertIn(b"hybrid-http-client", body)

    def test_snapshot_failures_are_classified_by_post_start_barrier(self) -> None:
        for after_post in (False, True):
            command = self._submission_command()
            source = cast(_SnapshotSource, command.snapshot_source)
            source.fail_before_open = not after_post
            source.fail_on_exit = after_post
            calls: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                if request.method == "GET":
                    return httpx.Response(404, json={"detail": "Task not found"})
                request.read()
                return httpx.Response(202, json=self._task_payload("pending"))

            expected = RemoteSubmissionAmbiguousV4 if after_post else OSError
            with self.subTest(after_post=after_post), self.assertRaises(expected):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler)
                ) as provider:
                    provider.reconcile_or_submit(command)
            self.assertEqual(calls, ["GET", "POST"] if after_post else ["GET"])

    def test_exhausted_budget_before_send_is_not_false_post_ambiguity(self) -> None:
        class ExhaustAfterLookup(_Guard):
            def __init__(self) -> None:
                super().__init__()
                self.remaining_calls = 0

            def remaining_seconds(self) -> float:
                self.remaining_calls += 1
                self.checkpoint()
                return 60.0 if self.remaining_calls == 1 else 0.0

        guard = ExhaustAfterLookup()
        command = replace(self._submission_command(), step_guard=guard)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method != "GET":
                raise AssertionError("POST must not start after its budget expired")
            return httpx.Response(404, json={"detail": "Task not found"})

        with self.assertRaises(RemoteProviderUnavailableV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(command)
        self.assertEqual(calls, ["GET"])

    def test_pre_post_guard_failure_preserves_exact_exception(self) -> None:
        class LeaseLost(RuntimeError):
            pass

        expected = LeaseLost("claim lease lost")

        class LoseBeforePost(_Guard):
            def __init__(self) -> None:
                super().__init__()
                self.remaining_calls = 0

            def remaining_seconds(self) -> float:
                self.remaining_calls += 1
                self.checkpoint()
                if self.remaining_calls > 1:
                    raise expected
                return 60.0

        guard = LoseBeforePost()
        command = replace(self._submission_command(), step_guard=guard)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(404, json={"detail": "Task not found"})

        with self.assertRaises(LeaseLost) as raised:
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(command)
        self.assertIs(raised.exception, expected)
        self.assertEqual(calls, ["GET"])

    def test_reconcile_transport_failure_after_post_is_ambiguous(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if calls == ["GET"]:
                return httpx.Response(404, json={"detail": "Task not found"})
            if request.method == "POST":
                request.read()
                return httpx.Response(500, json={"detail": "unknown"})
            raise httpx.ConnectError("reconcile unavailable", request=request)

        with self.assertRaises(RemoteSubmissionAmbiguousV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_uploaded_byte_drift_after_send_is_ambiguous(self) -> None:
        command = self._submission_command()
        self.snapshot.write_bytes(self.source + b"drift")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "Task not found"})
            request.read()
            return httpx.Response(202, json=self._task_payload("pending"))

        with self.assertRaises(RemoteSubmissionAmbiguousV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(command)
        self.assertEqual(calls, ["GET", "POST"])

    def test_oversized_reconcile_after_post_is_ambiguous_not_protocol_leak(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                request.read()
                return httpx.Response(500, json={})
            if calls == ["GET"]:
                return httpx.Response(404, json={"detail": "Task not found"})
            return httpx.Response(200, content=b"x" * (MAX_WIRE_JSON_BYTES + 1))

        with self.assertRaises(RemoteSubmissionAmbiguousV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_upload_guard_loss_is_ambiguous(self) -> None:
        posts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "Task not found"})
            posts += 1
            self.guard.fail = True
            request.read()
            raise AssertionError("guard should abort multipart read")

        with self.assertRaises(RemoteSubmissionAmbiguousV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(posts, 1)

    def test_request_spec_drift_fails_at_command_boundary(self) -> None:
        command = self._submission_command()
        with self.assertRaisesRegex(ValueError, "exact request"):
            replace(command, request_exact_bytes=b"wrong")

    def test_pending_poll_is_exactly_one_get_and_no_lease(self) -> None:
        accepted = self._accepted()
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json=self._task_payload("processing"))

        with MinerUHttpRemoteV4(transport=httpx.MockTransport(handler)) as provider:
            outcome = provider.poll_once(self._poll_command(accepted))
        self.assertIsInstance(outcome, RemoteProviderWaitingV4)
        self.assertEqual(calls, ["GET /tasks/task-1"])

    def test_poll_rejects_command_subclass_before_network(self) -> None:
        base = self._poll_command(self._accepted())

        class PollSubclass(RemotePollCommandV4):
            pass

        command = PollSubclass(
            submission_intent=base.submission_intent,
            accepted_submission=base.accepted_submission,
            provider_capability=base.provider_capability,
            artifact_byte_limit=base.artifact_byte_limit,
            result_lease_seconds=base.result_lease_seconds,
            step_guard=base.step_guard,
        )
        calls: list[str] = []

        def forbidden(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            raise AssertionError("non-exact poll command reached network")

        with self.assertRaises(RemoteProviderProtocolErrorV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(forbidden)
            ) as provider:
                provider.poll_once(command)
        self.assertEqual(calls, [])

    def test_completed_poll_validates_result_then_acquires_fresh_lease(self) -> None:
        accepted = self._accepted()
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(200, json=self._task_payload("completed", 123))
            self.assertEqual(request.url.params["seconds"], "300")
            return httpx.Response(
                200,
                json={
                    "schema": "mineru-task-protocol.v2",
                    "task_id": "task-1",
                    "lease_until_unix": 10_000.1,
                },
            )

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler), wall_clock=lambda: 10_000.0
        ) as provider:
            outcome = provider.poll_once(
                self._poll_command(accepted, byte_limit=123)
            )
        self.assertIsInstance(outcome, RemoteProviderCompletedV4)
        assert isinstance(outcome, RemoteProviderCompletedV4)
        self.assertEqual(outcome.receipt.artifact_byte_count, 123)
        self.assertEqual(outcome.lease_observed_at_unix, 10_000.0)
        self.assertEqual(
            calls,
            ["GET /tasks/task-1", "POST /tasks/task-1/lease"],
        )

    def test_stale_or_equal_lease_fails_closed_as_unavailable(self) -> None:
        accepted = self._accepted()
        for lease_until in (9_999.9, 10_000.0):
            calls: list[str] = []

            def handler(
                request: httpx.Request,
                lease_until: float = lease_until,
            ) -> httpx.Response:
                calls.append(request.method)
                if request.method == "GET":
                    return httpx.Response(
                        200, json=self._task_payload("completed", 123)
                    )
                return httpx.Response(
                    200,
                    json={
                        "schema": "mineru-task-protocol.v2",
                        "task_id": "task-1",
                        "lease_until_unix": lease_until,
                    },
                )

            with self.subTest(lease_until=lease_until), self.assertRaises(
                RemoteProviderUnavailableV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler),
                    wall_clock=lambda: 10_000.0,
                ) as provider:
                    provider.poll_once(self._poll_command(accepted, byte_limit=123))
            self.assertEqual(calls, ["GET", "POST"])

    def test_bad_or_over_limit_completed_result_never_acquires_lease(self) -> None:
        accepted = self._accepted()
        cases: tuple[tuple[str, dict[str, object], int], ...] = (
            ("owner", {"result_artifact_owner": "0" * 64}, 123),
            ("limit", {}, 122),
        )
        for name, mutation, limit in cases:
            calls: list[str] = []

            def handler(
                request: httpx.Request,
                mutation: dict[str, object] = mutation,
            ) -> httpx.Response:
                calls.append(request.method)
                return httpx.Response(
                    200,
                    json={**self._task_payload("completed", 123), **mutation},
                )

            with self.subTest(name=name), self.assertRaises(
                RemoteProviderProtocolErrorV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler)
                ) as provider:
                    provider.poll_once(self._poll_command(accepted, byte_limit=limit))
            self.assertEqual(calls, ["GET"])

    def test_result_lease_failure_never_returns_terminal(self) -> None:
        accepted = self._accepted()
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=self._task_payload("completed", 123))
            return httpx.Response(503, json={"detail": "lease unavailable"})

        with self.assertRaises(RemoteProviderUnavailableV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                provider.poll_once(self._poll_command(accepted, byte_limit=123))
        self.assertEqual(calls, ["GET", "POST"])

    def test_noncanonical_persisted_routes_fail_before_poll_network(self) -> None:
        accepted = self._accepted()
        receipts = (
            replace(
                accepted.receipt,
                status_url="https://mineru.invalid/other/task-1",
            ),
            replace(
                accepted.receipt,
                result_url="https://mineru.invalid/tasks/task-1/other",
            ),
        )
        for receipt in receipts:
            grafted = replace(accepted, receipt=receipt)
            calls: list[str] = []

            def forbidden_network(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                raise AssertionError("network must not be reached")

            with self.subTest(receipt=receipt), self.assertRaises(
                RemoteProviderProtocolErrorV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(forbidden_network)
                ) as provider:
                    provider.poll_once(self._poll_command(grafted))
            self.assertEqual(calls, [])

    def test_capability_graft_or_wrong_purpose_fails_at_poll_boundary(self) -> None:
        accepted = self._accepted()
        for capability in (
            replace(accepted.provider_capability, attempt_id="attempt-other"),
            replace(
                accepted.provider_capability,
                capability_purpose="result_download",
            ),
        ):
            with self.subTest(capability=capability), self.assertRaises(ValueError):
                RemotePollCommandV4(
                    submission_intent=self._submission_command().submission_intent,
                    accepted_submission=accepted.receipt,
                    provider_capability=capability,
                    artifact_byte_limit=1024,
                    result_lease_seconds=300,
                    step_guard=self.guard,
                )

    def test_failed_poll_returns_typed_observation(self) -> None:
        accepted = self._accepted()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={**self._task_payload("failed"), "error": "model failed"},
            )

        with MinerUHttpRemoteV4(transport=httpx.MockTransport(handler)) as provider:
            outcome = provider.poll_once(self._poll_command(accepted))
        self.assertIsInstance(outcome, RemoteProviderFailedV4)
        assert isinstance(outcome, RemoteProviderFailedV4)
        self.assertEqual(outcome.provider_error, "model failed")

    def test_result_download_reacquires_fresh_lease_immediately_before_get(
        self,
    ) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        events: list[str] = []
        clocks = iter((10_000.0, 10_000.1))

        def handler(request: httpx.Request) -> httpx.Response:
            events.append(f"{request.method} {request.url.path}")
            if request.method == "POST":
                self.assertEqual(request.url.params["seconds"], "300")
                return httpx.Response(200, json=self._lease_payload(10_100.0))
            return httpx.Response(
                200,
                content=body,
                headers=self._result_headers(terminal),
            )

        def before_result_get() -> None:
            events.append("claim-current")

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            wall_clock=lambda: next(clocks),
        ) as provider:
            downloaded = b"".join(
                provider.stream_result(
                    accepted_submission=accepted,
                    terminal_receipt=terminal,
                    provider_capability=capability,
                    result_lease_seconds=300,
                    step_guard=self.guard,
                    before_result_get=before_result_get,
                )
            )
        self.assertEqual(downloaded, body)
        self.assertEqual(
            events,
            [
                "POST /tasks/task-1/lease",
                "claim-current",
                "GET /tasks/task-1/result",
            ],
        )

    def test_result_lease_expiring_during_claim_recheck_makes_zero_gets(self) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        calls: list[str] = []
        clocks = iter((10_000.0, 10_001.0))
        claim_checks = 0

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method != "POST":
                raise AssertionError("expired lease must authorize zero result GETs")
            return httpx.Response(200, json=self._lease_payload(10_000.5))

        def before_result_get() -> None:
            nonlocal claim_checks
            claim_checks += 1

        with self.assertRaises(RemoteProviderUnavailableV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler),
                wall_clock=lambda: next(clocks),
            ) as provider:
                list(
                    provider.stream_result(
                        accepted_submission=accepted,
                        terminal_receipt=terminal,
                        provider_capability=capability,
                        result_lease_seconds=300,
                        step_guard=self.guard,
                        before_result_get=before_result_get,
                    )
                )
        self.assertEqual(calls, ["POST /tasks/task-1/lease"])
        self.assertEqual(claim_checks, 1)

    def test_claim_loss_before_result_get_preserves_identity_and_makes_zero_gets(
        self,
    ) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        calls: list[str] = []
        claim_lost = RuntimeError("claim lost")

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method != "POST":
                raise AssertionError("claim loss must authorize zero result GETs")
            return httpx.Response(200, json=self._lease_payload(10_100.0))

        def before_result_get() -> None:
            raise claim_lost

        with self.assertRaises(RuntimeError) as raised:
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler),
                wall_clock=lambda: 10_000.0,
            ) as provider:
                list(
                    provider.stream_result(
                        accepted_submission=accepted,
                        terminal_receipt=terminal,
                        provider_capability=capability,
                        result_lease_seconds=300,
                        step_guard=self.guard,
                        before_result_get=before_result_get,
                    )
                )
        self.assertIs(raised.exception, claim_lost)
        self.assertEqual(calls, ["POST /tasks/task-1/lease"])

    def test_stale_result_lease_or_lease_conflict_makes_zero_gets(self) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        for name, lease_status, lease_until in (
            ("stale", 200, 10_000.0),
            ("conflict", 409, 10_100.0),
        ):
            calls: list[str] = []

            def handler(
                request: httpx.Request,
                lease_status: int = lease_status,
                lease_until: float = lease_until,
            ) -> httpx.Response:
                calls.append(f"{request.method} {request.url.path}")
                if request.method != "POST":
                    raise AssertionError("failed lease must authorize zero result GETs")
                return httpx.Response(
                    lease_status,
                    json=self._lease_payload(lease_until),
                )

            with self.subTest(name=name), self.assertRaises(
                RemoteProviderUnavailableV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler),
                    wall_clock=lambda: 10_000.0,
                ) as provider:
                    list(
                        provider.stream_result(
                            accepted_submission=accepted,
                            terminal_receipt=terminal,
                            provider_capability=capability,
                            result_lease_seconds=300,
                            step_guard=self.guard,
                            before_result_get=lambda: None,
                        )
                    )
            self.assertEqual(calls, ["POST /tasks/task-1/lease"])

    def test_result_get_conflict_is_one_attempt_without_retry(self) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "POST":
                return httpx.Response(200, json=self._lease_payload(10_100.0))
            return httpx.Response(409, json={"detail": "active reader"})

        with self.assertRaises(RemoteProviderUnavailableV4):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler),
                wall_clock=lambda: 10_000.0,
            ) as provider:
                list(
                    provider.stream_result(
                        accepted_submission=accepted,
                        terminal_receipt=terminal,
                        provider_capability=capability,
                        result_lease_seconds=300,
                        step_guard=self.guard,
                        before_result_get=lambda: None,
                    )
                )
        self.assertEqual(
            calls,
            ["POST /tasks/task-1/lease", "GET /tasks/task-1/result"],
        )

    def test_result_evidence_grafts_fail_before_network(self) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        cases = (
            (
                "capability-purpose",
                accepted,
                terminal,
                replace(capability, capability_purpose="submitted_task_resume"),
            ),
            (
                "capability-task",
                accepted,
                terminal,
                replace(capability, remote_task_identity="task-other"),
            ),
            (
                "terminal-owner",
                accepted,
                replace(terminal, result_owner_identity="0" * 64),
                capability,
            ),
            (
                "result-route",
                replace(
                    accepted,
                    result_url="https://mineru.invalid/tasks/task-1/other",
                ),
                terminal,
                capability,
            ),
        )
        for name, candidate_accepted, candidate_terminal, candidate_capability in cases:
            calls: list[str] = []

            def forbidden_network(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                raise AssertionError("grafted evidence must not reach the network")

            with self.subTest(name=name), self.assertRaises(
                RemoteProviderProtocolErrorV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(forbidden_network),
                    wall_clock=lambda: 10_000.0,
                ) as provider:
                    list(
                        provider.stream_result(
                            accepted_submission=candidate_accepted,
                            terminal_receipt=candidate_terminal,
                            provider_capability=candidate_capability,
                            result_lease_seconds=300,
                            step_guard=self.guard,
                            before_result_get=lambda: None,
                        )
                    )
            self.assertEqual(calls, [])

    def test_abandoned_result_consumer_closes_streaming_response(self) -> None:
        chunk_size = 1024 * 1024
        body = (b"a" * chunk_size) + (b"b" * chunk_size)
        accepted, terminal, capability = self._result_evidence(body)
        response_stream = _RecordingStream(
            (body[:chunk_size], body[chunk_size:])
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=self._lease_payload(10_100.0))
            return httpx.Response(
                200,
                stream=response_stream,
                headers=self._result_headers(terminal),
            )

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            wall_clock=lambda: 10_000.0,
        ) as provider:
            result = iter(
                provider.stream_result(
                    accepted_submission=accepted,
                    terminal_receipt=terminal,
                    provider_capability=capability,
                    result_lease_seconds=300,
                    step_guard=self.guard,
                    before_result_get=lambda: None,
                )
            )
            self.assertEqual(next(result), body[:chunk_size])
            close_result = getattr(result, "close")
            close_result()
        self.assertTrue(response_stream.closed)

    def test_request_timeout_is_runtime_configuration_not_adapter_magic(self) -> None:
        accepted = self._accepted()
        observed: list[dict[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(cast(dict[str, float], request.extensions["timeout"]))
            return httpx.Response(200, json=self._task_payload("processing"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            request_timeout_seconds=7.0,
        ) as provider:
            provider.poll_once(self._poll_command(accepted))
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0].values()), {7.0})

    def test_ack_uses_canonical_route_after_last_claim_recheck(self) -> None:
        command, capability = self._ack_evidence()
        body = (
            b'{"schema":"mineru-task-protocol.v2","task_id":"task-1",'
            b'"status":"consumed"}'
        )
        events: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            events.append(f"{request.method} {request.url.path}")
            self.assertEqual(request.content, b"")
            return httpx.Response(200, content=body)

        def before_ack_post() -> None:
            events.append("claim-current")

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler)
        ) as provider:
            response = provider.acknowledge(
                command=command,
                provider_capability=capability,
                step_guard=self.guard,
                before_ack_post=before_ack_post,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.exact_bytes, body)
        self.assertEqual(
            events,
            ["claim-current", "POST /tasks/task-1/ack"],
        )

    def test_ack_claim_loss_and_capability_graft_have_zero_network_effects(
        self,
    ) -> None:
        command, capability = self._ack_evidence()
        cases = (
            (
                "claim-loss",
                capability,
                lambda: (_ for _ in ()).throw(RuntimeError("claim lost")),
                RuntimeError,
            ),
            (
                "wrong-purpose",
                replace(capability, capability_purpose="result_download"),
                lambda: None,
                RemoteProviderProtocolErrorV4,
            ),
            (
                "wrong-task",
                replace(capability, remote_task_identity="task-other"),
                lambda: None,
                RemoteProviderProtocolErrorV4,
            ),
        )
        for name, candidate, before_ack_post, expected in cases:
            calls: list[str] = []

            def forbidden_network(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                raise AssertionError("invalid ACK authority reached the network")

            with self.subTest(name=name), self.assertRaises(expected):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(forbidden_network)
                ) as provider:
                    provider.acknowledge(
                        command=command,
                        provider_capability=candidate,
                        step_guard=self.guard,
                        before_ack_post=before_ack_post,
                    )
            self.assertEqual(calls, [])

    def test_ack_response_loss_and_conflict_are_single_unavailable_attempts(
        self,
    ) -> None:
        command, capability = self._ack_evidence()
        for name, response in (
            ("conflict", httpx.Response(409, json={"detail": "active reader"})),
            ("server", httpx.Response(503, json={"detail": "unavailable"})),
        ):
            calls = 0

            def handler(
                _request: httpx.Request,
                response: httpx.Response = response,
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                return response

            with self.subTest(name=name), self.assertRaises(
                RemoteProviderUnavailableV4
            ):
                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler)
                ) as provider:
                    provider.acknowledge(
                        command=command,
                        provider_capability=capability,
                        step_guard=self.guard,
                        before_ack_post=lambda: None,
                    )
            self.assertEqual(calls, 1)

    def test_ack_absence_is_returned_exactly_and_oversize_fails_closed(self) -> None:
        command, capability = self._ack_evidence()
        absence = b'{"detail":"Task not found"}'
        for name, status, body, expected in (
            ("absence", 404, absence, None),
            ("oversize", 200, b"x" * (64 * 1024 + 1), RemoteProviderProtocolErrorV4),
        ):
            def handler(
                _request: httpx.Request,
                status: int = status,
                body: bytes = body,
            ) -> httpx.Response:
                return httpx.Response(status, content=body)

            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                if expected is not None:
                    with self.subTest(name=name), self.assertRaises(expected):
                        provider.acknowledge(
                            command=command,
                            provider_capability=capability,
                            step_guard=self.guard,
                            before_ack_post=lambda: None,
                        )
                else:
                    response = provider.acknowledge(
                        command=command,
                        provider_capability=capability,
                        step_guard=self.guard,
                        before_ack_post=lambda: None,
                    )
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.exact_bytes, body)

    def test_one_long_lived_client_disables_env_proxy_and_redirects(self) -> None:
        real_client = httpx.Client
        seen: list[dict[str, object]] = []

        def client_factory(*args: object, **kwargs: object) -> httpx.Client:
            seen.append(dict(kwargs))
            return cast(Any, real_client)(*args, **kwargs)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._task_payload("pending"))

        with patch(
            "disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4.httpx.Client",
            side_effect=client_factory,
        ):
            with MinerUHttpRemoteV4(
                transport=httpx.MockTransport(handler)
            ) as provider:
                accepted = provider.reconcile_or_submit(self._submission_command())
                provider.poll_once(self._poll_command(accepted))
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["trust_env"], False)
        self.assertIs(seen[0]["follow_redirects"], False)
        timeout = cast(httpx.Timeout, seen[0]["timeout"])
        self.assertIsNotNone(timeout.connect)
        self.assertLess(cast(float, timeout.connect), 60.0)

    def test_request_timeout_uses_ninety_percent_of_remaining_stage_budget(
        self,
    ) -> None:
        self.guard.remaining = 10.0
        observed: list[dict[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(cast(dict[str, float], request.extensions["timeout"]))
            return httpx.Response(200, json=self._task_payload("pending"))

        with _MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            request_timeout_seconds=30.0,
        ) as provider:
            provider.reconcile_or_submit(self._submission_command())
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0].values()), {9.0})

    def test_result_get_timeout_is_bounded_by_fresh_lease_remaining(self) -> None:
        body = b"artifact"
        accepted, terminal, capability = self._result_evidence(body)
        clocks = iter((10_000.0, 10_000.0))
        observed: list[dict[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=self._lease_payload(10_000.5))
            observed.append(cast(dict[str, float], request.extensions["timeout"]))
            return httpx.Response(
                200,
                content=body,
                headers=self._result_headers(terminal),
            )

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            wall_clock=lambda: next(clocks),
        ) as provider:
            self.assertEqual(
                b"".join(
                    provider.stream_result(
                        accepted_submission=accepted,
                        terminal_receipt=terminal,
                        provider_capability=capability,
                        result_lease_seconds=300,
                        step_guard=self.guard,
                        before_result_get=lambda: None,
                    )
                ),
                body,
            )
            _require_remote_ports(provider, provider)
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0].values()), {0.45})

    def test_v4_remote_modules_do_not_import_v3_durable_authority(self) -> None:
        root = Path(__file__).parents[2] / "src" / "disclosure_anchor"
        files = (
            root / "application" / "ports" / "remote_provider_v4.py",
            root / "adapters" / "parsers" / "mineru_medium" / "http_remote_v4.py",
        )
        forbidden = {
            "PersistedSubmissionReceipt",
            "RemoteArtifactReceipt",
            "PrivateSubmittedTaskResume",
            "RecoveredV3ResumeSecret",
            "_Task",
            "MinerUHttpRemoteHandle",
        }
        for source_path in files:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            self.assertFalse(
                imported & forbidden,
                f"{source_path.name} imported V3 authority: {imported & forbidden}",
            )

    def _submission_command(
        self, *, client_key: str | None = None
    ) -> RemoteSubmissionCommandV4:
        options = ParserOptions(
            timeout_seconds=30,
            api_url="https://mineru.invalid",
            server_url="http://127.0.0.1:8000/v1",
            runtime_bundle_identity_sha256=_SHA_B,
        )
        parser_identity = ParserIdentity(name="MinerU", version="3.4.4")
        target_exact = json.dumps(
            options.target_identity(parser_identity).to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        target_sha = "sha256:" + hashlib.sha256(target_exact).hexdigest()
        filename = self.source_sha.removeprefix("sha256:") + ".pdf"
        request_exact = submission_request_exact_bytes_v2(
            api_origin=options.api_url or "",
            form=submission_form_v2(options, server_url=options.server_url or ""),
            upload_filename=filename,
        )
        snapshot = SnapshotReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            preparation_intent_sha256="sha256:" + "c" * 64,
            snapshot_relpath="snapshots/snapshot.pdf",
            snapshot_sha256=self.source_sha,
            snapshot_byte_count=len(self.source),
            part_path_absent=True,
            part_owner_path_absent=True,
            file_fsync_completed=True,
            parent_fsync_completed=True,
        )
        intent = SubmissionIntentV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            snapshot_receipt_sha256=snapshot.sha256,
            source_pdf_sha256=self.source_sha,
            parser_target_sha256=target_sha,
            request_sha256="sha256:" + hashlib.sha256(request_exact).hexdigest(),
            runtime_epoch_sha256=_SHA_B,
            client_submit_key=client_key or self.key,
            submission_epoch_unix=1,
            provider_protocol_version="mineru-task-protocol.v2",
        )
        source = _SnapshotSource(path=self.snapshot, intent=intent, receipt=snapshot)
        return RemoteSubmissionCommandV4(
            submission_intent=intent,
            snapshot_receipt=snapshot,
            snapshot_source=source,
            source_byte_count=len(self.source),
            parser_identity=parser_identity,
            parser_options=options,
            upload_filename=filename,
            request_exact_bytes=request_exact,
            step_guard=self.guard,
        )

    def _accepted(self) -> AcceptedProviderSubmissionV4:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=self._task_payload("pending"))
            raise AssertionError("unexpected POST")

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda count: b"p" * count,
        ) as provider:
            return provider.reconcile_or_submit(self._submission_command())

    def _poll_command(
        self,
        accepted: AcceptedProviderSubmissionV4,
        *,
        byte_limit: int = 1024,
    ) -> RemotePollCommandV4:
        return RemotePollCommandV4(
            submission_intent=self._submission_command().submission_intent,
            accepted_submission=accepted.receipt,
            provider_capability=accepted.provider_capability,
            artifact_byte_limit=byte_limit,
            result_lease_seconds=300,
            step_guard=self.guard,
        )

    def _result_evidence(
        self, body: bytes
    ) -> tuple[
        AcceptedSubmissionReceiptV4,
        TerminalReceiptV4,
        PrivateProviderCapabilityV4,
    ]:
        accepted = self._accepted()
        digest = hashlib.sha256(body).hexdigest()
        terminal = TerminalReceiptV4(
            attempt_id=accepted.receipt.attempt_id,
            fence_identity=accepted.receipt.fence_identity,
            accepted_submission_receipt_sha256=accepted.receipt.sha256,
            remote_task_identity=accepted.receipt.remote_task_identity,
            result_owner_identity=canonical_result_owner_v2(
                task_id=accepted.receipt.remote_task_identity,
                artifact_sha256=digest,
                artifact_byte_count=len(body),
            ),
            artifact_sha256="sha256:" + digest,
            artifact_byte_count=len(body),
            provider_protocol_version=accepted.receipt.provider_protocol_version,
        )
        capability = replace(
            accepted.provider_capability,
            capability_purpose="result_download",
        )
        return accepted.receipt, terminal, capability

    @staticmethod
    def _ack_evidence() -> tuple[
        ProviderAckCommandV4,
        PrivateProviderCapabilityV4,
    ]:
        fixture = _happy_path_for_port()
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=fixture["chain"][8],
            accepted_submission=fixture["accepted"],
            terminal_receipt=fixture["terminal"],
            cleanup_plan=fixture["cleanup_plan"],
            cleanup_receipt=fixture["cleanup_receipt"],
            replay_context=_ack_replay(fixture),
        )
        token = fixture["token"]
        accepted = fixture["accepted"]
        capability = PrivateProviderCapabilityV4(
            attempt_id=accepted.attempt_id,
            remote_task_identity=accepted.remote_task_identity,
            provider_protocol_version=accepted.provider_protocol_version,
            secret_kind=accepted.secret_kind,
            secret_version=accepted.secret_version,
            capability_purpose="result_acknowledgement",
            token_bytes=token,
            token_sha256=accepted.token_sha256,
            token_byte_count=accepted.token_byte_count,
        )
        return command, capability

    @staticmethod
    def _lease_payload(lease_until_unix: float) -> dict[str, object]:
        return {
            "schema": "mineru-task-protocol.v2",
            "task_id": "task-1",
            "lease_until_unix": lease_until_unix,
        }

    @staticmethod
    def _result_headers(terminal: TerminalReceiptV4) -> dict[str, str]:
        return {
            "Content-Type": "application/zip",
            "Content-Length": str(terminal.artifact_byte_count),
            "X-MinerU-Result-SHA256": terminal.artifact_sha256.removeprefix(
                "sha256:"
            ),
            "X-MinerU-Result-Owner": terminal.result_owner_identity,
        }

    def _task_payload(
        self, status: str, artifact_bytes: int = 0
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": "task-1",
            "status": status,
            "status_url": "/tasks/task-1",
            "result_url": "/tasks/task-1/result",
            "task_protocol_schema": "mineru-task-protocol.v2",
            "idempotency_key": self.key,
            "attempt_identity": "attempt-1",
            "fence_identity": "fence-1",
            "protocol_state": status,
            "error": None,
        }
        if status == "completed":
            digest = hashlib.sha256(b"artifact").hexdigest()
            payload.update(
                {
                    "result_artifact_schema": "mineru-retained-result.v1",
                    "result_artifact_sha256": digest,
                    "result_artifact_bytes": artifact_bytes,
                    "result_artifact_owner": canonical_result_owner_v2(
                        task_id="task-1",
                        artifact_sha256=digest,
                        artifact_byte_count=artifact_bytes,
                    ),
                }
            )
        return payload


if __name__ == "__main__":
    unittest.main()
